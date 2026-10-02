"""Optional verification of a fix by build and Coverity re-analysis (spec D-10, D-42).

Only the commands written in the project settings are run; the AI cannot pass its own.
Re-analysis is heavy, so it runs one at a time. A baseline analysis of the latest code is
run once per run and used to tell new warnings from existing ones.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from .config import VerifyConfig
from .models import Issue
from .workspace import OverlayTree

_analyze_lock = threading.Lock()
LOG_TAIL = 2000


class VerifyError(Exception):
    """Raised when verification cannot be run."""


def _run_shell(command: str, cwd: Path, log: Path) -> bool:
    with open(log, "ab") as f:
        f.write(f"$ {command}\n".encode("utf-8"))
        f.flush()
        proc = subprocess.run(command, cwd=cwd, shell=True, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode == 0


def _tail(log: Path) -> str:
    return log.read_bytes()[-LOG_TAIL:].decode("utf-8", "replace") if log.is_file() else ""


def intermediate_dir(cov_analyze_args: str) -> str:
    """The ``--dir`` given to cov-analyze, which cov-format-errors reads as well."""
    match = re.search(r'(?:^|\s)--dir(?:=|\s+)(?:"([^"]*)"|(\S+))', cov_analyze_args)
    if match:
        return match.group(1) if match.group(1) is not None else match.group(2)
    raise VerifyError("verify.cov_analyze_args に --dir（中間ディレクトリ）を指定してください")


def load_analysis(json_path: Path) -> list[dict[str, Any]]:
    """Read ``cov-format-errors --json-output-v7`` output into simple records."""
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    records = []
    for item in data.get("issues", []):
        records.append({
            "merge_key": item.get("mergeKey"),
            "checker": item.get("checkerName", ""),
            "file": (item.get("mainEventFilePathname") or item.get("strippedMainEventFilePathname") or "").replace("\\", "/"),
            "function": item.get("functionDisplayName") or item.get("functionName"),
            "line": item.get("mainEventLineNumber"),
        })
    return records


def matches(issue: Issue, record: dict[str, Any]) -> bool:
    if issue.merge_key and record.get("merge_key"):
        return issue.merge_key == record["merge_key"]
    file = issue.file.replace("\\", "/").lstrip("/")
    return (record["checker"] == issue.checker
            and record["file"].endswith(file)
            and (issue.function is None or record.get("function") == issue.function))


def _key(record: dict[str, Any]) -> tuple:
    return record.get("merge_key") or (record["checker"], record["file"], record.get("function"))


class Verifier:
    def __init__(self, config: VerifyConfig, run_dir: str | Path):
        self.config = config
        self.dir = Path(run_dir) / "work" / "verify"
        self.log_dir = Path(run_dir) / "verify"

    def _analyze(self, tree_root: Path, name: str, log: Path) -> list[dict[str, Any]] | None:
        idir = intermediate_dir(self.config.cov_analyze_args)
        steps = [
            f"cov-build {self.config.cov_build_args} {self.config.build_command}",
            f"cov-analyze {self.config.cov_analyze_args}",
            f'cov-format-errors --dir {idir} --json-output-v7 "{self.dir / (name + ".json")}"',
        ]
        for step in steps:
            if not _run_shell(step, tree_root, log):
                return None
        return load_analysis(self.dir / (name + ".json"))

    def _baseline(self, latest_root: Path) -> list[dict[str, Any]] | None:
        cached = self.dir / "baseline.json"
        if cached.is_file():
            return load_analysis(cached)
        tree = self.dir / "baseline-tree"
        if not tree.exists():
            shutil.copytree(latest_root, tree)
        return self._analyze(tree, "baseline", self.log_dir / "baseline.log")

    def run(self, mode: str, item_id: str, kind: str, tree: OverlayTree,
            issues: list[Issue]) -> dict[str, Any]:
        if mode == "none":
            return {"mode": "none"}
        if not self.config.build_command:
            raise VerifyError("verify.build_command が設定されていません")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        name = f"{item_id}-{kind}"
        log = self.log_dir / f"{name}.log"
        log.unlink(missing_ok=True)
        root = tree.materialize(self.dir / name)
        result: dict[str, Any] = {"mode": mode, "log": str(log)}

        if mode == "build":
            result["build_ok"] = _run_shell(self.config.build_command, root, log)
            if not result["build_ok"]:
                result["log_tail"] = _tail(log)
            return result

        with _analyze_lock:
            baseline = self._baseline(tree.base)
            if baseline is None:
                return {**result, "build_ok": False, "error": "ベースライン解析に失敗しました",
                        "log_tail": _tail(self.log_dir / "baseline.log")}
            records = self._analyze(root, name, log)
        if records is None:
            return {**result, "build_ok": False, "log_tail": _tail(log)}
        remaining = [i.cid for i in issues if any(matches(i, r) for r in records)]
        known = {_key(r) for r in baseline}
        new = [r for r in records if _key(r) not in known]
        result.update(build_ok=True,
                      resolved_cids=[i.cid for i in issues if i.cid not in remaining],
                      remaining_cids=remaining, new_issues=new[:50],
                      new_issue_count=len(new))
        return result
