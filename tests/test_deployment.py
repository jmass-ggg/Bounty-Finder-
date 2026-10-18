"""Bounded offline execution and real subprocess shutdown, without touching systemd."""

import asyncio
import os
import selectors
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from test_discovery import NOW, engine, issue, payload
from test_email import options, result
from test_verification import candidate

from bountyradar import cli
from bountyradar.delivery import send_pending
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunState, run_lock

PROBE = Path(__file__).with_name("deployment_probe.py")


@pytest.mark.asyncio
async def test_search_deadline_keeps_committed_page_and_resumes(tmp_path, respx_mock):
    requested = []

    async def respond(request):
        requested.append(request.url)
        if len(requested) == 2:
            await asyncio.Event().wait()
        return httpx.Response(200, json=payload([issue()]))

    respx_mock.get(API_URL).mock(side_effect=respond)
    path = tmp_path / "resume.db"
    async with httpx.AsyncClient() as client:
        state = RunState(path)
        finder = engine(client, github_search_timeout_seconds=0.1)
        finder.progress = state
        scan = await finder.scan()
        assert not scan.complete
        assert "time budget" in scan.unprocessed_windows[0].reason
        assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
        assert len(state.search_progress().windows) == 1
        assert state.last_complete_at() is None
        state.close()
        state = RunState(path)
        finder = engine(client)
        finder.progress = state
        assert (await finder.scan()).complete
        assert state.search_progress().windows == []
        state.close()
    # respx records completed responses only; record cancelled requests at entry.
    assert len(requested) == 3
    assert requested[1] == requested[2]


@pytest.mark.asyncio
async def test_inspection_budget_counts_retries_and_prevents_more_http(respx_mock):
    route = respx_mock.get("https://api.github.com/repos/acme/tool/issues/8").respond(503)
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(
            client, options(github_max_inspection_requests_per_scan=2),
            sleep=asyncio_no_sleep,
        )
        first = await inspector.inspect(candidate())
        second = await inspector.inspect(candidate())
    assert route.call_count == inspector.requests == 2
    assert "request budget" in first.missing_sources[0]
    assert "request budget" in second.missing_sources[0]


async def asyncio_no_sleep(_seconds):
    pass


@pytest.mark.asyncio
async def test_inspection_deadline_cancels_hung_http(respx_mock):
    requested = []

    async def hang(_request):
        requested.append(_request.url)
        await asyncio.Event().wait()

    respx_mock.get("https://api.github.com/repos/acme/tool/issues/8").mock(
        side_effect=hang
    )
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(
            client, options(github_inspection_timeout_seconds=0.05)
        )
        assert "time budget" in (await inspector.inspect(candidate())).missing_sources[0]
        assert "time budget" in (await inspector.inspect(candidate())).missing_sources[0]
    assert len(requested) == inspector.requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_cooldown_persists_before_cancelled_wait(tmp_path, respx_mock, status):
    headers = (
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(NOW.timestamp() + 3600)}
        if status == 200 else {"retry-after": "60"}
    )
    route = respx_mock.get(API_URL).respond(status, headers=headers, json=payload())
    path = tmp_path / "cooldown.db"
    state = RunState(path)
    async with httpx.AsyncClient() as client:
        finder = engine(client, github_search_timeout_seconds=0.05)
        finder.progress = state
        finder.sleep = asyncio.sleep
        assert not (await finder.scan()).complete
        assert state.github_cooldown() > NOW.timestamp()
        state.close()
        state = RunState(path)
        resumed = GitHubDiscovery(client, finder.settings, progress=state, clock=lambda: NOW)
        assert not (await resumed.scan()).complete
        state.close()
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_inspection_cooldown_survives_deadline_and_restart(tmp_path, respx_mock, status):
    headers = (
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(NOW.timestamp() + 3600)}
        if status == 200 else {"retry-after": "60"}
    )
    route = respx_mock.get("https://api.github.com/repos/acme/tool/issues/8").respond(
        status, headers=headers, json={"state": "open", "comments": 0}
    )
    path = tmp_path / "inspection-cooldown.db"
    settings = options(github_inspection_timeout_seconds=0.05)
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            state = RunState(path)
            try:
                inspector = GitHubIssueInspector(client, settings, clock=lambda: NOW)
                inspector.progress = state
                assert (await inspector.inspect(candidate())).missing_sources
                assert state.github_cooldown() > NOW.timestamp()
            finally:
                state.close()
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_run_deadline_releases_lock_and_keeps_checkpoint(tmp_path):
    settings = options(
        database_path=tmp_path / "run.db", email_enabled=False, run_timeout_seconds=0.1
    )
    state = RunState(settings.database_path)
    state.save([], [], completed_at=NOW)
    state.close()

    class HungSource:
        name = "github"

        async def scan(self, _hours=None):
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await cli._run(settings, False, sources=[HungSource()])
    with run_lock(settings.database_path):
        state = RunState(settings.database_path)
        assert state.last_complete_at() == NOW
        assert state.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        state.close()


