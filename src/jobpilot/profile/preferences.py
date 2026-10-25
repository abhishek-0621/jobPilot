"""Search preferences and company boards; storage/ overrides committed examples."""

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, StringConstraints, field_validator, model_validator

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

    @model_validator(mode="after")
    def _source_extra(self) -> "CompanyBoard":
        if self.source == "workday":
            if not re.fullmatch(r"wd\d{1,2}", self.extra.get("wd_host", "")):
                raise ValueError("workday boards need extra.wd_host matching wd<digits>")
            if "site" not in self.extra:
                raise ValueError("workday boards need extra.site")
        return self


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


COMPANIES_HEADER = ("# Company boards to watch (layout: config/companies.example.yaml).\n"
                    "# Takes precedence over the committed example list.\n")


def append_companies(cfg: AppConfig, boards: list[CompanyBoard]) -> list[CompanyBoard]:
    """Append boards to storage/profiles/companies.yaml (created if missing), keeping existing
    entries and skipping any (source, token) already present. Returns the boards added."""
    path = cfg.storage_path / "profiles" / "companies.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    entries: list[dict[str, object]] = list((data or {}).get("companies") or [])
    seen = {(str(e.get("source")), str(e.get("token"))) for e in entries}
    added: list[CompanyBoard] = []
    for b in boards:
        if (b.source, b.token) in seen:
            continue
        seen.add((b.source, b.token))
        added.append(b)
        entry: dict[str, object] = {"name": b.name, "source": b.source, "token": b.token}
        if b.extra:
            entry["extra"] = dict(b.extra)
        entries.append(entry)
    if added or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(COMPANIES_HEADER + yaml.safe_dump({"companies": entries}, sort_keys=False),
                        encoding="utf-8")
    return added
