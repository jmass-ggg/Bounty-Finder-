"""Bounded SMTP subprocess supervision; credentials travel through stdin only."""

import asyncio
import base64
import json
import os
import sys
from email import policy
from email.message import EmailMessage

from bountyradar.notification import DeliveryError
from bountyradar.settings import Settings


class SMTPProcessTransport:
    def __init__(self, settings: Settings, *, worker_command: tuple[str, ...] | None = None):
        self.settings = settings
        self.worker_command = worker_command

    async def send(self, message: EmailMessage) -> None:
        config = self.settings
        if not config.email_enabled:
            raise DeliveryError("permanent", "email is disabled")
        payload = json.dumps({
            "smtp_host": config.smtp_host,
            "smtp_port": config.smtp_port,
            "smtp_timeout_seconds": config.smtp_timeout_seconds,
            "smtp_username": config.smtp_username,
            "smtp_password": (
                config.smtp_password.get_secret_value() if config.smtp_password else ""
            ),
            "email_to": config.email_to,
            "message": base64.b64encode(message.as_bytes(policy=policy.SMTP)).decode("ascii"),
        }).encode()
        command = self.worker_command or (
            sys.executable, "-m", "bountyradar.smtp_worker", str(os.getpid()),
        )
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("BOUNTYRADAR_") and key not in {"GITHUB_TOKEN", "EMAIL_ENABLED"}
        }
        # Shield startup so cancellation cannot lose a child between fork and registration.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=environment, start_new_session=True,
            close_fds=True,
        ))
        try:
            async with asyncio.timeout(config.smtp_delivery_timeout_seconds):
                try:
                    process = await asyncio.shield(spawn)
                except OSError as exc:
                    raise DeliveryError("retryable", "SMTP worker could not start") from exc
                output, _stderr = await process.communicate(payload)
                if process.returncode != 0:
                    raise DeliveryError("unknown", "SMTP worker exited without a confirmed outcome")
                try:
                    outcome = json.loads(output)["outcome"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise DeliveryError("unknown", "invalid SMTP worker outcome") from exc
                if outcome == "sent":
                    return
                if not isinstance(outcome, str) or outcome not in {
                    "retryable", "permanent", "unknown",
                }:
                    raise DeliveryError("unknown", "invalid SMTP worker outcome")
                raise DeliveryError(outcome, "SMTP worker reported delivery failure")
        except TimeoutError as exc:
            raise DeliveryError("unknown", "SMTP delivery time budget exhausted") from exc
        except OSError as exc:
            raise DeliveryError("unknown", "SMTP worker communication failed") from exc
        finally:
            cleanup = asyncio.create_task(_reap(spawn))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated SIGTERM must not release the caller's lock before reaping.
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError


async def _reap(spawn: asyncio.Task) -> None:
    try:
        process = await spawn
    except OSError:
        return
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if process.stdin:
        process.stdin.close()
    await process.wait()
