"""Every agent that writes text for the user cleans it, rejects stock "AI voice"
phrasing, retries once naming the phrases, then falls back like any other rejection."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.agents.outreach import OutreachAgent
from jobpilot.contracts.enums import FieldDecision
from jobpilot.documents.model import ResumeDoc
from jobpilot.orchestrator.engine import OutputRejected
from tests.agent_contracts import test_cover_letter as tcl
from tests.agent_contracts import test_essay as te
from tests.agent_contracts import test_outreach as to
from tests.agent_contracts.test_cover_letter import fake_pdf, seed_cl  # noqa: F401
from tests.agent_contracts.test_tailor import (  # noqa: F401
    CANNED,
    cfg,
    factory,
    fake_render,
    run_agent,
    seed,
)

DASHES = "—–―"


def _no_dash(text: str) -> None:
    assert not any(d in text for d in DASHES)
    assert "​" not in text


def _retry_note(gw: Any) -> str:
    return gw.calls[1]["messages"][1]["content"]


# ---- tailor -------------------------------------------------------------------------

STOCK_REWRITE = {"fact_id": "fact_exp_001",
                 "text": "Leveraged a retrieval pipeline serving 10k queries per day"}
DASH_REWRITE = {"fact_id": "fact_exp_001",
                "text": "Built a retrieval pipeline — serving 10k​ queries per day"}


async def test_tailor_retries_then_keeps_original_bullet(
    factory: sessionmaker[Session], cfg: Any, fake_render: list[ResumeDoc],  # noqa: F811
) -> None:
    seed(factory)
    gw = tcl.SeqGateway({**CANNED, "bullet_rewrites": [STOCK_REWRITE]})
    out = await run_agent(factory, cfg, gw)
    assert len(gw.calls) == 2 and "leveraged" in _retry_note(gw)
    assert out.rewrites_rejected == ["fact_exp_001"]
    assert fake_render[-1].roles[0].bullets[0].text == \
        "Built a retrieval pipeline serving 10k queries per day"


async def test_tailor_retry_success_uses_clean_rewrite(
    factory: sessionmaker[Session], cfg: Any, fake_render: list[ResumeDoc],  # noqa: F811
) -> None:
    seed(factory)
    gw = tcl.SeqGateway({**CANNED, "summary": "I delve into retrieval pipelines."},
                        {**CANNED, "bullet_rewrites": [DASH_REWRITE]})
    out = await run_agent(factory, cfg, gw)
    assert len(gw.calls) == 2
    text = fake_render[-1].roles[0].bullets[0].text
    assert text == "Built a retrieval pipeline, serving 10k queries per day"
    _no_dash(text)
    assert out.summary == CANNED["summary"]


async def test_tailor_stock_summary_falls_back(
    factory: sessionmaker[Session], cfg: Any, fake_render: list[ResumeDoc],  # noqa: F811
) -> None:
    seed(factory)
    gw = tcl.SeqGateway({**CANNED, "summary": "A testament to a seamless retrieval pipeline."})
    out = await run_agent(factory, cfg, gw)
    assert out.summary == "AI Engineer. 4+ years; Python, RAG."


async def test_tailor_clean_output_single_call(
    factory: sessionmaker[Session], cfg: Any, fake_render: list[ResumeDoc],  # noqa: F811
) -> None:
    seed(factory)
    gw = tcl.SeqGateway(CANNED)
    await run_agent(factory, cfg, gw)
    assert len(gw.calls) == 1


# ---- cover letter ---------------------------------------------------------------------

def _cl(p2: str) -> dict[str, Any]:
    return {**tcl.GOOD, "paragraphs": [tcl.GOOD["paragraphs"][0], p2]}


async def test_cover_letter_rejects_stock_and_exclamation_then_fails(
    factory: sessionmaker[Session], cfg: Any, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    bad = _cl("I built a retrieval pipeline serving 10k queries per day and a document "
              "question answering assistant, a pivotal step!")
    gw = tcl.SeqGateway(bad)
    with pytest.raises(OutputRejected, match="cover_letter_invalid"):
        await tcl.run_agent(factory, cfg, gw)
    note = _retry_note(gw)
    assert "pivotal" in note and "exclamation" in note


async def test_cover_letter_cleans_dashes_and_invisibles(
    factory: sessionmaker[Session], cfg: Any, fake_pdf: list[str],  # noqa: F811
) -> None:
    seed_cl(factory)
    gw = tcl.SeqGateway(_cl("I built a retrieval pipeline serving 10k queries per day "
                            "— and a document​ question answering assistant."))
    out = await tcl.run_agent(factory, cfg, gw)
    assert len(gw.calls) == 1
    _no_dash(out.text)
    _no_dash(Path(out.txt_path).read_text("utf-8"))
    assert "per day, and a document question" in out.text


# ---- outreach -----------------------------------------------------------------------

async def test_outreach_stock_phrase_falls_back_to_template() -> None:
    bad = {**to.GOOD, "sentences": ["I built a cutting-edge retrieval pipeline serving 10k "
                                    "queries per day.", "Thank you for your time."]}
    gw = to.SeqGateway(bad)
    out = await OutreachAgent().run(to.INP, to._ctx(gw, to._context()))
    assert len(gw.calls) == 2 and "cutting-edge" in _retry_note(gw)
    assert out.used_fallback
    assert "cutting-edge" not in out.message_text
    _no_dash(out.message_text)


async def test_outreach_rejects_exclamation_and_markdown() -> None:
    bad = {**to.GOOD, "sentences": ["**I built** a retrieval pipeline serving 10k queries "
                                    "per day!", "Thank you for your time."]}
    gw = to.SeqGateway(bad, to.GOOD)
    out = await OutreachAgent().run(to.INP, to._ctx(gw, to._context()))
    assert not out.used_fallback and len(gw.calls) == 2
    assert "exclamation" in _retry_note(gw) and "markdown" in _retry_note(gw)


# ---- essay (form prep + manual package drafts) ------------------------------------------

def test_essay_stock_phrase_asks_user() -> None:
    gw = te.SeqGateway(te.ok(te.PROUD_OK + " It was a testament to teamwork."))
    d = te.draft(gw, te.K.PROUD_OF, te.bank(), "What are you most proud of?")
    assert d.decision == FieldDecision.ASK_USER
    assert "stock_phrase:testament to" in (d.note or "")
    assert len(gw.calls) == 2 and "testament to" in _retry_note(gw)


def test_essay_answer_is_cleaned() -> None:
    dirty = te.PROUD_OK.replace("live support chats.", "live support chats — fast.")
    dirty = dirty.replace("I profiled", "I​ profiled")
    gw = te.SeqGateway(te.ok(dirty))
    d = te.draft(gw, te.K.PROUD_OF, te.bank(), "What are you most proud of?")
    assert d.decision == FieldDecision.FILL_AND_REVIEW
    _no_dash(d.answer or "")
    assert "chats, fast." in (d.answer or "")
