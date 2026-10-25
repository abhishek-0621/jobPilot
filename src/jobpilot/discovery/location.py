"""India-or-remote location eligibility (deterministic, used by the prefilter)."""

import re
from functools import lru_cache
from pathlib import Path

import yaml

from jobpilot.profile.preferences import CONFIG_DIR, SearchPreferences
from jobpilot.util.textmatch import has_phrase

_OPEN = ("worldwide", "anywhere", "global", "globally", "anywhere in the world",
         "apac", "asia", "asia-pacific", "asia pacific", "ist", "utc+5:30", "utc +5:30",
         "gmt+5:30", "utc+05:30", "gmt+05:30")
_REMOTE_WORDS = ("remote", "anywhere", "worldwide", "global", "work from home", "wfh")
# Words stripped before deciding whether a remote job names some other region.
_FILLER = re.compile(
    r"\b(?:remote|fully|full|first|only|based|friendly|work|from|home|wfh|in|the|of|"
    r"and|or|any|location|locations|time ?zones?|tz|hybrid|flexible|team|distributed|"
    r"position|role|job|n/a|na|all)\b", re.IGNORECASE)


@lru_cache(maxsize=1)
def india_places(path: Path | None = None) -> tuple[str, ...]:
    p = path or CONFIG_DIR / "india_locations.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(str(x).strip() for x in data.get("india_locations") or [] if str(x).strip())


def is_india(text: str | None) -> bool:
    return bool(text) and any(has_phrase(text or "", place) for place in india_places())


def is_remote(text: str | None) -> bool:
    return bool(text) and any(has_phrase(text or "", w) for w in _REMOTE_WORDS)


def remote_open_to_india(text: str | None) -> bool:
    """A remote job's restriction text allows India: empty, worldwide, India, APAC/Asia, IST."""
    if not text or not text.strip():
        return True
    if is_india(text) or any(has_phrase(text, w) for w in _OPEN):
        return True
    rest = re.sub(r"[^\w\s]", " ", _FILLER.sub(" ", text))
    return not rest.split()


def location_eligible(location: str | None, prefs: SearchPreferences,
                      remote_board: bool) -> tuple[bool, str]:
    """(keep, reason). reason is "ok", "location_mismatch" or "remote_region_mismatch"."""
    wanted = [p.strip() for p in prefs.locations if p.strip()]
    folded = {p.casefold() for p in wanted}
    wants_india = "india" in folded
    wants_remote = "remote" in folded or "remote" in {m.casefold() for m in prefs.work_modes}
    loc = (location or "").strip()
    if not loc:
        if remote_board:
            return (True, "ok") if wants_remote else (False, "location_mismatch")
        return True, "ok"
    if wants_india and is_india(loc):
        return True, "ok"
    if remote_board or is_remote(loc):
        if not remote_open_to_india(loc):
            return False, "remote_region_mismatch"
        if wants_remote:
            return True, "ok"
    others = [p for p in wanted if p.casefold() not in ("india", "remote")]
    if any(has_phrase(loc, p) for p in others):
        return True, "ok"
    return False, "location_mismatch"
