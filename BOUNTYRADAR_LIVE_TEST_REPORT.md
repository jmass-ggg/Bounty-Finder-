# BountyRadar live verification — 2026-10-18

## A. Overall status

**PARTIALLY WORKING. NO-GO for unattended verified bounty alerts.**

The laptop successfully authenticates to GitHub, discovers genuine candidates,
extracts provisional rewards, inspects issue evidence and saves findings in SQLite.
The enabled timer ran automatically after login. Recovery and notification behavior
pass offline tests using actual CLI/SMTP child processes with mocked external IO.
No authoritative funding provider is implemented, live Gmail delivery is unverified,
and the installed service still has obsolete timeout and exit-status settings.
Normal observed runs finish within 240 seconds but leave a substantial backlog.

Verification used the existing checkout and virtual environment. Existing untracked
files were preserved. No application defect requiring a source change was found.
No service was stopped, restarted, installed, enabled or disabled; no email was sent;
no commit or push occurred. Production SQLite was opened read-only for inspection.
All test writes used temporary databases. AGENTS.md, README.md,
STEP7_DEPLOYMENT_REPORT.md and PRODUCTION_READINESS_REPORT.md were reviewed.

## B. Commands and test results

Commands below ran from `/home/james/bounty/bountyradar`.

| Command | Exit | Result | Duration |
| --- | --- | --- | --- |
| `.venv/bin/python -m pytest -q` | 0 | 153 passed, 0 failed, 0 skipped; no warnings | 39.09s |
| `.venv/bin/ruff check .` | 0 | All checks passed | 0.008s |
| `.venv/bin/python -m pip check` | 0 | No broken requirements | 0.256s |
| `git diff --check` | 0 | No diagnostics | 0.002s |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | 0 | No diagnostics | 0.033s |
| `bash -n scripts/install.sh scripts/uninstall.sh` | 0 | No syntax errors | 0.002s |
| `.venv/bin/python -m bountyradar --help` | 0 | scan, run and email documented | 0.149s |
| `.venv/bin/python -m pytest -q -s tests/test_deployment.py::test_default_budgets_fit_interval_and_retain_dense_backlog` | 0 | 1 passed; no warnings | 0.87s |
| `.venv/bin/python /tmp/bountyradar-live-audit/live_probe.py > /tmp/bountyradar-live-audit/live_output.json` | 0 | Authenticated live discovery, resume, inspection and temporary persistence | 240.988s including waiting for production; about 50.6s network phase |
| `.venv/bin/python /tmp/bountyradar-live-audit/query_probe.py > /tmp/bountyradar-live-audit/query_output.json` | 0 | Four successful read-only query types | 8.519s |

The project is untracked, so ordinary `git diff --check` alone has limited coverage.
Each nonignored untracked file was additionally checked with
`git diff --no-index --check /dev/null <file>`: no diagnostics (aggregate exit 0).
CLI `scan --help`, `run --help` and `email test --help` also exited 0.
Initial sandboxed systemd reads failed with bus access denied; they were repeated
successfully after access changed. Those initial failures were environment restrictions.

Read-only deployment commands: `pwd`, `git status --short`,
`systemctl --user status bountyradar.service --no-pager`,
`systemctl --user status bountyradar.timer --no-pager`,
`systemctl --user list-timers --all --no-pager`,
`systemctl --user cat bountyradar.service bountyradar.timer`,
`systemctl --user show bountyradar.service --property=ActiveState,SubState,MainPID,TimeoutStartUSec,TimeoutStopUSec,SuccessExitStatus,Result,ExecMainStatus`,
`loginctl show-user james --property=Linger`, and Python process inspection.
Service status returns 3 while activating or failed; timer status, show, cat,
list-timers, loginctl, process inspection and Git inspection succeeded with exit 0.
Snapshots are time-dependent because the existing timer remains enabled.

### Secure configuration

Settings validated successfully. A nonempty GitHub token, SMTP username, secret and
recipient are configured. SMTP is `smtp.gmail.com:465`; production email is **enabled**.
The database resolves to `/home/james/bounty/bountyradar/data/bounties.db`.
`.env` permissions are 0600. The service process environment recognizes the expected
token/email aliases and database setting. No credential values are included here.

