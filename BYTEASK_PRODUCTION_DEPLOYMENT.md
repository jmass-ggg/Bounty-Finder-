# ByteAsk production deployment handoff

Prepared by Codex on 2026-10-18. **Deployment is gated on a verified GitHub PR
merge. No PR or merged SHA is available yet: PR creation returned HTTP 403.**
Do not deploy the feature branch directly to work around that gate.
See [CODEX_PRODUCTION_RELEASE_REVIEW.md](CODEX_PRODUCTION_RELEASE_REVIEW.md).

Repository: https://github.com/jmass-ggg/Bounty-Finder-
Production checkout: `/home/james/bounty/bountyradar`.
Reviewed ByteAsk tip: `18c9d37dc6e1e89b978e20ca606c700261413b0b`.
Codex correctness/CI commit: `df8e6d7745c67d52352ef3fe98ce9ec9bdb05beb`.
PR URL: **not created**. Merged commit: **not available**.

## 1. Establish the release and preserve local work

After the PR is merged, record its URL and full merge SHA here. Run the following
in a dedicated Bash session, one block at a time, investigating failures.
Replace the placeholder only with the verified merged SHA.

```bash
set -euo pipefail
cd /home/james/bounty/bountyradar
merged_sha='REPLACE_WITH_VERIFIED_MERGED_SHA'
[[ "$merged_sha" =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse --show-toplevel)" = /home/james/bounty/bountyradar
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain)"
git fetch origin
git merge-base --is-ancestor "$merged_sha" origin/main
git show --stat --oneline "$merged_sha"
```

If dirty, stop and preserve/review the local changes separately. Do not reset,
clean, overwrite, auto-stash credentials or touch the parent repository. Confirm
that the PR is MERGED and its required checks passed, including the final head.

## 2. Inspect delivery state, stop safely and back up

Before stopping, read the configured database without creating an outbox or
activation boundary. Run this again after the service stops. It prints counts,
not payloads, addresses or credentials. Any sending/unknown outcome requires
inbox reconciliation before activation; never blindly retry it.

```bash
.venv/bin/python - <<'PY'
import sqlite3
from bountyradar.settings import Settings
p = Settings().database_path.resolve()
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as db:
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'notification_outbox' in names:
        print('outbox:', db.execute('SELECT state,count(*) FROM notification_outbox GROUP BY state').fetchall())
    if 'notification_delivery_attempts' in names:
        print('attempts:', db.execute('SELECT outcome,count(*) FROM notification_delivery_attempts GROUP BY outcome').fetchall())
PY
systemctl --user disable --now bountyradar.timer
systemctl --user stop bountyradar.service
test "$(systemctl --user show bountyradar.service -p ActiveState --value)" = inactive
test "$(systemctl --user show bountyradar.service -p MainPID --value)" = 0
umask 077
release_backup="$(mktemp -d /home/james/bountyradar-rollback-XXXXXXXX)"
export release_backup
git rev-parse HEAD > "$release_backup/previous-sha"
cp -p .env "$release_backup/private.env"
cp -p "$HOME/.config/systemd/user/bountyradar.service" "$release_backup/"
cp -p "$HOME/.config/systemd/user/bountyradar.timer" "$release_backup/"
.venv/bin/python - <<'PY'
import os, sqlite3
from pathlib import Path
from bountyradar.settings import Settings
p = Settings().database_path.resolve()
target = Path(os.environ['release_backup']) / 'bounties.db'
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dest:
        src.backup(dest)
        assert dest.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
print('Consistent SQLite backup verified')
PY
```

Keep WAL/SHM/lock files intact. Do not copy only a live main database or prune the
backlog. Save the original timer enablement/status and environment precedence
privately. Do not print `systemctl show-environment` or the private `.env`.
The previous release did not provide live unverified alerting; restoring it
restores candidate collection, not proven funded notifications.

## 3. Update the pinned checkout and private configuration

```bash
git merge --ff-only "$merged_sha"
test "$(git rev-parse HEAD)" = "$merged_sha"
.venv/bin/python -m pip install -e '.[dev]'
```

If the checkout has moved beyond the target, investigate instead of downgrading.
Retain the existing verified single recipient, credentials, database path and
currency allowlist. Atomically edit only the intended private configuration,
preserving mode 0600, to use:

