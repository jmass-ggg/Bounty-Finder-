"""Production alert boundaries, with temporary SQLite and offline HTTP/mail."""

import asyncio
import json
from datetime import timedelta
from html.parser import HTMLParser

import httpx
import pytest
from test_discovery import NOW, issue, no_sleep, payload
from test_email import FakeTransport, options, result, valid
from test_verification import candidate

from bountyradar import cli
from bountyradar.delivery import send_pending
from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.models import ScanResult
from bountyradar.notification import eligible_for_delivery, opportunity_message
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunState, run_lock
from bountyradar.verification import Verifier
from bountyradar.verification_models import Category


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.append(dict(attrs)["href"])
        if tag == "script":
            self.scripts.append(tag)


def unverified(n=8):
    value = result(n, amount="5", category=Category.REWARD_CLAIMED)
    value.candidate.created_at = NOW
    value.evidence = []
    return value


def test_mobile_html_button_and_plain_fallback():
    value = unverified()
    value.candidate.title = '<script>alert("x")</script>'
    mail = opportunity_message(value, options(), "<img src=x onerror=alert(1)>")
    markup = mail.get_body(preferencelist=("html",)).get_content()
    parsed = Links()
    parsed.feed(markup)
    assert parsed.urls == [value.candidate.url, value.candidate.url]
    assert not parsed.scripts
    assert "&lt;script&gt;" in markup and "&lt;img" in markup
    assert 'name="viewport"' in markup
    assert "View Bounty on GitHub" in markup
    text = mail.get_body(preferencelist=("plain",)).get_content()
    assert value.candidate.url in text
    assert "UNVERIFIED" in text and "not guaranteed" in text


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "https://github.com.evil.invalid/acme/tool/issues/8",
    "https://github.com:444/acme/tool/issues/8",
    "https://user@github.com/acme/tool/issues/8",
    "https://github.com/acme/tool/issues/8?redirect=elsewhere",
    "https://github.com/acme/tool/issues/8#fragment",
    "https://github.com/acme/tool/pull/8",
    "https://github.com/acme/tool/issues/9",
    "https://github.com/acme/tool/issues/\n8",
])
def test_untrusted_action_url_never_queues_or_renders(url):
    value = result()
    value.candidate.url = url
    assert not eligible_for_delivery(value, options(), NOW)
    with pytest.raises(ValueError):
        opportunity_message(value, options(), "new_qualified_bounty")


@pytest.mark.asyncio
async def test_batch_and_daily_limits_preserve_pending_and_stable_identity(tmp_path):
    config = options(email_per_scan_cap=2, email_daily_cap=3)
    store = NotificationOutbox(tmp_path / "bounded.db", config, clock=lambda: NOW)
    for n in range(1, 7):
        store.enqueue(result(n))
    transport = FakeTransport()
    first = await send_pending(store, transport, config, valid)
    assert first.sent == 2 and first.scan_cap_reached
    assert len(store.list_entries(due_only=True)) == 4
    second = await send_pending(store, transport, config, valid)
    assert second.sent == 1 and second.daily_cap_reached
    assert len(store.list_entries(due_only=True)) == 3
    assert len({str(m["Message-ID"]) for m in transport.messages}) == 3
    assert all(m["Date"] for m in transport.messages)
    store.close()
    store = NotificationOutbox(tmp_path / "bounded.db", config, clock=lambda: NOW)
    assert (await send_pending(store, transport, config, valid)).sent == 0
    assert len(store.list_entries(due_only=True)) == 3
    store.close()


@pytest.mark.asyncio
async def test_failures_spend_batch_capacity(tmp_path):
    config = options(email_per_scan_cap=1)
    store = NotificationOutbox(tmp_path / "fail.db", config, clock=lambda: NOW)
    for n in (1, 2):
        store.enqueue(result(n))
    transport = FakeTransport(["retryable"])
    summary = await send_pending(store, transport, config, valid)
    assert summary.retryable_failure == 1 and summary.scan_cap_reached
    assert store.db.execute(
        "SELECT count(*) FROM notification_delivery_attempts",
    ).fetchone()[0] == 1
    store.close()


