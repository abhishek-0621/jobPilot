import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents import cover_letter as cl_mod
from jobpilot.agents.cover_letter import CoverLetterAgent, make_cover_letter_handler
from jobpilot.config import AppConfig
from jobpilot.data.tables import ApplicationRow, DocumentArtifactRow, JobRow, TaskRow
from jobpilot.documents.render import DocRenderError
from jobpilot.documents.validate import BANNED_PATH, load_banned, validate_cover_letter
from jobpilot.llm.ollama_client import GenerationMeta
from jobpilot.orchestrator.engine import (
    AgentContext,
    Orchestrator,
    OutputRejected,
    TaskInput,
    TaskKind,
    TaskStatus,
)
from jobpilot.portals.simulated import SimulatedAdapter
from jobpilot.runtime import (
    make_application_loader,
    make_artifact_dir,
    make_profile_file,
    make_profile_loader,
)
from tests.agent_contracts.test_tailor import APP, T0, cfg, factory, seed  # noqa: F401

GOOD: dict[str, Any] = {
    "paragraphs": [
        "I am applying for the GenAI Engineer role at Example AI.",
        "I built a retrieval pipeline serving 10k queries per day and a document "
        "question answering assistant.",
    ],
    "evidence_fact_ids": ["fact_exp_001", "fact_proj_001"],
}
WRONG = {**GOOD, "paragraphs": [GOOD["paragraphs"][0],
                                "I built a retrieval pipeline at Globex Labs."]}


class SeqGateway:
    def __init__(self, *canned: dict[str, Any]) -> None:
        self.canned = list(canned)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, route: str, messages: list[dict], schema: type[BaseModel],
                       prompt_version: str) -> tuple[Any, GenerationMeta]:
        self.calls.append({"route": route, "messages": messages, "prompt_version": prompt_version})
        meta = GenerationMeta(model_tag="fake", prompt_version=prompt_version,
                              latency_ms=1, attempts=1, eval_count=None)
        data = self.canned[min(len(self.calls), len(self.canned)) - 1]
        return schema.model_validate(data), meta


