"""Offline evidence classification, availability and ranking contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from bountyradar.github_inspector import API, GitHubIssueInspector
from bountyradar.models import BountyCandidate
from bountyradar.settings import Settings
from bountyradar.verification import Verifier
from bountyradar.verification_models import Availability, Category, FundingRecord, IssueInspection

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def candidate(**changes):
    data = dict(
        source_issue_id=8,
        repository="acme/tool",
        issue_number=8,
        title="Bounty: fix parser",
        url="https://github.com/acme/tool/issues/8",
        body="Reward $100",
        created_at=NOW,
        updated_at=NOW,
        discovered_at=NOW,
        potential_reward_amount=Decimal("100"),
        currency="USD",
    )
    data.update(changes)
    return BountyCandidate(**data)


class FakeInspector:
    def __init__(self, inspection=None):
        self.inspection = inspection or IssueInspection(
            state="open", checked_sources=["issue detail", "issue comments", "issue timeline"]
        )
        self.calls = 0

    async def inspect(self, _candidate):
        self.calls += 1
        return self.inspection.model_copy(deep=True)


class FakeFunding:
    def __init__(self, records):
        self.records = records

    async def records_for(self, _candidate):
        return self.records


def funded_record(**changes):
    data = dict(
        provider="trusted_api",
        issue_url=candidate().url,
        evidence_url="https://funding.example/records/8",
        observed_at=NOW,
        status="funded",
        amount=Decimal("100"),
        currency="USD",
    )
    data.update(changes)
    return FundingRecord(**data)


def settings(**changes):
    defaults = dict(
        trusted_funding_hosts=["funding.example"], trusted_funding_providers=["trusted_api"]
    )
    return Settings(_env_file=None, **(defaults | changes))


@pytest.mark.asyncio
async def test_funded_evidence_and_high_priority_gate():
    result = await Verifier(
        FakeInspector(), settings(), FakeFunding([funded_record()]), clock=lambda: NOW
    ).verify(candidate())
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.availability == Availability.AVAILABLE
    assert result.high_priority_email_eligible
    assert 0 <= result.score <= 100
    assert any(
        item.url == "https://funding.example/records/8" and item.observed_at == NOW
        for item in result.evidence
    )
    assert result.competition_assessment == "not found in checked sources"


@pytest.mark.asyncio
async def test_authoritative_funding_can_exceed_stale_provisional_amount():
    stale = candidate(body="Bounty $20", potential_reward_amount=Decimal("20"))
    inspector = FakeInspector()
    result = await Verifier(
        inspector,
        settings(),
        FakeFunding([funded_record(amount=Decimal("100"))]),
        clock=lambda: NOW,
    ).verify(stale)
    assert inspector.calls == 1
    assert result.amount == Decimal("100")
    assert result.high_priority_email_eligible


@pytest.mark.asyncio
async def test_independent_funding_records_are_preserved_without_summing():
    records = [
        funded_record(funding_id="award-a", amount=Decimal("60")),
        funded_record(
            funding_id="award-b",
            evidence_url="https://funding.example/records/9",
            amount=Decimal("75"),
        ),
    ]
    result = await Verifier(
        FakeInspector(), settings(), FakeFunding(records), clock=lambda: NOW
    ).verify(candidate())
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.amount == Decimal("75")
    assert len([e for e in result.evidence if e.kind == "funding_funded"]) == 2


@pytest.mark.asyncio
async def test_latest_status_applies_per_funding_record():
    records = [
        funded_record(funding_id="award-a", amount=Decimal("100")),
        funded_record(
            funding_id="award-a", status="paid", amount=Decimal("100"),
            observed_at=NOW + timedelta(seconds=1),
        ),
        funded_record(
            funding_id="award-b",
            evidence_url="https://funding.example/records/9",
            amount=Decimal("50"),
        ),
    ]
    result = await Verifier(
        FakeInspector(), settings(), FakeFunding(records),
        clock=lambda: NOW + timedelta(seconds=1),
    ).verify(candidate())
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.amount == Decimal("50")


@pytest.mark.asyncio
async def test_issue_claim_cannot_create_high_confidence_alert():
    for provider in (
        None,
        FakeFunding([]),
        FakeFunding([funded_record(evidence_url="https://untrusted.example/8")]),
    ):
        result = await Verifier(FakeInspector(), settings(), provider, clock=lambda: NOW).verify(
            candidate()
        )
        assert result.category == Category.REWARD_CLAIMED
        assert not result.notification_eligible
        assert not result.high_priority_email_eligible


@pytest.mark.asyncio
async def test_fresh_issue_detail_supersedes_stale_search_body():
    stale = candidate(title="Bounty was cancelled", body="Bounty cancelled; reward $100")
    fresh = IssueInspection(
        state="open", title="Bounty: fix parser", body="Reward $100",
        checked_sources=["issue detail", "issue comments", "issue timeline"],
    )
    result = await Verifier(
        FakeInspector(fresh), settings(), FakeFunding([funded_record()]), clock=lambda: NOW
    ).verify(stale)
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.availability == Availability.AVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inspection,expected_category,expected_availability",
    [
        (
            IssueInspection(state="open", assignees=["dev"]),
            Category.FUNDED_EVIDENCE,
            Availability.ASSIGNED,
        ),
        (IssueInspection(state="closed"), Category.UNAVAILABLE, Availability.CLOSED),
        (
            IssueInspection(state="open", terminal_reason="bounty cancelled"),
            Category.UNAVAILABLE,
            Availability.UNAVAILABLE,
        ),
        (
            IssueInspection(state="open", competing_work_found=True),
            Category.FUNDED_EVIDENCE,
            Availability.CONTESTED,
        ),
        (
            IssueInspection(state="open", missing_sources=["timeline: HTTP 403"]),
            Category.FUNDED_EVIDENCE,
            Availability.UNKNOWN,
        ),
    ],
)
async def test_unavailable_and_contested_are_never_alerted(
    inspection, expected_category, expected_availability
):
    result = await Verifier(
        FakeInspector(inspection), settings(), FakeFunding([funded_record()]), clock=lambda: NOW
    ).verify(candidate())
    assert result.category == expected_category
    assert result.availability == expected_availability
    assert not result.high_priority_email_eligible


@pytest.mark.asyncio
async def test_newly_created_bounty_already_assigned_is_not_alerted():
    fresh = candidate(created_at=NOW, updated_at=NOW, assignees=["developer"])
    result = await Verifier(
        FakeInspector(IssueInspection(state="open", assignees=["developer"])),
        settings(),
        FakeFunding([funded_record()]),
        clock=lambda: NOW,
    ).verify(fresh)
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.availability == Availability.ASSIGNED
    assert not result.high_priority_email_eligible


@pytest.mark.asyncio
async def test_false_positive_and_unknown_currency_visible():
    false = candidate(
        title="Reward discussion",
        body="API costs $25 per month",
        potential_reward_amount=None,
        currency=None,
    )
    result = await Verifier(FakeInspector(), settings(), clock=lambda: NOW).verify(false)
    assert result.category == Category.NOT_BOUNTY
    assert not result.notification_eligible
    unknown = candidate(
        title="Bounty for parser",
        body="Reward 50 units",
        potential_reward_amount=Decimal("50"),
        currency=None,
    )
    result = await Verifier(FakeInspector(), settings(), clock=lambda: NOW).verify(unknown)
    assert result.category == Category.REWARD_CLAIMED
    assert result.currency is None
    assert not result.notification_eligible


@pytest.mark.asyncio
async def test_filters_and_scan_bound():
    options = settings(
        preferred_repositories=["elsewhere/repo"],
        preferred_languages=["Rust"],
        earliest_updated_at=datetime(2026, 10, 18, tzinfo=UTC),
        max_verifications_per_scan=1,
    )
    inspector = FakeInspector()
    verifier = Verifier(inspector, options, FakeFunding([funded_record()]), clock=lambda: NOW)
    results = await verifier.verify_many(
        [
            candidate(),
            candidate(
                source_issue_id=9, issue_number=9, url="https://github.com/acme/tool/issues/9"
            ),
        ]
    )
    assert inspector.calls == 1
    assert len(results) == 2
    assert any("repository not preferred" in item.filter_reasons for item in results)
    assert all(not item.notification_eligible for item in results)
    assert any(
        "verification deferred" in item.missing_sources[0]
        for item in results
        if item.missing_sources
    )


async def no_sleep(_seconds):
    return None


@pytest.mark.asyncio
@respx.mock
async def test_github_inspection_claim_cancel_and_relevant_pr():
    base = f"{API}/repos/acme/tool/issues/8"
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "assignees": [],
                "body": "Contributors must sign the CLA",
                "title": "Bounty: fix parser",
                "labels": [],
                "comments": 2,
            },
        )
    )
    respx.get(f"{base}/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "body": "I am working on this",
                    "html_url": candidate().url + "#issuecomment-1",
                    "created_at": "2026-10-17T10:00:00Z",
                },
                {
                    "body": "Bounty cancelled",
                    "html_url": candidate().url + "#issuecomment-2",
                    "created_at": "2026-10-17T11:00:00Z",
                },
            ],
        )
    )
    respx.get(f"{base}/timeline").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "pull_request": {},
                            "html_url": "https://github.com/acme/tool/pull/12",
                        }
                    },
                },
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "pull_request": {},
                            "html_url": "https://github.com/else/thing/pull/7",
                        }
                    },
                },
            ],
        )
    )
    respx.get(f"{API}/repos/acme/tool/pulls/12").mock(
        return_value=httpx.Response(
            200, json={"state": "open", "title": "Fixes #8", "body": "parser change"}
        )
    )
    respx.get(f"{API}/repos/else/thing/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"state": "open", "title": "Fixes #8", "body": "unrelated repository issue"}
        )
    )
    async with httpx.AsyncClient() as client:
        inspected = await GitHubIssueInspector(
            client,
            settings(github_min_request_interval_seconds=0),
            clock=lambda: NOW,
            sleep=no_sleep,
        ).inspect(candidate())
    assert inspected.competing_work_found
    assert inspected.terminal_reason == "Bounty cancelled"
    assert len(inspected.linked_prs) == 1
    assert inspected.linked_prs == ["https://github.com/acme/tool/pull/12"]
    assert len(inspected.referenced_prs) == 2
    assert len([item for item in inspected.evidence if item.kind == "active_linked_pr"]) == 1
    assert "Contributors must sign the CLA" in inspected.restrictions
    assert inspected.missing_sources == []


@pytest.mark.asyncio
@respx.mock
async def test_inspection_missing_timeline_and_etag_cache():
    base = f"{API}/repos/acme/tool/issues/8"
    route = respx.get(base).mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={"state": "open", "title": "Bounty: fix parser", "labels": [],
                      "assignees": [], "comments": 0},
            ),
            httpx.Response(304),
        ]
    )
    respx.get(f"{base}/timeline").mock(return_value=httpx.Response(403))
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(
            client,
            settings(github_min_request_interval_seconds=0),
            clock=lambda: NOW,
            sleep=no_sleep,
        )
        first = await inspector.inspect(candidate())
        second = await inspector.inspect(candidate())
    assert route.call_count == 2
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'
    assert first.missing_sources == second.missing_sources == ["issue timeline: HTTP 403"]


@pytest.mark.asyncio
async def test_paid_record_and_untrusted_claim_never_queue():
    from bountyradar.verification import high_priority_queue

    verifier = Verifier(
        FakeInspector(), settings(), FakeFunding([funded_record(status="paid")]), clock=lambda: NOW
    )
    paid = await verifier.verify(candidate())
    assert paid.category == Category.UNAVAILABLE
    assert paid.availability == Availability.UNAVAILABLE
    assert any(item.kind == "funding_paid" for item in paid.evidence)
    claimed = await Verifier(
        FakeInspector(),
        settings(notification_categories=["FUNDED_EVIDENCE", "REWARD_CLAIMED"]),
        clock=lambda: NOW,
    ).verify(candidate())
    assert claimed.notification_eligible
    assert not claimed.high_priority_email_eligible
    assert high_priority_queue([paid, claimed]) == []


@pytest.mark.asyncio
async def test_foreign_currency_is_preserved_without_conversion():
    euro = funded_record(amount=Decimal("50"), currency="EUR")
    result = await Verifier(
        FakeInspector(), settings(), FakeFunding([euro]), clock=lambda: NOW
    ).verify(candidate())
    assert result.category == Category.FUNDED_EVIDENCE
    assert result.amount == Decimal("50")
    assert result.currency == "EUR"
    assert not result.notification_eligible
    assert "currency not allowed or unknown" in result.filter_reasons


@pytest.mark.asyncio
async def test_non_usd_requires_own_threshold_and_custom_weights():
    euro = funded_record(amount=Decimal("40"), currency="EUR")
    chosen = settings(
        allowed_currencies=["EUR"],
        currency_minimums={"EUR": Decimal("30")},
        score_weights={
            "funding": 50,
            "freshness": 10,
            "availability": 20,
            "competition": 10,
            "reward": 5,
            "technology": 5,
        },
    )
    result = await Verifier(FakeInspector(), chosen, FakeFunding([euro]), clock=lambda: NOW).verify(
        candidate()
    )
    assert result.notification_eligible
    assert result.amount == Decimal("40")
    assert result.currency == "EUR"
    assert 0 <= result.score <= 100
    without_threshold = settings(allowed_currencies=["EUR"])
    result = await Verifier(
        FakeInspector(), without_threshold, FakeFunding([euro]), clock=lambda: NOW
    ).verify(candidate())
    assert not result.notification_eligible
    assert "currency threshold not configured or unknown" in result.filter_reasons


def test_invalid_score_weights_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(score_weights={"funding": 100})


@pytest.mark.asyncio
async def test_stale_or_future_funding_never_becomes_high_confidence():
    from datetime import timedelta

    for observed_at in (NOW - timedelta(days=3), NOW + timedelta(hours=1)):
        result = await Verifier(
            FakeInspector(),
            settings(),
            FakeFunding([funded_record(observed_at=observed_at)]),
            clock=lambda: NOW,
        ).verify(candidate())
        assert result.category == Category.REWARD_CLAIMED
        assert not result.high_priority_email_eligible
        assert any(item.kind == "historical_funding_funded" for item in result.evidence)


@pytest.mark.asyncio
async def test_explicit_past_deadline_unavailable():
    result = await Verifier(
        FakeInspector(), settings(), FakeFunding([funded_record()]), clock=lambda: NOW
    ).verify(candidate(body="Reward $100. Deadline: 2026-10-16"))
    assert result.category == Category.UNAVAILABLE
    assert result.availability == Availability.UNAVAILABLE
    assert any(item.kind == "expired_deadline" for item in result.evidence)
    assert not result.high_priority_email_eligible


@pytest.mark.asyncio
@respx.mock
async def test_unrelated_cross_reference_does_not_block_funded_issue():
    base = f"{API}/repos/acme/tool/issues/8"
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "assignees": [],
                "body": "Reward $100",
                "title": "Bounty: fix parser",
                "labels": [],
                "comments": 0,
            },
        )
    )
    respx.get(f"{base}/timeline").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "pull_request": {},
                            "html_url": "https://github.com/else/thing/pull/7",
                        }
                    },
                }
            ],
        )
    )
    respx.get(f"{API}/repos/else/thing/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"state": "open", "title": "Fixes #8", "body": "unrelated repository issue"}
        )
    )
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(
            client,
            settings(github_min_request_interval_seconds=0),
            clock=lambda: NOW,
            sleep=no_sleep,
        )
        result = await Verifier(
            inspector, settings(), FakeFunding([funded_record()]), clock=lambda: NOW
        ).verify(candidate())
    assert result.referenced_prs == ["https://github.com/else/thing/pull/7"]
    assert result.linked_prs == []
    assert not result.competing_work_found
    assert result.high_priority_email_eligible
