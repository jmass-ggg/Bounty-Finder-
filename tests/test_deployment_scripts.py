"""Execute copied scripts against temporary files and stub systemd commands only."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_deployment import probe_environment

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sandbox(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ("install.sh", "uninstall.sh"):
        # The production script's location guard is adapted only in this disposable copy.
        (scripts / name).write_text(
            (ROOT / "scripts" / name).read_text().replace(str(ROOT), str(project))
        )
    shutil.copytree(ROOT / "deployment", project / "deployment")
    (project / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    (project / ".env").write_text(
        "EMAIL_ENABLED=false\nGITHUB_TOKEN=offline-fixture\n"
        "BOUNTYRADAR_DATABASE_PATH=data/custom.db\n"
    )
    (project / "data").mkdir()
    database = project / "data/custom.db"
    database.write_bytes(b"database sentinel: never overwritten")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemctl", "systemd-analyze"):
        target = bin_dir / name
        shutil.copyfile(Path(__file__).with_name("systemd_stub.py"), target)
        target.chmod(0o755)
    config = tmp_path / "config"
    log = tmp_path / "calls.jsonl"
    env = probe_environment() | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "XDG_CONFIG_HOME": str(config), "STUB_LOG": str(log),
        "BOUNTYRADAR_DATABASE_PATH": str(database), "EMAIL_ENABLED": "false",
    }
    return project, config / "systemd/user", log, env


def execute(sandbox, script, **environment):
    project, _units, log, env = sandbox
    completed = subprocess.run(
        ["bash", str(project / "scripts" / script)],
        env=env | environment, capture_output=True, text=True, timeout=10,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return completed, calls


def test_install_uninstall_preserve_data_and_config(sandbox):
    project, units, _log, _env = sandbox
    config_before = (project / ".env").read_bytes()
    installed, calls = execute(sandbox, "install.sh")
    assert installed.returncode == 0, installed.stderr
    assert (units / "bountyradar.service").read_bytes() == (
        project / "deployment/systemd/bountyradar.service"
    ).read_bytes()
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    assert (project / "data/custom.db").stat().st_mode & 0o777 == 0o600
    assert ["systemctl", "--user", "enable", "--now", "bountyradar.timer"] in calls
    uninstalled, _calls = execute(sandbox, "uninstall.sh")
    assert uninstalled.returncode == 0, uninstalled.stderr
    assert not (units / "bountyradar.service").exists()
    assert (project / ".env").read_bytes() == config_before
    assert (project / "data/custom.db").read_bytes() == b"database sentinel: never overwritten"


def test_install_refuses_active_service_without_mutation(sandbox):
    project, units, _log, _env = sandbox
    completed, calls = execute(sandbox, "install.sh", STUB_ACTIVE_STATE="activating")
    assert completed.returncode == 1
    assert "stop it explicitly" in completed.stderr
    assert not units.exists()
    assert not any("enable" in call or "daemon-reload" in call for call in calls)
    assert (project / "data/custom.db").read_bytes() == b"database sentinel: never overwritten"


def test_install_requires_token_in_deployed_config(sandbox):
    project, units, _log, _env = sandbox
    (project / ".env").write_text("EMAIL_ENABLED=false\n")
    completed, calls = execute(sandbox, "install.sh", GITHUB_TOKEN="shell-only-secret")
    assert completed.returncode == 1
    assert "GITHUB_TOKEN" in completed.stderr
    assert "shell-only-secret" not in completed.stdout + completed.stderr
    assert not units.exists()
    assert calls == []


def test_install_rejects_database_outside_service_writable_directory(sandbox):
    project, units, _log, _env = sandbox
    (project / ".env").write_text(
        "EMAIL_ENABLED=false\nGITHUB_TOKEN=offline-fixture\n"
        "BOUNTYRADAR_DATABASE_PATH=../outside.db\n"
    )
    completed, calls = execute(sandbox, "install.sh")
    assert completed.returncode == 1
    assert "database path must be under project data/" in completed.stderr
    assert not units.exists()
    assert calls == []


def test_uninstall_retains_units_if_service_cannot_stop(sandbox):
    _project, units, _log, _env = sandbox
    installed, _calls = execute(sandbox, "install.sh")
    assert installed.returncode == 0
    completed, calls = execute(sandbox, "uninstall.sh", STUB_STOP_FAIL="1")
    assert completed.returncode == 1
    assert (units / "bountyradar.service").exists()
    assert calls[-1] == ["systemctl", "--user", "stop", "bountyradar.service"]


def test_invalid_config_is_redacted_before_install(sandbox):
    project, units, _log, _env = sandbox
    (project / ".env").write_text(
        "GITHUB_TOKEN=offline-fixture\nEMAIL_ENABLED=false\n"
        "BOUNTYRADAR_SMTP_PORT=sensitive-invalid-value\n"
    )
    completed, calls = execute(sandbox, "install.sh")
    assert completed.returncode != 0
    assert "Invalid configuration: 1 field error(s)" in completed.stderr
    assert "sensitive-invalid-value" not in completed.stderr + completed.stdout
    assert not units.exists()
    assert not calls
