"""Deterministic advertised-reward evidence; never proof of available funding."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "NPR", "JPY", "CAD", "AUD",
    "CHF", "CNY", "BTC", "ETH", "USDC", "USDT",
}
AMOUNT = re.compile(
    r"(?<![\w\u2212-])(?:(?P<symbol>[$€£₹])\s*(?P<symbol_amount>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<code>USD|EUR|GBP|INR|NPR|JPY|CAD|AUD|CHF|CNY|BTC|ETH|USDC|USDT)"
    r"\s*(?:[$€£₹]\s*)?(?P<code_amount>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<suffix_amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<suffix_code>USD|EUR|GBP|INR|NPR|JPY|CAD|AUD|CHF|CNY|BTC|ETH|USDC|USDT))\b",
    re.I,
)
OFFER_PREFIX = re.compile(
    r"\b(?:(?:funded\s+)?bounty(?:\s+reward)?|reward|prize|paid\s+(?:task|issue|work))"
    r"\s*(?:(?:of|is|amount)\s*)?[:=\-]?\s*[\[(]?\s*$", re.I,
)
WORK = r"(?:fix(?:ing)?|implement(?:ing|ation)?|complet(?:ing|ion)|resolv(?:ing|e)|develop(?:ing)?)"
PAYMENT_PREFIX = re.compile(
    rf"\bpayment(?:\s+for\s+{WORK}\b[^.;!?\n]{{0,70}}[:=\-]?\s*|\s*[:=\-]?\s*)$", re.I,
)
WORK_SUFFIX = re.compile(
    rf"^\s*(?:USD|EUR|GBP|INR|NPR|CAD|AUD|USDC|USDT)?\s*(?:for|after|to)\s+"
    rf"(?:(?:this|the)\s+)?(?:bug\s+)?{WORK}\b", re.I,
)
FUTURE_PAYMENT = re.compile(
    r"\b(?:you\s+(?:will|shall)\s+(?:be\s+paid|receive)"
    r"|(?:we|I)\s+(?:will\s+)?pay)\s*$", re.I,
)
PAYMENT_TITLE = re.compile(r"^\[Due for payment \d{4}-\d{2}-\d{2}\]\s*\[\s*$", re.I)
NON_OFFER = re.compile(
    r"\b(?:previous|previously|prior|historical|yesterday|already|settled|transferred|received|receipt"
    r"|used to|last (?:week|month|year)|no|not|never|cancelled|canceled|paid out)\b", re.I,
)
PRICE_SUFFIX = re.compile(
    r"^\s*(?:USD|EUR|GBP|INR|NPR|CAD|AUD|USDC|USDT)?\s*"
    r"(?:/\s*(?:month|mo|year|request|search)\b|per\s+(?:month|request|search)\b"
    r"|monthly\b|(?:was\s+)?(?:settled|transferred|received|paid)\b)", re.I,
)
TRANSACTION = re.compile(
    r"\b(?:costs?|price|fee|subscription|retail|wallet|transfer|transaction|account"
    r"|deposit|settlement|receipt|previous|prior|historical|paid|funded|funding)\b", re.I,
)
PAST_PAYMENT = re.compile(
    r"\b(?:was|has been|had been|already|previously)\s+(?:\w+\s+){0,3}"
    r"(?:paid|settled|processed|refunded)\b", re.I,
)
COMMERCIAL_OBJECT = re.compile(
    r"\b(?:account|wallet|vendor|subscription|retail|transaction|settlement)\b", re.I,
)
HISTORICAL_SUFFIX = re.compile(r"\b(?:yesterday|last (?:week|month|year)|previously)\b", re.I)
BOUNDARY = re.compile(r"[.;!?](?:\s|$)|\n\s*\n")
ISSUE_URL = r"https://github\.com/[^/\s)]+/[^/\s)]+/issues/\d+"
LINKED_ISSUE = re.compile(rf"\[[^\n]*?\]\((?P<url>{ISSUE_URL})(?:[?#][^\s)]*)?\)")
TARGET_SUFFIX = re.compile(rf"^\s*(?:for|on|at)\s+(?P<url>{ISSUE_URL})", re.I)


class RewardKind(StrEnum):
    REWARD_OFFER = "REWARD_OFFER"
    UNRELATED_TRANSACTION = "UNRELATED_TRANSACTION"
    AMBIGUOUS_MONETARY_REFERENCE = "AMBIGUOUS_MONETARY_REFERENCE"
    NO_REWARD = "NO_REWARD"


class RewardEvidence(BaseModel):
    classification: RewardKind
    amount: Decimal | None = None
    currency: str | None = None
    text_span: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    source: str
    source_url: str | None = None
    issue_url: str | None = None
    source_timestamp: datetime | None = None
    author: str | None = None
    explanation: str

    @field_validator("source_timestamp")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reward source timestamp must include a timezone")
        return value.astimezone(UTC)


class RewardAssessment(BaseModel):
    classification: RewardKind
    amount: Decimal | None = None
    currency: str | None = None
    evidence: list[RewardEvidence] = Field(default_factory=list)
    explanation: str


def _summarize(evidence: list[RewardEvidence]) -> RewardAssessment:
    offers = [e for e in evidence if e.classification == RewardKind.REWARD_OFFER
              or e.classification == RewardKind.NO_REWARD
              and e.amount is not None and e.amount >= 0]
    amounts = {(e.amount, e.currency) for e in offers}
    ambiguous = any(e.classification == RewardKind.AMBIGUOUS_MONETARY_REFERENCE for e in evidence)
    if ambiguous or len(amounts) > 1:
        return RewardAssessment(
            classification=RewardKind.AMBIGUOUS_MONETARY_REFERENCE, evidence=evidence,
            explanation="ambiguous reward amounts or currencies; manual review required",
        )
    if offers:
        amount, currency = next(iter(amounts))
        return RewardAssessment(
            classification=(RewardKind.REWARD_OFFER if amount is not None and amount > 0
                            else RewardKind.NO_REWARD),
            amount=amount, currency=currency, evidence=evidence,
            explanation="Explicit advertised compensation; funding remains unverified"
            if amount is not None and amount > 0 else "Nonpositive reward cannot qualify",
        )
    unrelated = any(e.classification == RewardKind.UNRELATED_TRANSACTION for e in evidence)
    return RewardAssessment(
        classification=RewardKind.UNRELATED_TRANSACTION if unrelated else RewardKind.NO_REWARD,
        evidence=evidence,
        explanation="Financial references do not offer compensation for the requested work"
        if evidence else "No quantified reward offer found",
    )


def assess_reward(
    text: str, *, source: str = "text", source_url: str | None = None,
    source_timestamp: datetime | None = None, author: str | None = None,
) -> RewardAssessment:
    """Classify each monetary reference, retaining exact spans in the original source."""
    # Mask code and quotes without changing offsets. Records/examples are not offers.
    masked = re.sub(r"```.*?(?:```|$)",
                    lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
    masked = re.sub(r"(?m)^[ \t]*>[^\n]*",
                    lambda m: " " * len(m.group()), masked)
    issue_url = None
    if source_url:
        parsed = urlsplit(source_url)
        issue_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    links = list(LINKED_ISSUE.finditer(text))
    evidence = []
    for match in AMOUNT.finditer(text):
        prefix = masked[:match.start()]
        boundary = list(BOUNDARY.finditer(prefix))
        start = boundary[-1].end() if boundary else 0
        end_boundary = BOUNDARY.search(masked, match.end())
        end = end_boundary.start() if end_boundary else len(text)
        start, end = max(start, match.start() - 180), min(end, match.end() + 160)
        before, after = masked[start:match.start()], masked[match.end():end]
        linked = next((link for link in links
                       if link.start() <= match.start() < link.end()), None)
        target = linked or TARGET_SUFFIX.match(after)
        target_url = target["url"] if target else issue_url
        span = text[start:end]
        number = match["symbol_amount"] or match["code_amount"] or match["suffix_amount"]
        currency = (match["code"] or match["suffix_code"] or
                    {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}[match["symbol"]]).upper()
        malformed = (
            not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", number)
            or re.match(r"[.,]\d", text[match.end():])
        )
        explicit = re.match(r"\s+([A-Z]{3,5})\b", text[match.end():]) if match["symbol"] else None
        if explicit:
            currency = explicit.group(1)
            malformed = malformed or currency not in CURRENCIES
        amount = None if malformed else Decimal(number.replace(",", ""))
        payment = PAYMENT_PREFIX.search(before)
        offered = bool(
            OFFER_PREFIX.search(before) or PAYMENT_TITLE.fullmatch(before)
            or FUTURE_PAYMENT.search(before) and WORK_SUFFIX.search(after)
            or payment and (
                re.search(rf"\bfor\s+{WORK}\b", payment.group(), re.I)
                or WORK_SUFFIX.search(after)
            )
        )
        negative = re.search(r"[-\u2212]\s*$", text[max(0, match.start() - 3):match.start()])
        if not masked[match.start():match.end()].strip():
            kind, reason = RewardKind.UNRELATED_TRANSACTION, "Quoted or code example, not an offer"
        elif target and target_url != issue_url:
            kind = RewardKind.UNRELATED_TRANSACTION
            reason = "Reward refers to another GitHub issue; that task needs independent inspection"
        elif negative:
            kind, reason = RewardKind.NO_REWARD, "Negative amount cannot qualify"
            amount = -amount if amount is not None else None
        elif (NON_OFFER.search(before) or PRICE_SUFFIX.search(after)
              or PAST_PAYMENT.search(after) or HISTORICAL_SUFFIX.search(after)):
            kind = RewardKind.UNRELATED_TRANSACTION
            reason = "Historical, negated, settled or recurring payment, not a current work offer"
        elif offered and COMMERCIAL_OBJECT.search(span) and not WORK_SUFFIX.search(after):
            kind = RewardKind.UNRELATED_TRANSACTION
            reason = "Commercial reward without a work offer"
        elif offered:
            kind = (RewardKind.AMBIGUOUS_MONETARY_REFERENCE if malformed else
                    RewardKind.REWARD_OFFER if amount is not None and amount > 0
                    else RewardKind.NO_REWARD)
            reason = ("Malformed or unsupported reward currency/amount" if malformed else
                      "Explicit compensation attached to this issue/task; funding unverified")
        elif TRANSACTION.search(span):
            kind = RewardKind.UNRELATED_TRANSACTION
            reason = "Transaction or price without a work offer"
        else:
            kind = RewardKind.AMBIGUOUS_MONETARY_REFERENCE
            reason = "Monetary amount has no explicit link to compensation for the requested work"
        evidence.append(RewardEvidence(
            classification=kind, amount=amount, currency=currency, text_span=span,
            start=start, end=end, source=source, source_url=source_url,
            issue_url=target_url,
            source_timestamp=source_timestamp, author=author, explanation=reason,
        ))
    return _summarize(evidence)


def assess_issue_reward(title: str, body: str, url: str | None = None) -> RewardAssessment:
    return _summarize(
        assess_reward(title, source="issue_title", source_url=url).evidence
        + assess_reward(body, source="issue_body", source_url=url).evidence,
    )


def reward_mention(
    text: str, *, source_url: str | None = None,
) -> tuple[Decimal | None, str | None, str | None]:
    """Compatibility API for callers that need only the provisional amount."""
    assessment = assess_reward(text, source_url=source_url)
    if assessment.classification == RewardKind.AMBIGUOUS_MONETARY_REFERENCE:
        return None, None, assessment.explanation
    return assessment.amount, assessment.currency, (
        next((e.text_span for e in assessment.evidence
              if e.classification == RewardKind.REWARD_OFFER), None)
    )
