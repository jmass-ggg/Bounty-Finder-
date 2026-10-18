"""Offline SMTP and durable outbox behavior; no real network delivery."""

import smtplib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bountyradar.delivery import send_pending
from bountyradar.models import BountyCandidate
from bountyradar.notification import (
    DeliveryError,
    GmailSMTPTransport,
    opportunity_message,
)
from bountyradar.notification import (
    test_message as make_test_message,
)
from bountyradar.outbox import NotificationOutbox
from bountyradar.settings import Settings
from bountyradar.verification_models import Availability, Category, Evidence, VerificationResult

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def options(**values):
    defaults = dict(
        email_enabled=True,
        smtp_username="sender@example.com",
        smtp_password="placeholder-app-password",
        email_to="inbox@example.com",
        github_min_request_interval_seconds=0,
        trusted_funding_hosts=["funding.example"],
        trusted_funding_providers=["trusted_api"],
    )
    return Settings(_env_file=None, **(defaults | values))


def result(
    issue_id=8,
    amount="100",
    *,
    category=Category.FUNDED_EVIDENCE,
    availability=Availability.AVAILABLE,
    title="Fix parser",
    updated_at=NOW,
    eligible=True,
    assignees=None,
    restrictions=None,
):
    issue = BountyCandidate(
        source_issue_id=issue_id,
        repository="acme/tool",
        issue_number=issue_id,
        title=title,
        url=f"https://github.com/acme/tool/issues/{issue_id}",
        body="Bounty $100",
        created_at=NOW - timedelta(days=20),
        updated_at=updated_at,
        discovered_at=NOW,
    )
    return VerificationResult(
        candidate=issue,
        category=category,
        availability=availability,
        amount=Decimal(amount),
        currency="USD",
        checked_at=NOW,
        score=95,
        evidence=[
            Evidence(
                kind="funding_funded",
                source="trusted_api",
                url=f"https://funding.example/{issue_id}",
                issue_url=issue.url,
                observed_at=NOW,
                detail=f"{amount} USD",
            )
        ],
        high_priority_email_eligible=eligible,
        notification_eligible=eligible,
        assignees=assignees or [],
        restrictions=restrictions or [],
    )


@pytest.fixture
def setup_outbox(tmp_path):
    current = [NOW]
    store = NotificationOutbox(tmp_path / "data.sqlite3", options(), clock=lambda: current[0])
    yield store, current
    store.close()


class FakeSMTP:
    def __init__(self, *, login_error=None, send_error=None):
        self.login_error = login_error
        self.send_error = send_error
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def login(self, _user, _password):
        if self.login_error:
            raise self.login_error

    def send_message(self, message):
        if self.send_error:
            raise self.send_error
        self.messages.append(message)
        return {}


def smtp_factory(fake):
    def create(_host, _port, *, timeout, context):
        assert timeout > 0
        assert context is not None
        return fake

    return create


def test_successful_test_email_with_mocked_smtp():
    fake = FakeSMTP()
    settings = options()
    GmailSMTPTransport(settings, smtp_factory(fake)).send(make_test_message(settings))
    assert len(fake.messages) == 1
    assert fake.messages[0]["Subject"] == "BountyRadar test email"
    assert fake.messages[0].get_body(preferencelist=("plain",))
    assert fake.messages[0].get_body(preferencelist=("html",))


@pytest.mark.parametrize(
    "error,outcome",
    [
        (smtplib.SMTPAuthenticationError(535, b"bad auth"), "permanent"),
        (TimeoutError("timeout"), "retryable"),
        (OSError("network down"), "retryable"),
    ],
)
def test_authentication_timeout_and_network_failure(error, outcome):
    settings = options()
    fake = FakeSMTP(login_error=error)
    with pytest.raises(DeliveryError) as caught:
        GmailSMTPTransport(settings, smtp_factory(fake)).send(make_test_message(settings))
    assert caught.value.outcome == outcome
    assert "placeholder-app-password" not in str(caught.value)


def test_timeout_after_send_attempt_has_unknown_outcome():
    settings = options()
    fake = FakeSMTP(send_error=TimeoutError("lost response"))
    with pytest.raises(DeliveryError) as caught:
        GmailSMTPTransport(settings, smtp_factory(fake)).send(make_test_message(settings))
    assert caught.value.outcome == "unknown"


