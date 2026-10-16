"""Initial analysis (option verify.initial): what the person did by hand before running /coverity-run.

The open folder is built and analyzed with Coverity, and the result is committed to Coverity
Connect, to the stream of the run's filter, with the revision of the folder as the snapshot's
version. The search that follows then finds the warnings of the current code. Nothing else is done
with the local result.

It takes tens of minutes, so it runs as a detached process (``ct.py initial-run``) that writes
``analysis/status.json``; ``ct.py tick`` waits for it. When a step fails, the run stops before the
triage (the warnings would be the old ones). Only the commands written in the settings are run.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .common import CtError, pid_alive, read_json, resolve_path, spawn_detached, write_json
from .envvars import get_env
from .run import Run
from .verify import LOG_TAIL, intermediate_dir, safe_relpath

STATUS_FILE = "status.json"
LOG_FILE = "initial.log"
MARK = "__CT_STEP__"
STEPS = {"setup": "環境の設定", "build": "ビルド（cov-build）", "analyze": "解析（cov-analyze）",
         "commit": "サーバへの登録（cov-commit-defects）"}


def folder(run: Run) -> Path:
    return run.dir / "analysis"


def _status_file(run: Run) -> Path:
    return folder(run) / STATUS_FILE


def _tail(log: Path) -> str:
    return log.read_bytes()[-LOG_TAIL:].decode("utf-8", "replace") if log.is_file() else ""


def status(run: Run) -> dict[str, Any]:
    """none / running / done / failed, with the step, the seconds and (on failure) the end of the log."""
    data = read_json(_status_file(run), {}) if _status_file(run).is_file() else {}
    if not data:
        return {"status": "none"}
    if data.get("status") == "running" and not pid_alive(data.get("pid")) and \
            time.time() - data.get("started", 0) > 30:  # the process is gone without writing its end
        data = {**data, "status": "failed", "error": "初回解析のプロセスが途中で止まりました"}
    data["elapsed_seconds"] = round((data.get("finished") or time.time()) - data.get("started", time.time()))
    data["step_label"] = STEPS.get(data.get("step", ""), data.get("step", ""))
    if data["status"] == "failed":
        data["log_tail"] = _tail(folder(run) / LOG_FILE)
    return data


def start(run: Run) -> dict[str, Any]:
    """Start the initial analysis in the background (it keeps running when the command returns)."""
    if status(run)["status"] == "running":
        return status(run)
    config = run.config.verify
    if not config.build_command:
        raise CtError("初回解析の設定（ビルドのコマンド）がありません（/coverity-setup の「ビルドでの検証」で設定します）")
    folder(run).mkdir(parents=True, exist_ok=True)
    write_json(_status_file(run), {"status": "running", "step": "setup", "started": time.time(), "pid": None})
    script = resolve_path(__file__).parents[1] / "ct.py"
    with (folder(run) / "process.log").open("wb") as log:
        proc = spawn_detached([sys.executable, str(script), "initial-run", "--run", str(run.dir)], dict(os.environ),
                              run.dir, log)
    data = read_json(_status_file(run), {})
    if data.get("status") == "running" and not data.get("pid"):
        write_json(_status_file(run), {**data, "pid": proc.pid})
    from .triage import _log  # late import: triage is the larger module

    _log(run, "initial-start", pid=proc.pid)
    return status(run)


def wait(run: Run, seconds: int) -> dict[str, Any]:
    """Wait while the initial analysis runs, at most ``seconds`` (one tick of the orchestrating agent)."""
    deadline = time.monotonic() + max(0, seconds)
    current = status(run)
    while current["status"] == "running" and time.monotonic() < deadline:
        time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        current = status(run)
    return current


def command_line(run: Run, root: Path) -> tuple[str, dict[str, str]]:
    """One shell line (setup → build → analyze → commit) and the environment it needs.

    The authentication key goes in an environment variable (``COVERITY_PASSPHRASE``, which
    cov-commit-defects reads when --password is not given), never on the command line or in the log."""
    config, coverity = run.config.verify, run.config.coverity
    steps = []
    if config.setup_command:
        setup = config.setup_command.replace("{root}", str(root))
        steps.append(("call " if sys.platform == "win32" else "") + setup)
    if config.build_dir:
        target = root / safe_relpath(config.build_dir)
        steps.append(f'cd /d "{target}"' if sys.platform == "win32" else f'cd "{target}"')
    idir = intermediate_dir(config.cov_analyze_args)
    revision = run.vcs.current_revision()
    user = get_env(coverity.user_env) or ""
    steps += [f"echo {MARK}build", f"cov-build {config.cov_build_args} {config.build_command}",
              f"echo {MARK}analyze", f"cov-analyze {config.cov_analyze_args}", f"echo {MARK}commit"]
    if coverity.api == "fake":  # the self-test and the tests: nothing is sent anywhere
        steps.append(f"echo commit skipped because coverity.api is fake: stream {run.state.stream} version {revision}")
    else:
        if not user or not get_env(coverity.key_env):
            raise CtError(f"サーバへの登録に使う認証情報がありません（環境変数 {coverity.user_env} / {coverity.key_env}。"
                          "/coverity-setup で入れます）")
        commit = (f'cov-commit-defects --dir "{idir}" --url "{coverity.url}" --stream "{run.state.stream}" '
                  f'--user "{user}" --version "{revision}"')
        if config.cov_commit_args:
            commit += " " + config.cov_commit_args
        steps.append(commit)
    env = dict(os.environ)
    key = get_env(coverity.key_env)
    if key:
        env["COVERITY_PASSPHRASE"] = key
    return " && ".join(steps), env


def _stop_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def execute(run_dir: str) -> dict[str, Any]:
    """The body of the detached process (ct.py initial-run)."""
    run = Run(run_dir)
    log = folder(run) / LOG_FILE
    started = read_json(_status_file(run), {}).get("started") or time.time()
    base = {"started": started, "pid": os.getpid()}
    write_json(_status_file(run), {**base, "status": "running", "step": "setup"})
    timeout = run.config.verify.timeout_minutes * 60
    try:
        line, env = command_line(run, run.repo)
    except CtError as exc:
        out = {**base, "status": "failed", "step": "setup", "error": str(exc), "finished": time.time()}
        write_json(_status_file(run), out)
        return out
    with open(log, "wb") as f:
        f.write(f"$ {line}\n".encode("utf-8"))
        f.flush()
        extra: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                                 else {"start_new_session": True})
        proc = subprocess.Popen(line, cwd=run.repo, shell=True, stdout=f, stderr=subprocess.STDOUT, env=env, **extra)
        timed_out = False
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() > deadline:
                timed_out = True
                _stop_tree(proc)
                break
            step = _step(log)
            if step:
                write_json(_status_file(run), {**base, "status": "running", "step": step})
            time.sleep(2)
        proc.wait()
    step = _step(log) or "setup"
    if timed_out:
        out = {**base, "status": "failed", "step": step, "finished": time.time(),
               "error": f"{run.config.verify.timeout_minutes} 分たっても終わらなかったため止めました（設定 verify.timeout_minutes）"}
    elif proc.returncode != 0:
        out = {**base, "status": "failed", "step": step, "finished": time.time(),
               "error": f"{STEPS.get(step, step)}で失敗しました（終了コード {proc.returncode}）"}
    else:
        out = {**base, "status": "done", "step": "commit", "finished": time.time(),
               "revision": run.vcs.current_revision(), "stream": run.state.stream}
    write_json(_status_file(run), out)
    from .triage import _log

    _log(run, "initial", level="critical" if out["status"] == "failed" else "info", status=out["status"],
         step=step, error=out.get("error", ""), seconds=round(out["finished"] - started))
    return out


def _step(log: Path) -> str | None:
    """The last step the shell line reached (from the marks it echoes into the log)."""
    if not log.is_file():
        return None
    found = re.findall(rf"^{MARK}(\w+)", log.read_bytes()[-200_000:].decode("utf-8", "replace"), re.MULTILINE)
    return found[-1] if found else "setup"
