"""git / svn operations (spec D-4, D-16, D-17, D-25 to D-29, D-57, D-58, D-60).

The user's working copy is never modified while triaging:

* snapshots are exported (``git archive`` / ``svn export``) into the run folder;
* git commits and branches are created with a temporary index, without a worktree;
* svn patches are generated from the overlay and applied only after approval (D-29).
"""

from __future__ import annotations

import difflib
import io
import os
import re
import subprocess
import tarfile
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import VcsConfig
from .encoding import decode
from .workspace import OverlayTree

FixKind = str  # "fix" or "annotation"


class VcsError(Exception):
    """Raised when a VCS command fails."""


def run_cmd(args: list[str], cwd: str | Path | None = None, *, env: dict | None = None,
            input_bytes: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, input=input_bytes,
                              capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise VcsError(f"コマンドが見つかりません: {args[0]}") from exc
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise VcsError(f"{' '.join(args[:3])} が失敗しました: {message}")
    return proc.stdout


@dataclass
class SavedFix:
    files: list[str]
    patch_path: str
    mirror_dir: str
    branch: str | None = None
    commit: str | None = None


class Vcs(ABC):
    def __init__(self, repo_root: str | Path, config: VcsConfig, run_dir: str | Path, run_id: str):
        self.repo_root = Path(repo_root)
        self.config = config
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.work_dir = self.run_dir / "work"
        self._lock = threading.Lock()

    # ---- snapshots -------------------------------------------------------------------

    def investigation_root(self, revision: str | None) -> Path:
        """Code to investigate: the analyzed revision, or the local code when unknown (D-16)."""
        if revision is None:
            return self.repo_root
        dest = self.work_dir / f"analyzed-{_safe_name(revision)}"
        with self._lock:
            if not dest.is_dir():
                self._export(revision, dest)
        return dest

    def latest(self) -> tuple[str, Path]:
        """Latest revision of the integration target and its snapshot (D-58)."""
        marker = self.work_dir / "latest-revision.txt"
        with self._lock:
            if marker.is_file():
                revision = marker.read_text(encoding="utf-8").strip()
            else:
                revision = self._resolve_latest()
                self._export(revision, self.work_dir / "latest")
                marker.write_text(revision, encoding="utf-8")
        return revision, self.work_dir / "latest"

    def overlay(self, item_id: str, kind: FixKind) -> OverlayTree:
        _, base = self.latest()
        return OverlayTree(base, self.work_dir / "overlay" / item_id / kind)

    def _tmp_dir(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=dest.name + ".", dir=dest.parent))

    @abstractmethod
    def has_revision(self, revision: str) -> bool: ...

    @abstractmethod
    def _resolve_latest(self) -> str: ...

    @abstractmethod
    def _export(self, revision: str, dest: Path) -> None: ...

    # ---- fixes -------------------------------------------------------------------------

    def save_fix(self, item_id: str, kind: FixKind, message: str, cids: list[int]) -> SavedFix:
        tree = self.overlay(item_id, kind)
        files = tree.changed_files()
        if not files:
            raise VcsError("変更がありません。edit_source で修正してから保存してください")
        patch_path = self.run_dir / "patches" / f"{item_id}-{kind}.patch"
        mirror_dir = self.run_dir / "fixed" / item_id / kind
        tree.mirror_to(mirror_dir)
        saved = self._save(item_id, kind, message, cids, tree, files, patch_path)
        saved.mirror_dir = str(mirror_dir)
        return saved

    @abstractmethod
    def _save(self, item_id: str, kind: FixKind, message: str, cids: list[int],
              tree: OverlayTree, files: list[str], patch_path: Path) -> SavedFix: ...


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:60]


# ==== git ===================================================================================


