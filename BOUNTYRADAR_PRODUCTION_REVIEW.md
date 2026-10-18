# BountyRadar production review

Reviewed 2026-10-18, Asia/Kathmandu (+05:45). Issue/coverage timestamps are UTC.
This report supersedes earlier readiness conclusions where newer evidence is given.

**Current unattended alerting: NO-GO.** The tested implementation is suitable for
continued, explicitly limited candidate collection and experimental unverified alerts,
but production remains confirmed-only with no authoritative funding provider. Historical
coverage has not drained, the installed service has obsolete deadlines, and final
deployment/real bounty-alert behavior has not been approved or verified. Publishing the
honestly labeled project is safe after the checks below; that is not deployment approval.

## 1. Work resumed

The existing correct Git repository, source changes, temporary test artifacts and
SMTP attempt records were preserved. Recovery cursors, recent-discovery priority,
claim detection and initial downtime tests already existed when work resumed.
Two authorized SMTP tests had already been accepted. **No further email was sent.**
The continuation reviewed and strengthened the changes rather than rebuilding.

Read before/during the work: AGENTS.md, README.md, the live test and real-operation
audit, PRODUCTION_READINESS_REPORT.md, STEP7_DEPLOYMENT_REPORT.md, and source/funding
integration notes. Root-cause, scoped implementation and targeted testing workflows
were used. No production database was reset, pruned, deleted or replaced. No service
was stopped/restarted/reinstalled/enabled/disabled. The parent Git repository was not
staged or altered for publication; the project repository root is correct.

## 2. Code changes

- `settings.py`, `.env.example`, `cli.py`: cold-start coverage defaults to eight hours;
  valid checkpoints use the actual uncovered interval plus five-minute overlap. No
  eight-hour truncation of 12/24-hour outages. USD floor defaults to zero, with
  strictly-positive eligibility and existing currency policies retained.
- `discovery.py`, `models.py`, `run_state.py`: a separate persisted recent lane prevents
  a frozen historical cursor from being the only discovery path. It normally searches
  a two-hour window using the first configured bounty term/label, up to eight attempts
  and 20s, further capped at one quarter of total search capacity. Remaining capacity
  serves historical work. Lanes share pacing/cooldowns and issue identity. Each committed
  page saves its continuation with its findings. Partial fresh work is reported as
  incomplete and retains its own cursor; historical completion is independent.
- `run_state.py`, `cli.py`, `verification.py`: preserve first discovery time, prioritize
  newly recovered/changed candidates and reserve oldest-due slots. The verifier preserves
  the queue's order. Recent inspections receive at most 75% of inspection requests/time
  when both queues have work, retaining real capacity for historical inspection. Unused
  capacity is available to remaining work. Attempts, including failed attempts, are bounded.
- `github_inspector.py`, `verification_models.py`: explicit CLAIMED / APPARENTLY_UNCLAIMED /
  UNKNOWN statuses; busy labels, maintainer confirmations, ambiguous requests, linked PRs,
  assignments and supported trusted-provider claim states are distinguished. Malformed or
  incomplete issue/claim evidence cannot establish apparently-unclaimed status.
- `discovery.py`, `verification.py`, `delivery.py`: reparse current rewards rather than
  trusting stale search prices; inspect dated comment evidence; reject/defer malformed,
  ambiguous or conflicting amounts/currencies. Older search pages cannot overwrite newer
  issue observations. Unverified prices and availability are checked before delivery.
- `notification.py`: include claim status and its limited meaning in rendered mail.
  Existing classification, deduplication, transition keys, daily caps and supervised
  SMTP lifecycle remain in use. No real funding provider was invented.
- `tests/test_offline_recovery.py`: new downtime, queue, evidence, failure and reward
  regression coverage. Related test fixtures now contain valid required GitHub fields;
  meaningful assertions were preserved. Existing five-dollar configuration tests remain
  as explicit-floor tests, while default all-positive behavior includes $0.01 and $1.
