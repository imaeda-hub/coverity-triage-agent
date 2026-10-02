import subprocess
from pathlib import Path

import pytest

from coverity_triage.config import VcsConfig
from coverity_triage.vcs import GitVcs, SvnVcs, VcsError, github_repo
from coverity_triage.workspace import WorkspaceError

SJIS_FILE = "/* 日本語 */\r\nint f(int *p)\r\n{\r\n    return *p;\r\n}\r\n".encode("cp932")


def shb(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True).stdout


def sh(*args, cwd=None):
    return shb(*args, cwd=cwd).decode()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "t", cwd=repo)
    sh("git", "config", "user.email", "t@example.com", cwd=repo)
    sh("git", "config", "core.autocrlf", "false", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "a.c").write_bytes(SJIS_FILE)
    sh("git", "add", ".", cwd=repo)
    sh("git", "commit", "-qm", "init", cwd=repo)
    first = sh("git", "rev-parse", "HEAD", cwd=repo).strip()
    (repo / "src" / "b.c").write_bytes(b"int g;\n")
    sh("git", "add", ".", cwd=repo)
    sh("git", "commit", "-qm", "second", cwd=repo)
    return repo, first


def test_git_snapshots_fix_branch_and_patch(git_repo, tmp_path):
    repo, first = git_repo
    vcs = GitVcs(repo, VcsConfig(type="git"), tmp_path / "run", "20261002-1")
    (tmp_path / "run" / "patches").mkdir(parents=True)

    analyzed = vcs.investigation_root(first)
    assert (analyzed / "src" / "a.c").read_bytes() == SJIS_FILE
    assert not (analyzed / "src" / "b.c").exists()

    tree = vcs.overlay("123", "fix")
    tree.edit("src/a.c", "    return *p;\n", "    if (p == NULL) {\n        return 0;\n    }\n    return *p;\n")
    with pytest.raises(WorkspaceError):
        tree.edit("../x.c", "a", "b")

    saved = vcs.save_fix("123", "fix", "Fix CID 123", [123])
    assert saved.branch == "coverity-fix/cid-123"
    blob = shb("git", "show", f"{saved.branch}:src/a.c", cwd=repo)
    assert b"\r\n    if (p == NULL) {\r\n" in blob and "日本語".encode("cp932") in blob
    assert sh("git", "status", "--porcelain", cwd=repo) == ""  # user's working copy untouched
    patch = Path(saved.patch_path).read_bytes()
    assert b"+    if (p == NULL) {\r" in patch
    assert (Path(saved.mirror_dir) / "src" / "a.c").is_file()

    # applying the patch to the latest code works
    sh("git", "apply", "--check", saved.patch_path, cwd=repo)


def test_git_per_run_composes_approved_commits(git_repo, tmp_path):
    repo, _ = git_repo
    vcs = GitVcs(repo, VcsConfig(type="git", branch_mode="per_run"), tmp_path / "run", "R1")
    (tmp_path / "run" / "patches").mkdir(parents=True)
    commits = []
    for item, old, new in [("1", "int g;", "int g = 0;"), ("2", "return *p;", "return p ? *p : 0;")]:
        path = "src/b.c" if item == "1" else "src/a.c"
        vcs.overlay(item, "fix").edit(path, old, new)
        saved = vcs.save_fix(item, "fix", f"fix {item}", [int(item)])
        assert saved.branch is None
        commits.append((item, saved.commit))
    branch, included, conflicted = vcs.compose_run_branch(commits)
    assert branch == "coverity-fix/run-R1" and included == ["1", "2"] and conflicted == []
    assert sh("git", "show", f"{branch}:src/b.c", cwd=repo) == "int g = 0;\n"


def test_save_without_changes_fails(git_repo, tmp_path):
    repo, _ = git_repo
    vcs = GitVcs(repo, VcsConfig(type="git"), tmp_path / "run", "R1")
    with pytest.raises(VcsError, match="変更がありません"):
        vcs.save_fix("1", "fix", "m", [1])


def test_github_repo_urls():
    assert github_repo("https://github.com/o/r.git") == ("https://api.github.com", "o", "r")
    assert github_repo("git@ghe.example.co.jp:o/r.git") == ("https://ghe.example.co.jp/api/v3", "o", "r")


@pytest.fixture
def svn_wc(tmp_path):
    repo = tmp_path / "svnrepo"
    sh("svnadmin", "create", str(repo))
    wc = tmp_path / "wc"
    sh("svn", "checkout", "-q", repo.as_uri(), str(wc))
    (wc / "src").mkdir()
    (wc / "src" / "a.c").write_bytes(SJIS_FILE)
    sh("svn", "add", "-q", "src", cwd=wc)
    sh("svn", "commit", "-qm", "init", cwd=wc)
    (wc / "src" / "a.c").write_bytes(SJIS_FILE + b"/* r2 */\r\n")
    sh("svn", "commit", "-qm", "r2", cwd=wc)
    sh("svn", "update", "-q", cwd=wc)
    return wc


def test_svn_patch_and_apply(svn_wc, tmp_path):
    vcs = SvnVcs(svn_wc, VcsConfig(type="svn"), tmp_path / "run", "R1")
    (tmp_path / "run" / "patches").mkdir(parents=True)
    assert vcs.has_revision("1")
    analyzed = vcs.investigation_root("1")
    assert (analyzed / "src" / "a.c").read_bytes() == SJIS_FILE
    latest, base = vcs.latest()
    assert latest == "2"

    vcs.overlay("7", "fix").edit("src/a.c", "    return *p;", "    return p ? *p : 0;")
    saved = vcs.save_fix("7", "fix", "fix", [7])
    patch = Path(saved.patch_path).read_bytes()
    assert patch.startswith(b"Index: src/a.c\n") and b"(revision 2)" in patch
    assert sh("svn", "status", cwd=svn_wc) == ""  # untouched until approval

    vcs.apply_patch(saved.patch_path)
    assert (svn_wc / "src" / "a.c").read_bytes() == SJIS_FILE.replace(
        b"return *p;", b"return p ? *p : 0;") + b"/* r2 */\r\n"


def test_parallel_calls_share_one_snapshot(git_repo, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repo, first = git_repo

    def latest(_):
        # a new Vcs object per call, like one MCP tool call per subagent
        return GitVcs(repo, VcsConfig(type="git"), tmp_path / "run", "R1").latest()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(latest, range(8)))
    assert len({r[0] for r in results}) == 1
    assert (results[0][1] / "src" / "b.c").is_file()
