# Production Readiness Report

Repository: `/home/james/bounty/bountyradar`

Validation date: 2026-10-17, Asia/Kathmandu (+05:45).

## 1. Decision

**NO-GO for production verified monetary bounty alerts. Work is partially complete.**

The SMTP shutdown blocker is fixed and regression-tested. The final complete
suite passes: **153 passed in 39.74s**. The final SMTP/deployment/end-to-end suite
passes: **42 passed in 13.18s**. No test was skipped or reported a warning.

Two release blockers remain: no verified real funding provider is connected,
and corrected systemd units have not been activated. Candidate-only operation
is a conditional option after explicit user-approved activation, preferably
with email disabled. It is not a substitute for the missing funded-alert feature.

No production service was stopped, restarted, enabled, disabled or reinstalled.
No production database was opened or changed by this validation. Existing
uncommitted/untracked files were preserved. Tests used temporary databases,
mocked GitHub/SMTP and fictional funding records. No real email was sent by
the validation, and no GitHub issue was modified. The already enabled timer
continued its independent scheduled activity.

## 2. Implementation and Defects Fixed

| Files | Change |
| --- | --- |
| `src/bountyradar/smtp_process.py` (new) | Async supervisor starts one isolated Python worker per message; credentials/message travel through stdin. Absolute deadline covers DNS, authentication, send and disconnect. Timeout/crash/uncertain IPC becomes an unknown outcome. Worker is killed and awaited before returning or propagating cancellation. Startup is shielded against losing a just-created child; repeated cancellation cannot bypass cleanup. |
| `src/bountyradar/smtp_worker.py` (new) | Worker uses the existing synchronous Gmail backend and email format. Linux parent-death SIGKILL protection plus a parent-PID race check prevents an abandoned worker from continuing delivery after scanner death. Worker has no SQLite access. |
| `src/bountyradar/notification.py` | `MailTransport.send` is now async; synchronous `GmailSMTPTransport` is retained as the isolated backend with existing authentication/error classification. |
| `src/bountyradar/delivery.py` | Removed executor-thread delivery. Cancellation persists `unknown_delivery` after transport cleanup, then propagates. Ambiguous acceptance is never automatically retried. |
| `src/bountyradar/cli.py` | Normal `run`, `email send-pending` and explicit `email test` construct the supervised transport. Existing scanner SIGTERM handling and run lock remain in place. |
| `src/bountyradar/settings.py`, `.env.example` | Added `BOUNTYRADAR_SMTP_DELIVERY_TIMEOUT_SECONDS`, default 20, finite positive maximum 240. Socket timeout remains separate. |
| `tests/smtp_worker_probe.py`, `tests/test_smtp_process.py` (new) | Actual child processes with mocked DNS/SMTP exercise success, blocked operations, timeout, crash, repeated cancellation, scanner interruption, abrupt parent death and duplicate prevention. |
| `tests/deployment_probe.py`, `tests/test_deployment.py`, `tests/test_end_to_end.py`, `tests/test_email.py` | Updated async transport fixtures; added real worker orchestration/recovery coverage and invalid/unavailable funding pipeline cases. No assertion was weakened to permit false funding. |
| `README.md`, `docs/production-funding-review.md` (new), this report | Documented current lifecycle, conservative recovery, provider feasibility and approval-only activation. Historical Step 7 report retained. |

The confirmed defect was that cancelling `asyncio.to_thread()` did not stop its
SMTP thread, so scanner lock/resource cleanup could precede the end of an
uncontrolled send. Production no longer uses that thread delivery path.

## 3. Executed Test Evidence

Commands ran from the repository using `.venv`, unless indicated otherwise.
The first two incremental runs preceded the last added tests; the final runs
cover the completed implementation.