def test_html_escapes_untrusted_fields():
    settings = options()
    message = opportunity_message(
        result(title='<script>alert("x")</script>', restrictions=["Contributors must <b>sign</b>"]),
        settings,
        "new_qualified_bounty",
    )
    html = message.get_body(preferencelist=("html",)).get_content()
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html
    assert '<script>alert("x")</script>' in plain
    assert "https://funding.example/8" in plain


def test_issue_title_cannot_inject_email_headers():
    message = opportunity_message(
        result(title="Fix parser\r\nBcc: attacker@example.com"),
        options(),
        "new_qualified_bounty",
    )
    assert message["Bcc"] is None
    assert "\n" not in message["Subject"]


def test_transition_keys_duplicate_increase_and_requalification(setup_outbox):
    store, _clock = setup_outbox
    first = result()
    assert store.enqueue(first) == "github:8:1"
    assert store.enqueue(first.model_copy(deep=True)) is None
    ordinary = result(amount="110", updated_at=NOW + timedelta(minutes=1))
    assert store.enqueue(ordinary) is None
    increased = result(amount="125", updated_at=NOW + timedelta(minutes=2))
    assert store.enqueue(increased) == "github:8:2"
    assert store.enqueue(increased) is None
    unavailable = result(
        category=Category.UNAVAILABLE, availability=Availability.CLOSED, eligible=False
    )
    assert store.enqueue(unavailable) is None
    assert store.enqueue(first) == "github:8:3"
    assert [item.reason for item in store.list_entries()] == [
        "new_qualified_bounty",
        "reward_increased",
        "available_again",
    ]


def test_new_funding_on_older_issue(setup_outbox):
    store, _clock = setup_outbox
    claimed = result(category=Category.REWARD_CLAIMED, eligible=False)
    assert store.enqueue(claimed) is None
    assert store.enqueue(result()) == "github:8:1"
    assert store.list_entries()[0].reason == "funding_verified"
    assert store.enqueue(result()) is None


def test_new_reward_supersedes_older_unsent_alert(setup_outbox):
    store, _clock = setup_outbox
    assert store.enqueue(result(amount="100")) == "github:8:1"
    assert store.enqueue(result(amount="150")) == "github:8:2"
    assert [(entry.event_key, entry.state) for entry in store.list_entries()] == [
        ("github:8:1", "cancelled"),
        ("github:8:2", "pending"),
    ]
    assert [entry.event_key for entry in store.list_entries(due_only=True)] == ["github:8:2"]


def test_temporary_missing_evidence_does_not_create_requalification_alert(setup_outbox):
    store, _clock = setup_outbox
    assert store.enqueue(result()) == "github:8:1"
    unavailable_evidence = result(category=Category.REWARD_CLAIMED, eligible=False)
    unavailable_evidence.missing_sources = ["funding provider unavailable"]
    assert store.enqueue(unavailable_evidence) is None
    assert store.enqueue(result()) is None
    assert [entry.event_key for entry in store.list_entries()] == ["github:8:1"]


def test_score_only_requalification_does_not_send_again(setup_outbox):
    store, _clock = setup_outbox
    assert store.enqueue(result()) == "github:8:1"
    low_score = result(eligible=False)
    low_score.score = 60
    assert store.enqueue(low_score) is None
    assert store.enqueue(result()) is None
    assert [entry.event_key for entry in store.list_entries()] == ["github:8:1"]
    store.enqueue(low_score)
    assert store.enqueue(result(amount="150")) == "github:8:2"
    assert store.list_entries()[-1].reason == "reward_increased"


def test_explicit_closure_is_recorded_even_when_funding_source_is_unavailable(setup_outbox):
    store, _clock = setup_outbox
    assert store.enqueue(result()) == "github:8:1"
    closed = result(category=Category.UNAVAILABLE, availability=Availability.CLOSED, eligible=False)
    closed.missing_sources = ["funding provider unavailable"]
    assert store.enqueue(closed) is None
    assert store.enqueue(result()) == "github:8:2"
    assert store.list_entries()[-1].reason == "available_again"


class FakeTransport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome:
                raise DeliveryError(outcome, "mock failure")


async def valid(_result):
    return True


