# BountyRadar Post-Deployment Fix Report

Incident date: 2026-10-18. All times below are UTC.

## A. Root Causes

- Verification scheduling put recent arrival/update ordering ahead of positive
  provisional monetary signals. A 20-slot batch could contain no known monetary
  signals while such candidates waited. Monetary-first ordering now applies
  within the recent lane; five of 20 slots remain available for historical work.
- Search pagination restarted broad windows when GitHub changed `total_count`,
  or retained short pages without narrowing the query. Unstable windows now split
  into inclusive, adjacent second-resolution intervals, persisting children and
  findings together. Request/time limits still bound retries; indivisible
  unstable windows stay incomplete.
- Monetary parsing used proximity to generic financial words as offer evidence.
  Worse, slicing a context window could turn `bounty_number` into the standalone
  word `bounty`. Verification and pre-send revalidation reused that parser.
- A public comment's reward claim could establish an actionable amount without
  maintainer association. Actionable comment offers now require GitHub maintainer
  association and a usable creation timestamp; known edits cannot establish them.
  Older-issue freshness continues to require an explicitly unedited, recent
  maintainer announcement.
- Offers linked to another GitHub issue could be attributed to the inspected
  roundup issue. Production observation exposed this separate identity defect.
  Reward evidence now records its associated issue URL; linked offers for another
  task cannot establish a reward for the current issue or comment.

## B. False-Positive Email

Affected issue: https://github.com/auscaster/frantic-board/issues/521

The issue described a vendor's historical x402 settlement, not compensation for
fixing that GitHub issue. The extracted amount was **USD 11.00**. The old context
slice ended with `$11.00) and had been assigned bounty`, cutting off `_number`.
That manufactured reward context and admitted the transaction.

Production recorded `APPARENTLY_UNCLAIMED`, `POTENTIAL_UNVERIFIED`, with issue
state and generic reward-claim evidence, but no authoritative funding evidence.
SMTP submission was recorded sent at **07:34:24.458930**. A read-only Gmail
INBOX search confirmed one matching Message-ID:

`<2760e70e9d5a2b2a966c2cb1aacbd7a01452a0fd51bb6bd8a3d15da56ffc89b8@bountyradar.local>`

The message cannot be recalled. Its outbox and attempt records are preserved.
At the initial restart audit there were no later sends, pending rows, or unknown
attempts.

The permanent public fixture reproduces the exact saved 4,000-character body
(including its original truncation). It contains no credentials. The new parser
classifies the receipt as `UNRELATED_TRANSACTION`, extracts no actionable amount,
and rejects discovery confidence, fresh verification, notification eligibility,
and independent pre-send validation of an old cached positive record. The delivery
regression creates zero SMTP attempts. No repository exclusion was introduced.

### Additional Incident During Observation

After the first tested patch (`ff7b518`) was deployed and the timer restored, the
08:16:41 catch-up scan sent another incorrect alert at **08:19:56.711098**:
https://github.com/dev-kp-eloper/BountyScout/issues/1611

This roundup linked an **USD 80** reward for
https://github.com/SUSU-LABS/susu-indexer/issues/37.
The reward was attributed to the roundup itself without inspecting the actual
task. This did not reproduce the historical-payment parser defect; it exposed
missing task identity binding. It is still an incorrect alert.

Message-ID:
`<70ddfce959698e4df62666ebcde39d6b69ea05fca41958c88a927f51d653c929@bountyradar.local>`

SMTP recorded sent, and a read-only Gmail INBOX search confirmed its receipt in
the configured recipient's mailbox. The timer and service were stopped at
**08:20:21**. No synthetic email was sent. Neither alert's history was removed.

The sanitized roundup excerpt is now a second permanent fixture. Its new
regression first failed by extracting USD 80, then passed after issue binding was
added. Both fixtures now reject discovery, verification, eligibility and cached
pre-send validation, with zero SMTP attempts in their offline delivery tests.
The final patch was deployed with monitoring paused. No further live delivery
was attempted.

## C. Fixed Code

- `rewards.py`: deterministic `REWARD_OFFER`, `UNRELATED_TRANSACTION`,
  `AMBIGUOUS_MONETARY_REFERENCE`, and `NO_REWARD` assessments. Evidence retains
  amount, currency, exact source span/offsets, source URL, author, reliable comment
  UTC timestamp, associated issue URL, and explanation. Explicit compensation attaches
  to its own amount and task;
  prices, receipts, historical payments, quotes, code examples, and conflicting
  amounts cannot establish an actionable reward.