- README explains setup, GitHub/Gmail, systemd, catch-up, modes, claim uncertainty and
  funding/backlog limitations. Existing historical reports are preserved as history.

## 3. Code-review comments

Locations refer to the reviewed source. FIXED findings have regression evidence.

| Severity | Location | Evidence / cause and impact | Correction / status |
| --- | --- | --- | --- |
| High | `discovery.py:120`, `cli.py:109` | Frozen historical pagination delayed fresh work; default large lookbacks obscured actual missed coverage | Independent bounded recent cursor; actual checkpoint gap; 5/8/12/24h and concurrent-lane tests. FIXED |
| High | `run_state.py:106`, `verification.py:277`, `cli.py:183` | Old records first; sorting again discarded queue priority; slots alone did not reserve HTTP/time capacity | Priority preserved, fair older slots, recent inspection budget capped. Dedicated test exhausts recent request capacity and proves historical issue/timeline requests still occur. FIXED |
| High | `verification.py:265` | Trusted provider statuses claimed/reserved/in_progress were discarded before claim logic | Accept trusted statuses as claim evidence, never funded proof; three regression cases. FIXED |
| High | `github_inspector.py:73` | Missing assignee/comment/label fields could look like empty evidence and false availability | Required structural validation; incomplete evidence becomes UNKNOWN. Valid fixtures enriched; missing-field tests added. FIXED |
| High | `verification.py:147`, `delivery.py:97`, `discovery.py:47` | Search prices could survive removal/decrease; first money mention could select an ambiguous price | Reparse full current issue/comment evidence, defer conflicting amounts, check again before send. Edited/deleted/stale/zero/changed reward tests. FIXED |
| High | `run_state.py:177` | A delayed older page could overwrite a newer observation | Timestamp-guarded issue upsert, stable first-seen timestamp. FIXED |
| High, deployment | `deployment/systemd/bountyradar.service:13` vs installed unit | Installed 60min/90s/no success-143 differs from tested 270s/25s/143 settings | Correct repository unit passes verification; installation needs separate approval. OPEN operational blocker |
| High, operational | `run_state.py:106`, production `scan_state` | 76,520 unverified rows; historical coverage still frozen; bounded scheduling cannot prove eventual drainage under continuing arrivals | Fresh prioritization and bounded fairness implemented; narrow noisy queries, measure production lag/drainage and design retention without destructive resets. OPEN |
| High, intended workflow | `cli.py:158`, `settings.py` | Only GitHub registered, no authoritative provider, confirmed-only production mode prevents new claim-only alerts | Publish as unverified candidate monitor; enable labeled unverified mode only with separate approval, or implement permitted provider. OPEN |
| Medium | `discovery.py:120`, `cli.py:109` | Indexing delays beyond five-minute overlap/recent window, repeated result-count churn, or growing recent cursor can delay/miss visibility | Overlap, splitting, durable cursors tested; use measured reconciliation windows/feed in future. ACCEPTED documented limitation |
| Medium | `github_inspector.py` comment evidence | Claim text is heuristic; edited comment creation time does not prove when reward was introduced; comments/PRs are bounded | Use UNKNOWN for ambiguity/incomplete evidence; preserve provenance; do not infer announcements from issue updated_at. ACCEPTED documented limitation |
| Medium | `run_state.py:106` | Queue selection scans/join/sorts remain proportional to stored workload; no retention policy | Indexed timestamps and 50k timing verified; larger-load measurements and archival policy remain future work. OPEN |
| Low | `cli.py:141`, `run_state.py` SearchLane | Lane orchestration increases complexity; existing fixed Ubuntu install path remains | Kept within existing architecture, injected client/clock/provider, no broad refactor. ACCEPTED scope tradeoff |

No unfixed critical/high correctness defect was accepted as safe alerting. The OPEN
items are why full unattended readiness remains NO-GO despite safe publication.

