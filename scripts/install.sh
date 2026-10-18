#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
expected_dir="/home/james/bounty/bountyradar"

if [[ "$project_dir" != "$expected_dir" ]]; then
    echo "This unit expects $expected_dir; update its absolute paths before installing." >&2
    exit 1
fi
cd "$project_dir"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    echo "Missing .venv/bin/python; create the virtual environment first." >&2
    exit 1
fi
"$project_dir/.venv/bin/python" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
if [[ ! -f "$project_dir/.env" ]]; then
    echo "Missing .env; copy .env.example and configure it first." >&2
    exit 1
fi
"$project_dir/.venv/bin/python" -c 'import bountyradar, httpx, pydantic, pydantic_settings'
database_path="$("$project_dir/.venv/bin/python" - <<'PY'
import os
import sys
from pathlib import Path
from pydantic import ValidationError
from bountyradar.settings import Settings

# The scheduled service loads .env; shell-only overrides are not installed.
for name in list(os.environ):
    if name.startswith("BOUNTYRADAR_") or name in {"GITHUB_TOKEN", "EMAIL_ENABLED"}:
        del os.environ[name]
try:
    settings = Settings()
except ValidationError as exc:
    sys.exit(f"Invalid configuration: {exc.error_count()} field error(s)")
if not settings.github_token or not settings.github_token.get_secret_value().strip():
    sys.exit("GITHUB_TOKEN must be configured in .env for scheduled runs")
path = settings.database_path.resolve()
if not path.is_relative_to(Path("data").resolve()):
    sys.exit("database path must be under project data/")
print(path)
PY
)"
systemctl --user show-environment >/dev/null
service_state="$(systemctl --user show bountyradar.service --property=ActiveState --value)"
case "$service_state" in
    inactive|failed) ;;
    *) echo "Service is $service_state; stop it explicitly before refreshing units." >&2; exit 1 ;;
esac
systemd-analyze --user verify \
    "$project_dir/deployment/systemd/bountyradar.service" \
    "$project_dir/deployment/systemd/bountyradar.timer"

chmod 600 "$project_dir/.env"
mkdir -p "$project_dir/data" "$unit_dir"
chmod 700 "$project_dir/data"
if [[ -f "$database_path" ]]; then
    chmod 600 "$database_path"
fi
install -m 644 "$project_dir/deployment/systemd/bountyradar.service" "$unit_dir/"
install -m 644 "$project_dir/deployment/systemd/bountyradar.timer" "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable --now bountyradar.timer
systemctl --user status --no-pager bountyradar.timer
echo 'Logs: journalctl --user -u bountyradar.service -f'
echo 'Stop: systemctl --user disable --now bountyradar.timer'