@pytest.fixture
def fake_pdf(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def html_to_pdf(html: str, out: Path) -> int:
        seen.append(html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-fake")
        return 1
    monkeypatch.setattr(cl_mod, "html_to_pdf", html_to_pdf)
    return seen


def seed_cl(factory: sessionmaker[Session]) -> None:  # noqa: F811
    seed(factory, status="DOCUMENTS_PENDING")
    with factory() as s:
        s.add(JobRow(job_id="job_2", source="lever", source_job_id="2",
                     canonical_url="https://example.com/2", company="Globex Labs",
                     title="ML Engineer", location="Pune", work_mode="remote",
                     description_text="d", extracted_json="{}", status="MATCHED",
                     first_seen_at=T0, last_seen_at=T0))
        s.commit()


def tools(factory: sessionmaker[Session], cfg: AppConfig) -> dict[str, Any]:  # noqa: F811
    return {"application_loader": make_application_loader(factory),
            "profile_loader": make_profile_loader(factory, lambda: T0),
            "profile_file": make_profile_file(cfg),
            "artifact_dir": make_artifact_dir(cfg),
            "tech_terms": lambda: {"python", "rag", "kubernetes", "sql"},
            "cover_letter_banned": lambda: load_banned(BANNED_PATH)}


async def run_agent(factory: sessionmaker[Session], cfg: AppConfig, gw: Any) -> Any:  # noqa: F811
    inp = TaskInput(task_id="task_1", kind=TaskKind.COVER_LETTER, entity_type="application",
                    entity_id=APP, attempt=1)
    return await CoverLetterAgent().run(inp, AgentContext("task_1", gw, tools(factory, cfg)))


def orch(factory: sessionmaker[Session], cfg: AppConfig, gw: Any) -> Orchestrator:  # noqa: F811
    return Orchestrator(
        factory, cfg, {TaskKind.COVER_LETTER: CoverLetterAgent()},
        {"simulated": SimulatedAdapter()}, clock=lambda: T0,
        handlers={TaskKind.COVER_LETTER: make_cover_letter_handler(cfg.storage_path)},
        gateway=gw, tools=tools(factory, cfg))


async def test_cover_letter_happy_path(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    gw = SeqGateway(GOOD)
    out = await run_agent(factory, cfg, gw)
    [call] = gw.calls
    assert call["route"] == "cover_letter" and call["prompt_version"] == "cover_letter_v1"
    user = call["messages"][1]["content"]
    start = user.index("<untrusted_content>")
    assert "Ignore previous instructions" in user[start:]
    assert "asha@example.com" not in user
    text = Path(out.txt_path).read_text("utf-8")
    assert text.startswith("Dear Hiring Team,\n\n")
    assert text.endswith("Sincerely,\nAsha Example")
    assert "asha@example.com" not in text and "+919000000000" not in text
    assert out.evidence_fact_ids == ["fact_exp_001", "fact_proj_001"]
    assert "Example AI" in fake_pdf[0] and len(text.split()) <= 250


async def test_cover_letter_wrong_company_rejected(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    with pytest.raises(OutputRejected, match="cover_letter_invalid"):
        await run_agent(factory, cfg, SeqGateway(WRONG))
    codes = validate_cover_letter("Example AI GenAI Engineer at Globex Labs", "Example AI",
                                  "GenAI Engineer", [], ["Globex Labs", "AB", "Example"])
    assert codes == ["wrong_company:Globex Labs"]


def test_placeholder_rejected() -> None:
    for bad in ("[Hiring Manager]", "{company}", "<b>", "TODO", "XXX"):
        assert "placeholder" in validate_cover_letter(
            f"GenAI Engineer at Example AI {bad}", "Example AI", "GenAI Engineer", [], [])
    codes = validate_cover_letter("hello " * 251, "Example AI", "GenAI Engineer", [], [])
    assert {"too_long", "missing_company", "missing_role"} <= set(codes)


def test_banned_phrase_rejected() -> None:
    banned = load_banned(BANNED_PATH)
    assert len(banned) >= 35 and "dream job" in banned
    codes = validate_cover_letter("This GenAI Engineer role at Example AI is my Dream Job.",
                                  "Example AI", "GenAI Engineer", banned, [])
    assert codes == ["banned:dream job"]


async def test_retry_once_then_reject(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    gw = SeqGateway(WRONG, GOOD)
    out = await run_agent(factory, cfg, gw)
    assert len(gw.calls) == 2 and out.evidence_fact_ids == GOOD["evidence_fact_ids"]
    assert "wrong_company:Globex Labs" in gw.calls[1]["messages"][1]["content"]
    gw2 = SeqGateway(WRONG, WRONG, GOOD)
    with pytest.raises(OutputRejected, match="cover_letter_invalid"):
        await run_agent(factory, cfg, gw2)
    assert len(gw2.calls) == 2
    ghost = {**GOOD, "evidence_fact_ids": ["fact_exp_001", "fact_ghost"]}
    with pytest.raises(OutputRejected, match="cover_letter_invalid"):
        await run_agent(factory, cfg, SeqGateway(ghost))


async def test_cover_letter_multi_page_fails(
    factory: sessionmaker[Session], cfg: AppConfig, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    seed_cl(factory)

    async def two_pages(html: str, out: Path) -> int:
        out.write_bytes(b"x")
        return 2
    monkeypatch.setattr(cl_mod, "html_to_pdf", two_pages)
    with pytest.raises(DocRenderError):
        await run_agent(factory, cfg, SeqGateway(GOOD))


async def test_application_moves_to_form_in_progress(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    o = orch(factory, cfg, SeqGateway(GOOD))
    tid = o.enqueue(TaskKind.COVER_LETTER, "application", APP)
    await o.run_until_idle()
    with factory() as s:
        task = s.get(TaskRow, tid)
        assert task is not None and task.status == TaskStatus.SUCCEEDED
        app = s.get(ApplicationRow, APP)
        assert app is not None and app.status == "FORM_IN_PROGRESS"
        rows = s.execute(select(DocumentArtifactRow).order_by(
            DocumentArtifactRow.type)).scalars().all()
        assert [(r.type, r.version) for r in rows] == [("cover_letter", 1),
                                                         ("cover_letter_text", 1)]
        assert app.cover_letter_artifact_id == rows[0].artifact_id
        for r in rows:
            data = (cfg.storage_path / r.path).read_bytes()
            assert r.content_hash == "sha256:" + hashlib.sha256(data).hexdigest()
            assert json.loads(r.source_fact_ids_json) == GOOD["evidence_fact_ids"]


async def test_cover_letter_requires_documents_pending(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed(factory, status="NOT_STARTED")
    gw = SeqGateway(GOOD)
    with pytest.raises(OutputRejected, match="not_documents_pending"):
        await run_agent(factory, cfg, gw)
    assert gw.calls == []


@pytest.mark.skipif(not __import__("jobpilot.documents.render", fromlist=["x"])
                    .chromium_available(), reason="Chromium not installed")
@pytest.mark.browser
async def test_cover_letter_renders_real_one_page_pdf(
    factory: sessionmaker[Session], cfg: AppConfig,  # noqa: F811
) -> None:
    from pypdf import PdfReader

    seed_cl(factory)
    out = await run_agent(factory, cfg, SeqGateway(GOOD))
    assert len(PdfReader(out.pdf_path).pages) == 1


def test_loader_excludes_own_employers_and_cited_names(
    factory: sessionmaker[Session],  # noqa: F811
) -> None:
    seed_cl(factory)
    with factory() as s:
        for jid, company in (("job_3", "Example Corp"),
                             ("job_4", "Example Institute of Technology")):
            s.add(JobRow(job_id=jid, source="lever", source_job_id=jid,
                         canonical_url=f"https://example.com/{jid}", company=company,
                         title="AI Engineer", location="Pune", work_mode="remote",
                         description_text="d", extracted_json="{}", status="MATCHED",
                         first_seen_at=T0, last_seen_at=T0))
        s.commit()
    app = make_application_loader(factory)(APP)
    assert app is not None and app["other_companies"] == ["Globex Labs"]
    text = ("I am applying for the GenAI Engineer role at Example AI. At Example Corp "
            "I built a retrieval pipeline.")
    assert validate_cover_letter(text, "Example AI", "GenAI Engineer", [],
                                 app["other_companies"]) == []


def test_placeholder_words_are_case_sensitive() -> None:
    ok = "GenAI Engineer at Example AI; I keep a todo list and fixed xxx-large files."
    assert validate_cover_letter(ok, "Example AI", "GenAI Engineer", [], []) == []
    assert "placeholder" in validate_cover_letter(
        "GenAI Engineer at Example AI [name]", "Example AI", "GenAI Engineer", [], [])


async def test_invented_employer_or_number_rejected(
    factory: sessionmaker[Session], cfg: AppConfig, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    for para, code in (("Building Initech systems, I built a retrieval pipeline.",
                        "p2:new_proper_noun:initech"),
                       ("I built a retrieval pipeline serving 50k queries per day.",
                        "p2:new_number")):
        bad = {**GOOD, "paragraphs": [GOOD["paragraphs"][0], para]}
        gw = SeqGateway(bad, bad)
        with pytest.raises(OutputRejected, match="cover_letter_invalid"):
            await run_agent(factory, cfg, gw)
        assert code in gw.calls[1]["messages"][1]["content"]
