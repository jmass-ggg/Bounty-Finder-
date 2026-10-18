"""Offline CLI subprocess probe. All HTTP and SMTP are replaced before main runs."""

import asyncio
import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from bountyradar import cli
from bountyradar.discovery import GitHubDiscovery
from bountyradar.settings import Settings
from bountyradar.smtp_process import SMTPProcessTransport
from bountyradar.verification_models import FundingRecord


def main():
    mode, database = sys.argv[1:]
    calls = 0
    issue_calls = 0
    funded = mode.startswith("funded")
    amount = Decimal("150" if mode in {
        "funded-increase-interrupt", "funded-recover",
        "funded-process-increase-block", "funded-process-recover",
    } else "100")
    now = datetime.now(UTC)
    issue_url = "https://github.com/acme/tool/issues/8"
    detail = {
        "id": 8, "number": 8, "title": "Bounty: repair parser",
        "body": f"Reward ${amount}", "html_url": issue_url, "state": "open",
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
        "labels": [{"name": "bounty"}], "assignees": [], "comments": 0,
    }

    async def respond(request):
        nonlocal calls, issue_calls
        calls += 1
        if funded and request.url.path == "/repos/acme/tool/issues/8":
            issue_calls += 1
            if mode == "funded-increase-interrupt" and issue_calls == 2:
                print("READY_FOR_SIGTERM", flush=True)
                await asyncio.Event().wait()
            return httpx.Response(200, json=detail)
        if funded and request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if mode == "interrupt" and calls == 2:
            print("READY_FOR_SIGTERM", flush=True)
            await asyncio.Event().wait()
        assert request.url.path == "/search/issues", request.url
        return httpx.Response(200, json={
            "total_count": int(funded), "incomplete_results": False,
            "items": [detail] if funded else [],
        })

    original_client = httpx.AsyncClient
    clients = []

    def client_factory():
        client = original_client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    async def no_sleep(_seconds):
        pass

    cli.httpx.AsyncClient = client_factory
    cli.GitHubDiscovery = lambda client, settings, **kw: GitHubDiscovery(
        client, settings, **kw, sleep=no_sleep
    )
    config = Settings(
        _env_file=None, database_path=Path(database), github_token="offline-fixture",
        email_enabled=funded, search_terms=["bounty"], search_labels=[], monetary_terms=[],
        github_min_request_interval_seconds=0,
        smtp_username="sender@example.com", smtp_password="offline-fixture",
        email_to="recipient@example.com", trusted_funding_hosts=["funding.example"],
        trusted_funding_providers=["trusted_api"],
    )
    cli.Settings = lambda: config

    class MockSMTP:
        def __init__(self, _settings):
            pass

        async def send(self, message):
            assert funded, "offline probe must never deliver email"
            assert f"USD {amount}" in message["Subject"]
            print("MOCK_SMTP_ACCEPTED", flush=True)

    class TrustedFunding:
        async def records_for(self, candidate):
            assert candidate.url == issue_url
            return [FundingRecord(
                provider="trusted_api", issue_url=issue_url,
                evidence_url="https://funding.example/records/8",
                observed_at=now, status="funded", amount=amount, currency="USD",
            )]

    original_verifier = cli.Verifier
    if funded:
        cli.Verifier = lambda inspector, settings, _provider, **kw: original_verifier(
            inspector, settings, TrustedFunding(), **kw
        )
    if mode in {
        "funded-smtp-block", "funded-smtp-deadline", "funded-process",
        "funded-process-increase-block", "funded-process-recover",
    }:
        config.run_timeout_seconds = 1 if mode == "funded-smtp-deadline" else 240
        worker_mode = "success" if mode in {
            "funded-process", "funded-process-recover",
        } else "send-blocked"
        cli.SMTPProcessTransport = lambda settings: SMTPProcessTransport(
            settings, worker_command=(
                sys.executable, str(Path(__file__).with_name("smtp_worker_probe.py")),
                worker_mode, str(database), str(os.getpid()),
            ),
        )
    else:
        cli.SMTPProcessTransport = MockSMTP
    previous_handler = signal.getsignal(signal.SIGTERM)
    status = cli.main(["run", "--dry-run"] if mode == "dry-run" else ["run"])
    assert all(client.is_closed for client in clients), "HTTP client leaked"
    assert signal.getsignal(signal.SIGTERM) == previous_handler, "SIGTERM handler leaked"
    return status


if __name__ == "__main__":
    raise SystemExit(main())
