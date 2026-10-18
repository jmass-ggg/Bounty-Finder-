"""Independent release regressions; all HTTP and SMTP are offline fixtures."""

import httpx
import pytest
from test_discovery import NOW, issue
from test_email import options
from test_production_alerts import unverified
from test_verification import candidate

from bountyradar.delivery import github_revalidator
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.verification import Verifier


@pytest.mark.asyncio
async def test_expired_comment_reward_is_blocked_at_verification_and_before_send(respx_mock):
    config = options(notification_mode="include_unverified")
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(
        8, title="Repair parser", body="Please repair parser", comments=1,
    ))
    comments = respx_mock.get(base + "/comments")
    comments.respond(200, json=[{
        "body": "Bounty $5; deadline: 2020-01-01", "author_association": "OWNER",
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW)
        value = await Verifier(inspector, config, clock=lambda: NOW).verify(candidate())
        assert value.availability.value == "UNAVAILABLE"
        assert not value.notification_eligible
        assert await github_revalidator(inspector, unverified(), now=NOW) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("comment", [{}, {"body": []}, {"body": False}, {"body": None}])
async def test_unreadable_comment_cannot_establish_available(respx_mock, comment):
    config = options(notification_mode="include_unverified")
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(8, comments=1))
    respx_mock.get(base + "/comments").respond(200, json=[comment])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW)
        value = await Verifier(inspector, config, clock=lambda: NOW).verify(candidate())
        assert value.claim_status.value == "UNKNOWN"
        assert not value.notification_eligible
        assert await github_revalidator(inspector, unverified(), now=NOW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,body", [
    ("issue", []), ("issue", False), ("pr", []), ("pr", False), ("pr", "missing"),
])
async def test_malformed_issue_and_pr_bodies_remain_unknown(respx_mock, kind, body):
    config = options(notification_mode="include_unverified")
    base = "https://api.github.com/repos/acme/tool/issues/8"
    data = issue(8, comments=0)
    if kind == "issue":
        data["body"] = body
    respx_mock.get(base).respond(200, json=data)
    if kind == "pr":
        respx_mock.get(base + "/timeline").respond(200, json=[{
            "event": "cross-referenced", "source": {"issue": {
                "pull_request": {}, "html_url": "https://github.com/acme/tool/pull/12",
            }},
        }])
        pr = {"title": "Related work", "state": "open", "body": body}
        if body == "missing":
            del pr["body"]
        respx_mock.get("https://api.github.com/repos/acme/tool/pulls/12").respond(200, json=pr)
    async with httpx.AsyncClient() as client:
        value = await Verifier(
            GitHubIssueInspector(client, config, clock=lambda: NOW), config, clock=lambda: NOW,
        ).verify(candidate())
    assert value.claim_status.value == "UNKNOWN"
    assert not value.notification_eligible
