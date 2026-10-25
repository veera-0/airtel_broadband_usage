#!/usr/bin/env python3
"""
Airtel AirFiber data tracker (router side). Python 3.8+, standard library only.

  python airfiber_tracker.py log        -> log in, read PPPoE counters, store the reading in Supabase
  python airfiber_tracker.py report     -> usage report built from the Supabase rows
  python airfiber_tracker.py db-latest  -> last 5 rows stored in Supabase

Supabase is the only data store. If it is unreachable, readings wait in
pending_supabase.jsonl (a temporary buffer) and are uploaded on the next run.

Usage between two readings is the difference of the byte counters. If a counter
goes down (router reboot / reconnect / Reset button) the new value is counted
from zero. Anything used before the reset and after the last reading is lost,
so log every 1-2 hours.
"""
import hashlib, http.client, json, os, re, ssl, sys, warnings
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

try:
    from .supabase_service import SupabaseError, SupabaseService
except ImportError:                      # run directly as a script
    from supabase_service import SupabaseError, SupabaseService

ROUTER = "https://192.168.1.1"
USERNAME = os.environ.get("AIRFIBER_USER", "admin")        # <-- your router username
PASSWORD = os.environ.get("AIRFIBER_PASS", "CHANGE_ME")    # <-- your router password

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL_FILE = os.path.join(HERE, "login_failures.txt")
supabase = SupabaseService(pending_file=os.path.join(HERE, "pending_supabase.jsonl"))
MAX_FAILURES = 2   # the router locks you out after 3 wrong attempts, so stop at 2

# ---- Your plan -------------------------------------------------------------
CAP_GB = 1000                  # monthly data cap
CYCLE_START_DAY = 23           # day of the month your cap renews (1-28): check your Airtel bill
USED_BEFORE_TRACKING_GB = 0.0  # data already used this cycle before the first log (from Airtel, or an estimate)
WARN_PERCENT = 80              # flag when this much of the cap is used
# -----------------------------------------------------------------------------


LOGIN_PATH = "/cgi-bin/login_advance.cgi"
LOGOUT_PATH = "/cgi-bin/logout_advance.cgi"
DATA_PATH = "/cgi-bin/traffic_wan_frame2.cgi"
DATA_REFERER = ROUTER + "/cgi-bin/tabFW.cgi?tabJson=../html/pages/systemMonitoring/trafficStatus/tab.json&&tabIndex=0"
COOKIE_NAME = "COOKIE_SESSION_KEY"

# The router uses a self-signed certificate and an old-style TLS setup.
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
ctx.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)  # router closes without close_notify
try:
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    with warnings.catch_warnings():       # newer Pythons warn that TLSv1 is deprecated
        warnings.simplefilter("ignore")
        ctx.minimum_version = ssl.TLSVersion.TLSv1
except Exception:
    pass


class Session:
    def __init__(self):
        self.cookies = {}

    def _open(self, path, data=None, referer=None):
        req = urllib.request.Request(ROUTER + path, data=data)
        req.add_header("User-Agent", "Mozilla/5.0")
        if referer:
            req.add_header("Referer", referer)
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        resp = opener.open(req, timeout=20)
        for header in resp.headers.get_all("Set-Cookie") or []:
            m = re.match(r"\s*([^=;\s]+)=([^;]*)", header)
            if m:
                self.cookies[m.group(1)] = m.group(2)
        chunks = []
        try:
            while True:
                part = resp.read(4096)
                if not part:
                    break
                chunks.append(part)
        except (ssl.SSLError, http.client.IncompleteRead, ConnectionError) as e:
            if isinstance(e, http.client.IncompleteRead):
                chunks.append(e.partial)
            if not chunks:
                raise
        return b"".join(chunks).decode("latin-1", errors="replace")

    def get(self, path, referer=None):
        return self._open(path, None, referer)

    def post(self, path, form, referer=None):
        return self._open(path, urllib.parse.urlencode(form).encode(), referer)


def js_encode_uri_component(text):
    """Same as the login page: encodeURIComponent, then lowercase the %XX hex."""
    enc = urllib.parse.quote(text, safe="-_.!~*'()")
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), enc)


def password_hash(password, sid):
    return hashlib.md5(f"{js_encode_uri_component(password)}:{sid}".encode()).hexdigest()


def failure_count():
    try:
        return int(open(FAIL_FILE).read().strip() or 0)
    except (OSError, ValueError):
        return 0