## 4. Offline recovery results

| Scenario | Executed evidence | Result |
| --- | --- | --- |
| 5-hour outage | `test_offline_period_full_source_claim_outbox_pipeline[5]` | Actual gap + overlap; $5/$100/$200 queued/sent through fake SMTP; $10 assignment/$50 maintainer claim excluded |
| 8-hour outage | Same parameterized test [8] | Same eligibility and deduplication; entire interval covered |
| 12-hour outage | Same [12]; split/resume test | No truncation to eight hours; broad windows split and persisted across reopened stores |
| 24-hour outage | Same [24] | Actual 24h gap retained; normal five-minute scan resumes after completion |
| Cold start / invalid or future checkpoint | Eight-hour initial-lookback regression cases | Eight hours used only when no valid past completed coverage exists |
| Interrupted page / process | Existing deployment/search-deadline and real test-owned SIGTERM suites | Committed candidates/cursor survive; next run resumes; no early checkpoint |
| Crash before complete commit | Enqueue-crash and rollback tests | Checkpoint unchanged; replay remains safe |
| Crash/reopen after checkpoint | Delayed-indexing/checkpoint test | Coverage persists, no duplicate issue; five-minute overlap sees a late-indexed issue |
| Fresh issue during old catch-up | Independent-lane regression and live probe | Fresh request precedes exact historical query/page; both cursors persist independently |
| Repeated 09:00/05/10/15-style scans | Existing four-interval funded/unverified fixture tests | $5/$10/$100 plus $10→$20 event; no duplicate from unchanged rescan |

These tests use temporary SQLite, fake API/SMTP and injected clocks. They prove
algorithmic coverage/recovery of finite fixture workloads, not exhaustive GitHub
index completeness or real laptop power-loss durability. No real reboot or production
process kill was performed. SQLite transactions/locking and SMTP parent-death tests
cover process interruption without manipulating live data.

## 5. Recent bounty performance and live evidence

The retained controlled live probe used actual token authentication, temporary SQLite,
email disabled, strict request/runtime budgets and production-inactivity checks before
requests. At 04:58:31 UTC / 10:43:31 Nepal time, GET /user returned 200. Four search
and six inspection responses returned 200; plus authentication this is 11 completed
requests. The inspector reached its six-request budget on the third issue and reported
UNKNOWN rather than inventing availability. Last reported quota: search 26, core 4,868.
Network wall time **18.119s**; total **88.26s includes waiting for production**.

The probe exercised real pipeline discovery → availability/reward review → SQLite →
outbox in isolated `include_unverified` mode, with email disabled. It stored **185
unique candidates**, three verification snapshots and **one pending unverified event**;
sent **zero actual bounty emails**. Integrity is ok. Fresh recent results were combined
with a seeded older frozen cursor; completed coverage correctly remained at that old
cursor's endpoint rather than claiming all intervening hours were covered. The final
review added further fail-closed checks after this probe; the live snapshot is evidence
of actual integration, not a claim that every final source line was deployed live.

