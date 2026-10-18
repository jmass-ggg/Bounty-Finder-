#!/usr/bin/env python3
"""Test-only stand-in for systemctl and systemd-analyze in temporary script fixtures."""

import json
import os
import sys
from pathlib import Path

with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\n")
if "--property=ActiveState" in sys.argv:
    print(os.environ.get("STUB_ACTIVE_STATE", "inactive"))
elif "--property=LoadState" in sys.argv:
    print(os.environ.get("STUB_LOAD_STATE", "loaded"))
elif "stop" in sys.argv and os.environ.get("STUB_STOP_FAIL") == "1":
    raise SystemExit(1)
