"""Typed discovery results. Amounts are provisional, never funding verification."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BountyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["github"] = "github"
    source_issue_id: int = Field(gt=0)
    repository: str
    repository_language: str | None = None
    issue_number: int = Field(gt=0)
    title: str
    url: str
    body: str | None = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    state: Literal["open"] = "open"
    created_at: datetime
    updated_at: datetime
    discovered_at: datetime
    potential_reward_amount: Decimal | None = None
    currency: str | None = None
    candidate_evidence: list[str] = Field(default_factory=list)
    extraction_confidence: Literal["low", "medium"] = "low"

    @field_validator("created_at", "updated_at", "discovered_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)


class UnprocessedWindow(BaseModel):
    category: str
    query: str
    start: datetime
    end: datetime
    reason: str


class ScanResult(BaseModel):
    candidates: list[BountyCandidate]
    unprocessed_windows: list[UnprocessedWindow]
    invalid_items: int = 0
    requests: int = 0
    covered_until: datetime | None = None
    # This overlapping priority lane has its own cursor, not historical coverage.
    fresh_unprocessed_windows: list[UnprocessedWindow] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unprocessed_windows and self.invalid_items == 0


class SearchWindow(BaseModel):
    category: str
    term: str
    start: datetime
    end: datetime
    field: str
    extra: str = ""
    page: int = Field(default=1, ge=1, le=10)
    total: int | None = None

    @property
    def query(self) -> str:
        period = f"{self.field}:{self.start:%Y-%m-%dT%H:%M:%SZ}..{self.end:%Y-%m-%dT%H:%M:%SZ}"
        return " ".join(filter(None, (self.term, "is:issue", "is:open", "is:public",
                                       period, self.extra)))


class SearchProgress(BaseModel):
    covered_until: datetime
    windows: list[SearchWindow]
