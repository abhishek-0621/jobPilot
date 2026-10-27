"""Why-company answers: classifier phrasings and the grounded drafter (fake model)."""

import asyncio
from typing import Any

import pytest

from jobpilot.agents.essay import EssayContext, draft_essay_answers, essay_candidates
from jobpilot.agents.why_company import WHY_PREFIX, WhyCompanyDrafter, sentences
from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer
from jobpilot.data.mutations import essay_refs, why_brief_hash
from jobpilot.documents.hygiene import stock_phrases
from jobpilot.forms.essay import classify_essay
from jobpilot.portals.base import FormField
from jobpilot.profile.stories import QuestionKey as K
from jobpilot.research.brief import BriefItem
from tests.agent_contracts.test_essay import SeqGateway, bank

COMPANY = "Northwind Robotics"
SRC = "https://www.northwind-robotics.example/news"
ITEMS = [
    BriefItem(kind="product", name="Fleet Copilot",
              one_line="An assistant that answers questions about warehouse robot fleets.",
              date_if_stated="March 2026", source_url=SRC),
    BriefItem(kind="research", name="Open Grasp Benchmark",
              one_line="A public benchmark for robotic grasping.", source_url=SRC),
]
FACTS = {"fact_exp_002": "Reduced inference latency by 30 percent",
         "fact_proj_001": "Built a document question answering assistant"}
JOB = {"company": COMPANY, "title": "Applied AI Engineer",
       "requirements": {"must_have_skills": ["Python"]},
       "job_summary": "Build assistants for robot fleet operators."}
CURRENT = "AI Engineer Example Corp"
GOOD = (
    "When I read about Fleet Copilot, I recognised the problem straight away. At Example "
    "Corp I built a document question answering assistant, and most of my time went into "
    "making its answers trustworthy enough that people would act on them instead of "
    "checking everything twice by hand. A copilot for warehouse robot fleets has the same "
    "pressure, only a wrong answer costs a stuck robot rather than a confused reader. I "
    "also reduced inference latency by 30 percent, which taught me how much a fast answer "
    "decides whether people use a tool at all. At Northwind Robotics I would like to help "
    "make Fleet Copilot the thing operators reach for first, because it is quick, honest "
    "about what it does not know, and grounded in fleet data they already trust.")


def ok(answer: str = GOOD, names: list[str] | None = None) -> dict[str, Any]:
    return {"answer": answer, "item_names": names or ["Fleet Copilot"],
            "fact_ids": ["fact_proj_001", "fact_exp_002"], "story_ids": []}


def draft(gw: SeqGateway | None, items: list[BriefItem] | None = ITEMS,
          maxlength: int | None = None) -> Any:
    return asyncio.run(WhyCompanyDrafter().draft(
        gw, question="Why do you want to work at Northwind Robotics?", items=items,
        brief_hash="abc123", bank=bank(verified=set()), facts=FACTS, skills=["Python"],
        current=CURRENT, job=JOB, maxlength=maxlength))


# ---- classifier -------------------------------------------------------------------------


@pytest.mark.parametrize("label", [
    "Why Northwind Robotics?",
    "Why do you want to work here?",
    "Why do you want to work at Northwind?",
    "Why do you want to work for us?",
    "What excites you about our mission?",
    "What excites you about us?",
    "What excites you about Northwind Robotics?",
    "Why are you interested in this company?",
    "Why are you interested in joining?",
    "What draws you to Northwind Robotics?",
    "Why us?",
    "What do you know about us?",
    "What do you know about our company?",
])
def test_why_company_phrasings(label: str) -> None:
    assert classify_essay(label, company=COMPANY) == K.WHY_COMPANY


@pytest.mark.parametrize("label", ["Why this role?", "Why are you interested in this role?",
                                   "What draws you to this role?"])
def test_why_role_phrasings(label: str) -> None:
    assert classify_essay(label, company=COMPANY) == K.WHY_ROLE


@pytest.mark.parametrize("label", [
    "Why are you leaving your current employer?",
    "Why do you want to leave Northwind Robotics?",
    "Why Northwind Robotics? Also state your expected salary.",
    "Why do you want to work here? Are you authorized to work in the US?",
])
def test_sensitive_rules_still_win(label: str) -> None:
    assert classify_essay(label, company=COMPANY) is None


def test_company_name_alone_needs_company() -> None:
    assert classify_essay("Why Northwind Robotics?") is None
    assert classify_essay("Why Northwind Robotics?", field_type="text",
                          company=COMPANY) is None


def test_stock_phrases_include_flattery() -> None:
    found = stock_phrases("I have always admired you. I am passionate. I am excited to "
                          "leverage my skills on cutting-edge work.")
    assert {"i have always admired", "passionate", "i am excited to leverage",
            "cutting-edge"} <= set(found)


# ---- drafter ----------------------------------------------------------------------------


