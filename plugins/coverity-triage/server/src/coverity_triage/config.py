"""Project settings and filter files (spec D-49, D-50; design 5.1, 5.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_DIR_NAME = ".coverity-triage"
CONFIG_FILE_NAME = "config.yaml"
FILTERS_DIR_NAME = "filters"
NO_GROUPING_FILE_NAME = "no-grouping.yaml"


class ConfigError(Exception):
    """Raised when a settings or filter file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverityConfig(_Strict):
    url: str
    # "fake" reads issues from a local file instead of a server (for trials and tests).
    api: Literal["rest", "soap", "auto", "fake"] = "auto"
    user_env: str = "COV_USER"
    key_env: str = "COV_AUTH_KEY"
    revision_field: str | None = "version"
    # Prefixes removed from file paths reported by Coverity (e.g. "C:/build/product/").
    path_strip_prefixes: list[str] = Field(default_factory=list)
    fake_data: str | None = None


class VcsConfig(_Strict):
    type: Literal["git", "svn"]
    base_branch: str = "main"
    branch_mode: Literal["per_cid", "per_run"] = "per_cid"
    branch_prefix: str = "coverity-fix/"
    github_token_env: str = "GITHUB_TOKEN"


VerifyMode = Literal["none", "build", "build+analyze"]


class VerifyConfig(_Strict):
    default: VerifyMode = "none"
    build_command: str = ""
    cov_build_args: str = ""
    cov_analyze_args: str = ""


class ProjectConfig(_Strict):
    coverity: CoverityConfig
    vcs: VcsConfig
    output_dir: str
    max_items: int = Field(default=100, ge=1)
    parallel: int = Field(default=1, ge=1)
    deviation_target: Literal["coverity", "coverity+annotation"] = "coverity"
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    # Shown for reference only; the model is pinned in the agent definitions (spec D-47).
    model: str | None = None

    def secret_values(self) -> list[str]:
        """Values of the credential environment variables, for log redaction."""
        names = [self.coverity.user_env, self.coverity.key_env, self.vcs.github_token_env]
        return [v for v in (os.environ.get(n) for n in names) if v]


class TriageFilter(_Strict):
    status: list[str] = Field(default_factory=list)
    classification: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)


class FilterSpec(_Strict):
    """Issue selection. Values inside one key are OR-ed, keys are AND-ed."""

    name: str = ""
    project: str | None = None
    streams: list[str] = Field(default_factory=list)
    checkers: list[str] = Field(default_factory=list)
    impacts: list[str] = Field(default_factory=list)
    triage: TriageFilter = Field(default_factory=TriageFilter)
    max_items: int | None = Field(default=None, ge=1)
    revision: str = ""


def config_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / CONFIG_DIR_NAME


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"ファイルが見つかりません: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML の書式が不正です: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"YAML の最上位はキーと値の組である必要があります: {path}")
    return data


def _validate(model: type[BaseModel], data: dict[str, Any], source: Path | str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"設定値が不正です: {source}\n{exc}") from exc


def load_project_config(repo_root: str | Path) -> ProjectConfig:
    path = config_dir(repo_root) / CONFIG_FILE_NAME
    config: ProjectConfig = _validate(ProjectConfig, _read_yaml(path), path)
    if config.coverity.fake_data:
        fake = Path(config.coverity.fake_data)
        if not fake.is_absolute():
            config.coverity.fake_data = str((config_dir(repo_root) / fake).resolve())
    return config


def resolve_filter_path(repo_root: str | Path, filter_file: str) -> Path:
    """Accept an absolute path, a path relative to the repo, or a bare file name in filters/."""
    candidates = [Path(filter_file), Path(repo_root) / filter_file,
                  config_dir(repo_root) / FILTERS_DIR_NAME / filter_file]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ConfigError(f"条件ファイルが見つかりません: {filter_file}")


def load_filter(repo_root: str | Path, filter_file: str,
                overrides: dict[str, Any] | None = None) -> FilterSpec:
    """Load a filter file and apply overrides given in chat (spec D-13).

    Overrides replace values key by key; ``triage`` is merged one level deeper.
    """
    path = resolve_filter_path(repo_root, filter_file)
    data = _read_yaml(path)
    for key, value in (overrides or {}).items():
        if key == "triage" and isinstance(value, dict):
            merged = dict(data.get("triage") or {})
            merged.update(value)
            data["triage"] = merged
        else:
            data[key] = value
    spec: FilterSpec = _validate(FilterSpec, data, path)
    if not spec.name:
        spec.name = path.stem
    return spec


def load_no_grouping(repo_root: str | Path) -> set[int]:
    path = config_dir(repo_root) / NO_GROUPING_FILE_NAME
    if not path.is_file():
        return set()
    data = _read_yaml(path)
    return {int(c) for c in data.get("cids") or []}


def add_no_grouping(repo_root: str | Path, cids: list[int]) -> None:
    """Record CIDs of a rejected group so they are processed one by one next time (design 5.4)."""
    path = config_dir(repo_root) / NO_GROUPING_FILE_NAME
    current = load_no_grouping(repo_root)
    merged = sorted(current | {int(c) for c in cids})
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# 却下されたグループの CID。次回以降はグループ化せず個別に処理する（自動追記）\n"
    path.write_text(header + yaml.safe_dump({"cids": merged}, allow_unicode=True),
                    encoding="utf-8")
