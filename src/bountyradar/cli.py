"""Discovery, verification and opt-in notification commands."""

import argparse
import asyncio
import json
import logging
import math
import signal
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from bountyradar import __version__
from bountyradar.delivery import github_revalidator, send_pending
from bountyradar.discovery import GitHubDiscovery
from bountyradar.github_inspector import GitHubIssueInspector
from bountyradar.notification import DeliveryError, MailTransport, test_message
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunAlreadyActive, RunState, run_lock
from bountyradar.settings import Settings
from bountyradar.smtp_process import SMTPProcessTransport
from bountyradar.sources import BountySource, scan_sources
from bountyradar.verification import FundingProvider, Verifier


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"level": record.levelname, "message": record.getMessage()})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bountyradar", description="Discover potential GitHub bounty issues."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    scan = commands.add_parser("scan", help="Search public open GitHub issues")
    scan.add_argument(
        "--hours", type=float, default=None, help="Override all search lookback windows (hours)"
    )
    scan.add_argument("--json", action="store_true", help="Print the full structured scan result")
    email = commands.add_parser("email", help="Inspect or deliver the email outbox")
    email_commands = email.add_subparsers(dest="email_command", required=True)
    for name in ("test", "pending", "send-pending"):
        email_commands.add_parser(name).add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="Discover, verify and queue eligible opportunities")
    run.add_argument(
        "--dry-run", action="store_true", help="Preview without database writes or email"
    )
    return parser


async def _scan(settings: Settings, hours: float | None):
    async with httpx.AsyncClient() as client:
        return await GitHubDiscovery(client, settings).scan(hours)


async def _send_due(outbox: NotificationOutbox, settings: Settings):
    state = RunState(settings.database_path)
    try:
        async with httpx.AsyncClient() as client:
            inspector = GitHubIssueInspector(client, settings)
            inspector.progress = state
            return await send_pending(
                outbox,
                SMTPProcessTransport(settings),
                settings,
                lambda result: github_revalidator(inspector, result),
            )
    finally:
        state.close()


async def _run(
    settings: Settings,
    dry_run: bool,
    funding_provider: FundingProvider | None = None,
    transport: MailTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sources: Sequence[BountySource] | None = None,
) -> tuple[int, int, int, int, int, int]:
    with run_lock(settings.database_path):
        async with asyncio.timeout(settings.run_timeout_seconds):
            return await _run_locked(settings, dry_run, funding_provider, transport, clock, sources)


async def _run_with_delay_report(settings: Settings, dry_run: bool):
    async def report_delay():
        await asyncio.sleep(settings.scan_interval_seconds)
        print(json.dumps({"event": "scan_delayed", "reason": "scan_exceeded_interval"}))

    reporter = asyncio.create_task(report_delay())
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_handler = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await _run(settings, dry_run)
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous_handler)


async def _run_locked(
    settings: Settings,
    dry_run: bool,
    funding_provider: FundingProvider | None,
    transport: MailTransport | None,
    clock: Callable[[], datetime],
    sources: Sequence[BountySource] | None,
) -> tuple[int, int, int, int, int, int]:
    started_at = clock().astimezone(UTC)
    state = RunState(settings.database_path) if not dry_run else None
    last_complete = state.last_complete_at() if state else None
    hours = None
    if last_complete is not None:
        elapsed_hours = max(0, (started_at - last_complete).total_seconds() / 3600)
        hours = max(
            settings.created_lookback_hours,
            settings.updated_lookback_hours,
            settings.funding_lookback_hours,
            elapsed_hours + settings.scan_interval_seconds / 3600,
        )
    try:
        return await _process_run(
            settings,
            dry_run,
            funding_provider,
            transport,
            state,
            hours,
            started_at,
            clock,
            sources,
        )
    finally:
        if state:
            state.close()


