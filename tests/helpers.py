from pathlib import Path
from typing import Any

import yaml


def write_min_config(tmp_path: Path, **app_overrides: Any) -> Path:
    """Write a minimal config dir (app.yaml overrides + one model route)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(yaml.safe_dump(app_overrides))
    routes = {"routes": {"job_extraction": {"model_tag": "test:model"}}}
    (config_dir / "model_routes.yaml").write_text(yaml.safe_dump(routes))
    return config_dir
