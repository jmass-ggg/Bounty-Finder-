"""Focused GitHub REST inspection for one promising issue at a time."""

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from bountyradar.models import BountyCandidate
from bountyradar.rewards import RewardKind, assess_reward
from bountyradar.settings import Settings
from bountyradar.verification import EXPIRY_DATE
from bountyradar.verification_models import Evidence, IssueInspection

API = "https://api.github.com"
TERMINAL = re.compile(
    r"\b(?:bounty|reward)\s+(?:is\s+)?(?:paid|cancelled|canceled|expired)\b|\b(?:paid|cancelled|canceled|expired)\s+(?:bounty|reward)\b",
    re.I,
)
CLAIM = re.compile(
    r"\b(?:claiming this|i(?:'| a)m working on this|i will take this|working on this issue)\b", re.I
)
CLAIM_LABEL = re.compile(
    r"(?:(?:status|bounty|work)\s*[:/]\s*)?"
    r"(?:claimed|reserved|assigned|in[ -]progress|work in progress|wip)", re.I,
)
CLAIM_REQUEST = re.compile(
    r"\b(?:(?:can|may|could) i (?:work|take|claim)|i(?: would|'d) like to (?:work|take|claim))",
    re.I,
)
CONFIRMED_CLAIM = re.compile(
    r"\b(?:assigned|reserved|claimed)\s+(?:to|for|by)\s+@?[\w-]+"
    r"|@[\w-]+\s+(?:is\s+)?(?:working on|assigned|has claimed)"
    r"|\b(?:bounty|reward|issue)\s+(?:is\s+)?(?:claimed|reserved|assigned)\b"
    r"|\b(?:go ahead|please proceed)\b", re.I,
)
RESTRICTION = re.compile(
    r"(?im)^.*\b(?:eligib(?:le|ility)|contributors? must|residents? only)\b.*$"
)


class ApiUnavailable(Exception):
    pass


