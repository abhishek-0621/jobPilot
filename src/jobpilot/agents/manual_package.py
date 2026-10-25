"""MANUAL_PACKAGE: for jobs JobPilot never automates (LinkedIn, Naukri, any deny-listed or
unsupported site), draft answers to common application questions from the profile, the
answer library and the cover letter. No model, no browser: the user applies themselves."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from jobpilot.agents.essay import draft_essay_answers, guard_essay_answers
from jobpilot.agents.form_prep import USER_SOURCE, answer_rows
from jobpilot.contracts.enums import ApplicationStatus, FieldDecision
from jobpilot.contracts.models import AgentOutput, FieldAnswer
from jobpilot.data import mutations
from jobpilot.data.repositories import get_application
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.forms.mapping import map_fields
from jobpilot.forms.model import MappingContext
from jobpilot.orchestrator.documents_flow import current_artifact_version, stale_follow_up
from jobpilot.orchestrator.engine import AgentContext, FollowUp, OutputRejected, TaskInput
from jobpilot.orchestrator.state_machine import transition
from jobpilot.portals.base import FormField, FormSnapshot
from jobpilot.portals.registry import apply_mode

PROMPT_VERSION = "manual_package_v1"
_ACTOR = "orchestrator"
_A = ApplicationStatus
FIELD_PREFIX = "manual:"

# (key, label shown to the user, label matched against the alias table, field type).
# Matching on the canonical alias keeps every mapping rule (sensitive, compensation,
# user-only, answer library) in force; the display label is restored afterwards.
QUESTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("why_company", "Why do you want to work at {company}?", "why do you want to work here",
     "textarea"),
    ("why_role", "Why are you interested in the {role} role?", "why this role", "textarea"),
    ("notice_period", "What is your notice period?", "notice period", "text"),
    ("expected_ctc", "What is your expected CTC?", "expected ctc", "text"),
    ("current_ctc", "What is your current CTC?", "current ctc", "text"),
    ("total_experience", "How many years of total experience do you have?",
     "total experience", "text"),
    ("relocation", "Are you willing to relocate?", "willing to relocate", "text"),
    ("work_authorization", "Are you legally authorized to work in this country?",
     "work authorization", "text"),
    ("linkedin", "LinkedIn profile", "linkedin profile", "text"),
    ("github", "GitHub profile", "github profile", "text"),
    ("portfolio", "Portfolio / website", "portfolio", "text"),
    ("email", "Email", "email", "text"),
    ("phone", "Phone", "phone", "text"),
)
# Common open-ended questions, drafted from verified stories (FILL_AND_REVIEW) or left
# to the user with a note naming the missing story (ASK_USER).
ESSAY_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("proud_of", "What are you most proud of?"),
    ("failure", "Tell us about a time you failed."),
    ("challenge", "Describe the most challenging project you worked on."),
    ("strengths", "What are your greatest strengths?"),
    ("weaknesses", "What is your greatest weakness?"),
    ("why_ai", "Why are you interested in AI?"),
)
ESSAY_PREFIX = FIELD_PREFIX + "essay_"
_FILLED = (FieldDecision.AUTO_FILL, FieldDecision.FILL_AND_REVIEW)


class ManualPackage(AgentOutput):
    application_id: str
    profile_version: int
    fields: list[FieldAnswer]


def first_paragraph(letter: str | None) -> str | None:
    """The letter's first body paragraph (after the greeting)."""
    if not letter:
        return None
    parts = [p.strip() for p in letter.split("\n\n") if p.strip()]
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else None)


