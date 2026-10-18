# Phase 7 source feasibility (reviewed 2026-10-17)

Only a documented, permitted API or feed can be enabled. A listing or bot
comment is a reward claim, not proof that money is held or will be paid.

| Source | Official interface / authentication | Rate limits and automated access | Reward, status, funding capabilities | Decision |
| --- | --- | --- | --- | --- |
| GitHub | [REST search](https://docs.github.com/en/rest/search/search) with `GITHUB_TOKEN` | Existing adapter handles GitHub search limits and retry headers | Issue text/labels and availability; no authoritative funding | Enabled |
| Algora | [API reference landing page](https://api.docs.algora.io/) advertises bounty operations, but a stable public discovery contract and authentication/rate policy were not established | [Terms](https://algora.io/terms) prohibit robots or other automated monitoring without consent | Public pages show USD bounties and open/completed status; a listing alone does not establish funded escrow | Disabled; written permission and a documented interface are required |
| Opire | [Public documentation](https://docs.opire.dev/) describes web and bot workflows, but no documented public discovery API/feed or authentication/rate policy was found | [Terms](https://opire.dev/terms-of-service) do not grant a documented automated feed; limits unknown | [Commands](https://docs.opire.dev/overview/commands) use USD; [lifecycle](https://docs.opire.dev/rewards/lifecycle) covers attempts, claims, multiple awards; [payments](https://docs.opire.dev/rewards/pricing) say creators arrange payment directly. The older terms describe Stripe payments, so funding behavior needs clarification | Disabled; no authoritative funding evidence available through documented public interface |
| IssueHunt OSS | [Public OSS issue pages](https://oss.issuehunt.io/) exist, but no official discovery API/feed or authentication contract was found | [Terms](https://oss.issuehunt.io/terms) apply; automated-access limits were not established | Pages display USD amounts and issue status, but funding and payout verification are not documented as an API | Disabled |
| Bountysource | [Legacy API docs](https://bountysource.github.io/) exist | Current access policy and reliable service status were not established | Historical issue bounties; current funding/claim status cannot be trusted from the old docs | Disabled |
| Bounties Network | [Public on-chain bounty list API](https://bounties.readme.io/reference/bounty) documents search, paging, stage and token amount; list endpoint is shown without authentication | No documented rate limit was found | Crypto-token amounts and active/completed/expired stages; a canonical GitHub issue link is optional, and USD conversion is unsupported | Disabled for this GitHub-issue/USD MVP |

The `BountySource` protocol accepts a lookback and returns `ScanResult` with
normalized `BountyCandidate` objects. Adapters must resolve each linked GitHub
issue to its real GitHub issue ID, repository, number and HTTPS issue URL before
returning it. This lets the existing inspector, persistence, ranking and outbox
operate on the same identity. `scan_sources` deduplicates by canonical
repository/issue number and merges original evidence URLs. It does **not** add
provisional amounts from multiple sources. A future platform adapter must return
separate `FundingRecord` entries with stable `funding_id` values for distinct
awards and use an independently justified trust configuration before producing
`FUNDED_EVIDENCE`.

Each successful source has its own `last_complete_at:<source>` checkpoint.
The old GitHub checkpoint remains readable for existing databases. A failing
source does not advance its checkpoint or stop other sources; the next run uses
the missed interval. The source failure log gives the exception type without
logging tokens or request URLs. Only GitHub is registered by default, so this
phase does not add live discovery from Algora, Opire, or other platforms.