@pytest.mark.asyncio
async def test_cancelled_delivery_remains_recoverable_without_resend(tmp_path):
    settings = options(database_path=tmp_path / "outbox.db")
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    key = outbox.enqueue(result())
    entered = asyncio.Event()

    async def revalidate(_result):
        entered.set()
        await asyncio.Event().wait()

    class ForbiddenTransport:
        async def send(self, _message):
            raise AssertionError("SMTP must not be reached")

    task = asyncio.create_task(send_pending(outbox, ForbiddenTransport(), settings, revalidate))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    outbox.close()
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    assert [(row.event_key, row.state) for row in outbox.list_entries()] == [(key, "pending")]
    outbox.close()


@pytest.mark.asyncio
async def test_cancelled_active_smtp_recovers_as_unknown_without_resend(tmp_path):
    from test_smtp_process import process_transport, wait_ready

    settings = options(database_path=tmp_path / "smtp-interrupt.db")
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    key = outbox.enqueue(result())

    async def available(_result):
        return True

    transport = process_transport(settings, tmp_path / "smtp", "send-blocked")
    task = asyncio.create_task(send_pending(outbox, transport, settings, available))
    try:
        pid = await wait_ready(tmp_path / "smtp")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert [(row.event_key, row.state) for row in outbox.list_entries()] == [
            (key, "unknown_delivery")
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        outbox.close()
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    try:
        assert outbox.recover_expired() == 0
        assert (await send_pending(outbox, transport, settings, available)).sent == 0
    finally:
        outbox.close()


def probe_environment():
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith("BOUNTYRADAR_") and key not in {"GITHUB_TOKEN", "EMAIL_ENABLED"}
    }