The actual Pydantic settings implementation was inspected, and constructing Settings
after setting both `EMAIL_ENABLED=false` and `BOUNTYRADAR_EMAIL_ENABLED=false`, plus
`BOUNTYRADAR_DATABASE_PATH=/tmp/bountyradar-live-audit/live.sqlite3`, confirmed that
environment values override `.env`. Both live probes asserted email was disabled;
they call discovery/verification/storage directly and never construct a mail sender.
Offline SMTP tests use placeholder credentials and fake transports.

`.env` and runtime databases are Git-ignored. Neither configured secret appeared in
tracked files or the last 500 service journal lines checked. This was a scoped check,
not an assertion about every historical log. Settings validation errors are reduced
to field-error counts by the CLI. Token validity was established by the live API test.

## C. Live GitHub results

The probe waited for production to finish naturally and checked service inactivity
before every HTTP request. Network work began at **09:53:41 Nepal time**
(`2026-10-18T04:08:41Z`). All requests used the configured token and GET endpoints.
`GET /user` and `GET /rate_limit` returned **200**. Initial available quota was
4,898/5,000 core requests and 30/30 search requests. No completed request returned
401, 403, 429 or a server error. No active rate-limit block was observed.

Live scan limits: one `bounty` term, monetary term `USD`, no label queries, default
24-hour created/updated and 72-hour older-issue funding lookbacks; six search attempts
and 18 seconds per scan, no retries, five-second HTTP timeout, maximum one-second
rate wait. Inspection had an 18-attempt/22-second shared budget. An outer 85-second
network deadline bounded the probe. These deliberately smaller budgets differ from
production defaults.

| Scan | Search attempts | Candidates returned | Unique stored total | Incomplete windows | Invalid items | Search wall time |
| --- | --- | --- | --- | --- | --- | --- |
| First | 6 | 510 | 510 | 4 | 0 | 13.035s |
| Resume | 6 | 600 | 1,110 | 4 | 0 | 13.727s |

Real search pagination traversed created-bounty pages 1–4, created-USD pages 1–6,
then updated-bounty pages 1–2. The second scanner instance resumed persisted progress.
The request ceiling deliberately left four windows unfinished; no completed checkpoint
was incorrectly advanced. Returned results are broad candidates, not 1,110 funded jobs.

The main probe recorded **24 completed HTTP responses**, all 200: two auth/quota,
12 search and ten inspection responses. The inspector counted 11 attempts: one was
cancelled by its shared deadline before recording a response. Thus its total was
25 attempts, including the cancelled inspection. The final completed main-probe
responses reported search remaining 18 and core remaining 4,888.

A separate bounded first-page probe checked every requested query type:

| Query | GitHub total_count | Returned / valid | HTTP | Duration including pacing |
| --- | --- | --- | --- | --- |
| `bounty is:issue is:open is:public created:>=2026-10-17T04:09:48Z` | 314 | 100 / 100 | 200 | 2.300s |
| `bounty is:issue is:open is:public updated:>=2026-10-17T04:09:48Z` | 943 | 100 / 100 | 200 | 1.730s |
| `"USD" is:issue is:open is:public updated:>=2026-10-17T04:09:48Z` | 1,704 | 100 / 100 | 200 | 2.886s |
| `label:bounty is:issue is:open is:public` | 4,262 | 100 / 100 | 200 | 1.577s |

These four requests took 8.519s, all reported `incomplete_results=false`, and the last
reported 26 search requests remaining after quota reset. Only page 1 was requested;
total_count is the server's query count, not the number downloaded. These 400 results
were not added to the 1,110 unique persisted count. Across both probes: 29 attempts,
28 completed HTTP 200 responses, one cancelled inspection.

### Genuine sampled results

