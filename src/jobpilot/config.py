from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_tag: str
    temperature: float = 0.0
    num_ctx: int = 8192
    timeout_s: float = 120
    keep_alive: str = "10m"
    fallback: str | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind_host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = 8765
    db_path: Path = Path("storage/jobpilot.db")
    storage_path: Path = Path("storage")
    dry_run: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    browser_headless: bool = False
    model_routes: dict[str, ModelRoute]

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
    return AppConfig(**app, model_routes=routes)
