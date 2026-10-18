"""Transparent prioritization heuristic and separate alert gate."""

from datetime import datetime, timedelta
from decimal import Decimal

from bountyradar.settings import Settings
from bountyradar.verification_models import Availability, Category, VerificationResult


def rank(result: VerificationResult, settings: Settings, now: datetime) -> VerificationResult:
    """Compute a 0-100 score; never use it alone as proof or alert eligibility."""
    weights = settings.score_weights
    funding_factor = {
        Category.FUNDED_EVIDENCE: 1.0,
        Category.REWARD_CLAIMED: 0.35,
        Category.UNCERTAIN: 0.1,
        Category.NOT_BOUNTY: 0.0,
        Category.UNAVAILABLE: 0.0,
    }[result.category]
    age = max(timedelta(0), now - result.candidate.updated_at)
    freshness_factor = 1.0 if age <= timedelta(days=1) else 0.5 if age <= timedelta(days=7) else 0.0
    availability_factor = {
        Availability.AVAILABLE: 1.0,
        Availability.UNKNOWN: 0.25,
        Availability.ASSIGNED: 0.0,
        Availability.CONTESTED: 0.0,
        Availability.CLOSED: 0.0,
        Availability.UNAVAILABLE: 0.0,
    }[result.availability]
    competition_factor = (
        0.0 if result.competing_work_found else 1.0 if not result.missing_sources else 0.3
    )
    reward_factor = 0.0
    threshold = (
        settings.min_reward_usd
        if result.currency == "USD"
        else settings.currency_minimums.get(result.currency or "")
    )
    if result.amount is not None and result.amount > 0 and threshold is not None:
        reward_factor = (
            1.0
            if result.amount >= threshold * Decimal("4")
            else 0.5
            if result.amount >= threshold
            else 0.0
        )
    repo_fit = (
        not settings.preferred_repositories
        or result.candidate.repository in settings.preferred_repositories
    )
    language_fit = (
        not settings.preferred_languages
        or result.candidate.repository_language in settings.preferred_languages
    )
    score = round(
        weights["funding"] * funding_factor
        + weights["freshness"] * freshness_factor
        + weights["availability"] * availability_factor
        + weights["competition"] * competition_factor
        + weights["reward"] * reward_factor
        + weights["technology"] * (1.0 if repo_fit and language_fit else 0.0)
    )
    result.score = min(100, max(0, score))

    reasons: list[str] = []
    if result.availability.value not in settings.notification_availabilities:
        reasons.append(f"availability: {result.availability.value}")
    if any(
        not source.startswith("authoritative funding provider") for source in result.missing_sources
    ):
        reasons.append("incomplete availability evidence")
    if result.competing_work_found:
        reasons.append("competing work found")
    if result.restrictions:
        reasons.append("contribution restrictions require review")
    if threshold is None:
        reasons.append("currency threshold not configured or unknown")
    elif result.amount is None or result.amount <= 0 or result.amount < threshold:
        reasons.append("reward below threshold or unknown")
    if result.currency not in settings.allowed_currencies:
        reasons.append("currency not allowed or unknown")
    if not repo_fit:
        reasons.append("repository not preferred")
    if not language_fit:
        reasons.append("language not preferred or unknown")
    if settings.earliest_created_at and result.candidate.created_at < settings.earliest_created_at:
        reasons.append("created before cutoff")
    if settings.earliest_updated_at and result.candidate.updated_at < settings.earliest_updated_at:
        reasons.append("updated before cutoff")
    unverified_mode = settings.notification_mode == "include_unverified" and result.category in {
        Category.REWARD_CLAIMED, Category.UNCERTAIN,
    }
    if result.category not in settings.notification_categories and not unverified_mode:
        reasons.append("verification category excluded")
    result.filter_reasons = reasons
    result.notification_eligible = not reasons
    result.high_priority_email_eligible = (
        result.notification_eligible
        and result.category == Category.FUNDED_EVIDENCE
        and result.availability == Availability.AVAILABLE
        and result.score >= settings.high_priority_score
    )
    return result