class GitHubIssueInspector:
    """Reads only the candidate issue, its comments/timeline, and referenced PRs."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.client, self.settings = client, settings
        self.clock, self.sleep, self.monotonic, self.jitter = clock, sleep, monotonic, jitter
        self.cache: dict[str, tuple[str, Any]] = {}
        self.next_request_at = 0.0
        self.rate_limit_reset: float | None = None
        self.progress = None
        self.requests = 0
        self.deadline: float | None = None

    async def inspect(self, candidate: BountyCandidate) -> IssueInspection:
        result = IssueInspection()
        base = f"/repos/{candidate.repository}/issues/{candidate.issue_number}"
        try:
            issue = await self._get(base)
            if not isinstance(issue, dict):
                raise ApiUnavailable("invalid issue detail")
        except ApiUnavailable as exc:
            result.missing_sources.append(f"issue detail: {exc}")
            return result
        if (issue.get("state") not in {"open", "closed"}
                or not isinstance(issue.get("title"), str)
                or not isinstance(issue.get("assignees"), list)
                or not isinstance(issue.get("labels"), list)
                or type(issue.get("comments")) is not int or issue["comments"] < 0
                or "body" not in issue
                or (issue["body"] is not None and not isinstance(issue["body"], str))):
            result.missing_sources.append("issue detail: missing or invalid availability fields")
            return result
        now = self.clock()
        result.state = issue.get("state")
        result.title = issue.get("title") if isinstance(issue.get("title"), str) else None
        result.assignees = [
            user["login"]
            for user in (issue.get("assignees") or [])
            if isinstance(user, dict) and isinstance(user.get("login"), str)
        ]
        if any(not isinstance(user, dict) or not isinstance(user.get("login"), str)
               for user in issue["assignees"]):
            result.missing_sources.append("invalid issue assignee identity")
        result.body = issue.get("body") or ""
        result.labels = [
            label["name"]
            for label in (issue.get("labels") or [])
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ]
        if any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
               for label in issue["labels"]):
            result.missing_sources.append("invalid issue label")
        result.checked_sources.append("issue detail")
        result.evidence.append(
            Evidence(
                kind="issue_state",
                source="github_issue",
                url=candidate.url,
                observed_at=now,
                detail=str(result.state),
            )
        )
        if result.assignees:
            result.evidence.append(
                Evidence(
                    kind="assignment",
                    source="github_issue",
                    url=candidate.url,
                    observed_at=now,
                    detail=", ".join(result.assignees),
                )
            )
        result.restrictions.extend(RESTRICTION.findall(result.body))
        for label in result.labels:
            if CLAIM_LABEL.fullmatch(label.replace('_', ' ').strip()):
                result.competing_work_found = True
                result.evidence.append(Evidence(kind="claim_label", source="github_issue",
                                                url=candidate.url, observed_at=now, detail=label))
        if result.state != "open":
            return result
        if issue.get("comments", 0):
            try:
                comments = await self._list(f"{base}/comments")
                result.checked_sources.append("issue comments")
                for comment in comments:
                    if not isinstance(comment, dict):
                        result.missing_sources.append("invalid issue comment object")
                        continue
                    body = comment.get("body")
                    if not isinstance(body, str):
                        result.missing_sources.append("invalid issue comment body")
                        continue
                    trusted = comment.get("author_association") in {
                        "OWNER", "MEMBER", "COLLABORATOR",
                    }
                    confirmed = trusted and CONFIRMED_CLAIM.search(body)
                    ambiguous = CLAIM_REQUEST.search(body)
                    if confirmed:
                        if re.search(r"\b(?:not|never|no longer)\b", body, re.I):
                            result.missing_sources.append("contradictory maintainer claim evidence")
                        else:
                            result.missing_sources = [s for s in result.missing_sources
                                if s != "unconfirmed contributor claim request"]
                            result.competing_work_found = True
                            result.evidence.append(Evidence(
                                kind="maintainer_claim", source="github_comment",
                                url=comment.get("html_url") or candidate.url,
                                observed_at=self._when(comment, now), detail=body[:300]))
                    elif ambiguous:
                        result.missing_sources.append("unconfirmed contributor claim request")
                    comment_reward = assess_reward(
                        body, source="maintainer_comment" if trusted else "public_comment",
                        source_url=comment.get("html_url") or candidate.url,
                        author=(comment.get("user") or {}).get("login")
                        if isinstance(comment.get("user"), dict) else None,
                    )
                    mentioned = comment_reward.amount
                    parsed = (
                        comment_reward.explanation
                        if comment_reward.classification == RewardKind.AMBIGUOUS_MONETARY_REFERENCE
                        else None
                    )
                    trusted_offer = False
                    if mentioned is None and parsed:
                        result.missing_sources.append(f"comment reward: {parsed}")
                    if mentioned is not None and mentioned > 0:
                        for match in EXPIRY_DATE.finditer(body):
                            try:
                                expired = date.fromisoformat(match.group(1)) < now.date()
                            except ValueError:
                                continue
                            if expired:
                                result.terminal_reason = "reward comment deadline has passed"
                                result.evidence.append(Evidence(
                                    kind="expired_deadline", source="github_comment",
                                    url=comment.get("html_url") or candidate.url,
                                    observed_at=now, detail=match.group(0),
                                ))
                        # Only an actual parseable comment date is an announcement timestamp.
                        try:
                            announced = datetime.fromisoformat(
                                comment['created_at'].replace('Z', '+00:00'))
                            if announced.tzinfo is not None and announced <= now:
                                result.evidence.append(Evidence(
                                    kind="reward_announcement", source="github_comment",
                                    url=comment.get('html_url') or candidate.url,
                                    observed_at=announced, detail=body))
                                # An edit date cannot establish when a reward was
                                # added to an older comment. Require an unedited
                                # maintainer announcement for old-issue alerts.
                                edited = datetime.fromisoformat(
                                    comment["updated_at"].replace("Z", "+00:00"),
                                ) if comment.get("updated_at") else None
                                if trusted and (edited is None or edited == announced):
                                    trusted_offer = True
                                    result.evidence.append(Evidence(
                                        kind="maintainer_reward_offer",
                                        source="github_comment",
                                        url=comment.get("html_url") or candidate.url,
                                        observed_at=announced, detail=body,
                                    ))
                                    for item in comment_reward.evidence:
                                        item.source_timestamp = announced.astimezone(UTC)
                                if trusted and edited == announced:
                                    result.evidence.append(Evidence(
                                        kind="trusted_reward_announcement",
                                        source="github_comment",
                                        url=comment.get("html_url") or candidate.url,
                                        observed_at=announced, detail=body,
                                    ))
                        except (KeyError, ValueError, TypeError, AttributeError):
                            pass
                    if not trusted_offer:
                        for item in comment_reward.evidence:
                            if item.classification == RewardKind.REWARD_OFFER:
                                item.classification = RewardKind.AMBIGUOUS_MONETARY_REFERENCE
                                item.explanation = (
                                    "Comment lacks credible maintainer identity or unedited date"
                                )
                    result.reward_evidence.extend(comment_reward.evidence)
                    if TERMINAL.search(body):
                        result.terminal_reason = body[:300]
                        result.evidence.append(
                            Evidence(
                                kind="terminal_claim",
                                source="github_comment",
                                url=comment.get("html_url") or candidate.url,
                                observed_at=self._when(comment, now),
                                detail=body[:300],
                            )
                        )
                    if CLAIM.search(body) and not ambiguous:
                        result.missing_sources.append("unconfirmed contributor claim request")
                    result.restrictions.extend(RESTRICTION.findall(body))
            except ApiUnavailable as exc:
                result.missing_sources.append(f"issue comments: {exc}")
        else:
            result.checked_sources.append("issue comments (count zero)")
        try:
            events = await self._list(f"{base}/timeline")
            result.checked_sources.append("issue timeline")
            for event in events:
                if not isinstance(event, dict):
                    result.missing_sources.append("invalid issue timeline object")
                    continue
                if event.get("event") not in (
                    "cross-referenced",
                    "connected",
                ):
                    continue
                source = event.get("source") or {}
                if event.get("event") == "connected" and not source:
                    result.missing_sources.append("connected timeline event lacks PR identity")
                    continue
                if not isinstance(source, dict):
                    continue
                linked = source.get("issue") or {}
                if not isinstance(linked, dict) or linked.get("pull_request") is None:
                    continue
                pr_url = linked.get("html_url")
                if not isinstance(pr_url, str):
                    result.missing_sources.append("referenced PR lacks canonical identity")
                    continue
                parsed = urlparse(pr_url)
                segments = parsed.path.strip("/").split("/")
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "github.com"
                    or len(segments) != 4
                    or segments[2] != "pull"
                    or not segments[3].isdigit()
                    or parsed.query
                    or parsed.fragment
                ):
                    result.missing_sources.append("invalid referenced PR identity")
                    continue
                result.referenced_prs.append(pr_url)
                result.evidence.append(
                    Evidence(
                        kind="pr_cross_reference",
                        source="github_timeline",
                        url=pr_url,
                        observed_at=self._when(event, now),
                        detail="PR referenced issue; implementation link not yet established",
                    )
                )
                try:
                    pr = await self._get(f"/repos/{segments[0]}/{segments[1]}/pulls/{segments[3]}")
                    if not isinstance(pr, dict):
                        raise ApiUnavailable("invalid pull request detail")
                    if (pr.get("state") not in {"open", "closed"}
                            or not isinstance(pr.get("title"), str)
                            or "body" not in pr
                            or (pr["body"] is not None and not isinstance(pr["body"], str))):
                        raise ApiUnavailable("invalid pull request availability fields")
                    result.checked_sources.append(f"pull request {pr_url}")
                    text = (pr.get("title") or "") + " " + (pr.get("body") or "")
                    full_reference = re.search(
                        rf"\b(?:fix(?:es|ed)?|clos(?:e[sd]?|ing)|resolv(?:e[sd]?|ing))"
                        rf"\s+{re.escape(candidate.url)}(?!\d)",
                        text,
                        re.I,
                    )
                    same_repository = "/".join(segments[:2]).casefold() == (
                        candidate.repository.casefold()
                    )
                    short_reference = same_repository and re.search(
                        rf"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)"
                        rf"\s+#{candidate.issue_number}(?!\d)",
                        text,
                        re.I,
                    )
                    qualified_reference = re.search(
                        rf"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)"
                        rf"\s+{re.escape(candidate.repository)}#{candidate.issue_number}(?!\d)",
                        text,
                        re.I,
                    )
                    linked = bool(
                        full_reference or short_reference or qualified_reference
                        or event.get("event") == "connected"
                    )
                    if linked:
                        result.linked_prs.append(pr_url)
                        result.evidence.append(
                            Evidence(
                                kind="linked_pr",
                                source="github_pull_request",
                                url=pr_url,
                                observed_at=now,
                                detail="PR explicitly linked to issue",
                            )
                        )
                    if linked and (pr.get("state") == "open" or pr.get("merged_at")
                                   or pr.get("merged") is True):
                        result.competing_work_found = True
                        result.evidence.append(
                            Evidence(
                                kind="active_linked_pr",
                                source="github_pull_request",
                                url=pr_url,
                                observed_at=now,
                                detail=(
                                    "open or merged implementation PR explicitly linked to issue"
                                ),
                            )
                        )
                except ApiUnavailable as exc:
                    result.missing_sources.append(f"pull request {pr_url}: {exc}")
        except ApiUnavailable as exc:
            result.missing_sources.append(f"issue timeline: {exc}")
        result.linked_prs = list(dict.fromkeys(result.linked_prs))
        result.referenced_prs = list(dict.fromkeys(result.referenced_prs))
        result.restrictions = list(dict.fromkeys(result.restrictions))
        if result.competing_work_found:
            result.missing_sources = [
                source for source in result.missing_sources
                if source != "unconfirmed contributor claim request"
            ]
        return result

    @staticmethod
    def _when(item: dict[str, Any], fallback: datetime) -> datetime:
        try:
            value = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            return value if value.tzinfo and value.utcoffset() is not None else fallback
        except (KeyError, ValueError, TypeError, AttributeError):
            return fallback

    async def _list(self, path: str) -> list[Any]:
        output: list[Any] = []
        for page in range(1, 6):
            data = await self._get(path, page=page)
            if not isinstance(data, list):
                raise ApiUnavailable("invalid list response")
            output.extend(data)
            if len(data) < 100:
                return output
        raise ApiUnavailable("pagination exceeded five-page inspection limit")

    async def _get(self, path: str, *, page: int | None = None) -> Any:
        if self.deadline is None:
            self.deadline = self.monotonic() + self.settings.github_inspection_timeout_seconds
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise ApiUnavailable("GitHub inspection time budget exhausted")
        try:
            async with asyncio.timeout(remaining):
                return await self._request(path, page=page)
        except TimeoutError as exc:
            raise ApiUnavailable("GitHub inspection time budget exhausted") from exc

    async def _request(self, path: str, *, page: int | None = None) -> Any:
        if self.progress and self.progress.github_cooldown() > self.clock().timestamp():
            raise ApiUnavailable("GitHub cooldown from an earlier request is still active")
        url = API + path
        key = f"{url}?page={page or 1}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "BountyRadar/0.1",
        }
        token = self.settings.github_token
        if token and token.get_secret_value():
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        if key in self.cache:
            headers["If-None-Match"] = self.cache[key][0]
        for attempt in range(self.settings.github_max_retries + 1):
            if self.requests >= self.settings.github_max_inspection_requests_per_scan:
                raise ApiUnavailable("GitHub inspection request budget exhausted")
            if self.rate_limit_reset is not None:
                rate_wait = self.rate_limit_reset - self.clock().timestamp()
                if rate_wait > self.settings.github_max_rate_wait_seconds:
                    raise ApiUnavailable(f"primary rate limit; retry after {rate_wait:.1f}s")
                if rate_wait > 0:
                    await self.sleep(rate_wait)
                self.rate_limit_reset = None
            wait = self.next_request_at - self.monotonic()
            if wait > 0:
                await self.sleep(wait)
            self.next_request_at = (
                self.monotonic() + self.settings.github_min_request_interval_seconds
            )
            try:
                self.requests += 1
                response = await self.client.get(
                    url,
                    params={"per_page": 100, "page": page} if page else None,
                    headers=headers,
                    timeout=self.settings.http_timeout_seconds,
                )
            except httpx.TransportError as exc:
                if attempt == self.settings.github_max_retries:
                    raise ApiUnavailable(type(exc).__name__) from exc
                await self.sleep(
                    min(2**attempt + self.jitter(), self.settings.github_max_rate_wait_seconds)
                )
                continue
            if response.status_code == 304 and key in self.cache:
                return self.cache[key][1]
            if response.status_code in (403, 429) and (
                response.status_code == 429
                or "rate limit" in response.text.lower()
                or "retry-after" in response.headers
                or response.headers.get("x-ratelimit-remaining") == "0"
            ):
                try:
                    delay = float(response.headers.get("retry-after", ""))
                except ValueError:
                    try:
                        delay = max(
                            0.0,
                            (
                                parsedate_to_datetime(response.headers["retry-after"])
                                - self.clock()
                            ).total_seconds(),
                        )
                    except (KeyError, ValueError, TypeError, OverflowError):
                        delay = 60.0 * 2**attempt
                if response.headers.get("x-ratelimit-remaining") == "0":
                    try:
                        delay = max(
                            delay,
                            float(response.headers["x-ratelimit-reset"]) - self.clock().timestamp(),
                        )
                    except (KeyError, ValueError):
                        pass
                if self.progress:
                    self.progress.defer_github(self.clock().timestamp() + max(0, delay))
                if (
                    attempt == self.settings.github_max_retries
                    or delay > self.settings.github_max_rate_wait_seconds
                ):
                    self.rate_limit_reset = self.clock().timestamp() + max(0, delay)
                    raise ApiUnavailable(f"rate limited; retry after {delay:.1f}s")
                await self.sleep(max(0, delay))
                continue
            if response.status_code >= 500:
                if attempt == self.settings.github_max_retries:
                    raise ApiUnavailable(f"HTTP {response.status_code}")
                await self.sleep(
                    min(2**attempt + self.jitter(), self.settings.github_max_rate_wait_seconds)
                )
                continue
            if response.status_code != 200:
                raise ApiUnavailable(f"HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:
                raise ApiUnavailable("invalid JSON") from exc
            if response.headers.get("etag"):
                self.cache[key] = (response.headers["etag"], data)
            if response.headers.get("x-ratelimit-remaining") == "0":
                try:
                    self.rate_limit_reset = float(response.headers["x-ratelimit-reset"])
                    if self.progress:
                        self.progress.defer_github(self.rate_limit_reset)
                except (KeyError, ValueError):
                    pass
            return data
        raise AssertionError("unreachable")
