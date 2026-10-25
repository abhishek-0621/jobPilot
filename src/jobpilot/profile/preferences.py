"""Search preferences and company boards; storage/ overrides committed examples."""

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, StringConstraints, field_validator

from jobpilot.config import AppConfig

CONFIG_DIR = Path("config")
_SAFE = r"^[A-Za-z0-9_-]{1,64}$"
_SAFE_RE = re.compile(_SAFE)


class SearchPreferences(BaseModel):
    target_titles: list[str] = []
    locations: list[str] = []
    work_modes: list[str] = []
    sources: list[str] = []
    min_match_score: float = 70
    max_jobs_per_run: int = 100
    exclude_keywords: list[str] = []
    require_user_review_for_new_sources: bool = True


class CompanyBoard(BaseModel):
    name: str
    source: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday"]
    token: Annotated[str, StringConstraints(pattern=_SAFE)]
    extra: dict[str, str] = {}

    @field_validator("extra")
    @classmethod
    def _extra_safe(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if not _SAFE_RE.fullmatch(k) or not _SAFE_RE.fullmatch(val):
                raise ValueError("extra keys and values must match [A-Za-z0-9_-]{1,64}")
        return v


def _pick(cfg: AppConfig, override: str, default: str) -> Path:
    p = cfg.storage_path / "profiles" / override
    return p if p.exists() else CONFIG_DIR / default


def load_search_preferences(cfg: AppConfig) -> SearchPreferences:
    path = _pick(cfg, "search_preferences.yaml", "search_preferences.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SearchPreferences(**(data.get("search") or {}))


def load_companies(cfg: AppConfig) -> list[CompanyBoard]:
    path = _pick(cfg, "companies.yaml", "companies.example.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [CompanyBoard(**c) for c in data.get("companies") or []]
