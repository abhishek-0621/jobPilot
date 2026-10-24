"""Lever application-form adapter (prepare only; never submits).

Lever uses one ``name`` field for the full name, ``urls[...]`` for links and
``cards[...]`` for custom questions; all are addressed by their ``name`` attribute.
"""

from typing import ClassVar

from jobpilot.portals.playwright_form import PlaywrightFormAdapter


class LeverAdapter(PlaywrightFormAdapter):
    name: ClassVar[str] = "lever"
    form_roots: ClassVar[tuple[str, ...]] = ('form[action*="/apply"]', ".application-form")
    ASSET_HOSTS: ClassVar[frozenset[str]] = frozenset({"jobs.lever.co", "api.lever.co"})

    @classmethod
    def apply_url(cls, canonical_url: str) -> str:
        base = canonical_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return base if base.endswith("/apply") else base + "/apply"