def login(s):
    fails = failure_count()
    if fails >= MAX_FAILURES:
        sys.exit(f"Stopped: {fails} failed logins in a row. Check the password in this script, "
                 f"log in once by hand to confirm it, then delete {FAIL_FILE} to resume.")
    page = s.get(LOGIN_PATH)
    m = re.search(r"var\s+sid\s*=\s*'([^']+)'", page)
    if not m:
        raise RuntimeError("Could not find the sid on the login page (is someone else logged in, "
                           "or is the router busy?)")
    sid = m.group(1)
    form = [
        ("Loginuser", USERNAME),
        ("fake_username", ""),
        ("fake_pass", ""),
        ("LoginPasswordValue", password_hash(PASSWORD, sid)),
        ("submitValue", "1"),
        ("LoginSidValue", sid),
        ("Prestige_Login", "Login"),
    ]
    body = s.post(LOGIN_PATH, form, referer=ROUTER + LOGIN_PATH)
    if "indexmain.cgi" in body and "LoginPasswordValue" not in body:
        if os.path.exists(FAIL_FILE):
            os.remove(FAIL_FILE)
        return
    with open(FAIL_FILE, "w") as f:
        f.write(str(fails + 1))
    raise RuntimeError("Login was not accepted (wrong username/password, or another admin session is active).")


def grab(html, element_id):
    m = re.search(r'id="%s"[^>]*>\s*([^<]+?)\s*<' % element_id, html)
    if not m:
        raise RuntimeError(f"Could not find {element_id} on the traffic page")
    return m.group(1).strip()


def read_counters():
    s = Session()
    login(s)
    try:
        html = s.get(DATA_PATH, referer=DATA_REFERER)
        uptime = grab(html, "If_Conn_Uptime_0")         # PPPoE row, d:h:m:s
        sent = int(grab(html, "SendBytes_count_0"))
        recv = int(grab(html, "RecvBytes_counts_0"))
    finally:
        try:                                            # free the admin session (best effort)
            s.get(LOGOUT_PATH)
        except Exception:
            pass
    d, h, m, sec = (int(x) for x in uptime.split(":"))
    return ((d * 24 + h) * 60 + m) * 60 + sec, sent, recv


GB = 1e9   # decimal GB (1 GB = 1,000,000,000 bytes). Use 1024**3 if you prefer GiB.


def fmt_uptime(sec):
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}d {h:02d}h {m:02d}m {s:02d}s"


def usage_between(prev, cur):
    """prev/cur = (uptime_days, downloaded_GB, uploaded_GB).
    Returns (down_GB, up_GB, was_reset). After a reset the counters start from zero."""
    reset = cur[0] < prev[0] or cur[1] < prev[1] or cur[2] < prev[2]
    if reset:
        return cur[1], cur[2], True
    return cur[1] - prev[1], cur[2] - prev[2], False


