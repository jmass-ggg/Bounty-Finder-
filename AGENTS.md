# BountyRadar contributor instructions

## Project and scope
BountyRadar is a local-first Python 3.12+ background bounty monitor. The MVP
source is public GitHub repositories, with all programming languages supported
and a configurable reward threshold defaulting to all positive amounts. SQLite stores findings;
Gmail receives qualifying opportunities; a systemd user timer will run every
five minutes. There is no frontend or web server.

## Phase workflow
Phase 0 provided packaging, settings, CLI metadata, documentation, and tests.
Phase 1 added GitHub REST discovery and offline API tests. Phase 3 added
verification, availability inspection, ranking, and notification eligibility.
Phase 4 added a narrow SQLite notification outbox and opt-in Gmail delivery.
Phase 5 added a findings/checkpoint store and systemd user timer deployment.
Phase 6 added deterministic offline end-to-end validation and reliability fixes.
Phase 7 added the source protocol, independent checkpoints and canonical issue
deduplication. No third-party platform was enabled because permitted, reliable
discovery and authoritative funding interfaces were not established; see
`docs/phase7-sources.md`.
There is still no concrete authoritative funding provider in this workspace.
Implement subsequent phases only when requested. Inspect existing code and Git
status first, keep changes scoped, and report verification and limitations.
Never commit or push without explicit user approval.

## Coding rules and boundaries
Use typed Pydantic v2 data models and pydantic-settings configuration. Keep CLI,
configuration, discovery, reward assessment, availability assessment, storage,
and notifications separate. Inject external clients, clocks, repositories, and
mail transports rather than constructing them in domain logic. Discovery should
be asynchronous with httpx; SMTP delivery must be synchronous or isolated from
the event loop. Use standard-library email and smtplib. No runtime AI dependency,
Docker, Celery, Redis, PostgreSQL, paid SaaS, or web server.

Use documented or permitted external interfaces. A money mention is evidence,
not reward verification; preserve original evidence URLs and distinguish confidence.
Store timezone-aware UTC timestamps. Logs must be structured and redact
credentials. Scans and notifications should be idempotent and restart-safe, including bounded
network retries, durable progress, and notification deduplication across failures.
Never hardcode secrets or commit .env, credentials, databases, or runtime logs.

## Testing and verification
Use pytest, pytest-asyncio, and respx. Tests must be deterministic and offline:
mock HTTP and inject fake email transports; never send real email. Test validation
boundaries, failure paths, idempotency, and restart behavior when implemented.
Run the smallest relevant tests followed by the project checks:
`pip install -e ".[dev]"`, `python -m bountyradar --help`, `pytest -q`,
`ruff check .`, and `git diff --check`. Report exact failures; do not claim an
unexecuted check passed. Keep Ruff clean and avoid unrelated refactoring.
