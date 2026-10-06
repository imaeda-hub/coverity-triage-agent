"""Helpers shared by the commands of ct.py."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[4]


class CtError(Exception):
    """A problem the agent (and the person) can act on. ct.py prints it as ``{"ok": false, ...}``."""

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return default
        raise CtError(f"ファイルがありません: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CtError(f"ファイルを読めません: {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    """Write via a temporary file so an interrupted command never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Exclusive lock between ct.py processes (several items can be worked on at once)."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # A lock older than the timeout was left by a process that died.
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > timeout:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise CtError(f"ほかの処理が終わるのを待ちましたが、終わりませんでした: {lock}") from None
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def run_cmd(args: list[str], cwd: str | Path | None = None, *, env: dict[str, str] | None = None,
            input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. Output stays bytes (no decoding of file contents)."""
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, input=input_bytes, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise CtError(f"コマンドが見つかりません: {args[0]}（インストールして PATH を通してください）") from exc
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise CtError(f"{' '.join(args[:3])} が失敗しました: {message[:1000]}")
    return proc


def out_text(proc: subprocess.CompletedProcess[bytes]) -> str:
    """Command output that is not file content (revisions, URLs, status lines)."""
    return proc.stdout.decode("utf-8", "replace").strip()


def display_text(data: bytes) -> str:
    """Bytes of a patch shown inside a Markdown report. Only for display; files keep their bytes."""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")