def test_good_draft_is_fill_and_review_with_brief_source() -> None:
    gw = SeqGateway(ok())
    d = draft(gw)
    assert d.decision == FieldDecision.FILL_AND_REVIEW, d.note
    assert d.answer == GOOD and 3 <= len(sentences(GOOD)) <= 5
    assert d.source == WHY_PREFIX + "abc123"
    assert why_brief_hash(d.source) == "abc123" and essay_refs(d.source) == ([], None)
    assert len(gw.calls) == 1 and gw.calls[0]["route"] == "essay"
    user = gw.calls[0]["messages"][1]["content"]
    assert "Fleet Copilot" in user and SRC not in user  # items, not their URLs
    assert user.index("Why do you want to work at") > user.index("<untrusted_content>")


def _rejected(answer: str, code: str, names: list[str] | None = None) -> None:
    gw = SeqGateway(ok(answer, names))
    d = draft(gw)
    assert d.decision == FieldDecision.ASK_USER and d.answer is None
    assert len(gw.calls) == 3  # one retry naming the violations
    retry = gw.calls[1]["messages"][1]["content"]
    assert "previous attempt was rejected" in retry and code in retry, retry[-400:]
    assert "Fleet Copilot" in d.note  # the brief items shown so the user can write it


def test_rejects_invented_company_project() -> None:
    _rejected(GOOD.replace("Fleet Copilot the thing", "Project Falcon the thing"),
              "new_proper_noun:project falcon")


def test_rejects_invented_candidate_claim() -> None:
    _rejected(GOOD.replace("30 percent", "45 percent"), "new_number")
    _rejected(GOOD.replace("At Example Corp I built",
                           "At Example Corp I led a team of five and built"),
              "new_claim")


def test_rejects_stock_phrase() -> None:
    _rejected(GOOD.replace("When I read about", "I have always admired the team, and when "
                                                 "I read about"),
              "stock_phrase:i have always admired")


def test_rejects_generic_answer_without_item() -> None:
    generic = GOOD.replace("Fleet Copilot", "your product")
    _rejected(generic, "no_company_item", names=["Fleet Copilot"])


def test_rejects_length_and_exclamation() -> None:
    _rejected("I want to work at Northwind Robotics on Fleet Copilot. It is great. I am "
              "ready!", "too_short")
    _rejected(GOOD.replace("straight away.", "straight away!"), "exclamation")


def test_retry_can_succeed() -> None:
    gw = SeqGateway(ok(GOOD.replace("30 percent", "45 percent")), ok())
    d = draft(gw)
    assert d.decision == FieldDecision.FILL_AND_REVIEW and len(gw.calls) == 2


def test_no_brief_items_asks_user_without_model() -> None:
    for items in (None, []):
        gw = SeqGateway(ok())
        d = draft(gw, items=items)
        assert d.decision == FieldDecision.ASK_USER and d.answer is None
        assert d.note == ("No recent company information found; add research URLs for "
                          "Northwind Robotics in Companies")
        assert gw.calls == []


def test_no_gateway_asks_user() -> None:
    assert draft(None).decision == FieldDecision.ASK_USER


# ---- wiring -----------------------------------------------------------------------------


def _ectx(items: list[BriefItem] | None) -> EssayContext:
    return EssayContext(bank=bank(verified=set(), canned={"why_company": "Generic text."}),
                        facts=FACTS, skills=["Python"], job=JOB,
                        library={"essay_why_company": {"text": "Library text."}},
                        brief_items=items, brief_hash="abc123", current=CURRENT)


def test_why_company_replaces_cover_letter_text_and_ignores_canned_library() -> None:
    f = FormField(field_id="q_why", label="Why Northwind Robotics?", field_type="textarea",
                  required=True)
    cover = FieldAnswer(field_id="q_why", label=f.label, field_type="textarea",
                        proposed_value="I am applying for the role.",
                        decision=FieldDecision.FILL_AND_REVIEW,
                        source_fact_id="artifact:cover_letter_text", confidence=0.8,
                        rationale="cover letter")
    assert essay_candidates([f], [cover], COMPANY) == [(f, K.WHY_COMPANY)]
    user = cover.model_copy(update={"source_fact_id": "user"})
    assert essay_candidates([f], [user], COMPANY) == []  # the user's own answer stays
    out = asyncio.run(draft_essay_answers([f], [cover], SeqGateway(ok()), _ectx(ITEMS)))
    assert out[0].decision == FieldDecision.FILL_AND_REVIEW
    assert out[0].proposed_value == GOOD and out[0].source_fact_id == WHY_PREFIX + "abc123"
    out = asyncio.run(draft_essay_answers([f], [cover], SeqGateway(ok()), _ectx(None)))
    assert out[0].decision == FieldDecision.ASK_USER and out[0].proposed_value is None
    assert "add research URLs" in out[0].rationale


def test_standing_rule_figures_count_as_source() -> None:
    from jobpilot.agents import why_company as wc
    assert "rules" in wc.why_violations.__code__.co_varnames
