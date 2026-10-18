"""Exercise the real worker protocol and Gmail backend with entirely fake sockets."""

import json
import os
import signal
import socket
import sys
from pathlib import Path

from bountyradar import notification, smtp_worker


def main():
    mode, marker, parent_pid = sys.argv[1:]
    marker = Path(marker)

    def blocked(*_args, **_kwargs):
        marker.with_suffix(".ready").write_text(str(os.getpid()))
        # SIGTERM resistance ensures the supervisor actually kills, rather than waits.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            signal.pause()

    socket.getaddrinfo = blocked

    class MockSMTP:
        def __init__(self, host, port, *, timeout, context):
            assert host == "smtp.gmail.com" and port == 465
            assert timeout > 0 and context is not None
            if mode == "dns-blocked":
                socket.getaddrinfo(host, port)
            if mode == "socket-timeout":
                raise TimeoutError("fixture timeout")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, username, password):
            assert username == "sender@example.com"
            assert password in {"placeholder-app-password", "offline-fixture"}

        def send_message(self, message):
            marker.with_suffix(".accepted").write_text(json.dumps({
                "pid": os.getpid(), "subject": str(message["Subject"]),
                "body": message.get_body(preferencelist=("plain",)).get_content(),
            }))
            if mode == "send-blocked":
                blocked()
            if mode == "crash":
                os._exit(17)
            if mode == "send-timeout":
                raise TimeoutError("fixture timeout after send")
            return {}

    notification.smtplib.SMTP_SSL = MockSMTP
    sys.argv = [sys.argv[0], parent_pid]
    return smtp_worker.main()


if __name__ == "__main__":
    raise SystemExit(main())
