import subprocess

import pytest
import yaml

from coverity_triage import config as cfg
from coverity_triage import onboarding, service


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_bytes("/* 日本語 */\nint a;\n".encode("cp932"))
    (repo / "src" / "b.c").write_bytes("/* 日本語 */\nint b;\n".encode("cp932"))
    (repo / "src" / "c.c").write_bytes("/* 日本語 */\nint c;\n".encode("utf-8"))
    (repo / "src" / "d.c").write_bytes(b"int d;\n")
    sh("git", "init", "-q", "-b", "develop", cwd=repo)
    return repo


def test_detect_project(repo):
    found = onboarding.detect_project(str(repo))
    assert found["vcs_type"] == "git" and found["base_branch"] == "develop"
    assert found["ascii_file_encoding"] == "cp932"
    assert found["encoding_evidence"]["files_with_cp932"] == 2
    assert found["has_config"] is False


def test_write_config_then_doctor_and_run(repo, tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.load_project_config(repo)
    out = onboarding.write_project_config(str(repo), "https://cov:8443/", "P", "S", "git",
                                          "develop", "cp932", api="fake")
    assert len(out["written"]) == 3
    knowledge = repo / ".coverity-triage" / "knowledge.md"
    assert "## 推奨の方針" in knowledge.read_text(encoding="utf-8")
    knowledge.write_text("チームの知識\n", encoding="utf-8")
    conf = cfg.load_project_config(repo)
    assert conf.coverity.url == "https://cov:8443" and conf.ascii_file_encoding == "cp932"
    spec = cfg.load_filter(repo, "untriaged.yaml")
    assert spec.project == "P" and spec.streams == ["S"] and spec.max_items == 20
    with pytest.raises(ValueError, match="overwrite"):
        onboarding.write_project_config(str(repo), "https://x", "P", "S", "git")
    onboarding.write_project_config(str(repo), "https://cov:8443/", "P", "S", "git",
                                    "develop", "cp932", api="fake", overwrite=True)
    assert knowledge.read_text(encoding="utf-8") == "チームの知識\n"  # never overwritten

    # fake data is needed for api: fake; doctor reports the problem instead of crashing
    report = onboarding.doctor(str(repo))
    names = {c["check"]: c["status"] for c in report["checks"]}
    assert names["設定ファイル"] == "ok" and names["条件ファイル"] == "ok" and names["出力先"] == "ok"
    assert names["Coverity 接続"] == "warning" and names["Coverity から警告を取得"] == "ng"
    assert report["ready"] is False

    path = cfg.config_dir(repo) / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["coverity"]["fake_data"] = "fake.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    (cfg.config_dir(repo) / "fake.yaml").write_text(yaml.safe_dump({"issues": [
        {"cid": 1, "project": "P", "stream": "S", "checker": "X", "file": "src/a.c",
         "classification": "Unclassified"}]}), encoding="utf-8")
    report = onboarding.doctor(str(repo))
    fetched = next(c for c in report["checks"] if c["check"] == "Coverity から警告を取得")
    assert fetched["status"] == "ok" and fetched["detail"].startswith("1 件")

    assert onboarding.list_runs(str(repo))["runs"] == []
    sh("git", "add", ".", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i", cwd=repo)
    run_dir = service.start_run(str(repo), "untriaged.yaml")["run_dir"]
    runs = onboarding.list_runs(str(repo))["runs"]
    assert runs[0]["run_dir"] == run_dir and runs[0]["unfinished"] is True


def test_doctor_without_config(tmp_path):
    report = onboarding.doctor(str(tmp_path))
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["リポジトリ"] == "ng" and statuses["設定ファイル"] == "ng"


def test_output_dir_inside_repo_is_flagged(repo):
    onboarding.write_project_config(str(repo), "https://cov", "P", "S", "git", output_dir="out")
    check = next(c for c in onboarding.doctor(str(repo))["checks"] if c["check"] == "出力先")
    assert check["status"] == "ng"
