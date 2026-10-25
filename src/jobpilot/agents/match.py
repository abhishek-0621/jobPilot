"""MATCH: explainable job matching; LLM judges three dimensions, code scores the rest."""

import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from jobpilot.agents.extract import JobRequirements
from jobpilot.contracts.enums import JobStatus
from jobpilot.contracts.models import Confidence, MatchAssessment, Score
from jobpilot.data.tables import CandidateFactRow, JobRow, MatchAssessmentRow, TaskRow
from jobpilot.llm.prompts import build_messages
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    Handler,
    OutputRejected,
    Provided,
    TaskInput,
    resolve_tool,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.util.hashing import canonical_json
from jobpilot.util.textmatch import has_phrase

PROMPT_VERSION = "match_v1"
_ACTOR = "job_matcher"
WEIGHTS = {"skill_fit": 0.30, "experience_fit": 0.25, "project_fit": 0.20,
           "role_seniority_fit": 0.15, "location_fit": 0.10}
_MAX_FACTS = 40
_LOW_CONFIDENCE = 0.5
_REJECT_SENIORITY = frozenset({"intern", "manager"})
_GAP_YEARS = 4
_PRIMARY = ("work_experience", "project")
_SECONDARY = ("skill", "education", "achievement")
_HARD_PREFIX = "hard_filter:"
_FILTER_NAMES = ("location", "excluded_keyword", "seniority", "experience_gap")


class _MatchLLM(BaseModel):
    """What the model is asked for: the three judgement dimensions plus evidence."""

    skill_fit: Score
    project_fit: Score
    role_seniority_fit: Score
    matched_evidence_fact_ids: list[str] = Field(max_length=12)
    missing_requirements: list[str] = Field(max_length=12)
    concerns: list[str] = Field(max_length=6)
    explanation: str = Field(max_length=600)
    confidence: Confidence


def weighted_score(dims: dict[str, float]) -> float:
    return round(sum(w * dims[k] for k, w in WEIGHTS.items()), 1)


def location_fit(job_location: str | None, job_mode: str, prefs: SearchPreferences) -> float:
    modes = {m.casefold() for m in prefs.work_modes}
    mode = (job_mode or "unknown").casefold()
    loc = (job_location or "").casefold()
    matches = bool(loc) and any(has_phrase(loc, p.strip()) for p in prefs.locations if p.strip())
    if mode in modes and (matches or mode == "remote"):
        return 100.0
    if not loc or (matches and mode == "unknown"):
        return 60.0
    return 0.0


def experience_fit(min_years: float | None, candidate_years: float) -> float:
    if min_years is None or candidate_years >= min_years:
        return 100.0
    return max(0.0, 100.0 - 25.0 * (min_years - candidate_years))


def hard_filters(job: dict, req: JobRequirements, prefs: SearchPreferences, *,
                 candidate_years: float) -> list[str]:
    reasons: list[str] = []
    mode = job.get("work_mode") or req.work_mode
    if location_fit(job.get("location") or req.location, mode, prefs) == 0:
        reasons.append("location")
    text = f"{job.get('title') or ''}\n{req.summary}"
    if any(kw.strip() and has_phrase(text, kw.strip()) for kw in prefs.exclude_keywords):
        reasons.append("excluded_keyword")
    if req.seniority in _REJECT_SENIORITY:
        reasons.append("seniority")
    if req.experience_min_years is not None and \
            req.experience_min_years - candidate_years >= _GAP_YEARS:
        reasons.append("experience_gap")
    return reasons


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"\w+", text.casefold()) if len(t) >= 2}


def select_facts(facts: list[dict], req: JobRequirements) -> list[dict]:
    wanted: set[str] = set()
    for skill in req.required_skills + req.preferred_skills:
        wanted |= _tokens(skill)
    primary = [f for f in facts if f["category"] in _PRIMARY]
    secondary = [f for f in facts
                 if f["category"] in _SECONDARY and _tokens(f["value"]) & wanted]
    return (primary + secondary)[:_MAX_FACTS]


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "match_v1.md").read_text("utf-8")


