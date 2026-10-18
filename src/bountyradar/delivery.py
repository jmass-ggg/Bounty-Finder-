"""Outbox delivery orchestration with bounded, cancellable transport ownership."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import format_datetime

from bountyradar.discovery import reward_mention
from bountyradar.notification import (
    DeliveryError,
    MailTransport,
    eligible_for_delivery,
    opportunity_message,
)
from bountyradar.outbox import NotificationOutbox
from bountyradar.settings import Settings
from bountyradar.verification import CANCELLED, EXPIRY_DATE, RESTRICTION
from bountyradar.verification_models import Category, VerificationResult


@dataclass
class DeliverySummary:
    sent: int = 0
    retryable_failure: int = 0
    permanent_failure: int = 0
    unknown_delivery: int = 0
    deferred: int = 0
    cancelled: int = 0
    daily_cap_reached: bool = False
    disabled: bool = False
    scan_cap_reached: bool = False


Revalidator = Callable[[VerificationResult], Awaitable[bool | None]]


async def send_pending(
    outbox: NotificationOutbox,
    transport: MailTransport,
    settings: Settings,
    revalidate: Revalidator,
) -> DeliverySummary:
    """Deliver due rows only after a fresh availability check.

    `None` from the revalidator means source data was unavailable, so the row
    remains pending. A recovered `sending` row becomes `unknown_delivery` and
    is deliberately excluded from automatic retries.
    """
    summary = DeliverySummary()
    if not settings.email_enabled:
        summary.disabled = True
        return summary
    outbox.recover_expired()
    attempts = 0
    for entry in outbox.list_entries(due_only=True, limit=settings.email_per_scan_cap * 4):
        if attempts >= settings.email_per_scan_cap:
            summary.scan_cap_reached = True
            break
        if outbox.daily_cap_reached():
            summary.daily_cap_reached = True
            break
        now = outbox.clock().astimezone(UTC)
        if not eligible_for_delivery(entry.result, settings, now):
            outbox.cancel(entry.event_key, "stored result no longer qualifies")
            summary.cancelled += 1
            continue
        try:
            current = await revalidate(entry.result)
        except Exception:
            current = None
        if current is None:
            outbox.defer(entry.event_key)
            summary.deferred += 1
            continue
        if current is False:
            outbox.cancel(entry.event_key, "GitHub availability no longer qualifies")
            summary.cancelled += 1
            continue
        message = opportunity_message(entry.result, settings, entry.reason)
        identity = hashlib.sha256(entry.event_key.encode()).hexdigest()
        message["Message-ID"] = f"<{identity}@bountyradar.local>"
        message["Date"] = format_datetime(now)
        if not outbox.claim(entry.event_key):
            summary.daily_cap_reached = True
            break
        attempts += 1
        try:
            await transport.send(message)
        except asyncio.CancelledError:
            # The transport has already terminated/reaped its worker before raising.
            outbox.finish(entry.event_key, "unknown")
            raise
        except DeliveryError as exc:
            outbox.finish(entry.event_key, exc.outcome)
            if exc.outcome == "unknown":
                summary.unknown_delivery += 1
            elif exc.outcome == "retryable":
                summary.retryable_failure += 1
            else:
                summary.permanent_failure += 1
        except Exception:
            outbox.finish(entry.event_key, "unknown")
            summary.unknown_delivery += 1
        else:
            outbox.finish(entry.event_key, "sent")
            summary.sent += 1
    return summary


async def github_revalidator(
    inspector, result: VerificationResult, *, now: datetime | None = None
) -> bool | None:
    """Return unknown when GitHub detail, comments, timeline or linked PRs are incomplete."""
    inspection = await inspector.inspect(result.candidate)
    if inspection.missing_sources:
        return None
    text = " ".join((inspection.title or "", inspection.body, *inspection.labels))
    if CANCELLED.search(text) or RESTRICTION.search(text):
        return False
    if result.category != Category.FUNDED_EVIDENCE:
        current_amount, currency, parsed = reward_mention(text)
        if current_amount is None and parsed:
            return None
        if current_amount is None:
            announcements = [e for e in inspection.evidence if e.kind == "reward_announcement"]
            if announcements:
                latest = max(announcements, key=lambda e: e.observed_at)
                current_amount, currency, _ = reward_mention(latest.detail)
        if current_amount is None or current_amount <= 0:
            return False
        if current_amount != result.amount or currency != result.currency:
            return None  # Defer until the normal verifier updates the stored snapshot.
        for evidence in inspection.evidence:
            if evidence.kind == "reward_announcement":
                amount, code, _ = reward_mention(evidence.detail)
                if amount is not None and (amount, code) != (result.amount, result.currency):
                    return None
    for match in EXPIRY_DATE.finditer(text):
        try:
            if date.fromisoformat(match.group(1)) < (now or datetime.now(UTC)).date():
                return False
        except ValueError:
            continue
    return (
        inspection.state == "open"
        and not inspection.assignees
        and not inspection.competing_work_found
        and not inspection.terminal_reason
        and not inspection.restrictions
    )
