import json
from pathlib import Path

import httpx
import pytest

from jobpilot import bench as bench_mod
from jobpilot.bench import bench_tag, run_bench, skill_scores
from jobpilot.config import AppConfig, ModelRoute
from jobpilot.llm.ollama_client import ModelGateway

FIXTURES = Path("tests/fixtures/bench/jobs")


def _resp(skills: list[str]) -> httpx.Response:
    content = json.dumps({
        "required_skills": skills, "preferred_skills": [], "experience_min_years": None,
        "experience_max_years": None, "seniority": "mid", "work_mode": "remote",
        "location": None, "role_family": "ml", "summary": "s", "extraction_confidence": 0.9})
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content},
                                     "done": True, "eval_count": 5})


def _gw(handler, **route: object) -> ModelGateway:
    cfg = AppConfig(model_routes={"job_extraction": ModelRoute(model_tag="primary:1", **route)})
    return ModelGateway(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(storage_path=tmp_path,
                     model_routes={"job_extraction": ModelRoute(model_tag="primary:1")})


def test_skill_scores_normalization() -> None:
    assert skill_scores(["  Python ", "FastAPI.", "Go"], ["python", "fast  api", "fastapi"]) \
        == (pytest.approx(2 / 3), pytest.approx(2 / 3))
    assert skill_scores([], []) == (1.0, 1.0)
    assert skill_scores([], ["a"]) == (0.0, 1.0)


async def test_bench_metrics() -> None:
    fixtures = [("1.json", {"title": "t", "company": "c", "location": None, "description": "d",
                            "expected_required_skills": ["Python", "SQL"]}),
                ("2.json", {"title": "t", "company": "c", "location": None, "description": "d",
                            "expected_required_skills": ["Go"]}),
                ("3.json", {"title": "t", "company": "c", "location": None, "description": "d",
                            "expected_required_skills": ["Rust"]})]
    replies = iter([_resp(["python", "Java"]), _resp(["go."]), httpx.Response(404)])
    gw = _gw(lambda req: next(replies))
    r = await bench_tag(gw, "m:x", "job_extraction", fixtures)
    assert r.schema_valid_rate == pytest.approx(2 / 3)
    assert r.required_skill_recall == pytest.approx((0.5 + 1.0) / 2)
    assert r.required_skill_precision == pytest.approx((0.5 + 1.0) / 2)
    assert r.failures == [{"fixture": "3.json", "error_code": "MODEL_UNAVAILABLE"}]
    assert r.p50_latency_ms >= 0 and r.p95_latency_ms >= r.p50_latency_ms


async def test_generate_model_tag_override() -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(404)

    cfg = AppConfig(model_routes={"job_extraction": ModelRoute(
        model_tag="primary:1", fallback="backup:2")})
    gw = ModelGateway(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    from jobpilot.llm.ollama_client import ModelError
    from jobpilot.agents.extract import _ExtractLLM
    with pytest.raises(ModelError):
        await gw.generate("job_extraction", [], _ExtractLLM, "v1", model_tag="other:3")
    assert [s["model"] for s in seen] == ["other:3"]  # no fallback


async def test_generate_override_reflected_in_meta() -> None:
    gw = _gw(lambda req: _resp(["a"]))
    from jobpilot.agents.extract import _ExtractLLM
    _, meta = await gw.generate("job_extraction", [], _ExtractLLM, "v1", model_tag="other:3")
    assert meta.model_tag == "other:3"


async def test_run_bench_uses_fixtures_and_report(tmp_path: Path) -> None:
    gw = _gw(lambda req: _resp(["Python"]))
    results = await run_bench(_cfg(tmp_path), ["a:1", "b:2"], "job_extraction", FIXTURES, gateway=gw)
    assert [r.tag for r in results] == ["a:1", "b:2"]
    assert all(r.schema_valid_rate == 1.0 for r in results)
    path = bench_mod.write_report(_cfg(tmp_path), "job_extraction", results)
    data = json.loads(path.read_text())
    assert path.parent == tmp_path / "bench" and len(data["results"]) == 2
    assert "a:1" in bench_mod.format_table(results)


def test_fixtures_are_labeled() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert len(files) == 6
    for f in files:
        d = json.loads(f.read_text())
        assert d["expected_required_skills"] and d["description"]
