"""Paths shared by the tests, and the skill scripts on the import path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "coverity-triage"
SCRIPTS = PLUGIN / "skills" / "coverity-triage-scripts" / "scripts"
SAMPLE = PLUGIN / "skills" / "coverity-selftest" / "assets" / "sample-target"

sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Tests commit in sample repositories; CI machines have no git user configured."""
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.com")
