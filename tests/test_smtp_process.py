"""Real process lifetime and outbox recovery tests, with fake DNS/SMTP only."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_deployment import PROBE, probe_environment
from test_email import NOW, options, result, valid

from bountyradar.delivery import send_pending
from bountyradar.notification import DeliveryError
from bountyradar.notification import test_message as mail
from bountyradar.outbox import NotificationOutbox
from bountyradar.run_state import RunAlreadyActive, RunState, run_lock
from bountyradar.smtp_process import SMTPProcessTransport

WORKER = Path(__file__).with_name("smtp_worker_probe.py")


def process_transport(settings, marker, mode):
    return SMTPProcessTransport(settings, worker_command=(
        sys.executable, str(WORKER), mode, str(marker), str(os.getpid()),
    ))


async def wait_ready(marker):
    async with asyncio.timeout(5):
        while not marker.with_suffix(".ready").exists():
            await asyncio.sleep(0.01)
    return int(marker.with_suffix(".ready").read_text())


@pytest.mark.asyncio
async def test_worker_preserves_message_and_returns_success(tmp_path):
    settings = options()
    marker = tmp_path / "success"
    await process_transport(settings, marker, "success").send(mail(settings))
    accepted = json.loads(marker.with_suffix(".accepted").read_text())
    assert accepted["subject"] == "BountyRadar test email"
    assert "SMTP configuration test" in accepted["body"]
    with pytest.raises(ProcessLookupError):
        os.kill(accepted["pid"], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dns-blocked", "send-blocked"])
async def test_absolute_delivery_deadline_kills_worker(tmp_path, mode):
    settings = options(smtp_delivery_timeout_seconds=0.8)
    marker = tmp_path / mode
    started = time.monotonic()
    with pytest.raises(DeliveryError, match="time budget") as error:
        await process_transport(settings, marker, mode).send(mail(settings))
    assert error.value.outcome == "unknown"
    assert time.monotonic() - started < 3
    pid = int(marker.with_suffix(".ready").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,outcome", [
    ("socket-timeout", "retryable"), ("send-timeout", "unknown"), ("crash", "unknown"),
])
async def test_worker_errors_preserve_conservative_delivery_outcomes(tmp_path, mode, outcome):
    settings = options()
    with pytest.raises(DeliveryError) as error:
        await process_transport(settings, tmp_path / mode, mode).send(mail(settings))
    assert error.value.outcome == outcome


@pytest.mark.asyncio
async def test_uncertain_acceptance_not_resent_after_database_reopen(tmp_path):
    settings = options(
        database_path=tmp_path / "outbox.db", smtp_delivery_timeout_seconds=0.8
    )
    marker = tmp_path / "acceptance"
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    key = outbox.enqueue(result())
    transport = process_transport(settings, marker, "send-blocked")
    try:
        assert (await send_pending(outbox, transport, settings, valid)).unknown_delivery == 1
    finally:
        outbox.close()
    accepted_before = marker.with_suffix(".accepted").read_bytes()
    outbox = NotificationOutbox(settings.database_path, settings, clock=lambda: NOW)
    try:
        assert [(entry.event_key, entry.state) for entry in outbox.list_entries()] == [
            (key, "unknown_delivery")
        ]
        assert (await send_pending(outbox, transport, settings, valid)).sent == 0
        assert marker.with_suffix(".accepted").read_bytes() == accepted_before
        assert outbox.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_startup_reaps_before_unlock(tmp_path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    registered = asyncio.Event()
    children = []

    async def delayed_registration(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        children.append(process)
        spawned.set()
        await registered.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_registration)
    settings = options(database_path=tmp_path / "locked.db")

    async def scan():
        with run_lock(settings.database_path):
            await process_transport(settings, tmp_path / "startup", "send-blocked").send(
                mail(settings)
            )

    task = asyncio.create_task(scan())
    try:
        await asyncio.wait_for(spawned.wait(), timeout=5)
        task.cancel()
        # Yield to deliver cancellation, then cancel again during shielded cleanup.
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(RunAlreadyActive), run_lock(settings.database_path):
            pass
    finally:
        registered.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(children) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
    with run_lock(settings.database_path):
        pass


def start_scanner(tmp_path, mode):
    database = tmp_path / "scanner.db"
    process = subprocess.Popen(
        [sys.executable, str(PROBE), mode, str(database)], cwd=tmp_path,
        env=probe_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return process, database


def ready_pid(process, database):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        ready = database.with_suffix(".ready")
        if ready.exists():
            return int(ready.read_text())
        assert process.poll() is None, process.communicate()
        time.sleep(0.01)
    raise AssertionError("test scanner did not enter mocked SMTP")


@pytest.mark.parametrize("mode", ["funded-smtp-block", "funded-smtp-deadline"])
def test_cli_sigterm_or_global_deadline_reaps_smtp_before_unlock(tmp_path, mode):
    process, database = start_scanner(tmp_path, mode)
    try:
        pid = ready_pid(process, database)
        with pytest.raises(RunAlreadyActive), run_lock(database):
            pass
        started = time.monotonic()
        if mode == "funded-smtp-block":
            process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == (143 if mode == "funded-smtp-block" else 2), (stdout, stderr)
        assert ("run_interrupted" if mode == "funded-smtp-block" else "run_deferred") in stdout
        assert stderr == ""
        assert time.monotonic() - started < 5
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with run_lock(database), sqlite3.connect(database) as db:
            assert db.execute("SELECT state FROM notification_outbox").fetchall() == [
                ("unknown_delivery",)
            ]
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        state = RunState(database)
        try:
            assert state.last_complete_at() is not None
        finally:
            state.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_abrupt_parent_death_stops_worker_and_retains_recoverable_attempt(tmp_path):
    process, database = start_scanner(tmp_path, "funded-smtp-block")
    try:
        pid = ready_pid(process, database)
        process.kill()  # This test-owned parent only.
        process.communicate(timeout=5)
        deadline = time.monotonic() + 5
        status = Path(f"/proc/{pid}/stat")
        while status.exists():
            try:
                if status.read_text().split()[2] == "Z":
                    break  # Dead child awaiting adoption/reaping by systemd/init.
            except FileNotFoundError:
                break
            assert time.monotonic() < deadline, "orphaned SMTP worker still running"
            time.sleep(0.01)
        with run_lock(database), sqlite3.connect(database) as db:
            assert db.execute("SELECT state FROM notification_outbox").fetchone()[0] == "sending"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_full_cli_process_delivery_increase_interruption_and_duplicate_prevention(tmp_path):
    database = tmp_path / "scanner.db"

    def invoke(mode):
        completed = subprocess.run(
            [sys.executable, str(PROBE), mode, str(database)], cwd=tmp_path,
            env=probe_environment(), capture_output=True, text=True, timeout=5,
        )
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        assert completed.stderr == ""
        return json.loads(completed.stdout)["sent"]

    assert invoke("funded-process") == 1
    accepted_path = database.with_suffix(".accepted")
    assert "USD 100" in json.loads(accepted_path.read_text())["subject"]
    first_acceptance = accepted_path.read_bytes()
    assert invoke("funded-process") == 0
    assert accepted_path.read_bytes() == first_acceptance
    process, database = start_scanner(tmp_path, "funded-process-increase-block")
    try:
        pid = ready_pid(process, database)
        increase_acceptance = accepted_path.read_bytes()
        assert "USD 150" in json.loads(increase_acceptance)["subject"]
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 143, (stdout, stderr)
        assert stderr == ""
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert invoke("funded-process-recover") == 0
        assert accepted_path.read_bytes() == increase_acceptance
        with run_lock(database), sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT reason,state,attempts FROM notification_outbox ORDER BY event_key"
            ).fetchall() == [
                ("new_qualified_bounty", "sent", 1),
                ("reward_increased", "unknown_delivery", 1),
            ]
            assert db.execute("SELECT count(*) FROM discovered_issues").fetchone()[0] == 1
            assert db.execute(
                "SELECT json_extract(payload_json, '$.amount') FROM verification_results"
            ).fetchone()[0] == "150"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        state = RunState(database)
        try:
            assert state.last_complete_at() is not None
            assert state.search_progress() is None
        finally:
            state.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
