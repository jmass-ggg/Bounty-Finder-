# Codex production release review

Reviewed 2026-10-18, Asia/Kathmandu. **Release result: BLOCKED on GitHub PR
write access, not on missing authoritative funding integration.** Code review,
local validation and the first GitHub Actions run pass after the corrections
below. Production activation remains ByteAsk's separate task.

## A. Repository and PR

- Repository: https://github.com/jmass-ggg/Bounty-Finder-
- Base: `main`, independently fetched at
  `5d497e550785bdb3e88c0dce7e286f300b4ac76c`.
- Head: `feat/production-gmail-alerts`.
- Expected ByteAsk tip verified locally and remotely:
  `18c9d37dc6e1e89b978e20ca606c700261413b0b`.
- ByteAsk implementation: `1a6a6c2401bbebb24c98f870f1cadc8c73b9aba2`.
- Codex correctness/CI commit:
  [`df8e6d7745c67d52352ef3fe98ce9ec9bdb05beb`](https://github.com/jmass-ggg/Bounty-Finder-/commit/df8e6d7745c67d52352ef3fe98ce9ec9bdb05beb).
- PR URL: **not available; creation rejected HTTP 403, Resource not accessible
  by integration**. A search found no existing PR for this branch.
- Merged SHA: **not available; main was not modified**.
- [Review/PR creation comparison](https://github.com/jmass-ggg/Bounty-Finder-/compare/main...feat/production-gmail-alerts).

The local review worktree is `/home/james/bounty/bountyradar-production-gmail-alerts`.
The production checkout remains clean on its original main commit. No parent
repository, installed unit, private setting or production database was changed.
No history was rewritten; the feature branch was pushed normally through existing
SSH authentication. Origin remains the HTTPS URL. GitHub's normal PR merge must
be completed after authentication; a direct main push is not a substitute.

## B. Reviewed ByteAsk implementation

Reviewed all 26 changed files and relevant surrounding discovery, verification,
storage, delivery and worker code, plus AGENTS.md, README, handoff and existing
audit reports. The branch matches its reported commit and descends directly
from main. Changes are scoped to the monitoring/alerting workflow and tests.

The branch reserves inspection requests/time for immediate delivery revalidation,
persists completed verifications and notification transitions incrementally,
retains all configured queries in the durable recent lane, and adds scan/backlog
metrics. Notification delivery has bounded due-row selection, deferred-evidence
rotation, per-scan/daily caps and a fixed initial unverified activation boundary.
Old updates alone do not turn historical promises into initial alerts.
The boundary survives restart and does not impose a rolling cutoff after downtime.

Multipart HTML escapes source text and contains a canonical GitHub issue button,
plain link and payment disclaimer. Claim validation distinguishes accepted
claims, ambiguous self-claims and relevant open/merged implementation PRs.
Systemd adds a startup trigger and explicit control-group termination while
retaining the five-minute calendar and bounded service deadlines.

## C. Independent engineering findings

| Severity | Location | Evidence, cause and impact | Correction/status |
| --- | --- | --- | --- |
| HIGH | `github_inspector.py:174`, comment reward processing | A reward comment containing `Bounty $5; deadline: 2020-01-01` produced AVAILABLE and could pass pre-send review: deadlines were checked only in issue text. Reproduced before correction. | FIXED: inspect explicit dates in positive reward comments; expired evidence sets terminal availability. Regression checks verification and pre-send rejection. |
| HIGH | `github_inspector.py:149`, issue/PR body validation | Missing/null/false/list comment bodies were coerced to empty strings, allowing APPARENTLY_UNCLAIMED without readable evidence. False-valued malformed issue/PR bodies had the same normalization gap; missing PR body was also accepted. | FIXED: require string comment bodies; allow only null/string issue/PR bodies with required keys. Unreadable evidence becomes UNKNOWN. Nine malformed-payload cases cover the boundaries. |
| MEDIUM | `.github/workflows/ci.yml` (previously absent) | No remote checks protected the proposed release. | FIXED configuration; actual Actions success independently observed on the reviewed code commit. Verify latest documentation head and PR checks before merge. |
| MEDIUM | `cli.py:195`, verification/delivery budgets | Delivery reserves 25% only with at least four inspection requests; complex comments/PRs or small custom budgets can consume delivery capacity. Global runtime also bounds SMTP. | ACCEPTED documented limitation: same-scan attempt is conditional on complete checks and remaining capacity; durable pending work survives. No eligibility bypass to force a send. |
| MEDIUM | `run_state.py:129`, `discovery.py:118` | Queue priority/fairness and independent search cursors do not guarantee catch-up drainage under continuous noisy arrivals. Frozen recent pagination can itself lag; indexing is not an exhaustive feed. | OPEN operational measurement/retention work; not represented as solved or as guaranteed instant alerts. No destructive cleanup. |
| MEDIUM | `github_inspector.py`, `verification.py` | Claim/reward language and dates are heuristic. Conflicting historical comments and expired announcements can conservatively suppress a later valid offer. Edited comment creation dates do not date a new reward. | ACCEPTED RISK: defer ambiguity; avoid false availability/funding promises; preserve evidence. No universal language/date parser claimed. |
| LOW | `cli.py`, `run_complete.high_priority_eligible` | This legacy summary key now contains ordinary notification-eligible counts, including unverified results. | OPEN naming follow-up; `scan_metrics` queue/send counts and classifications remain the operational evidence. Do not interpret this key as a count of confirmed funds. |
| HIGH, release process | GitHub PR creation | Connector returned HTTP 403; local gh has no authenticated host. Existing SSH access permits branch pushes but cannot perform GitHub's PR merge API. | OPEN blocker: authenticate an authorized GitHub client; create/update PR and honor all repository rules/checks. |

No unresolved CRITICAL/HIGH code defect was identified in this bounded review
after the corrections. This is not a guarantee of defect-free code or dependable
full backlog coverage. No unrelated rewrite or integration was introduced.

## D. Security and data safety

Configured-value checks used the actual private GitHub token, SMTP password,
login and recipient without publishing their values. Pattern checks cover GitHub
PATs, AWS access keys and private-key headers. All intended publication files
and reachable history blobs were checked; no configured-value/pattern findings
or tracked runtime artifacts were found. The final publication scan is also
performed after adding this report and the deployment handoff.

Neither gitleaks nor trufflehog is installed. No dependency CVE scanner or external
penetration test was executed. `pip check` verifies dependency consistency, not
known vulnerability absence. These limits are explicit.

HTML labels/values and attributes are escaped. The action URL is reconstructed
only after canonical issue validation; other hosts, userinfo, ports, queries,
fragments, whitespace and issue-identity mismatches are rejected. Issue-body
links do not control the action. Rendering/URL regressions pass.

SQL values are parameterized; dynamic clauses use internal constants/placeholders.
Subprocesses use argument arrays without shell execution; SMTP secrets pass via
stdin with redacted outcomes, bounded supervision and parent-death handling.
Source text is never executed. Current production rendering takes sender and
one recipient only from validated private settings, never from a candidate.
The transport is an internal API; callers must use the supported renderer.
TLS verification remains enabled. Unknown SMTP acceptance is durable and not
automatically resent; stable Message-ID assists reconciliation but is not a
claim of SMTP-level deduplication.

CI has only `contents: read`, commit-pinned actions and disabled checkout credential
persistence. It uses pull_request rather than pull_request_target, no secrets,
no production data, mocked integrations and syntax-only systemd validation.
Runtime files remain ignored. Filesystem permissions and installer active-service
guards remain in place. Deployment backup/rollback preserves delivery history.

## E. Independently executed validation

ByteAsk reported **277 passed in 44.93s**. Codex independently ran the untouched
branch: **277 passed in 45.15s**. New regressions first reproduced five failures;
54 focused tests passed after the correction. Additional issue/PR payload tests
bring the final independent suite to **287 passed in 45.06s**, no failures or skips.

| Executed check | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q --junitxml=/tmp/codex-release-final-tests.xml` | 287 passed, exit 0 |
| `.venv/bin/ruff check .` | All checks passed, exit 0 |
| `.venv/bin/python -m pip check` | No broken requirements, exit 0 |
| `.venv/bin/python -m bountyradar --help` | Exit 0 |
| `git diff --check` and staged equivalent | No diagnostics |
| `bash -n scripts/install.sh scripts/uninstall.sh` | Exit 0 |
| `systemd-analyze --user verify` repository units | Exit 0 |
| CI-style relocated unit verification, no installation | Exit 0 |

The suite includes HTML/URL injection, positive USD boundaries ($0.01/$1/$5/$10/
$50/$100/$1,000), removal/decrease/increase/conflicts, claim/PR evidence,
5/8/12/24-hour recovery, checkpoint interruption and deduplication, same-scan
fake SMTP, uncertain acceptance, real test-owned subprocess interruption and
50,000 synthetic historical candidates with fresh priority and older fairness.
These are offline fixture/process tests, not laptop reboot or production tests.

Read-only Gmail search/read independently found ByteAsk's existing HTML test
message by its exact Message-ID. It remains in INBOX, is multipart HTML/plain,
contains the synthetic marker and payment-test disclaimer, matching canonical
GitHub button/plain URLs, and authenticated TLS 1.3 Received evidence. No new
message was sent, marked read, labeled or otherwise modified. This verifies that
existing synthetic receipt; real scheduled bounty delivery remains unverified.

## F. GitHub Actions

Added one minimal workflow; there was no prior workflow to duplicate.
Actual [CI run 39103552583](https://github.com/jmass-ggg/Bounty-Finder-/actions/runs/39103552583)
on `df8e6d7745c67d52352ef3fe98ce9ec9bdb05beb` completed **SUCCESS**.
The workflow installs `.[dev]` in Python 3.12, runs pytest/Ruff/pip check/CLI help/
whitespace/shell checks, and verifies relocated unit syntax without installing it.
No Gmail credentials or real SMTP test is used. Actions pins were verified
against the official action repositories' v4/v5 tag refs before publication.

A report/handoff-only follow-up triggers CI again. Its final check result must
also be observed before merge; prior commit success is not a substitute for a
required head check. Repository protections could not be independently audited
through an authorized admin API. No rules or required checks were bypassed.

## G. Product acceptance and production behavior

| Acceptance area | Assessment |
| --- | --- |
| Login and five-minute scans | Repository units support it; actual final deployment/login observation pending |
| Fresh monetary GitHub discovery | Supported with independent recent lane; incomplete searches remain explicit |
| Actual outage recovery, without three-hour cutoff | 5/8/12/24-hour fixture tests pass; real quotas/indexing limit completeness |
| Positive allowed USD rewards, minimum zero | Parser/eligibility and regression tests pass; zero/negative excluded |
| Assignees, claims, busy labels, relevant PRs and UNKNOWN | Conservative filtering and immediate pre-send inspection tested |
| Same-scan Gmail attempt | Fake SMTP end-to-end and reserved-budget tests pass; conditional on complete evidence/budgets |
| Clickable GitHub HTML button and plain fallback | Injection/URL tests pass; existing synthetic Gmail receipt independently verified |
| Durable deduplication, caps, failure recovery | SQLite/process regressions pass; unknown acceptance is not automatically retried |
| Fresh work during historical backlog | Selection, real request-capacity fairness and 50k fixtures pass; total backlog drainage not guaranteed |
| Real unattended bounty inbox receipt | NOT VERIFIED; production activation remains pending |

## H. Operational limitations

No authoritative funding provider exists. A properly labeled unverified alert
feature can be released without one; money text never becomes proof of funds.
Apparently unclaimed means no claim detected in checked sources, not guaranteed
availability. GitHub API quotas, result limits, index delay, truncated inspection
and search churn prevent an exhaustive-feed claim. Historical queue and disk
growth require measurement and a non-destructive retention design.

Laptop sleep/power-off/network loss prevent execution; the user manager must be
active. Catch-up is bounded work after resumption. Gmail/device push settings
are outside the application. Final installed timeouts, private notification mode
and naturally scheduled real bounty receipt still need deployment verification.

## I. Merge result

**BLOCKED.** PR creation was attempted through the connected GitHub API and
rejected with HTTP 403. `gh auth status` reports no authenticated host; no
browser session was available. No PR URL, PR MERGED state or merge SHA can
truthfully be reported. Remote main remains at the SHA in section A.
The reviewed fixes and CI are published on the feature branch.

After authorized local `gh auth login`, create the PR with base main/head
feat/production-gmail-alerts, use the prepared accurate description, verify its
current diff/checks/protections, and merge normally. Verify the PR's MERGED state
and resulting main SHA. Update this report and the handoff with those exact
values before production deployment. Do not bypass protections or force-push.

## J. ByteAsk deployment handoff

[BYTEASK_PRODUCTION_DEPLOYMENT.md](BYTEASK_PRODUCTION_DEPLOYMENT.md) contains the
staged, executable deployment procedure: verify merged SHA/clean checkout,
inspect pending/sending/unknown outcomes, stop safely, take a consistent SQLite
backup and save private settings/units, update the pinned release, set explicitly
labeled unverified mode and zero floor, retain the existing recipient, cap rollout
at 2 attempts/scan and 10/day, refresh units, observe scheduled metrics/inbox and
preserve rollback without reverting accepted-mail history.

**Current production decision: NO-GO for claiming the final release activated.**
Code is suitable for a limited unverified rollout after normal merge gates;
ByteAsk must report actual GO / CONDITIONAL GO / NO-GO from deployment evidence.
