# Airtel AirFiber usage tracker

This Python script reads the PPPoE uptime and byte counters from an Airtel
AirFiber router, then records a timestamped snapshot in a local CSV file. It
does **not** update Google Sheets, Excel, or another online spreadsheet.

The router must be reachable at the configured address, and the computer or
phone running the tracker must be connected to the router's Wi-Fi.

## Requirements

- Python 3.13 or newer (as declared in `pyproject.toml`)
- [uv](https://docs.astral.sh/uv/) on a computer, or Python and pip in Termux
- An Airtel AirFiber router whose admin page exposes the traffic counters used
  by the script

The project uses `python-dotenv` to read credentials from a `.env` file.

## Configure credentials

Create a file named `.env` in the project root (the same folder as
`pyproject.toml`):

```dotenv
ROUTER_IP=https://192.168.1.1
ROUTER_PASSWORD="your router admin password"
```

The router username is currently fixed to `admin` in
`src/airtel_wifi_project/airtel_data.py`. Set `ROUTER_IP` to the router's
reachable HTTPS address if it differs from the default. Keep `.env` private;
it is ignored by Git. The script also accepts these variables from the process
environment, which take precedence over values in `.env`.

The tracker does not print the password. If `ROUTER_PASSWORD` is missing, the
current code falls back to `admin`; configure the real password rather than
relying on that fallback.

## Run on Windows

From the project root, install/synchronize dependencies and take a reading:

```powershell
uv sync
uv run python src/airtel_wifi_project/airtel_data.py log
```

To show the report:

```powershell
uv run python src/airtel_wifi_project/airtel_data.py report
```

If already in `src/airtel_wifi_project`, the equivalent commands are:

```powershell
uv run python airtel_data.py log
uv run python airtel_data.py report
```

Use these commands rather than the `airtel-wifi-project` console command;
that package entry point currently prints a placeholder greeting and does not
run the tracker.

## Run in Termux every two hours

Install Termux and the Termux:API Android app from the same trusted source.
Open Termux and install the required packages:

```sh
pkg update
pkg install python python-pip termux-api
```

Check that `python --version` reports Python 3.13 or newer. Copy or clone this
project to `~/airtel-wifi-project`, then from that directory create an isolated
environment and install the project:

```sh
cd ~/airtel-wifi-project
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Create `~/airtel-wifi-project/.env` with the credentials shown above, using the
router address reachable from the phone. Restrict access to the file:

```sh
chmod 600 .env
```

Test a reading manually while the phone is connected to the router's Wi-Fi:

```sh
.venv/bin/python src/airtel_wifi_project/airtel_data.py log
```

Create a small wrapper for scheduled runs:

```sh
mkdir -p ~/bin
cat > ~/bin/airtel-log.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$HOME/airtel-wifi-project"
exec >> "$HOME/airtel-wifi-project/termux-run.log" 2>&1
date '+%F %T'
exec "$HOME/airtel-wifi-project/.venv/bin/python" \
  "$HOME/airtel-wifi-project/src/airtel_wifi_project/airtel_data.py" log
EOF
chmod 700 ~/bin/airtel-log.sh
```

Test the wrapper, then register a persistent two-hour Termux job:

```sh
~/bin/airtel-log.sh
termux-job-scheduler --script "$HOME/bin/airtel-log.sh" \
  --job-id 1 --period-ms 7200000 --network any --persisted true
```

Android may delay a periodic job because of battery optimization; it is not
guaranteed to run at an exact two-hour interval. Allow Termux to run in the
background (for example, set its battery usage to Unrestricted) and keep the
phone connected to the router's Wi-Fi. Review scheduled-run output in
`~/airtel-wifi-project/termux-run.log`.

## Output and reports

Each successful `log` run appends a row to
`src/airtel_wifi_project/usage_log.csv`, beside the tracker script. The CSV
contains the timestamp, router uptime, cumulative downloaded/uploaded GB, total
GB, estimated use since the previous reading, and a note.

The `report` command summarizes daily usage and estimates usage for the current
billing cycle. The cycle settings are constants near the top of the script:

- `CAP_GB`: monthly data cap (default `1000`)
- `CYCLE_START_DAY`: billing-cycle renewal day (default `23`; values are capped
  at day 28)
- `USED_BEFORE_TRACKING_GB`: estimated use before the first saved reading
- `WARN_PERCENT`: usage percentage at which the report adds a note

The router counters are cumulative. The script calculates usage by comparing
successive readings; after a router/counter reset, it counts from the new
counters and marks the row. Usage during missed intervals or before a reset
cannot be recovered, so run the tracker regularly. The report attributes the
usage between readings to the date of the later reading.

After two consecutive failed logins the script stops making login attempts to
avoid triggering the router's lockout. Check the credentials and whether
another router admin session is active before resetting the failure counter in
`src/airtel_wifi_project/login_failures.txt`.

## Router compatibility note

The script connects to the router's HTTPS admin page using a self-signed
certificate and permits legacy TLS settings for compatibility. Its TLS
configuration does not verify the router certificate. Use it only on a trusted
home network; do not expose the router admin page to the public internet.

## CRON in Termux
1. clone the repo: git clone https://github.com/veera-0/airtel_broadband_usage.git

2. Update the phone

cd ~/YOUR_PROJECT_FOLDER/YOUR_REPO_FOLDER
git pull

3. Create the password file (outside the repo)

cat > ~/.airfiber_env <<'EOF'
AIRFIBER_USER='admin'
AIRFIBER_PASS='YOUR_ROUTER_PASSWORD'
EOF
chmod 600 ~/.airfiber_env

4. Test it

sh run_tracker.sh
sh run_tracker.sh report

Run these from the repo folder. You should see a timestamp line, then “Logged: ...”.

5. Switch cron to the new script
Your current cron entry points to the old ~/run_tracker.sh. Replace it, using your real folder path in the new line:

crontab -l | grep -v run_tracker | crontab - ; (crontab -l 2>/dev/null; echo "0 */2 * * * sh $PWD/run_tracker.sh >> $HOME/tracker_run.log 2>&1") | crontab - && crontab -l

Run this from inside the repo folder, because $PWD fills in the path for you. The last command should show a line ending in .../run_tracker.sh. Then delete the old copy with rm ~/run_tracker.sh