async def _process_run(
    settings: Settings,
    dry_run: bool,
    funding_provider: FundingProvider | None,
    transport: MailTransport | None,
    state: RunState | None,
    hours: float | None,
    started_at: datetime,
    clock: Callable[[], datetime],
    sources: Sequence[BountySource] | None,
) -> tuple[int, int, int, int, int, int]:
    async with httpx.AsyncClient() as client:
        active_sources = (
            sources if sources is not None else [GitHubDiscovery(client, settings, clock=clock)]
        )
        if sources is None:
            active_sources[0].progress = state
        lookbacks = {}
        for source in active_sources:
            name = getattr(source, "name", "github")
            if name == "github":
                lookbacks[name] = hours
            else:
                checkpoint = state.last_complete_at(name) if state else None
                if checkpoint is not None:
                    elapsed = max(0, (started_at - checkpoint).total_seconds() / 3600)
                    lookbacks[name] = elapsed + settings.scan_interval_seconds / 3600
                else:
                    lookbacks[name] = None
        scan = await scan_sources(active_sources, lookbacks)
        if state:
            state.save(scan.candidates, [], completed_at=None)
        inspector = GitHubIssueInspector(client, settings, clock=clock)
        inspector.progress = state
        verifier = Verifier(inspector, settings, funding_provider, clock=clock)
        work = (
            state.verification_queue(settings.max_verifications_per_scan)
            if state else scan.candidates
        )
        results = await verifier.verify_many(work)
        if state:
            checked_ids = {item.candidate.source_issue_id for item in results}
            tracked = state.tracked_eligible_candidates(settings.max_rechecks_per_scan)
            results.extend(
                await verifier.verify_many(
                    [item for item in tracked if item.source_issue_id not in checked_ids]
                )
            )
        eligible = sum(item.high_priority_email_eligible for item in results)
        queued = sent = 0
        if not dry_run:
            assert state is not None
            outbox = NotificationOutbox(settings.database_path, settings, clock=clock)
            try:
                for item in results:
                    queued += outbox.enqueue(item) is not None
                state.save([], results, completed_at=None)
                for source_name, source_result in scan.results.items():
                    if source_result.complete:
                        state.save(
                            [], [], completed_at=source_result.covered_until or started_at,
                            source=source_name,
                        )
                summary = await send_pending(
                    outbox,
                    transport or SMTPProcessTransport(settings),
                    settings,
                    lambda item: github_revalidator(inspector, item, now=clock()),
                )
                sent = summary.sent
                failures = (
                    summary.retryable_failure + summary.permanent_failure + summary.unknown_delivery
                )
            finally:
                outbox.close()
        else:
            failures = 0
        for window in scan.unprocessed_windows:
            print(
                json.dumps(
                    {
                        "event": "unprocessed_window",
                        "category": window.category,
                        "reason": window.reason,
                        "start": window.start.isoformat(),
                        "end": window.end.isoformat(),
                    }
                )
            )
        for source_name, reason in scan.failures.items():
            print(json.dumps({"event": "source_failure", "source": source_name, "reason": reason}))
        return (
            len(scan.candidates),
            eligible,
            queued,
            sent,
            len(scan.unprocessed_windows) + scan.invalid_items + len(scan.failures),
            failures,
        )


