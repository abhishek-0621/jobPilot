"""Jobs-list filters: validated from the query string (bad values dropped, never an error),
applied with parameterized SQL plus in-Python score/apply-mode/posted checks."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Select, or_, select

from jobpilot.contracts.enums import JobStatus
from jobpilot.data.tables import JobRow

PAGE_SIZE = 50
MAX_TEXT = 200
MAX_VALUES = 50
SORTS = ("score", "posted", "company")
WORK_MODES = ("remote", "hybrid", "onsite")
POSTED_WITHIN = (7, 14, 30)
_STATUSES = frozenset(st.value for st in JobStatus)
MULTI = ("company", "source", "status")
# Remote boards credited on job pages ("via <board>" with a link back, as their terms ask).
BOARD_LABELS = {"himalayas": "Himalayas", "remotive": "Remotive",
                "weworkremotely": "We Work Remotely", "remoteok": "RemoteOK",
                "jobicy": "Jobicy", "workingnomads": "Working Nomads"}


def board_label(source: str) -> str | None:
    """Display name of a remote board, None for company boards and manual adds."""
    return BOARD_LABELS.get(source)


class JobFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = ""
    company: list[str] = []
    source: list[str] = []
    location: str = ""
    work_mode: Literal["remote", "hybrid", "onsite"] | None = None
    status: list[str] = []
    apply_mode: Literal["automated", "manual"] | None = None
    min_score: float | None = None
    posted_within: Literal[7, 14, 30] | None = None
    sort: Literal["score", "posted", "company"] = "posted"
    page: int = 1
    show_duplicates: bool = False

    def active(self) -> bool:
        return any((self.q, self.company, self.source, self.location, self.work_mode,
                    self.status, self.apply_mode, self.min_score is not None,
                    self.posted_within))


def _text(v: str | None) -> str:
    return " ".join((v or "").split())[:MAX_TEXT]


def parse_filters(params: Any) -> JobFilters:
    """From a Starlette QueryParams (or any mapping with getlist). Unknown keys are ignored
    and each invalid value is dropped on its own, so a bad link still renders a page."""
    def one(key: str) -> str | None:
        vals = params.getlist(key)
        return vals[-1] if vals else None

    def field(name: str, value: Any) -> Any:
        try:
            return getattr(JobFilters.model_validate({name: value}), name)
        except ValidationError:
            return None

    data: dict[str, Any] = {"q": _text(one("q")), "location": _text(one("location"))}
    for key in MULTI:
        vals = [_text(v) for v in params.getlist(key)][:MAX_VALUES]
        vals = [v for v in dict.fromkeys(vals) if v]
        if key == "status":
            vals = [v for v in vals if v in _STATUSES]
        data[key] = vals
    for key in ("work_mode", "apply_mode", "sort"):
        raw = one(key)
        if raw:
            data[key] = field(key, raw)
    raw = one("min_score")
    if raw:
        try:
            score = float(raw)
            data["min_score"] = score if 0 <= score <= 100 else None
        except ValueError:
            pass
    raw = one("posted_within")
    if raw and raw.isdigit() and int(raw) in POSTED_WITHIN:
        data["posted_within"] = int(raw)
    raw = one("page")
    if raw and raw.isdigit() and 1 <= int(raw) <= 100_000:
        data["page"] = int(raw)
    data["show_duplicates"] = one("show_duplicates") in ("1", "true", "on")
    return JobFilters.model_validate({k: v for k, v in data.items() if v is not None})


def _like(value: str) -> str:
    """A LIKE pattern matching ``value`` literally (wildcards escaped; bound as a parameter)."""
    esc = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def job_query(f: JobFilters) -> Select[tuple[JobRow]]:
    q = select(JobRow)
    if f.q:
        pat = _like(f.q)
        q = q.where(or_(JobRow.title.ilike(pat, escape="\\"),
                        JobRow.company.ilike(pat, escape="\\"),
                        JobRow.description_text.ilike(pat, escape="\\")))
    if f.company:
        q = q.where(JobRow.company.in_(f.company))
    if f.source:
        q = q.where(JobRow.source.in_(f.source))
    if f.location:
        q = q.where(JobRow.location.ilike(_like(f.location), escape="\\"))
    if f.work_mode:
        q = q.where(JobRow.work_mode == f.work_mode)
    if f.status:
        q = q.where(JobRow.status.in_(f.status))
    elif not f.show_duplicates:
        q = q.where(JobRow.status != JobStatus.DUPLICATE.value)
    return q.order_by(JobRow.first_seen_at.desc(), JobRow.job_id)


def posted_at(job: JobRow) -> datetime:
    """The board's posting date when discovery recorded one, else when JobPilot first saw it."""
    try:
        data = json.loads(job.extracted_json or "{}")
        raw = data.get("posted_at") if isinstance(data, dict) else None
        if isinstance(raw, str) and raw:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    seen = job.first_seen_at
    return seen if seen.tzinfo else seen.replace(tzinfo=UTC)


def posted_cutoff(f: JobFilters, now: datetime) -> datetime | None:
    return now - timedelta(days=f.posted_within) if f.posted_within else None


def query_string(f: JobFilters, **change: Any) -> str:
    """The URL query for ``f`` with ``change`` applied (None/[] removes a key)."""
    data = f.model_dump()
    data.update(change)
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        if value in (None, "", [], False) or (key == "sort" and value == "posted") \
                or (key == "page" and value == 1):
            continue
        if isinstance(value, list):
            pairs.extend((key, str(v)) for v in value)
        elif value is True:
            pairs.append((key, "1"))
        else:
            pairs.append((key, f"{value:g}" if isinstance(value, float) else str(value)))
    return urlencode(pairs)


def chips(f: JobFilters) -> list[dict[str, str]]:
    """One removable chip per active filter value: label and the URL without it."""
    out: list[dict[str, str]] = []

    def add(label: str, **change: Any) -> None:
        out.append({"label": label, "href": "/ui/jobs?" + query_string(f, page=1, **change)})
    if f.q:
        add(f"Search: {f.q}", q="")
    for key in MULTI:
        for v in getattr(f, key):
            add(f"{key.title()}: {v}", **{key: [x for x in getattr(f, key) if x != v]})
    if f.location:
        add(f"Location: {f.location}", location="")
    if f.work_mode:
        add(f"Work mode: {f.work_mode}", work_mode=None)
    if f.apply_mode:
        add(f"Apply: {f.apply_mode}", apply_mode=None)
    if f.min_score is not None:
        add(f"Score ≥ {f.min_score:g}", min_score=None)
    if f.posted_within:
        add(f"Posted within {f.posted_within} days", posted_within=None)
    return out
