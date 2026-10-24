"""Deterministic, model-free prefilter applied to raw postings before storage."""

from jobpilot.discovery.normalize import html_to_text, normalize_title
from jobpilot.discovery.sources.base import RawPosting
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.util.textmatch import has_phrase

KEYWORDS = ("ai", "ml", "genai", "llm", "machine learning", "rag", "applied scientist")
_ANYWHERE = ("remote", "anywhere")


def _title_ok(title: str, prefs: SearchPreferences) -> bool:
    norm = normalize_title(title)
    tokens = set(norm.split())
    for target in prefs.target_titles:
        want = set(normalize_title(target).split())
        if want and want <= tokens:
            return True
    return any(has_phrase(norm, k) for k in KEYWORDS)


def _location_ok(location: str | None, prefs: SearchPreferences) -> bool:
    if location is None:
        return True
    loc = location.casefold()
    if any(has_phrase(loc, p.strip()) for p in prefs.locations if p.strip()):
        return True
    remote = "remote" in {m.casefold() for m in prefs.work_modes}
    return remote and any(has_phrase(loc, w) for w in _ANYWHERE)


def prefilter(p: RawPosting, prefs: SearchPreferences) -> tuple[bool, str]:
    if prefs.exclude_keywords:
        haystack = f"{p.title}\n{html_to_text(p.description_html)}".casefold()
        for kw in prefs.exclude_keywords:
            if kw.strip() and has_phrase(haystack, kw.strip()):
                return False, f"excluded_keyword:{kw.strip()}"
    if not _title_ok(p.title, prefs):
        return False, "title_mismatch"
    if not _location_ok(p.location, prefs):
        return False, "location_mismatch"
    return True, "ok"
