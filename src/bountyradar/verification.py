"""Evidence-based bounty classification and candidate verification orchestration."""

import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol
from urllib.parse import urlparse

from bountyradar.models import BountyCandidate
from bountyradar.ranking import rank
from bountyradar.settings import Settings
from bountyradar.verification_models import (
    Availability,
    Category,
    Evidence,
    FundingRecord,
    IssueInspection,
    VerificationResult,
)

CLAIM = re.compile(
    r"\b(?:bounty|paid issue|payment for (?:this|the) issue"
    r"|reward\s+(?:of|is|:|\$|USD)|payout\s+(?:of|is|:|\$|USD))\b",
    re.I,
)
CANCELLED = re.compile(
    r"\b(?:bounty|reward)\s+(?:is\s+)?(?:cancelled|canceled|expired|paid out|paid)\b"
    r"|\b(?:cancelled|canceled|expired|paid out|paid)\s+(?:bounty|reward)\b",
    re.I,
)
EXPIRY_DATE = re.compile(r"\b(?:expires?\s+(?:on\s+)?|deadline\s*:?\s*)(\d{4}-\d{2}-\d{2})\b", re.I)
RESTRICTION = re.compile(
    r"(?im)^.*\b(?:eligib(?:le|ility)|contributors? must|residents? only"
    r"|first.time contributors? only)\b.*$"
)


class IssueInspector(Protocol):
    async def inspect(self, candidate: BountyCandidate) -> IssueInspection: ...


class FundingProvider(Protocol):
    async def records_for(self, candidate: BountyCandidate) -> Sequence[FundingRecord]: ...


