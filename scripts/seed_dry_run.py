"""Run the deterministic stub pipeline against the configured DB so the dashboard has data.

Usage: uv run python scripts/seed_dry_run.py   (refuses unless config/app.yaml has dry_run: true)
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobpilot.api.actions import approve_and_queue  # noqa: E402
from jobpilot.config import load_config  # noqa: E402
from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head  # noqa: E402
from jobpilot.ids import new_id  # noqa: E402
from jobpilot.orchestrator.approval import ApprovalService  # noqa: E402
from jobpilot.orchestrator.engine import TaskKind  # noqa: E402
from jobpilot.portals.simulated import SimulatedAdapter  # noqa: E402
from tests.e2e.stub_agents import ready_application_ids, stub_orchestrator  # noqa: E402


async def main(config_dir: Path = ROOT / "config") -> int:
    cfg = load_config(config_dir)
    if not cfg.dry_run:
        print("Refusing to seed: dry_run is not true in config/app.yaml.", file=sys.stderr)
        return 1
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(cfg.db_path)
    engine = make_engine(cfg.db_path)
    factory = make_session_factory(engine)
    orch = stub_orchestrator(factory, cfg, SimulatedAdapter())
    try:
        with factory() as s:
            before = set(ready_application_ids(s))
        orch.enqueue(TaskKind.DISCOVER, "run", new_id("run"))
        await orch.run_until_idle()
        with factory() as s:
            fresh = [a for a in ready_application_ids(s) if a not in before]
        if not fresh:
            print("Seed produced no new applications (jobs already seeded?).")
            return 0
        with factory() as s:
            batch = ApprovalService(s).build_batch(fresh)
            s.commit()
        with factory() as s:
            approval = approve_and_queue(s, orch, batch.batch_id, batch.version,
                                         batch.content_hash or "", fresh[:1])
        await orch.run_until_idle()
        print(f"Seeded {len(fresh)} applications; batch {batch.batch_id} "
              f"(approval {approval.approval_id}): 1 simulated submission, "
              f"{len(fresh) - 1} awaiting review.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
