"""Full offline lifecycle with a fake clock, GitHub API and SMTP socket."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from test_discovery import no_sleep

from bountyradar import cli
from bountyradar.cli import _run
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.notification import GmailSMTPTransport
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunState
from bountyradar.settings import Settings
from bountyradar.verification_models import FundingRecord

START = datetime(2026, 10, 17, 12, tzinfo=UTC)
ISSUE_URL = "https://github.com/acme/tool/issues/8"
API_ISSUE = "https://api.github.com/repos/acme/tool/issues/8"


@pytest.fixture(autouse=True)
def offline_search_pacing(monkeypatch):
    monkeypatch.setattr(
        cli, "GitHubDiscovery",
        lambda client, settings, **kw: GitHubDiscovery(
            client, settings, **kw, sleep=no_sleep
        ),
    )


class FakeSMTPSession:
    def __init__(self, sent, fail_login):
        self.sent = sent
        self.fail_login = fail_login

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def login(self, _username, _password):
        if self.fail_login[0]:
            self.fail_login[0] = False
            raise OSError("simulated network outage")

    def send_message(self, message):
        self.sent.append(message)
        return {}


@pytest.mark.asyncio
async def test_funded_issue_lifecycle_five_minute_rescan_increase_and_restart(tmp_path, respx_mock):
    clock = [START]
    amount = [Decimal("100")]
    updated = [START]
    issue_open = [True]
    sent = []
    fail_login = [False]
    search = respx_mock.get(API_URL).mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "total_count": 1 if issue_open[0] else 0,
                "incomplete_results": False,
                "items": [
                    {
                        "id": 8,
                        "number": 8,
                        "title": "Bounty: repair parser",
                        "body": "Reward $100",
                        "html_url": ISSUE_URL,
                        "state": "open",
                        "created_at": START.isoformat(),
                        "updated_at": updated[0].isoformat(),
                        "labels": [{"name": "bounty"}],
                        "assignees": [],
                    }
                ]
                if issue_open[0]
                else [],
            },
        )
    )
    respx_mock.get(API_ISSUE).mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "state": "open" if issue_open[0] else "closed",
                "body": "Reward $100",
                "labels": [{"name": "bounty"}],
                "assignees": [],
                "comments": 0,
            },
        )
    )
    respx_mock.get(f"{API_ISSUE}/timeline").mock(return_value=httpx.Response(200, json=[]))

    class TrustedFunding:
        async def records_for(self, candidate):
            assert candidate.url == ISSUE_URL
            return [
                FundingRecord(
                    provider="trusted_api",
                    issue_url=ISSUE_URL,
                    evidence_url="https://funding.example/records/8",
                    observed_at=clock[0],
                    status="funded",
                    amount=amount[0],
                    currency="USD",
                )
            ]

    def smtp_factory(_host, _port, *, timeout, context):
        assert timeout > 0 and context is not None
        return FakeSMTPSession(sent, fail_login)

    database = tmp_path / "data" / "bounties.db"
    settings = Settings(
        _env_file=None,
        database_path=database,
        email_enabled=True,
        smtp_username="sender@example.com",
        smtp_password="fake-app-password",
        email_to="recipient@example.com",
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
        github_min_request_interval_seconds=0,
        trusted_funding_hosts=["funding.example"],
        trusted_funding_providers=["trusted_api"],
    )
    funding = TrustedFunding()
    class MockTransport:
        async def send(self, message):
            GmailSMTPTransport(settings, smtp_factory=smtp_factory).send(message)

    transport = MockTransport()

    first = await _run(settings, False, funding, transport, clock=lambda: clock[0])
    assert first == (1, 1, 1, 1, 0, 0)
    assert search.call_count == 2  # Created and updated searches return the same issue.
    assert len(sent) == 1
    assert "USD 100" in sent[0]["Subject"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM notification_outbox").fetchone()[0] == "sent"
        assert (
            connection.execute("SELECT outcome FROM notification_delivery_attempts").fetchone()[0]
            == "sent"
        )

    clock[0] += timedelta(minutes=5)
    assert await _run(settings, False, funding, transport, clock=lambda: clock[0]) == (
        1,
        1,
        0,
        0,
        0,
        0,
    )
    assert len(sent) == 1

    clock[0] += timedelta(minutes=5)
    updated[0] = clock[0]
    amount[0] = Decimal("150")
    fail_login[0] = True
    assert await _run(settings, False, funding, transport, clock=lambda: clock[0]) == (
        1,
        1,
        1,
        0,
        0,
        1,
    )
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_key,reason,state FROM notification_outbox ORDER BY event_key"
        ).fetchall()
        assert rows == [
            ("github:8:1", "new_qualified_bounty", "sent"),
            ("github:8:2", "reward_increased", "retryable_failure"),
        ]
    state = RunState(database)
    try:
        assert state.last_complete_at() == clock[0]
    finally:
        state.close()

    # Simulate process exit: all connections closed by _run. A new invocation
    # reopens the database and delivers the persisted retry after its backoff.
    clock[0] += timedelta(minutes=5)
    assert await _run(settings, False, funding, transport, clock=lambda: clock[0]) == (
        1,
        1,
        0,
        1,
        0,
        0,
    )
    assert len(sent) == 2
    assert "USD 150" in sent[1]["Subject"]
    assert (
        "https://funding.example/records/8"
        in sent[1].get_body(preferencelist=("plain",)).get_content()
    )
    outbox = NotificationOutbox(database, settings, clock=lambda: clock[0])
    try:
        assert outbox.list_entries() == []
    finally:
        outbox.close()

    clock[0] += timedelta(minutes=5)
    issue_open[0] = False
    assert await _run(settings, False, funding, transport, clock=lambda: clock[0]) == (
        0,
        0,
        0,
        0,
        0,
        0,
    )
    assert len(sent) == 2

    clock[0] += timedelta(minutes=5)
    issue_open[0] = True
    updated[0] = clock[0]
    assert await _run(settings, False, funding, transport, clock=lambda: clock[0]) == (
        1,
        1,
        1,
        1,
        0,
        0,
    )
    assert len(sent) == 3
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT reason FROM notification_outbox WHERE event_key='github:8:3'"
            ).fetchone()[0]
            == "available_again"
        )


@pytest.mark.asyncio
async def test_older_claim_gains_external_funding_without_github_update(tmp_path, respx_mock):
    clock = [START]
    visible_in_search = [True]
    funded = [False]
    sent = []
    issue = {
        "id": 8,
        "number": 8,
        "title": "Bounty: repair parser",
        "body": "Reward $100",
        "html_url": ISSUE_URL,
        "state": "open",
        "created_at": START.isoformat(),
        "updated_at": START.isoformat(),
        "labels": [],
        "assignees": [],
    }
    respx_mock.get(API_URL).mock(
        side_effect=lambda _request: httpx.Response(
            200,
            json={
                "total_count": 1 if visible_in_search[0] else 0,
                "incomplete_results": False,
                "items": [issue] if visible_in_search[0] else [],
            },
        )
    )
    respx_mock.get(API_ISSUE).mock(
        return_value=httpx.Response(
            200,
            json={"state": "open", "body": "Reward $100", "assignees": [], "comments": 0},
        )
    )
    respx_mock.get(f"{API_ISSUE}/timeline").mock(return_value=httpx.Response(200, json=[]))

    class Funding:
        async def records_for(self, _candidate):
            if not funded[0]:
                return []
            return [
                FundingRecord(
                    provider="trusted_api",
                    issue_url=ISSUE_URL,
                    evidence_url="https://funding.example/records/8",
                    observed_at=clock[0],
                    status="funded",
                    amount=Decimal("100"),
                    currency="USD",
                )
            ]

    class Mail:
        async def send(self, message):
            sent.append(message)

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "data" / "bounties.db",
        email_enabled=True,
        smtp_username="sender@example.com",
        smtp_password="fake-app-password",
        email_to="recipient@example.com",
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
        github_min_request_interval_seconds=0,
        trusted_funding_hosts=["funding.example"],
        trusted_funding_providers=["trusted_api"],
    )
    assert await _run(settings, False, Funding(), Mail(), clock=lambda: clock[0]) == (
        1,
        0,
        0,
        0,
        0,
        0,
    )
    visible_in_search[0] = False
    funded[0] = True
    clock[0] += timedelta(minutes=5)
    assert await _run(settings, False, Funding(), Mail(), clock=lambda: clock[0]) == (
        0,
        1,
        1,
        1,
        0,
        0,
    )
    assert len(sent) == 1
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT reason FROM notification_outbox").fetchone()[0] == (
            "funding_verified"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "claim-only", "cancelled", "unrelated", "untrusted", "rate-limited", "unavailable",
])
async def test_pipeline_refuses_invalid_or_unavailable_funding(tmp_path, respx_mock, case):
    """The funding.example endpoint is a test fixture, not a real provider API."""
    issue = {
        "id": 8, "number": 8, "title": "Bounty: repair parser", "body": "Reward $100",
        "html_url": ISSUE_URL, "state": "open", "created_at": START.isoformat(),
        "updated_at": START.isoformat(), "assignees": [], "comments": 0,
    }
    respx_mock.get(API_URL).respond(200, json={
        "total_count": 1, "incomplete_results": False, "items": [issue],
    })
    respx_mock.get(API_ISSUE).respond(200, json=issue)
    respx_mock.get(f"{API_ISSUE}/timeline").respond(200, json=[])
    funding_url = "https://funding.example/records/8"
    record = FundingRecord(
        provider="untrusted_api" if case == "untrusted" else "trusted_api",
        issue_url=(
            "https://github.com/acme/tool/issues/9" if case == "unrelated" else ISSUE_URL
        ),
        evidence_url=funding_url, observed_at=START,
        status="cancelled" if case == "cancelled" else "funded",
        amount=Decimal("100"), currency="USD",
    )
    respx_mock.get(funding_url).respond(
        429 if case == "rate-limited" else 503 if case == "unavailable" else 200,
        headers={"Retry-After": "60"},
        json=[] if case == "claim-only" else [record.model_dump(mode="json")],
    )

    class FixtureFunding:
        async def records_for(self, candidate):
            assert candidate.url == ISSUE_URL
            async with httpx.AsyncClient() as client:
                response = await client.get(funding_url)
            response.raise_for_status()
            return [FundingRecord.model_validate(value) for value in response.json()]

    class ForbiddenMail:
        async def send(self, _message):
            raise AssertionError("unverified funding must not produce email")

    settings = Settings(
        _env_file=None, database_path=tmp_path / "funding.db", email_enabled=True,
        smtp_username="sender@example.com", smtp_password="offline-fixture",
        email_to="recipient@example.com", search_terms=["bounty"], search_labels=[],
        monetary_terms=[], github_min_request_interval_seconds=0,
        trusted_funding_hosts=["funding.example"], trusted_funding_providers=["trusted_api"],
    )
    summary = await _run(
        settings, False, FixtureFunding(), ForbiddenMail(), clock=lambda: START
    )
    assert summary == (1, 0, 0, 0, 0, 0)
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0
        stored = db.execute(
            "SELECT json_extract(payload_json, '$.category'), "
            "json_extract(payload_json, '$.missing_sources') FROM verification_results"
        ).fetchone()
        assert stored[0] != "FUNDED_EVIDENCE"
        if case in {"rate-limited", "unavailable"}:
            assert "funding provider unavailable" in stored[1]
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    state = RunState(settings.database_path)
    try:
        assert state.last_complete_at() == START
    finally:
        state.close()
