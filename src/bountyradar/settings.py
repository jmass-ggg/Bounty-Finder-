"""Validated configuration; constructing settings has no external side effects."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read prefixed environment variables, optionally from the local .env file.

    Environment variables override .env values. Email credentials are optional
    while delivery is disabled and required when it is enabled.
    """

    model_config = SettingsConfigDict(
        env_prefix="BOUNTYRADAR_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Zero disables the configured floor; monetary eligibility still requires > 0.
    min_reward_usd: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    scan_interval_seconds: int = Field(default=300, gt=0)
    run_timeout_seconds: float = Field(default=240, gt=0, allow_inf_nan=False)
    database_path: Path = Path("data/bountyradar.sqlite3")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    http_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TOKEN", "BOUNTYRADAR_GITHUB_TOKEN", "github_token"),
    )
    created_lookback_hours: float = Field(default=24, gt=0, allow_inf_nan=False)
    updated_lookback_hours: float = Field(default=24, gt=0, allow_inf_nan=False)
    funding_lookback_hours: float = Field(default=72, gt=0, allow_inf_nan=False)
    search_terms: list[str] = Field(
        default_factory=lambda: ["bounty", "paid", "reward", "cash reward", "paid issue"]
    )
    search_labels: list[str] = Field(default_factory=lambda: ["bounty", "paid"])
    monetary_terms: list[str] = Field(default_factory=lambda: ["USD"])
    languages: list[str] = Field(default_factory=list)
    github_min_request_interval_seconds: float = Field(default=2, ge=0)
    github_max_retries: int = Field(default=3, ge=0, le=10)
    github_max_rate_wait_seconds: float = Field(default=300, gt=0)
    github_max_search_requests_per_scan: int = Field(default=60, gt=0)
    github_search_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    github_max_inspection_requests_per_scan: int = Field(default=60, gt=0)
    github_inspection_timeout_seconds: float = Field(default=90, gt=0, allow_inf_nan=False)

    @field_validator("search_terms", "search_labels", "monetary_terms", "languages")
    @classmethod
    def valid_search_values(cls, values: list[str]) -> list[str]:
        if any(
            not value.strip() or len(value) > 80 or any(char in value for char in "\n\r:")
            for value in values
        ):
            raise ValueError(
                "search values must be nonempty, under 81 characters, and contain no qualifiers"
            )
        return values

    allowed_currencies: list[str] = Field(default_factory=lambda: ["USD"])
    currency_minimums: dict[str, Decimal] = Field(default_factory=dict)
    preferred_repositories: list[str] = Field(default_factory=list)
    preferred_languages: list[str] = Field(default_factory=list)
    earliest_created_at: datetime | None = None
    earliest_updated_at: datetime | None = None
    notification_categories: list[str] = Field(default_factory=lambda: ["FUNDED_EVIDENCE"])
    notification_mode: Literal["confirmed_only", "include_unverified"] = "confirmed_only"
    notification_availabilities: list[str] = Field(default_factory=lambda: ["AVAILABLE"])
    score_weights: dict[str, int] = Field(
        default_factory=lambda: {
            "funding": 35,
            "freshness": 15,
            "availability": 20,
            "competition": 10,
            "reward": 10,
            "technology": 10,
        }
    )
    high_priority_score: int = Field(default=75, ge=0, le=100)
    max_verifications_per_scan: int = Field(default=20, gt=0)
    max_rechecks_per_scan: int = Field(default=5, gt=0)
    trusted_funding_hosts: list[str] = Field(default_factory=list)
    trusted_funding_providers: list[str] = Field(default_factory=list)
    funding_evidence_max_age_hours: float = Field(default=24, gt=0, allow_inf_nan=False)

    @field_validator("earliest_created_at", "earliest_updated_at")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("date filters must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @field_validator("score_weights")
    @classmethod
    def valid_score_weights(cls, weights: dict[str, int]) -> dict[str, int]:
        expected = {"funding", "freshness", "availability", "competition", "reward", "technology"}
        if (
            set(weights) != expected
            or any(type(value) is not int or value < 0 for value in weights.values())
            or sum(weights.values()) != 100
        ):
            raise ValueError(
                "score weights must contain six nonnegative integer factors summing to 100"
            )
        return weights

    @field_validator("notification_categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        valid = {"FUNDED_EVIDENCE", "REWARD_CLAIMED", "UNCERTAIN", "NOT_BOUNTY", "UNAVAILABLE"}
        if any(value not in valid for value in values):
            raise ValueError("unknown verification category")
        return values

    @field_validator("notification_availabilities")
    @classmethod
    def valid_availabilities(cls, values: list[str]) -> list[str]:
        valid = {"AVAILABLE", "ASSIGNED", "CONTESTED", "CLOSED", "UNAVAILABLE", "UNKNOWN"}
        if any(value not in valid for value in values):
            raise ValueError("unknown availability category")
        return values

    @field_validator("currency_minimums")
    @classmethod
    def valid_currency_minimums(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        if any(
            len(code) != 3
            or not code.isalpha()
            or code != code.upper()
            or amount < 0
            or not amount.is_finite()
            for code, amount in values.items()
        ):
            raise ValueError(
                "currency minimums need uppercase three-letter codes and nonnegative finite amounts"
            )
        return values

    @field_validator("allowed_currencies")
    @classmethod
    def valid_currencies(cls, values: list[str]) -> list[str]:
        if any(
            len(value) != 3 or not value.isalpha() or value != value.upper() for value in values
        ):
            raise ValueError("currencies must be uppercase three-letter codes")
        return values

    email_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("EMAIL_ENABLED", "BOUNTYRADAR_EMAIL_ENABLED")
    )
    smtp_host: str = Field(default="smtp.gmail.com", min_length=1)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_timeout_seconds: float = Field(default=20, gt=0, allow_inf_nan=False)
    smtp_delivery_timeout_seconds: float = Field(default=20, gt=0, le=240, allow_inf_nan=False)
    email_daily_cap: int = Field(default=20, gt=0)
    email_max_attempts: int = Field(default=3, gt=0, le=10)
    email_retry_base_seconds: int = Field(default=60, gt=0)
    email_sending_lease_seconds: int = Field(default=300, gt=0)
    reward_increase_min_percent: Decimal = Field(default=Decimal("20"), ge=0, le=1000)
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    email_to: str = ""

    @model_validator(mode="after")
    def validate_email_configuration(self) -> "Settings":
        if self.email_enabled:
            if not self.smtp_username or not self.smtp_password or not self.email_to:
                raise ValueError("enabled email requires SMTP username, app password and recipient")
            for address in (self.smtp_username, self.email_to):
                if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", address):
                    raise ValueError("SMTP addresses must be single email addresses")
        return self
