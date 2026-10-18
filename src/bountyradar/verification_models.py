"""Evidence, availability and ranking results for candidate review."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field, field_validator

from bountyradar.models import BountyCandidate


class Category(StrEnum):
    FUNDED_EVIDENCE = "FUNDED_EVIDENCE"
    REWARD_CLAIMED = "REWARD_CLAIMED"
    UNCERTAIN = "UNCERTAIN"
    NOT_BOUNTY = "NOT_BOUNTY"
    UNAVAILABLE = "UNAVAILABLE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    CONTESTED = "CONTESTED"
    CLOSED = "CLOSED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class Evidence(BaseModel):
    kind: str
    source: str
    url: str
    issue_url: str | None = None
    observed_at: datetime
    detail: str

    @field_validator("observed_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamp must be timezone-aware")
        return value.astimezone(UTC)


class FundingRecord(BaseModel):
    """A status read from an injected, trusted funding platform adapter."""

    provider: str
    funding_id: str | None = None
    issue_url: str
    evidence_url: str
    observed_at: datetime
    status: str
    amount: Decimal = Field(gt=0)
    currency: str

    @field_validator("observed_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("funding timestamp must be timezone-aware")
        return value.astimezone(UTC)


class IssueInspection(BaseModel):
    state: str | None = None
    title: str | None = None
    terminal_reason: str | None = None
    assignees: list[str] = Field(default_factory=list)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    linked_prs: list[str] = Field(default_factory=list)
    referenced_prs: list[str] = Field(default_factory=list)
    competing_work_found: bool = False
    restrictions: list[str] = Field(default_factory=list)
    checked_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    candidate: BountyCandidate
    category: Category
    availability: Availability
    amount: Decimal | None = None
    currency: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    linked_prs: list[str] = Field(default_factory=list)
    referenced_prs: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    competing_work_found: bool = False
    competition_assessment: str = "not found in checked sources"
    restrictions: list[str] = Field(default_factory=list)
    checked_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    checked_at: datetime
    score: int = Field(ge=0, le=100)
    notification_eligible: bool = False
    high_priority_email_eligible: bool = False
    filter_reasons: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def notification_classification(self) -> str:
        if self.category == Category.FUNDED_EVIDENCE:
            return "CONFIRMED_FUNDED"
        if any(item.kind == "platform_listing" for item in self.evidence):
            return "PLATFORM_LISTED_UNVERIFIED"
        return "POTENTIAL_UNVERIFIED"

    @field_validator("checked_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("check timestamp must be timezone-aware")
        return value.astimezone(UTC)
