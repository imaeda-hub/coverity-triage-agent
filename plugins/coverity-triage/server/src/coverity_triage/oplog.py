"""Tool operation log in the run folder, with credentials masked (design 5.7)."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_FILE = "operations.log"
_lock = threading.Lock()


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


def log(run_dir: str | Path | None, event: str, secrets: list[str] | None = None,
        **fields: Any) -> None:
    """Append one JSON line. Does nothing when there is no run folder yet."""
    if run_dir is None:
        return
    record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event}
    record.update(_mask(fields, secrets or []))
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        with open(Path(run_dir) / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
