"""The parts of the project settings the MCP server needs: the Coverity connection and a search filter.

The project settings file (``.coverity-triage/config.yaml``) belongs to the scripts of the skill
``coverity-triage-scripts``; the server reads only its ``coverity`` section.
The search filter is the JSON file that ``ct.py new-run`` writes for one run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_PATH = Path(".coverity-triage") / "config.yaml"


class SettingsError(Exception):
    """A settings or filter file is missing or invalid."""


class CoverityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    # auto: Coverity Connect. fake: issues from a local file (self-test and automated tests).
    api: Literal["auto", "fake"] = "auto"
    user_env: str = "COV_USER"
    key_env: str = "COV_AUTH_KEY"
    triage_store: str = "Default Triage Store"
    # Element of the SOAP snapshot information that holds the analyzed revision.
    revision_field: str | None = "sourceVersion"
    path_strip_prefixes: list[str] = Field(default_factory=list)
    # CA certificate file; empty means the OS certificate store.
    ca_file: str | None = None
    fake_data: str | None = None


class TriageFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: list[str] = Field(default_factory=list)
    classification: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)


class FilterSpec(BaseModel):
    """Issue selection. Values inside one key are OR-ed, keys are AND-ed."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    project: str | None = None
    streams: list[str] = Field(default_factory=list)
    checkers: list[str] = Field(default_factory=list)
    impacts: list[str] = Field(default_factory=list)
    triage: TriageFilter = Field(default_factory=TriageFilter)
    limit: int | None = Field(default=None, ge=1)


def load_coverity_config(repo_root: str | Path) -> CoverityConfig:
    path = Path(repo_root) / CONFIG_PATH
    if not path.is_file():
        raise SettingsError(f"設定ファイルがありません: {path}（/coverity-setup で作成します）")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SettingsError(f"設定ファイルの書式が正しくありません: {path}: {exc}") from exc
    section = data.get("coverity") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise SettingsError(f"設定ファイルに coverity の項目がありません: {path}")
    try:
        config = CoverityConfig.model_validate(section)
    except ValidationError as exc:
        raise SettingsError(f"設定ファイルの coverity の値が正しくありません: {path}\n{exc}") from exc
    if config.fake_data and not Path(config.fake_data).is_absolute():
        config.fake_data = str((path.parent / config.fake_data).resolve())
    return config


def load_filter(path: str | Path) -> FilterSpec:
    file = Path(path)
    if not file.is_file():
        raise SettingsError(f"条件のファイルがありません: {file}（ct.py new-run が作ります）")
    try:
        spec = FilterSpec.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise SettingsError(f"条件のファイルが正しくありません: {file}\n{exc}") from exc
    if len(spec.streams) != 1:
        raise SettingsError("条件にはストリームを 1 つだけ指定してください")
    return spec