class Verifier:
    def __init__(
        self,
        inspector: IssueInspector,
        settings: Settings,
        funding_provider: FundingProvider | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.inspector = inspector
        self.settings = settings
        self.funding_provider = funding_provider
        self.clock = clock

    async def verify(self, candidate: BountyCandidate) -> VerificationResult:
        now = self.clock().astimezone(UTC)
        evidence = [
            Evidence(
                kind="candidate",
                source="github_search",
                url=candidate.url,
                observed_at=now,
                detail="Search candidate; reward unverified",
            )
        ]
        threshold = (
            self.settings.min_reward_usd
            if candidate.currency == "USD"
            else self.settings.currency_minimums.get(candidate.currency or "")
        )
        if (
            candidate.potential_reward_amount is not None
            and threshold is not None
            and candidate.potential_reward_amount < threshold
            and self.funding_provider is None
        ):
            inspection = IssueInspection(
                state=candidate.state, missing_sources=["detail skipped below threshold"]
            )
        else:
            inspection = await self.inspector.inspect(candidate)
        evidence.extend(inspection.evidence)
        records: Sequence[FundingRecord] = []
        if self.funding_provider and not inspection.missing_sources == [
            "detail skipped below threshold"
        ]:
            try:
                records = await self.funding_provider.records_for(candidate)
                inspection.checked_sources.append("funding provider")
            except Exception:
                inspection.missing_sources.append("funding provider unavailable")
        valid = [record for record in records if self._authoritative(record, candidate)]
        for record in records:
            if record not in valid:
                evidence.append(
                    Evidence(
                        kind="untrusted_funding_claim",
                        source=record.provider,
                        url=record.evidence_url,
                        issue_url=record.issue_url,
                        observed_at=record.observed_at,
                        detail="Funding record did not meet configured trust and issue-link checks",
                    )
                )
        latest_records: dict[tuple[str, str], FundingRecord] = {}
        for record in valid:
            key = (record.provider, record.funding_id or record.evidence_url)
            previous = latest_records.get(key)
            if previous is None or record.observed_at > previous.observed_at:
                latest_records[key] = record
        current = [
            record
            for record in latest_records.values()
            if 0
            <= (now - record.observed_at).total_seconds()
            <= self.settings.funding_evidence_max_age_hours * 3600
        ]
        # Separate awards are never summed without a provider's explicit aggregate.
        funded = max(
            (record for record in current if record.status == "funded"),
            key=lambda record: record.amount,
            default=None,
        )
        paid = funded is None and any(
            record.status in ("paid", "cancelled", "expired")
            for record in latest_records.values()
        )
        for record in valid:
            evidence.append(
                Evidence(
                    kind=(
                        f"funding_{record.status}"
                        if record in current
                        else f"historical_funding_{record.status}"
                    ),
                    source=record.provider,
                    url=record.evidence_url,
                    issue_url=record.issue_url,
                    observed_at=record.observed_at,
                    detail=f"{record.amount} {record.currency}",
                )
            )
        # A fresh issue detail may have removed an old bounty or terminal claim.
        # Keep search text only when the injected inspector supplied no title.
        issue_title = inspection.title if inspection.title is not None else candidate.title
        issue_body = inspection.body if inspection.title is not None else candidate.body or ""
        text = " ".join((issue_title, issue_body, *inspection.labels))
        if CLAIM.search(text):
            evidence.append(
                Evidence(
                    kind="reward_claim",
                    source="github_issue",
                    url=candidate.url,
                    observed_at=now,
                    detail="Issue text or label claims a reward",
                )
            )
        if CANCELLED.search(text):
            evidence.append(
                Evidence(
                    kind="terminal_claim",
                    source="github_issue",
                    url=candidate.url,
                    observed_at=now,
                    detail="Issue text indicates cancellation, expiry or payout",
                )
            )
        expired_date = None
        for match in EXPIRY_DATE.finditer(text):
            try:
                listed_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if listed_date < now.date():
                expired_date = listed_date
                evidence.append(
                    Evidence(
                        kind="expired_deadline",
                        source="github_issue",
                        url=candidate.url,
                        observed_at=now,
                        detail=f"Explicit deadline passed: {listed_date.isoformat()}",
                    )
                )
                break
        unavailable = (
            inspection.state != "open"
            or paid
            or bool(inspection.terminal_reason)
            or bool(CANCELLED.search(text))
            or expired_date is not None
        )
        if unavailable:
            category = Category.UNAVAILABLE
        elif funded:
            category = Category.FUNDED_EVIDENCE
        elif CLAIM.search(text):
            category = Category.REWARD_CLAIMED
        elif candidate.potential_reward_amount is not None:
            category = Category.UNCERTAIN
        else:
            category = Category.NOT_BOUNTY if not inspection.missing_sources else Category.UNCERTAIN
        if inspection.state != "open":
            availability = (
                Availability.CLOSED if inspection.state == "closed" else Availability.UNKNOWN
            )
        elif (
            paid or inspection.terminal_reason or CANCELLED.search(text) or expired_date is not None
        ):
            availability = Availability.UNAVAILABLE
        elif inspection.assignees:
            availability = Availability.ASSIGNED
        elif inspection.competing_work_found:
            availability = Availability.CONTESTED
        elif inspection.missing_sources:
            availability = Availability.UNKNOWN
        else:
            availability = Availability.AVAILABLE
        amount = funded.amount if funded else candidate.potential_reward_amount
        currency = funded.currency if funded else candidate.currency
        restrictions = list(dict.fromkeys(inspection.restrictions + RESTRICTION.findall(text)))
        for restriction in RESTRICTION.findall(text):
            evidence.append(
                Evidence(
                    kind="contribution_restriction",
                    source="github_issue",
                    url=candidate.url,
                    observed_at=now,
                    detail=restriction[:300],
                )
            )
        if valid and not current and not paid:
            inspection.missing_sources.append("funding evidence stale")
        if self.funding_provider is None:
            inspection.missing_sources.append("authoritative funding provider not configured")
        result = VerificationResult(
            candidate=candidate,
            category=category,
            availability=availability,
            amount=amount,
            currency=currency,
            evidence=evidence,
            linked_prs=inspection.linked_prs,
            referenced_prs=inspection.referenced_prs,
            assignees=inspection.assignees,
            competing_work_found=inspection.competing_work_found,
            competition_assessment="competing work found"
            if inspection.competing_work_found
            else "not found in checked sources",
            restrictions=restrictions,
            checked_sources=inspection.checked_sources,
            missing_sources=inspection.missing_sources,
            checked_at=now,
            score=0,
        )
        return rank(result, self.settings, now)

    def _authoritative(self, record: FundingRecord, candidate: BountyCandidate) -> bool:
        host = urlparse(record.evidence_url).hostname
        return (
            record.issue_url == candidate.url
            and record.status in ("funded", "paid", "cancelled", "expired")
            and urlparse(record.evidence_url).scheme == "https"
            and bool(host)
            and host in self.settings.trusted_funding_hosts
            and record.provider in self.settings.trusted_funding_providers
        )

    async def verify_many(self, candidates: Sequence[BountyCandidate]) -> list[VerificationResult]:
        """Inspect a bounded number of promising candidates; retain all for review."""
        ordered = sorted(candidates, key=lambda item: item.updated_at, reverse=True)
        results = []
        for index, candidate in enumerate(ordered):
            if index >= self.settings.max_verifications_per_scan:
                now = self.clock().astimezone(UTC)
                result = VerificationResult(
                    candidate=candidate,
                    category=Category.UNCERTAIN,
                    availability=Availability.UNKNOWN,
                    checked_at=now,
                    score=0,
                    missing_sources=["verification deferred by scan limit"],
                )
                results.append(rank(result, self.settings, now))
            else:
                results.append(await self.verify(candidate))
        return results


def high_priority_queue(results: Sequence[VerificationResult]) -> list[VerificationResult]:
    """Select only verified, available opportunities for a future mail transport."""
    return [result for result in results if result.high_priority_email_eligible]
