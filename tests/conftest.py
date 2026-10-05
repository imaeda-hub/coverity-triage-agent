"""Paths shared by the tests, and the skill scripts on the import path."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "coverity-triage"
SCRIPTS = PLUGIN / "skills" / "coverity-triage-scripts" / "scripts"
SAMPLE = PLUGIN / "skills" / "coverity-selftest" / "assets" / "sample-target"

sys.path.insert(0, str(SCRIPTS))
