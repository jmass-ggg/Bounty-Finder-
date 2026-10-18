"""Offline GitHub discovery contracts using respx and injected time."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from bountyradar.discovery import API_URL, GitHubDiscovery
from bountyradar.settings import Settings

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def issue(number=1, **overrides):
    value = {
        "id": number,
        "number": number,
        "title": "Bounty: fix parser",
        "body": "Reward $50 USD",
        "html_url": f"https://github.com/acme/tool/issues/{number}",
        "state": "open",
        "created_at": "2026-10-16T11:00:00-01:00",
        "updated_at": "2026-10-17T10:00:00Z",
        "labels": [{"name": "bounty"}],
        "assignees": [],
    }
    value.update(overrides)
    return value


def payload(items=None, total=None, incomplete=False):
    items = [] if items is None else items
    return {
        "items": items,
        "total_count": len(items) if total is None else total,
        "incomplete_results": incomplete,
    }


async def no_sleep(_seconds):
    return None


def engine(client, **config):
    defaults = dict(
        search_terms=["bounty"],
        search_labels=[],
        monetary_terms=[],
        github_min_request_interval_seconds=0,
    )
    settings = Settings(_env_file=None, **(defaults | config))
    return GitHubDiscovery(client, settings, clock=lambda: NOW, sleep=no_sleep, jitter=lambda: 0)


@pytest.mark.asyncio
@respx.mock
async def test_valid_deduplicated_and_utc():
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload([issue()])))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan(24)
    assert route.call_count == 2  # created and updated searches
    assert result.complete
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.created_at.isoformat() == "2026-10-16T12:00:00+00:00"
    assert candidate.potential_reward_amount == 50
    assert candidate.extraction_confidence == "medium"
    assert candidate.candidate_evidence[0] == candidate.url
    assert "is:issue" in route.calls[0].request.url.params["q"]


@pytest.mark.asyncio
@respx.mock
async def test_duplicate_queries_keep_newest_issue_observation():
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(
                200, json=payload([issue(updated_at="2026-10-17T11:00:00Z", body="Reward $150")])
            ),
            httpx.Response(
                200, json=payload([issue(updated_at="2026-10-17T10:00:00Z", body="Reward $100")])
            ),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert result.complete
    assert len(result.candidates) == 1
    assert result.candidates[0].potential_reward_amount == 150


@pytest.mark.asyncio
@respx.mock
async def test_empty_and_pull_request_excluded():
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=payload([issue(pull_request={"url": "x"})]))
    )
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert result.candidates == []
    assert result.complete


@pytest.mark.asyncio
@respx.mock
async def test_invalid_payload_and_false_positive_money():
    items = [
        issue(1, created_at="bad"),
        issue(2, title="Reward discussion", body="The API costs $25 per month"),
    ]
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload(items)))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert result.invalid_items == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].potential_reward_amount is None
    assert result.candidates[0].extraction_confidence == "low"


@pytest.mark.asyncio
@respx.mock
async def test_pagination():
    def respond(request):
        page = int(request.url.params["page"])
        numbers = range(1, 101) if page == 1 else [101]
        return httpx.Response(200, json=payload([issue(number) for number in numbers], total=101))

    route = respx.get(API_URL).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert route.call_count == 4
    assert {item.issue_number for item in result.candidates} == set(range(1, 102))
    assert result.complete


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_stops_and_reports_all_remaining():
    respx.get(API_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert not result.complete
    assert len(result.unprocessed_windows) == 2
    assert result.requests == 1


@pytest.mark.asyncio
@respx.mock
async def test_retry_after_then_success():
    waits = []
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(
                403, headers={"Retry-After": "2"}, json={"message": "secondary rate limit"}
            ),
            httpx.Response(200, json=payload()),
            httpx.Response(200, json=payload()),
        ]
    )

    async def sleep(seconds):
        waits.append(seconds)

    async with httpx.AsyncClient() as client:
        finder = engine(client)
        finder.sleep = sleep
        result = await finder.scan()
    assert route.call_count == 3
    assert waits[0] == 2
    assert all(wait >= 6 for wait in waits[1:])
    assert result.complete


@pytest.mark.asyncio
@respx.mock
async def test_timeout_retries_and_reports_failure():
    route = respx.get(API_URL).mock(side_effect=httpx.ReadTimeout("interrupted"))
    async with httpx.AsyncClient() as client:
        result = await engine(client, github_max_retries=1).scan()
    assert route.call_count == 4
    assert len(result.unprocessed_windows) == 2
    assert "network failure" in result.unprocessed_windows[0].reason


@pytest.mark.asyncio
@respx.mock
async def test_dns_failure_reports_unprocessed_windows_without_leaking_host_details():
    route = respx.get(API_URL).mock(side_effect=httpx.ConnectError("DNS lookup failed"))
    async with httpx.AsyncClient() as client:
        result = await engine(client, github_max_retries=0).scan()
    assert route.call_count == 2
    assert len(result.unprocessed_windows) == 2
    assert all("ConnectError" in item.reason for item in result.unprocessed_windows)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json={"items": "bad"}), httpx.Response(200, content=b"not json")],
)
async def test_corrupt_search_response_keeps_checkpoint_incomplete(response):
    respx.get(API_URL).mock(return_value=response)
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert not result.complete
    assert len(result.unprocessed_windows) == 2


@pytest.mark.asyncio
@respx.mock
async def test_invalid_token_response_is_reported_without_retry():
    route = respx.get(API_URL).mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert route.call_count == 2
    assert all("401" in item.reason for item in result.unprocessed_windows)


@pytest.mark.asyncio
@respx.mock
async def test_incomplete_results_splits_window():
    seen = set()

    def respond(request):
        query = request.url.params["q"]
        field = "created" if "created:" in query else "updated"
        if field not in seen:
            seen.add(field)
            return httpx.Response(200, json=payload(incomplete=True))
        return httpx.Response(200, json=payload([issue()], incomplete=False))

    respx.get(API_URL).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        result = await engine(client, updated_lookback_hours=0.001).scan(0.001)
    assert result.complete
    assert len(result.candidates) == 1
    assert result.requests > 2


@pytest.mark.asyncio
@respx.mock
async def test_persistent_incomplete_results_stop_at_request_budget():
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, json=payload(incomplete=True))
    )
    async with httpx.AsyncClient() as client:
        result = await engine(client, github_max_search_requests_per_scan=4).scan()
    assert route.call_count == 4
    assert not result.complete
    assert result.unprocessed_windows
    assert "budget" in result.unprocessed_windows[0].reason


@pytest.mark.asyncio
@respx.mock
async def test_unauthenticated_search_uses_ten_per_minute_spacing():
    waits = []
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload()))

    async def record_sleep(seconds):
        waits.append(seconds)

    async with httpx.AsyncClient() as client:
        finder = engine(client, github_token=None)
        finder.sleep = record_sleep
        result = await finder.scan()
    assert result.complete
    assert any(wait >= 6 for wait in waits)


@pytest.mark.asyncio
@respx.mock
async def test_422_reported_without_retry():
    route = respx.get(API_URL).mock(return_value=httpx.Response(422))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert route.call_count == 2
    assert len(result.unprocessed_windows) == 2


@pytest.mark.asyncio
@respx.mock
async def test_authenticated_language_queries_and_funding_window(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload()))
    async with httpx.AsyncClient() as client:
        result = await engine(client, languages=["Python", "Rust"], monetary_terms=["USD"]).scan()
    assert result.complete
    assert route.call_count == 10
    assert all(call.request.headers["Authorization"] == "Bearer test-token" for call in route.calls)
    queries = [call.request.url.params["q"] for call in route.calls]
    assert any('language:"Python"' in query for query in queries)
    assert any('language:"Rust"' in query for query in queries)
    assert any("created:<" in query and "updated:" in query for query in queries)


@pytest.mark.asyncio
@respx.mock
async def test_search_cap_splits_and_deduplicates():
    seen = set()

    def respond(request):
        query = request.url.params["q"]
        category = "created" if "created:" in query else "updated"
        if category not in seen:
            seen.add(category)
            return httpx.Response(200, json=payload(total=1001))
        return httpx.Response(200, json=payload([issue()]))

    route = respx.get(API_URL).mock(side_effect=respond)
    async with httpx.AsyncClient() as client:
        result = await engine(client, updated_lookback_hours=0.001).scan(0.001)
    assert result.complete
    assert len(result.candidates) == 1
    assert route.call_count == 6


@pytest.mark.asyncio
@respx.mock
async def test_primary_rate_reset_reports_remaining(monkeypatch):
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(NOW.timestamp()) + 600),
            },
            json=payload([issue()]),
        )
    )
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert route.call_count == 1
    assert len(result.candidates) == 1
    assert len(result.unprocessed_windows) == 1
    assert "primary rate limit" in result.unprocessed_windows[0].reason


@pytest.mark.asyncio
@respx.mock
async def test_server_error_retried_and_permission_error_reported():
    route = respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=payload()),
            httpx.Response(403, json={"message": "Resource not accessible"}),
        ]
    )
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert route.call_count == 3
    assert len(result.unprocessed_windows) == 1
    assert "permission denied" in result.unprocessed_windows[0].reason


@pytest.mark.asyncio
@respx.mock
async def test_empty_results():
    respx.get(API_URL).mock(return_value=httpx.Response(200, json=payload()))
    async with httpx.AsyncClient() as client:
        result = await engine(client).scan()
    assert result.complete
    assert result.candidates == []
    assert result.requests == 2


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_lookback(value):
    with pytest.raises(ValueError, match="hours must be positive"):
        import asyncio

        async def check():
            async with httpx.AsyncClient() as client:
                await engine(client).scan(value)

        asyncio.run(check())


def test_cli_json_output_without_network(monkeypatch, capsys):
    from bountyradar import cli
    from bountyradar.models import ScanResult

    async def fake_scan(_settings, hours):
        assert hours == 1
        return ScanResult(candidates=[], unprocessed_windows=[], requests=2)

    monkeypatch.setattr(cli, "_scan", fake_scan)
    assert cli.main(["scan", "--hours", "1", "--json"]) == 0
    assert '"requests": 2' in capsys.readouterr().out


def test_cli_rejects_nonfinite_hours():
    from bountyradar.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["scan", "--hours", "nan"])
    assert exc.value.code == 2