- `discovery.py`: separate title/body reward assessment and diagnostic evidence;
  preserved durable unstable-window splitting.
- `github_inspector.py`, `verification.py`, `verification_models.py`: retain typed
  reward evidence and distinguish actionable maintainer offers from public claims.
- `notification.py`, `delivery.py`: require matching structured offer evidence
  when available and independently parse current issue/comment evidence before SMTP.
- `run_state.py`: retained monetary priority and historical fairness.
- `tests/test_monitoring_diagnosis.py`, `tests/fixtures/frantic-board-521.md`,
  `tests/fixtures/bountyscout-1611.md`:
  incident, parser, comment, queue, and pagination regressions.
- `tests/test_offline_recovery.py`: the reward-edit/deletion fixture now identifies
  its offer writer as a repository owner. All lifecycle assertions remain intact;
  separate regressions reject untrusted commenters.

No scheduler, notification caps, recipient, SMTP credentials, or funding-provider
policy was changed. Advertised offers remain **UNVERIFIED** without a provider.

## D. Tests

- Before-fix reproduction against `651ef61`: expected failure, extracting
  `Decimal('11.00')` from the exact incident fixture.
- Final focused monitoring regressions: **97 passed in 1.74 seconds**.
- Final complete offline suite: **384 passed in 48.21 seconds**.
- The first deployment passed 170 focused tests; the final deployed-code focused
  suite passed **182 tests in 28.52 seconds**.
- **24** explicit offer cases recognized through parsing, verification, and
  notification eligibility, including every requested example and tiny rewards.
- **33** unrelated/historical financial cases rejected, plus mixed-price,
  conflicting-amount, maintainer identity/edit, expiry, stale-cache and SMTP tests.
- Editable development install, Ruff, `pip check`, CLI help, `git diff --check`,
  shell syntax checks and systemd unit verification passed.
- Existing claimed/UNKNOWN, PR competition, stale funding, duplicate prevention,
  daily/per-scan cap, HTML email, interruption and recovery tests passed.
- Both real incident fixtures fail against their respective earlier parsers and
  pass with the final fix. Same-task linked offers and credible comment offers
  remain supported; foreign-task offers cannot become current-task rewards.
- Three additional failing reproductions verified that bounded context could
  manufacture `bounty`, `reward`, or `prize` from a longer word across long
  whitespace. The final parser preserves original token boundaries. A separate
  regression ensures target URLs are inspected in full, beyond excerpt limits.
- Maintainer comment regressions include non-UTC source offsets and verify that
  persisted structured reward timestamps are normalized to UTC.

## E. Production Deployment

Reviewed application revision:
`e590e7c8c564428af80ed5296b74362b6bb56701`.

The production checkout remains on main commit
`651ef61c56a88178b3e1802ce1b1e523226cb485` with the reviewed source patch applied.
Its eight changed application modules match the feature commit byte for byte.
The production interpreter resolves `rewards.py` from the production checkout.
A private SHA-256 deployment manifest is retained under
`data/post-deployment-diagnosis/reward-fix-deployed.sha256`.

The enabled five-minute timer was temporarily restored at **08:16:41** after
the first deployed tests passed. Its catch-up ran from the production virtualenv.
Observation found the additional incorrect alert and both timer and service were
stopped at **08:20:21**. The timer remains **enabled but inactive**; the oneshot
service is **inactive**, with deliberate SIGTERM exit status 143.

The final issue-binding patch is applied to the stopped checkout. No competing
scanner is running. Three completed scheduled scans were not observed: one
catch-up completed, and the 08:20 scheduled scan was interrupted for safety.
Unattended email is deliberately not reactivated pending review and controlled
observation of the final patch.

## F. Search Coverage

Before restoration: recent completed checkpoint **06:40:00**; recent unfinished
coverage ends **06:45:00** with three windows. Historical coverage ends
**2026-10-17 17:47:15**, with 33 unfinished windows. No checkpoint was reset or
advanced over unfinished coverage.

After observation: recent unfinished windows decreased from three to **one**,
at page four of an updated `paid` query. The completed recent checkpoint remains
**06:40:00**; it did not advance over unfinished work. Historical windows changed
from 33 to **37** as unstable windows split; the first remaining historical
interval narrowed to **10:20:27..10:20:58 on 2026-10-16**.

The completed scan used 54 search requests. Logs reported one indivisible
unstable window and bounded search-time exhaustion. Historical coverage remains
incomplete; the patch improves durable progress but does not claim complete
coverage or fully recovered real-time freshness.

