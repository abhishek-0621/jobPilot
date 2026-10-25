"""Search preferences and company boards; storage/ overrides committed examples."""

import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, StringConstraints, field_validator, model_validator

from jobpilot.config import AppConfig
from jobpilot.contracts.models import is_http_url

CONFIG_DIR = Path("config")
_SAFE = r"^[A-Za-z0-9_-]{1,64}$"
_SAFE_RE = re.compile(_SAFE)
CAREERS_URL_KEY = "careers_url"  # optional public careers page (outreach inbox lookup)
# Public remote-job boards (one feed each, not per company); always the manual apply path.
RemoteBoardName = Literal["himalayas", "remotive", "weworkremotely", "remoteok", "jobicy",
                          "workingnomads"]
REMOTE_BOARDS: tuple[str, ...] = ("himalayas", "remotive", "weworkremotely", "remoteok",
                                  "jobicy", "workingnomads")


class SearchPreferences(BaseModel):
    target_titles: list[str] = []
    locations: list[str] = []
    work_modes: list[str] = []
    sources: list[str] = []
    min_match_score: float = 70
    max_jobs_per_run: int = 100
    exclude_keywords: list[str] = []
    require_user_review_for_new_sources: bool = True
    # Which public remote boards discovery reads (default: all of them).
    remote_boards: list[RemoteBoardName] = list(REMOTE_BOARDS)  # type: ignore[arg-type]


class CompanyBoard(BaseModel):
    name: str
    source: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday"]
    token: Annotated[str, StringConstraints(pattern=_SAFE)]
    extra: dict[str, str] = {}

    @field_validator("extra")
    @classmethod
    def _extra_safe(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if k == CAREERS_URL_KEY:
                if not is_http_url(val) or any(c.isspace() for c in val) or len(val) > 500:
                    raise ValueError("extra.careers_url must be an http(s) URL")
                continue
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


class CompaniesFileError(Exception):
    """storage/profiles/companies.yaml exists but is not a usable companies list."""


def _read_entries(path: Path) -> list[dict[str, object]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise CompaniesFileError(
            f"{path} is not valid YAML; fix or remove it, then retry ({type(e).__name__})") from e
    if data is None:
        return []
    if not isinstance(data, dict):
        raise CompaniesFileError(f"{path} must be a mapping with a 'companies' list")
    entries = data.get("companies")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise CompaniesFileError(f"{path} must be a mapping with a 'companies' list")
    if not all(isinstance(e, dict) for e in entries):
        raise CompaniesFileError(f"{path}: every entry under 'companies' must be a mapping")
    return list(entries)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".companies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_companies(cfg: AppConfig, boards: list[CompanyBoard]) -> list[CompanyBoard]:
    """Append boards to storage/profiles/companies.yaml (created if missing), keeping existing
    entries and skipping any (source, token) already present. Returns the boards added.
    Raises CompaniesFileError, leaving the file untouched, if the existing file is unusable.
    The file is rewritten atomically; hand-written comments are not preserved."""
    path = cfg.storage_path / "profiles" / "companies.yaml"
    entries = _read_entries(path) if path.exists() else []
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
        _write_atomic(path, COMPANIES_HEADER + yaml.safe_dump({"companies": entries},
                                                              sort_keys=False))
    return added
