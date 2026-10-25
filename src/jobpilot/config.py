import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_tag: str
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout_s: float = 120
    keep_alive: str = "10m"
    fallback: str | None = None


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discover_at: str = "08:30"
    backup: bool = True

    @field_validator("discover_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hhmm = r"([01]\d|2[0-3]):[0-5]\d"
        if not re.fullmatch(rf"{hhmm}(\s*,\s*{hhmm})*", v):
            raise ValueError("discover_at must be HH:MM (24h), or several separated by commas")
        return v


class RetentionDays(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: int = Field(365, ge=1)
    contacts: int = Field(180, ge=1)
    exports: int = Field(7, ge=1)


class OutreachConfig(BaseModel):
    """Recruiter outreach drafts. JobPilot only drafts; the user sends."""

    model_config = ConfigDict(extra="forbid")

    auto_draft: bool = True
    campaign: str = "default"

    @field_validator("campaign")
    @classmethod
    def _campaign_safe(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
            raise ValueError("outreach.campaign must match [A-Za-z0-9_-]{1,64}")
        return v


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind_host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = 8765
    db_path: Path = Path("storage/jobpilot.db")
    storage_path: Path = Path("storage")
    dry_run: bool = True
    live_portals: list[Literal["greenhouse", "lever", "ashby"]] = []
    ollama_url: str = "http://127.0.0.1:11434"
    browser_headless: bool = False
    field_aliases_path: Path = Path("config/field_aliases.yaml")
    schedule: ScheduleConfig = ScheduleConfig()
    backup_keep: int = 14
    retention_days: RetentionDays = RetentionDays()
    outreach: OutreachConfig = OutreachConfig()
    model_routes: dict[str, ModelRoute]

    @field_validator("live_portals", mode="before")
    @classmethod
    def _live_portals_list(cls, v: Any) -> Any:
        """``live_portals:`` left empty in YAML means none; duplicates collapse."""
        if v is None:
            return []
        return list(dict.fromkeys(v)) if isinstance(v, list) else v

    @property
    def effective_live_portals(self) -> list[str]:
        """Portals that really submit: only when BOTH dry_run is off and listed."""
        return [] if self.dry_run else list(self.live_portals)

    @property
    def browser_state_dir(self) -> Path:
        return self.storage_path / "browser_state"


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def load_config(config_dir: Path = Path("config")) -> AppConfig:
    app_path = config_dir / "app.yaml"
    app = _read_yaml(app_path) if app_path.exists() else {}
    routes = _read_yaml(config_dir / "model_routes.yaml").get("routes", {})
    app.setdefault("field_aliases_path", config_dir / "field_aliases.yaml")
    return AppConfig(**app, model_routes=routes)
