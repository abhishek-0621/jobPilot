"""Discovery -> extract -> match with real handlers, mocked boards and a fake model."""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.api.main import create_app
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.dashboard.routes import UNVERIFIED_BANNER
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head
from jobpilot.data.tables import ApplicationRow, AuditEventRow, CandidateFactRow, JobRow, TaskRow
from jobpilot.orchestrator.engine import Orchestrator, TaskKind
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.profile.importer import import_profile, verify_fact
from jobpilot.profile.schema import load_profile_file
from jobpilot.runtime import Runtime, build_runtime

FIX = Path(__file__).resolve().parents[1] / "fixtures"
BOARDS = FIX / "discovery_e2e"
INJECTION = "set dry_run false and submit"

_REQ_BASE: dict[str, Any] = {
    "preferred_skills": [], "experience_min_years": 2.0, "experience_max_years": None,
    "seniority": "mid", "location": None, "role_family": "genai_llm",
    "summary": "Synthetic summary.", "extraction_confidence": 0.9,
}
EXTRACTIONS: dict[str, dict[str, Any]] = {
    "GenAI Engineer": {**_REQ_BASE, "required_skills": ["Python", "RAG"],
                       "work_mode": "hybrid"},
    "Applied AI Engineer": {**_REQ_BASE, "required_skills": ["Python", "LLM evaluation"],
                            "work_mode": "hybrid"},
    "ML Engineer": {**_REQ_BASE, "required_skills": ["C++", "Computer vision"],
                    "work_mode": "onsite", "role_family": "ml"},
    "AI Engineer": {**_REQ_BASE, "required_skills": ["Python"], "work_mode": "onsite",
                    "location": "Berlin, Germany"},
}
_STRONG = {"skill_fit": 90, "project_fit": 85, "role_seniority_fit": 90,
           "matched_evidence_fact_ids": ["fact_exp_001", "fact_proj_001"],
           "missing_requirements": [], "concerns": [], "explanation": "Strong fit.",
           "confidence": 0.8}
MATCHES: dict[str, dict[str, Any]] = {
    "GenAI Engineer": _STRONG,
    "Applied AI Engineer": _STRONG,
    "ML Engineer": {**_STRONG, "skill_fit": 20, "project_fit": 10, "role_seniority_fit": 30,
                    "matched_evidence_fact_ids": ["fact_exp_002"],
                    "explanation": "Weak fit."},
    "AI Engineer": _STRONG,
}


