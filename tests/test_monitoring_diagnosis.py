"""Production regressions: monetary priority, commercial noise and search churn."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from test_all_prices import options
from test_discovery import API_URL, NOW, issue, no_sleep, payload
from test_verification import FakeInspector, candidate

from bountyradar.delivery import github_revalidator, send_pending
from bountyradar.discovery import GitHubDiscovery, reward_mention
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.models import SearchProgress, SearchWindow
from bountyradar.notification import eligible_for_delivery
from bountyradar.outbox import NotificationOutbox
from bountyradar.rewards import RewardKind, assess_reward
from bountyradar.run_state import RunState
from bountyradar.verification import Verifier
from bountyradar.verification_models import IssueInspection


def test_pending_monetary_signal_precedes_newer_nonmonetary_noise(tmp_path):
    state = RunState(tmp_path / "priority.db")
    old = NOW - timedelta(days=30)
    historical = [
        candidate(source_issue_id=n, issue_number=n, created_at=old, updated_at=old,
                  discovered_at=old, url=f"https://github.com/acme/tool/issues/{n}")
        for n in range(1, 50_001)
    ]
    noise = [
        candidate(source_issue_id=n, issue_number=n, potential_reward_amount=None,
                  currency=None, body="Discuss paid API access", discovered_at=NOW,
                  url=f"https://github.com/acme/tool/issues/{n}")
        for n in range(100_000, 100_500)
    ]
    bounty = candidate(
        source_issue_id=200_000, issue_number=200_000,
        url="https://github.com/acme/tool/issues/200000",
        potential_reward_amount=Decimal("1"), body="Bounty $1",
        discovered_at=NOW - timedelta(minutes=35),
        updated_at=NOW - timedelta(hours=1),
    )
    monetary = [bounty] + [
        bounty.model_copy(update={
            "source_issue_id": 200_000 + n, "issue_number": 200_000 + n,
            "url": f"https://github.com/acme/tool/issues/{200_000 + n}",
            "body": f"Bounty ${amount}", "potential_reward_amount": Decimal(amount),
        })
        for n, amount in enumerate(["5", "50"] * 7, 1)
    ]
    state.save(historical + noise + monetary, [], completed_at=None)
    try:
        started = perf_counter()
        work = state.verification_queue(20, now=NOW)
        print(f"50,515 candidates: selection took {perf_counter() - started:.3f}s")
        assert {c.source_issue_id for c in work[:15]} == {
            c.source_issue_id for c in monetary
        }
        assert len(work) == 20
        assert sum(c.source_issue_id < 100_000 for c in work) == 5
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Render lists $7/month, existing paid spend only.",
    "Default --cap is $10 per role for paid model calls.",
    "The paid Console hosted search ($0.01/search) requires an account.",
    "Amount: $0.010000 USDC. A wallet paid the storefront.",
    "The retail value is €79.99, compared with the amount paid.",
    "The intake was funded on 2026-10-10 (x402 settlement posted "
    "2026-10-10T08:39:57Z, receipt `hfr_d83072e11a6e19f5c7bfc77f36a88a89`,\n"
    "$11.00) and had been assigned bounty_number 137 by 2026-10-12.",
    "The vendor account was funded $11 yesterday; this is its settlement history.",
    "Vendor account funded $11 yesterday",
    "The payment processor settled $50",
    "Paid API usage costs $7 monthly",
    "The product subscription is $10/month",
    "Model inference costs $0.01 per request",
    "Retail value €79.99",
    "An account received $100 in funding",
    "The vendor's previous bounty_number was 137; wallet transferred $25",
    "The previous bounty was $50, already paid.",
    "Payment: $100",
    "We will pay $50 for the subscription",
    "You will receive $100 in account funding",
    "No reward $50 is offered",
    "Bounty $50 was paid yesterday",
    "Previous payment of $100 was processed.",
    "The customer paid $50 for hosting.",
    "The product's retail value is $79.99.",
    "The wallet received a $25 transfer.",
    "The settlement system completed a $100 transaction.",
    "The previous bounty was paid last month.",
    "An investor funded the company with $1000.",
    "A vendor posted $11 as an account deposit.",
    "Bounty $50 was previously paid for this fix",
    "The account reward $50 was a deposit",
    "Retail bounty $79.99",
    "Bounty $50 from last month",
])
async def test_commercial_payments_are_not_advertised_bounty_rewards(text):
    # Search may have stored an old false-positive amount. Fresh detail must
    # replace it and reject delivery, rather than trusting that old extraction.
    result = await Verifier(
        FakeInspector(IssueInspection(
            state="open", title="Improve the application", body=text,
            checked_sources=["issue detail", "issue comments", "issue timeline"],
        )),
        options(notification_mode="include_unverified"), clock=lambda: NOW,
    ).verify(candidate(potential_reward_amount=Decimal("7"), currency="USD"))
    assert reward_mention(text)[0] is None
    assert result.amount is None
    assert not eligible_for_delivery(result, options(notification_mode="include_unverified"), NOW)
    stored = result.model_copy(update={
        "amount": Decimal("7"), "currency": "USD", "notification_eligible": True,
    })
    assert await github_revalidator(
        FakeInspector(IssueInspection(
            state="open", title="Improve the application", body=text,
            checked_sources=["issue detail", "issue comments", "issue timeline"],
        )), stored, now=NOW,
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Bounty $0.01", "Reward $1", "Paid task: $5 USD",
    "Payment for implementing this issue: USD 10",
    "You will be paid $25 for this fix", "We will pay $50 for the implementation",
    "Prize $1000", "Payment $5 for implementing this feature",
    "Bounty: $0.01", "Reward $1 for fixing this issue",
    "Payment $5 for implementing this feature", "We will pay $50 for this fix",
    "You will receive $100 for completing this issue", "Funded bounty: $250",
    "Prize: $1000 for the implementation", "Bounty:\n$1",
    "Bounty: $0.01 for this fix", "Reward $1 for implementing this feature",
    "Payment: $10 for resolving this issue", "We will pay $25 for this bug fix",
    "You will receive $50 after completing this issue",
    "Funded bounty: $100 to implement OAuth", "Prize: $1000 for the accepted solution",
    "[Due for payment 2026-10-16] [$250] Require address in setup",
])
async def test_explicit_reward_offers_remain_supported(text):
    amount, currency, _ = reward_mention(text)
    assert amount is not None and amount > 0 and currency == "USD"
    config = options(notification_mode="include_unverified")
    value = await Verifier(FakeInspector(IssueInspection(
        state="open", title="Fix this application", body=text,
        checked_sources=["issue detail", "issue comments", "issue timeline"],
    )), config, clock=lambda: NOW).verify(candidate())
    assert value.amount == amount
    assert eligible_for_delivery(value, config, NOW)
    assert value.notification_classification == "POTENTIAL_UNVERIFIED"


@pytest.mark.parametrize("text,kind", [
    ("Bounty: $1 for this fix", RewardKind.REWARD_OFFER),
    ("Vendor account funded $11 yesterday", RewardKind.UNRELATED_TRANSACTION),
    ("Amount $11", RewardKind.AMBIGUOUS_MONETARY_REFERENCE),
    ("Please fix this issue", RewardKind.NO_REWARD),
    ("Reward $0", RewardKind.NO_REWARD),
    ("Reward -$1", RewardKind.NO_REWARD),
])
def test_structured_reward_classification(text, kind):
    assessment = assess_reward(text, source="issue_body", source_url=candidate().url)
    assert assessment.classification == kind
    for entry in assessment.evidence:
        assert text[entry.start:entry.end] == entry.text_span
        assert entry.source == "issue_body" and entry.source_url == candidate().url
        assert entry.explanation


def test_quotes_and_code_are_not_offers_but_do_not_hide_later_offers():
    text = '> Previous example: Bounty $100\n\n```json\n{"reward": "$250"}\n```\n\nReward $1'
    assessment = assess_reward(text)
    assert assessment.amount == Decimal("1")
    assert len([e for e in assessment.evidence
                if e.classification == RewardKind.UNRELATED_TRANSACTION]) == 2


@pytest.mark.parametrize("body,expected", [
    ("[Bounty: $5](https://github.com/acme/tool/issues/8)", "5"),
    ("[Bounty: $80](https://github.com/other/tool/issues/37)", None),
    ("Reward $1 for this fix; [Bounty: $80](https://github.com/other/tool/issues/37)", "1"),
    ("Reward $50 for https://github.com/other/tool/issues/37", None),
    ("Reward $5 for this fix; related issue https://github.com/other/tool/issues/37", "5"),
])
def test_reward_is_bound_to_the_task_being_inspected(body, expected):
    assessment = assess_reward(body, source="issue_body", source_url=candidate().url)
    assert assessment.amount == (Decimal(expected) if expected else None)
    for entry in assessment.evidence:
        if entry.classification == RewardKind.REWARD_OFFER:
            assert entry.issue_url == candidate().url


@pytest.mark.parametrize("body", ["Bounty $100 or $50", "Reward USD 5 or EUR 5"])
def test_alternative_reward_amounts_are_ambiguous(body):
    assert reward_mention(body)[0] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("association,edited,eligible", [
    ("OWNER", False, True), ("MEMBER", False, True),
    ("COLLABORATOR", False, True), ("NONE", False, False), ("OWNER", True, False),
])
async def test_comment_reward_requires_credible_current_maintainer_offer(
    respx_mock, association, edited, eligible,
):
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(
        8, title="Repair parser", body="Please fix the parser", comments=1,
        created_at=NOW.isoformat(), labels=[],
    ))
    respx_mock.get(base + "/comments").respond(200, json=[{
        "body": "You will receive $50 after completing this issue",
        "author_association": association, "user": {"login": "maintainer"},
        "created_at": NOW.isoformat(),
        "updated_at": (NOW + timedelta(seconds=int(edited))).isoformat(),
        "html_url": candidate().url + "#issuecomment-1",
    }])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    config = options(notification_mode="include_unverified")
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW, sleep=no_sleep)
        value = await Verifier(inspector, config, clock=lambda: NOW).verify(candidate())
        assert eligible_for_delivery(value, config, NOW) is eligible
        assert await github_revalidator(inspector, value, now=NOW) is eligible
        if eligible:
            entry = next(e for e in value.reward_evidence if e.source == "maintainer_comment")
            assert entry.amount == Decimal("50")
            assert entry.source_timestamp == NOW
            assert entry.author == "maintainer"
            assert entry.text_span == "You will receive $50 after completing this issue"
            assert "compensation" in entry.explanation


@pytest.mark.asyncio
@pytest.mark.parametrize("bodies,expected", [
    (["Vendor account funded $11 yesterday"], False),
    (["Reward $5; deadline: 2020-01-01"], False),
    (["Reward $5", "Reward $100"], None),
    (["[Bounty: $80](https://github.com/other/tool/issues/37)"], False),
    (["[Bounty: $5](https://github.com/acme/tool/issues/8)"], True),
])
async def test_maintainer_history_expiry_and_conflict_are_not_actionable(
    respx_mock, bodies, expected,
):
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(
        8, title="Repair parser", body="Please fix", comments=len(bodies), labels=[],
    ))
    respx_mock.get(base + "/comments").respond(200, json=[{
        "body": body, "author_association": "OWNER", "user": {"login": "maintainer"},
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        "html_url": candidate().url + f"#issuecomment-{n}",
    } for n, body in enumerate(bodies, 1)])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    config = options(notification_mode="include_unverified")
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW, sleep=no_sleep)
        value = await Verifier(inspector, config, clock=lambda: NOW).verify(candidate())
        assert eligible_for_delivery(value, config, NOW) is (expected is True)
        assert await github_revalidator(inspector, value, now=NOW) is expected


@pytest.mark.parametrize("text,expected", [
    ("Bounty $1; the subscription costs $10/month.", "1"),
    ("Vendor account funded $11 yesterday. Reward $0.01 for fixing this issue", "0.01"),
    ("Paid API usage costs $7 monthly. Paid task: $5 USD", "5"),
    ("Reward $50 for fixing this issue; prior payment $11 was settled.", "50"),
    ("Bounty $5 for fixing this issue while the subscription costs $1000 monthly.", "5"),
])
def test_reward_amount_does_not_inherit_unrelated_financial_references(text, expected):
    amount, currency, _ = reward_mention(text)
    assert amount == Decimal(expected)
    assert currency == "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,title,url,issue_id,number,stale_amount", [
    ("frantic-board-521.md",
     "Vendor posting status gives a funded poster no decision, reason, "
     "or expected time in funded_pending_review",
     "https://github.com/auscaster/frantic-board/issues/521", 5906661784, 521, "11.00"),
    ("bountyscout-1611.md", "Bounty Alert: 14 New Opportunityies found",
     "https://github.com/dev-kp-eloper/BountyScout/issues/1611", 5906508355, 1611, "80"),
])
async def test_sent_false_positive_fixtures_never_reach_smtp(
    tmp_path, respx_mock, fixture, title, url, issue_id, number, stale_amount,
):
    # The vendor fixture is the exact 4,000-character saved search body;
    # the roundup fixture preserves its original reward-bearing excerpt.
    body = (Path(__file__).parent / "fixtures" / fixture).read_text().rstrip("\n")
    if fixture == "frantic-board-521.md":
        assert len(body) == 4000
    config = options(notification_mode="include_unverified", email_enabled=True,
                     smtp_username="sender@example.com", smtp_password="offline",
                     email_to="inbox@example.com")
    raw = issue(number, id=issue_id, title=title, body=body, html_url=url,
                labels=[], comments=0)
    base = url.replace("https://github.com/", "https://api.github.com/repos/")
    respx_mock.get(base).respond(200, json=raw)
    respx_mock.get(base + "/timeline").respond(200, json=[])
    async with httpx.AsyncClient() as client:
        discovered = GitHubDiscovery(client, config, clock=lambda: NOW)._candidate(raw)
        assert discovered.potential_reward_amount is None
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW, sleep=no_sleep)
        old = discovered.model_copy(update={
            "potential_reward_amount": Decimal(stale_amount), "currency": "USD",
        })
        verified = await Verifier(inspector, config, clock=lambda: NOW).verify(old)
        assert verified.amount is None
        assert not eligible_for_delivery(verified, config, NOW)
        # Reproduce a cached, previously eligible result independently of the
        # corrected verifier, then exercise the actual delivery orchestrator.
        cached = await Verifier(FakeInspector(IssueInspection(
            state="open", title=title, body=f"Bounty ${stale_amount}",
            checked_sources=["issue detail", "issue comments", "issue timeline"],
        )), config, clock=lambda: NOW).verify(old)
        assert eligible_for_delivery(cached, config, NOW)
        cached = cached.model_copy(update={"reward_evidence": []})
        assert await github_revalidator(inspector, cached, now=NOW) is False

        class ForbiddenTransport:
            async def send(self, _message):
                pytest.fail("The incident fixture must never invoke SMTP")

        store = NotificationOutbox(tmp_path / "incident.db", config, clock=lambda: NOW)
        try:
            assert store.enqueue(verified) is None
            assert store.enqueue(cached)

            async def revalidate(result):
                return await github_revalidator(inspector, result, now=NOW)

            summary = await send_pending(store, ForbiddenTransport(), config, revalidate)
            assert summary.cancelled == 1 and summary.sent == 0
            assert store.db.execute(
                "SELECT count(*) FROM notification_delivery_attempts",
            ).fetchone()[0] == 0
        finally:
            store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["changed_count", "short_page"])
async def test_unstable_window_splits_durably_without_false_completion(
    tmp_path, respx_mock, fault,
):
    state = RunState(tmp_path / "churn.db")
    start = NOW - timedelta(hours=2)
    state.save([], [], completed_at=start)
    state.save([], [], completed_at=None, progress=SearchProgress(
        covered_until=NOW,
        windows=[SearchWindow(
            category="updated", term="bounty", field="updated", start=start,
            end=NOW, page=2 if fault == "changed_count" else 1,
            total=101 if fault == "changed_count" else None,
        )],
    ))
    response = payload([issue(3)], total=102 if fault == "changed_count" else 2)
    respx_mock.get(API_URL).respond(200, json=response)
    config = options(
        search_terms=["bounty"], search_labels=[], monetary_terms=[],
        github_max_search_requests_per_scan=1,
    )
    async with httpx.AsyncClient() as client:
        scan = await GitHubDiscovery(
            client, config, progress=state, clock=lambda: NOW, sleep=no_sleep,
        ).scan()
    assert not scan.complete
    assert scan.requests == 1
    assert state.last_complete_at() == start
    pending = state.search_progress()
    assert len(pending.windows) == 2
    assert pending.windows[0].start == start
    assert pending.windows[-1].end == NOW
    assert pending.windows[0].end + timedelta(seconds=1) == pending.windows[1].start
    assert all(w.page == 1 and w.total is None for w in pending.windows)
    assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
    # Resume both children, using the same persisted cursor after interruption.
    respx_mock.get(API_URL).respond(200, json=payload())
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            await GitHubDiscovery(
                client, config, progress=state, clock=lambda: NOW, sleep=no_sleep,
            ).scan()
    assert state.search_progress().windows == []
    assert state.db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
    state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["changed_count", "short_page"])
async def test_indivisible_unstable_window_is_retained(tmp_path, respx_mock, fault):
    state = RunState(tmp_path / "indivisible.db")
    start = NOW - timedelta(seconds=1)
    state.save([], [], completed_at=start)
    state.save([], [], completed_at=None, progress=SearchProgress(
        covered_until=NOW, windows=[SearchWindow(
            category="updated", term="bounty", field="updated", start=start, end=NOW,
            page=2 if fault == "changed_count" else 1,
            total=101 if fault == "changed_count" else None,
        )],
    ))
    respx_mock.get(API_URL).respond(
        200, json=payload([issue(3)], total=102 if fault == "changed_count" else 2),
    )
    async with httpx.AsyncClient() as client:
        scan = await GitHubDiscovery(
            client, options(search_terms=["bounty"], search_labels=[], monetary_terms=[],
                            github_max_search_requests_per_scan=1),
            progress=state, clock=lambda: NOW, sleep=no_sleep,
        ).scan()
    assert not scan.complete
    assert state.last_complete_at() == start
    assert len(state.search_progress().windows) == 1
    assert state.search_progress().windows[0].start == start
    assert state.search_progress().windows[0].end == NOW
    state.close()
