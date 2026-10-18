# BountyRadar

Local-first discovery and evidence review of potential open-source bounty issues.
Phase 1 searches public open GitHub issues through the official REST Search API.
Phase 3 adds issue availability inspection, evidence classification, ranking and
notification eligibility. A detected amount is **provisional** until an injected,
trusted funding adapter supplies an authoritative record.
Phase 4 adds a durable SQLite email outbox and opt-in Gmail SMTP delivery.
Phase 5 adds local findings, a complete-scan checkpoint, and a systemd user timer.

## Setup

Requires Ubuntu 24.04 and Python 3.12 or newer.

```bash
cd /home/james/bounty/bountyradar
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
[ -e .env ] || cp .env.example .env
chmod 600 .env
# Set GITHUB_TOKEN in .env, or export it in your shell.
python -m bountyradar scan --hours 24
python -m bountyradar scan --hours 1 --json
```

`--help` and `--version` work without loading settings or contacting GitHub.
The `scan` command performs live requests. Exit code 0 means all windows were
processed and all returned issue objects were valid; exit code 2 means results
may be incomplete. JSON output includes candidates, request and invalid-item
counts, and `unprocessed_windows` with reasons. `scan` does not write the database or send email.

`--hours` overrides all three lookback windows. Without it, created and updated
issues each have a 24-hour window. A 72-hour updated window also searches monetary
terms on issues created before the created-issue cutoff. These are configurable
in `.env`; all windows are calculated from the current UTC time.

The default search terms are `bounty`, `paid`, `reward`, `cash reward`, and
`paid issue`; labels are `bounty` and `paid`; the default monetary term is `USD`.
List settings in `.env` use JSON arrays. `BOUNTYRADAR_LANGUAGES=[]` means all
languages. When set to several languages, each is searched separately.
`GITHUB_TOKEN` enables authenticated requests. Search requests run serially
and use GitHub's authenticated or unauthenticated search-rate spacing. Scheduled
`run` requires a token because issue revalidation can otherwise exhaust the
unauthenticated REST quota. `BOUNTYRADAR_GITHUB_MAX_SEARCH_REQUESTS_PER_SCAN`
caps each scan (default 60); exhaustion leaves the checkpoint unchanged and
persists unprocessed queries/pages for a later run. Search spacing is at least
2.1 seconds with a token or 6.1 seconds without one, and can be increased with
`BOUNTYRADAR_GITHUB_MIN_REQUEST_INTERVAL_SECONDS`. GitHub's rate headers and
`Retry-After` are honored within the configured maximum wait; failed or
incomplete windows are reported. `BOUNTYRADAR_MIN_REWARD_USD=0` (the default)
accepts every strictly positive USD reward, including fractional amounts. Set a
larger value only to opt into a floor. Existing `.env` files with a value of 25
must be explicitly updated; defaults do not override configured values.
The floor is a later qualification threshold, not a discovery filter. Discovery keeps even
candidates without a parsed amount for later review.

For scheduled execution, put `GITHUB_TOKEN` in `.env`; exporting it in the
installer's shell does not configure systemd. Create an expiring personal access
token in GitHub Settings > Developer settings > Personal access tokens. Use
the minimum access needed for public issue searches and inspection, with no write
permissions. See GitHub's [token setup guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
Do not put credentials in commands, logs, or version control. Settings use the
`BOUNTYRADAR_` prefix except the supported aliases `GITHUB_TOKEN` and
`EMAIL_ENABLED`. Environment values override `.env` for manual commands.

## Architecture and limits

`settings.py` validates configuration; `discovery.py` owns an injected
`httpx.AsyncClient` and an injected clock/sleep for offline testing;
`models.py` defines typed UTC candidates; `cli.py` renders discovery output.
`github_inspector.py` fetches one candidate issue, its comments and timeline,
and PRs directly referenced by timeline events. It records PR mentions separately
from explicit issue links, so an unrelated repository's `Fixes #8` does not
count as a competing PR for this issue. It uses in-memory ETag caching
and reports missing or truncated sources. `verification.py` classifies evidence
and accepts an optional `FundingProvider` through dependency injection.
`ranking.py` scores results from 0 to 100 and applies alert filters separately.
`high_priority_queue()` selects only verified, available, eligible results.
Queries exclude pull requests, deduplicate by GitHub issue ID, paginate up to
the GitHub search cap, and split broad time windows where possible. The search
response supplies issue fields, so no per-issue detail fetch is needed.