def package_answers(company: str, role: str, mctx: MappingContext) -> list[FieldAnswer]:
    ctx = mctx.model_copy(update={"artifacts": mctx.artifacts.model_copy(update={
        "cover_letter_text": first_paragraph(mctx.artifacts.cover_letter_text)})})
    fields = [FormField(field_id=FIELD_PREFIX + key, label=match, field_type=ftype,
                        required=True) for key, _, match, ftype in QUESTIONS]
    labels = {FIELD_PREFIX + key: label.format(company=company, role=role)
              for key, label, _, _ in QUESTIONS}
    answers, _unmatched = map_fields(FormSnapshot(url="manual:", fields=fields), ctx)
    out = []
    for a in answers:
        update: dict[str, Any] = {"label": labels[a.field_id]}
        if a.decision in _FILLED:  # the user reviews everything they paste themselves
            update["decision"] = FieldDecision.FILL_AND_REVIEW
        out.append(a.model_copy(update=update))
    return out


def essay_fields() -> list[FormField]:
    return [FormField(field_id=ESSAY_PREFIX + key, label=label, field_type="textarea",
                      required=True) for key, label in ESSAY_QUESTIONS]


async def essay_answers(gateway: Any, ectx: Any) -> list[FieldAnswer]:
    fields = essay_fields()
    pending = [FieldAnswer(field_id=f.field_id, label=f.label, field_type=f.field_type,
                           decision=FieldDecision.ASK_USER, confidence=0.0,
                           rationale="Essay question") for f in fields]
    return await draft_essay_answers(fields, pending, gateway, ectx)


class ManualPackageAgent:
    name = "manual_package"
    prompt_version = PROMPT_VERSION
    output_model = ManualPackage
    tools = ("application_loader", "mapping_context", "active_profile_version",
             "essay_context")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> ManualPackage:
        load_app: Callable[[str], dict | None] = ctx.tools["application_loader"]
        mapping_context: Callable[[str], MappingContext] = ctx.tools["mapping_context"]
        active_version: Callable[[], int | None] = ctx.tools["active_profile_version"]
        app = load_app(inp.entity_id)
        if app is None:
            raise OutputRejected("application_not_found")
        if app["status"] != _A.FORM_IN_PROGRESS.value:
            raise OutputRejected("not_form_in_progress")
        version = active_version()
        if version is None:
            raise OutputRejected("no_profile")
        fields = package_answers(app["company"], app["title"], mapping_context(inp.entity_id))
        load_essay = ctx.tools.get("essay_context")
        if load_essay is not None:
            fields += await essay_answers(ctx.gateway, load_essay(inp.entity_id))
        return ManualPackage(task_id=ctx.task_id, created_at=datetime.now(UTC),
                             application_id=inp.entity_id, profile_version=version,
                             fields=fields)


def manual_package_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
    if not isinstance(out, ManualPackage) or out.application_id != task.entity_id:
        raise OutputRejected("manual package does not match task application")
    app = get_application(s, out.application_id)
    if app.status != _A.FORM_IN_PROGRESS.value:
        raise OutputRejected("not_form_in_progress")
    job = s.get(JobRow, app.job_id)
    if job is None or apply_mode(job) != "manual":
        raise OutputRejected("automated_apply_mode")
    if any(not a.field_id.startswith(FIELD_PREFIX) for a in out.fields):
        raise OutputRejected("field_id_not_manual")
    stale = stale_follow_up(s, task, app.application_id, [
        out.profile_version, current_artifact_version(s, app.resume_artifact_id),
        current_artifact_version(s, app.cover_letter_artifact_id)], _ACTOR)
    if stale is not None:
        return stale
    existing = {r.field_id: r for r in answer_rows(s, app.application_id)}
    for a in guard_essay_answers(s, out.fields):
        row = existing.get(a.field_id)
        if row is None:
            mutations.upsert_answer_initial(s, app.application_id, a, required=True,
                                            options=[], actor=_ACTOR)
        elif row.source_fact_id != USER_SOURCE:  # the user's own answer is never replaced
            mutations.set_answer(s, app.application_id, a.field_id, a.proposed_value,
                                 actor=_ACTOR, source_fact_id=a.source_fact_id,
                                 decision=a.decision, rationale=a.rationale,
                                 confidence=a.confidence)
            row.source_fact_id = a.source_fact_id  # may legitimately be None
    transition(s, app, _A.MANUAL_APPLY_READY, actor=_ACTOR, reason="manual_package_ready")
    return []