| Repository / issue title | Issue URL | Possible reward | Application classification | Availability / score / alert |
| --- | --- | --- | --- | --- |
| NextDotID/proof_server — [Bounty] Build a frontend application for Next.ID | https://github.com/NextDotID/proof_server/issues/18 | USD 5,000 | REWARD_CLAIMED | AVAILABLE / 70 / no |
| kryptokrona/hugin-native — Help us translate Hugin Messenger! | https://github.com/kryptokrona/hugin-native/issues/13 | USD 20 | REWARD_CLAIMED | UNKNOWN / 45 / no |
| claude-builders-bounty/claude-builders-bounty — [BOUNTY $50] SKILL: Generate a structured CHANGELOG from git history | https://github.com/claude-builders-bounty/claude-builders-bounty/issues/1 | USD 50 | REWARD_CLAIMED | UNKNOWN / 50 / no |
| claude-builders-bounty/claude-builders-bounty — [BOUNTY $75] TEMPLATE: CLAUDE.md for a Next.js + SQLite SaaS project | https://github.com/claude-builders-bounty/claude-builders-bounty/issues/2 | USD 75 | UNAVAILABLE | UNKNOWN / 38 / no |
| claude-builders-bounty/claude-builders-bounty — [BOUNTY $100] HOOK: Pre-tool-use hook that blocks destructive bash commands | https://github.com/claude-builders-bounty/claude-builders-bounty/issues/3 | USD 100 | UNAVAILABLE | UNKNOWN / 43 / no |

All amounts above are published but **unconfirmed**. The $20 issue was correctly
rejected below the configured USD 25 minimum and detail inspection was skipped.
The $5,000 issue had detail, comments and timeline inspected, no assignees and no linked
competing PR found in checked sources. That does not establish that nobody is working
on it. The $50 issue exceeded the five-page comment limit and then the shared deadline.
The $75/$100 application classifications are conservative fallbacks because issue
detail was deferred after the deadline (`inspection.state` remained unknown): their
current open/closed and assignment status were **not independently confirmed**.
Their availability remains UNKNOWN. No sampled result was eligible for email.

Classification boundaries: confirmed funded rewards observed **0**; published reward
claims are unconfirmed; other search hits remain potential candidates until reviewed;
UNAVAILABLE/unknown or incomplete evidence prevents alerts. There is no concrete
funding adapter in the CLI and the configured provider trust list is empty. Changing
the trust list alone would not create a provider. Live currency samples were USD;
foreign-currency handling was verified offline without guessed conversions.

## D. SQLite, persistence and recovery

The production read-only snapshot initially contained **32,225 discovered issues**,
160 verification records, 30 pending search windows and no completed-scan checkpoint.
Later observation showed 180 verification records: 100 NOT_BOUNTY, 53 REWARD_CLAIMED,
24 UNAVAILABLE and three UNCERTAIN. The outbox was empty at that snapshot. Production
continued changing independently under its timer; these are snapshots, not fixed totals.

The isolated live database contains **1,110 unique issues and five verification
records**, valid UTC timestamps, four saved unfinished windows and no complete
checkpoint. `PRAGMA integrity_check` returned **ok**. Replaying the first scan's
candidates left its count at 510, demonstrating deduplication. Reopening RunState and
resuming discovery preserved the frozen window and pagination progress.

Offline coverage, rerun in the 153-test suite, verifies table creation, restart
persistence, queue rotation, atomic page/cursor commits, per-source checkpoints,
rollback on a forced trigger failure, lock exclusion, cancellation, HTTP closure and
restart after an interrupted scan. `test_sigterm_real_process_preserves_search_progress`
terminates only a test-owned CLI process, checks exit 143 and database health, and
retains unfinished search work. Continuation/backlog tests then verify resumption.
The load test stores 1,000 issues, retains the cursor, and checks a different set of
20 issues after restart (40 stored verification results total).

The implementation has checkpoint/search-progress tables and journal run summaries;
it does **not** maintain a separate append-only per-scan SQLite history table.

## E. Email verification

**Mocked notification logic: PASS. Live Gmail: NOT VERIFIED.**

The suite covers a new trusted fixture-funded USD 100 bounty, one accepted fake-SMTP
delivery, recorded sent state, no duplicate on a second run, a USD 150 increase,
SMTP authentication rejection, pre-send timeout, uncertain send, worker crash,
SIGTERM, abrupt parent death, repeated cancellation, worker reaping and restart
without automatic resend of an unknown outcome. Funding fixtures are not live funds.

