from pathlib import Path

import pytest

from coverity_triage import config as cfg
from coverity_triage.encoding import EncodingError, decode, replace_once
from coverity_triage.grouping import build_work_items
from coverity_triage.models import Issue
from coverity_triage.run_state import RunError, RunStore


def issue(cid, checker="NULL_RETURNS", file="a.c", function=None, line=None, macro=None):
    return Issue(cid=cid, checker=checker, file=file, function=function, line=line, macro=macro)


# ---- encoding ----------------------------------------------------------------------------


def test_cp932_roundtrip_keeps_bytes():
    raw = "/* 日本語コメント */\r\nint a;\r\nint b;\r\n".encode("cp932")
    src = decode(raw)
    assert src.encoding == "cp932" and src.newline == "\r\n"
    edited = replace_once(src, "int a;\nint b;", "int a = 0;\nint b;")
    out = edited.encode()
    assert out == "/* 日本語コメント */\r\nint a = 0;\r\nint b;\r\n".encode("cp932")


def test_utf8_bom_is_kept():
    raw = b"\xef\xbb\xbfint x;\n"
    edited = replace_once(decode(raw), "int x;", "int x = 1;")
    assert edited.encode() == b"\xef\xbb\xbfint x = 1;\n"


def test_unencodable_text_is_rejected():
    src = decode("int a;\n".encode("cp932") + "/* あ */".encode("cp932"))
    with pytest.raises(EncodingError):
        replace_once(src, "int a;", "int a; /* \U0001F600 */")


def test_replace_requires_unique_match():
    src = decode(b"x;\nx;\n")
    with pytest.raises(ValueError, match="2"):
        replace_once(src, "x;", "y;")
    with pytest.raises(ValueError, match="見つかりません"):
        replace_once(src, "z;", "y;")


# ---- grouping ----------------------------------------------------------------------------


def test_grouping_by_function_macro_and_line():
    issues = [
        issue(1, function="f"), issue(2, function="f"),           # same function
        issue(3, checker="MISRA", macro="M"), issue(4, checker="MISRA", file="b.c", macro="M"),
        issue(5, line=10), issue(6, line=10, function="g"),     # same line
        issue(7, checker="OVERRUN", function="f"),               # other checker
    ]
    items = build_work_items(issues)
    assert [(i.id, i.cids) for i in items] == [
        ("G1", [1, 2]), ("G2", [3, 4]), ("G3", [5, 6]), ("7", [7])]


def test_no_grouping_list_is_respected():
    items = build_work_items([issue(1, function="f"), issue(2, function="f")], no_grouping={2})
    assert [(i.id, i.cids) for i in items] == [("1", [1]), ("2", [2])]


# ---- config ------------------------------------------------------------------------------


def write_config(repo: Path, extra: str = "") -> None:
    d = repo / ".coverity-triage" / "filters"
    d.mkdir(parents=True)
    (repo / ".coverity-triage" / "config.yaml").write_text(
        "coverity:\n  url: https://cov\n  api: fake\n  fake_data: fake.yaml\n"
        "vcs:\n  type: git\noutput_dir: out\n" + extra, encoding="utf-8")
    (d / "f.yaml").write_text("project: P\nimpacts: [High]\ntriage:\n  status: [New]\n",
                              encoding="utf-8")


def test_config_and_filter_overrides(tmp_path):
    write_config(tmp_path)
    conf = cfg.load_project_config(tmp_path)
    assert conf.coverity.fake_data == str((tmp_path / ".coverity-triage" / "fake.yaml").resolve())
    spec = cfg.load_filter(tmp_path, "f.yaml", {"impacts": ["Medium"], "triage": {"action": ["Undecided"]}})
    assert spec.name == "f" and spec.impacts == ["Medium"]
    assert spec.triage.status == ["New"] and spec.triage.action == ["Undecided"]


def test_unknown_config_key_is_an_error(tmp_path):
    write_config(tmp_path, "typo_key: 1\n")
    with pytest.raises(cfg.ConfigError, match="typo_key"):
        cfg.load_project_config(tmp_path)


def test_no_grouping_file(tmp_path):
    cfg.add_no_grouping(tmp_path, [3, 1])
    cfg.add_no_grouping(tmp_path, [2, 3])
    assert cfg.load_no_grouping(tmp_path) == {1, 2, 3}


# ---- run state ---------------------------------------------------------------------------


def make_run(tmp_path):
    issues = [issue(1, function="f"), issue(2, function="f"), issue(3)]
    return RunStore.create(tmp_path / "out", repo_root=str(tmp_path), filter_name="f",
                           filter_data={}, verify_mode="none", issues=issues,
                           items=build_work_items(issues), analyzed_revision=None,
                           analyzed_revision_source="local")


def test_claim_finish_and_resume(tmp_path):
    store = make_run(tmp_path)
    a = store.claim_next()
    b = store.claim_next()
    assert (a.id, b.id) == ("G1", "3")
    assert store.claim_next() is None
    store.mark_done("G1")
    store.mark_error("3", "boom")
    assert store.status_counts() == {"pending": 0, "in_progress": 0, "done": 1, "error": 1}
    assert store.reset_for_resume() == {"in_progress": 0, "error": 1}
    again = store.claim_next()
    assert again.id == "3" and again.attempts == 2


def test_split_group(tmp_path):
    store = make_run(tmp_path)
    assert store.split_group("G1", [2]) == ["2"]
    assert store.item("G1").cids == [1]
    assert store.item("2").split_from == "G1"
    with pytest.raises(RunError):
        store.split_group("G1", [1])


# ---- path mapping ------------------------------------------------------------------------

def test_path_mapping(tmp_path):
    from coverity_triage.pathmap import PathMapper
    (tmp_path / "src" / "io").mkdir(parents=True)
    (tmp_path / "src" / "io" / "reader.c").write_text("x", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "util.c").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "util.c").write_text("x", encoding="utf-8")
    m = PathMapper(tmp_path, ["C:\\build\\product"])
    assert m.map("src/io/reader.c") == ("src/io/reader.c", "as_is")
    assert m.map("C:\\build\\product\\src\\io\\reader.c") == ("src/io/reader.c", "prefix")
    assert m.map("/home/ci/work/src/io/reader.c") == ("src/io/reader.c", "suffix")
    assert m.map("D:\\x\\src\\util.c") == ("src/util.c", "suffix")
    assert m.map("D:\\x\\util.c")[1] == "unmapped"          # ambiguous: lib/util.c or src/util.c
    issue, notes = m.map_issue(Issue(cid=1, checker="X", file="/ci/src/io/reader.c"))
    assert issue.file == "src/io/reader.c" and "自動で対応づけました" in notes[0]
