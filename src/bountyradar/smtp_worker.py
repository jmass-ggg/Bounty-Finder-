"""One isolated Gmail delivery. No database, logging, or credential-bearing arguments."""

import base64
import ctypes
import json
import os
import signal
import sys
from email import policy
from email.parser import BytesParser

from bountyradar.notification import DeliveryError, GmailSMTPTransport
from bountyradar.settings import Settings


def guard_parent(parent_pid: int) -> None:
    """Linux parent-death protection, including the race before prctl registration."""
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG: linux/prctl.h and Linux man-pages PR_SET_PDEATHSIG(2const).
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "parent-death guard unavailable")
    if os.getppid() != parent_pid:
        raise SystemExit(1)


def main() -> int:
    try:
        guard_parent(int(sys.argv[1]))
        payload = json.loads(sys.stdin.buffer.read(1_048_577))
        message = BytesParser(policy=policy.default).parsebytes(
            base64.b64decode(payload.pop("message"), validate=True)
        )
        settings = Settings(_env_file=None, email_enabled=True, **payload)
        GmailSMTPTransport(settings).send(message)
    except DeliveryError as exc:
        outcome = exc.outcome
    except Exception:
        outcome = "unknown"
    else:
        outcome = "sent"
    print(json.dumps({"outcome": outcome}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
