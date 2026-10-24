"""Inputs for form-field mapping: profile facts, answer library, artifacts, aliases."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobpilot.contracts.enums import FieldDecision
from jobpilot.profile.schema import Education, Employment, Identity


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AliasEntry(_Frozen):
    profile: str | list[str] | None = None
    aliases: list[str] = []
    formats: dict[str, str] = {}
    options_match: list[str] = []
    decision: FieldDecision | None = None
    rule: str | None = None


class AliasConfig(_Frozen):
    version: int = 1
    fields: dict[str, AliasEntry]

    @model_validator(mode="after")
    def _unique_aliases(self) -> "AliasConfig":
        seen: dict[str, str] = {}
        for key, entry in self.fields.items():
            for alias in entry.aliases:
                norm = " ".join(alias.casefold().split())
                if norm in seen and seen[norm] != key:
                    raise ValueError(f"alias '{alias}' appears under '{seen[norm]}' and '{key}'")
                seen[norm] = key
        return self


class BoundArtifacts(_Frozen):
    resume_path: Path | None = None
    cover_letter_path: Path | None = None
    cover_letter_text: str | None = None


class MappingContext(_Frozen):
    identity: Identity
    links: dict[str, str] = {}
    current_employment: Employment | None = None
    education: list[Education] = []
    languages: list[str] = []
    library: dict[str, tuple[Any, FieldDecision]] = {}
    artifacts: BoundArtifacts = Field(default_factory=BoundArtifacts)
    aliases: AliasConfig
    years_experience: float | None = None


def load_aliases(path: Path) -> AliasConfig:
    return AliasConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