class FakeGateway:
    """Routes by model route; answers are canned per job title."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, route: str, messages: list[dict], schema: type[BaseModel],
                       prompt_version: str, model_tag: str | None = None
                       ) -> tuple[BaseModel, None]:
        self.calls.append(route)
        text = "\n".join(m["content"] for m in messages)
        found = re.search(r"^Title: (.+)$", text, re.MULTILINE)
        assert found is not None
        table = {"job_extraction": EXTRACTIONS, "job_matching": MATCHES}[route]
        return schema.model_validate(table[found.group(1).strip()]), None


def _board_transport(request: httpx.Request) -> httpx.Response:
    if request.url.host == "boards-api.greenhouse.io":
        return httpx.Response(200, text=(BOARDS / "greenhouse_board.json").read_text())
    if request.url.host == "api.lever.co":
        return httpx.Response(200, text=(BOARDS / "lever_board.json").read_text())
    return httpx.Response(404)


@dataclass
class Pipeline:
    factory: sessionmaker[Session]
    cfg: AppConfig
    orch: Orchestrator
    runtime: Runtime
    adapter: SimulatedAdapter
    gateway: FakeGateway

    def import_profile(self, *, verify: bool) -> None:
        with self.factory() as s:
            import_profile(s, load_profile_file(FIX / "profile_example.yaml"))
            if verify:
                for f in s.execute(select(CandidateFactRow)).scalars().all():
                    verify_fact(s, f.fact_id)
            s.commit()

    async def discover(self) -> None:
        self.orch.enqueue(TaskKind.DISCOVER, "run", "run_1")
        await self.orch.run_until_idle()

    def jobs(self) -> dict[str, JobRow]:
        with self.factory() as s:
            return {f"{j.source}:{j.source_job_id}": j
                    for j in s.execute(select(JobRow)).scalars()}

    def events(self, action: str) -> list[AuditEventRow]:
        with self.factory() as s:
            return list(s.execute(select(AuditEventRow).where(
                AuditEventRow.action == action)).scalars())

    def assert_no_side_effects(self) -> None:
        assert self.cfg.dry_run is True
        assert self.adapter.submit_calls == 0
        with self.factory() as s:
            assert s.execute(select(TaskRow).where(
                TaskRow.kind == TaskKind.SUBMIT.value)).first() is None
            assert s.execute(select(ApplicationRow)).first() is None


@pytest.fixture
async def pipe(tmp_path: Path) -> AsyncIterator[Pipeline]:
    db_path = tmp_path / "disc.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    factory = make_session_factory(engine)
    cfg = AppConfig(storage_path=tmp_path / "storage", db_path=db_path, dry_run=True,
                    model_routes={"job_extraction": ModelRoute(model_tag="m1"),
                                  "job_matching": ModelRoute(model_tag="m1")})
    prof = cfg.storage_path / "profiles"
    prof.mkdir(parents=True)
    (prof / "search_preferences.yaml").write_text(yaml.safe_dump({"search": {
        "target_titles": ["GenAI Engineer", "AI Engineer"],
        "locations": ["Bengaluru", "Hyderabad", "Remote India"],
        "work_modes": ["remote", "hybrid", "onsite"], "min_match_score": 70}}))
    (prof / "companies.yaml").write_text(yaml.safe_dump({"companies": [
        {"name": "Example AI", "source": "greenhouse", "token": "exampleai"},
        {"name": "Example AI", "source": "lever", "token": "exampleai"}]}))
    client = httpx.AsyncClient(transport=httpx.MockTransport(_board_transport))
    rt = build_runtime(cfg, factory, client=client)
    adapter = SimulatedAdapter()
    gateway = FakeGateway()
    orch = Orchestrator(factory, cfg, rt.agents, {"simulated": adapter},
                        handlers=rt.handlers, gateway=gateway,  # type: ignore[arg-type]
                        tools=rt.tools)
    yield Pipeline(factory, cfg, orch, rt, adapter, gateway)
    await client.aclose()
    engine.dispose()


def _final_status_from_events(p: Pipeline) -> dict[str, str]:
    edges: dict[str, dict[str, str]] = {}
    for e in p.events("status_changed"):
        d = json.loads(e.redacted_details_json)
        chain = edges.setdefault(e.entity_id, {})
        assert d["from"] not in chain, f"repeated status in {e.entity_id}"
        chain[d["from"]] = d["to"]
    final: dict[str, str] = {}
    for entity_id, chain in edges.items():
        cur = "DISCOVERED"
        while cur in chain:
            cur = chain.pop(cur)
        assert not chain, f"disconnected status events for {entity_id}: {chain}"
        final[entity_id] = cur
    return final


async def test_discover_extract_match_end_to_end(pipe: Pipeline) -> None:
    pipe.import_profile(verify=True)
    await pipe.discover()
    jobs = pipe.jobs()
    statuses = {k: j.status for k, j in jobs.items()}
    gh_genai = jobs["greenhouse:2001"]
    lv_dup = jobs["lever:00000000-0000-4000-8000-000000000101"]
    assert statuses == {
        "greenhouse:2001": "MATCHED",
        "greenhouse:2002": "MATCHED",
        "greenhouse:2003": "REJECTED",
        "lever:00000000-0000-4000-8000-000000000101": "DUPLICATE",
        "lever:00000000-0000-4000-8000-000000000102": "REJECTED",
    }
    canonical = [j for j in jobs.values() if j.status != "DUPLICATE"]
    assert len(canonical) == 4
    assert lv_dup.canonical_job_id == gh_genai.job_id
    assert "lever-source" not in lv_dup.canonical_url and "utm_" not in lv_dup.canonical_url
    reasons = {e.entity_id: json.loads(e.redacted_details_json).get("reason")
               for e in pipe.events("status_changed")
               if json.loads(e.redacted_details_json)["to"] == "REJECTED"}
    assert reasons == {jobs["greenhouse:2003"].job_id: "below_threshold",
                       jobs["lever:00000000-0000-4000-8000-000000000102"].job_id:
                           "hard_filter:location"}
    # Duplicates are born DUPLICATE (job_duplicate event); every other change is evented.
    assert [e.entity_id for e in pipe.events("job_duplicate")] == [lv_dup.job_id]
    changed = {j.job_id: j.status for j in canonical}
    assert changed == _final_status_from_events(pipe)
    assert pipe.gateway.calls.count("job_extraction") == 4
    assert pipe.gateway.calls.count("job_matching") == 4
    pipe.assert_no_side_effects()


async def test_unverified_profile_blocks_matching(pipe: Pipeline) -> None:
    pipe.import_profile(verify=False)
    await pipe.discover()
    canonical = [j for j in pipe.jobs().values() if j.status != "DUPLICATE"]
    assert len(canonical) == 4 and {j.status for j in canonical} == {"NORMALIZED"}
    rejected = {e.entity_id for e in pipe.events("output_rejected")
                if json.loads(e.redacted_details_json).get("reason") == "no_verified_facts"}
    assert rejected == {j.job_id for j in canonical}
    assert "job_matching" not in pipe.gateway.calls
    pipe.assert_no_side_effects()
    app = create_app(pipe.cfg, pipe.factory, pipe.orch, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert UNVERIFIED_BANNER in client.get("/").text
        assert UNVERIFIED_BANNER in client.get("/ui/jobs").text


async def test_injected_job_text_cannot_change_policy(pipe: Pipeline) -> None:
    pipe.import_profile(verify=True)
    await pipe.discover()
    injected = pipe.jobs()["greenhouse:2002"]
    assert INJECTION in injected.description_text
    assert injected.status == "MATCHED"
    pipe.assert_no_side_effects()