def parse_ts(text):
    """Supabase timestamp -> datetime in this device's local timezone."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()


def valid(r):
    return all(r.get(k) is not None for k in ("recorded_at", "uptime_days", "downloaded_gb", "uploaded_gb"))


def row_values(r):
    return (float(r["uptime_days"]), float(r["downloaded_gb"]), float(r["uploaded_gb"]))


def require_db():
    if not supabase.configured:
        sys.exit("SUPABASE_URL / SUPABASE_KEY are not set (see ~/.airfiber_env). "
                 "Supabase is the only data store, so it is required.")


def previous_reading():
    """The last stored reading: the newest queued row if offline rows are waiting,
    otherwise the newest valid row in Supabase. Returns (row or None, error text or None)."""
    queued = supabase.last_pending()
    if queued:
        return queued, None
    try:
        for r in supabase.latest(5):
            if valid(r):
                return r, None
        return None, None
    except (SupabaseError, urllib.error.URLError, OSError) as e:
        return None, str(e)


def do_log():
    require_db()
    uptime_sec, sent, recv = read_counters()
    cur = (round(uptime_sec / 86400, 4), round(recv / GB, 4), round(sent / GB, 4))

    prev, err = previous_reading()
    used, note = 0.0, None
    if prev:
        d_dn, d_up, reset = usage_between(row_values(prev), cur)
        used = d_dn + d_up
        if reset:
            note = "counter reset (usage before reset not counted)"
    elif err:
        used, note = None, "previous reading unavailable"

    now = datetime.now().astimezone()
    total = round(cur[1] + cur[2], 4)
    used_text = "unknown (database unreachable)" if used is None else f"{used:.3f} GB"
    print(f"Reading: {fmt_uptime(uptime_sec)} up | downloaded {cur[1]:.2f} GB | "
          f"uploaded {cur[2]:.2f} GB | used since last reading {used_text}")
    saved = supabase.insert_queued({
        "recorded_at": now.isoformat(timespec="seconds"),
        "uptime": fmt_uptime(uptime_sec),
        "uptime_days": cur[0],
        "downloaded_gb": cur[1],
        "uploaded_gb": cur[2],
        "total_gb": total,
        "used_since_last_gb": None if used is None else round(used, 4),
        "note": note,
    })
    if saved:
        print(f"Supabase: saved {saved} row(s)")
    else:
        print("Not saved to Supabase yet. It is queued locally and will upload on the next run.")


def cycle_start(d):
    day = min(CYCLE_START_DAY, 28)
    if d.day >= day:
        return d.replace(day=day)
    return (d.replace(day=1) - timedelta(days=1)).replace(day=day)


def next_cycle_start(start):
    day = min(CYCLE_START_DAY, 28)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=day)


def do_db_latest():
    if not supabase.configured:
        sys.exit("SUPABASE_URL / SUPABASE_KEY are not set (see ~/.airfiber_env).")
    rows = supabase.latest(5)
    if not rows:
        print("No rows in Supabase yet.")
    for r in rows:
        print(f"{r['recorded_at']}  up {r['uptime']}  down {r['downloaded_gb']} GB  "
              f"up {r['uploaded_gb']} GB  used {r['used_since_last_gb']} GB")


def do_report():
    require_db()
    rows = [r for r in supabase.select_all(columns="recorded_at,uptime_days,downloaded_gb,uploaded_gb",
                                           order="recorded_at.asc") if valid(r)]
    if not rows:
        sys.exit("No readings in Supabase yet. Run the 'log' command first.")
    for r in rows:
        r["_t"] = parse_ts(r["recorded_at"])

    daily = defaultdict(lambda: [0.0, 0.0])   # date -> [down, up]
    for prev, cur in zip(rows, rows[1:]):
        d_dn, d_up, _ = usage_between(row_values(prev), row_values(cur))
        day = cur["_t"].date()
        daily[day][0] += d_dn
        daily[day][1] += d_up

    print("Last 14 days (GB)")
    print(f"{'Date':<12}{'Down':>9}{'Up':>9}{'Total':>9}")
    cycles = defaultdict(float)               # cycle start date -> GB used
    for day in sorted(daily):
        dn, up = daily[day]
        cycles[cycle_start(day)] += dn + up
    for day in sorted(daily)[-14:]:
        dn, up = daily[day]
        print(f"{str(day):<12}{dn:>9.2f}{up:>9.2f}{dn + up:>9.2f}")

    cycles[cycle_start(rows[0]["_t"].date())] += USED_BEFORE_TRACKING_GB

    now = datetime.now()
    start = cycle_start(now.date())
    end = next_cycle_start(start)
    total_days = (end - start).days
    elapsed = (now - datetime.combine(start, dtime())).total_seconds() / 86400
    left = max(total_days - elapsed, 0.01)
    used = cycles.get(start, 0.0)
    remaining = CAP_GB - used

    print(f"\nCurrent cycle: {start} to {end - timedelta(days=1)}  (renews {end})")
    print(f"  Used:       {used:8.1f} GB of {CAP_GB} GB  ({used / CAP_GB * 100:.1f}%)")
    print(f"  Remaining:  {remaining:8.1f} GB")
    print(f"  Days:       {elapsed:.1f} elapsed, {left:.1f} left")
    if elapsed >= 1:
        avg = used / elapsed
        projected = avg * total_days
        print(f"  Your pace:  {avg:8.1f} GB/day  ->  projected {projected:.0f} GB by renewal")
        if projected > CAP_GB:
            print(f"  WARNING: at this pace you pass the cap around day "
                  f"{CAP_GB / avg:.0f} of the cycle.")
    else:
        print("  Your pace:  too early to project (needs about a day of data)")
    print(f"  To stay under the cap: {max(remaining, 0) / left:.1f} GB/day for the rest of the cycle")
    if used / CAP_GB * 100 >= WARN_PERCENT:
        print(f"  NOTE: {WARN_PERCENT}% of the cap is already used.")

    older = sorted(c for c in cycles if c != start)
    if older:
        print("\nEarlier cycles (GB):")
        for c in older:
            print(f"  {c}: {cycles[c]:.1f}")
    print("\n(Counts download + upload. Gaps from missed runs or resets are not included.)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {"log": do_log, "report": do_report, "db-latest": do_db_latest}
    if cmd not in commands:
        sys.exit(__doc__)
    try:
        commands[cmd]()
    except (urllib.error.URLError, ssl.SSLError, OSError, RuntimeError, SupabaseError) as e:
        sys.exit(f"Error: {e}")