@pytest.mark.asyncio
async def test_activation_boundary_survives_restart_and_long_downtime(tmp_path):
    config = options(notification_mode="include_unverified")
    clock = [NOW]
    store = NotificationOutbox(tmp_path / "epoch.db", config, clock=lambda: clock[0])
    old = unverified()
    old.candidate.created_at = NOW - timedelta(days=100)
    # Even a new issue update and discovery do not establish a reward announcement.
    assert store.enqueue(old) is None
    assert store.db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0
    store.close()
    clock[0] += timedelta(days=4)
    store = NotificationOutbox(tmp_path / "epoch.db", config, clock=lambda: clock[0])
    missed = unverified(9)
    missed.candidate.created_at = NOW + timedelta(hours=1)
    missed.checked_at = clock[0]
    assert store.enqueue(missed)
    assert store.enqueue(missed) is None
    assert len(store.list_entries()) == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("association,edited,announced_hours,expected", [
    ("OWNER", False, 1, True),
    ("MEMBER", False, 1, True),
    ("NONE", False, 1, False),
    ("OWNER", True, 1, False),
    ("OWNER", False, 48, False),
])
async def test_old_issue_requires_recent_unedited_maintainer_announcement(
    tmp_path, respx_mock, association, edited, announced_hours, expected,
):
    config = options(notification_mode="include_unverified")
    created = NOW - timedelta(days=100)
    announced = NOW - timedelta(hours=announced_hours)
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(
        8, title="Repair parser", body="Please repair parser", comments=1,
        created_at=created.isoformat(),
    ))
    respx_mock.get(base + "/comments").respond(200, json=[{
        "body": "Bounty reward $5", "author_association": association,
        "created_at": announced.isoformat(),
        "updated_at": (announced + timedelta(minutes=int(edited))).isoformat(),
        "html_url": candidate().url + "#issuecomment-1",
    }])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    async with httpx.AsyncClient() as client:
        value = await Verifier(
            GitHubIssueInspector(client, config, clock=lambda: NOW),
            config, clock=lambda: NOW,
        ).verify(candidate(created_at=created))
    store = NotificationOutbox(tmp_path / "announcement.db", config, clock=lambda: NOW)
    assert bool(store.enqueue(value)) is expected
    store.close()


@pytest.mark.asyncio
async def test_self_asserted_work_without_acceptance_is_unknown(respx_mock):
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(8, comments=1))
    respx_mock.get(base + "/comments").respond(200, json=[{
        "body": "I am working on this", "author_association": "NONE",
    }])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    config = options(notification_mode="include_unverified")
    async with httpx.AsyncClient() as client:
        value = await Verifier(
            GitHubIssueInspector(client, config), config,
        ).verify(candidate())
    assert value.claim_status.value == "UNKNOWN"
    assert not value.notification_eligible


@pytest.mark.asyncio
async def test_conflicting_comment_amount_defers_instead_of_choosing_old_price(respx_mock):
    from bountyradar.delivery import github_revalidator

    config = options(notification_mode="include_unverified")
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(8, body="Bounty reward $5", comments=1))
    respx_mock.get(base + "/comments").respond(200, json=[{
        "body": "Bounty reward $1", "author_association": "OWNER",
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }])
    respx_mock.get(base + "/timeline").respond(200, json=[])
    async with httpx.AsyncClient() as client:
        inspector = GitHubIssueInspector(client, config, clock=lambda: NOW)
        value = await Verifier(inspector, config, clock=lambda: NOW).verify(candidate())
        assert value.claim_status.value == "UNKNOWN"
        assert not value.notification_eligible
        assert await github_revalidator(inspector, unverified(), now=NOW) is None


@pytest.mark.asyncio
async def test_merged_relevant_implementation_also_blocks_available_claim(respx_mock):
    config = options(notification_mode="include_unverified")
    base = "https://api.github.com/repos/acme/tool/issues/8"
    respx_mock.get(base).respond(200, json=issue(8, comments=0))
    respx_mock.get(base + "/timeline").respond(200, json=[{
        "event": "cross-referenced", "source": {"issue": {
            "pull_request": {}, "html_url": "https://github.com/acme/tool/pull/12",
        }},
    }])
    respx_mock.get("https://api.github.com/repos/acme/tool/pulls/12").respond(200, json={
        "title": "Fixes #8", "body": "Implementation", "state": "closed",
        "merged_at": NOW.isoformat(),
    })
    async with httpx.AsyncClient() as client:
        value = await Verifier(
            GitHubIssueInspector(client, config), config,
        ).verify(candidate())
    assert value.claim_status.value == "CLAIMED"
    assert not value.notification_eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["1", "5"])
async def test_fresh_small_bounty_discovery_to_same_scan_smtp(
    tmp_path, respx_mock, monkeypatch, amount,
):
    config = options(
        database_path=tmp_path / "small.db", notification_mode="include_unverified",
        search_terms=["bounty"], search_labels=[], monetary_terms=[],
    )
    data = issue(
        8, title="Fix parser", body=f"Bounty reward ${amount}", comments=0,
        created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
    )
    respx_mock.get(API_URL).respond(200, json=payload([data]))
    base = "https://api.github.com/repos/acme/tool/issues/8"
    detail = respx_mock.get(base).respond(200, json=data)
    respx_mock.get(base + "/timeline").respond(200, json=[])
    monkeypatch.setattr(cli, "GitHubDiscovery", lambda client, settings, **kw:
                        GitHubDiscovery(client, settings, **kw, sleep=no_sleep))
    transport = FakeTransport()
    first = await cli._run(config, False, transport=transport, clock=lambda: NOW)
    assert first[:4] == (1, 1, 1, 1)
    assert detail.call_count == 2  # Verification, then immediate pre-delivery inspection.
    assert f"${amount} USD" in transport.messages[0]["Subject"]
    await cli._run(
        config, False, transport=transport, clock=lambda: NOW + timedelta(minutes=5),
    )
    assert len(transport.messages) == 1


