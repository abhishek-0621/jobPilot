"""Per-bullet diff between the base resume and the tailored one."""

from typing import Literal

from pydantic import BaseModel

from jobpilot.documents.model import Bullet, ResumeDoc


class DiffRow(BaseModel):
    fact_id: str
    section: str
    original: str
    tailored: str
    status: Literal["kept", "rewritten", "rewrite_rejected", "dropped", "added_from_bank"]


def _sections(doc: ResumeDoc) -> list[tuple[str, Bullet]]:
    out: list[tuple[str, Bullet]] = []
    for role in doc.roles:
        out += [("experience", b) for b in role.bullets]
    for proj in doc.projects:
        out += [("projects", b) for b in proj.bullets]
    out += [("achievements", b) for b in doc.achievements]
    return out


def bullet_diff(base: ResumeDoc, tailored: ResumeDoc, rejected: set[str],
                dropped: set[str]) -> list[DiffRow]:
    tail = {b.fact_id: b for _, b in _sections(tailored)}
    base_ids = {b.fact_id for _, b in _sections(base)}
    rows: list[DiffRow] = []
    for section, b in _sections(base):
        t = tail.get(b.fact_id)
        tailored_text = t.text if t else ""
        if b.fact_id in dropped:
            status = "dropped"
        elif b.fact_id in rejected:
            status = "rewrite_rejected"
        elif t is not None and t.text == b.text:
            status = "kept"
        elif t is not None:
            status = "rewritten"
        else:
            status = "dropped"
        rows.append(DiffRow(fact_id=b.fact_id, section=section, original=b.text,
                            tailored=tailored_text, status=status))
    for section, b in _sections(tailored):
        if b.fact_id not in base_ids:
            rows.append(DiffRow(fact_id=b.fact_id, section=section, original="",
                                tailored=b.text, status="added_from_bank"))
    return rows
