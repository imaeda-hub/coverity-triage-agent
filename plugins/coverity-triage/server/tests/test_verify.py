import json
import sys

from coverity_triage.config import VerifyConfig
from coverity_triage.models import Issue
from coverity_triage.verify import Verifier, intermediate_dir, load_analysis, matches
from coverity_triage.workspace import OverlayTree


def test_build_mode_runs_configured_command(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.c").write_text("int a;\n", encoding="utf-8")
    tree = OverlayTree(base, tmp_path / "ov")
    tree.edit("a.c", "int a;", "int a = 1;")
    ok = Verifier(VerifyConfig(build_command=f'"{sys.executable}" -c "print(open(\'a.c\').read())"'),
                  tmp_path / "run").run("build", "1", "fix", tree, [])
    assert ok["build_ok"] is True
    assert "int a = 1;" in open(ok["log"], encoding="utf-8").read()
    ng = Verifier(VerifyConfig(build_command=f'"{sys.executable}" -c "raise SystemExit(2)"'),
                  tmp_path / "run").run("build", "2", "fix", tree, [])
    assert ng["build_ok"] is False


def test_analysis_parsing_and_matching(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"issues": [
        {"mergeKey": "k1", "checkerName": "NULL_RETURNS",
         "mainEventFilePathname": "C:\\work\\src\\a.c", "functionDisplayName": "f"}]}))
    records = load_analysis(path)
    assert records[0]["file"] == "C:/work/src/a.c"
    assert matches(Issue(cid=1, checker="NULL_RETURNS", file="src/a.c", function="f"), records[0])
    assert matches(Issue(cid=1, checker="X", file="z.c", merge_key="k1"), records[0])
    assert not matches(Issue(cid=1, checker="NULL_RETURNS", file="src/b.c"), records[0])


def test_intermediate_dir():
    assert intermediate_dir("--dir idir --all") == "idir"
    assert intermediate_dir('--all --dir="C:\\cov idir"') == "C:\\cov idir"
