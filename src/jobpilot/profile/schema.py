"""Pydantic mirror of the profile YAML layout. Unknown keys are forbidden."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

Scalar = str | int


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Address(_Strict):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: Scalar | None = None
    country: str | None = None
    country_code: str | None = None


class Identity(_Strict):
    full_name: str
    email: str
    first_name: str | None = None
    last_name: str | None = None
    phone_country_code: Scalar | None = None
    phone_national: Scalar | None = None
    phone_e164: str | None = None
    location_display: str | None = None
    address: Address | None = None
    source: str | None = None


class Employment(_Strict):
    employer: str
    title: str
    location: str | None = None
    since: str | None = None
    company_start: str | None = None
    total_experience_years: float | None = None


class FactEntry(_Strict):
    fact_id: str
    value: str
    source: str | None = None


class Education(_Strict):
    fact_id: str
    degree: str
    field: str | None = None
    institution: str
    end: Scalar | None = None
    source: str | None = None


class Role(_Strict):
    role_id: str
    employer: str
    title: str
    start: Scalar | None = None
    end: Scalar | None = None
    facts: list[FactEntry] = []


class Project(_Strict):
    project_id: str
    name: str
    start: Scalar | None = None
    end: Scalar | None = None
    facts: list[FactEntry] = []


class ProfileFile(_Strict):
    profile_version: int
    default_resume: str
    identity: Identity
    links: dict[str, str] = {}
    languages: list[str] = []
    current_employment: Employment
    education: list[Education] = []
    experience: list[Role] = []
    projects: list[Project] = []
    skills: dict[str, list[str]] = {}
    achievements: list[FactEntry] = []
    answer_library: dict[str, Any] = {}
    open_questions: list[str] = []

    @model_validator(mode="after")
    def _unique_fact_ids(self) -> "ProfileFile":
        ids = [e.fact_id for e in self.education]
        ids += [f.fact_id for r in self.experience for f in r.facts]
        ids += [f.fact_id for p in self.projects for f in p.facts]
        ids += [f.fact_id for f in self.achievements]
        ids += [f"fact_skill_{g}" for g in self.skills]
        seen: set[str] = set()
        dups = sorted({i for i in ids if i in seen or seen.add(i)})
        if dups:
            raise ValueError(f"duplicate fact_id: {', '.join(dups)}")
        return self


def load_profile_file(path: Path) -> ProfileFile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ProfileFile.model_validate(data or {})
