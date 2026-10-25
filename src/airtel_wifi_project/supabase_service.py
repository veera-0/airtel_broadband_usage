"""
Small Supabase client for the tracker. Talks to the Supabase REST API (PostgREST)
using only Python's standard library, so it installs nothing and works in Termux.

Configuration (from ~/.airfiber_env or the environment):
    SUPABASE_URL    https://YOUR_PROJECT.supabase.co   (no /rest/v1 at the end)
    SUPABASE_KEY    publishable key (sb_publishable_...) or legacy anon key
    SUPABASE_TABLE  optional, default "usage_readings"

Filters are PostgREST strings, e.g. {"id": "eq.5"} or {"recorded_at": "gte.2026-10-23"}.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class SupabaseError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SupabaseService:
    def __init__(self, url=None, key=None, table=None, pending_file=None, timeout=20):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_KEY", "")
        self.table = table or os.environ.get("SUPABASE_TABLE", "usage_readings")
        self.pending_file = pending_file     # where rows wait after a failed upload
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self.url and self.key)

    # ---- low level -----------------------------------------------------------
    def _headers(self, prefer=None):
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        # Legacy anon keys are JWTs and may also go in Authorization. The new sb_... keys
        # must be sent in the apikey header only (Bearer makes Supabase reject them).
        if self.key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + self.key
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(self, method, params=None, body=None, prefer=None):
        if not self.configured:
            raise SupabaseError(0, "SUPABASE_URL / SUPABASE_KEY are not set")
        url = f"{self.url}/rest/v1/{self.table}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(prefer), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise SupabaseError(e.code, e.read().decode("utf-8", "replace")[:300]) from None
        return json.loads(raw) if raw.strip() else None

    @staticmethod
    def _require_filters(filters, action):
        if not filters:
            raise ValueError(f"Refusing to {action} without a filter (it would affect every row).")

    # ---- write ---------------------------------------------------------------
    def insert(self, rows):
        """Insert one row (dict) or several (list of dicts). Returns the number inserted."""
        batch = rows if isinstance(rows, list) else [rows]
        self._request("POST", body=batch, prefer="return=minimal")
        return len(batch)

    def update(self, filters, values):
        """Update rows that match filters. Returns the updated rows."""
        self._require_filters(filters, "update")
        return self._request("PATCH", params=dict(filters), body=values,
                             prefer="return=representation") or []

    def delete(self, filters):
        """Delete rows that match filters. Returns the deleted rows."""
        self._require_filters(filters, "delete")
        return self._request("DELETE", params=dict(filters),
                             prefer="return=representation") or []

    # ---- read ----------------------------------------------------------------
    def select(self, filters=None, columns="*", order=None, limit=None):
        """Read rows. Example: select({"recorded_at": "gte.2026-10-23"}, order="recorded_at.asc")."""
        params = {"select": columns}
        params.update(filters or {})
        if order:
            params["order"] = order
        if limit:
            params["limit"] = limit
        return self._request("GET", params=params) or []

    def latest(self, n=1):
        return self.select(order="recorded_at.desc", limit=n)

    def select_all(self, filters=None, columns="*", order=None, page_size=500):
        """Read every matching row, page by page (the API returns at most ~1000 rows per call)."""
        rows, offset = [], 0
        while True:
            params = {"select": columns}
            params.update(filters or {})
            if order:
                params["order"] = order
            params["limit"] = page_size
            params["offset"] = offset
            page = self._request("GET", params=params) or []
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size

    # ---- insert with an offline queue -----------------------------------------
    def _load_pending(self):
        if not (self.pending_file and os.path.exists(self.pending_file)):
            return []
        with open(self.pending_file) as f:
            return [json.loads(line) for line in f if line.strip()]

    def last_pending(self):
        """Newest row still waiting to be uploaded, or None."""
        rows = self._load_pending()
        return rows[-1] if rows else None

    def _save_pending(self, rows):
        if not self.pending_file:
            return
        with open(self.pending_file, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def insert_queued(self, row):
        """Insert a reading together with any rows queued by earlier failures.
        Never raises: on failure everything is queued again for the next run.
        Returns the number of rows saved, 0 if queued, or None if Supabase is not configured."""
        if not self.configured:
            return None
        batch = self._load_pending() + [row]
        try:
            self.insert(batch)
        except SupabaseError as e:
            self._save_pending(batch)
            print(f"Supabase insert failed ({e}). Queued {len(batch)} row(s).", file=sys.stderr)
            return 0
        except (urllib.error.URLError, OSError) as e:
            self._save_pending(batch)
            print(f"Supabase unreachable ({e}). Queued {len(batch)} row(s).", file=sys.stderr)
            return 0
        if self.pending_file and os.path.exists(self.pending_file):
            os.remove(self.pending_file)
        return len(batch)