```dotenv
BOUNTYRADAR_NOTIFICATION_MODE=include_unverified
BOUNTYRADAR_MIN_REWARD_USD=0
BOUNTYRADAR_EMAIL_ENABLED=true
BOUNTYRADAR_EMAIL_PER_SCAN_CAP=2
BOUNTYRADAR_EMAIL_DAILY_CAP=10
BOUNTYRADAR_NOTIFICATION_INITIAL_LOOKBACK_HOURS=24
```

Remove conflicting `EMAIL_ENABLED` overrides: that alias takes precedence.
Check shell/user-manager overrides privately against the scheduled environment.
Use `Settings()` to assert mode, floor, caps, email enabled, USD-only allowlist,
expected database path and that the recipient equals the previously verified
recipient; print only boolean results. Never accept an address from issue text.
Do not change recipient or broaden currencies as part of this deployment.

Inspect all existing pending/retryable rows before enabling: the activation
boundary does not remove already queued mail. Reconcile each unknown/sending
attempt; the internal `resolve_unknown` API is for evidence-based manual
resolution, not a blanket retry command. Do not delete delivery history.
The fixed initial boundary suppresses old promises lacking a new unedited
maintainer reward announcement. It does not roll forward after downtime.
Keep the caps and do not manually bulk-run `email send-pending`.

## 4. Validate, refresh units and observe scheduled work

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m pip check
git diff --check
bash -n scripts/install.sh scripts/uninstall.sh
systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer
./scripts/install.sh
systemctl --user show bountyradar.service -p WorkingDirectory -p ExecStart -p EnvironmentFiles -p TimeoutStartUSec -p TimeoutStopUSec -p SuccessExitStatus -p KillMode -p SendSIGKILL
systemctl --user is-enabled bountyradar.timer
systemctl --user status bountyradar.timer --no-pager
systemctl --user cat bountyradar.timer
systemctl --user list-timers --all --no-pager
journalctl --user -u bountyradar.service --since '30 minutes ago' --no-pager -o short-iso
```

Installer enables the timer; persistent/startup catch-up can run immediately.
Verify the original project venv/workdir and `.env`, 270s startup / 25s stop,
success exit 143, control-group killing, `OnStartupSec=30s`, `OnCalendar=*:0/5`,
`Persistent=true`. Do not enable linger without a separate request.

Observe at least three actual scheduled starts/completions. Record timestamps,
durations, overlap/timeout results, quotas and `scan_metrics`. Partial exit 2
is incomplete work, not full coverage success. Inspect independent recent and
historical cursor progress and fresh-candidate lag, not just timer syntax.
Inspect verified snapshots and outbox states read-only: CLAIMED/UNKNOWN must
not send; APPARENTLY_UNCLAIMED requires positive allowed reward and complete
inspection, followed by pre-send revalidation. Check daily/per-scan attempt
counts, pending rotation and no automatic unknown retry.

For a naturally eligible real alert, match durable attempt status and Message-ID
with Gmail receipt. Inspect its unverified warning, HTML GitHub button and plain
fallback; the target must be the exact canonical issue URL. Do not fabricate an
opportunity or send another synthetic message merely to obtain a pass. If no
candidate qualifies, record actual real-alert receipt as NOT VERIFIED.
Gmail/mobile push depends on the user's account and device settings.

## 5. Rollback and decision

On failure, disable the timer, stop/reap the scanner, retain current SQLite and
new delivery outcomes, save failure evidence, and restore the saved private
configuration and unit files with correct permissions. Select the saved source
SHA using a clean, reviewed checkout without reset/force or overwriting local
changes; reinstall its editable package, daemon-reload and enable the prior
timer only after verifying its configuration. Keep the live outbox: restoring an
old database over newly accepted SMTP mail can cause duplicate delivery.
Inspect schema compatibility before a source rollback; the new policy table is
additive, but notification history must remain authoritative.

Report GO / CONDITIONAL GO / NO-GO separately for scheduling, recent discovery,
claim filtering, real Gmail receipt and overall operation. Clearly retain the
limits: no funding guarantee/provider, GitHub indexing/quotas, historical queue
and storage growth, laptop power/network/user-manager availability. A successful
merge alone never proves production activation or real bounty receipt.
