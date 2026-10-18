# Step 7 Deployment Validation

Repository: `/home/james/bounty/bountyradar`

Validation date: 2026-10-17, Asia/Kathmandu (+05:45).
Read-only deployment snapshot taken between approximately 23:13 and 23:25.
Existing untracked implementation and production data were preserved.

## A. Executive Summary

**Step 7: PARTIALLY COMPLETE. Deployment verdict: NO-GO for production verified
bounty alerts.**

The interrupted deployment validation was resumed successfully. All 135 project
tests pass, including 24 deployment/end-to-end tests. Ruff, dependency checks,
CLI help, shell syntax, and repository systemd unit verification pass. Two
installer configuration defects were reproduced and fixed. Additional tests
prove actual CLI subprocess interruption, outbox recovery, overlap exclusion,
HTTP client closure, signal-handler restoration, persistent inspection cooldowns,
and backlog resumption.

Three release requirements remain:

1. No real authoritative funding provider is implemented or constructed by the
   production CLI. New verified bounty alerts cannot be generated as shipped.
2. The installed service still uses its old one-hour startup timeout. Corrected
   repository units have not been activated because service changes require
   user approval.
3. Cancellation cannot stop an already running synchronous SMTP worker.
   Coroutine deadlines do not establish a strict graceful process-exit bound
   during SMTP. Conservative outbox recovery is verified, but bounded delivery
   worker termination remains an implementation task.

The tested discovery/persistence path is suitable for further candidate-only
validation after manual deployment activation. This is not a claim of live
funding or Gmail integration success.

## B. Test Evidence

Commands below ran from the repository unless a different working directory is
specified. Pytest commands were offline: GitHub was mocked, mail delivery was
mocked, databases belonged to pytest temporary directories, and deployment
scripts used copied projects plus stub systemd commands. No test targeted the
installed service or production database.

### Automated Commands

Every pytest run below reported zero skipped tests and no warnings.

