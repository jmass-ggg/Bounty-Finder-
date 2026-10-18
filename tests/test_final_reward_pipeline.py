"""Independent search-to-delivery acceptance cases with offline HTTP and mail."""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_all_prices import options
from test_discovery import API_URL, NOW, issue, no_sleep, payload

from bountyradar.delivery import github_revalidator, send_pending
from bountyradar.discovery import GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.notification import eligible_for_delivery
from bountyradar.outbox import NotificationOutbox
from bountyradar.verification import Verifier

URL = "https://github.com/acme/tool/issues/1"
DETAIL = "https://api.github.com/repos/acme/tool/issues/1"
PROPOSAL = (Path(__file__).parent / "fixtures" / "omi-22091.md").read_text()
PROPOSAL_TITLE = (
    "[Bounty proposal] fix(database): sanitize uid and protect announcement streams "
    "against store failures ( proposed)"
)


class CapturingTransport:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def config():
    return options(
        notification_mode="include_unverified",
        email_enabled=True,
        smtp_username="sender@example.invalid",
        smtp_password="offline",
        email_to="inbox@example.invalid",
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,amount,labels,assignees,eligible",
    [
        ("Reward $0.01 for fixing this issue", "0.01", [], [], True),
        ("Reward $1 for fixing this issue", "1", [], [], True),
        ("Paid task: $5 USD", "5", [], [], True),
        ("We will pay $50 for the implementation", "50", [], [], True),
        ("Reward $100 for completing this issue", "100", [], [], True),
        ("[Bounty: $5](https://github.com/acme/tool/issues/1)", "5", [], [], True),
        ("The previous bounty was $50, already paid.", None, [], [], False),
        ("The product subscription is $10/month", None, [], [], False),
        ("Model inference costs $0.01 per request", None, [], [], False),
        ("Vendor account funded $11 yesterday", None, [], [], False),
        ("[Bounty: $80](https://github.com/other/tool/issues/37)", None, [], [], False),
        ("Amount $11", None, [], [], False),
        ("Reward $50 for fixing this issue", "50", [{"name": "claimed"}], [], False),
        ("Reward $50 for fixing this issue", "50", [], [{"login": "developer"}], False),
        ("Reward $50 for fixing this issue", "50", [{"name": "in progress"}], [], False),
        (PROPOSAL, None, [], [], False),
        ("Requested bounty: $50 for my completed implementation", None, [], [], False),
        ("Reward $50 for implementing the proposed API", "50", [], [], True),
    ],
    ids=[
        "cent", "one-dollar", "five-dollars", "fifty-dollars", "hundred-dollars",
        "same-issue", "historical", "subscription", "api-price", "settlement",
        "other-issue", "ambiguous", "claimed", "assigned", "contested",
        "proposed-payout-incident", "requested-payment", "proposed-work-with-offer",
    ],
)
async def test_search_through_final_delivery(
    tmp_path, respx_mock, body, amount, labels, assignees, eligible,
):
    settings = config()
    title = PROPOSAL_TITLE if body == PROPOSAL else "Implement this feature"
    url = "https://github.com/BasedHardware/omi/issues/22091" if body == PROPOSAL else URL
    base = url.replace("https://github.com/", "https://api.github.com/repos/")
    raw = issue(22091 if body == PROPOSAL else 1, title=title, body=body, html_url=url,
                labels=labels, assignees=assignees, comments=0)
    respx_mock.get(API_URL).respond(200, json=payload([raw]))
    detail = respx_mock.get(base).respond(200, json=raw)
    respx_mock.get(base + "/timeline").respond(200, json=[])
    transport = CapturingTransport()
    store = NotificationOutbox(tmp_path / "pipeline.db", settings, clock=lambda: NOW)
    try:
        async with httpx.AsyncClient() as client:
            scan = await GitHubDiscovery(
                client, settings, clock=lambda: NOW, sleep=no_sleep,
            ).scan(24)
            assert scan.complete
            assert len(scan.candidates) == 1
            candidate = scan.candidates[0]
            expected = Decimal(amount) if amount else None
            assert candidate.potential_reward_amount == expected
            inspector = GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep)
            result = await Verifier(inspector, settings, clock=lambda: NOW).verify(candidate)
            assert result.amount == expected
            assert eligible_for_delivery(result, settings, NOW) is eligible
            if eligible:
                assert result.notification_classification == "POTENTIAL_UNVERIFIED"
            assert (store.enqueue(result) is not None) is eligible
            # A different inspector must fetch current GitHub data before delivery.
            fresh = GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep)
            assert await github_revalidator(fresh, result, now=NOW) is eligible
            detail.reset()
            delivery = GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep)

            async def revalidate(value):
                return await github_revalidator(delivery, value, now=NOW)

            summary = await send_pending(store, transport, settings, revalidate)
            assert summary.sent == int(eligible)
            assert len(transport.messages) == int(eligible)
            assert detail.call_count == int(eligible)
            assert store.db.execute(
                "SELECT count(*) FROM notification_delivery_attempts",
            ).fetchone()[0] == int(eligible)
            assert store.db.execute(
                "SELECT count(*) FROM notification_outbox "
                "WHERE state IN ('pending','sending','retryable_failure')",
            ).fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,labels,assignees,status,pre_send,terminal",
    [
        ("Please implement this feature", [], [], 200, False, "cancelled"),
        ("Vendor account funded $11 yesterday", [], [], 200, False, "cancelled"),
        ("[Bounty: $80](https://github.com/other/tool/issues/37)",
         [], [], 200, False, "cancelled"),
        ("Amount $50", [], [], 200, False, "cancelled"),
        ("Reward $50 for fixing this issue", [{"name": "claimed"}], [],
         200, False, "cancelled"),
        ("Reward $50 for fixing this issue", [], [{"login": "developer"}],
         200, False, "cancelled"),
        ("Reward $50 for fixing this issue", [], [], 503, None, "pending"),
        ("Reward $5 for fixing this issue", [], [], 200, None, "pending"),
        (PROPOSAL, [], [], 200, False, "cancelled"),
    ],
    ids=[
        "removed-reward", "stale-settlement", "stale-cross-issue", "ambiguous",
        "new-claim", "new-assignment", "unknown-availability", "changed-amount",
        "proposed-payout-incident",
    ],
)
async def test_cached_eligible_result_never_bypasses_fresh_delivery_check(
    tmp_path, respx_mock, body, labels, assignees, status, pre_send, terminal,
):
    settings = config().model_copy(update={"github_max_retries": 0})
    original = issue(title="Implement this feature", body="Reward $50 for fixing this issue",
                     html_url=URL, labels=[], comments=0)
    detail = respx_mock.get(DETAIL).respond(200, json=original)
    respx_mock.get(DETAIL + "/timeline").respond(200, json=[])
    store = NotificationOutbox(tmp_path / "stale.db", settings, clock=lambda: NOW)
    transport = CapturingTransport()
    try:
        async with httpx.AsyncClient() as client:
            candidate = GitHubDiscovery(client, settings, clock=lambda: NOW)._candidate(original)
            cached = await Verifier(
                GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep),
                settings, clock=lambda: NOW,
            ).verify(candidate)
            assert eligible_for_delivery(cached, settings, NOW)
            assert store.enqueue(cached)
            current = issue(title="Implement this feature", body=body, html_url=URL,
                            labels=labels, assignees=assignees, comments=0)
            detail.respond(status, json=current if status == 200 else {"message": "Unavailable"})
            fresh = GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep)
            assert await github_revalidator(fresh, cached, now=NOW) is pre_send
            delivery = GitHubIssueInspector(client, settings, clock=lambda: NOW, sleep=no_sleep)

            async def revalidate(value):
                return await github_revalidator(delivery, value, now=NOW)

            summary = await send_pending(store, transport, settings, revalidate)
            assert summary.sent == 0
            assert summary.cancelled == int(terminal == "cancelled")
            assert summary.deferred == int(terminal == "pending")
            assert not transport.messages
            assert store.db.execute(
                "SELECT state FROM notification_outbox",
            ).fetchone()[0] == terminal
            assert store.db.execute(
                "SELECT count(*) FROM notification_delivery_attempts",
            ).fetchone()[0] == 0
    finally:
        store.close()