@pytest.mark.asyncio
async def test_successful_retry_and_sent_deduplication(setup_outbox):
    store, clock = setup_outbox
    store.enqueue(result())
    transport = FakeTransport(["retryable", None])
    first = await send_pending(store, transport, options(email_retry_base_seconds=1), valid)
    assert first.retryable_failure == 1
    assert store.list_entries()[0].state == "retryable_failure"
    assert (
        await send_pending(store, transport, options(email_retry_base_seconds=1), valid)
    ).sent == 0
    clock[0] += timedelta(seconds=60)
    second = await send_pending(store, transport, options(email_retry_base_seconds=1), valid)
    assert second.sent == 1
    assert store.list_entries() == []
    assert (
        await send_pending(store, transport, options(email_retry_base_seconds=1), valid)
    ).sent == 0
    assert len(transport.messages) == 2


@pytest.mark.asyncio
async def test_unknown_outcome_is_not_automatically_retried(setup_outbox):
    store, clock = setup_outbox
    key = store.enqueue(result())
    summary = await send_pending(store, FakeTransport(["unknown"]), options(), valid)
    assert summary.unknown_delivery == 1
    assert store.list_entries()[0].state == "unknown_delivery"
    clock[0] += timedelta(days=1)
    assert (await send_pending(store, FakeTransport(), options(), valid)).sent == 0
    assert store.resolve_unknown(key, delivered=True)
    assert store.list_entries() == []


def test_crash_recovery_marks_sending_unknown(setup_outbox):
    store, clock = setup_outbox
    key = store.enqueue(result())
    assert store.claim(key)
    clock[0] += timedelta(seconds=301)
    assert store.recover_expired() == 1
    assert store.list_entries()[0].state == "unknown_delivery"
    assert store.list_entries(due_only=True) == []


@pytest.mark.asyncio
async def test_daily_cap_includes_claims_and_test_email(tmp_path):
    current = [NOW]
    settings = options(email_daily_cap=1)
    store = NotificationOutbox(Path(tmp_path) / "cap.sqlite3", settings, clock=lambda: current[0])
    try:
        store.enqueue(result(issue_id=8))
        store.enqueue(result(issue_id=9))
        summary = await send_pending(store, FakeTransport(), settings, valid)
        assert summary.sent == 1
        assert summary.daily_cap_reached
        assert store.claim_test() is None
        assert len(store.list_entries(due_only=True)) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_no_email_when_disabled_or_no_longer_available(setup_outbox):
    store, _clock = setup_outbox
    store.enqueue(result())
    transport = FakeTransport()
    assert (await send_pending(store, transport, options(email_enabled=False), valid)).disabled

    async def unavailable(_result):
        return False

    summary = await send_pending(store, transport, options(), unavailable)
    assert summary.cancelled == 1
    assert transport.messages == []
    assert store.list_entries()[0].state == "cancelled"


def test_forged_eligibility_or_wrong_issue_funding_is_not_queued(setup_outbox):
    store, _clock = setup_outbox
    forged = result(category=Category.REWARD_CLAIMED, eligible=True)
    assert store.enqueue(forged) is None
    wrong_link = result()
    wrong_link.evidence[0].issue_url = "https://github.com/acme/tool/issues/999"
    assert store.enqueue(wrong_link) is None
    assert store.list_entries() == []


def test_outbox_persists_across_reopen(tmp_path):
    path = tmp_path / "durable.sqlite3"
    first = NotificationOutbox(path, options(), clock=lambda: NOW)
    assert first.enqueue(result()) == "github:8:1"
    first.close()
    second = NotificationOutbox(path, options(), clock=lambda: NOW)
    try:
        assert second.enqueue(result()) is None
        assert [entry.event_key for entry in second.list_entries()] == ["github:8:1"]
    finally:
        second.close()


