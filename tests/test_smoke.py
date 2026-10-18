"""Offline checks for packaging, CLI metadata, and settings boundaries."""

import os
import subprocess
import sys
from decimal import Decimal
from importlib.metadata import version

import pytest
from pydantic import ValidationError

from bountyradar import __version__
from bountyradar.settings import Settings


def test_cli_help_and_version():
    for argument, expected in [("--help", "usage:"), ("--version", __version__)]:
        result = subprocess.run(
            [sys.executable, "-m", "bountyradar", argument],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert expected in result.stdout
    assert version("bountyradar") == __version__


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("BOUNTYRADAR_"):
            monkeypatch.delenv(key)


def test_default_settings():
    settings = Settings(_env_file=None)
    assert settings.min_reward_usd == Decimal("0")
    assert settings.scan_interval_seconds == 300


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_reward_usd", "-0.01"),
        ("min_reward_usd", "NaN"),
        ("scan_interval_seconds", 0),
        ("max_rechecks_per_scan", 0),
        ("http_timeout_seconds", -1),
        ("http_timeout_seconds", "inf"),
        ("smtp_port", 65536),
        ("log_level", "invalid"),
    ],
)
def test_invalid_settings(key, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{key: value})


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("BOUNTYRADAR_MIN_REWARD_USD=30\n")
    monkeypatch.setenv("BOUNTYRADAR_MIN_REWARD_USD", "50")
    settings = Settings(_env_file=dotenv, github_token="private-token")
    assert settings.min_reward_usd == Decimal("50")
    assert "private-token" not in repr(settings)