## G. Verification Throughput

The deterministic 50,515-candidate regression includes 500 recent nonmonetary
issues, a USD 1 offer, fourteen USD 5/50 offers, and 50,000 historical candidates.
It selects all 15 monetary signals and five historical candidates in about
**0.08 seconds**. Provisional amounts are still revalidated by the full inspector.

The completed catch-up inspected **21** candidates, including a tracked recheck;
**16** carried cached positive monetary signals. These are signals, not 16
verified rewards. Current results classified six as CLAIMED, thirteen as UNKNOWN,
and two as APPARENTLY_UNCLAIMED. No CLAIMED or UNKNOWN result was eligible.
The original 16 apparently unclaimed/no-valid-reward findings remain excluded.

Of the seven saved delayed signals, three were rechecked in the catch-up:
`5906522903` and `5905379747` became UNKNOWN with no accepted amount;
`5315616601` was CLAIMED with no accepted amount. `5906830812` had already been
checked before this restoration and remained ineligible. The remaining three
(`5906324072`, `5791509924`, `5883384185`) are still unverified; observation
stopped before they were processed. None is asserted to be a genuine offer.

The run found 1,815 search candidates, including 195 new stored candidates,
used 36 inspection requests, and finished in **195.45 seconds**, below five
minutes. It queued/sent one alert, subsequently identified as the roundup error.

## H. Gmail

Before restoration: one sent outbox row and one sent SMTP attempt, both belonging
to the incident. No pending, sending, retryable or unknown entries existed.
No synthetic email was sent during this fix. Recipient and 2-per-scan/10-per-day
limits remain unchanged; unverified funding warnings remain prominent.

Final state: **two sent outbox rows and two sent SMTP attempts**, one for each
incident. There are **zero pending, sending, retryable or unknown entries**.
Both incorrect messages were confirmed in Gmail by read-only Message-ID search.
Neither is eligible for automatic resend. No genuine qualified-bounty Gmail
delivery has been demonstrated by this repair.

## I. Database Safety

Consistent SQLite backup:
`data/backups/pre-reward-parser-fix-20261018T081347Z.sqlite3`
(349,802,496 bytes). Both backup and production integrity checks returned `ok`.
The backup contains the original sent event and SMTP attempt.

A second consistent backup preserves both incidents:
`data/backups/pre-issue-attribution-fix-20261018T082613Z.sqlite3`
(350,965,760 bytes), integrity `ok`, two sent events and two sent attempts.

Baseline: 82,027 candidates, 965 verification results. Final stopped snapshot:
**82,223 candidates**, **972 verification results**, **350,965,760 database bytes**
(1,163,264 bytes of growth). Production integrity remains `ok`.
No database, WAL/SHM,
delivery history, backlog, or checkpoint was deleted or restored over live data.
The private `.env` retains mode 0600 and its original content hash.
Production data, logs and backups are excluded from the feature commit.

## J. GitHub

Branch: `fix/fresh-monetary-priority`.
Initial parser commit: `ff7b5186c64e3dbcee42cc784a2b253fa822d426`.
Issue-binding commit: `502255d293df9c963b4a88e910ebbd36a08a0dc0`.
Boundary-hardening commit: `229846217cd5297576a8e225cd10a99ca6164418`.
Final code commit: `e590e7c8c564428af80ed5296b74362b6bb56701`.
The branch is published to `jmass-ggg/Bounty-Finder-`; no force-push or main
merge was performed. Review must reconcile the live source patch with the
eventual reviewed merge while preserving `.env` and all runtime data.

Review handoff: https://github.com/jmass-ggg/Bounty-Finder-/compare/main...fix/fresh-monetary-priority

No pull request was created: the GitHub CLI is not authenticated in this
environment. Git push authentication worked, and the complete branch and report
are published for review through the comparison link.

## K. Final Readiness

**GO for GitHub review; NO-GO for unattended production email at handoff.**
Both reproduced defects are fixed in automated discovery-to-SMTP regressions,
but the final patch has not completed three real scheduled observations.
Production is deliberately stopped rather than risking another bad alert.
Review and observation with outbound mail disabled should precede any further
production email restart.

Conservative ambiguity can suppress unusually worded legitimate offers.
Natural-language evidence cannot prove available funds; no authoritative funding
provider is configured. Completed recent coverage is still behind real time,
and three of the saved seven monetary signals remain unverified.
