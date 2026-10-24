from pathlib import Path

import pytest
from pydantic import ValidationError

from jobpilot.config import load_config
from jobpilot.ids import new_id
from tests.helpers import write_min_config


def test_defaults_are_safe(tmp_path: Path) -> None:
    cfg = load_config(write_min_config(tmp_path))
    assert cfg.dry_run is True and cfg.bind_host == "127.0.0.1"


def test_rejects_public_bind(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(write_min_config(tmp_path, bind_host="0.0.0.0"))


def test_loads_routes() -> None:
    cfg = load_config(Path("config"))
    assert cfg.model_routes["resume_tailoring"].model_tag == "gemma4:12b"


def test_new_id_prefix_and_unique() -> None:
    a, b = new_id("job"), new_id("job")
    assert a.startswith("job_") and a != b
