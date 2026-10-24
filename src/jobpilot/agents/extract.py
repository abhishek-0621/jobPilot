"""EXTRACT: local-LLM extraction of structured job requirements."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from jobpilot.contracts.enums import JobStatus
from jobpilot.contracts.models import AgentOutput, Confidence
from jobpilot.data.tables import JobRow, TaskRow
from jobpilot.llm.prompts import build_messages
from jobpilot.observability.events import record_event
from jobpilot.orchestrator.engine import (
    AgentContext,
    FollowUp,
    OutputRejected,
    TaskInput,
    TaskKind,
)
from jobpilot.orchestrator.state_machine import transition
from jobpilot.util.hashing import canonical_json

PROMPT_VERSION = "extract_v1"
_ACTOR = "job_extractor"
_MAX_SKILLS = 25
_MAX_SUMMARY = 400
_LOW_CONFIDENCE = 0.4

Seniority = Literal["intern", "junior", "mid", "senior", "staff", "principal", "lead",
                    "manager", "unknown"]
WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]
RoleFamily = Literal["genai_llm", "ml", "data", "backend", "frontend", "fullstack", "other"]


class _ExtractLLM(BaseModel):
    """What the model is asked for: content fields only, no envelope."""

    required_skills: list[str]
    preferred_skills: list[str]
    experience_min_years: float | None
    experience_max_years: float | None
    seniority: Seniority
    work_mode: WorkMode
    location: str | None
    role_family: RoleFamily
    summary: str
    extraction_confidence: Confidence


class JobRequirements(AgentOutput):
    job_id: str
    required_skills: list[str] = Field(max_length=_MAX_SKILLS)
    preferred_skills: list[str] = Field(max_length=_MAX_SKILLS)
    experience_min_years: float | None = Field(ge=0, le=40)
    experience_max_years: float | None = Field(ge=0, le=40)
    seniority: Seniority
    work_mode: WorkMode
    location: str | None
    role_family: RoleFamily
    summary: str = Field(max_length=_MAX_SUMMARY)
    extraction_confidence: Confidence


def _clean_skills(skills: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in skills:
        skill = raw.strip()
        key = skill.casefold()
        if skill and key not in seen:
            seen.add(key)
            out.append(skill)
    return out[:_MAX_SKILLS]


def _clamp_years(v: float | None) -> float | None:
    return None if v is None else min(max(v, 0.0), 40.0)


def _system_prompt() -> str:
    return (resources.files("jobpilot.agents.prompts") / "extract_v1.md").read_text("utf-8")


class ExtractAgent:
    name = "job_extractor"
    prompt_version = PROMPT_VERSION
    output_model = JobRequirements
    tools = ("job_loader",)

    async def run(self, inp: TaskInput, ctx: AgentContext) -> JobRequirements:
        if ctx.gateway is None:
            raise OutputRejected("no_model_gateway")
        loader: Callable[[str], dict | None] = ctx.tools["job_loader"]
        job = loader(inp.entity_id)
        if job is None:
            raise OutputRejected("job_not_found")
        instructions = ("Extract the structured requirements for the job posting in the "
                        "untrusted content block.")
        posting = (
            f"Title: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job.get('location') or 'unknown'}\n\n{job['description_text']}"
        )
        messages = build_messages(_system_prompt(), instructions, untrusted=posting)
        raw, _meta = await ctx.gateway.generate(
            "job_extraction", messages, _ExtractLLM, PROMPT_VERSION)
        lo = _clamp_years(raw.experience_min_years)
        hi = _clamp_years(raw.experience_max_years)
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        location = (raw.location or "").strip() or None
        return JobRequirements(
            job_id=inp.entity_id, task_id=ctx.task_id, created_at=datetime.now(UTC),
            required_skills=_clean_skills(raw.required_skills),
            preferred_skills=_clean_skills(raw.preferred_skills),
            experience_min_years=lo, experience_max_years=hi, seniority=raw.seniority,
            work_mode=raw.work_mode, location=location, role_family=raw.role_family,
            summary=raw.summary.strip()[:_MAX_SUMMARY],
            extraction_confidence=raw.extraction_confidence,
        )


def extract_handler(s: Session, task: TaskRow, out: Any) -> list[FollowUp]:
    if not isinstance(out, JobRequirements) or out.job_id != task.entity_id:
        raise OutputRejected("extract output does not match task job")
    job = s.get(JobRow, out.job_id)
    if job is None:
        raise OutputRejected("job_not_found")
    if job.status != JobStatus.DISCOVERED.value:
        raise OutputRejected("job_not_discoverable")
    try:
        data = json.loads(job.extracted_json or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["requirements"] = out.model_dump(mode="json")
    job.extracted_json = canonical_json(data)
    if job.work_mode is None and out.work_mode != "unknown":
        job.work_mode = out.work_mode
    if job.location is None and out.location:
        job.location = out.location
    transition(s, job, JobStatus.NORMALIZED, actor="orchestrator", reason="extracted")
    if out.extraction_confidence < _LOW_CONFIDENCE:
        record_event(s, entity_type="job", entity_id=job.job_id, actor=_ACTOR,
                     action="low_confidence_extraction", outcome="warning",
                     details={"extraction_confidence": out.extraction_confidence})
    return [(TaskKind.MATCH, "job", job.job_id)]