| Command | Exit | Passed / Failed / Skipped | Duration or result |
| --- | --- | --- | --- |
| `.venv/bin/python -m pytest -q tests/test_smtp_process.py tests/test_deployment.py tests/test_end_to_end.py tests/test_email.py` | 0 | 54 / 0 / 0 | 34.68s |
| `.venv/bin/python -m pytest -q tests/test_smtp_process.py tests/test_deployment.py tests/test_deployment_scripts.py` | 0 | 33 / 0 / 0 | 11.20s |
| `.venv/bin/python -m pytest -q tests/test_smtp_process.py tests/test_end_to_end.py` | 0 | 20 / 0 / 0 | 7.26s |
| `.venv/bin/python -m pytest -q` | 0 | 153 / 0 / 0 | 39.74s |
| `.venv/bin/python -m pytest -q tests/test_smtp_process.py tests/test_deployment.py tests/test_deployment_scripts.py tests/test_end_to_end.py` | 0 | 42 / 0 / 0 | 13.18s |
| `.venv/bin/ruff check .` | 0 | N/A | All checks passed; also passed earlier incremental check |
| `.venv/bin/python -m pip check` | 0 | N/A | No broken requirements found |
| `git diff --check` | 0 | N/A | No diagnostics; supplemented for untracked files below |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | 0 | N/A | No diagnostics |
| `.venv/bin/python -m bountyradar --help` | 0 | N/A | `scan`, `email`, `run` available; no network/config loading |
| `bash -n scripts/install.sh scripts/uninstall.sh` | 0 | N/A | No syntax diagnostics |

No warnings were reported by these checks. Since the project is untracked,
ordinary `git diff --check` alone would be insufficient. This additional check
ran with exit 0 and no diagnostics:

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

Read-only provider probe, attempted twice during the investigation with the
same result:

```bash
curl --connect-timeout 5 --max-time 10 --fail --silent --show-error \
  --output /dev/null --write-out 'HTTP %{http_code}\n' \
  'https://api.bounties.network/bounty?limit=1'
```

Exit 6, approximately 0.1s, DNS resolution failure, HTTP 000. This is an
environment-specific failure, not evidence that the service is globally dead.
Official documentation was researched through the browser; no funding API
response was successfully validated.

## 4. SMTP Shutdown Verification

Every PASS below refers to offline regression evidence, not a Gmail smoke test.

| Requirement | Status | Evidence |
| --- | --- | --- |
| Bounded blocked DNS and send | PASS | `test_absolute_delivery_deadline_kills_worker` tests both; 0.8s deadline and less-than-3s elapsed assertion; PID no longer exists |
| SMTP timeout classification | PASS | `test_worker_errors_preserve_conservative_delivery_outcomes`: pre-send socket timeout is retryable; timeout after send and child crash are unknown |
| Successful Gmail backend/message path | PASS, mocked | `test_worker_preserves_message_and_returns_success`: real worker protocol, fake SMTP login/send, subject/body preserved, child reaped |
| Lock retained through cleanup | PASS | `test_repeated_cancellation_during_startup_reaps_before_unlock`: repeated cancellation during delayed child registration; competing lock refused until child is reaped |
| SIGTERM/global run timeout | PASS | `test_cli_sigterm_or_global_deadline_reaps_smtp_before_unlock`: real CLI processes exit 143/2, worker gone, database valid, lock reacquirable, unknown outbox state |
| Abrupt scanner death | PASS | `test_abrupt_parent_death_stops_worker_and_retains_recoverable_attempt`: test parent SIGKILL leaves no running SMTP child, durable sending row and valid DB |
| Ambiguous delivery not resent | PASS | `test_uncertain_acceptance_not_resent_after_database_reopen` and full CLI process workflow prove no automatic second send after reopening/restart |
| Existing HTTP/SQLite/checkpoint recovery | PASS | Deployment suite tests HTTP cancellation/closure, search continuation, cooldown persistence, global deadline, lock release, signal restoration and SQLite integrity |

After graceful cancellation the outbox row is immediately unknown. After abrupt
parent death it can remain `sending` until its lease expires; recovery then
marks it unknown. Operator reconciliation is intentionally required. A dead
worker after abrupt parent death may briefly be a zombie awaiting init/systemd
reaping; it cannot continue sending.

Default application budget is 240s, search 120s/60 attempts and inspection
90s/60 attempts. SMTP has a per-message absolute 20s default budget and is also
cancelled by the outer scanner deadline. The supervisor kills and waits before
the scanner releases its lock. Repository systemd provides 270s start and 25s
stop limits. These are tested software/OS termination bounds under normal
scheduling, not guarantees against a frozen kernel or storage subsystem.
Standalone email commands use the same transport, but are not scheduled scans.

