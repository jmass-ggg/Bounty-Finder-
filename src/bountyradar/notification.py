"""High-priority email content, async transport contract and synchronous Gmail backend."""

import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal, Protocol

from bountyradar.settings import Settings
from bountyradar.verification_models import VerificationResult


class DeliveryError(Exception):
    """Transport failure with an explicit delivery outcome class."""

    def __init__(self, outcome: Literal["retryable", "permanent", "unknown"], reason: str):
        super().__init__(reason)
        self.outcome = outcome


class MailTransport(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class GmailSMTPTransport:
    """Synchronous Gmail backend, executed only inside the production mail worker."""

    def __init__(self, settings: Settings, smtp_factory=None):
        self.settings = settings
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL

    def send(self, message: EmailMessage) -> None:
        if not self.settings.email_enabled:
            raise DeliveryError("permanent", "email is disabled")
        if not self.settings.smtp_username or not self.settings.smtp_password:
            raise DeliveryError("permanent", "SMTP credentials are incomplete")
        if not self.settings.email_to:
            raise DeliveryError("permanent", "email recipient is missing")
        send_attempted = False
        try:
            with self.smtp_factory(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            ) as smtp:
                try:
                    smtp.login(
                        self.settings.smtp_username,
                        self.settings.smtp_password.get_secret_value(),
                    )
                except smtplib.SMTPAuthenticationError as exc:
                    outcome = "retryable" if 400 <= exc.smtp_code < 500 else "permanent"
                    raise DeliveryError(outcome, "SMTP authentication failed") from exc
                except (TimeoutError, OSError, smtplib.SMTPException) as exc:
                    raise DeliveryError("retryable", "SMTP login or connection failed") from exc
                try:
                    send_attempted = True
                    refused = smtp.send_message(message)
                    if refused:
                        raise smtplib.SMTPRecipientsRefused(refused)
                except smtplib.SMTPRecipientsRefused as exc:
                    temporary = bool(exc.recipients) and all(
                        400 <= status[0] < 500 for status in exc.recipients.values()
                    )
                    raise DeliveryError(
                        "retryable" if temporary else "permanent", "SMTP recipient refused"
                    ) from exc
                except smtplib.SMTPResponseException as exc:
                    outcome = "retryable" if 400 <= exc.smtp_code < 500 else "permanent"
                    raise DeliveryError(
                        outcome, f"SMTP rejected message ({exc.smtp_code})"
                    ) from exc
                except (TimeoutError, OSError, smtplib.SMTPException) as exc:
                    raise DeliveryError(
                        "unknown", "SMTP outcome unknown after send attempt"
                    ) from exc
        except DeliveryError:
            raise
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            outcome = "unknown" if send_attempted else "retryable"
            raise DeliveryError(outcome, "SMTP connection failed or outcome unknown") from exc


def _safe_subject(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:150]


def _message(subject: str, rows: list[tuple[str, str]], settings: Settings) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _safe_subject(subject)
    message["From"] = settings.smtp_username
    message["To"] = settings.email_to
    message.set_content("\n".join(f"{label}: {value}" for label, value in rows))
    rendered = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>" for label, value in rows
    )
    message.add_alternative(
        f"<!doctype html><html><body><h1>{html.escape(_safe_subject(subject))}</h1>"
        f"<dl>{rendered}</dl></body></html>",
        subtype="html",
    )
    return message


def opportunity_message(
    result: VerificationResult, settings: Settings, reason: str
) -> EmailMessage:
    """Render a stored verification snapshot into plain text and escaped HTML."""
    candidate = result.candidate
    evidence = [item for item in result.evidence if item.kind == "funding_funded"]
    funding = evidence[0] if evidence else None
    classification = result.notification_classification
    subject = (f"BountyRadar [{classification}]: {result.currency} {result.amount}"
               f" — {candidate.title}")
    rows = [
        ("Alert reason", reason),
        ("Notification classification", classification),
        ("Payment status", "Authoritative funding evidence recorded" if classification ==
         "CONFIRMED_FUNDED" else "UNVERIFIED — payment and funding are not guaranteed"),
        ("Source platform", candidate.source),
        ("Repository", candidate.repository),
        ("Issue title", candidate.title),
        ("Issue number", str(candidate.issue_number)),
        ("Issue URL", candidate.url),
        ("Reward", f"{result.currency} {result.amount}"),
        ("Funding evidence", funding.detail if funding else "Unavailable"),
        ("Funding source URL", funding.url if funding else "Unavailable"),
        ("Funding observed at", funding.observed_at.isoformat() if funding else "Unavailable"),
        ("Verification category", result.category.value),
        ("Availability", result.availability.value),
        ("Claim status", result.claim_status.value),
        ("Claim limitation",
         "No claim detected in inspected sources; not a guarantee of availability"),
        ("Assignees", ", ".join(result.assignees) or "None found in checked sources"),
        ("Competition", result.competition_assessment),
        ("Created at", candidate.created_at.isoformat()),
        ("Updated at", candidate.updated_at.isoformat()),
        ("Discovered at", candidate.discovered_at.isoformat()),
        ("Priority score", str(result.score)),
        ("Restrictions", "; ".join(result.restrictions) or "None found in checked sources"),
    ]
    return _message(subject, rows, settings)


def test_message(settings: Settings) -> EmailMessage:
    return _message("BountyRadar test email", [("Status", "SMTP configuration test")], settings)


def eligible_for_delivery(result: VerificationResult, settings: Settings, now) -> bool:
    """Recheck the stored high-priority gate before enqueue and before send."""
    from urllib.parse import urlparse

    from bountyradar.ranking import rank
    from bountyradar.verification_models import Availability, Category

    if not result.notification_eligible:
        return False
    result = rank(result.model_copy(deep=True), settings, now)

    if not (
        result.notification_eligible
        and result.availability == Availability.AVAILABLE
        and not result.assignees
        and not result.competing_work_found
        and not any(source != "authoritative funding provider not configured"
                    for source in result.missing_sources)
        and not result.restrictions
        and result.amount is not None
        and result.currency in settings.allowed_currencies
    ):
        return False
    threshold = (
        settings.min_reward_usd
        if result.currency == "USD"
        else settings.currency_minimums.get(result.currency or "")
    )
    if threshold is None or result.amount <= 0 or result.amount < threshold:
        return False
    if result.category != Category.FUNDED_EVIDENCE:
        return (
            settings.notification_mode == "include_unverified"
            and result.category in {Category.REWARD_CLAIMED, Category.UNCERTAIN}
        )
    if not result.high_priority_email_eligible or result.missing_sources:
        return False
    return any(
        item.kind == "funding_funded"
        and item.issue_url == result.candidate.url
        and item.source in settings.trusted_funding_providers
        and urlparse(item.url).scheme == "https"
        and urlparse(item.url).hostname in settings.trusted_funding_hosts
        and 0
        <= (now - item.observed_at).total_seconds()
        <= settings.funding_evidence_max_age_hours * 3600
        for item in result.evidence
    )
