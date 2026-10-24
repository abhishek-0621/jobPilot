"""Resume document model, built only from verified facts plus profile identity."""

from pydantic import BaseModel

from jobpilot.data.tables import CandidateFactRow
from jobpilot.profile.schema import FactEntry, ProfileFile


class Bullet(BaseModel):
    fact_id: str
    text: str
    priority: int  # lower = more important


class RoleBlock(BaseModel):
    employer: str
    title: str
    start: str
    end: str
    location: str | None
    bullets: list[Bullet]


class ResumeDoc(BaseModel):
    name: str
    headline: str
    contact_line: str
    summary: str
    skills: list[tuple[str, list[str]]]
    roles: list[RoleBlock]
    projects: list[RoleBlock]
    education: list[str]
    achievements: list[Bullet]


def _s(v: object) -> str:
    return "" if v is None else str(v)


def base_resume(profile: ProfileFile, facts: list[CandidateFactRow]) -> ResumeDoc:
    verified = {f.fact_id: f for f in facts if f.verified_by_user}
    counter = iter(range(1_000_000))

    def bullets(entries: list[FactEntry]) -> list[Bullet]:
        return [Bullet(fact_id=e.fact_id, text=verified[e.fact_id].value,
                       priority=next(counter))
                for e in entries if e.fact_id in verified]

    cur = profile.current_employment
    roles = [RoleBlock(employer=r.employer, title=r.title, start=_s(r.start), end=_s(r.end),
                       location=cur.location if r.employer == cur.employer else None,
                       bullets=bullets(r.facts))
             for r in profile.experience]
    projects = [RoleBlock(employer="", title=p.name, start=_s(p.start), end=_s(p.end),
                          location=None, bullets=bullets(p.facts))
                for p in profile.projects]
    roles = [r for r in roles if r.bullets]
    projects = [p for p in projects if p.bullets]
    achievements = bullets(profile.achievements)
    education = [verified[e.fact_id].value for e in profile.education if e.fact_id in verified]
    skills = [(f.field.title(), f.value.split(", ")) for f in facts
              if f.category == "skill" and f.fact_id in verified]
    order = list(profile.skills)
    skills.sort(key=lambda g: order.index(g[0].lower()) if g[0].lower() in order else len(order))
    ident = profile.identity
    contact = [ident.phone_e164 or "", ident.email,
               *(v for k, v in profile.links.items() if k != "source")]
    return ResumeDoc(name=ident.full_name, headline=cur.title,
                     contact_line=" | ".join(c for c in contact if c), summary="",
                     skills=skills, roles=roles, projects=projects, education=education,
                     achievements=achievements)
