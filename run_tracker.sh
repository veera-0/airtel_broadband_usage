#!/data/data/com.termux/files/usr/bin/sh
# Runs the AirFiber tracker once. Meant for cron in Termux.
#
#   sh run_tracker.sh          -> log a reading   (default)
#   sh run_tracker.sh report   -> print the usage report
#
# Credentials are NOT stored in this repo. Create ~/.airfiber_env on the phone:
#   AIRFIBER_USER='admin'
#   AIRFIBER_PASS='your-router-password'
# (set another location with AIRFIBER_ENV_FILE=/path/to/file)

# Folder this script lives in. Keep it in the repo root, next to the src/ folder.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 1
ENV_FILE="${AIRFIBER_ENV_FILE:-$HOME/.airfiber_env}"

if [ -f "$ENV_FILE" ]; then
    . "$ENV_FILE"
fi

if [ -z "$AIRFIBER_PASS" ]; then
    echo "AIRFIBER_PASS is not set. Create $ENV_FILE with AIRFIBER_USER and AIRFIBER_PASS." >&2
    exit 1
fi

AIRFIBER_USER="${AIRFIBER_USER:-admin}"
export AIRFIBER_USER AIRFIBER_PASS

cd "$SCRIPT_DIR" || exit 1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
python src/airtel_wifi_project/airtel_data.py "${1:-log}"