class EmptySource:
    name = "github"

    async def scan(self, hours):
        return ScanResult(candidates=[], unprocessed_windows=[])


@pytest.mark.asyncio
async def test_delivery_has_reserved_requests_after_busy_verification(
    tmp_path, respx_mock, capsys,
):
    config = options(
        database_path=tmp_path / "reserved.db", notification_mode="include_unverified",
        github_max_inspection_requests_per_scan=8, max_verifications_per_scan=4,
        email_per_scan_cap=1,
    )
    state = RunState(config.database_path)
    state.save([candidate(
        source_issue_id=n, issue_number=n, url=f"https://github.com/acme/tool/issues/{n}",
    ) for n in range(1, 5)], [], completed_at=None)
    state.close()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        n = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue(
            n, comments=0, created_at=NOW.isoformat(),
        ))

    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=respond,
    )
    transport = FakeTransport()
    summary = await cli._run(
        config, False, transport=transport, sources=[EmptySource()], clock=lambda: NOW,
    )
    assert summary[3] == 1
    assert len(calls) == 8
    assert calls[-1].endswith("/timeline")
    metrics = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert metrics[-1]["event"] == "scan_metrics"
    assert metrics[-1]["inspection_requests"] == 8
    assert metrics[-1]["verified"] == 4 and metrics[-1]["sent"] == 1


@pytest.mark.asyncio
async def test_interruption_during_second_inspection_keeps_first_alert(tmp_path, respx_mock):
    config = options(
        database_path=tmp_path / "interrupted.db", notification_mode="include_unverified",
        run_timeout_seconds=0.5, max_verifications_per_scan=2,
    )
    state = RunState(config.database_path)
    state.save([candidate(
        source_issue_id=n, issue_number=n, url=f"https://github.com/acme/tool/issues/{n}",
    ) for n in (1, 2)], [], completed_at=None)
    state.close()
    first_number = []

    async def respond(request):
        path = request.url.path
        n = int(path.split("/issues/")[1].split("/")[0])
        if not first_number:
            first_number.append(n)
        if n != first_number[0]:
            await asyncio.Event().wait()
        return httpx.Response(200, json=[] if path.endswith("/timeline") else issue(
            n, comments=0, created_at=NOW.isoformat(),
        ))

    respx_mock.get(url__regex=r"https://api\.github\.com/repos/acme/tool/issues/").mock(
        side_effect=respond,
    )
    with pytest.raises(TimeoutError):
        await cli._run(config, False, sources=[EmptySource()], clock=lambda: NOW)
    with run_lock(config.database_path):
        store = NotificationOutbox(config.database_path, config, clock=lambda: NOW)
        assert len(store.list_entries()) == 1
        assert store.list_entries()[0].state == "pending"
        assert store.db.execute("SELECT count(*) FROM verification_results").fetchone()[0] == 1
        store.close()


@pytest.mark.asyncio
async def test_recent_lane_retains_all_supported_terms(tmp_path, respx_mock):
    config = options(
        search_terms=["bounty", "paid"], search_labels=["paid"], monetary_terms=["USD"],
    )
    state = RunState(tmp_path / "terms.db")
    # Leave a historical cursor to engage the independent recent lane.
    from bountyradar.models import SearchProgress, SearchWindow

    state.save([], [], completed_at=None, progress=SearchProgress(
        covered_until=NOW - timedelta(hours=12), windows=[SearchWindow(
            category="created", term="bounty", field="created",
            start=NOW - timedelta(hours=13), end=NOW - timedelta(hours=12),
        )],
    ))
    queries = []
    respx_mock.get(API_URL).mock(side_effect=lambda request: (
        queries.append(request.url.params["q"]) or httpx.Response(200, json=payload())
    ))
    async with httpx.AsyncClient() as client:
        finder = GitHubDiscovery(client, config, clock=lambda: NOW, sleep=no_sleep, progress=state)
        await finder.scan()
    assert any(q.startswith('paid ') for q in queries)
    assert any(q.startswith('label:"paid"') for q in queries)
    assert any(q.startswith('"USD"') for q in queries)
    state.close()
