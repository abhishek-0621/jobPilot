"""Private story bank: the user's own stories for open-ended application questions.

The file (storage/profiles/stories.yaml, git-ignored) is validated with ``extra="forbid"``
and imported into the ``story`` / ``story_bank`` tables. Only stories the user verified are
ever used; a story whose content changes becomes unverified again. Events carry ids and
counts only, never story text.
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobpilot.data.db import utcnow
from jobpilot.data.tables import CandidateProfileRow, StoryBankRow, StoryRow
from jobpilot.observability.events import record_event
from jobpilot.util.hashing import canonical_json, content_hash

BANK_ID = "default"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Theme(StrEnum):
    OWNERSHIP = "ownership"
    ACHIEVEMENT = "achievement"
    PROUD_OF = "proud_of"
    CHALLENGE = "challenge"
    FAILURE = "failure"
    PERFORMANCE = "performance"
    CUSTOMER = "customer"
    DISAGREEMENT = "disagreement"
    TEAMWORK = "teamwork"
    LEADERSHIP = "leadership"
    LEARNING = "learning"
    AMBIGUITY = "ambiguity"
    QUALITY_VS_SPEED = "quality_vs_speed"
    BUILT_EXPLAIN = "built_explain"
    STRENGTHS = "strengths"
    WEAKNESSES = "weaknesses"
    WHY_AI = "why_ai"
    OTHER = "other"


class QuestionKey(StrEnum):
    PROUD_OF = "proud_of"
    FAILURE = "failure"
    CHALLENGE = "challenge"
    BUILT_EXPLAIN = "built_explain"
    CUSTOMER = "customer"
    DISAGREEMENT = "disagreement"
    TEAMWORK = "teamwork"
    LEADERSHIP = "leadership"
    LEARNING = "learning"
    AMBIGUITY = "ambiguity"
    QUALITY_VS_SPEED = "quality_vs_speed"
    OWNERSHIP = "ownership"
    STRENGTHS = "strengths"
    WEAKNESSES = "weaknesses"
    SWOT = "swot"
    WHY_AI = "why_ai"
    OVERALL_EXPERTISE = "overall_expertise"
    WHY_COMPANY = "why_company"
    WHY_ROLE = "why_role"


# Themes a story needs to answer each question type (any one of them).
KEY_THEMES: dict[QuestionKey, tuple[Theme, ...]] = {
    QuestionKey.PROUD_OF: (Theme.PROUD_OF, Theme.ACHIEVEMENT),
    QuestionKey.FAILURE: (Theme.FAILURE,),
    QuestionKey.CHALLENGE: (Theme.CHALLENGE, Theme.AMBIGUITY),
    QuestionKey.BUILT_EXPLAIN: (Theme.BUILT_EXPLAIN,),
    QuestionKey.CUSTOMER: (Theme.CUSTOMER,),
    QuestionKey.DISAGREEMENT: (Theme.DISAGREEMENT,),
    QuestionKey.TEAMWORK: (Theme.TEAMWORK,),
    QuestionKey.LEADERSHIP: (Theme.LEADERSHIP, Theme.OWNERSHIP),
    QuestionKey.LEARNING: (Theme.LEARNING,),
    QuestionKey.AMBIGUITY: (Theme.AMBIGUITY,),
    QuestionKey.QUALITY_VS_SPEED: (Theme.QUALITY_VS_SPEED,),
    QuestionKey.OWNERSHIP: (Theme.OWNERSHIP,),
    QuestionKey.STRENGTHS: (Theme.STRENGTHS,),
    QuestionKey.WEAKNESSES: (Theme.WEAKNESSES,),
    QuestionKey.SWOT: (Theme.STRENGTHS, Theme.WEAKNESSES),
    QuestionKey.WHY_AI: (Theme.WHY_AI,),
    QuestionKey.OVERALL_EXPERTISE: (Theme.STRENGTHS, Theme.BUILT_EXPLAIN, Theme.ACHIEVEMENT),
    # why-company answers are written from the company brief; stories only add detail
    QuestionKey.WHY_COMPANY: (Theme.BUILT_EXPLAIN, Theme.ACHIEVEMENT, Theme.PROUD_OF,
                              Theme.STRENGTHS),
    QuestionKey.WHY_ROLE: (Theme.BUILT_EXPLAIN, Theme.STRENGTHS, Theme.ACHIEVEMENT),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Story(_Strict):
    story_id: str
    title: str = Field(min_length=1)
    themes: list[Theme] = Field(min_length=1)
    situation: str = ""
    task: str = ""
    action: str = ""
    result: str = ""
    tradeoffs: str | None = None
    evidence: list[str] = []
    missing: list[str] = []
    verified: bool = False

    @field_validator("story_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("story_id must be a lower-case slug (a-z, 0-9, - and _)")
        return v

    def content_hash(self) -> str:
        """Hash of everything except the verified flag."""
        return content_hash(self.model_dump(mode="json", exclude={"verified"}))

    def source_text(self) -> str:
        """The story's stated content. ``missing`` notes are never part of it."""
        parts = [self.title, self.situation, self.task, self.action, self.result,
                 self.tradeoffs or "", *self.evidence]
        return "\n".join(p for p in parts if p)


class StoryFile(_Strict):
    standing_rules: list[str] = []
    stories: list[Story] = []
    canned: dict[QuestionKey, str] = {}

    @model_validator(mode="after")
    def _unique(self) -> "StoryFile":
        ids = [s.story_id for s in self.stories]
        if len(ids) != len(set(ids)):
            raise ValueError("story_id values must be unique")
        return self


def load_stories_file(path: Path) -> StoryFile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StoryFile.model_validate(raw or {})


@dataclass(frozen=True)
class StoredStory:
    story: Story
    verified: bool


