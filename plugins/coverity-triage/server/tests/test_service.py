"""End-to-end flow with the fake Coverity client: run -> worker -> summary -> apply."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coverity_triage import service
from coverity_triage.service import ServiceError

SOURCE = (
    "#include <stddef.h>\n"
    "char *get_buf(void);\n"
    "int read_all(void)\n"
    "{\n"
    "    char *buf = get_buf();\n"
    "    return buf[0];\n"
    "}\n"
    "int other(void)\n"
    "{\n"
    "    char *buf = get_buf();\n"
    "    return buf[1];\n"
    "}\n"
)


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout.decode()


def fake_issue(cid, function, line):
    return {"cid": cid, "project": "P", "stream": "S", "checker": "NULL_RETURNS",
            "file": "src/sample.c", "function": function, "line": line, "impact": "High",
            "status": "New", "classification": "Unclassified", "action": "Undecided",
            "events": [{"file": "src/sample.c", "line": line, "tag": "dereference",
                        "description": "deref", "main": True}]}


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "sample.c").write_text(SOURCE, encoding="utf-8")
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    sh("git", "config", "user.email", "t@e", cwd=repo)
    service.init_project(str(repo), "git")
    conf = repo / ".coverity-triage"
    (conf / "fake-issues.yaml").write_text(yaml.safe_dump({
        "snapshot": {"version": ""},
        "issues": [fake_issue(1, "read_all", 6), fake_issue(2, "read_all", 5), fake_issue(3, "other", 11)],
    }), encoding="utf-8")
    config = yaml.safe_load((conf / "config.yaml").read_text(encoding="utf-8"))
    config["output_dir"] = str(tmp_path / "out")
    (conf / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    (conf / "filters" / "all.yaml").write_text("project: P\n", encoding="utf-8")
    sh("git", "add", ".", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    return repo


def result(item, recommendation="deviation", confidence="high", excluded=()):
    attrs = {"classification": "False Positive", "action": "Ignore", "severity": "Unspecified"}
    return {
        "work_item": item,
        "verdict": {"judgement": "false_positive", "summary": "呼び出し元で保証", "rationale": "経路を追跡した",
                    "evidence": [{"file": "src/sample.c", "line": 5, "note": "x"}]},
        "recommendation": recommendation, "confidence": confidence, "confidence_reason": "根拠がコードにある",
        "deviation": {**attrs, "comment": "誤検知。呼び出し元で保証されている。"},
        "fix": {"classification": "Bug", "action": "Fix Submitted", "severity": "Major",
                "summary": "NULL チェックを追加", "impact": "read_all のみ"},
        "revision_drift": {"status": "unknown"},
        "group_excluded_cids": list(excluded),
    }


def work(run_dir, item_id, old, new, **kwargs):
    service.prepare_workspaces(run_dir, item_id)
    service.edit_source(run_dir, item_id, "fix", "src/sample.c", old, new)
    service.save_fix(run_dir, item_id, "fix", f"Fix {item_id}")
    return service.submit_result(run_dir, item_id, result(item_id, **kwargs))


def test_full_flow(repo):
    started = service.start_run(str(repo), "all.yaml")
    run_dir = started["run_dir"]
    assert (started["found"], started["items"], started["groups"]) == (3, 2, 1)
    assert started["analyzed_revision_source"] == "local"

    first = service.next_work_item(run_dir)
    assert first["item"] == "G1" and first["cids"] == [1, 2]
    detail = service.get_issue_detail(run_dir, "G1")
    assert detail["details"][0]["events"][0]["tag"] == "dereference"
    prep = service.prepare_workspaces(run_dir, "G1")
    assert "drift_check" in prep
    lines = service.read_source(run_dir, "G1", "analyzed", "src/sample.c", 5, 6)
    assert lines["content"].startswith("5:     char *buf")
    hits = service.search_source(run_dir, "G1", "analyzed", r"get_buf\(\)")
    assert len(hits["hits"]) == 2
    with pytest.raises(ServiceError):
        service.edit_source(run_dir, "G1", "analyzed", "src/sample.c", "a", "b")
    with pytest.raises(ServiceError, match="修正案のコード"):
        service.submit_result(run_dir, "G1", result("G1"))
    out = work(run_dir, "G1", "    return buf[0];", "    return buf ? buf[0] : 0;", excluded=[2])
    assert out["split_into_new_items"] == ["2"]

    second = service.next_work_item(run_dir)
    assert second["item"] == "3"
    work(run_dir, "3", "    return buf[1];", "    return buf ? buf[1] : 0;",
         recommendation="fix", confidence="low")
    third = service.next_work_item(run_dir)
    assert third["item"] == "2"
    service.report_error(run_dir, "2", "ソースが見つからない")
    assert service.next_work_item(run_dir)["item"] is None

    summary = service.build_summary(run_dir)
    text = Path(summary["summary"]).read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if l.startswith("| 修正") or l.startswith("| 逸脱")]
    assert rows[0].startswith("| 修正 | 3 |") and rows[1].startswith("| 逸脱 | G1（1 件） |")
    assert "| 2 | ソースが見つからない |" in text

    # person edits the deviation comment and approves; rebuilding keeps the approval
    report = Path(run_dir) / "cid" / "G1.md"
    report.write_text(report.read_text(encoding="utf-8").replace("呼び出し元で保証されている。", "呼び出し元 main() で保証。"), encoding="utf-8")
    Path(summary["summary"]).write_text(text.replace("| 修正 | 3 |", "| 却下 | 3 |"), encoding="utf-8")
    service.build_summary(run_dir)
    assert "| 却下 | 3 |" in Path(summary["summary"]).read_text(encoding="utf-8")

    preview = service.preview_apply(run_dir)
    assert preview["counts"] == {"修正": 0, "逸脱": 1, "却下": 1}
    with pytest.raises(ServiceError, match="preview_apply"):
        service.apply_approvals(run_dir, "wrong")
    applied = service.apply_approvals(run_dir, preview["confirmation_token"])
    assert all(r["ok"] for r in applied["results"])

    writes = (repo / ".coverity-triage" / "fake-issues.yaml.writes.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(writes[0]) == {"cids": [1], "classification": "False Positive", "action": "Ignore",
                                     "severity": "Unspecified", "comment": "誤検知。呼び出し元 main() で保証。"}
    # already applied items are not applied twice
    assert service.preview_apply(run_dir)["items"] == []

    stats = service.get_stats(str(Path(run_dir).parent))
    assert stats["approvals"] == {"fix": 0, "deviation": 1, "reject": 1}
    assert stats["recommendation_adopted_rate"] == 0.5
    assert stats["deviation_edited_rate"] == 1.0
    # only the fake client's write log appears; the user's code is untouched
    assert sh("git", "status", "--porcelain", cwd=repo) == "?? .coverity-triage/fake-issues.yaml.writes.jsonl\n"


def test_resume_retries_errors(repo):
    run_dir = service.start_run(str(repo), "all.yaml")["run_dir"]
    item = service.next_work_item(run_dir)["item"]
    service.report_error(run_dir, item, "x")
    resumed = service.resume_run(run_dir)
    assert resumed["reset"] == {"in_progress": 0, "error": 1}
    assert service.next_work_item(run_dir)["item"] == item


def test_fix_approval_pushes_branch_and_creates_pr(repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    sh("git", "init", "-q", "--bare", str(remote), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(remote), cwd=repo)
    sh("git", "push", "-q", "origin", "main", cwd=repo)
    created = {}

    def fake_pr(self, branch, title, body):
        created.update(branch=branch, title=title)
        return "https://github.example/pr/1"

    monkeypatch.setattr(service.GitVcs, "create_pull_request", fake_pr)
    run_dir = service.start_run(str(repo), "all.yaml", {"checkers": ["NULL_RETURNS"]})["run_dir"]
    service.next_work_item(run_dir)
    work(run_dir, "G1", "    return buf[0];", "    return buf ? buf[0] : 0;", recommendation="fix")
    service.build_summary(run_dir)
    token = service.preview_apply(run_dir)["confirmation_token"]
    out = service.apply_approvals(run_dir, token)["results"][0]
    assert out["ok"] and out["fix"]["pull_request"] == "https://github.example/pr/1"
    assert created["branch"] == "coverity-fix/" + Path(run_dir).name + "-G1"
    assert "coverity-fix/" in sh("git", "ls-remote", "--heads", str(remote), cwd=repo)