A title, body, label, or money expression is only a signal. The issue URL is
retained as original evidence. Funding added only in comments or on an external
platform may escape GitHub issue search, even when the issue is updated.
`FUNDED_EVIDENCE` requires a funding record tied to the exact issue URL from a
provider and HTTPS host explicitly trusted in settings, with a funding
observation no older than `BOUNTYRADAR_FUNDING_EVIDENCE_MAX_AGE_HOURS`. GitHub text alone yields
`REWARD_CLAIMED` or `UNCERTAIN`; closed, cancelled, expired or paid issues yield
`UNAVAILABLE`. The default trust lists are empty, and this repository has no
concrete external funding provider, so live results cannot become funded yet.
Unknown currencies remain in review results, with no guessed conversion. Non-USD
notification filters require both an allowed currency and an explicit threshold
in `BOUNTYRADAR_CURRENCY_MINIMUMS`. Ranking weights are configurable through
`BOUNTYRADAR_SCORE_WEIGHTS` and must total 100. Missing
PR links mean only “not found in checked sources,” never proof that nobody is
working on the issue. The score is a review heuristic, not a payment guarantee.

`python -m bountyradar run --dry-run` performs discovery and verification without
database writes or email. `run` without `--dry-run` persists candidates and
verification results, enqueues qualifying transitions, and attempts delivery when
`EMAIL_ENABLED=true`. It advances the scan checkpoint only after all search
windows complete and notification transitions are queued. The next run expands its lookback to cover downtime or incomplete
scans. A file lock prevents concurrent `run` processes; systemd also keeps its
oneshot service from overlapping itself. The default funding trust lists are
empty and no concrete funding provider is bundled. With the default
`BOUNTYRADAR_NOTIFICATION_MODE=confirmed_only`, live runs therefore produce no
new funded opportunities or alert emails. Integrators can inject
a `FundingProvider` into `cli._run` after configuring trusted provider names and
HTTPS hosts; this is exercised in the offline integration test. ETag cache is
in-memory. Large catch-up windows span multiple bounded runs. Each completed
search page commits its candidates and continuation together; the next run
resumes that frozen window. Once it completes, the following scan extends its
lookback from the saved coverage time to include issues arriving during recovery.
Previously eligible issues are rechecked directly in a bounded batch even if
GitHub's open-issue search omits them after closure. This lets a later reopening
become a new availability transition. An issue that closes and reopens entirely
between scans may still escape observation. Previously discovered but unverified
claims also receive rotating verification, so an injected funding provider can
detect new external funding without a GitHub update. The main durable queue
checks up to `BOUNTYRADAR_MAX_VERIFICATIONS_PER_SCAN` (default 20), serving
never-verified candidates first and then rotating by last check time. Previously
eligible issues receive up to `BOUNTYRADAR_MAX_RECHECKS_PER_SCAN` (default 5)
additional checks. Large or continually growing backlogs can delay revisits.
An old issue never discovered by GitHub search cannot be revisited this way.

Default execution controls:

| Setting | Default | Scope |
| --- | --- | --- |
| `BOUNTYRADAR_RUN_TIMEOUT_SECONDS` | 240 | Cooperative timeout around the complete `run` |
| `BOUNTYRADAR_GITHUB_SEARCH_TIMEOUT_SECONDS` | 120 | Search, including pacing, retries and rate waits |
| `BOUNTYRADAR_GITHUB_MAX_SEARCH_REQUESTS_PER_SCAN` | 60 | Search HTTP attempts, including retries |
| `BOUNTYRADAR_GITHUB_INSPECTION_TIMEOUT_SECONDS` | 90 | Shared inspection time from its first request |
| `BOUNTYRADAR_GITHUB_MAX_INSPECTION_REQUESTS_PER_SCAN` | 60 | All inspection/revalidation attempts, including retries |
| `BOUNTYRADAR_SMTP_DELIVERY_TIMEOUT_SECONDS` | 20 | Absolute lifetime of each SMTP worker, including DNS and disconnect |

HTTP waits are cancellable. Rate-limit cooldowns are persisted before waiting,
so interruption does not immediately retry against GitHub's requested cooldown.
SIGTERM cancels `run`, closes HTTP/SQLite resources, releases the database lock,
and exits 143. A global timeout exits 2 and preserves committed work; an incomplete
scan also exits 2, so a systemd failed state can mean deferred work rather than
data loss. Signal handlers are restored on exit.