| Command | Exit | Passed | Failed | Duration / Result |
| --- | --- | --- | --- | --- |
| `.venv/bin/python -m pytest -q tests/test_deployment.py tests/test_deployment_scripts.py tests/test_end_to_end.py` (initial) | 0 | 17 | 0 | 59.19s |
| `.venv/bin/python -m pytest -q --durations=10` (before this session's fixes) | 0 | 128 | 0 | 85.80s |
| `.venv/bin/python -m pytest -q tests/test_deployment_scripts.py::test_install_requires_token_in_deployed_config tests/test_deployment_scripts.py::test_install_rejects_database_outside_service_writable_directory` (new defect reproductions) | 1 | 0 | 2 | 1.19s; installer incorrectly returned 0 for both unsafe configurations |
| `.venv/bin/python -m pytest -q tests/test_deployment.py tests/test_deployment_scripts.py tests/test_end_to_end.py --durations=10` (installer fix and lifecycle coverage) | 0 | 21 | 0 | 6.68s |
| Same command, after inspection-cooldown and active-SMTP recovery tests | 0 | 24 | 0 | 7.17s |
| `.venv/bin/python -m pytest -q -s tests/test_deployment.py::test_default_budgets_fit_interval_and_retain_dense_backlog` | 0 | 1 | 0 | 1.03s; simulation metrics below |
| `.venv/bin/python -m pytest -q` (final full regression) | 0 | 135 | 0 | 33.39s |
| `.venv/bin/python -m pytest -q tests/test_deployment.py tests/test_deployment_scripts.py tests/test_end_to_end.py` (final focused rerun) | 0 | 24 | 0 | 7.24s |
| `.venv/bin/ruff check .` (initial) | 0 | N/A | N/A | All checks passed |
| `.venv/bin/ruff check .` (after adding tests) | 1 | N/A | N/A | One E501: 102-character test function declaration; wrapped without behavioral change |
| `.venv/bin/ruff check .` (final) | 0 | N/A | N/A | All checks passed |
| `.venv/bin/python -m pip check` (initial and final) | 0 | N/A | N/A | No broken requirements found; approximately 0.2s each |
| `.venv/bin/python -m bountyradar --help` (initial and final) | 0 | N/A | N/A | Advertised `scan`, `email`, and `run`; approximately 0.1s each |
| `bash -n scripts/install.sh scripts/uninstall.sh` | 0 | N/A | N/A | No syntax errors |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | 0 | N/A | N/A | No diagnostics on initial and subsequent verification |
| `git diff --check` (initial and subsequent checks) | 0 | N/A | N/A | No tracked-file whitespace errors; files are untracked, so additional check below was necessary |

Non-pytest commands have no passed/failed/skipped test counts. Successful checks
above emitted no warnings. Dependencies were already installed; dependency
installation was not repeated.

### Offline Dry Run

`mktemp -d /tmp/bountyradar-step7.XXXXXX` exited 0 and created
`/tmp/bountyradar-step7.M7YgT3`.

From that directory, this command exited 0 in approximately 0.1s:

```bash
/home/james/bounty/bountyradar/.venv/bin/python /home/james/bounty/bountyradar/tests/deployment_probe.py dry-run /tmp/bountyradar-step7.M7YgT3/offline.sqlite3
```

It invokes the actual CLI with mocked HTTP and SMTP installed first:

```json
{"event": "run_complete", "candidates": 0, "high_priority_eligible": 0, "queued": 0, "sent": 0, "incomplete": 0, "delivery_failures": 0, "dry_run": true, "duration_seconds": 0.0, "timer_interval_exceeded": false}
```

The subprocess regression separately asserts that dry run creates no SQLite
database. It can create the lock file and parent directory.

### Read-Only Recovery and Deployment Commands

| Command | Exit | Result |
| --- | --- | --- |
| `pwd` | 0 | `/home/james/bounty/bountyradar` |
| `rg --files -g AGENTS.md -g '!**/.venv/**' -g '!**/.git/**' /home/james/bounty /home/james/.byteask` | 0 | Found repository `AGENTS.md`; read before edits |
| `git status --short` | 0 | Existing project source/configuration/tests untracked; preserved |
| `git branch --show-current` | 0 | `master` |
| `systemctl --user status bountyradar.service --no-pager` | 3 | Service activating/start, PID 111903, about 41 minutes elapsed at initial observation; status 3 reflects not-yet-active oneshot |
| `systemctl --user status bountyradar.timer --no-pager` | 0 | Timer enabled and active/running; service already starting |
| `systemctl --user list-timers --all` | 0 | BountyRadar timer present; no next activation while service remained starting |
| `ps -eo pid,ppid,etimes,stat,comm,args` | 0 | Process inventory; long output truncated, followed by targeted inspection |
| `pgrep -af 'python\|pytest'` | 0 | Existing production Python PID 111903; no orphaned pytest process at recovery |
| `ps -C python -C pytest -o pid,ppid,etimes,stat,args` | 0 | Only production process and this session's deployment pytest worker |
| `ps -C python -o pid,ppid,etimes,stat,args` | 0 | Only production process and this session's full pytest worker |
| `rg --files -g '!*.db' -g '!*.sqlite3' -g '!*.lock' -g '!*.pyc'` | 0 | Inspected actual source/test inventory rather than relying on Git diff |
| `systemctl --user cat bountyradar.service bountyradar.timer` | 0 | Installed service timeout 60min; installed timer every five minutes and persistent |
| `loginctl show-user james --property=Linger --property=State` | 0 | `State=active`, `Linger=no` |
| `systemctl --user show bountyradar.service --property=ActiveState --property=SubState --property=TimeoutStartUSec --property=TimeoutStopUSec --property=SuccessExitStatus --property=MainPID --property=ExecMainStartTimestamp --property=Result` (two observations) | 0 | Start timeout 1h; stop timeout 1min30s; no success exit-status override; PID 111903 still activating/start |
| `diff -u /home/james/.config/systemd/user/bountyradar.service deployment/systemd/bountyradar.service` | 1 | Expected difference: repository has 270s start, 25s stop and success status 143 |
| `diff -u /home/james/.config/systemd/user/bountyradar.timer deployment/systemd/bountyradar.timer` | 0 | Identical at inspection; later removed repository EOF blank line only |
| `systemd-analyze calendar --iterations=3 '*:0/5'` | 0 | Next occurrences: 23:25, 23:30, 23:35 +05:45 |
| `journalctl --user -u bountyradar.service --since '2026-10-17 22:32:15' --no-pager -o cat \| rg -c 'HTTP Request: GET https://api.github.com/'` | 0 | 1,324 logged GitHub GET requests during the observed old deployment run |
| `stat -c '%a %n' .env data data/bountyradar.sqlite3` | 1 | `.env` 600 and `data` 700; example/default database name absent |
| `find data -maxdepth 1 -type f -printf '%m %f\n'` | 0 | Actual `bounties.db`, WAL, SHM and lock files all mode 600; no data opened |
| `date --iso-8601=seconds` (two observations) | 0 | `2026-10-17T23:17:12+05:45`, `2026-10-17T23:24:47+05:45` |

The settings-only inspection below exited 0 and printed only nonsecret values:

```bash
.venv/bin/python -c 'import json; from bountyradar.settings import Settings; s=Settings(); print(json.dumps({name: getattr(s, name) for name in ("run_timeout_seconds", "scan_interval_seconds", "github_search_timeout_seconds", "github_max_search_requests_per_scan", "github_inspection_timeout_seconds", "github_max_inspection_requests_per_scan", "smtp_timeout_seconds", "email_enabled")}))'
```

Values: overall 240s; interval 300s; search 120s/60 attempts; inspection
90s/60 attempts; SMTP socket timeout 20s; email enabled. This does not establish
the settings loaded in the already running old Python process.

All cited file inspections were read-only, including `AGENTS.md`, implementation,
tests, scripts, README, example configuration and source feasibility notes.
An attempted read of `tests/conftest.py` found no such file.

### Untracked Whitespace Check

The following initial command exited 123 through `xargs`, reporting one extra
blank line at EOF in `deployment/systemd/bountyradar.timer`:

```bash
git ls-files --others --exclude-standard -z | xargs -0 -r -n 1 git diff --no-index --check /dev/null
```

That blank line was removed. A second invocation produced no diagnostics but
still exited 123 because no-index comparisons return 1 for different contents.
The following check handles that expected difference exit while rejecting
diagnostics/errors; it exited 0:

```bash
git ls-files --others --exclude-standard -z | while IFS= read -r -d '' path
do
    diagnostics="$(git diff --no-index --check /dev/null "$path" 2>&1)"
    result=$?
    if [[ -n "$diagnostics" || "$result" -gt 1 ]]
    then
        printf '%s\n' "$diagnostics"
        exit 1
    fi
done
```

## C. Implementation Fixes

Changes made during this resumed session:

| File | Change and verification |
| --- | --- |
| `scripts/install.sh` | Validate deployed `.env` independently of shell-only application overrides; require its nonempty GitHub token. Prevent shell settings from masking an unsafe deployed database path. Both defects failed before the fix and pass afterward. |
| `tests/test_deployment_scripts.py` | Add both installer regressions; adapt isolated fixture to store actual deployment settings in its `.env`; retain private permissions, secret-redaction, active-service protection, stop-failure and data-preservation assertions. |
| `tests/deployment_probe.py` | Exercise actual `cli.main()` with mocked HTTP/funding/SMTP; assert clients close and SIGTERM handlers restore after normal and interrupted runs; remove real search pacing only in this offline probe. |
| `tests/test_deployment.py` | Add full subprocess funded-alert/increase/interruption/recovery workflow, concurrent-process lock rejection, durable inspection cooldowns, active-SMTP ambiguous-outcome recovery, fresh issue catch-up, and second-scan dense-backlog continuation/fairness checks. |
| `tests/test_end_to_end.py` | Inject no-op pacing for mocked discovery. Preserve lifecycle assertions and remove approximately 49s of unnecessary real waiting. |
| `.env.example` | Expose overall/search/inspection timeout and inspection-attempt controls already implemented in source. Actual `.env` was untouched. |
| `deployment/systemd/bountyradar.timer` | Remove extra EOF blank line only; scheduling unchanged. |
| `README.md` | Correct queue bounds/recovery description; document private environment setup, token/Gmail setup, budgets, shutdown limits, safe updates, logs, uninstall and offline evidence. |
| `STEP7_DEPLOYMENT_REPORT.md` | This evidence and release decision. |

The previous session's CLI, discovery, inspector, settings, service timeout and
uninstaller improvements were retained and verified rather than rewritten.
No commit, push, real SMTP operation, GitHub modification, service activation,
or production database write was performed by this validation session.

## D. Deployment Matrix

| Requirement | Status | Evidence |
| --- | --- | --- |
| systemd oneshot service | PASS, configured | Repository and installed units both use `Type=oneshot`; unit verification passes |
| Five-minute timer | PASS, configured | `OnCalendar=*:0/5`; calendar parser confirms five-minute occurrences |
| Persistent scheduling | PASS, configured | `Persistent=true`; real suspend/reboot behavior not exercised |
| Environment loading | PASS offline; live activation pending | Correct `.env` path; installer validates deployed file and redacts invalid values |
| Timeout handling | PARTIAL | Default coroutine/HTTP deadlines tested; old installed unit still 1h; active SMTP cannot be cancelled |
| SIGTERM shutdown | PASS during async HTTP; SMTP limitation | Real subprocess exits 143 within test's 10s bound, before repository 25s stop deadline |
| Lock release | PASS | Reacquisition after timeout, exception and subprocess SIGTERM |
| Scan overlap prevention | PASS for same database | Second real CLI process returns 2/`another_scan_active`; `flock` regression; timer does not restart active service |
| SQLite recovery | PASS offline | Transaction rollback, unchanged incomplete checkpoint, reopened database quick/integrity checks |
| Email outbox recovery | PASS offline | Pending reward increase delivered once after subprocess restart; active SMTP becomes unknown after lease expiry and is not resent |
| Installation script | PASS isolated | Six script tests, disposable unit directory, stub-only systemctl/analyze, data permissions and preservation |
| Uninstallation script | PASS isolated | Retains `.env` and existing DB sentinel; failed service stop retains units |
| Login startup | CONFIGURED; physical login untested | Timer enabled and wanted by `timers.target`; user manager active; linger disabled |
| End-to-end testing | PASS offline | Actual CLI process lifecycle and existing injected GitHub/SMTP integration tests |

Service paths are correct for this checkout:

- ExecStart: `/home/james/bounty/bountyradar/.venv/bin/python -m bountyradar run`
- WorkingDirectory: `/home/james/bounty/bountyradar`
- EnvironmentFile: `/home/james/bounty/bountyradar/.env`
- Writable database directory: `/home/james/bounty/bountyradar/data`

No Docker, VPS, or system-wide service is required. The laptop must be awake
and online. An enabled user timer starts with the user manager at login;
logout operation is not configured (`Linger=no`). Systemd's
[timer documentation](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)
describes persistence and suppression of duplicate activation of an active unit.

## E. Performance

Default/source settings and the currently readable local configuration agree:

| Control | Value | Meaning |
| --- | --- | --- |
| Scheduled interval | 300s | Calendar schedule remains fixed in unit; changing the application interval alone does not change it |
| Overall run budget | 240s | Cooperative cancellation, not a hard process deadline during blocking synchronous work |
| Search time | 120s | Includes HTTP, retry backoff, pacing and rate waits |
| Search attempts | 60 | Retry attempts count toward cap |
| Inspection time | 90s | Shared across verification and delivery revalidation, starting with first inspection request |
| Inspection attempts | 60 | Shared inspector instance; retries/revalidation count |
| Main verification batch | 20 | Durable never-verified-first queue, followed by rotation |
| Eligible rechecks | 5 | Additional bounded batch, sharing the inspector budget |
| SMTP timeout | 20s | Per socket operation; no complete-send/DNS deadline |
| Repository systemd limits | 270s start / 25s stop | Approximately 295s forced-termination envelope; orderly SMTP shutdown is not guaranteed |

Normal mocked CLI scans finish in well under one second and within the configured
budget. Timeout tests use 0.05-0.1s budgets against intentionally hung operations,
not multi-minute sleeps. Search and inspection retries stop at their request
caps. Rate cooldown state is committed before cancelled waits for both 200
exhaustion headers and 429 responses; reopened scanners do not issue early HTTP.

The dense-backlog test uses fake pacing time and instantaneous HTTP responses.
The measured **simulation**, not live API throughput, reports:

```text
SIMULATED first scan: paced_seconds=199.8, searches=59, inspections=40, stored=1000, verified=20
```

It asserts both attempt caps, modeled time below 240s/300s, durable storage of
1,000 distinct candidates, only 20 verified results, an unchanged completion
checkpoint, and a remaining search continuation. A second invocation requests
the exact saved query/page, respects both caps, inspects a disjoint batch of 20,
and leaves 40 durable verification results with a valid database.

The fake clock models pacing only: it is not the real event loop clock enforcing
`asyncio.timeout`, and mocked responses include no latency or rate-limit cost.
Separate real-clock hung-request tests verify actual cancellation. These
results do not establish real-world requests/second or time to drain a live
GitHub backlog.

A separate deterministic orchestration test discovers an issue arriving while
the original window is unfinished: the second run finishes the frozen window,
and the third opens a new window from saved coverage and stores the new issue.
No per-issue latency bound is promised for continuously growing backlogs.

Observed old deployment: PID 111903 started at 22:32:15 and remained in
activating/start at approximately 23:24:47 (over 52 minutes). The journal counted
1,324 GitHub GETs since that start. Its installed unit still had a one-hour
startup timeout and default 90-second stop timeout. This is evidence of the old
deployment's problem, not a performance measurement of corrected source.
It was neither stopped nor restarted.

## F. Remaining Issues

| Severity | Issue | Required follow-up |
| --- | --- | --- |
| P0, product release blocker | No concrete authoritative funding provider or production CLI registration | Establish a permitted authoritative interface, implement adapter and integration, then test real funding semantics; do not trust reward text as funding |
| P1, deployment blocker | Installed service lacks corrected timeout/exit handling; old run remains active | User-approved stop and unit refresh using section I |
| P1, shutdown requirement gap | SMTP runs in an uncancellable worker thread; SIGTERM/global timeout can leave it running and release the application lock before that worker exits | Add bounded, cancellable delivery-worker lifecycle or equivalent absolute transport termination; test SIGTERM during actual blocked worker execution |
| P2, completeness/latency limitation | Search cannot guarantee discovery of an old never-seen issue funded only externally; growing discovery queues can postpone rechecks | Future provider discovery/update feed and measured backlog policy |
| P2, integration evidence gap | No live corrected-unit run, real funding integration, Gmail acceptance test, reboot/login or suspend/resume test | Explicitly approved controlled operational validation after blockers are addressed |
| Operational constraint | Linger disabled; sleeping/offline laptop cannot scan | Keep laptop awake/online and user logged in for this deployment model |

The SMTP limitation was reproduced deterministically: cancel the task while a
mock synchronous send waits on an event, observe that its worker remains active,
then release it explicitly. The durable row remains `sending`; after lease
expiry it becomes `unknown_delivery`, with no automatic resend. This protects
against duplicate mail but does not meet strict graceful shutdown or worker
overlap requirements. No expected-failure marker hides this gap.

Python's [runner documentation](https://docs.python.org/3.12/library/asyncio-runner.html)
also documents executor shutdown waiting. Fixing the worker lifecycle is a
separate implementation requirement; increasing systemd timeouts would not
prove bounded SMTP shutdown.

## G. Funding Provider

The blocker remains. `verification.py` defines the `FundingProvider` protocol,
but no concrete real provider exists. `cli._run()` accepts injection for tests
and integrations; normal `cli.main(["run"])` supplies none. Trust-list settings
alone do not instantiate an adapter.

GitHub discovery can fetch real candidate issues, and verification distinguishes
unverified reward claims from authoritative records. `FUNDED_EVIDENCE` requires
an exact issue link, configured trusted provider/HTTPS host, a current funded
record, and the ordinary availability/threshold/ranking gates. These gates were
not weakened.

Consequently, production cannot create new high-confidence verified alerts
from GitHub text alone. SMTP is configured as enabled locally, but that does not
supply funding proof. Manual test email or delivery of previously verified
outbox entries can still use SMTP; neither was attempted here.

An already discovered older issue can be reverified through the rotating queue
when a provider is injected, including when GitHub has not changed. The existing
offline end-to-end regression demonstrates this. A never-discovered older issue
with external-only new funding is not covered by that mechanism.

Mocked funding records used solely by the CLI subprocess probe demonstrate
orchestration and persistence, not real funding availability. Existing
`docs/phase7-sources.md` remains the source-feasibility record.

## H. Deployment Verdict

**NO-GO for production verified bounty alerts.**

All feasible offline validation and the confirmed installer corrections in this
session are complete. Release remains partially complete because funding
integration is absent, corrected units need approval to activate, and strict
SMTP graceful shutdown remains unresolved. Candidate discovery can proceed as
a limited deployment after explicit activation, with these limitations recorded.

## I. Manual Activation Commands

**Not executed. These commands require user approval because they stop scheduled
and active production work, replace installed units, and re-enable the timer.**
Review `.env` privately first: scheduled runs need its token and a database path
under project `data/`. Decide whether to leave SMTP enabled while the funding
blocker remains; preexisting outbox entries could still be delivered.

```bash
set -e
cd /home/james/bounty/bountyradar

# Stop scheduling first, then explicitly stop the old active process.
systemctl --user disable --now bountyradar.timer
systemctl --user stop bountyradar.service
systemctl --user show bountyradar.service --property=ActiveState --value

# Installer refuses an active/starting service and preserves existing data.
# It verifies/copies units, reloads the manager and enables/starts the timer.
./scripts/install.sh

systemctl --user cat bountyradar.service bountyradar.timer
systemctl --user show bountyradar.service \
  --property=TimeoutStartUSec --property=TimeoutStopUSec --property=SuccessExitStatus
systemctl --user status bountyradar.timer --no-pager
systemctl --user list-timers --all bountyradar.timer
journalctl --user -u bountyradar.service -n 100 --no-pager
```

Expected installed settings after approval: 270s start timeout, 25s stop timeout,
success exit status 143, every-five-minute persistent timer. A later source update
should also occur only after stopping scheduling and the active service; follow
the README update procedure. No linger change is needed for login-based startup.
Do not delete or replace the existing SQLite/WAL/SHM files.