The retained full-process test database was inspected independently:
`/tmp/pytest-of-james/pytest-0/test_full_cli_process_delivery0/scanner.db`.
It contains one issue, one verification result, one `new_qualified_bounty` event
`sent` with one attempt, and one `reward_increased` event `unknown_delivery` with
one attempt. Integrity is **ok**. No second delivery occurred after recovery.

The actual multipart message was rendered to
`/tmp/bountyradar-live-audit/mock_email.eml` using placeholder addresses. Subject:
`BountyRadar: USD 100 — Fix parser`. Plain-text and escaped HTML contain repository,
reward, GitHub URL, funding evidence and URL, availability, competition assessment,
UTC discovery timestamp and score. All requested fields were checked. SMTP fixtures
accepted the message; neither Gmail acceptance nor inbox delivery was tested.

Only after separate explicit approval, send one real test message using an isolated
outbox (this command sends real email to the configured recipient):

```bash
cd /home/james/bounty/bountyradar
EMAIL_ENABLED=true BOUNTYRADAR_EMAIL_ENABLED=true \
BOUNTYRADAR_DATABASE_PATH="$(mktemp -d /tmp/bountyradar-mail.XXXXXX)/mail.sqlite3" \
  .venv/bin/python -m bountyradar email test --json
```

Check the recipient inbox separately. Unknown delivery requires operator reconciliation;
do not blindly resend. SMTP cannot guarantee exactly-once delivery after an ambiguous
server response.

## F. systemd deployment

At 09:49 Nepal time the active service was PID 2620, executing
`/home/james/bounty/bountyradar/.venv/bin/python -m bountyradar run`.
It finished naturally; the timer subsequently ran PID 8704. At 09:54:59 the service
was failed with exit 2 because discovery was incomplete, while the enabled timer was
active/waiting for 09:55. Exit 2 here means deferred work, not authentication failure.

| Property | Installed | Repository |
| --- | --- | --- |
| Type | oneshot | oneshot |
| Python, working directory, .env | Correct project paths | Same |
| Startup timeout | **60 minutes** | **270 seconds** |
| Stop timeout | Default 90 seconds | 25 seconds |
| SuccessExitStatus | Empty | 143 |
| Timer calendar | `*:0/5` | Same |
| Persistent | true | true |
| Login startup | Enabled, WantedBy=timers.target | Same |
| Logging | stdout/stderr to journal | Same |

Installed service differs materially; timer differs only by a trailing blank line.
SIGTERM is the normal stop signal. Oneshot unit execution prevents timer overlap;
the application additionally locks each database path. Tests verify manual overlap
exclusion and lock release after interruption. Different database paths have separate
locks, which is why the live probe also checked production state explicitly.

`Linger=no`: login starts the user timer; logged-out scanning is not configured.
The observed boot/login run at 09:47 supports automatic resumption. Laptop sleep,
power-off and loss of network still delay work. Persistent catch-up is enabled.
Installer/uninstaller source was inspected: installer validates deployed settings,
refuses an active service, verifies units and protects data permissions; uninstall
retains data/config and refuses to remove units if stopping fails. Script tests use
copies and stub systemctl commands, not the installed deployment.

## G. Performance and failure recovery

Production defaults are application 240s, search 120s/60 attempts, inspection
90s/60 attempts and SMTP worker 20s; timer interval is 300s. Repository systemd
allows 270s startup and 25s shutdown. These are cooperative application deadlines
with an OS termination backstop, not hard real-time guarantees.

| Scenario | Observed result |
| --- | --- |
| Natural post-login run finishing 09:50:09 | 188.8s; 4,210 candidates; 30 incomplete; zero eligible/queued/sent |
| Natural run finishing 09:53:39 | 210.2s; 4,609 candidates; 29 incomplete; zero eligible/queued/sent |
| Bounded live scans | 13.035s and 13.727s search; six attempts each |
| Focused four-query live probe | 8.519s actual wall time; four HTTP 200 responses |
| Main probe network phase | About 50.6s including inspection; total 240.988s includes waiting for production |
| Mocked dense backlog | 199.8s **simulated pacing**, 59 searches, 40 inspections, 1,000 stored, 20 verified; test wall time 0.87s including resumed scan |