@dataclass(frozen=True)
class StoredBank:
    stories: list[StoredStory]
    standing_rules: list[str]
    canned: dict[QuestionKey, str]

    def verified(self) -> list[Story]:
        return [s.story for s in self.stories if s.verified]


def _profile_version(s: Session) -> int | None:
    return s.execute(select(func.max(CandidateProfileRow.version))).scalar()


def import_stories(s: Session, sf: StoryFile, *, actor: str = "user") -> dict[str, int]:
    """Upsert the bank; return counts. A changed story is unverified unless the file
    itself marks it verified. Stories removed from the file are deleted. Answers drafted
    from a changed or removed story go back to review (see ``mutations``)."""
    from jobpilot.data import mutations  # mutations imports profile modules

    old = {r.story_id: r for r in s.execute(select(StoryRow)).scalars()}
    version = _profile_version(s)
    added = changed = 0
    touched: set[str] = set()
    for pos, story in enumerate(sf.stories):
        h = story.content_hash()
        row = old.get(story.story_id)
        content_changed = row is None or row.content_hash != h
        if row is None:
            row = StoryRow(story_id=story.story_id)
            s.add(row)
            added += 1
            keep = was = False
        else:
            was = row.verified_by_user
            keep = not content_changed and was
            if content_changed:
                changed += 1
                touched.add(story.story_id)
        # the file's flag never re-verifies a story the user unverified in the dashboard,
        # unless the story changed since
        file_ok = story.verified and (content_changed
                                      or not _user_unverified_since(s, row))
        row.position = pos
        row.title = story.title
        row.themes_json = canonical_json([t.value for t in story.themes])
        row.body_json = canonical_json(story.model_dump(mode="json", exclude={"verified"}))
        if row.content_hash != h or row.profile_version is None:
            row.updated_at = utcnow()
        row.content_hash = h
        row.verified_by_user = bool(file_ok or keep)
        row.profile_version = version
        if row.verified_by_user and not was and not keep:
            s.flush()
            record_event(s, entity_type="story", entity_id=story.story_id, actor=actor,
                         action="story_verified", outcome="ok",
                         details={"story_id": story.story_id, "verified": True,
                                  "via": "file"})
    removed = sorted(set(old) - {st.story_id for st in sf.stories})
    for sid in removed:
        s.delete(old[sid])
    touched.update(removed)

    canned = {k.value: v for k, v in sf.canned.items()}
    prev = s.get(StoryBankRow, BANK_ID)
    old_canned = json.loads(prev.canned_json) if prev else {}
    canned_changed = sorted(k for k in set(old_canned) | set(canned)
                            if old_canned.get(k) != canned.get(k) and k in old_canned)
    bank_hash = content_hash({"rules": sf.standing_rules, "canned": canned})
    bank = s.get(StoryBankRow, BANK_ID)
    if bank is None:
        bank = StoryBankRow(bank_id=BANK_ID)
        s.add(bank)
    bank.standing_rules_json = canonical_json(sf.standing_rules)
    bank.canned_json = canonical_json(canned)
    bank.content_hash = bank_hash
    bank.updated_at = utcnow()
    s.flush()
    stale = mutations.invalidate_story_answers(s, sorted(touched), reason="story_changed",
                                               canned_keys=canned_changed)
    counts = {"stories": len(sf.stories), "added": added, "changed": changed,
              "removed": len(removed), "verified": sum(
                  1 for r in s.execute(select(StoryRow)).scalars() if r.verified_by_user),
              "canned": len(canned), "standing_rules": len(sf.standing_rules),
              "answers_invalidated": stale}
    record_event(s, entity_type="story_bank", entity_id=BANK_ID, actor=actor,
                 action="stories_imported", outcome="ok", details=counts)
    return counts


def _user_unverified_since(s: Session, row: StoryRow) -> bool:
    """The user unverified this story in the dashboard after its content last changed."""
    from jobpilot.data.tables import AuditEventRow

    events = s.execute(select(AuditEventRow).where(
        AuditEventRow.entity_type == "story", AuditEventRow.entity_id == row.story_id,
        AuditEventRow.action == "story_verified", AuditEventRow.actor == "user")
        .order_by(AuditEventRow.timestamp_utc.desc())).scalars().first()
    if events is None or (row.updated_at and events.timestamp_utc < row.updated_at):
        return False
    return json.loads(events.redacted_details_json).get("verified") is False


def verify_story(s: Session, story_id: str, verified: bool = True, *,
                 actor: str = "user") -> None:
    from jobpilot.data import mutations

    row = s.get(StoryRow, story_id)
    if row is None:
        raise LookupError(f"unknown story {story_id}")
    was = row.verified_by_user
    row.verified_by_user = verified
    record_event(s, entity_type="story", entity_id=story_id, actor=actor,
                 action="story_verified", outcome="ok",
                 details={"story_id": story_id, "verified": verified})
    s.flush()
    if was and not verified:
        mutations.invalidate_story_answers(s, [story_id], reason="story_unverified")


def _story(row: StoryRow) -> Story:
    return Story.model_validate(json.loads(row.body_json))


def stored_bank(s: Session) -> StoredBank:
    rows = s.execute(select(StoryRow).order_by(StoryRow.position)).scalars()
    bank = s.get(StoryBankRow, BANK_ID)
    rules = json.loads(bank.standing_rules_json) if bank else []
    canned_raw = json.loads(bank.canned_json) if bank else {}
    return StoredBank(
        stories=[StoredStory(_story(r), r.verified_by_user) for r in rows],
        standing_rules=[str(x) for x in rules],
        canned={QuestionKey(k): str(v) for k, v in canned_raw.items()
                if k in QuestionKey._value2member_map_})


def verified_stories(s: Session) -> list[Story]:
    return stored_bank(s).verified()
