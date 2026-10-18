"""All-positive prices and explicitly unverified alerts, with offline external IO."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_discovery import NOW, issue, no_sleep, payload
from test_email import result
from test_verification import FakeFunding, FakeInspector, candidate, funded_record

from bountyradar import cli
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.notification import eligible_for_delivery, opportunity_message
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunState
from bountyradar.settings import Settings
from bountyradar.verification import Verifier
from bountyradar.verification_models import Category, Evidence, FundingRecord

AMOUNTS = ["1", "5", "10", "15", "20", "25", "50", "100", "500", "1000", "0.001"]


def options(**changes):
    return Settings(_env_file=None, **(dict(
        min_reward_usd=0, email_enabled=False, github_token="offline-token",
        github_min_request_interval_seconds=0,
        trusted_funding_hosts=["funding.example"],
        trusted_funding_providers=["trusted_api"],
    ) | changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", AMOUNTS)
@pytest.mark.parametrize("funded", [True, False])
async def test_every_positive_amount_extracts_verifies_and_queues(tmp_path, amount, funded):
    settings = options(notification_mode="include_unverified")
    async with httpx.AsyncClient() as client:
        c = GitHubDiscovery(client, settings, clock=lambda: NOW)._candidate(
            issue(body=f"Bounty reward ${amount}")
        )
    assert c.potential_reward_amount == Decimal(amount)
    provider = FakeFunding([funded_record(amount=Decimal(amount))]) if funded else None
    # The funding fixture must refer to the exact discovered issue.
    c = c.model_copy(update={"source_issue_id":8, "issue_number":8,
                             "url":"https://github.com/acme/tool/issues/8"})
    r = await Verifier(FakeInspector(), settings, provider, clock=lambda: NOW).verify(c)
    assert eligible_for_delivery(r, settings, NOW)
    assert r.notification_classification == (
        "CONFIRMED_FUNDED" if funded else "POTENTIAL_UNVERIFIED"
    )
    store = NotificationOutbox(tmp_path / "prices.db", settings, clock=lambda: NOW)
    try:
        assert store.enqueue(r)
        assert store.enqueue(r) is None
        assert store.db.execute("select count(*) from notification_outbox").fetchone()[0] == 1
        text = opportunity_message(r, settings, "new_qualified_bounty").as_string()
        assert r.notification_classification in text
        if not funded:
            assert "UNVERIFIED" in text
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["0", "-1", None])
async def test_nonpositive_or_unknown_price_never_emails(amount):
    c = candidate(potential_reward_amount=Decimal(amount) if amount else None)
    s = options(notification_mode="include_unverified")
    r = await Verifier(FakeInspector(), s, clock=lambda: NOW).verify(c)
    assert not eligible_for_delivery(r, s, NOW)


@pytest.mark.asyncio
async def test_explicit_floor_and_unverified_opt_in():
    c = candidate(potential_reward_amount=Decimal("5"), body="Bounty $5")
    for s in [options(), options(min_reward_usd=10, notification_mode="include_unverified")]:
        r = await Verifier(FakeInspector(), s, clock=lambda: NOW).verify(c)
        assert not eligible_for_delivery(r, s, NOW)
    s = options(notification_mode="include_unverified")
    r = await Verifier(FakeInspector(), s, clock=lambda: NOW).verify(c)
    assert eligible_for_delivery(r, s, NOW)
    r.missing_sources.append("issue timeline unavailable")
    assert not eligible_for_delivery(r, s, NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("mention,currency,amount", [
    ("EUR 5", "EUR", "5"), ("€0.01", "EUR", "0.01"),
    ("£10", "GBP", "10"), ("INR 100", "INR", "100"),
    ("5 CAD", "CAD", "5"), ("BTC 0.001", "BTC", "0.001"),
    ("CAD $5", "CAD", "5"), ("$5 CAD", "CAD", "5"),
])
async def test_original_currency_is_preserved(mention, currency, amount):
    async with httpx.AsyncClient() as client:
        c = GitHubDiscovery(client, options())._candidate(issue(body=f"Reward {mention}"))
    assert c.currency == currency
    assert c.potential_reward_amount == Decimal(amount)
    assert c.currency != "USD"


@pytest.mark.asyncio
@pytest.mark.parametrize("mention", ["-$5 USD", "$-5 USD", "-5 USD"])
async def test_negative_mentions_do_not_become_positive(mention):
    async with httpx.AsyncClient() as client:
        c = GitHubDiscovery(client, options())._candidate(issue(body=f"Reward {mention}"))
    assert c.potential_reward_amount is None


@pytest.mark.asyncio
async def test_foreign_currency_can_opt_into_all_positive_floor():
    s = options(allowed_currencies=["EUR"], currency_minimums={"EUR": Decimal("0")})
    r = await Verifier(FakeInspector(), s, FakeFunding([
        funded_record(amount=Decimal("0.01"), currency="EUR")]), clock=lambda: NOW).verify(
            candidate())
    assert r.amount == Decimal("0.01") and r.currency == "EUR"
    assert eligible_for_delivery(r, s, NOW)


def test_platform_listing_remains_unverified_and_funding_upgrade_is_new_event(tmp_path):
    s = options(notification_mode="include_unverified")
    r = result(amount="5", category=Category.REWARD_CLAIMED)
    r.evidence = [Evidence(kind="platform_listing", source="fixture", url="https://example.com",
                           observed_at=NOW, detail="Unverified fixture listing")]
    assert r.notification_classification == "PLATFORM_LISTED_UNVERIFIED"
    store = NotificationOutbox(tmp_path / "upgrade.db", s, clock=lambda: NOW)
    try:
        assert store.enqueue(r)
        assert store.enqueue(r) is None
        assert store.enqueue(result(amount="5"))
        reasons = [x[0] for x in store.db.execute("select reason from notification_outbox")]
        assert reasons == ["new_qualified_bounty", "funding_verified"]
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("funded", [True, False])
async def test_four_five_minute_intervals_source_to_mock_smtp(tmp_path, monkeypatch,
                                                            respx_mock, funded):
    clock = [NOW]
    amounts = {}
    messages = []
    s = options(database_path=tmp_path / "intervals.db", search_terms=["bounty"],
                search_labels=[], monetary_terms=[], notification_mode="include_unverified",
                email_enabled=True, smtp_username="sender@example.com",
                smtp_password="fixture", email_to="sender@example.com")

    def item(n):
        return issue(n, body=f"Bounty reward ${amounts[n]}", comments=0,
                     updated_at=clock[0].isoformat())

    respx_mock.get(API_URL).mock(side_effect=lambda _: httpx.Response(
        200, json=payload([item(n) for n in amounts])))

    def inspect(request):
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=item(int(request.url.path.rsplit("/", 1)[1])))

    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=inspect)
    monkeypatch.setattr(cli, "GitHubDiscovery", lambda client, settings, **kw:
                        GitHubDiscovery(client, settings, **kw, sleep=no_sleep))

    class Provider:
        async def records_for(self, c):
            return [FundingRecord(provider="trusted_api", issue_url=c.url,
                                  evidence_url=f"https://funding.example/{c.issue_number}",
                                  observed_at=clock[0], status="funded",
                                  amount=amounts[c.issue_number], currency="USD")]

    class Transport:
        async def send(self, message):
            messages.append(message)

    for minute, n, amount in [(0, 1, "5"), (5, 2, "10"), (10, 3, "100"), (15, 2, "20")]:
        clock[0] = NOW + timedelta(minutes=minute)
        amounts[n] = Decimal(amount)
        summary = await cli._run(s, False, Provider() if funded else None, Transport(),
                                 clock=lambda: clock[0])
        assert summary[3] == 1
        assert summary[4:] == (0, 0)
    assert len(messages) == 4
    assert (await cli._run(s, False, Provider() if funded else None, Transport(),
                           clock=lambda: clock[0]))[3] == 0
    assert len(messages) == 4
    expected = "CONFIRMED_FUNDED" if funded else "POTENTIAL_UNVERIFIED"
    assert all(expected in str(m["Subject"]) for m in messages)
    state = RunState(s.database_path)
    try:
        assert state.db.execute("select count(*) from discovered_issues").fetchone()[0] == 3
        assert state.db.execute("select count(*) from notification_outbox").fetchone()[0] == 4
        assert state.last_complete_at() == clock[0]
        assert state.db.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        state.close()