Scheduled journal summaries do not expose HTTP request counts; those counts were
measured only in the controlled probes and offline tests. No completed normal
full-backlog scan was demonstrated. The live test's smaller inspection budget
deliberately produced unknown/deferred results rather than overrunning its deadline.

Offline tests cover 403/429 cooldown persistence before cancellation, retry ceilings,
network/DNS failures, hung HTTP cancellation, scan deadline and signal restoration,
pending verification rotation, cursor retention and next-window catch-up. They pass
without infinite loops or lost committed work. Live rate limiting and network failure
were not induced; failure injection remained mocked as requested. No duplicate alerts
were observed. Large broad keyword results can outpace bounded inspection throughput;
request/time bounds do not prove eventual backlog drainage.

## H. Remaining blockers

1. No authoritative funding provider is implemented or connected to production CLI.
   New verified funded alerts cannot be produced as shipped.
2. Corrected repository service settings are not installed; activation needs approval.
3. Gmail delivery remains unverified and requires separately authorized testing.
4. Large discovery/inspection backlog remains unfinished. Current bounds protect
   runtime but completeness and revisit latency are not established.
5. Production email is already enabled. Review configuration and any pending outbox
   before approving deployment activation; do not infer readiness from an empty snapshot.

## I. Final verdict

| Question | Answer |
| --- | --- |
| Is the scanner working? | Yes for discovery and bounded partial processing; full backlog completion unproved. |
| Is GitHub authentication working? | Yes, live HTTP 200 with configured credentials. |
| Does it find real bounty candidates? | Yes, 1,110 unique candidates saved in isolated live scans. |
| Can it verify real funding? | No; no concrete authoritative provider. |
| Does SQLite save records? | Yes; live records, deduplication, durable cursor, integrity ok. |
| Does notification logic work? | Yes with trusted funding fixtures and mocked SMTP. |
| Has live Gmail delivery been verified? | No. |
| Is the five-minute timer active? | Yes, enabled and observed running/waiting. |
| Is the corrected service installed? | No; installed service still uses the old one-hour timeout. |
| Ready for unattended operation? | No for verified bounty alerts. Candidate collection is running with incomplete backlog. |

## J. Exact next actions

No activation commands below were executed. Explicit approval is required to change
services. To refresh the service for candidate-only operation, run each step only
after the preceding step succeeds:

```bash
cd /home/james/bounty/bountyradar
systemctl --user disable --now bountyradar.timer
systemctl --user stop bountyradar.service
test "$(systemctl --user show bountyradar.service --property=ActiveState --value)" = inactive
test "$(systemctl --user show bountyradar.service --property=MainPID --value)" = 0

# Privately set both email-enable aliases false; preserve credentials and database.
nano .env
# EMAIL_ENABLED=false
# BOUNTYRADAR_EMAIL_ENABLED=false

./scripts/install.sh
systemctl --user show bountyradar.service --property=TimeoutStartUSec,TimeoutStopUSec,SuccessExitStatus
systemctl --user status bountyradar.timer --no-pager
systemctl --user list-timers bountyradar.timer --no-pager
journalctl --user -u bountyradar.service -n 50 --no-pager
```

The installer enables the timer and persistent catch-up may run immediately.
Expected refreshed values: 4min30s startup, 25s stop and success status 143.
Verify any user-manager email overrides are also disabled before enabling scheduling.
This activates safer candidate collection, not funded-alert readiness.

For verified alerts, first establish a permitted documented authoritative funding
interface, implement and register its bounded adapter, and validate actual funded,
cancelled, paid and wrong-issue records. Then authorize the isolated Gmail test in
section E, confirm delivery and explicitly approve production email activation.
Monitor consecutive complete checkpoints and queue latency before claiming backlog
coverage. No package reinstallation is needed for this report-only verification.

Temporary evidence is retained in `/tmp/bountyradar-live-audit/`: live/query probe
scripts, response metadata, scan JSON, verification snapshots, redacted configuration,
deployment differences, check results, offline database summary and mock email.
These temporary files may be cleaned by the OS; this report preserves the key results.
