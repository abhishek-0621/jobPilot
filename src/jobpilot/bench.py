"""Local model benchmark: run the job-extraction prompt over labeled fixtures per model tag."""

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobpilot.agents.extract import PROMPT_VERSION, _ExtractLLM, build_extract_messages
from jobpilot.config import AppConfig
from jobpilot.llm.ollama_client import ModelError, ModelGateway


@dataclass
class BenchResult:
    tag: str
    schema_valid_rate: float
    required_skill_recall: float
    required_skill_precision: float
    p50_latency_ms: float
    p95_latency_ms: float
    failures: list[dict[str, str]] = field(default_factory=list)


def normalize_skill(skill: str) -> str:
    s = " ".join(skill.casefold().split())
    return s[:-1].rstrip() if s.endswith(".") else s


def _percentile(sorted_vals: list[int], pct: float) -> float:
    """Nearest-rank percentile; a single sample is its own p50 and p95."""
    if not sorted_vals:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(sorted_vals)))
    return float(sorted_vals[rank - 1])


def skill_scores(predicted: list[str], expected: list[str]) -> tuple[float, float]:
    pred = {normalize_skill(s) for s in predicted}
    exp = {normalize_skill(s) for s in expected}
    hit = len(pred & exp)
    recall = hit / len(exp) if exp else 1.0
    precision = hit / len(pred) if pred else 1.0
    return recall, precision


async def bench_tag(
    gateway: ModelGateway, tag: str, route: str, fixtures: list[tuple[str, dict]]
) -> BenchResult:
    recalls: list[float] = []
    precisions: list[float] = []
    latencies: list[int] = []
    failures: list[dict[str, str]] = []
    for name, fx in fixtures:
        messages = build_extract_messages(
            fx["title"], fx["company"], fx.get("location"), fx["description"])
        try:
            out, meta = await gateway.generate(
                route, messages, _ExtractLLM, PROMPT_VERSION, model_tag=tag)
        except ModelError as e:
            failures.append({"fixture": name, "error_code": e.code.value})
            continue
        r, p = skill_scores(out.required_skills, fx["expected_required_skills"])
        recalls.append(r)
        precisions.append(p)
        latencies.append(meta.latency_ms)
    ok = len(latencies)
    latencies.sort()
    return BenchResult(
        tag=tag,
        schema_valid_rate=ok / len(fixtures) if fixtures else 0.0,
        required_skill_recall=sum(recalls) / ok if ok else 0.0,
        required_skill_precision=sum(precisions) / ok if ok else 0.0,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        failures=failures,
    )


def load_fixtures(fixtures: Path) -> list[tuple[str, dict]]:
    return [(p.name, json.loads(p.read_text("utf-8"))) for p in sorted(fixtures.glob("*.json"))]


async def run_bench(
    cfg: AppConfig, tags: list[str], route: str, fixtures: Path,
    gateway: ModelGateway | None = None,
) -> list[BenchResult]:
    items = load_fixtures(fixtures)
    gw = gateway or ModelGateway(cfg)
    try:
        return [await bench_tag(gw, tag, route, items) for tag in tags]
    finally:
        if gateway is None:
            await gw.aclose()


def format_table(results: list[BenchResult]) -> str:
    head = f"{'tag':<24}{'valid%':>8}{'recall':>8}{'prec':>8}{'p50 ms':>9}{'p95 ms':>9}  failures"
    rows = [head]
    for r in results:
        rows.append(
            f"{r.tag:<24}{r.schema_valid_rate * 100:>7.0f}%{r.required_skill_recall:>8.2f}"
            f"{r.required_skill_precision:>8.2f}{r.p50_latency_ms:>9.0f}{r.p95_latency_ms:>9.0f}"
            f"  {len(r.failures)}"
        )
    return "\n".join(rows)


def write_report(cfg: AppConfig, route: str, results: list[BenchResult]) -> Path:
    out_dir = cfg.storage_path / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    path.write_text(
        json.dumps({"route": route, "results": [asdict(r) for r in results]}, indent=2),
        encoding="utf-8")
    return path