Implementation references:
[Python subprocess lifecycle](https://docs.python.org/3.12/library/asyncio-subprocess.html),
[Linux parent-death signal and registration race](https://www.man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html),
[Linux PR_SET_PDEATHSIG definition](https://github.com/torvalds/linux/blob/master/include/uapi/linux/prctl.h).

## 5. Notification Pipeline

**PASS for injected/mock funding and mocked SMTP; NOT VERIFIED live.**

`test_full_cli_process_delivery_increase_interruption_and_duplicate_prevention`
executes actual CLI orchestration across subprocesses and temporary SQLite:
GitHub discovery and issue/timeline inspection, trusted fixture funding of USD
100, one queued/sent event through the real supervised worker with fake SMTP,
a duplicate scan with no second delivery, funding increase to USD 150, and a
second qualifying event. The mocked server accepts that second message and
blocks; SIGTERM kills its worker. Restart does not resend it. Assertions prove
one discovered issue, persisted amount 150, first event sent once, second event
unknown with one attempt, intact database and completed search checkpoint.

The funding object in that workflow is an injected fixture, not a real provider.
`test_pipeline_refuses_invalid_or_unavailable_funding` separately uses mocked
HTTP at fictional `funding.example` for claim-only, cancelled, wrong-issue,
untrusted-provider, 429 and 503 cases. All six yield no funded classification or
outbox event and preserve valid state; unavailable providers retain missing
source evidence. Existing tests cover eligibility, competition, reward
transitions, new funding on previously discovered older issues and search
checkpoint continuation. The offline dry-run subprocess test confirms no
database creation or email.

No supported live funding adapter or real Gmail delivery was validated.
Mocks establish application behavior, not external platform compatibility.

## 6. Funding Provider Status

**BLOCKED. No provider implemented or enabled.**

The [detailed review](docs/production-funding-review.md) records official sources
and missing prerequisites. Findings:

- [Algora's reference](https://api.docs.algora.io/) advertises bounty operations,
  but actual endpoint/schema/auth/rate and active funded-balance semantics could
  not be verified. Direct bounty reference retrieval timed out. Its
  [terms section 8](https://algora.io/terms) prohibit automated monitoring;
  explicit supported API authorization needs establishing before integration.
- [Opire's payment docs](https://docs.opire.dev/rewards/pricing) and
  [lifecycle](https://docs.opire.dev/rewards/lifecycle) describe USD GitHub
  bounties paid directly by creators following review, not automatically funded
  escrow. No authoritative funding-read API contract was established.
- [Gitpay](https://docs.gitpay.me/docs/en/payments/) describes funded tasks,
  separate payment/release stages and Whop transfers, but no supported external
  funding-read endpoint/schema/auth/rate contract was established.
- [Bounties Network](https://bounties.readme.io/reference/bounty) documents a
  GET endpoint and stage/pagination filters. Live read failed DNS; current
  response schema, exact GitHub association and currency handling remain
  unverified.

These are explicit integration gaps, not invented APIs or a blanket assertion
that the platforms have no APIs. No scraping, guessed amount/status mapping,
implicit currency conversion or weakened funding rule was added.

Normal `python -m bountyradar run` still passes no real funding provider to the
verifier. It can discover real GitHub candidates, and distinguishes unverified
reward claims from authoritative records in its model, but cannot generate
new verified monetary alerts as shipped. Configuring trust lists alone does
not supply records. Previously stored pending alerts can still be delivered
when email is enabled. Previously discovered old issues are revisited; an old,
never-seen issue funded only externally may remain missed without a provider
discovery feed.

## 7. Installed Deployment

Read-only snapshots, not activation evidence:

| Inspection command | Exit | Observation |
| --- | --- | --- |
| `git status --short` | 0 | Existing project files remain untracked; preserved |
| `systemctl --user status bountyradar.service --no-pager` | 3 | Initially old PID 111903 activating for about 59 minutes; later PID 187517 activating, started 23:40:00 |
| `systemctl --user status bountyradar.timer --no-pager` | 0 | Enabled, active/running; started 22:32:11 |
| `systemctl --user cat bountyradar.service bountyradar.timer` | 0 | Installed oneshot has correct project/interpreter/env paths but old 60min startup timeout; timer already five-minute and persistent |
| `systemctl --user show bountyradar.service --property=ActiveState,SubState,MainPID,TimeoutStartUSec,TimeoutStopUSec,SuccessExitStatus,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp` | 0 | At approximately 23:43:50: start timeout 1h, stop timeout 1min30s, empty success-exit list; PID 0, failed/exit-code, status 2; preceding scan exited naturally at 23:43:31 |
| `systemctl --user list-timers --all --no-pager` | 0 | Next BountyRadar scan 23:45:00; previous 23:40:00 |
| `loginctl show-user james --property=Linger` | 0 | `Linger=no` |
| `ps -C python,python3,pytest -o pid,ppid,etime,stat,comm` | 0 | At the inactive-service snapshot, only this validation's Python process remained |
| `journalctl --user -u bountyradar.service --since '2026-10-17 23:39:00' --no-pager --grep='"event": "run_'` | 0 | Two natural scheduled completions recorded below |

The journal recorded 23:39:16 and 23:43:31 completions of 210.14s and 210.17s:
4,388/4,665 candidates, 18/17 incomplete windows, zero eligible/queued/sent,
no delivery failures, interval not exceeded. These are observations of
independently scheduled runs from the shared checkout under old unit settings,
not controlled performance or live funding/email success. They demonstrate
bounded partial runs, not a completely drained backlog.

The old hour-long process exited without intervention; enabled scheduling
started subsequent scans naturally. Status can change again every five minutes.
Editing shared source affects future executions even without a unit refresh.

Repository unit syntax passes and contains `Type=oneshot`,
`TimeoutStartSec=270s`, `TimeoutStopSec=25s`, `SuccessExitStatus=143`.
Installed units still need updating. The timer already has `OnCalendar=*:0/5`
and `Persistent=true`. Login startup is configured; logged-out scanning is not
configured (`Linger=no`). The laptop must be awake and connected.

## 8. Remaining Issues

| Severity | Issue | Required action |
| --- | --- | --- |
| Release blocker | No real authoritative funding provider/CLI registration | Obtain documented permitted interface and representative funded/status records; implement bounded adapter/discovery and real schema tests without promoting reward claims |
| Deployment blocker | Old installed service timeouts/exit handling | Explicitly approved stop, outbox review and unit refresh |
| Operational risk | Unknown email acceptance cannot be resolved automatically | Check mailbox/provider evidence before library `resolve_unknown()` reconciliation; do not blindly resend |
| Completeness limitation | Newly funded never-seen older issues and growing backlog can be delayed/missed | Provider discovery/update feed plus measured queue policy |
| Unverified integration | Real Gmail acceptance and provider compatibility | Separately authorized live checks after supported funding integration; mocks do not prove these |

## 9. Manual Activation Commands

**Not executed. Require explicit authorization.** Stop the timer first so it
cannot start another scan while deployment is being changed. Stop the existing
service and wait; do not delete SQLite, WAL, SHM or lock files, and do not force
unlock a process still running. Ensure any separately started manual scan has
also ended. If a stop/check fails, stop the procedure and investigate.

Pending entries may send as soon as scheduling is re-enabled. Before running
the installer, review the outbox and set `EMAIL_ENABLED=false` in private `.env`
for candidate-only validation (also remove/disable any conflicting email-enable
alias or systemd-manager override). Keep Gmail disabled while funding readiness
is unresolved. The installer enables the timer and a persistent trigger may
run immediately.

```bash
cd /home/james/bounty/bountyradar
systemctl --user disable --now bountyradar.timer
systemctl --user stop bountyradar.service
test "$(systemctl --user show bountyradar.service --property=ActiveState --value)" = inactive
test "$(systemctl --user show bountyradar.service --property=MainPID --value)" = 0

# Read/reconcile existing outbox only after stopping; do not invoke send-pending.
.venv/bin/python -m bountyradar email pending --json
# Privately review .env and set EMAIL_ENABLED=false before timer activation.
chmod 600 .env
nano .env

.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m pip check
systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer

./scripts/install.sh
systemctl --user cat bountyradar.service bountyradar.timer
systemctl --user show bountyradar.service --property=TimeoutStartUSec,TimeoutStopUSec,SuccessExitStatus
systemctl --user status bountyradar.timer --no-pager
systemctl --user list-timers bountyradar.timer --no-pager
journalctl --user -u bountyradar.service -n 100 --no-pager
```

Run these commands interactively and proceed only after each succeeds, not as
an unchecked pasted batch. `email pending` does not send, but may perform normal
expired-attempt recovery. Installer validates configuration, refuses an active
service, copies repository units, reloads systemd and enables the timer.
Do not interpret candidate-only activation as GO for verified monetary alerts.