class MatchAgent:
    name = "job_matcher"
    prompt_version = PROMPT_VERSION
    output_model = MatchAssessment
    tools = ("job_loader", "profile_loader", "prefs")

    async def run(self, inp: TaskInput, ctx: AgentContext) -> MatchAssessment:
        if ctx.gateway is None:
            raise OutputRejected("no_model_gateway")
        load_job: Callable[[str], dict | None] = ctx.tools["job_loader"]
        load_profile: Callable[[], tuple[int, list[dict], float]] = ctx.tools["profile_loader"]
        prefs: SearchPreferences = ctx.tools["prefs"]
        job = load_job(inp.entity_id)
        if job is None:
            raise OutputRejected("job_not_found")
        if not job.get("requirements"):
            raise OutputRejected("not_extracted")
        try:
            req = JobRequirements.model_validate(job["requirements"])
        except ValidationError as e:
            raise OutputRejected("not_extracted") from e
        version, facts, years = load_profile()
        if not facts:
            raise OutputRejected("no_verified_facts")
        selected = select_facts(facts, req)
        requirements = req.model_dump(
            mode="json", include={"required_skills", "preferred_skills",
                                  "experience_min_years", "experience_max_years",
                                  "seniority", "work_mode", "location", "role_family"})
        instructions = (
            "Assess how well the candidate matches the job described in the untrusted "
            "content block.\n"
            f"Candidate facts (verified): {canonical_json(selected)}"
        )
        job_block = (
            f"Title: {job.get('title') or 'unknown'}\n"
            f"Requirements: {canonical_json(requirements)}\n"
            f"Summary: {req.summary}"
        )
        messages = build_messages(_system_prompt(), instructions, untrusted=job_block)
        raw, _meta = await ctx.gateway.generate(
            "job_matching", messages, _MatchLLM, PROMPT_VERSION)
        verified_ids = {f["fact_id"] for f in facts}
        if any(fid not in verified_ids for fid in raw.matched_evidence_fact_ids):
            raise OutputRejected("unverified_fact")
        mode = job.get("work_mode") or req.work_mode
        dims = {"skill_fit": raw.skill_fit, "project_fit": raw.project_fit,
                "role_seniority_fit": raw.role_seniority_fit,
                "experience_fit": experience_fit(req.experience_min_years, years),
                "location_fit": location_fit(job.get("location") or req.location, mode, prefs)}
        failed = hard_filters(job, req, prefs, candidate_years=years)
        concerns = [c.strip() for c in raw.concerns
                    if c.strip() and not c.strip().casefold().startswith(_HARD_PREFIX)]
        concerns += [f"{_HARD_PREFIX}{r}" for r in failed]
        if not all(math.isfinite(v) for v in (*dims.values(), raw.confidence)):
            raise OutputRejected("non_finite_score")
        return MatchAssessment(
            job_id=inp.entity_id, profile_version=version, task_id=ctx.task_id,
            created_at=datetime.now(UTC), total_score=weighted_score(dims), **dims,
            hard_filter_passed=not failed,
            matched_evidence_fact_ids=list(dict.fromkeys(raw.matched_evidence_fact_ids)),
            missing_requirements=[m.strip() for m in raw.missing_requirements if m.strip()],
            concerns=concerns, explanation=raw.explanation.strip(),
            confidence=raw.confidence,
        )


def _require_finite(out: "MatchAssessment") -> None:
    """NaN/inf scores never reach the database or the audit log (canonical JSON refuses
    them); the task fails with a clear reason instead."""
    values = [out.total_score, out.confidence, *(getattr(out, k) for k in WEIGHTS)]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise OutputRejected("non_finite_score")


def make_match_handler(prefs: SearchPreferences | Provided) -> Handler:
    """``prefs`` may be a ``Provided`` so edits to the preferences file apply per task."""
    def match_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
        if not isinstance(out, MatchAssessment) or out.job_id != task.entity_id:
            raise OutputRejected("match output does not match task job")
        _require_finite(out)
        job = s.get(JobRow, out.job_id)
        if job is None:
            raise OutputRejected("job_not_found")
        if job.status != JobStatus.NORMALIZED.value:
            raise OutputRejected("job_not_matchable")
        verified = {f.fact_id for f in s.query(CandidateFactRow).filter(
            CandidateFactRow.profile_version == out.profile_version,
            CandidateFactRow.verified_by_user.is_(True))}
        if any(fid not in verified for fid in out.matched_evidence_fact_ids):
            raise OutputRejected("unverified_fact")
        row = s.get(MatchAssessmentRow, (out.job_id, out.profile_version))
        if row is None:
            row = MatchAssessmentRow(job_id=out.job_id, profile_version=out.profile_version)
            s.add(row)
        for k in WEIGHTS:
            setattr(row, k, getattr(out, k))
        row.total_score = out.total_score
        row.hard_filter_passed = out.hard_filter_passed
        row.confidence = out.confidence
        row.evidence_json = canonical_json({
            "matched_evidence_fact_ids": out.matched_evidence_fact_ids,
            "missing_requirements": out.missing_requirements,
            "concerns": out.concerns, "explanation": out.explanation})
        transition(s, job, JobStatus.MATCHED, actor="orchestrator", reason="scored",
                   details={"total_score": out.total_score, "confidence": out.confidence})
        if not out.hard_filter_passed:
            tagged = {c[len(_HARD_PREFIX):] for c in out.concerns if c.startswith(_HARD_PREFIX)}
            names = [n for n in _FILTER_NAMES if n in tagged] or ["unspecified"]
            transition(s, job, JobStatus.REJECTED, actor="orchestrator",
                       reason=f"{_HARD_PREFIX}{','.join(names)}")
        elif out.total_score < resolve_tool(prefs).min_match_score:
            transition(s, job, JobStatus.REJECTED, actor="orchestrator",
                       reason="below_threshold")
        elif out.confidence < _LOW_CONFIDENCE:
            record_event(s, entity_type="job", entity_id=job.job_id, actor=_ACTOR,
                         action="needs_review", outcome="warning",
                         details={"confidence": out.confidence})
        return []
    return match_handler
