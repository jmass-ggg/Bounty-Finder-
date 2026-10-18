# Production funding integration review

Reviewed 2026-10-17. No provider was enabled. This is a bounded feasibility
investigation, not a claim that no suitable provider exists anywhere.

An adapter must have a supported, permitted read interface, a verified response
schema, authentication and rate-limit requirements, an exact GitHub issue link,
and authoritative evidence of the currently active funded amount/currency.
A reward listing or a promise to pay after acceptance does not establish funds.
No undocumented browser endpoint was used as a production integration.

| Candidate | Primary evidence | Findings and missing prerequisites |
| --- | --- | --- |
| Algora | [Official API reference](https://api.docs.algora.io/), [bounties reference](https://api.docs.algora.io/bounties), [terms section 8](https://algora.io/terms), [Swift workflow](https://algora.io/swift) | The indexed reference advertises bounty operations, but direct reference retrieval failed/timed out. Actual endpoints, schemas, authentication, rate policy and funded-balance semantics were not verified. Terms prohibit automatic monitoring; an explicit API authorization/permission needs establishing. A listed USD reward and post-acceptance payment workflow do not prove prefunding. No scraping adapter added. |
| Opire | [Costs and payments](https://docs.opire.dev/rewards/pricing), [lifecycle](https://docs.opire.dev/rewards/lifecycle), [commands](https://docs.opire.dev/overview/commands) | USD bounties link to GitHub issues, but creators arrange payments directly after reviewing solutions. A listing/claim is not automatic payment or escrow. No supported public funding-read endpoint/schema, authentication contract or rate policy was established. Bot commands are not funding verification. |
| Gitpay | [Official docs](https://docs.gitpay.me/), [payments](https://docs.gitpay.me/docs/en/payments/), [official project](https://github.com/worknenjoy/gitpay) | Documents funded GitHub tasks and separate acceptance, release, transfer and payout stages through Whop. No supported external funding-read API contract, response schema, authentication/rate policy or monitoring authorization was established. Open source code alone is not a hosted-service API authorization. |
| Bounties Network | [Official GET /bounty reference](https://bounties.readme.io/reference/bounty) | Documents `https://api.bounties.network/bounty`, pagination and stage filters. A read-only `limit=1` check failed DNS from this environment (curl exit 6, HTTP 000). That does not prove global unavailability. Current response schema, authorization/rate policy, exact GitHub association and currency normalization remain unverified; no guessed crypto-to-USD conversion added. |

## Consequences

- Normal CLI execution still has no concrete `FundingProvider`; default funding
  trust lists remain empty. It can discover/review GitHub candidates but cannot
  generate new verified monetary alerts as shipped.
- Previously discovered claims are revisited by the bounded rotating queue.
  Never-seen old issues funded only externally can still be missed because no
  provider discovery feed is connected.
- Mocked pipeline tests deliberately use `funding.example`; this is a fictional
  fixture, not a supported API, provider implementation, or live funding proof.
- Provider 429/503 tests prove safe degradation/no false alert, not a real
  adapter's retry policy or schema compatibility.

## Required next integration work

Obtain an official supported API/feed and permission from a suitable operator,
verify representative active/cancelled/expired records and funding semantics,
then implement the adapter with bounded requests, rate handling, exact issue
matching, evidence URLs and currency/status normalization. Connect it explicitly
to CLI execution and add provider discovery checkpoints if the feed supports
new funding on old issues. Do not promote issue text or unfunded listings to
`FUNDED_EVIDENCE` to bypass this blocker.
