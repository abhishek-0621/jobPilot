from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig, ModelRoute, load_config
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.orchestrator.engine import Orchestrator
from jobpilot.portals.simulated import SimulatedAdapter
from tests.factories import make_app_row

Portal = Literal["greenhouse", "lever"]


class FakeSubmitter(SimulatedAdapter):
    """Stands in for the real LiveSubmitter (arrives in a later task)."""

    def __init__(self, portal: str) -> None:
        super().__init__()
        self.portal = portal


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    upgrade_to_head(tmp_path / "live.db")
    engine = make_engine(tmp_path / "live.db")
    yield make_session_factory(engine)
    engine.dispose()


def make_cfg(tmp_path: Path, dry_run: bool, live: list[Portal]) -> AppConfig:
    return AppConfig(storage_path=tmp_path, dry_run=dry_run, live_portals=live,
                     model_routes={"a": ModelRoute(model_tag="m1")})


def test_defaults_dry_run_and_no_live() -> None:
    cfg = load_config(Path("config"))
    assert cfg.dry_run is True
    assert cfg.live_portals == []
    assert cfg.effective_live_portals == []


@pytest.mark.parametrize(("dry_run", "live", "expect_live"), [
    (True, [], False),
    (True, ["greenhouse"], False),
    (False, [], False),
    (False, ["greenhouse"], True),
])
def test_live_requires_both_switches(
    factory: sessionmaker[Session], tmp_path: Path,
    dry_run: bool, live: list[Portal], expect_live: bool,
) -> None:
    sim = SimulatedAdapter()
    orch = Orchestrator(factory, make_cfg(tmp_path, dry_run, live), {}, {"simulated": sim},
                        submitter_factory=FakeSubmitter)
    with factory() as s:
        app = make_app_row(s)
        app.portal = "greenhouse"
        adapter, simulated = orch._adapter_for(app)
    if expect_live:
        assert isinstance(adapter, FakeSubmitter) and adapter.portal == "greenhouse"
        assert simulated is False
    else:
        assert adapter is sim and simulated is True


def test_other_portal_stays_simulated_and_no_factory_is_safe(
    factory: sessionmaker[Session], tmp_path: Path,
) -> None:
    cfg = make_cfg(tmp_path, False, ["greenhouse"])
    sim = SimulatedAdapter()
    with factory() as s:
        app = make_app_row(s)
        app.portal = "lever"
        assert Orchestrator(factory, cfg, {}, {"simulated": sim},
                            submitter_factory=FakeSubmitter)._adapter_for(app) == (sim, True)
        app.portal = "greenhouse"
        assert Orchestrator(factory, cfg, {}, {"simulated": sim})._adapter_for(app) == (sim, True)


def _client(factory: sessionmaker[Session], cfg: AppConfig) -> TestClient:
    orch = Orchestrator(factory, cfg, {}, {"simulated": SimulatedAdapter()})
    return TestClient(create_app(cfg, factory, orch, None, start_worker=False),
                      base_url="http://127.0.0.1")


def test_banner_shown_when_live(factory: sessionmaker[Session], tmp_path: Path) -> None:
    live = _client(factory, make_cfg(tmp_path, False, ["greenhouse", "lever"])).get("/")
    assert "LIVE SUBMISSION ON for: greenhouse, lever" in live.text
    off = _client(factory, make_cfg(tmp_path, True, ["greenhouse"])).get("/")
    assert "LIVE SUBMISSION" not in off.text
    assert "Dry run — nothing is submitted" in off.text


def test_health_reports_live_portals(factory: sessionmaker[Session], tmp_path: Path) -> None:
    on = _client(factory, make_cfg(tmp_path, False, ["lever"])).get("/health").json()
    assert on["live_portals"] == ["lever"]
    off = _client(factory, make_cfg(tmp_path, True, ["lever"])).get("/health").json()
    assert off["live_portals"] == []


def test_live_portals_none_and_duplicates(tmp_path: Path) -> None:
    routes = {"a": ModelRoute(model_tag="m1")}
    assert AppConfig(live_portals=None, model_routes=routes).live_portals == []
    cfg = AppConfig(live_portals=["lever", "greenhouse", "lever"], model_routes=routes)
    assert cfg.live_portals == ["lever", "greenhouse"]


def test_no_portal_stays_simulated(factory: sessionmaker[Session], tmp_path: Path) -> None:
    sim = SimulatedAdapter()
    orch = Orchestrator(factory, make_cfg(tmp_path, False, ["greenhouse"]), {},
                        {"simulated": sim}, submitter_factory=FakeSubmitter)
    with factory() as s:
        app = make_app_row(s)
        app.portal = None
        assert orch._adapter_for(app) == (sim, True)
