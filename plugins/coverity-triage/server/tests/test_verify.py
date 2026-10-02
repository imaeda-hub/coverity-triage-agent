"""Batch verification (spec D-72 to D-75) with fake cov-build / cov-analyze / cov-format-errors.

The fake analyzer reports one issue per source line containing ``WARN:<checker>:<function>``.
The fake build fails when a source file contains ``SYNTAX_ERROR``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coverity_triage import config as cfg
from coverity_triage import service
from coverity_triage.models import Issue
from coverity_triage.verify import intermediate_dir, load_analysis, matches

FAKE_COV = r'''
import json, os, subprocess, sys
tool = sys.argv[1]
args = sys.argv[2:]
if tool == "cov-build":
    rest = args[2:] if args[:1] == ["--dir"] else args
    sys.exit(subprocess.call(" ".join(rest), shell=True))
if tool == "cov-analyze":
    sys.exit(0)
if tool == "cov-format-errors":
    out = args[args.index("--json-output-v7") + 1]
    issues = []
    for dirpath, dirs, files in os.walk("."):
        for name in files:
            if not name.endswith(".c"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ".").replace(os.sep, "/")
            for n, line in enumerate(open(path, encoding="utf-8"), 1):
                if "WARN:" in line:
                    checker, func = line.split("WARN:")[1].split("*/")[0].strip().split(":")
                    issues.append({"mergeKey": f"{checker}:{rel}:{func}", "checkerName": checker,
                                   "mainEventFilePathname": os.path.abspath(path),
                                   "functionDisplayName": func, "mainEventLineNumber": n})
    json.dump({"issues": issues}, open(out, "w", encoding="utf-8"))
'''

BUILD_PY = r'''
import os, sys
for dirpath, dirs, files in os.walk("."):
    for name in files:
        if name.endswith(".c") and "SYNTAX_ERROR" in open(os.path.join(dirpath, name), encoding="utf-8").read():
            print(f"{os.path.join(dirpath, name)}:1: error: syntax error")
            sys.exit(1)
print("build ok")
'''

A_C = ("int f1(void)\n{\n    return 1; /* WARN:NULL_RETURNS:f1 */\n}\n"
       "int f2(void)\n{\n    return 2; /* WARN:RESOURCE_LEAK:f2 */\n}\n")
B_C = "int g(void)\n{\n    return 3;\n}\n"


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_cov.py").write_text(FAKE_COV, encoding="utf-8")
    for tool in ("cov-build", "cov-analyze", "cov-format-errors"):
        if sys.platform == "win32":
            (bin_dir / f"{tool}.cmd").write_text(
                f'@"{sys.executable}" "{bin_dir / "fake_cov.py"}" {tool} %*\n', encoding="utf-8")
        else:
            script = bin_dir / tool
            script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_cov.py"}" {tool} "$@"\n',
                              encoding="utf-8")
            script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir


@pytest.fixture
def repo(tmp_path, fake_tools):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_bytes(A_C.encode())
    (repo / "src" / "b.c").write_bytes(B_C.encode())
    (repo / "build.py").write_text(BUILD_PY, encoding="utf-8")
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    sh("git", "config", "user.email", "t@e", cwd=repo)
    sh("git", "config", "core.autocrlf", "false", cwd=repo)
    conf = cfg.config_dir(repo)
    (conf / "filters").mkdir(parents=True)
    issues = [
        {"cid": 1, "project": "P", "checker": "NULL_RETURNS", "file": "src/a.c", "function": "f1",
         "line": 3, "merge_key": "NULL_RETURNS:src/a.c:f1"},
        {"cid": 2, "project": "P", "checker": "RESOURCE_LEAK", "file": "src/b.c", "function": "g",
         "line": 3, "merge_key": "RESOURCE_LEAK:src/a.c:f2"},
    ]
    (conf / "fake.yaml").write_text(yaml.safe_dump({"issues": issues}), encoding="utf-8")
    setup = f'"{sys.executable}" -c "import sys; open(sys.argv[1] + \'/setup_ok.txt\', \'w\').write(\'ok\')" "{{root}}"'
    (conf / "config.yaml").write_text(yaml.safe_dump({
        "coverity": {"url": "https://cov", "api": "fake", "fake_data": "fake.yaml"},
        "vcs": {"type": "git"}, "output_dir": str(tmp_path / "out"),
        "verify": {"default": "build+analyze", "setup_command": setup,
                   "build_command": f'"{sys.executable}" build.py',
                   "cov_build_args": "--dir idir", "cov_analyze_args": "--dir idir --all"},
    }), encoding="utf-8")
    (conf / "filters" / "all.yaml").write_text("project: P\n", encoding="utf-8")
    sh("git", "add", ".", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    return repo


def result(item, confidence="high"):
    attrs = {"classification": "Bug", "action": "Fix Required", "severity": "Minor"}
    return {"work_item": item,
            "verdict": {"judgement": "true_bug", "summary": "s", "rationale": "r"},
            "recommendation": "fix", "confidence": confidence, "confidence_reason": "根拠あり",
            "deviation": {**attrs, "comment": "c"}, "fix": {**attrs, "summary": "s", "impact": "i"},
            "revision_drift": {"status": "none"}}


def triage(run_dir, edits):
    """Act as the worker for each item: edits = {item_id: (path, old, new)}."""
    while True:
        item = service.next_work_item(run_dir)["item"]
        if item is None:
            return
        path, old, new = edits[item]
        service.edit_source(run_dir, item, "fix", path, old, new)
        service.save_fix(run_dir, item, "fix", f"fix {item}")
        service.submit_result(run_dir, item, result(item))


def test_batch_reanalysis_attributes_results(repo):
    run_dir = service.start_run(str(repo), "all.yaml")["run_dir"]
    triage(run_dir, {
        "1": ("src/a.c", "return 1; /* WARN:NULL_RETURNS:f1 */", "return 1;"),          # resolves CID 1
        "2": ("src/b.c", "    return 3;", "    return 3; /* WARN:NEW_CHECK:g */"),     # CID 2 stays, new warning
    })
    out = service.verify_run(run_dir)
    assert out["build_ok"] is True and out["downgraded_to_low"] == ["2"]

    v1 = json.loads((Path(run_dir) / "results" / "1.verify.json").read_text(encoding="utf-8"))["fix"]
    assert v1["resolved_cids"] == [1] and v1["problems"] == []
    v2 = json.loads((Path(run_dir) / "results" / "2.verify.json").read_text(encoding="utf-8"))["fix"]
    assert v2["remaining_cids"] == [2] and v2["new_issue_count"] == 1
    assert (Path(run_dir) / "work" / "verify" / "after" / "setup_ok.txt").is_file()  # {root} replaced

    r2 = json.loads((Path(run_dir) / "results" / "2.json").read_text(encoding="utf-8"))
    assert r2["confidence"] == "low" and r2["confidence_before_verify"] == "high"
    assert "自動検証で問題" in r2["confidence_reason"]
    report = (Path(run_dir) / "cid" / "2.md").read_text(encoding="utf-8")
    assert "要確認: 警告が残っています" in report and "確信度: 低" in report

    service.build_summary(run_dir)
    summary = (Path(run_dir) / "summary.md").read_text(encoding="utf-8")
    assert "| 修正 | 2 | 修正 | 低 |" in summary and "警告残" in summary and "成功" in summary

    # running again keeps the original confidence as the base
    service.verify_run(run_dir)
    r2 = json.loads((Path(run_dir) / "results" / "2.json").read_text(encoding="utf-8"))
    assert r2["confidence_before_verify"] == "high" and r2["confidence_reason"].count("自動検証で問題") == 1


def test_build_failure_is_attributed_to_the_changed_file(repo):
    run_dir = service.start_run(str(repo), "all.yaml", verify_mode="build")["run_dir"]
    triage(run_dir, {
        "1": ("src/a.c", "return 1; /* WARN:NULL_RETURNS:f1 */", "return 1;"),
        "2": ("src/b.c", "    return 3;", "    SYNTAX_ERROR return 3;"),
    })
    out = service.verify_run(run_dir)
    assert out["build_ok"] is False and out["downgraded_to_low"] == ["2"]
    v1 = json.loads((Path(run_dir) / "results" / "1.verify.json").read_text(encoding="utf-8"))["fix"]
    assert v1["build_ok"] is None and "確認できませんでした" in v1["note"]


def test_conflicting_fixes_are_reported(repo):
    run_dir = service.start_run(str(repo), "all.yaml", verify_mode="build")["run_dir"]
    same_line = ("src/a.c", "    return 1;", "    return 10;")
    triage(run_dir, {"1": same_line, "2": ("src/a.c", "    return 1;", "    return 11;")})
    service.verify_run(run_dir)
    v2 = json.loads((Path(run_dir) / "results" / "2.verify.json").read_text(encoding="utf-8"))["fix"]
    assert v2["applied"] is False and "まとめた検証に含められません" in v2["problems"][0]


def test_trial_build_and_saving_settings(repo):
    ok = service.trial_build(str(repo), "", f'"{sys.executable}" build.py')
    assert ok["build_ok"] is True
    ng = service.trial_build(str(repo), "", f'"{sys.executable}" -c "raise SystemExit(3)"')
    assert ng["build_ok"] is False
    service.write_verify_config(str(repo), "envset.bat \"{root}\"", "make -f makefileXX", default="build")
    conf = cfg.load_project_config(repo)
    assert conf.verify.setup_command == 'envset.bat "{root}"' and conf.verify.default == "build"


def test_verify_none_does_nothing(repo):
    run_dir = service.start_run(str(repo), "all.yaml", verify_mode="none")["run_dir"]
    assert service.verify_run(run_dir)["mode"] == "none"


def test_analysis_parsing_and_matching(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"issues": [
        {"mergeKey": "k1", "checkerName": "NULL_RETURNS",
         "mainEventFilePathname": "C:\\work\\src\\a.c", "functionDisplayName": "f"}]}), encoding="utf-8")
    records = load_analysis(path)
    assert records[0]["file"] == "C:/work/src/a.c"
    assert matches(Issue(cid=1, checker="NULL_RETURNS", file="src/a.c", function="f"), records[0])
    assert matches(Issue(cid=1, checker="X", file="z.c", merge_key="k1"), records[0])
    assert not matches(Issue(cid=1, checker="NULL_RETURNS", file="src/b.c"), records[0])


def test_intermediate_dir():
    assert intermediate_dir("--dir idir --all") == "idir"
    assert intermediate_dir('--all --dir="C:\\cov idir"') == "C:\\cov idir"


def test_batch_build_with_svn_patches(tmp_path, fake_tools):
    svnrepo = tmp_path / "svnrepo"
    sh("svnadmin", "create", str(svnrepo), cwd=tmp_path)
    wc = tmp_path / "wc"
    sh("svn", "checkout", "-q", svnrepo.as_uri(), str(wc), cwd=tmp_path)
    (wc / "src").mkdir()
    (wc / "src" / "a.c").write_bytes(A_C.replace("\n", "\r\n").encode())
    (wc / "src" / "b.c").write_bytes(B_C.encode())
    (wc / "build.py").write_text(BUILD_PY, encoding="utf-8")
    conf = cfg.config_dir(wc)
    (conf / "filters").mkdir(parents=True)
    (conf / "fake.yaml").write_text(yaml.safe_dump({"issues": [
        {"cid": 1, "project": "P", "checker": "NULL_RETURNS", "file": "src/a.c", "function": "f1"},
        {"cid": 2, "project": "P", "checker": "X", "file": "src/b.c", "function": "g"}]}), encoding="utf-8")
    (conf / "config.yaml").write_text(yaml.safe_dump({
        "coverity": {"url": "https://cov", "api": "fake", "fake_data": "fake.yaml"},
        "vcs": {"type": "svn"}, "output_dir": str(tmp_path / "out"),
        "verify": {"default": "build+analyze", "build_command": f'"{sys.executable}" build.py',
                   "cov_analyze_args": "--dir idir"}}), encoding="utf-8")
    (conf / "filters" / "all.yaml").write_text("project: P\n", encoding="utf-8")
    sh("svn", "add", "-q", "src", "build.py", ".coverity-triage", cwd=wc)
    sh("svn", "commit", "-qm", "init", cwd=wc)
    sh("svn", "update", "-q", cwd=wc)

    run_dir = service.start_run(str(wc), "all.yaml")["run_dir"]
    triage(run_dir, {
        "1": ("src/a.c", "    return 1; /* WARN:NULL_RETURNS:f1 */", "    return 1;"),
        "2": ("src/b.c", "    return 3;", "    return 30;"),
    })
    out = service.verify_run(run_dir)
    assert out["build_ok"] is True and out["downgraded_to_low"] == []
    after = Path(run_dir) / "work" / "verify" / "after"
    assert (after / "src" / "a.c").read_bytes() == A_C.replace(" /* WARN:NULL_RETURNS:f1 */", "").replace("\n", "\r\n").encode()
    assert b"return 30;" in (after / "src" / "b.c").read_bytes()
    v1 = json.loads((Path(run_dir) / "results" / "1.verify.json").read_text(encoding="utf-8"))["fix"]
    assert v1["resolved_cids"] == [1]


def test_setup_command_passes_two_arguments(repo):
    """envset.bat takes two arguments (D-71): {root} is replaced, the other is passed as is."""
    setup = (f'"{sys.executable}" -c "import sys; open(sys.argv[1] + \'/args.txt\', \'w\').write(\'|\'.join(sys.argv[1:]))" '
             '"{root}" SECOND_ARG')
    result = service.trial_build(str(repo), setup, f'"{sys.executable}" build.py')
    assert result["build_ok"] is True
    written = (Path(result["built_in"]) / "args.txt").read_text(encoding="utf-8").split("|")
    assert written == [str(Path(result["built_in"])), "SECOND_ARG"]
