"""Outbox delivery orchestration with bounded, cancellable transport ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from bountyradar.notification import (
    DeliveryError,
    MailTransport,
    eligible_for_delivery,
    opportunity_message,
)
from bountyradar.outbox import NotificationOutbox
from bountyradar.settings import Settings
from bountyradar.verification import CANCELLED, EXPIRY_DATE, RESTRICTION
from bountyradar.verification_models import VerificationResult


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
    for entry in outbox.list_entries(due_only=True):
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
            summary.deferred += 1
            continue
        if current is False:
            outbox.cancel(entry.event_key, "GitHub availability no longer qualifies")
            summary.cancelled += 1
            continue
        if not outbox.claim(entry.event_key):
            summary.daily_cap_reached = True
            break
        message = opportunity_message(entry.result, settings, entry.reason)
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
