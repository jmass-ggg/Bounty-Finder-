# ByteAsk implementation handoff

Prepared 2026-10-18 for Codex review. Implementation is complete locally;
production activation is deferred until Codex reviews and merges this branch
and the user supplies the merged commit in a separate deployment request.

## Branch and commits

- Repository: https://github.com/jmass-ggg/Bounty-Finder-
- Branch: `feat/production-gmail-alerts`
- Base: `5d497e5` (existing `main`, preserved).
- Implementation commit: [`1a6a6c2401bbebb24c98f870f1cadc8c73b9aba2`](https://github.com/jmass-ggg/Bounty-Finder-/commit/1a6a6c2401bbebb24c98f870f1cadc8c73b9aba2)
- Remote publication: implementation pushed successfully, independently verified
  through HTTPS `git ls-remote`. This handoff follows as a documentation commit
  on the same branch; final branch-tip verification accompanies the handoff reply.
- [Remote review branch](https://github.com/jmass-ggg/Bounty-Finder-/tree/feat/production-gmail-alerts)
- [Compare with main](https://github.com/jmass-ggg/Bounty-Finder-/compare/main...feat/production-gmail-alerts)
- Review checkout: `/home/james/bounty/bountyradar-production-gmail-alerts`.
- Production checkout: `/home/james/bounty/bountyradar`, left on clean `main`.

The installed service executes the production checkout directly. A separate
worktree and virtual environment prevented scheduled scans from loading branch
edits. No merge, force push, installed-unit refresh, timer operation or private
configuration change was performed.

## Result and changes

The existing GitHub → claim/reward verification → SQLite outbox → supervised
Gmail SMTP path now has delivery capacity reserved within the same scan.
All positive USD amounts remain supported with a zero default minimum.
There is still no authoritative funding provider: production must explicitly
opt into clearly labeled unverified reward promises.

| Files | Change |
| --- | --- |
| `src/bountyradar/cli.py`, `verification.py` | Reserve inspection requests/time for delivery revalidation; persist each completed verification and alert incrementally; count ordinary unverified eligibility; emit structured scan metrics |
| `discovery.py` | Recent lane retains every configured term/label/monetary query, alongside its independent durable continuation and the historical lane |
| `github_inspector.py` | Unedited maintainer reward announcements have distinct provenance; unsupported self-claims remain UNKNOWN; validate malformed availability/PR data; open or merged relevant implementation PRs block availability |
| `outbox.py` | Persist fixed initial-unverified activation boundary; suppress old promises without new reward evidence; preserve pending work; bounded due-row selection and deferred-evidence rotation |
| `delivery.py` | Per-invocation SMTP attempt cap, early daily-cap check, stable Message-ID, current price-conflict deferral |
| `notification.py`, `sources.py` | Escaped multipart mobile HTML, canonical GitHub issue button and plain link; reject unsafe URLs; explicit payment warning and synthetic test marker |
| `settings.py`, `.env.example` | Configurable batch cap and initial notification lookback; private production settings untouched |
| `deployment/systemd/bountyradar.service`, `.timer` | Explicit control-group termination/SIGKILL; login-manager startup trigger plus persistent five-minute calendar; retain correct venv, private env, working directory, 270s/25s/143 limits |
| `tests/test_production_alerts.py` | New offline rendering, URL, batch/daily limits, activation, old-announcement, small-bounty, interruption, delivery-budget, PR and recent-query regressions |
| Existing email, deployment, end-to-end, source-review, run-state, SMTP and verification tests | Preserve assertions while updating subjects, JSON event parsing, required payload fields, injected contracts and worktree-safe sandbox path substitution |
| `README.md`, this handoff | Explain behavior, limits, metrics and deployment separation |

The unverified activation boundary is recorded once as UTC minus
`BOUNTYRADAR_NOTIFICATION_INITIAL_LOOKBACK_HOURS` (24 by default). It never rolls
forward. Old issue updates alone cannot create initial alerts: an issue must
have been created since that boundary or have an unedited maintainer reward
announcement since it. Existing pending alerts survive. Later offline coverage
is not subject to a three-hour or daily rolling age cutoff.

One initial event is emitted per qualifying issue. Existing meaningful-change
events remain: reward increases, funding verification and observed availability
returns. Uncertain SMTP acceptance is never automatically resent.

## Actual verification

Commands below ran in the isolated review checkout and its own Python 3.12 venv.
The original production checkout's baseline suite was also rerun: **250 passed
in 44.67s**. Earlier reported test counts were not substituted for new executions.

| Command | Final result |
| --- | --- |
| `.venv/bin/python -m pip install -e '.[dev]'` | Exit 0, installed isolated editable package and dev dependencies |
| `.venv/bin/python -m bountyradar --help` | Exit 0 |
| `.venv/bin/python -m pytest -q --junitxml=/tmp/bountyradar-production-alerts-tests.xml` | Exit 0, **277 passed in 44.93s**, no failed/skipped tests |
| `.venv/bin/ruff check .` | Exit 0, all checks passed |
| `.venv/bin/python -m pip check` | Exit 0, no broken requirements |
| `git diff --check` | Exit 0 |
| `bash -n scripts/install.sh scripts/uninstall.sh` | Exit 0 |
| `systemd-analyze --user verify deployment/systemd/bountyradar.service deployment/systemd/bountyradar.timer` | Exit 0, no diagnostics |

Development failures were resolved before the final run: new subject/output
contracts, outdated sandbox path assumptions and incomplete test payloads;
one newly added fake transport supplied the wrong fixture argument type.
The last stricter-body validation run had five fixture failures (271 passed);
fixtures now include the normal nullable `body` field and an additional missing-body
regression verifies that incomplete data remains UNKNOWN.
The final suite was green; Ruff then found one 101-character line in the new PR
evidence description. That string was wrapped without changing behavior and all
remaining checks were rerun successfully.

Coverage includes repeated five-minute scans; fresh $1/$5 discovery through
same-scan fake SMTP; all positive amount boundaries; assigned/maintainer-claimed,
reserved, relevant open/merged PR and ambiguous-claim exclusion; reward removal
and conflicting prices before delivery; unchanged deduplication; 5/8/12/24-hour
catch-up; persisted notification eligibility after four days offline; interrupted
pages and per-issue verification recovery; 50,000 old candidates with fresh
priority and older-work fairness; 403/429/network/cooldown handling; SMTP
retryable/permanent/unknown outcomes; hostile HTML/URLs; real test-owned process
SIGTERM, timeout, lock, worker-parent-death and kill/reap behavior.
Tests use temporary SQLite, mocked HTTP and fake mail. They never send email.
Unit syntax verification does not substitute for deployment observation.

## Measurements and live checks

No production findings were deleted, pruned, reset or replaced.

- Initial read-only snapshot: 79,916 candidates, 580 verification rows, no
  notification outbox events or attempts, 43 historical windows unfinished.
- Consistent SQLite backup for local measurements: 80,328 candidates,
  600 verification rows, **79,728 never verified**, 40 incomplete windows,
  **341,012,480 bytes**. Snapshot creation 0.368s; integrity `ok`.
- Three selections of 20 on that copy: **404.149 / 414.454 / 403.638 ms**,
  each with 15 recent and 5 older slots. Aggregate metrics query: 1.167ms.
- Separately executed 50,000-candidate test with printed timings:
  selection **0.056s**; eight mocked verifications including fresh work
  **0.058s** from queue entry; creation plus integrity **0.821s**.
  These are local fake-IO measurements, not live GitHub latency.
- Six observed scheduled production runs returned 3,997–4,977 search candidates
  per scan, took 197.69–210.88s, and queued/sent zero mail. Those counts include
  rediscovery; the old logger did not distinguish newly inserted rows or expose
  API usage. New `scan_metrics` reports those separately, plus verification
  counts, HTTP attempts, backlog, windows, size and delivery outcomes.
- Read-only authenticated GitHub `/rate_limit` returned HTTP 200: core remaining
  4,956, search remaining 29 at the check. No discovery or real-mail production
  command was manually invoked with the feature code.

## One authorized Gmail test

Exactly **one** additional synthetic message was submitted to the existing
configured, previously verified recipient. Sender/recipient identity matched
privately; neither address nor credentials are disclosed here.

- Subject: `[BountyRadar PRODUCTION TEST] HTML Gmail Alert`
- Body marker: `THIS IS A TEST — NOT A REAL BOUNTY`
- Shared HTML/plain renderer, real button, canonical issue link and text fallback.
  The message explicitly said it was a synthetic link-format test with no
  bounty eligibility assertion.
- SMTP accepted in **3.707s**; one durable temporary test attempt marked sent.
- Independently found in **INBOX** by matching Message-ID and exact subject,
  using IMAP `EXAMINE`/read-only selection and `BODY.PEEK` header retrieval.
- Message-ID: `<bountyradar-production-test-b73e1d1d93c842d781524c4dbd9ad531@bountyradar.local>`
- Local private audit: review checkout `data/html-production-test/attempt.json`
  and `test-outbox.db`, ignored by Git. An exclusive reservation prevents
  accidental repeat submission.

No real bounty email was sent. Gmail/Android push depends on account/device
notification settings; the Python service does not configure them.

## Security and current service

Configured-secret/address and credential-pattern scans found no matches in
all 51 indexed implementation files and 72 existing-history blobs. The handoff
also received the same configured-value/pattern check before publication.
`.env`, credentials, SQLite files/WAL/SHM/locks, logs, `.venv`
and data directories are excluded; nine representative ignore checks passed.
The private production `.env` is mode 0600. HTML content is escaped, canonical
action links reject userinfo/ports/query/fragment/other hosts, HTTP authorization
stays on GitHub's API, and SMTP credentials travel over worker stdin rather than
argv. No remote source or runtime AI dependency was introduced.

This is a local code/configuration check, not a claim of a complete dependency
vulnerability audit or an external penetration test. Codex still owns the
independent security and pull-request review.

The existing production timer remains enabled and active. Its service uses the
correct original venv, `.env` and working directory but still has obsolete
**1-hour startup / 90-second shutdown** limits. Private mode remains
`confirmed_only`, email enabled, USD minimum 0 and daily cap 20.
It continues collecting candidates; this branch has not been activated.
At the final service check the timer was active/running and the existing oneshot
was activating/start with a live scanner PID. Remote `main` was independently
verified still at `5d497e550785bdb3e88c0dce7e286f300b4ac76c`.
Exit 2 / a failed oneshot can indicate deliberately incomplete bounded work,
while the timer remains scheduled.

## Known limitations and deployment after merge

- No concrete authoritative funding provider. Include-unverified emails describe
  advertised promises; payment and availability are not guaranteed.
- Large discovery/verification backlogs remain. Fair priority and bounded
  delivery do not guarantee complete catch-up latency under continuous growth.
  No destructive retention policy was added.
- GitHub search is not an exhaustive feed. Index delays beyond overlap/recent
  coverage, repeated result churn, truncated inspections and undiscovered
  comments/external announcements can delay or hide opportunities.
- Claim/reward language is conservative and heuristic. Conflicting historical
  prices, edited reward comments or incomplete evidence may defer valid work.
  Old body/title reward edits without reliable dated announcements are retained
  for review rather than treated as newly rewarded.
- Real reboot/login, installation of this branch, real scheduled unverified
  delivery and rollback were intentionally deferred. No successful real bounty
  alert is asserted.

Only continue deployment after the user supplies Codex's merged commit:

1. Fetch and verify that commit; inspect working trees and Codex changes before
   updating production. Stop scheduled starts and let/cancel the active scan
   cleanly. Inspect pending, sending and unknown delivery attempts first.
2. Preserve private `.env`; create a consistent SQLite backup through SQLite's
   backup API, including committed WAL content. Save installed unit files and
   settings for rollback. Do not copy a live main database file alone.
3. Update the safe production checkout to the reviewed merge and refresh its
   existing venv. Set these private values, retaining all credentials and other
   settings:

   ```dotenv
   BOUNTYRADAR_NOTIFICATION_MODE=include_unverified
   BOUNTYRADAR_MIN_REWARD_USD=0
   BOUNTYRADAR_EMAIL_ENABLED=true
   BOUNTYRADAR_EMAIL_PER_SCAN_CAP=2
   BOUNTYRADAR_EMAIL_DAILY_CAP=10
   BOUNTYRADAR_NOTIFICATION_INITIAL_LOOKBACK_HOURS=24
   ```

   `EMAIL_ENABLED` is also supported and takes precedence if both enablement
   aliases are present. Keep one unambiguous value. Do not reset the persisted
   activation boundary or discard pending/unknown events.
4. Run checks, then `./scripts/install.sh` in the original production directory.
   It refuses to refresh an active service, validates private settings and
   paths, refreshes systemd, and enables/starts the timer. Persistent catch-up
   can start immediately: make backup and settings changes first.
5. Verify effective venv/workdir/env path, 270s/25s/143 limits, control-group
   killing, `OnStartupSec=30s`, five-minute calendar and timer enablement.
   Inspect several actual scheduled `scan_metrics`/completion records and
   SQLite outbox progress. Reconcile unknown SMTP outcomes manually.
6. Verify naturally eligible mail through Gmail; retain conservative caps.
   Do not fabricate a real bounty to pass acceptance. If deployment fails,
   stop starts/scanner, preserve new delivery outcomes, restore saved units
   and configuration, and re-enable only the known-good version. Never blindly
   restore an old outbox over newly accepted mail and cause duplicate resends.

Reference behavior was checked against the official
[systemd timer documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html),
[GitHub search documentation](https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28),
and [Gmail IMAP/SMTP documentation](https://developers.google.com/workspace/gmail/imap/imap-smtp).

The implementation was pushed using existing SSH authentication to the same
repository. `origin` retains its original HTTPS URL. No credential was placed
in a URL or helper. Only the feature branch was pushed; main was not changed.
Codex can review and merge from the remote branch above. ByteAsk has not
created or merged a pull request and has not treated publication as deployment.
