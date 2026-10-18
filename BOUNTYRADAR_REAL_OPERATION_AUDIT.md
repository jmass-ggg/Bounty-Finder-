# BountyRadar real-operation audit

Audit date: 2026-10-18. Local scheduling times below use Asia/Kathmandu (UTC+05:45).
Issue timestamps use UTC. Existing repository work and production data were preserved.

**Overall: NO-GO for dependable real funded-bounty alerts.** Automatic candidate
collection and real Gmail submission/inbox receipt work. All-positive-price support
is implemented and the production minimum was changed to zero with explicit approval.
The large verification backlog, missing funding integration and outdated installed
service settings remain blockers. These findings do not negate the successful parts.

No production services were stopped, restarted, enabled, disabled or reinstalled.
No actual bounty alert was sent during testing. Only two synthetic TEST messages
were submitted, both to the existing configured inbox. No commits or pushes occurred.

## A. Five-minute monitoring status

The timer is **enabled and active**, with `OnCalendar=*:0/5`, `Persistent=true`
and `AccuracySec=1s`. Journal evidence verifies consecutive five-minute starts at
09:55, 10:00, 10:05, 10:10 and 10:15 while the laptop/user manager were running.
This conclusion uses execution events, not merely the calendar configuration.

Read-only commands executed: `systemctl --user status bountyradar.timer --no-pager`,
`systemctl --user status bountyradar.service --no-pager`,
`systemctl --user list-timers --all --no-pager`,
`systemctl --user cat bountyradar.timer bountyradar.service`,
`systemctl --user show bountyradar.service -p ActiveState -p SubState -p MainPID -p ExecMainStartTimestamp -p Result`,
and `journalctl --user -u bountyradar.service --since "2 hours ago" --no-pager -o short-iso`
(the same journal window was also captured as JSON for timestamp analysis).
Status returned 3 for an activating/failed service and 0 for the active timer;
show/cat/list-timers/journal reads succeeded. No service mutation was performed.

The final two-hour journal capture was at approximately 10:15. No retained starts
were found before 08:51 in that window. The gap from 09:10 through 09:45 crosses
a boot/user-session boundary; it does not establish scanning while the manager was
unavailable. Continuous cadence across those missing periods is **NOT VERIFIED**.

| Expected time | Actual start | Actual end | Result / duration | Delay |
| --- | --- | --- | --- | --- |
| 08:50:00 | 08:51:08 | 08:54:29 | Partial, exit 2 / 200.7s | 68s |
| 08:55:00 | 08:55:00 | 08:58:23 | Partial, exit 2 / 202.77s | 0s |
| 09:00:00 | 09:00:00 | 09:03:30 | Partial, exit 2 / 210.17s | 0s |
| 09:05:00 | 09:05:00 | 09:05:19 | Interrupted, SIGTERM / exit 143 | 0s |
| Persistent startup/catch-up | 09:47:00 | 09:50:09 | Partial, exit 2 / 188.8s | Not a regular boundary |
| 09:50:00 | 09:50:09 | 09:53:39 | Partial, exit 2 / 210.2s | 9s |
| 09:55:00 | 09:55:00 | 09:57:59 | Partial, exit 2 / 178.89s | 0s |
| 10:00:00 | 10:00:00 | 10:03:30 | Partial, exit 2 / 210.19s | 0s |
| 10:05:00 | 10:05:00 | 10:08:30 | Partial, exit 2 / 210.21s | 0s |
| 10:10:00 | 10:10:00 | 10:13:03 | Partial, exit 2 / 182.77s | 0s |
| 10:15:00 | 10:15:00 | Still running at snapshot | In progress | 0s |

