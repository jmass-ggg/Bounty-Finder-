"""Offline multi-source discovery and checkpoint behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bountyradar.cli import _run
from bountyradar.models import BountyCandidate, ScanResult
from bountyradar.run_state import RunState
from bountyradar.settings import Settings
from bountyradar.sources import scan_sources

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def candidate(issue_id: int, amount: str, evidence: str) -> BountyCandidate:
    return BountyCandidate(
        source_issue_id=issue_id,
        repository="acme/tool",
        issue_number=7,
        title="Bounty issue",
        url="https://github.com/acme/tool/issues/7",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        discovered_at=NOW,
        potential_reward_amount=Decimal(amount),
        currency="USD",
        candidate_evidence=[evidence],
    )


class FakeSource:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.hours = []

    async def scan(self, hours=None):
        self.hours.append(hours)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_sources_deduplicate_by_github_issue_without_summing_rewards():
    github = FakeSource(
        "github", ScanResult(candidates=[candidate(7, "100", "https://github.com/acme/tool/issues/7")],
                             unprocessed_windows=[])
    )
    other = FakeSource(
        "permitted_feed",
        ScanResult(candidates=[candidate(700, "150", "https://example.org/award/1")],
                   unprocessed_windows=[]),
    )
    scan = await scan_sources([github, other], {})
    assert scan.complete
    assert len(scan.candidates) == 1
    assert scan.candidates[0].potential_reward_amount == Decimal("100")
    assert scan.candidates[0].candidate_evidence == [
        "https://github.com/acme/tool/issues/7", "https://example.org/award/1"
    ]


@pytest.mark.asyncio
async def test_source_failure_does_not_discard_other_source():
    bad = FakeSource("unavailable", error=TimeoutError("secret endpoint details"))
    good = FakeSource(
        "github", ScanResult(candidates=[candidate(7, "100", "https://github.com/a")],
                             unprocessed_windows=[])
    )
    scan = await scan_sources([bad, good], {})
    assert len(scan.candidates) == 1
    assert scan.failures == {"unavailable": "TimeoutError"}
    assert "secret endpoint details" not in str(scan.failures)


@pytest.mark.asyncio
async def test_run_keeps_separate_source_checkpoints(tmp_path):
    path = tmp_path / "bounties.sqlite3"
    settings = Settings(_env_file=None, database_path=path, email_enabled=False)
    good = FakeSource("github", ScanResult(candidates=[], unprocessed_windows=[]))
    feed = FakeSource("feed", ScanResult(candidates=[], unprocessed_windows=[]))
    await _run(settings, False, clock=lambda: NOW, sources=[good, feed])
    store = RunState(path)
    try:
        assert store.last_complete_at("github") == NOW
        assert store.last_complete_at("feed") == NOW
    finally:
        store.close()
    feed.error = TimeoutError()
    await _run(settings, False, clock=lambda: NOW + timedelta(hours=1), sources=[good, feed])
    store = RunState(path)
    try:
        assert store.last_complete_at("github") == NOW + timedelta(hours=1)
        assert store.last_complete_at("feed") == NOW
    finally:
        store.close()
    feed.error = None
    await _run(settings, False, clock=lambda: NOW + timedelta(hours=2), sources=[good, feed])
    store = RunState(path)
    try:
        assert store.last_complete_at("feed") == NOW + timedelta(hours=2)
    finally:
        store.close()
    assert good.hours[2] >= 1
    assert feed.hours[0] is None
    assert feed.hours[2] >= 2


@pytest.mark.asyncio
async def test_invalid_cross_source_issue_does_not_contaminate_results():
    valid = FakeSource("github", ScanResult(candidates=[candidate(7, "100", "https://github.com/a")],
                                             unprocessed_windows=[]))
    wrong = candidate(8, "500", "https://example.org/award/2").model_copy(
        update={"url": "https://example.org/issue/7"}
    )
    invalid = FakeSource("feed", ScanResult(candidates=[wrong], unprocessed_windows=[]))
    scan = await scan_sources([valid, invalid], {})
    assert scan.failures == {"feed": "ValueError"}
    assert scan.candidates[0].potential_reward_amount == Decimal("100")
