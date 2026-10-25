"""Deterministic, model-free prefilter applied to raw postings before storage."""

from jobpilot.discovery.location import location_eligible
from jobpilot.discovery.normalize import html_to_text, normalize_title
from jobpilot.discovery.sources.base import REMOTE_BOARDS, RawPosting
from jobpilot.profile.preferences import SearchPreferences
from jobpilot.util.textmatch import has_phrase

KEYWORDS = ("ai", "ml", "genai", "llm", "machine learning", "rag", "applied scientist")


def _title_ok(title: str, prefs: SearchPreferences) -> bool:
    norm = normalize_title(title)
    tokens = set(norm.split())
    for target in prefs.target_titles:
        want = set(normalize_title(target).split())
        if want and want <= tokens:
            return True
    return any(has_phrase(norm, k) for k in KEYWORDS)


def prefilter(p: RawPosting, prefs: SearchPreferences) -> tuple[bool, str]:
    if prefs.exclude_keywords:
        haystack = f"{p.title}\n{html_to_text(p.description_html)}".casefold()
        for kw in prefs.exclude_keywords:
            if kw.strip() and has_phrase(haystack, kw.strip()):
                return False, f"excluded_keyword:{kw.strip()}"
    if not _title_ok(p.title, prefs):
        return False, "title_mismatch"
    ok, reason = location_eligible(p.location, prefs, remote_board=p.source in REMOTE_BOARDS)
    if not ok:
        return False, reason
    return True, "ok"