def test_email_settings_disabled_by_default_and_require_credentials(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.delenv("EMAIL_ENABLED", raising=False)
    assert not Settings(_env_file=None).email_enabled
    with pytest.raises(ValidationError):
        Settings(_env_file=None, email_enabled=True)


@pytest.mark.parametrize("address", ["a@example.com,evil@example.com", "a@b\r\nBcc:evil@x.com"])
def test_email_settings_reject_address_injection(address):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        options(email_to=address)


def test_cli_email_test_only_uses_mocked_smtp(tmp_path, monkeypatch, capsys):
    from bountyradar import cli

    monkeypatch.chdir(tmp_path)
    fake = FakeSMTP()

    class MockTransport:
        def __init__(self, settings):
            self.backend = GmailSMTPTransport(settings, smtp_factory(fake))

        async def send(self, message):
            self.backend.send(message)

    monkeypatch.setattr(cli, "SMTPProcessTransport", MockTransport)
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("BOUNTYRADAR_SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("BOUNTYRADAR_SMTP_PASSWORD", "placeholder-app-password")
    monkeypatch.setenv("BOUNTYRADAR_EMAIL_TO", "inbox@example.com")
    monkeypatch.setenv("BOUNTYRADAR_DATABASE_PATH", str(tmp_path / "cli.sqlite3"))
    assert cli.main(["email", "test"]) == 0
    assert len(fake.messages) == 1
    assert "accepted by SMTP" in capsys.readouterr().out
    assert cli.main(["email", "pending"]) == 0
    assert len(fake.messages) == 1


@pytest.mark.asyncio
async def test_run_dry_run_uses_mocked_github_without_database(tmp_path, respx_mock):
    import httpx

    from bountyradar.cli import _run
    from bountyradar.discovery import API_URL

    issue_payload = {
        "id": 8,
        "number": 8,
        "title": "Bounty: fix parser",
        "body": "Reward $100",
        "html_url": "https://github.com/acme/tool/issues/8",
        "state": "open",
        "created_at": "2026-10-17T10:00:00Z",
        "updated_at": "2026-10-17T11:00:00Z",
        "labels": [],
        "assignees": [],
    }
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"total_count": 1, "incomplete_results": False, "items": [issue_payload]}
        )
    )
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "body": "Reward $100",
                "labels": [],
                "assignees": [],
                "comments": 0,
            },
        )
    )
    respx_mock.get(f"{base}/timeline").mock(return_value=httpx.Response(200, json=[]))
    database = tmp_path / "dry-run.sqlite3"
    settings = options(
        email_enabled=False,
        database_path=database,
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
    )
    counts = await _run(settings, True, clock=lambda: NOW)
    assert counts == (1, 0, 0, 0, 0, 0)
    assert not database.exists()


@pytest.mark.asyncio
async def test_run_queues_and_delivers_once_with_trusted_funding(tmp_path, respx_mock):
    import httpx

    from bountyradar.cli import _run
    from bountyradar.discovery import API_URL
    from bountyradar.verification_models import FundingRecord

    now = NOW
    funded_amount = [Decimal("100")]
    issue_url = "https://github.com/acme/tool/issues/8"
    payload = {
        "id": 8,
        "number": 8,
        "title": "Bounty: fix parser",
        "body": "Reward $100",
        "html_url": issue_url,
        "state": "open",
        "created_at": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "labels": [],
        "assignees": [],
    }
    respx_mock.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"total_count": 1, "incomplete_results": False, "items": [payload]}
        )
    )
    api_issue = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(api_issue).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "body": "Reward $100",
                "labels": [],
                "assignees": [],
                "comments": 0,
            },
        )
    )
    respx_mock.get(f"{api_issue}/timeline").mock(return_value=httpx.Response(200, json=[]))

    class TrustedFunding:
        async def records_for(self, _candidate):
            return [
                FundingRecord(
                    provider="trusted_api",
                    issue_url=issue_url,
                    evidence_url="https://funding.example/8",
                    observed_at=now,
                    status="funded",
                    amount=funded_amount[0],
                    currency="USD",
                )
            ]

    settings = options(
        database_path=tmp_path / "notifications.sqlite3",
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
    )
    transport = FakeTransport()
    assert await _run(settings, False, TrustedFunding(), transport, clock=lambda: now) == (
        1,
        1,
        1,
        1,
        0,
        0,
    )
    assert await _run(settings, False, TrustedFunding(), transport, clock=lambda: now) == (
        1,
        1,
        0,
        0,
        0,
        0,
    )
    assert len(transport.messages) == 1
    from bountyradar.run_state import RunState

    funded_amount[0] = Decimal("125")
    transport.outcomes.append("retryable")
    assert await _run(settings, False, TrustedFunding(), transport, clock=lambda: now) == (
        1,
        1,
        1,
        0,
        0,
        1,
    )
    state = RunState(settings.database_path)
    try:
        assert state.last_complete_at() is not None
    finally:
        state.close()
    outbox = NotificationOutbox(settings.database_path, settings)
    try:
        assert [entry.state for entry in outbox.list_entries()] == ["retryable_failure"]
    finally:
        outbox.close()
