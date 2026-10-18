"""GitHub REST issue discovery with bounded, paced search requests."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from bountyradar.models import (
    BountyCandidate,
    ScanResult,
    SearchProgress,
    SearchWindow,
    UnprocessedWindow,
)
from bountyradar.settings import Settings

if TYPE_CHECKING:
    from bountyradar.run_state import RunState

API_URL = "https://api.github.com/search/issues"
API_VERSION = "2022-11-28"
MAX_RESULTS = 1000
AMOUNT = re.compile(
    r"(?<![\w−-])(?:(?P<symbol>[$€£₹])\s*(?P<symbol_amount>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<code>USD|EUR|GBP|INR|NPR|JPY|CAD|AUD|CHF|CNY|BTC|ETH|USDC|USDT)"
    r"\s*(?:[$€£₹]\s*)?(?P<code_amount>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<suffix_amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<suffix_code>USD|EUR|GBP|INR|NPR|JPY|CAD|AUD|CHF|CNY|BTC|ETH|USDC|USDT))\b",
    re.I,
)
REWARD_CONTEXT = re.compile(r"\b(?:bounty|reward|paid|payment|payout|funded|prize)\b", re.I)


class DiscoveryError(Exception):
    """A query could not be completed safely."""


class RateLimited(DiscoveryError):
    """Retries were exhausted or the server requested a long pause."""


class SearchBudgetExceeded(DiscoveryError):
    """Stop a broad scan before it consumes unbounded API time or quota."""


class GitHubDiscovery:
    """Sequential search worker; callers own the injected async HTTP client."""

    name = "github"

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        progress: RunState | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sleep = sleep
        self.clock = clock
        self.monotonic = monotonic
        self.jitter = jitter
        self.next_request_at = 0.0
        self.requests = 0
        self.scan_start_requests = 0
        self.rate_limit_reset: float | None = None
        self.progress = progress

    async def scan(self, hours: float | None = None) -> ScanResult:
        """Search created, updated, and older recently updated funding candidates."""
        lookback = hours if hours is not None else self.settings.created_lookback_hours
        if not math.isfinite(lookback) or lookback <= 0:
            raise ValueError("hours must be positive")
        now = self.clock().astimezone(UTC).replace(microsecond=0)
        created_start = now - timedelta(hours=lookback)
        updated_start = now - timedelta(
            hours=hours if hours is not None else self.settings.updated_lookback_hours
        )
        funding_start = now - timedelta(
            hours=hours if hours is not None else self.settings.funding_lookback_hours
        )
        terms = [f'"{term}"' if " " in term else term for term in self.settings.search_terms]
        terms += [f'label:"{label}"' for label in self.settings.search_labels]
        monetary = [f'"{term}"' for term in self.settings.monetary_terms]
        base_queries = (
            [("created", term, created_start, now, "created", "") for term in terms + monetary]
            + [("updated", term, updated_start, now, "updated", "") for term in terms + monetary]
            + [
                (
                    "possible_funding",
                    term,
                    funding_start,
                    now,
                    "updated",
                    f"created:<{created_start:%Y-%m-%dT%H:%M:%SZ}",
                )
                for term in monetary
            ]
        )
        queries = [
            (
                category,
                f'{term} language:"{language}"' if language else term,
                start,
                end,
                field,
                extra,
            )
            for category, term, start, end, field, extra in base_queries
            for language in (self.settings.languages or [None])
        ]
        saved = self.progress.search_progress() if self.progress else None
        covered_until = saved.covered_until if saved else now
        windows = saved.windows if saved else [
            SearchWindow(category=category, term=term, start=start, end=end, field=field,
                         extra=extra)
            for category, term, start, end, field, extra in queries
        ]
        candidates: dict[int, BountyCandidate] = {}
        pending: list[UnprocessedWindow] = []
        retry: list[SearchWindow] = []
        invalid = 0
        initial_requests = self.requests
        self.scan_start_requests = initial_requests
        deadline = self.monotonic() + self.settings.github_search_timeout_seconds
        if self.progress:
            self.progress.save([], [], completed_at=None, progress=SearchProgress(
                covered_until=covered_until, windows=windows,
            ))
        while windows:
            window = windows[0]
            page_candidates: dict[int, BountyCandidate] = {}
            try:
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise SearchBudgetExceeded("GitHub search time budget exhausted")
                async with asyncio.timeout(remaining):
                    page = await self._page(window.query, window.page)
                total = page["total_count"]
                if total > MAX_RESULTS or page["incomplete_results"]:
                    midpoint = (window.start + (window.end - window.start) / 2).replace(
                        microsecond=0
                    )
                    if not window.start < midpoint < window.end:
                        raise DiscoveryError(
                            "incomplete results or search exceeds 1000-result limit"
                        )
                    windows[:1] = [
                        window.model_copy(update={"end": midpoint, "page": 1, "total": None}),
                        window.model_copy(update={
                            "start": midpoint + timedelta(seconds=1), "page": 1, "total": None,
                        }),
                    ]
                else:
                    bad = self._collect(page["items"], page_candidates)
                    invalid += bad
                    if bad:
                        raise DiscoveryError("invalid issue objects; page retained for retry")
                    if window.total is not None and total != window.total:
                        window.page, window.total = 1, None
                        raise DiscoveryError(
                            "search count changed during pagination; restarting window"
                        )
                    expected = min(100, max(0, total - (window.page - 1) * 100))
                    if len(page["items"]) < expected:
                        raise DiscoveryError("search returned fewer items than total_count")
                    if window.page * 100 < total:
                        windows[0] = window.model_copy(update={
                            "page": window.page + 1, "total": total,
                        })
                    else:
                        windows.pop(0)
            except (RateLimited, SearchBudgetExceeded, TimeoutError) as exc:
                for remaining in windows:
                    pending.append(
                        UnprocessedWindow(
                            category=remaining.category, query=remaining.query,
                            start=remaining.start, end=remaining.end,
                            reason=str(exc) or "GitHub search time budget exhausted",
                        )
                    )
                break
            except (DiscoveryError, ValueError, TypeError) as exc:
                pending.append(UnprocessedWindow(
                    category=window.category, query=window.query,
                    start=window.start, end=window.end, reason=str(exc),
                ))
                retry.append(windows.pop(0))
            self._merge(page_candidates, candidates)
            if self.progress:
                # Findings and the cursor that skips them commit together.
                self.progress.save(
                    list(page_candidates.values()), [], completed_at=None,
                    progress=SearchProgress(covered_until=covered_until, windows=windows + retry),
                )
        return ScanResult(
            candidates=sorted(candidates.values(), key=lambda item: item.updated_at, reverse=True),
            unprocessed_windows=pending,
            invalid_items=invalid,
            requests=self.requests - initial_requests,
            covered_until=covered_until,
        )

    @staticmethod
    def _merge(new: dict[int, BountyCandidate], candidates: dict[int, BountyCandidate]) -> None:
        for key, item in new.items():
            if key not in candidates or item.updated_at >= candidates[key].updated_at:
                candidates[key] = item

    async def _page(self, query: str, page: int) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "BountyRadar/0.1",
        }
        token = self.settings.github_token
        if token and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        for attempt in range(self.settings.github_max_retries + 1):
            if (
                self.requests - self.scan_start_requests
                >= self.settings.github_max_search_requests_per_scan
            ):
                raise SearchBudgetExceeded("GitHub search request budget exhausted")
            await self._pace()
            try:
                self.requests += 1
                response = await self.client.get(
                    API_URL,
                    params={"q": query, "per_page": 100, "page": page,
                            "sort": "created", "order": "asc"},
                    headers=headers,
                    timeout=self.settings.http_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.settings.github_max_retries:
                    raise DiscoveryError(
                        f"network failure after retries: {type(exc).__name__}"
                    ) from exc
                await self.sleep(self._backoff(attempt))
                continue
            if response.status_code in (403, 429):
                # Authentication and permission failures must not be retried as rate limits.
                if (
                    response.status_code == 403
                    and response.headers.get("x-ratelimit-remaining") != "0"
                    and "retry-after" not in response.headers
                    and "rate limit" not in response.text.lower()
                ):
                    raise DiscoveryError("GitHub returned 403 (permission denied)")
                delay = self._rate_delay(response, attempt)
                if self.progress:
                    # Persist before sleeping: a deadline or shutdown may cancel this wait.
                    self.progress.defer_github(self.clock().timestamp() + delay)
                if (
                    delay > self.settings.github_max_rate_wait_seconds
                    or attempt == self.settings.github_max_retries
                ):
                    raise RateLimited(f"GitHub rate limit; retry after {delay:.1f}s")
                await self.sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt == self.settings.github_max_retries:
                    raise DiscoveryError(f"GitHub returned {response.status_code} after retries")
                await self.sleep(self._backoff(attempt))
                continue
            if response.status_code == 422:
                raise DiscoveryError("GitHub rejected search query (422)")
            if response.status_code != 200:
                raise DiscoveryError(f"GitHub returned {response.status_code}")
            try:
                payload = response.json()
                if (
                    not isinstance(payload, dict)
                    or type(payload.get("total_count")) is not int
                    or payload["total_count"] < 0
                    or not isinstance(payload.get("incomplete_results"), bool)
                    or not isinstance(payload.get("items"), list)
                ):
                    raise ValueError("invalid GitHub search response")
                if response.headers.get("x-ratelimit-remaining") == "0":
                    try:
                        self.rate_limit_reset = float(response.headers["x-ratelimit-reset"])
                        if self.progress:
                            self.progress.defer_github(self.rate_limit_reset)
                    except (KeyError, ValueError):
                        self.rate_limit_reset = None
                return payload
            except (ValueError, TypeError) as exc:
                raise DiscoveryError("invalid GitHub search response") from exc
        raise AssertionError("unreachable")

    async def _pace(self) -> None:
        if self.progress and self.progress.github_cooldown() > self.clock().timestamp():
            raise RateLimited("GitHub cooldown from an earlier request is still active")
        if self.rate_limit_reset is not None:
            rate_wait = self.rate_limit_reset - self.clock().timestamp()
            self.rate_limit_reset = None
            if rate_wait > self.settings.github_max_rate_wait_seconds:
                raise RateLimited(f"GitHub primary rate limit; retry after {rate_wait:.1f}s")
            if rate_wait > 0:
                await self.sleep(rate_wait)
        wait = self.next_request_at - self.monotonic()
        if wait > 0:
            await self.sleep(wait)
        # GitHub Search permits 30 authenticated or 10 unauthenticated
        # requests/minute. Leave a little margin around the rolling window.
        authenticated = bool(
            self.settings.github_token and self.settings.github_token.get_secret_value()
        )
        minimum = 2.1 if authenticated else 6.1
        self.next_request_at = self.monotonic() + max(
            minimum, self.settings.github_min_request_interval_seconds
        )

    def _backoff(self, attempt: int) -> float:
        return min(2**attempt + self.jitter(), self.settings.github_max_rate_wait_seconds)

    def _rate_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(
                        0.0, (parsedate_to_datetime(retry_after) - self.clock()).total_seconds()
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
        if response.headers.get("x-ratelimit-remaining") == "0":
            try:
                return max(
                    0.0, float(response.headers["x-ratelimit-reset"]) - self.clock().timestamp()
                )
            except (KeyError, ValueError):
                pass
        return max(60.0 * 2**attempt, self._backoff(attempt))

    def _collect(self, items: list[Any], candidates: dict[int, BountyCandidate]) -> int:
        invalid = 0
        for item in items:
            try:
                candidate = self._candidate(item)
            except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
                invalid += 1
                continue
            if candidate is not None:
                previous = candidates.get(candidate.source_issue_id)
                if previous is None or candidate.updated_at >= previous.updated_at:
                    candidates[candidate.source_issue_id] = candidate
        return invalid

    def _candidate(self, item: Any) -> BountyCandidate | None:
        if not isinstance(item, dict):
            raise ValueError("issue must be an object")
        if item.get("pull_request") or item.get("state") != "open":
            return None
        url = item["html_url"]
        parts = urlparse(url)
        segments = parts.path.strip("/").split("/")
        if (
            parts.scheme != "https"
            or parts.netloc != "github.com"
            or len(segments) != 4
            or segments[2] != "issues"
            or int(segments[3]) != item["number"]
        ):
            raise ValueError("invalid issue URL")
        repository = "/".join(segments[:2])
        if not all(segments[:2]):
            raise ValueError("invalid repository")
        title = item["title"]
        body = item.get("body") or ""
        labels = [label["name"] for label in item.get("labels", [])]
        assignees = [person["login"] for person in item.get("assignees", [])]
        if (item.get("repository") or {}).get("private") is True:
            return None
        text = " ".join((title, body))
        match = next(
            (
                match
                for match in AMOUNT.finditer(text)
                if REWARD_CONTEXT.search(text[max(0, match.start() - 60) : match.end() + 30])
                and not re.search(r"[-−][$€£₹]$", text[max(0, match.start() - 2):match.start()])
                and not re.search(
                    r"\b(?:costs?|price|fee|subscription|budget)\b",
                    text[max(0, match.start() - 30) : match.end() + 20],
                    re.I,
                )
            ),
            None,
        )
        amount = Decimal((match['symbol_amount'] or match['code_amount'] or
                          match['suffix_amount']).replace(",", "")) if match else None
        currency = None
        if match:
            currency = (match['code'] or match['suffix_code'] or
                        {'$': 'USD', '€': 'EUR', '£': 'GBP', '₹': 'INR'}[match['symbol']])
            if match['symbol']:
                explicit = re.match(
                    r"\s+(USD|EUR|GBP|INR|NPR|JPY|CAD|AUD|CHF|CNY|BTC|ETH|USDC|USDT)\b",
                    text[match.end():], re.I,
                )
                if explicit:
                    currency = explicit.group(1)
        evidence = [url]
        if not REWARD_CONTEXT.search(" ".join((title, body, *labels))):
            evidence.append("search match may be in comments; reward unverified")
        if match:
            evidence.append(f"provisional amount mention: {match.group(0)}")
        return BountyCandidate(
            source_issue_id=item["id"],
            repository=repository,
            repository_language=(item.get("repository") or {}).get("language"),
            issue_number=item["number"],
            title=title,
            url=url,
            body=body[:4000],
            labels=labels,
            assignees=assignees,
            created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")),
            discovered_at=self.clock(),
            potential_reward_amount=amount,
            currency=currency.upper() if currency else None,
            candidate_evidence=evidence,
            extraction_confidence="medium" if amount is not None else "low",
        )
