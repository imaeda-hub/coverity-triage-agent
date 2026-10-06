"""git / svn: work copies outside the repository, fixes as branches or patches, and applying them.

The person's working copy is never changed while triaging. Fixes are made in work copies under
the run folder (git: ``git worktree``; svn: ``svn checkout``). Patches are kept as the bytes git
or svn wrote, so the files' character encoding is never touched.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .common import CtError, out_text, run_cmd, sha
from .config import VcsConfig

# Never wait for a password prompt in the terminal; fail with a message instead.
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0"}


@dataclass
class SavedFix:
    files: list[str]
    patch: str
    branch: str | None = None
    commit: str | None = None


def detect(repo: Path) -> str | None:
    if (repo / ".git").exists():
        return "git"
    if (repo / ".svn").exists():
        return "svn"
    return None


class Vcs(ABC):
    def __init__(self, repo: Path, config: VcsConfig):
        self.repo = repo
        self.config = config

    @abstractmethod
    def has_revision(self, revision: str) -> bool: ...

    @abstractmethod
    def resolve_latest(self) -> tuple[str, str]:
        """Latest revision of the integration target, and a note when it could not be updated."""

    @abstractmethod
    def ensure_copy(self, dest: Path, revision: str) -> None:
        """Create the work copy at ``revision`` unless it is already there."""

    @abstractmethod
    def reset_copy(self, dest: Path, revision: str) -> None:
        """Discard every change in the work copy and move it to ``revision``."""

    @abstractmethod
    def changed_files(self, dest: Path) -> list[str]: ...

    @abstractmethod
    def save(self, dest: Path, revision: str, message: str, branch: str | None, keep_ref: str,
             patch: Path) -> SavedFix:
        """Record the changes of the work copy: patch file, and for git a commit and branch."""

    @abstractmethod
    def remove_copy(self, dest: Path) -> None: ...

    @abstractmethod
    def export(self, revision: str, dest: Path) -> None:
        """Plain files of ``revision`` (for builds), without version control data."""

    @abstractmethod
    def fingerprint(self) -> str:
        """A value that changes when files of the person's working copy change."""


# ==== git ===================================================================================


class GitVcs(Vcs):
    def git(self, *args: str, cwd: Path | None = None, check: bool = True,
            input_bytes: bytes | None = None, env: dict[str, str] | None = None):
        full_env = {**os.environ, **QUIET_ENV, **(env or {})}
        return run_cmd(["git", *args], cwd=cwd or self.repo, env=full_env, input_bytes=input_bytes, check=check)

    def has_revision(self, revision: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False).returncode == 0

    def resolve_latest(self) -> tuple[str, str]:
        base = self.config.base_branch
        note = ""
        if "origin" in out_text(self.git("remote")).split():
            fetched = self.git("fetch", "--quiet", "origin", base, check=False)
            if fetched.returncode == 0 and self.has_revision(f"origin/{base}"):
                return out_text(self.git("rev-parse", f"origin/{base}^{{commit}}")), note
            note = f"origin から {base} を取得できなかったため、手元の {base} を使います。"
        if self.has_revision(base):
            return out_text(self.git("rev-parse", f"{base}^{{commit}}")), note
        raise CtError(f"取り込み先のブランチ {base} が見つかりません（設定 vcs.base_branch を確認してください）")

    def ensure_copy(self, dest: Path, revision: str) -> None:
        if (dest / ".git").is_file():
            return
        if dest.exists():
            shutil.rmtree(dest)
        self.git("worktree", "prune")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", "--force", str(dest), revision)

    def reset_copy(self, dest: Path, revision: str) -> None:
        self.ensure_copy(dest, revision)
        self.git("checkout", "--quiet", "--force", "--detach", revision, cwd=dest)
        self.git("reset", "--quiet", "--hard", revision, cwd=dest)
        self.git("clean", "-q", "-f", "-d", "-x", cwd=dest)

    def changed_files(self, dest: Path) -> list[str]:
        raw = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=dest).stdout
        files, entries, i = [], raw.split(b"\0"), 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            files.append(entry[3:].decode("utf-8", "surrogateescape"))
            if entry[:1] in (b"R", b"C"):
                i += 1  # the original name follows a rename or copy
        return sorted(set(files))

    def _identity(self) -> dict[str, str]:
        """Commits need a name and e-mail; use neutral ones when the person has not set any."""
        env = {}
        for key, var, default in (("user.name", "GIT_AUTHOR_NAME", "coverity-triage"),
                                  ("user.email", "GIT_AUTHOR_EMAIL", "coverity-triage@localhost")):
            if not out_text(self.git("config", key, check=False)):
                env[var] = env[var.replace("AUTHOR", "COMMITTER")] = default
        return env

    def save(self, dest: Path, revision: str, message: str, branch: str | None, keep_ref: str,
             patch: Path) -> SavedFix:
        files = self.changed_files(dest)
        self.git("add", "-A", cwd=dest)
        self.git("commit", "-q", "--no-verify", "-F", "-", cwd=dest, input_bytes=message.encode("utf-8"),
                 env=self._identity())
        commit = out_text(self.git("rev-parse", "HEAD", cwd=dest))
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_bytes(self.git("diff", "--binary", revision, commit, cwd=dest).stdout)
        self.git("update-ref", keep_ref, commit)  # keeps the commit even without a branch
        if branch:
            branch = self.unique_branch(branch)
            self.git("branch", branch, commit)
        return SavedFix(files, str(patch), branch, commit)

    def unique_branch(self, name: str) -> str:
        candidate, n = name, 1
        while self.has_revision(f"refs/heads/{candidate}"):
            n += 1
            candidate = f"{name}-{n}"
        return candidate

    def remove_copy(self, dest: Path) -> None:
        if (dest / ".git").is_file():
            self.git("worktree", "remove", "--force", str(dest), check=False)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        self.git("worktree", "prune", check=False)

    def export(self, revision: str, dest: Path) -> None:
        data = self.git("archive", "--format=tar", revision).stdout
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")

    def fingerprint(self) -> str:
        status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        diff = self.git("diff", "HEAD", "--binary", check=False).stdout
        return sha(status + b"\0" + diff)

    # ---- after approval ------------------------------------------------------------------

    def push(self, branch: str) -> None:
        self.git("push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")

    def combine(self, dest: Path, revision: str, commits: list[tuple[str, str]], branch: str
                ) -> tuple[str | None, list[str], list[str]]:
        """One branch with the approved commits on top of ``revision`` (option per_run_branch).

        Returns the branch (None when nothing could be combined), the ids included, and the ids
        whose change conflicted with an earlier one.
        """
        self.reset_copy(dest, revision)
        included, conflicted = [], []
        for item_id, commit in commits:
            picked = self.git("cherry-pick", "--allow-empty", commit, cwd=dest, check=False, env=self._identity())
            if picked.returncode == 0:
                included.append(item_id)
            else:
                self.git("cherry-pick", "--abort", cwd=dest, check=False)
                conflicted.append(item_id)
        if not included:
            return None, included, conflicted
        branch = self.unique_branch(branch)
        self.git("branch", branch, out_text(self.git("rev-parse", "HEAD", cwd=dest)))
        return branch, included, conflicted

    def compare_url(self, branch: str, title: str, body: str) -> str | None:
        """URL of the GitHub page that opens a pull request with the title and body filled in."""
        remote = out_text(self.git("remote", "get-url", "origin", check=False))
        web = github_web_url(remote)
        if not web:
            return None
        query = f"expand=1&title={quote(title)}&body={quote(body[:3000])}"
        return f"{web}/compare/{quote(self.config.base_branch)}...{quote(branch)}?{query}"


