"""Regression cases identified by the independent production review."""

import smtplib
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_discovery import NOW, engine, issue, payload
from test_email import FakeSMTP, options, result
from test_verification import FakeInspector, candidate

from bountyradar.delivery import github_revalidator
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.notification import DeliveryError, GmailSMTPTransport
from bountyradar.notification import test_message as mail
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunState
from bountyradar.verification_models import IssueInspection


@pytest.mark.asyncio
async def test_discovery_queries_are_public(respx_mock):
    route = respx_mock.get(API_URL).respond(200, json=payload())
    async with httpx.AsyncClient() as client:
        await engine(client).scan()
    assert all("is:public" in call.request.url.params["q"] for call in route.calls)


@pytest.mark.asyncio
async def test_comment_only_search_match_is_retained(respx_mock):
    respx_mock.get(API_URL).respond(
        200, json=payload([issue(title="Repair parser", body="Reproduction steps", labels=[])])
    )
    async with httpx.AsyncClient() as client:
        scan = await engine(client).scan()
    assert len(scan.candidates) == 1
    assert scan.candidates[0].potential_reward_amount is None


@pytest.mark.asyncio
async def test_search_budget_resumes_frozen_scan_after_restart(tmp_path, respx_mock):
    route = respx_mock.get(API_URL).respond(200, json=payload([issue()]))
    path = tmp_path / "resume.sqlite3"
    settings = options(
        database_path=path,
        search_terms=["bounty", "reward"],
        search_labels=[],
        monetary_terms=[],
        github_max_search_requests_per_scan=1,
    )
    for index in range(4):
        state = RunState(path)
        async with httpx.AsyncClient() as client:
            finder = GitHubDiscovery(
                client, settings, clock=lambda index=index: NOW + timedelta(minutes=index * 5),
                progress=state,
            )
            scan = await finder.scan()
        if index < 3:
            assert not scan.complete
        else:
            assert scan.complete
            assert scan.covered_until == NOW
        state.close()
    queries = [call.request.url.params["q"] for call in route.calls]
    assert len(queries) == len(set(queries)) == 4
    state = RunState(path)
    assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
    state.close()


@pytest.mark.asyncio
async def test_verification_queue_rotates_past_newest_and_recovers_offline(tmp_path, monkeypatch):
    from bountyradar import cli
    from bountyradar.models import ScanResult

    candidates = [
        candidate(source_issue_id=i, issue_number=i, url=f"https://github.com/acme/tool/issues/{i}")
        for i in range(1, 4)
    ]
    inspected = []

    class Inspector(FakeInspector):
        requests = 0
        next_request_at = 0
        rate_limit_reset = None

        async def inspect(self, item):
            inspected.append(item.source_issue_id)
            return await super().inspect(item)

    class Source:
        name = "github"

        async def scan(self, _hours=None):
            return ScanResult(candidates=candidates, unprocessed_windows=[])

    monkeypatch.setattr(cli, "GitHubIssueInspector", lambda *a, **kw: Inspector())
    settings = options(
        email_enabled=False, database_path=tmp_path / "queue.sqlite3",
        max_verifications_per_scan=1, max_rechecks_per_scan=1,
    )
    source = Source()
    for step in range(3):
        await cli._run(
            settings, False, sources=[source],
            clock=lambda step=step: NOW + timedelta(minutes=step * 5)
        )
    assert set(inspected) == {1, 2, 3}
    assert len(inspected) == 3


@pytest.mark.asyncio
async def test_inspector_does_not_repeat_requests_during_long_rate_limit(respx_mock):
    route = respx_mock.get("https://api.github.com/repos/acme/tool/issues/8").respond(
        429, headers={"retry-after": "3600"}
    )
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, options(), clock=lambda: NOW)
        assert (await inspector.inspect(candidate())).missing_sources
        assert (await inspector.inspect(candidate())).missing_sources
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Fix #8", "Fixed #8", "Resolve #8", "Closes acme/tool#8"])
async def test_all_github_closing_forms_block_competing_pr(text, respx_mock):
    base = "https://api.github.com/repos/acme/tool"
    respx_mock.get(f"{base}/issues/8").respond(
        200, json={"state": "open", "title": "Bounty", "comments": 0,
                   "labels": [], "assignees": [], "body": ""}
    )
    respx_mock.get(f"{base}/issues/8/timeline").respond(
        200,
        json=[{
            "event": "cross-referenced",
            "source": {"issue": {
                "html_url": "https://github.com/acme/tool/pull/9",
                "pull_request": {},
            }},
        }],
    )
    respx_mock.get(f"{base}/pulls/9").respond(
        200, json={"state": "open", "title": text, "body": ""}
    )
    async with httpx.AsyncClient() as client:
        inspected = await GitHubIssueInspector(client, options()).inspect(candidate())
    assert inspected.competing_work_found


@pytest.mark.asyncio
async def test_delivery_checks_title_cancellation():
    inspector = FakeInspector(IssueInspection(state="open", title="Bounty cancelled"))
    assert await github_revalidator(inspector, result(), now=NOW) is False


def test_current_funding_refreshes_unsent_snapshot(tmp_path):
    now = [NOW]
    store = NotificationOutbox(tmp_path / "outbox.sqlite3", options(), clock=lambda: now[0])
    try:
        first = result()
        key = store.enqueue(first)
        now[0] += timedelta(days=2)
        fresh = first.model_copy(deep=True)
        fresh.checked_at = now[0]
        fresh.evidence[0].observed_at = now[0]
        fresh.amount = Decimal("90")
        assert store.enqueue(fresh) is None
        entry = store.list_entries(due_only=True)[0]
        assert entry.event_key == key
        assert entry.result.checked_at == now[0]
        assert entry.result.amount == 90
    finally:
        store.close()


def test_zero_increase_threshold_still_requires_an_increase(tmp_path):
    store = NotificationOutbox(
        tmp_path / "outbox.sqlite3", options(reward_increase_min_percent=0), clock=lambda: NOW
    )
    try:
        assert store.enqueue(result()) is not None
        assert store.enqueue(result()) is None
    finally:
        store.close()


def test_temporary_recipient_rejection_is_retryable():
    class TemporaryRejection(FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused({"inbox@example.com": (450, b"try later")})

    config = options()
    transport = GmailSMTPTransport(config, smtp_factory=lambda *a, **kw: TemporaryRejection())
    with pytest.raises(DeliveryError) as error:
        transport.send(mail(config))
    assert error.value.outcome == "retryable"