def _email_command(args, settings: Settings) -> int:
    outbox = NotificationOutbox(settings.database_path, settings)
    try:
        outbox.recover_expired()
        if args.email_command == "pending":
            entries = outbox.list_entries()
            if args.json:
                import json

                print(
                    json.dumps(
                        [
                            {
                                "event_key": item.event_key,
                                "state": item.state,
                                "reason": item.reason,
                                "attempts": item.attempts,
                                "next_attempt_at": item.next_attempt_at.isoformat(),
                            }
                            for item in entries
                        ],
                        indent=2,
                    )
                )
            else:
                print(f"Unsent outbox entries: {len(entries)}")
                for item in entries:
                    print(
                        f"{item.event_key} | {item.state} | {item.reason} | "
                        f"attempts={item.attempts}"
                    )
            return 0
        if not settings.email_enabled:
            print("Email disabled; set BOUNTYRADAR_EMAIL_ENABLED=true to allow SMTP delivery.")
            return 2
        if args.email_command == "test":
            attempt = outbox.claim_test()
            if attempt is None:
                print("Daily email cap reached; test message not sent.")
                return 2
            try:
                asyncio.run(SMTPProcessTransport(settings).send(test_message(settings)))
            except DeliveryError as exc:
                outbox.finish_test(attempt, exc.outcome)
                print(f"Test email failed: {exc}")
                return 2
            else:
                outbox.finish_test(attempt, "sent")
                print("Test email accepted by SMTP server.")
                return 0
        summary = asyncio.run(_send_due(outbox, settings))
        if args.json:
            import json

            print(json.dumps(vars(summary), indent=2))
        else:
            print(
                f"Sent: {summary.sent} | Retryable failures: {summary.retryable_failure} | "
                f"Unknown outcomes: {summary.unknown_delivery} | Deferred: {summary.deferred} | "
                f"Cancelled: {summary.cancelled} | Daily cap reached: {summary.daily_cap_reached}"
            )
        return (
            2
            if summary.retryable_failure or summary.permanent_failure or summary.unknown_delivery
            else 0
        )
    finally:
        outbox.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if (
        args.command == "scan"
        and args.hours is not None
        and (not math.isfinite(args.hours) or args.hours <= 0)
    ):
        parser.error("--hours must be positive")
    try:
        settings = Settings()
    except ValidationError as exc:
        parser.error(f"invalid configuration: {exc.error_count()} field error(s)")
    logging.basicConfig(level=settings.log_level)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_JsonFormatter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.command == "email":
        try:
            with run_lock(settings.database_path):
                return _email_command(args, settings)
        except RunAlreadyActive:
            print(json.dumps({"event": "email_deferred", "reason": "another_scan_active"}))
            return 2
    if args.command == "run":
        if not settings.github_token or not settings.github_token.get_secret_value():
            print(json.dumps({"event": "run_failed", "error_type": "MissingGitHubToken"}))
            return 2
        started = time.monotonic()
        try:
            candidates, eligible, queued, sent, incomplete, failures = asyncio.run(
                _run_with_delay_report(settings, args.dry_run)
            )
        except RunAlreadyActive:
            print(json.dumps({"event": "scan_delayed", "reason": "another_scan_active"}))
            return 2
        except asyncio.CancelledError:
            print(json.dumps({"event": "run_interrupted", "reason": "SIGTERM"}))
            return 143
        except TimeoutError:
            print(json.dumps({"event": "run_deferred", "reason": "run_time_budget_exhausted"}))
            return 2
        except Exception as exc:
            print(json.dumps({"event": "run_failed", "error_type": type(exc).__name__}))
            return 2
        elapsed = time.monotonic() - started
        print(
            json.dumps(
                {
                    "event": "run_complete",
                    "candidates": candidates,
                    "high_priority_eligible": eligible,
                    "queued": queued,
                    "sent": sent,
                    "incomplete": incomplete,
                    "delivery_failures": failures,
                    "dry_run": args.dry_run,
                    "duration_seconds": round(elapsed, 2),
                    "timer_interval_exceeded": elapsed > settings.scan_interval_seconds,
                }
            )
        )
        return 2 if incomplete or failures else 0
    result = asyncio.run(_scan(settings, args.hours))
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(
            f"Candidates: {len(result.candidates)} | Requests: {result.requests} | "
            f"Invalid items: {result.invalid_items} | "
            f"Unprocessed windows: {len(result.unprocessed_windows)}"
        )
        for candidate in result.candidates:
            amount = (
                f" | provisional {candidate.currency} {candidate.potential_reward_amount}"
                if candidate.potential_reward_amount is not None
                else ""
            )
            print(
                f"{candidate.repository}#{candidate.issue_number}: "
                f"{candidate.title}{amount}\n  {candidate.url}"
            )
        for window in result.unprocessed_windows:
            print(f"UNPROCESSED {window.category}: {window.reason} | {window.query}")
    return 0 if result.complete else 2