The last five real starts are the final five rows. Every completed run in the
capture was partial (exit 2); **no fully successful completed scan was observed**.
The last completed scan was 10:10–10:13:03, 182.77s, 4,156 candidates and 47
incomplete windows. None of the nine completed runs exceeded 240 seconds; their
reported durations ranged from 178.89s to 210.21s. The 09:05 interruption predates
this work; its cause was not inferred. A 9-second delay at 09:50 coincided with
the preceding oneshot still running. No overlapping service copies or skipped
whole five-minute intervals were observed in the stable 09:55–10:15 segment.

At 10:15:06 the service was activating under PID 20334, started at 10:15:00.
It is the current bounded scanner, not an old hour-long process. The next configured
calendar boundary is 10:20; list-timers displayed no next timestamp while its
target service was active. Actual future activation is not guaranteed by that schedule.
The timer resumed after login at 09:47. `Linger=no`: logged-out operation is not enabled.

Installed service vs repository: both are oneshot with correct interpreter,
working directory, EnvironmentFile, journal logging and writable data directory.
Installed startup timeout is **60min**, stop timeout defaults to **90s**, and
SuccessExitStatus is empty. Repository settings are **270s / 25s / 143**.
The installed timer differs only by a trailing blank line. Repository unit verification
passes. Unit refresh still requires separate approval; none was given or performed.

## B. Live GitHub discovery

The configured token authenticated successfully: **GET /user returned HTTP 200**.
The isolated probe started at 04:28:05 UTC (10:13:05 Nepal time), waited for a safe
idle interval and checked production state before each request. Both email-enable
aliases were false and SQLite was under `/tmp/bountyradar-operation-audit/`.
Settings alias precedence and the documented CLI were inspected before execution.

Budget: 65s network deadline, 4s HTTP timeout, no retries, 2.1s search spacing,
17 search requests, and at most six inspection attempts over a shared 10s inspection
budget. Observed **23 attempts and 23 HTTP 200 responses**: one authentication,
17 search and five inspection. Actual network wall time was **58.04s**; total process
wall time **253.431s includes waiting for production to finish**, not slow scanning.
No live 401/403/429/5xx or network failure occurred. Final response headers showed
13 search requests and 4,725 core requests remaining; no active rate block was seen.

| Window | Created total / fetched | Updated total / fetched | API result |
| --- | --- | --- | --- |
| 5m | 1 / 1 | 2 / 2 | Both 200, incomplete_results=false |
| 15m | 13 / 10 | 26 / 10 | Both 200, incomplete_results=false |
| 1h | 16 / 10 | 121 / 10 | Both 200, incomplete_results=false |
| 6h | 71 / 10 | 380 / 10 | Both 200, incomplete_results=false |
| 24h | 326 / 10 | 953 / 10 | Both 200, incomplete_results=false |
| 7d | 1828 / 10 | 3286 / 10 | Both 200, incomplete_results=false |

These are bounded first-page samples, ten items/page; total_count is the server's
count, not downloaded/verified opportunities. All 12 fixed-window responses contained
timestamps inside the requested range. There were no empty results in this probe;
a 200 with zero items would have been a valid empty search, not an API failure.

