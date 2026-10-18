#!/usr/bin/env bash
set -euo pipefail

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ "$(systemctl --user show bountyradar.timer --property=LoadState --value)" != "not-found" ]]; then
    systemctl --user disable --now bountyradar.timer
fi
if [[ "$(systemctl --user show bountyradar.service --property=LoadState --value)" != "not-found" ]]; then
    systemctl --user stop bountyradar.service
fi
rm -f "$unit_dir/bountyradar.service" "$unit_dir/bountyradar.timer"
systemctl --user daemon-reload
echo 'BountyRadar user units removed. The .env and database were retained.'