class GitVcs(Vcs):
    def git(self, *args: str, env: dict | None = None, input_bytes: bytes | None = None) -> bytes:
        return run_cmd(["git", *args], cwd=self.repo_root, env=env, input_bytes=input_bytes)

    def has_revision(self, revision: str) -> bool:
        try:
            self.git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
            return True
        except VcsError:
            return False

    def _resolve_latest(self) -> str:
        base = self.config.base_branch
        remotes = self.git("remote").decode().split()
        if "origin" in remotes:
            try:
                self.git("fetch", "--quiet", "origin", base)
                return self.git("rev-parse", f"origin/{base}^{{commit}}").decode().strip()
            except VcsError:
                pass  # offline: fall back to the local branch
        return self.git("rev-parse", f"{base}^{{commit}}").decode().strip()

    def _export(self, revision: str, dest: Path) -> None:
        tmp = self._tmp_dir(dest)
        data = self.git("archive", "--format=tar", revision)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        os.replace(tmp, dest)

    def branch_name(self, item_id: str, kind: FixKind, cids: list[int]) -> str:
        name = f"cid-{cids[0]}" if not item_id.startswith("G") else f"{self.run_id}-{item_id}"
        if kind == "annotation":
            name += "-annotation"
        branch = self.config.branch_prefix + name
        if self.has_revision(f"refs/heads/{branch}"):
            branch += f"-{self.run_id}"
        return branch

    def _commit_files(self, parent: str, tree: OverlayTree, files: list[str], message: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
            self.git("read-tree", parent, env=env)
            for rel in files:
                mode = "100644"
                listing = self.git("ls-tree", parent, "--", rel).decode().split()
                if listing:
                    mode = listing[0]
                sha = self.git("hash-object", "-w", f"--path={rel}", "--stdin",
                               input_bytes=(tree.overlay / rel).read_bytes()).decode().strip()
                self.git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{rel}", env=env)
            tree_sha = self.git("write-tree", env=env).decode().strip()
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", self._config_or("user.name", "coverity-triage"))
        env.setdefault("GIT_AUTHOR_EMAIL", self._config_or("user.email", "coverity-triage@localhost"))
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        return run_cmd(["git", "commit-tree", tree_sha, "-p", parent, "-F", "-"],
                       cwd=self.repo_root, env=env,
                       input_bytes=message.encode("utf-8")).decode().strip()

    def _config_or(self, key: str, default: str) -> str:
        try:
            return self.git("config", key).decode().strip() or default
        except VcsError:
            return default

    def _save(self, item_id, kind, message, cids, tree, files, patch_path) -> SavedFix:
        latest, _ = self.latest()
        commit = self._commit_files(latest, tree, files, message)
        patch_path.write_bytes(self.git("diff", "--binary", latest, commit))
        if self.config.branch_mode == "per_cid":
            branch = self.branch_name(item_id, kind, cids)
            self.git("branch", branch, commit)
        else:
            # per_run: the run branch is composed from approved commits only (apply time).
            branch = None
            self.git("update-ref", f"refs/coverity-triage/{self.run_id}/{item_id}-{kind}", commit)
        return SavedFix(files, str(patch_path), "", branch, commit)

    # ---- apply (after approval) --------------------------------------------------------

    def compose_run_branch(self, commits: list[tuple[str, str]]) -> tuple[str, list[str], list[str]]:
        """Build ``<prefix>run-<run_id>`` from approved commits (per_run mode, D-25).

        Returns the branch, the item ids included, and the item ids that conflicted.
        """
        latest, _ = self.latest()
        head, included, conflicted = latest, [], []
        for item_id, commit in commits:
            patch = self.git("diff", "--binary", f"{commit}^", commit)
            with tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
                self.git("read-tree", head, env=env)
                try:
                    self.git("apply", "--cached", "-", env=env, input_bytes=patch)
                except VcsError:
                    conflicted.append(item_id)
                    continue
                tree_sha = self.git("write-tree", env=env).decode().strip()
            message = self.git("log", "-1", "--format=%B", commit)
            head = run_cmd(["git", "commit-tree", tree_sha, "-p", head, "-F", "-"],
                           cwd=self.repo_root, input_bytes=message).decode().strip()
            included.append(item_id)
        branch = f"{self.config.branch_prefix}run-{self.run_id}"
        if included:
            self.git("branch", "-f", branch, head)
        return branch, included, conflicted

    def push(self, branch: str) -> None:
        self.git("push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")

    def create_pull_request(self, branch: str, title: str, body: str) -> str:
        token = os.environ.get(self.config.github_token_env)
        if not token:
            raise VcsError(f"環境変数 {self.config.github_token_env} に GitHub のトークンが設定されていません")
        api, owner, repo = github_repo(self.git("remote", "get-url", "origin").decode().strip())
        response = httpx.post(
            f"{api}/repos/{owner}/{repo}/pulls",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"title": title, "head": branch, "base": self.config.base_branch, "body": body},
            timeout=60,
        )
        if response.status_code >= 300:
            raise VcsError(f"プルリクエストの作成に失敗しました ({response.status_code}): {response.text[:300]}")
        return response.json()["html_url"]