Additional recent-updated searches returned totals of 11,092 for `reward`, 580 for
`"paid issue"`, 156 for `label:bounty`, and 30 for `"algora"`; each returned ten items,
HTTP 200 and incomplete_results=false. These four diagnostic queries had a lower
time bound but no frozen upper bound. A second page of the seven-day created query
also returned ten valid items. Queries requested descending created/updated ordering;
the production scanner requests ascending created ordering for durable pagination.
Links/pages and request parameters were checked; exhaustive ordering/coverage of
all server results was not claimed. GitHub can cap results at 1,000 and return
incomplete searches; the actual scanner's splitting/checkpoint logic was tested offline.
See [GitHub's search API documentation](https://docs.github.com/en/rest/search/search).

Across overlapping queries, **62 unique candidates and three verification records**
were stored; integrity is ok. These direct bounded diagnostic queries intentionally
did not advance a production or complete-scan checkpoint.

Representative genuine results follow. Prices are **unconfirmed**, not guaranteed
funds. GitHub is the discovery source for every row. Dates below are UTC.

| Repository / issue | Created | Updated | Original amount | Source / status |
| --- | --- | --- | --- | --- |
| [adwate-ui/rivr-platform #149: B-033 League points on completion and flow 18 end to end](https://github.com/adwate-ui/rivr-platform/issues/149) | 2026-10-18T04:24:14Z | 2026-10-18T04:24:14Z | No amount | GitHub / Potential, not inspected |
| [paraloom-labs/paraloom-core #1002: [BOUNTY] Medium: Non-atomic cross-CF deposit persistence & unvalidated l…](https://github.com/paraloom-labs/paraloom-core/issues/1002) | 2026-10-18T04:21:23Z | 2026-10-18T04:21:23Z | 150 USD | GitHub / REWARD_CLAIMED; AVAILABLE |
| [dwebagents/AgentPipe #95: [13.12 BTC BOUNTY] implement zero-latency continuous time banana pudding…](https://github.com/dwebagents/AgentPipe/issues/95) | 2026-06-24T04:32:37Z | 2026-10-18T04:15:32Z | 13.12 BTC | GitHub / REWARD_CLAIMED; AVAILABLE |
| [anysearch-team/open-source-bounty #2: GPT Researcher: eligibility of a separate retriever plugin](https://github.com/anysearch-team/open-source-bounty/issues/2) | 2026-10-18T04:05:04Z | 2026-10-18T04:05:04Z | 2500 CNY | GitHub / Deferred detail; UNKNOWN availability |
| [copperheadhq/copperhead #66: Bounty: end-to-end test copperhead create (brief → clean full run) + fin…](https://github.com/copperheadhq/copperhead/issues/66) | 2026-07-24T08:17:43Z | 2026-10-18T03:48:55Z | 50 USD | GitHub / Potential, not inspected |
| [theselfish/SlopStation13 #5: [Bounty] [$600] Add Thursday's Boots](https://github.com/theselfish/SlopStation13/issues/5) | 2026-07-29T05:02:58Z | 2026-10-18T03:48:54Z | 600 USD | GitHub / Potential, not inspected |
| [Nexussyn/ai-growth-engine #3: [AGENT-TASK] Auto-upsell trigger after 5th free call — +25% revenue](https://github.com/Nexussyn/ai-growth-engine/issues/3) | 2026-07-03T14:30:52Z | 2026-10-18T01:46:11Z | 10 USD | GitHub / Potential, not inspected |
| [Circadian-agent/agent-collective #1: Introductions: who you are, what you are selling, one thing you measured](https://github.com/Circadian-agent/agent-collective/issues/1) | 2026-08-02T23:54:20Z | 2026-10-18T03:39:24Z | 83 USDC | GitHub / Potential, not inspected |

The five-minute match has no extracted monetary amount and may be a keyword false
positive, not a paid software bounty. The USD 150 issue passed availability inspection,
with no assignee or competing linked PR found in checked sources, but lacks funding
proof. BTC remains BTC, CNY remains CNY and USDC remains USDC; no USD conversion was
invented. The third inspection hit the shared deadline, so its conservative UNAVAILABLE
category/UNKNOWN availability does not establish that the issue is actually closed.
None of the live records was emailed.

Recently updated is distinct from newly created: old issues updated today were found,
but updated_at alone does not prove when a reward was announced. Newly added external
platform funding cannot be discovered reliably without a provider feed. Search indexing
delay was not measured; immediate visibility and absence of missed issues are not proved.

Current configured search terms are bounty, paid, reward, cash reward and paid issue;
labels are bounty/paid; the monetary term is USD. Broad searches produce considerable
noise. Saved incomplete windows and queue starvation delay verification even when a
new opportunity is visible to a fresh diagnostic query.

## C. All-price bounty coverage

Implemented changes retain the existing pipeline and SQLite schema:

- `min_reward_usd` now accepts nonnegative configured floors and defaults to **0**.
  Eligibility separately requires amount **strictly greater than zero**. Discovery
  never used the old floor to drop database records; verification and delivery did.
- The default `$25` search term was removed. Prices 1, 5, 10, 15, 20, 25, 50, 100,
  500, 1,000 and 0.001 pass extraction, verification, outbox and deduplication tests.
  Zero, negative and missing amounts cannot generate monetary email; relevant
  no-price candidates remain stored. Explicit larger floors still work.
- Currency prefixes/suffixes and common symbols are retained, including EUR, GBP,
  INR, CAD and token amounts. Explicit `CAD $5`/`$5 CAD` are not relabeled USD.
  Supported decimal precision is not restricted to cents. Unsupported/ambiguous
  amounts remain review material; there is no universal currency parser or conversion.
- Non-USD emails still require an allowed currency and an explicit currency floor;
  a zero floor is now valid. Currency policies are not bypassed by a small amount.
- Positive amounts get appropriate reward scores without a hidden $25 cutoff.
  Opted-in unverified alerts do not require the confirmed-funding priority score.

**Approved production change:** only `BOUNTYRADAR_MIN_REWARD_USD=25` was changed to
`0` in private `.env`, using an atomic replacement that preserved mode 0600.
Email enablement, credentials, recipient and confirmed-only mode were unchanged.
The naturally scheduled 10:15 process was inspected and its environment contains
`BOUNTYRADAR_MIN_REWARD_USD=0`. No service restart was required or performed.

## D. Source health

Only `GitHubDiscovery` is implemented and registered by the normal production CLI.
`BountySource`/`FundingProvider` are injection interfaces, not configured integrations.

| Source | Implemented | Live API works | Relevant results | Funding verified | Production enabled |
| --- | --- | --- | --- | --- | --- |
| GitHub REST | PASS | PASS | PARTIAL: genuine candidate/reward signals, false positives remain | NOT VERIFIED | PASS |
| Algora | FAIL: no adapter | NOT VERIFIED | NOT VERIFIED via provider | NOT VERIFIED | FAIL: disabled |
| Opire | FAIL: no adapter | NOT VERIFIED | NOT VERIFIED | NOT VERIFIED | FAIL: disabled |
| IssueHunt OSS | FAIL: no adapter | NOT VERIFIED | NOT VERIFIED | NOT VERIFIED | FAIL: disabled |
| Bountysource | FAIL: no adapter | NOT VERIFIED | NOT VERIFIED | NOT VERIFIED | FAIL: disabled |
| Bounties Network | FAIL: no adapter | NOT VERIFIED this audit | NOT VERIFIED | NOT VERIFIED | FAIL: disabled |
| Gitpay | FAIL: no adapter | NOT VERIFIED | NOT VERIFIED | NOT VERIFIED | FAIL: disabled |

FAIL for disabled platforms means absent implementation, not a failed configured
production source. Names in documentation do not constitute integrations. The Algora
text search above is a GitHub query, not a successful Algora API test.

GitHub uses `https://api.github.com/search/issues` and issue/comments/timeline REST
endpoints with the configured token, documented search pacing, actual payload validation,
canonical GitHub identity, SQLite persistence and durable search progress. Offline tests
cover retries, malformed payloads, 403/429, source failures and independent checkpoints;
a failed source does not become a clean successful source checkpoint.

The reviewed [source-integration notes](docs/phase7-sources.md) and
[funding review](docs/production-funding-review.md) describe missing permitted contracts,
authentication/rate policies and authoritative status schemas for the other platforms.
Their historical research was not relabeled as current API success. No undocumented
endpoint, scraping adapter, inferred funds/status mapping or invented provider was added.
Because no other adapter is configured, there was no supported production feed to probe
for funded/claimed/completed/expired/cancelled schema compatibility.

## E. Funding classification and notification modes

Default `BOUNTYRADAR_NOTIFICATION_MODE=confirmed_only` retains authoritative funding
checks and high-priority requirements. No live funding provider is registered, so it
cannot produce new confirmed-funded alerts. Trust-list changes alone cannot fix that.

`include_unverified` is a new **explicit opt-in**, not enabled in production. It permits
clearly labeled positive reward claims/potential opportunities after complete availability,
assignment, competition, restriction and currency checks. Missing availability evidence
still blocks an alert; missing funding proof is disclosed rather than treated as funded.
Classification is persisted in the verification/outbox snapshot and rendered in subject/body:

| Classification | Meaning / observed evidence |
| --- | --- |
| CONFIRMED_FUNDED | Only a result verified against trusted authoritative funding records; exercised with fixtures, **zero live** |
| PLATFORM_LISTED_UNVERIFIED | Explicit platform-listing evidence, no funding guarantee; fixture coverage only, no platform adapter bundled |
| POTENTIAL_UNVERIFIED | GitHub reward claim or provisional opportunity; genuine live examples above |

Unverified emails explicitly say payment/funding are not guaranteed. Unavailable,
zero/no-price, assigned, contested or incompletely inspected findings do not bypass
the gate. A transition from an unverified listing to verified funding can produce
a separate funding-verified event. Ordinary unchanged scans do not create new events.

## F. Gmail test results

The configured recipient matches both the SMTP login account and the independently
connected Gmail account; no random or third-party recipient was used. Account details
and credentials are redacted here. The existing `test_message`, `NotificationOutbox`
test-attempt API and `SMTPProcessTransport.send` were used in an isolated SQLite outbox.
No production send-pending command was invoked.

| Test | Actual SMTP submission | Result | Wall time |
| --- | --- | --- | --- |
| TEST 1/3 connectivity-only preparation | None | Local probe tried replacing multipart content incorrectly; fixed with clear_content; reservation retained as preparation_failed | Not an SMTP attempt |
| [BountyRadar TEST 2/3] $5 Synthetic Bounty | One | Accepted; persisted sent | 4.510s |
| [BountyRadar TEST 3/3] $10 Synthetic Bounty | One | Accepted; persisted sent | 6.031s |

**Two actual messages attempted, two accepted, zero rejected/unknown submissions.**
Both bodies contain `THIS IS A TEST — NOT A REAL BOUNTY`. No preparation failure
was counted as an SMTP submission, no uncertain send was retried, and the three
reserved-attempt cap was not reset to send additional messages. The initial error
was in the audit harness, not an application transport defect.

The worker used SMTP_SSL with default certificate verification and authenticated to
Gmail. Both live submissions completed within the default 20s absolute worker deadline;
supervision awaited process completion/cleanup. No SMTP worker remained afterward.
The temporary SQLite has two sent attempts, one preparation_failed reservation and
integrity ok. Timeouts/authentication errors, retry/unknown states, process cancellation,
reaping and no automatic uncertain resend are additionally verified offline.

**Inbox receipt independently VERIFIED:** the read-only Gmail connector found exactly
these two TEST messages with INBOX labels, matching subjects and body markers. Their
Received headers record TLS 1.3 authenticated SMTP submission. Message IDs are
`1a14d40451b1a7e1` ($5) and `1a14d405aace0c11` ($10). Gmail internal receipt times
were 10:08:39 and 10:08:45 Nepal time. This is stronger evidence than SMTP acceptance
alone. The check did not mark, archive, label or otherwise modify messages.

Mocked tests cover synthetic $5/$10/$100 notifications, subject/body classifications,
trusted vs unverified funding, authentication/error paths, daily cap, duplicate
suppression, reward increases, worker shutdown and retries. No real source alert was
sent by these tests. Configured secrets were absent from the last 500 journal lines
checked; SMTP credentials pass to workers over stdin, not command-line arguments.

## G. SQLite and recovery

Production database is `/home/james/bounty/bountyradar/data/bounties.db`, not the
top-level `/home/james/bounty/data/bountyradar.sqlite3` previously opened in the IDE.
The `.sqlite3-wal` tab is a sidecar, not the application's configured main database.
No production data or WAL/SHM files were reset, renamed or deleted.

Read-only snapshot at 10:13:07: **50,740 issues, 280 verification records, zero outbox
events**, 207,986,688-byte main database and `PRAGMA quick_check=ok`. There were
**50,460 never-verified candidates** and 47 saved unfinished windows. Frozen discovery
coverage was still `2026-10-17T17:47:15Z`, with no completed-scan checkpoint.
Production continued changing under its timer, so these are timestamped counts.

The isolated live database has 62 issues, three verification records and integrity ok.
Overlapping queries deduplicated by issue identity. The synthetic four-interval tests
introduce $5, $10 and $100 at minutes 00/05/10, then increase $10 to $20 at minute 15.
They prove three unique issue rows, four notification events, no extra event from an
unchanged fifth scan, correct updated price, valid checkpoints and integrity. Both
fixture-funded and opted-in unverified variants pass.

The complete suite also runs real test-owned CLI process interruption/SIGTERM, overlap
locks, durable page/cursor recovery, transactional rollback, queue rotation, cooldown
recovery, subprocess parent-death handling and unknown delivery without duplicate resend.
These are offline IO fixtures, not production interruption experiments.

**Retention/latency are not healthy enough to claim dependable operation.** There is
no automatic retention policy. Broad searches add thousands of candidates per run,
but the normal verification batch is 20 (plus up to five eligible rechecks). At the
observed 50,460 unverified rows, even an ideal static backlog needs about 2,523 scans
or 210 hours at 20/scan; arrivals and time limits make actual latency worse. New
verification work can therefore wait days. The database is durable, but durability
does not prove timely alerting or bounded disk growth. No destructive pruning was done.

## H. Tests and checks

Commands ran in `/home/james/bounty/bountyradar`; tests mock HTTP/SMTP and write only
temporary databases. Dependencies were already installed; no package reinstall was needed.

| Command | Exit | Result | Duration |
| --- | --- | --- | --- |
| `.venv/bin/python -m pytest -q` | 0 | 194 passed; 0 failed/skipped/warnings | 41.66s |
| `.venv/bin/ruff check .` | 0 | All checks passed! | 0.011s |
| `.venv/bin/python -m pip check` | 0 | No broken requirements found. | 0.259s |
| `git diff --check` | 0 | No diagnostics | 0.002s |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | 0 | No diagnostics | 0.033s |
| `bash -n scripts/install.sh scripts/uninstall.sh` | 0 | No diagnostics | 0.002s |

The existing project files are untracked: each nonignored file was additionally
checked with `git diff --no-index --check /dev/null <file>` (aggregate exit 0,
no diagnostics). AST parsing of 32 source/test Python files passed. Initial lint
found three import/line-length diagnostics, all corrected. No pytest failures occurred.

Focused runs: 86 existing settings/discovery/verification/email tests passed in 26.21s;
35 initial all-price tests passed in 1.38s; expanded targeted checks passed 65 in
2.06s and 67 in 2.01s. Final suite has **194 tests, 41 more than the original 153**,
all passing in 41.66s. Earlier complete runs also passed 188 in 41.12s and 194 in
41.31s; the final run follows the completed code changes.

Live probe commands (exit 0):

```bash
.venv/bin/python /tmp/bountyradar-operation-audit/live_windows.py > /tmp/bountyradar-operation-audit/live_output.json
.venv/bin/python /tmp/bountyradar-operation-audit/smtp_probe.py
```

The first SMTP harness invocation exited 1 before transport invocation; its corrected
invocation exited 0 with the two submissions documented above. These scripts and
redacted metrics are retained in `/tmp/bountyradar-operation-audit/`; OS cleanup may
remove them. The report preserves the important results. GitHub documentation was
checked against its official search/rate-limit reference; no external platform was
claimed live based on a mock or documentation mention.

## I. Final functionality matrix

| Feature | Status | Evidence |
| --- | --- | --- |
| Scans every five minutes | PASS while awake/logged in; historical gaps unverified | Real 09:55–10:15 starts |
| Recently created issues | PASS for discovery; timely verification PARTIAL | All six live windows returned 200; one five-minute match |
| Recently updated issues | PASS for discovery; timely verification PARTIAL | Old issues with today's updated_at; backlog remains |
| $5 bounties | PASS | Extraction/verification/outbox fixtures and synthetic Gmail receipt |
| $10 bounties | PASS | Same, including $10→$20 update |
| $25+ bounties | PASS | All listed price fixtures; genuine live USD 150 claim |
| GitHub source | PASS | Auth + 17 searches + five inspection requests, all 200 |
| Other provider sources | NOT VERIFIED / not implemented | No registered production adapters |
| Authoritative funding checks | PASS with fixtures; live NOT VERIFIED | No concrete funding provider |
| SQLite storage | PASS | Production quick_check; live temp integrity; low-value persistence |
| Duplicate suppression | PASS offline/live query merge | Unique issue keys; unchanged scan no email event |
| Email generation | PASS | Classified messages; synthetic $5/$10/$100 fixtures |
| Real SMTP submission | PASS | Two accepted synthetic messages |
| Gmail inbox receipt | PASS | Independent INBOX records and matching TEST markers |
| systemd deployment | PARTIAL | Active timer; installed service retains old deadlines |
| Interrupted scan recovery | PASS offline process tests | SIGTERM, persisted cursor, lock release, restart |
| Backlog freshness / retention | FAIL for dependable latency | 50,460 unverified, no retention policy |

## J. Final verdict and minimum next work

1. **Automatic five-minute runs? Yes** in the observed awake/login segment; no claim
   of operation while powered off, asleep or logged out.
2. **Recent bounties found? Yes, genuine candidate/reward signals**, including a recent
   USD 150 claim. Recently created keyword hits can also be false positives. Bounded
   discovery does not prove every recent funded job is found or promptly inspected.
3. **All positive rewards? Yes for the supported parser/currency policies**, with
   the approved production minimum now zero. Explicit currency allowlists remain.
4. **Configured sources working? GitHub yes.** No other provider is actually configured.
5. **Gmail SMTP working? Yes**, two controlled messages accepted and independently
   found in the inbox.
6. **Recipient verified? Yes**, it matches SMTP and connected Gmail account identity.
7. **What prevents dependable alerts?** No live funding provider; production remains
   confirmed-only by design; large stale verification backlog; unbounded retained
   candidate growth; installed unit timeouts are still old. Unverified mode exists
   but was not authorized for production actual bounty mail.
8. **Minimum remaining work:** approve/refresh installed units; choose whether to
   operate with explicitly labeled unverified alerts or implement a permitted
   authoritative funding adapter; narrow discovery and establish a fair fresh/backlog
   verification budget plus non-destructive retention/archive policy. Measure complete
   checkpoint advancement and recent-issue latency under that policy before declaring
   dependable unattended alerting. Merely raising request limits risks GitHub quotas.

The report's overall NO-GO applies to dependable funded-bounty alerts. Candidate
collection and Gmail transport are working components. There is no need to rebuild.

The approved minimum-only change is already effective. The following service refresh
commands were **not executed** and still require separate approval. Run each only
after the prior step succeeds; preserve the database and private configuration:

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

The installer enables the timer; persistent catch-up may run immediately. Existing
production email remains enabled but confirmed-only. To opt into actual unverified
bounty mail, separately approve changing `BOUNTYRADAR_NOTIFICATION_MODE` to
`include_unverified`; this is not part of the synthetic Gmail authorization and was
not done. Foreign-currency alerts additionally need explicit currency/floor settings.
No further SMTP test is needed to establish the submission/inbox path proved here.
