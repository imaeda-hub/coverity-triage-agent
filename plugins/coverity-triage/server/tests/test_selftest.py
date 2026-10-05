"""/coverity-selftest: result folder, masking and every step (spec D-81)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
import test_connect
from test_connect import FakeServer
from test_verify import BUILD_PY, FAKE_COV

from coverity_triage import config as cfg
from coverity_triage import connect, knowledge, mcp_server, onboarding, runs, selftest, worker

FAKE_TOOLS = FAKE_COV.replace(
    'if tool == "cov-format-errors":',
    'if tool == "cov-format-errors" and "--help" in args:\n'
    '    print("--json-output-v7 <file>")\n'
    '    sys.exit(0)\n'
    'if tool == "cov-format-errors":')


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout.decode()


def state(result_dir):
    return json.loads((Path(result_dir) / selftest.STATE_FILE).read_text(encoding="utf-8"))


def status(result_dir, check_id):
    return state(result_dir)["checks"][check_id]["status"]


def test_start_skips_sections_without_setup(tmp_path):
    out = selftest.start(["①", "3"], repo_root=str(tmp_path), client="VS Code", out_dir=str(tmp_path / "out"))
    assert out["sections"] == {"1": "プラグインの組み込み", "3": "社内 Coverity 接続（読み取りのみ）"}
    assert out["checks_to_run"] == [f"1-{n}" for n in range(1, 9)]
    assert set(out["skipped"]) == {f"3-{n}" for n in range(1, 10)}
    report = Path(out["report"]).read_text(encoding="utf-8")
    assert "| 3-1 | 準備状況（doctor） | B-2 | 未実施 |" in report
    assert "## 2." not in report  # not selected
    with pytest.raises(ValueError, match="①〜④"):
        selftest.start(["5"], out_dir=str(tmp_path / "out"))


def test_record_masks_credentials_users_and_hosts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    onboarding.write_project_config(str(repo), "https://cov.corp.example:8443", "P", "S", "git")
    monkeypatch.setenv("COV_USER", "alice")
    monkeypatch.setenv("COV_AUTH_KEY", "s3cret-key")
    result_dir = selftest.start(["1"], str(repo), out_dir=str(tmp_path / "out"))["result_dir"]
    selftest.record(result_dir, "1-3", "fail", "alice が cov.corp.example で s3cret-key を見た",
                    raw="C:\\Users\\alice\\x と COV.CORP.EXAMPLE")
    entry = state(result_dir)["checks"]["1-3"]
    assert entry["actual"] == "<user> が <coverity-host> で **** を見た"
    raw = (Path(result_dir) / entry["raw"]).read_text(encoding="utf-8")
    assert raw == "C:\\Users\\<user>\\x と <coverity-host>"
    assert "alice" not in (Path(result_dir) / "report.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="確認項目"):
        selftest.record(result_dir, "9-9", "pass", "x")
    with pytest.raises(ValueError, match="status"):
        selftest.record(result_dir, "1-3", "ok", "x")


def test_plugin_and_worker_steps(tmp_path, monkeypatch):
    result_dir = selftest.start(["1"], out_dir=str(tmp_path / "out"))["result_dir"]
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", sys.prefix)
    names = list(mcp_server.TOOL_NAMES)
    assert len(names) == 28 and "selftest_step" in names
    visible = ", ".join(f"coverity-triage/{n}" for n in names)
    selftest.step("plugin", result_dir, answer=visible, tool_names=names)
    assert status(result_dir, "1-1") == "pass"
    selftest.step("plugin", result_dir, answer=visible.replace("coverity-triage/doctor", ""), tool_names=names)
    assert "doctor" in state(result_dir)["checks"]["1-1"]["actual"]

    heading = selftest.worker_probe_heading()
    assert heading
    tools = ", ".join(["read", "skill"] + [f"coverity-triage/{t}" for t in selftest.WORKER_TOOLS])
    selftest.step("worker", result_dir, answer=f"TOOLS: {tools}\nSKILL: {heading}")
    assert [status(result_dir, c) for c in ("1-5", "1-6", "1-7")] == ["pass", "pass", "pass"]
    selftest.step("worker", result_dir, answer=f"TOOLS: {tools}, run_in_terminal\nSKILL: 読めない")
    assert [status(result_dir, c) for c in ("1-6", "1-7")] == ["fail", "fail"]
    assert "run_in_terminal" in state(result_dir)["checks"]["1-6"]["actual"]
    assert "申告に基づく" in state(result_dir)["checks"]["1-6"]["detail"]
    selftest.step("worker", result_dir, answer="")
    assert status(result_dir, "1-5") == "fail"


# ② fake data flow --------------------------------------------------------------------------------


def triage_result(item, recommendation):
    attrs = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified"}
    return {"work_item": item,
            "verdict": {"judgement": "false_positive", "summary": "s", "rationale": "r"},
            "recommendation": recommendation, "confidence": "high", "confidence_reason": "根拠あり",
            "deviation": {**attrs, "comment": "誤検知。"},
            "fix": {"classification": "Bug", "action": "Fix Required", "severity": "Major",
                    "summary": "チェックを追加", "impact": "この関数のみ"},
            "revision_drift": {"status": "none"}}


EDITS = {"20001": ("    if (fgets(out, BUF_SIZE, fp) == NULL) {\n        return -1;",
                   "    if (fgets(out, BUF_SIZE, fp) == NULL) {\n        fclose(fp);\n        return -1;"),
         "20002": ("    FILE *fp = fopen(path, \"r\");", "    FILE *fp = path ? fopen(path, \"r\") : NULL;"),
         "20003": ("    int c;\n    buf[0]", "    int c;\n    if (buf == NULL) {\n        return -1;\n    }\n    buf[0]"),
         "G1": ("    unsigned char a = value;", "    unsigned char a = (unsigned char)value;")}


def act_as_worker(run_dir, recommendations):
    while (item := runs.next_work_item(run_dir)["item"]) is not None:
        worker.prepare_workspaces(run_dir, item)
        old, new = EDITS[item]
        worker.edit_source(run_dir, item, "fix", "src/reader.c", old, new)
        worker.save_fix(run_dir, item, "fix", f"fix {item}")
        worker.submit_result(run_dir, item, triage_result(item, recommendations.get(item, "deviation")))


def test_fake_data_flow(tmp_path):
    result_dir = selftest.start(["2"], out_dir=str(tmp_path / "out"))["result_dir"]
    sample = selftest.step("sample", result_dir)
    repo = sample["repo_root"]
    assert sh("git", "status", "--porcelain", cwd=repo) == ""
    run_dir = runs.start_run(repo, sample["filter_file"])["run_dir"]
    assert Path(run_dir).is_relative_to(Path(result_dir))  # output stays in the result folder
    act_as_worker(run_dir, {"20001": "fix", "20002": "fix", "20003": "fix"})  # 20002 differs from expected
    runs.build_summary(run_dir)

    selftest.step("flow_run", result_dir, run_dir=run_dir)
    checks = state(result_dir)["checks"]
    assert [checks[c]["status"] for c in ("2-1", "2-2", "2-3", "2-4", "2-5")] == \
        ["pass", "pass", "pass", "pass", "review"]
    assert "20002" in checks["2-5"]["actual"]
    assert (Path(result_dir) / "raw" / "2-3-G1.md").is_file()

    out = selftest.step("flow_apply", result_dir, run_dir=run_dir)
    assert status(result_dir, "2-6") == "pass"
    assert status(result_dir, "2-7") == "pass", state(result_dir)["checks"]["2-7"]
    assert {c["item"] for c in out["knowledge_candidates"]} == {"20002", "G1"}
    writes = (Path(repo) / ".coverity-triage" / "fake-issues.yaml.writes.jsonl").read_text(encoding="utf-8")
    assert "テストで手直しした一文" in writes

    selftest.step("flow_knowledge", result_dir, run_dir=run_dir)
    assert status(result_dir, "2-8") == "fail"  # nothing added yet
    knowledge.add_knowledge(run_dir, ["path は呼び出し元の read_config で検証済み"])
    selftest.step("flow_knowledge", result_dir, run_dir=run_dir)
    assert [status(result_dir, c) for c in ("2-8", "2-9")] == ["pass", "pass"]
    report = (Path(result_dir) / "report.md").read_text(encoding="utf-8")
    assert "| 2-5 | AI の結論 | A-5 | 要確認 |" in report and "### 2-5 AI の結論（要確認）" in report


# ③ Coverity Connect ---------------------------------------------------------------------------------

WSDL = '<definitions><operation name="{}"/></definitions>'


class SelftestServer(FakeServer):
    """Adds the WSDL, getVersion and sourceCodeInfo to the fake server of test_connect."""

    def __call__(self, request):
        path = request.url.path
        if request.method == "GET" and path.startswith("/ws/v9/"):
            service = path.rsplit("/", 1)[1]
            return httpx.Response(200, text="".join(WSDL.format(op) for op in selftest.SOAP_OPERATIONS[service]))
        if "getVersion" in request.content.decode(errors="replace"):
            return httpx.Response(200, text=(
                '<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/"><S:Body>'
                '<ns2:getVersionResponse xmlns:ns2="http://ws.coverity.com/v9"><return>'
                "<externalVersion>2024.6.1</externalVersion></return></ns2:getVersionResponse></S:Body></S:Envelope>"))
        if path == "/api/v2/issues/sourceCodeInfo":
            return httpx.Response(200, json={"events": [{"line": 12, "code": "secret code"}]})
        return super().__call__(request)


@pytest.fixture
def connect_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    onboarding.write_project_config(str(repo), "https://cov.example:8443", "P", "main", "git")
    monkeypatch.setenv("COV_USER", "alice")
    monkeypatch.setenv("COV_AUTH_KEY", "secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    server = SelftestServer()
    snapshot = sh("git", "rev-parse", "HEAD", cwd=repo).strip()
    monkeypatch.setattr(test_connect, "SNAPSHOT_INFO", test_connect.SNAPSHOT_INFO.replace("a1b2c3d", snapshot))

    def client(config, transport=None):
        return connect.ConnectClient(config, transport=httpx.MockTransport(server))

    monkeypatch.setattr(selftest, "ConnectClient", client)
    monkeypatch.setattr(onboarding, "make_client", lambda config: client(config))
    return repo


def test_coverity_step(connect_repo, tmp_path):
    repo = connect_repo
    result_dir = selftest.start(["3"], str(repo), out_dir=str(tmp_path / "out"))["result_dir"]
    out = selftest.step("coverity", result_dir)
    checks = state(result_dir)["checks"]
    assert checks["3-1"]["status"] == "review"  # no GitHub token: warning only
    assert [checks[f"3-{n}"]["status"] for n in range(2, 8)] == ["pass", "review", "pass", "pass", "pass", "info"]
    assert "displayCategory" in checks["3-3"]["actual"]  # optional column missing on the fake server
    assert checks["3-5"]["actual"] == "バージョン 2024.6.1"
    raw = (Path(result_dir) / checks["3-7"]["raw"]).read_text(encoding="utf-8")
    assert "secret code" not in raw and '"code": "str"' in raw  # only the shape is kept
    assert checks["3-8"]["status"] == "pass"
    assert out["filter_file"] == "untriaged.yaml"
    assert "cov.example" not in (Path(result_dir) / "report.md").read_text(encoding="utf-8")


def test_real_run_records_report_shape_only(tmp_path):
    result_dir = selftest.start(["2"], out_dir=str(tmp_path / "out"))["result_dir"]
    repo = selftest.step("sample", result_dir)["repo_root"]
    run_dir = runs.start_run(repo, "all.yaml", {"max_items": 1})["run_dir"]
    act_as_worker(run_dir, {})
    runs.build_summary(run_dir)
    selftest.step("real_run", result_dir, run_dir=run_dir)
    entry = state(result_dir)["checks"]["3-9"]
    assert entry["status"] == "pass"
    raw = json.loads((Path(result_dir) / entry["raw"]).read_text(encoding="utf-8"))
    shape = raw["report_shapes"]["20001"]
    assert shape["events"] == 3 and shape["deviation_comment"] and shape["fix_diff"]
    assert "誤検知" not in json.dumps(raw, ensure_ascii=False)  # no report text


# ④ build verification ---------------------------------------------------------------------------------


@pytest.fixture
def build_repo(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_cov.py").write_text(FAKE_TOOLS, encoding="utf-8")
    for tool in selftest.COV_COMMANDS:
        if sys.platform == "win32":
            (bin_dir / f"{tool}.cmd").write_text(f'@"{sys.executable}" "{bin_dir / "fake_cov.py"}" {tool} %*\n',
                                                 encoding="utf-8")
        else:
            script = bin_dir / tool
            script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_cov.py"}" {tool} "$@"\n',
                              encoding="utf-8")
            script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text("int f1(void)\n{\n    return 1; /* WARN:NULL_RETURNS:f1 */\n}\n", encoding="utf-8")
    (repo / "build.py").write_text(BUILD_PY, encoding="utf-8")
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    conf = cfg.config_dir(repo)
    (conf / "filters").mkdir(parents=True)
    (conf / "fake.yaml").write_text(yaml.safe_dump({"issues": [
        {"cid": 1, "project": "P", "checker": "NULL_RETURNS", "file": "src/a.c", "function": "f1", "line": 3,
         "merge_key": "NULL_RETURNS:src/a.c:f1",
         "events": [{"file": "src/a.c", "line": 3, "tag": "deref", "main": True}]}]}), encoding="utf-8")
    (conf / "config.yaml").write_text(yaml.safe_dump({
        "coverity": {"url": "https://cov", "api": "fake", "fake_data": "fake.yaml"},
        "vcs": {"type": "git"}, "output_dir": str(tmp_path / "work-out"),
        "verify": {"build_command": f'"{sys.executable}" build.py',
                   "cov_build_args": "--dir idir", "cov_analyze_args": "--dir idir --all"}}), encoding="utf-8")
    (conf / "filters" / "all.yaml").write_text("project: P\n", encoding="utf-8")
    sh("git", "add", ".", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init", cwd=repo)
    return repo


def test_build_and_verify_steps(build_repo, tmp_path):
    repo = str(build_repo)
    assert selftest._not_ready("4", repo) == "設定が偽データ（coverity.api: fake）のため"
    result_dir = selftest.start(["4"], repo, out_dir=str(tmp_path / "out"))["result_dir"]
    out = selftest.step("build", result_dir, repo_root=repo)
    assert out["ready"]
    assert [status(result_dir, c) for c in ("4-1", "4-2", "4-3")] == ["pass", "pass", "pass"]

    run_dir = runs.start_run(repo, "all.yaml")["run_dir"]
    item = runs.next_work_item(run_dir)["item"]
    worker.edit_source(run_dir, item, "fix", "src/a.c", "    return 1; /* WARN:NULL_RETURNS:f1 */", "    return 1;")
    worker.save_fix(run_dir, item, "fix", "fix")
    worker.submit_result(run_dir, item, triage_result(item, "fix"))
    selftest.step("verify", result_dir, run_dir=run_dir)
    assert status(result_dir, "4-4") == "fail"  # verify_run has not run yet
    runs.verify_run(run_dir, "build+analyze")
    selftest.step("verify", result_dir, run_dir=run_dir)
    entry = state(result_dir)["checks"]["4-4"]
    assert entry["status"] == "pass", entry


def test_error_lines_skip_echoed_source(tmp_path):
    log = tmp_path / "b.log"
    log.write_text("a.c:3:5: error: 'x' undeclared\n    3 |     return x;\n      |            ^\nok\n", encoding="utf-8")
    assert selftest._error_lines(log) == ["a.c:3:5: error: 'x' undeclared"]


def test_skill_assets_match_expectations():
    expected = yaml.safe_load((selftest.ASSETS / "expected.yaml").read_text(encoding="utf-8"))
    fake = yaml.safe_load((selftest.ASSETS / "sample-target" / ".coverity-triage" / "fake-issues.yaml")
                          .read_text(encoding="utf-8"))
    cids = {str(i["cid"]) for i in fake["issues"]}
    assert set(expected["items"]) | {str(c) for c in expected["group"]["cids"]} == cids