| Genuine issue | Created UTC | Updated UTC | Price in probe | Inspection result |
| --- | --- | --- | --- | --- |
| [BasedHardware/omi](https://github.com/BasedHardware/omi/issues/22091) | 2026-10-18T04:56:02Z | 2026-10-18T04:56:02Z | 50 USD | APPARENTLY_UNCLAIMED; POTENTIAL_UNVERIFIED |
| [oll4com/spaceapp](https://github.com/oll4com/spaceapp/issues/62) | 2026-10-16T05:30:26Z | 2026-10-18T04:55:49Z | 750 USD | APPARENTLY_UNCLAIMED; POTENTIAL_UNVERIFIED |
| [Henry00IS/ShapeEditor](https://github.com/Henry00IS/ShapeEditor/issues/17) | 2022-09-14T16:39:33Z | 2026-10-18T03:48:54Z | Unknown | UNKNOWN; POTENTIAL_UNVERIFIED |

The first issue is a proposal, not confirmed funding. No maintainer acceptance or
payment guarantee is implied. The second is an older issue updated recently, not
proof of a newly announced reward. The third's inspection was incomplete. None is
described as authoritative funded evidence. Current conservative parsing can defer
conflicting title/body amounts instead of reproducing a provisional earlier amount.

Under the **50,000 historical-record fixture**, fresh candidates were selected first;
queue selection took **0.061s** and eight mocked verifications including the fresh
ones finished within **0.062s** after queue entry. Fixture creation plus integrity
check took **0.843s**. These are real local wall times with mocked IO, not simulated
GitHub latency. They do not promise subsecond real inspection. Live request pacing,
comment pagination, rate limits and availability of the laptop bound actual latency.

Batch sizes **1, 2, 5, 20 and 100**, empty queues and refill behavior were tested.
For sizes above one the oldest-due reservation is max(1, floor(batch/4)); size one
serves historical work every fourth selection, persisted across restart. A separate
test proves historical HTTP requests occur after dense recent work consumes its
allocation. Newly recovered old-dated opportunities remain eligible for recent
priority; unchanged failures do not jump ahead of newly arrived work.

## 6. Claim and reward verification

CLAIMED: valid assignees, exact busy/reserved/claimed labels, trusted maintainer
confirmation, relevant open implementation PR or current trusted provider claim.
APPARENTLY_UNCLAIMED: issue open, required inspection complete, no claim detected in
checked sources. UNKNOWN: ambiguous contributor request, contradictory statement,
malformed payload, truncated evidence, API failure or inspection budget exhaustion.
Unknown is retried; neither claimed nor unknown is eligible for mail.

Tests distinguish `unclaimed` from `claimed` labels, contributor permission requests
from owner/member approval, negative maintainer statements, unrelated PR references
from actual closing/connected links, assigned/closed/expired issues, and provider
claimed/reserved/in_progress. Pre-delivery revalidation cancels a newly assigned issue.
Claim text can produce conservative false negatives; absence of detected evidence is
never a guarantee that nobody else is working.

Rewards are tested at $0.01, $1, $5, $10, $15, $20, $25, $50, $100, $500, $1,000 and
fractional values. Zero/negative/no-price emails are excluded, but relevant findings
remain stored. Tests cover $100→$50, removal, $5→$10, stale historical observations,
multiple distinct prices/currencies, unsupported token text, malformed separators,
and edited/deleted comments. Conflicting values defer rather than selecting a sum or
guessing a rate. A changed pending price waits for normal re-verification; a vanished
or zero promise cancels submission. Already sent mail cannot be recalled.

Original currencies are retained. USD is the production allowlist; non-USD alerts
require explicit allowlist and floor configuration. No exchange rates are invented.
Funding providers remain injected fixtures only. Confirmed-only never promotes GitHub
money text into funded proof; opted-in unverified emails prominently disclose that
payment/funding are not guaranteed.

## 7. Gmail results

The interrupted session's two test records already show **sent**, one attempt each.
The existing recipient matches both the SMTP login and the connected Gmail account.
No address, token or SMTP password is disclosed in this report. The full two-message
authorization budget has been used; **this continuation sent zero additional messages**.

| Actual subject | SMTP status | Duration | Independent receipt |
| --- | --- | --- | --- |
| [BountyRadar TEST] SMTP Connectivity Check | Accepted; test attempt persisted sent | 4.700s | Matching audit ID and TEST marker in INBOX |
| [BountyRadar TEST] $5 Unclaimed Bounty Simulation | Accepted; test attempt persisted sent | 4.002s | Matching audit ID and TEST marker in INBOX |

The second subject differs from the later continuation's proposed subject because
the already-authorized interrupted-session message was sent before that instruction.
It was not resent to change the subject. Its content used fictional repository/issue
data, a $5 reward, APPARENTLY_UNCLAIMED and UNVERIFIED, no real issue link, and the
explicit `THIS IS A TEST — NOT A REAL BOUNTY` marker. These live messages used the
supported test renderer with controlled synthetic content and supervised SMTP transport;
the opportunity renderer is separately exercised end-to-end with fake SMTP.

Audit ID: `e05dbd985e5e4017a1021ee021a43f5d`. Read-only Gmail verification found both
exact audit-ID messages in INBOX, with matching subjects and TLS 1.3 Received headers.
This independently establishes receipt, beyond SMTP acceptance. The read did not
modify messages. Temporary SMTP database integrity is ok; two sent attempts, no
pending/unknown attempts, no surviving SMTP worker. The application's default absolute
worker deadline was 20s; both submissions completed within it. No duplicate was sent.

Offline suites additionally test authentication rejection, DNS/socket/send timeouts,
uncertain acceptance, retries, daily cap, SIGTERM during SMTP, abrupt parent death,
repeated cancellation, kill/reap-before-unlock and restart without automatic uncertain
resend. An isolated live probe retains one pending real candidate with email disabled;
it is not production data and was never delivered.

## 8. Automation and current deployment

Read-only systemctl status/cat/show/list-timers and three-hour journal captures were
executed. Timer is enabled/active, five-minute calendar and persistent catch-up are
configured; correct project Python, working directory and .env are installed. Recent
actual start/completion evidence below uses Nepal time and second-resolution journal
timestamps; zero delay means within that recorded second, not microsecond precision.

| Expected boundary | Actual start | Actual end | Duration / result | Delay |
| --- | --- | --- | --- | --- |
| 10:30:00 | 10:30:00 | 10:33:19 | 198.87s; partial exit 2; 55 incomplete | 0s |
| 10:35:00 | 10:35:00 | 10:38:19 | 198.92s; partial exit 2; 57 incomplete | 0s |
| 10:40:00 | 10:40:00 | 10:43:31 | 210.82s; partial exit 2; 58 incomplete | 0s |
| 10:45:00 | 10:45:00 | 10:48:30 | 210.35s; partial exit 2; 57 incomplete | 0s |
| 10:50:00 | 10:50:00 | 10:53:31 | 210.58s; partial exit 2; 60 incomplete | 0s |
| 10:55:00 | 10:55:00 | Running at snapshot | In progress | 0s |

All five completed rows are bounded partial runs; none completed full historical
coverage. Most recent completed sample: 10:50–10:53:31, 210.58s, 3,722 candidates,
60 incomplete windows, no queued/sent mail or delivery failures. No run in these rows
exceeded 240s or overlapped another oneshot. Consecutive starts prove operation while
awake/user manager available, not during sleep/power-off. Earlier login at 09:47 and
the existing `Linger=no` configuration remain observed facts; no actual reboot was
performed in this continuation. Future runs are not inferred from unit syntax alone.

At the 10:55 snapshot the next configured boundary was 11:00; list-timers can show
no next timestamp while the oneshot is running. Historical outage/session gaps in the
three-hour capture are not proof of timer failure while asleep/logged out. No active
hour-long legacy process was observed. Current source is shared with the scheduled
service, so naturally started processes can see successive working-tree versions;
intermediate production snapshots are not a clean activation test of the final commit.

Installed startup timeout **60min**, stop timeout **90s**, empty SuccessExitStatus;
repository specifies **270s**, **25s**, and **143**. Timer contents differ only in
whitespace. Installed units were not refreshed. SIGTERM, file locks, resource cleanup,
and process bounds pass isolated tests. The final installed configuration still
requires separately approved deployment and subsequent observation.

## 9. Production configuration and database

**Effective USD minimum is 0**, explicitly authorized by the continuation. It was $5
because of the user's earlier explicit five-dollar request, not a hidden filter.
Private `.env` alone was atomically changed from 5 to 0 with mode 0600 preserved;
unrelated settings/secrets were unchanged. Settings defaults and .env.example are zero.
Environment settings override dotenv. `MIN_BOUNTY_USD` is not a supported alias;
the actual setting is `BOUNTYRADAR_MIN_REWARD_USD`. There is no CLI price override
or database insertion floor. Monetary eligibility always separately requires >0.

Production notification mode remains **confirmed_only**. Only GitHub discovery is
registered; zero trusted funding providers are configured and no concrete provider
is implemented. This setting cannot generate new verified-funded alerts from ordinary
GitHub candidates. The fully tested unverified mode can queue apparently-unclaimed
positive rewards, but its production activation was not approved. An approval question
was issued; no affirmative response was assumed. Exact opt-in setting, requiring
separate approval for real bounty emails:

```dotenv
BOUNTYRADAR_NOTIFICATION_MODE=include_unverified
```

Production email enablement remains unchanged. No test invoked production
send-pending, no actual external bounty alert was sent, and unknown/pending delivery
records were neither deleted nor blindly retried. Lack of a funding provider remains
a documented source-integration limitation, not a claim of confirmed funds.

Read-only database snapshot at **05:10:07 UTC / 10:55:07 Nepal time**:

- Main file `data/bounties.db`: **326,729,728 bytes**, **76,960 issues**, **440
  verification records**, **76,520 never verified**, **zero outbox events**.
- `PRAGMA quick_check`: **ok**. No production SQLite/WAL/SHM file was replaced.
- Historical `github_search_progress`: covered-until **2026-10-17T17:47:15Z**,
  **60 unfinished windows**; no completed historical checkpoint observed.
- Separate recent lane completed at **2026-10-18T05:05:00Z**; next recent cursor
  frozen at **05:10:00Z**, two unfinished windows. This demonstrates recent progress
  despite stalled historical coverage, not a claim that catch-up finished.
- Main database grew from the earlier audit's approximately 208 MB/50,740 issues.
  No retention policy was added or destructive backlog cleanup performed.

Priority latency is improved and tested, but production queue growth and total catch-up
latency are **not solved or proven bounded**. Narrow noisy terms, measure per-lane
coverage and queue delay, and design an archive/retention policy before daily reliance.
The broad keyword source can yield false positives, so counts are candidates, not jobs.

## 10. Validation evidence

Final source validation ran in `/home/james/bounty/bountyradar` using the existing venv.
No package reinstall was required. All destructive/failure tests use temporary data.

| Command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q --junitxml=/tmp/bountyradar-final-audit/final-tests.xml` | 0 | 250 passed, 0 failed/errors/skipped; pytest reports 43.60s; no warnings |
| `.venv/bin/ruff check .` | 0 | All checks passed! |
| `.venv/bin/python -m pip check` | 0 | No broken requirements found. |
| `git diff --check` | 0 | No diagnostics |
| `bash -n scripts/install.sh scripts/uninstall.sh` | 0 | No diagnostics |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | 0 | No diagnostics |
| `AST parse + untracked whitespace` | 0 | No diagnostics |

CLI `--help` also exited 0. Nonignored untracked files were checked with
`git diff --no-index --check /dev/null <file>`; ignored runtime/credential files were
inspected by name only. Focused downtime/review tests passed 50 in 2.65s; final
priority/deployment regression run passed 67 in 6.47s. Earlier full review runs
surfaced real provider-status and payload-validation/fixture/API-contract issues;
they were fixed and followed by the complete green run. A previous complete run
passed 247 tests before the final additional tests; 194 is not the current count.

Failure coverage includes GitHub 401, 403, 429, 500, DNS/transport timeouts, incomplete
results/splitting, invalid JSON/items, missing issue fields, database locks/rollback,
duplicate events, SMTP auth/timeouts/uncertainty, scanner/worker SIGTERM, parent crash,
repeated starts/locks and invalid settings. Indexing overlap and before/after checkpoint
recovery tests pass. Sleep/shutdown timing uses injected-clock simulations and process
interruption; no physical power-cut test or blanket external-availability guarantee.

## 11. GitHub publication and security

Repository root: `/home/james/bounty/bountyradar`.
Branch: `main`, tracking `origin/main`.
Origin: `https://github.com/jmass-ggg/Bounty-Finder-.git`.
Remote was fetched; initial divergence was 0/0. Existing commits/history are preserved.
No parent-directory staging, repository replacement, force push or history rewrite.

Security review scanned intended project files and existing history for the actual
configured token/password/recipient values without printing them, and for GitHub-token,
AWS-key and private-key patterns. No matches. `.env.example` credential/address fields
are blank placeholders. Real .env, databases/sidecars/locks, .venv, logs and caches are
ignored and absent from tracked/staged publication files. Known local deployment paths
remain intentional. Neither gitleaks nor trufflehog is installed; this was an explicit
configured-value/pattern check, not a claim of an unavailable scanner or perfect detection.
The final staged diff and index receive the same checks before committing.

Publication evidence is recorded below after the normal commit/push verification.
The implementation commit contains the tested source and fixtures. A separate report
commit can record its SHA without a self-referential commit hash. Publication is
authorized by the user and is independent of service activation or email-mode approval.

## 12. Final decision and remaining actions

| Area | Decision |
| --- | --- |
| GitHub discovery | CONDITIONAL GO: real authenticated requests and recent lane work; broad/noisy and partial historical coverage |
| Offline catch-up | CONDITIONAL GO: 5/8/12/24h finite-workload tests and durable cursors; production backlog not drained |
| Claim verification | CONDITIONAL GO: conservative evidence checks; no guarantee nobody else is working |
| Real funding verification | NO-GO: no authoritative provider |
| Gmail transport/rendering | GO for verified controlled test path; actual opportunity renderer tested offline |
| systemd deployment | NO-GO for final unattended claim: installed service settings remain outdated |
| SQLite | CONDITIONAL GO: integrity/recovery pass; growth/retention unresolved |
| Backlog performance | CONDITIONAL GO for tested fresh priority; NO-GO for guaranteed full catch-up latency |
| Security/publication | GO after green validation and final index scan; no secrets found |
| Current unattended bounty alerts | NO-GO: production confirmed-only, missing provider, partial coverage and deployment gaps |

Minimum steps: separately approve labeled unverified notifications if desired; approve
the safe unit refresh below; observe the final version over repeated scheduled runs,
including per-lane progress, fresh issue lag and request quotas; reduce discovery noise
and implement measured retention/archive policy without deleting live data blindly.
A real provider requires a permitted documented interface, current schema/status tests
and authoritative funding evidence tied to the exact issue. No guessed integration.

The following disruptive deployment commands are **prepared, not executed**, and
require separate approval. Run one at a time and investigate any failure:

```bash
cd /home/james/bounty/bountyradar
systemctl --user disable --now bountyradar.timer
systemctl --user stop bountyradar.service
test "$(systemctl --user show bountyradar.service --property=ActiveState --value)" = inactive
test "$(systemctl --user show bountyradar.service --property=MainPID --value)" = 0
./scripts/install.sh
systemctl --user show bountyradar.service --property=TimeoutStartUSec,TimeoutStopUSec,SuccessExitStatus
systemctl --user status bountyradar.timer --no-pager
```

The installer enables the timer and persistent catch-up may run immediately. Preserve
the private .env and all database/WAL/SHM files. No real email-mode change is implied by
this deployment recipe. Temporary evidence is retained in `/tmp/bountyradar-final-audit/`
but may be removed by OS cleanup; key counts and limits are preserved in this report.