def test_sigterm_real_process_preserves_search_progress(tmp_path):
    database = tmp_path / "interrupted.db"
    process = subprocess.Popen(
        [sys.executable, str(PROBE), "interrupt", str(database)],
        cwd=tmp_path, env=probe_environment(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "probe did not enter its mocked request"
        assert process.stdout.readline().strip() == "READY_FOR_SIGTERM"
        overlap = subprocess.run(
            [sys.executable, str(PROBE), "run", str(database)],
            cwd=tmp_path, env=probe_environment(), capture_output=True, text=True, timeout=10,
        )
        assert overlap.returncode == 2
        assert '"reason": "another_scan_active"' in overlap.stdout
        started = time.monotonic()
        process.terminate()  # Only this test-owned subprocess, never a systemd service.
        stdout, stderr = process.communicate(timeout=10)
        assert time.monotonic() - started < 10  # Repository TimeoutStopSec is 25s.
        assert process.returncode == 143, (stdout, stderr)
        assert '"event": "run_interrupted"' in stdout
        assert stderr == ""
        with run_lock(database):
            state = RunState(database)
            assert state.last_complete_at() is None
            assert len(state.search_progress().windows) == 1
            assert state.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            state.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("mode", ["dry-run", "run"])
def test_cli_clean_completion_and_offline_dry_run(tmp_path, mode):
    database = tmp_path / "probe.db"
    completed = subprocess.run(
        [sys.executable, str(PROBE), mode, str(database)], cwd=tmp_path,
        env=probe_environment(), capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert '"event": "run_complete"' in completed.stdout
    assert '"sent": 0' in completed.stdout
    assert completed.stderr == ""
    assert database.exists() == (mode == "run")
    if mode == "run":
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_default_budgets_fit_interval_and_retain_dense_backlog(
    tmp_path, monkeypatch, respx_mock
):
    elapsed = [0.0]
    searches, inspections = [], []

    async def advance(seconds):
        elapsed[0] += seconds

    def search(request):
        searches.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=payload(
            [issue(i) for i in range((page - 1) * 100 + 1, page * 100 + 1)], total=1000
        ))

    def inspect(request):
        inspections.append(request)
        data = [] if request.url.path.endswith("/timeline") else {
            "title": "Bounty: repair parser", "body": "Reward $100", "state": "open",
            "comments": 0, "labels": [], "assignees": [],
        }
        return httpx.Response(200, json=data)

    respx_mock.get(API_URL).mock(side_effect=search)
    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=inspect
    )
    monkeypatch.setattr(cli, "GitHubDiscovery", lambda client, settings, **kw: GitHubDiscovery(
        client, settings, **kw, sleep=advance, monotonic=lambda: elapsed[0]
    ))
    monkeypatch.setattr(
        cli, "GitHubIssueInspector",
        lambda client, settings, **kw: GitHubIssueInspector(
            client, settings, **kw, sleep=advance, monotonic=lambda: elapsed[0]
        ),
    )
    settings = options(
        database_path=tmp_path / "dense.db", email_enabled=False,
        github_token="offline-token", github_min_request_interval_seconds=2,
    )
    summary = await cli._run(
        settings, False, clock=lambda: NOW + timedelta(seconds=elapsed[0])
    )
    assert summary[4] > 0
    assert len(searches) <= settings.github_max_search_requests_per_scan
    assert len(inspections) <= settings.github_max_inspection_requests_per_scan
    assert elapsed[0] < settings.run_timeout_seconds < settings.scan_interval_seconds
    state = RunState(settings.database_path)
    assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1000
    assert state.db.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 20
    assert state.last_complete_at() is None
    assert state.search_progress().windows
    continuation = state.search_progress().windows[0]
    state.close()
    print(
        f"SIMULATED first scan: paced_seconds={elapsed[0]:.1f}, "
        f"searches={len(searches)}, inspections={len(inspections)}, stored=1000, verified=20"
    )
    first_inspected_ids = {int(request.url.path.split("/")[5]) for request in inspections}
    searches.clear()
    inspections.clear()
    elapsed[0] = 0
    await cli._run(
        settings, False, clock=lambda: NOW + timedelta(minutes=5, seconds=elapsed[0])
    )
    # Bounded fresh work precedes, rather than replaces, the historical continuation.
    assert searches[0].url.params["q"] != continuation.query
    historical_first = searches[settings.github_fresh_search_requests]
    assert historical_first.url.params["q"] == continuation.query
    assert int(historical_first.url.params["page"]) == continuation.page
    assert len(searches) <= settings.github_max_search_requests_per_scan
    assert len(inspections) <= settings.github_max_inspection_requests_per_scan
    assert elapsed[0] < settings.run_timeout_seconds
    inspected_ids = {
        int(request.url.path.split("/")[5]) for request in inspections
    }
    assert len(inspected_ids) == 20
    assert inspected_ids.isdisjoint(first_inspected_ids)
    state = RunState(settings.database_path)
    try:
        assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1000
        assert state.db.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 40
        assert state.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        state.close()


def test_cli_funded_alert_increase_sigterm_and_recovery(tmp_path):
    database = tmp_path / "lifecycle.db"

    def invoke(mode):
        completed = subprocess.run(
            [sys.executable, str(PROBE), mode, str(database)],
            cwd=tmp_path, env=probe_environment(), capture_output=True, text=True, timeout=10,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        assert completed.stderr == ""
        return completed.stdout

    assert invoke("funded").count("MOCK_SMTP_ACCEPTED") == 1
    assert "MOCK_SMTP_ACCEPTED" not in invoke("funded")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
        assert db.execute("SELECT state FROM notification_outbox").fetchall() == [("sent",)]
        assert db.execute(
            "SELECT json_extract(payload_json, '$.category') FROM verification_results"
        ).fetchone()[0] == "FUNDED_EVIDENCE"

    process = subprocess.Popen(
        [sys.executable, str(PROBE), "funded-increase-interrupt", str(database)],
        cwd=tmp_path, env=probe_environment(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "probe did not reach delivery revalidation"
        assert process.stdout.readline().strip() == "READY_FOR_SIGTERM"
        # The reward transition must already be durable before interrupting HTTP.
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT reason,state,attempts FROM notification_outbox ORDER BY event_key"
            ).fetchall() == [
                ("new_qualified_bounty", "sent", 1),
                ("reward_increased", "pending", 0),
            ]
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 143, (stdout, stderr)
        assert '"event": "run_interrupted"' in stdout
        assert "MOCK_SMTP_ACCEPTED" not in stdout
        assert stderr == ""
        with run_lock(database), sqlite3.connect(database) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert invoke("funded-recover").count("MOCK_SMTP_ACCEPTED") == 1
        assert "MOCK_SMTP_ACCEPTED" not in invoke("funded-recover")
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT state FROM notification_outbox ORDER BY event_key"
            ).fetchall() == [("sent",), ("sent",)]
            assert db.execute(
                "SELECT outcome FROM notification_delivery_attempts"
            ).fetchall() == [("sent",), ("sent",)]
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.asyncio
async def test_backlog_completion_catches_new_issue_in_next_window(
    tmp_path, monkeypatch, respx_mock
):
    clock = [NOW]
    requests = []
    new_at = NOW + timedelta(minutes=5)

    def search(request):
        query = request.url.params["q"]
        requests.append(query)
        items = [issue()]
        if clock[0] >= new_at and new_at.strftime("%Y-%m-%dT%H:%M:%SZ") <= (
            query.split("..")[1].split()[0]
        ):
            items.append(issue(2, created_at=new_at.isoformat(), updated_at=new_at.isoformat()))
        return httpx.Response(200, json=payload(items))

    respx_mock.get(API_URL).mock(side_effect=search)
    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=lambda request: httpx.Response(200, json=(
            [] if request.url.path.endswith("/timeline") else
            {"state": "open", "body": "Reward $100", "comments": 0, "assignees": []}
        ))
    )
    monkeypatch.setattr(
        cli, "GitHubDiscovery",
        lambda client, settings, **kw: GitHubDiscovery(
            client, settings, **kw, sleep=asyncio_no_sleep
        ),
    )
    settings = options(
        database_path=tmp_path / "catchup.db", email_enabled=False,
        search_terms=["bounty"], search_labels=[], monetary_terms=[],
        github_max_search_requests_per_scan=1,
    )
    for index in range(3):
        clock[0] = NOW + timedelta(minutes=index * 5)
        summary = await cli._run(settings, False, clock=lambda: clock[0])
        state = RunState(settings.database_path)
        try:
            assert state.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            if index == 0:
                assert summary[4] == 1
                assert state.last_complete_at() is None
                assert state.search_progress().windows[0].category == "updated"
            elif index == 1:
                assert summary[4] == 0
                assert state.last_complete_at() == NOW
                assert state.search_progress() is None
            else:
                assert state.db.execute(
                    "SELECT count(*) FROM discovered_issues"
                ).fetchone()[0] == 2
                assert state.last_complete_at() == NOW
                assert state.search_progress().covered_until == clock[0]
        finally:
            state.close()
    assert len(requests) == 3
    assert "created:" in requests[0] and "updated:" in requests[1]
    assert requests[2] != requests[0]