def github_repo(remote_url: str) -> tuple[str, str, str]:
    """Return (API base URL, owner, repo) for github.com or GitHub Enterprise Server (D-28)."""
    match = (re.match(r"^https?://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url)
             or re.match(r"^(?:ssh://)?[^@]+@([^:/]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote_url))
    if not match:
        raise VcsError(f"GitHub のリモート URL として解釈できません: {remote_url}")
    host, owner, repo = match.groups()
    api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
    return api, owner, repo


# ==== svn ===================================================================================


class SvnVcs(Vcs):
    def svn(self, *args: str) -> bytes:
        return run_cmd(["svn", "--non-interactive", *args], cwd=self.repo_root)

    def url(self) -> str:
        return self.svn("info", "--show-item", "url", str(self.repo_root)).decode().strip()

    def has_revision(self, revision: str) -> bool:
        try:
            self.svn("info", "-r", revision, self.url())
            return True
        except VcsError:
            return False

    def _resolve_latest(self) -> str:
        return self.svn("info", "-r", "HEAD", "--show-item", "revision", self.url()).decode().strip()

    def _export(self, revision: str, dest: Path) -> None:
        tmp = self._tmp_dir(dest)
        self.svn("export", "--quiet", "--force", "-r", revision, self.url(), str(tmp))
        os.replace(tmp, dest)

    def _save(self, item_id, kind, message, cids, tree, files, patch_path) -> SavedFix:
        latest, base = self.latest()
        patch_path.write_bytes(svn_patch(base, tree.overlay, files, latest))
        return SavedFix(files, str(patch_path), "")

    def apply_patch(self, patch_path: str) -> str:
        """Apply an approved patch to the user's working copy (D-29). No commit."""
        output = self.svn("patch", patch_path, str(self.repo_root)).decode("utf-8", "replace")
        if re.search(r"^(C|>\s+rejected)", output, re.MULTILINE):
            raise VcsError(f"svn patch で競合が発生しました:\n{output}")
        return output


def svn_patch(base: Path, overlay: Path, files: list[str], revision: str) -> bytes:
    """Unified diff in the ``svn diff`` layout, each file in its own encoding (D-36)."""
    out = bytearray()
    for rel in files:
        old_path = base / rel
        old_raw = old_path.read_bytes() if old_path.is_file() else b""
        new_raw = (overlay / rel).read_bytes()
        new_src = decode(new_raw)
        old_text = decode(old_raw).text if old_raw else ""
        encoding = new_src.encoding
        old_label = f"{rel}\t(revision {revision})" if old_raw else f"{rel}\t(nonexistent)"
        out += f"Index: {rel}\n{'=' * 67}\n".encode(encoding)
        diff = difflib.unified_diff(old_text.splitlines(keepends=True),
                                    new_src.text.splitlines(keepends=True),
                                    fromfile=old_label, tofile=f"{rel}\t(working copy)", n=3)
        for line in diff:
            if line.startswith(("---", "+++")):
                out += line.encode(encoding)
                if not line.endswith("\n"):
                    out += b"\n"
                continue
            out += line.encode(encoding)
            if not line.endswith("\n"):
                out += b"\n\\ No newline at end of file\n"
    return bytes(out)


def make_vcs(repo_root: str | Path, config: VcsConfig, run_dir: str | Path, run_id: str) -> Vcs:
    cls = GitVcs if config.type == "git" else SvnVcs
    return cls(repo_root, config, run_dir, run_id)
