"""Log of the operations of a run (``operations.log``), with credential values masked.

One JSON object per line: ``time``, ``event``, ``level`` (info / warning / critical), ``source``
(``ct`` for the scripts, ``mcp`` for the MCP server) and the fields of the event. The monitor of
the settings screen (``ct.py gui``, ``activity.py``) reads it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .envvars import get_env

LOG_FILE = "operations.log"


def _mask(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "****")
        return value
    if isinstance(value, dict):
        return {k: _mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, secrets) for v in value]
    return value


def mask(value: Any, secrets: list[str]) -> Any:
    """``value`` with every secret replaced by ****."""
    return _mask(value, secrets)


def secrets_of(config: Any) -> list[str]:
    """Values of the credential environment variables named in the settings."""
    try:
        names = [config.coverity.user_env, config.coverity.key_env]
    except AttributeError:
        return []
    return [v for v in (get_env(n) for n in names) if v]


def log(run_dir: str | Path, event: str, secrets: list[str] | None = None, level: str = "info",
        source: str = "ct", **fields: Any) -> None:
    """Append one JSON line to the run's log."""
    record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, "level": level,
              "source": source}
    record.update(_mask(fields, secrets or []))
    with open(Path(run_dir) / LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read(run_dir: str | Path) -> list[dict[str, Any]]:
    """The records of the run's log, oldest first. Lines that are not a JSON object are skipped
    (a line being written at the same moment, or a damaged file)."""
    try:
        text = (Path(run_dir) / LOG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str):
            records.append(record)
    return records