def github_web_url(remote: str) -> str | None:
    """``https://<host>/<owner>/<repo>`` for an https or ssh remote of GitHub (or GitHub Enterprise)."""
    match = (re.match(r"^https?://(?:[^@/]+@)?([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", remote)
             or re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", remote))
    if not match:
        return None
    host, owner, repo = match.groups()
    return f"https://{host}/{owner}/{repo}"


# ==== svn ===================================================================================


class SvnVcs(Vcs):
    def svn(self, *args: str, cwd: Path | None = None, check: bool = True):
        return run_cmd(["svn", "--non-interactive", *args], cwd=cwd or self.repo, check=check)

    def url(self) -> str:
        return out_text(self.svn("info", "--show-item", "url", str(self.repo)))

    def has_revision(self, revision: str) -> bool:
        if not revision.isdigit():
            return False
        return self.svn("info", "-r", revision, self.url(), check=False).returncode == 0

    def resolve_latest(self) -> tuple[str, str]:
        return out_text(self.svn("info", "-r", "HEAD", "--show-item", "revision", self.url())), ""

    def ensure_copy(self, dest: Path, revision: str) -> None:
        if (dest / ".svn").is_dir():
            return
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.svn("checkout", "--quiet", "-r", revision, self.url(), str(dest))

    def _status(self, dest: Path, *extra: str) -> list[tuple[str, str]]:
        lines = out_text(self.svn("status", *extra, cwd=dest)).splitlines()
        return [(line[:1], line[8:]) for line in lines if len(line) > 8 and line[:1] != " "]

    def reset_copy(self, dest: Path, revision: str) -> None:
        self.ensure_copy(dest, revision)
        self.svn("revert", "--quiet", "-R", ".", cwd=dest)
        for code, rel in self._status(dest, "--no-ignore"):
            if code in ("?", "I"):
                target = dest / rel
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        self.svn("update", "--quiet", "-r", revision, cwd=dest)

    def changed_files(self, dest: Path) -> list[str]:
        return sorted(rel.replace("\\", "/") for code, rel in self._status(dest) if code in "MAD?!R")

    def save(self, dest: Path, revision: str, message: str, branch: str | None, keep_ref: str,
             patch: Path) -> SavedFix:
        files = self.changed_files(dest)
        self.svn("add", "--quiet", "--force", ".", cwd=dest)
        for code, rel in self._status(dest):
            if code == "!":
                self.svn("delete", "--quiet", rel, cwd=dest)
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_bytes(self.svn("diff", cwd=dest).stdout)
        return SavedFix(files, str(patch))

    def remove_copy(self, dest: Path) -> None:
        shutil.rmtree(dest, ignore_errors=True)

    def export(self, revision: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.svn("export", "--quiet", "--force", "-r", revision, self.url(), str(dest))

    def fingerprint(self) -> str:
        status = self.svn("status").stdout
        diff = self.svn("diff", check=False).stdout
        return sha(status + b"\0" + diff)

    # ---- after approval ------------------------------------------------------------------

    def apply_patch(self, patch: Path) -> str:
        """Apply an approved patch to the person's working copy. Committing is left to the person."""
        output = out_text(self.svn("patch", str(patch), str(self.repo)))
        if re.search(r"^(C|>\s+rejected)", output, re.MULTILINE):
            raise CtError(f"svn patch で競合しました。ワーキングコピーを確認してください:\n{output}")
        return output


def make_vcs(repo: Path, config: VcsConfig) -> Vcs:
    return (GitVcs if config.type == "git" else SvnVcs)(repo, config)
