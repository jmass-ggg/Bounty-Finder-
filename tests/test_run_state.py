"""Offline checkpoint, findings and overlap behavior for scheduled runs."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from bountyradar.models import BountyCandidate, ScanResult, UnprocessedWindow
from bountyradar.run_state import RunAlreadyActive, RunState, run_lock
from bountyradar.verification_models import Availability, Category, VerificationResult

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def finding():
    candidate = BountyCandidate(
        source_issue_id=7,
        repository="acme/tool",
        issue_number=7,
        title="Bounty issue",
        url="https://github.com/acme/tool/issues/7",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        discovered_at=NOW,
    )
    result = VerificationResult(
        candidate=candidate,
        category=Category.REWARD_CLAIMED,
        availability=Availability.AVAILABLE,
        checked_at=NOW,
        score=40,
    )
    return candidate, result


def test_findings_survive_restart_and_incomplete_scan_preserves_checkpoint(tmp_path):
    path = tmp_path / "data" / "bounties.db"
    candidate, result = finding()
    first = RunState(path)
    first.save([candidate], [result], completed_at=NOW)
    first.close()

    second = RunState(path)
    try:
        assert second.last_complete_at() == NOW
        second.save([candidate], [result], completed_at=None)
        assert second.last_complete_at() == NOW
        connection = second.db
        assert connection.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 1
        second.save([], [], completed_at=NOW + timedelta(hours=3))
        assert second.last_complete_at() == NOW + timedelta(hours=3)
    finally:
        second.close()


def test_run_lock_rejects_overlap_and_releases_after_failure(tmp_path):
    database = tmp_path / "data" / "bounties.db"
    with pytest.raises(RuntimeError, match="scan crashed"):
        with run_lock(database):
            with pytest.raises(RunAlreadyActive):
                with run_lock(database):
                    pass
            raise RuntimeError("scan crashed")
    with run_lock(database):
        pass
    assert not database.exists()


def test_scan_state_and_outbox_can_share_sqlite_database(tmp_path):
    from bountyradar.outbox import NotificationOutbox
    from bountyradar.settings import Settings

    database = tmp_path / "data" / "bounties.db"
    store = RunState(database)
    candidate, result = finding()
    store.save([candidate], [result], completed_at=NOW)
    store.close()
    outbox = NotificationOutbox(database, Settings(_env_file=None, email_enabled=False))
    outbox.close()
    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"discovered_issues", "verification_results", "notification_outbox"} <= names


def test_scheduled_run_requires_github_token_without_network(monkeypatch, tmp_path, capsys):
    from bountyradar import cli
    from bountyradar.settings import Settings

    config = Settings(_env_file=None, github_token=None, database_path=tmp_path / "db.sqlite3")
    monkeypatch.setattr(cli, "Settings", lambda: config)
    assert cli.main(["run"]) == 2
    assert "MissingGitHubToken" in capsys.readouterr().out
    assert not config.database_path.exists()


def test_locked_database_does_not_advance_checkpoint(tmp_path):
    path = tmp_path / "data" / "bounties.db"
    holder = RunState(path)
    contender = RunState(path)
    contender.db.execute("PRAGMA busy_timeout=0")
    candidate, result = finding()
    try:
        holder.db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.save([candidate], [result], completed_at=NOW)
        holder.db.rollback()
        assert contender.last_complete_at() is None
        assert contender.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 0
    finally:
        holder.close()
        contender.close()


def test_interrupted_transaction_rolls_back_findings_and_checkpoint(tmp_path):
    path = tmp_path / "data" / "bounties.db"
    store = RunState(path)
    candidate, result = finding()
    try:
        store.db.execute("""
            CREATE TRIGGER fail_verification BEFORE INSERT ON verification_results
            BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="simulated interruption"):
            store.save([candidate], [result], completed_at=NOW)
        assert store.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 0
        assert store.last_complete_at() is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_retries_missed_interval_until_scan_is_complete(tmp_path, monkeypatch):
    from bountyradar import cli
    from bountyradar.settings import Settings

    path = tmp_path / "data" / "bounties.db"
    state = RunState(path)
    current = NOW + timedelta(days=4)
    old_checkpoint = current - timedelta(days=4)
    state.save([], [], completed_at=old_checkpoint)
    state.close()
    requested_hours = []
    complete = [False]

    class FakeDiscovery:
        def __init__(self, _client, _settings, *, clock):
            pass

        async def scan(self, hours=None):
            requested_hours.append(hours)
            missing = []
            if not complete[0]:
                missing = [
                    UnprocessedWindow(
                        category="updated",
                        query="mock query",
                        start=NOW,
                        end=NOW,
                        reason="network failure after retries",
                    )
                ]
            return ScanResult(candidates=[], unprocessed_windows=missing)

    monkeypatch.setattr(cli, "GitHubDiscovery", FakeDiscovery)
    settings = Settings(_env_file=None, database_path=path, email_enabled=False)
    assert (await cli._run(settings, False, clock=lambda: current))[4] == 1
    state = RunState(path)
    assert state.last_complete_at() == old_checkpoint
    state.close()
    complete[0] = True
    assert (await cli._run(settings, False, clock=lambda: current))[4] == 0
    state = RunState(path)
    assert state.last_complete_at() == current
    state.close()
    assert requested_hours[0] >= 96
    assert requested_hours[1] >= 96


@pytest.mark.asyncio
async def test_crash_during_enqueue_keeps_checkpoint_for_replay(tmp_path, monkeypatch):
    from bountyradar import cli
    from bountyradar.outbox import NotificationOutbox
    from bountyradar.settings import Settings

    candidate, verification = finding()
    database = tmp_path / "data" / "bounties.db"

    class FakeDiscovery:
        def __init__(self, _client, _settings, *, clock):
            pass

        async def scan(self, _hours=None):
            return ScanResult(candidates=[candidate], unprocessed_windows=[])

    class FakeVerifier:
        def __init__(self, _inspector, _settings, _provider, *, clock):
            pass

        async def verify_many(self, candidates, *, preserve_order=False):
            return [verification] if candidates else []

    monkeypatch.setattr(cli, "GitHubDiscovery", FakeDiscovery)
    monkeypatch.setattr(cli, "Verifier", FakeVerifier)
    settings = Settings(_env_file=None, database_path=database, email_enabled=False)

    def crash(_self, _result):
        raise RuntimeError("simulated scanner crash")

    with monkeypatch.context() as patch:
        patch.setattr(NotificationOutbox, "enqueue", crash)
        with pytest.raises(RuntimeError, match="simulated scanner crash"):
            await cli._run(settings, False, clock=lambda: NOW)
    state = RunState(database)
    try:
        assert state.last_complete_at() is None
        assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
        # A failed enqueue leaves the candidate due for verification after restart.
        assert state.db.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 0
    finally:
        state.close()
    await cli._run(settings, False, clock=lambda: NOW + timedelta(minutes=5))
    state = RunState(database)
    try:
        assert state.last_complete_at() == NOW + timedelta(minutes=5)
    finally:
        state.close()