HTTP/application deadlines are cooperative. Production SMTP uses a separate
supervised Python process, not an executor thread. Its default 20-second absolute
deadline includes DNS, login, send and disconnect; the separate
`BOUNTYRADAR_SMTP_TIMEOUT_SECONDS` remains a socket-operation timeout.
Cancellation or deadline expiry kills and reaps the worker before releasing the
run lock. An interrupted or ambiguous send becomes `unknown_delivery`, never an
automatic retry. Linux parent-death protection kills a running worker if the
scanner dies abruptly; its durable `sending` row becomes unknown after lease
expiry. No delivery worker accesses SQLite. These guarantees were tested with
real subprocesses and mocked SMTP/DNS, not real Gmail. They depend on normal OS
process scheduling and termination, not a hard real-time guarantee.

## Email outbox

Copy `.env.example` to `.env` and set `EMAIL_ENABLED=true`,
`BOUNTYRADAR_SMTP_USERNAME`, `BOUNTYRADAR_SMTP_PASSWORD` (a Gmail app password),
and `BOUNTYRADAR_EMAIL_TO` to enable delivery. The transport uses `SMTP_SSL` on
port 465, and can be replaced through the asynchronous `MailTransport.send`
interface. The worker retains the standard-library Gmail authentication and
message formatting; credentials pass over stdin, not command-line arguments.
The isolated worker requires Linux, matching the Ubuntu deployment. Enable
Google 2-Step Verification, then create an app password as described in
[Google's instructions](https://support.google.com/mail/answer/185833?hl=en);
do not use the account's normal password. Email is
disabled by default. `python -m bountyradar email test` sends one test email **only
when explicitly invoked with email enabled** and counts toward the UTC daily cap.
`python -m bountyradar email pending` lists unsent and unresolved entries; add
`--json` for machine-readable output. `python -m bountyradar email send-pending`
revalidates due issues, then sends eligible entries. Both `run` and `send-pending`
can send real email when enabled; neither was run with real credentials in tests.

In `confirmed_only` mode an alert requires trusted funding evidence tied to the exact issue, a
configured currency threshold, an open and available issue, no assignee or known
competing work, and a high-priority score. Ordinary issue edits do not create a
new alert. New funding, a newly available issue, or a configured material reward
increase can create one. SQLite stores deterministic event keys, attempts,
retries, and a daily cap. Startup recovery marks an expired in-flight attempt
`unknown_delivery`; it is not automatically resent. An operator can reconcile
that state through the `NotificationOutbox.resolve_unknown()` library API after
checking their mailbox. SMTP cannot guarantee exactly-once delivery after an
ambiguous network failure: a server may accept a message before the client loses
the response. This policy favors avoiding duplicate alerts.

`BOUNTYRADAR_NOTIFICATION_MODE=include_unverified` explicitly opts into monetary
reward promises and potential opportunities. These emails display
`POTENTIAL_UNVERIFIED`, or `PLATFORM_LISTED_UNVERIFIED` only when an adapter supplies
platform-listing evidence, in the subject and body, with a payment warning. They
still require a strictly positive amount, an allowed currency with its own threshold,
complete availability checks, no assignee, competition or restrictions, and the
normal deduplication/daily cap. Their review score does not impose a hidden price
floor. They never become `CONFIRMED_FUNDED` without authoritative funding evidence.
No external platform adapter is bundled. Non-USD amounts are retained without
conversion; configure their currency in `BOUNTYRADAR_ALLOWED_CURRENCIES` and an
explicit floor (zero for all positive amounts) in `BOUNTYRADAR_CURRENCY_MINIMUMS`.

## Ubuntu background deployment

The user timer runs at minutes 0, 5, 10, and so on in local time. The laptop must
be awake and connected to the internet, and the user systemd manager must be
running. Enabling the timer starts it again on login. Linger was disabled on the
validated laptop; scanning while logged out is not configured by these scripts.
Suspension and power-off prevent scans.
`Persistent=true` asks systemd to trigger a missed timer occurrence when the
timer resumes; BountyRadar's complete-scan checkpoint extends the lookback over
the actual outage. The repository unit uses `Type=oneshot`, `TimeoutStartSec=270s`,
`TimeoutStopSec=25s`, and `SuccessExitStatus=143`. These leave room after the
default 240-second application budget and provide a final kill deadline of
approximately 295 seconds if normal cancellation fails. Keep custom application
budgets below the unit timeout and allow for cleanup. The timer does not start
another copy of an active service; a direct overlapping `run` logs `scan_delayed`.
The lock is per database path, so every invocation must use the same configured
database. See the [systemd timer documentation](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).

Set `GITHUB_TOKEN` and the other options in the private `.env` file. The install
script requires the SQLite path to stay under the project `data/` directory,
which the service is allowed to write. Keep `EMAIL_ENABLED=false` until an
authoritative funding adapter and trusted provider settings are configured if
you do not want the SMTP path active. The installer validates `.env` independently
of shell overrides, requires a nonempty token, sets `.env`/existing database to
mode 600 and `data/` to 700, verifies units, and enables the timer. It refuses to
replace units while the service is active or starting. The test command sends a real email only
when explicitly invoked with email enabled.

```bash
cd /home/james/bounty/bountyradar
./scripts/install.sh
systemctl --user list-timers bountyradar.timer
journalctl --user -u bountyradar.service -n 100 --no-pager
systemctl --user disable --now bountyradar.timer  # stop scheduled scans
systemctl --user stop bountyradar.service        # cancel a currently running scan
systemctl --user enable --now bountyradar.timer   # start again
```

To scan manually without sending email, use
`./.venv/bin/python -m bountyradar run --dry-run`. To run the complete workflow
manually, use `systemctl --user start bountyradar.service`. Check the outbox with
`./.venv/bin/python -m bountyradar email pending --json`; use `email send-pending`
to revalidate and deliver due entries. Review `unknown_delivery` entries manually
before resolving them through the library API. `journalctl --user -u
bountyradar.service -f` follows structured run records.

Before updating code or reinstalling units, explicitly disable the timer and stop
the service using the commands above. Wait for it to become inactive, update the
checkout, run `./.venv/bin/pip install -e ".[dev]"`, run the offline tests, then run
`./scripts/install.sh` to refresh the units and enable the timer again. Inspect
`systemctl --user cat bountyradar.service`, timer status and logs afterward.
Keep `.env` and the existing database. Do not replace a live SQLite file or copy
only its main file while WAL writes are active.

`./scripts/uninstall.sh` disables and removes only the BountyRadar user units;
it retains `.env` and `data/`. If stopping the service fails, it retains the unit
files and exits with an error. The timer uses the existing virtual environment
and does not require an open terminal, VPS, Docker, or a system-wide service.
Laptop shutdown, network outages, GitHub rate limits, and the absence of a
funding adapter can delay or prevent alerts. No exact-once SMTP guarantee exists.

For current blocker status, SMTP lifecycle evidence, provider research and
approval-only activation commands, see
[PRODUCTION_READINESS_REPORT.md](PRODUCTION_READINESS_REPORT.md). The earlier
[Step 7 report](STEP7_DEPLOYMENT_REPORT.md) is a historical audit, not the current
SMTP implementation status. No real funding provider is enabled: changing the
trust lists alone does not connect one.

## Development

```bash
source .venv/bin/activate
pytest -q
ruff check .
git diff --check
```

Tests mock GitHub requests with `respx` and never send email. See [AGENTS.md](AGENTS.md)
for contributor rules. Runtime data, logs, environments, and credentials are
ignored by Git.

Deployment checks:

```bash
.venv/bin/python -m pytest -q tests/test_deployment.py tests/test_deployment_scripts.py tests/test_end_to_end.py
systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer
.venv/bin/python -m pip check
```

Deployment script tests use disposable project/configuration directories and
stub systemd commands. Real SIGTERM tests target test-owned CLI subprocesses.
The dense-backlog simulation models pacing with zero-latency HTTP responses;
it establishes durable bounded work and resumption, not real GitHub throughput.
See [STEP7_DEPLOYMENT_REPORT.md](STEP7_DEPLOYMENT_REPORT.md) for this checkout's
verification evidence, installed-unit differences, and outstanding blockers.

GitHub API references: [Issue timeline events](https://docs.github.com/en/rest/issues/timeline),
[Issue comments](https://docs.github.com/en/rest/issues/comments),
[Issue event types](https://docs.github.com/en/rest/using-the-rest-api/issue-event-types),
[Search issues and pull requests](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests),
[REST API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api),
[REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

# Multi-platform source status

The source interface, per-source checkpoints and cross-source GitHub issue
deduplication are in place. Only the GitHub adapter is enabled. Algora, Opire
and other investigated services are documented in
[`docs/phase7-sources.md`](docs/phase7-sources.md); no undocumented platform
endpoints or HTML scraping are used.
# Bounty-Finder-
