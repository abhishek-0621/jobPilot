"""Ashby application-form adapter.

Ashby names its built-in fields ``_systemfield_name`` / ``_systemfield_email`` /
``_systemfield_resume``; custom questions get generated names. All are addressed by their
``name`` attribute.
"""

from typing import ClassVar

from jobpilot.portals.playwright_form import PlaywrightFormAdapter


class AshbyAdapter(PlaywrightFormAdapter):
    name: ClassVar[str] = "ashby"
    form_roots: ClassVar[tuple[str, ...]] = (
        'form[data-testid="application-form"], form#application-form, form',)
    ASSET_HOSTS: ClassVar[frozenset[str]] = frozenset({
        "jobs.ashbyhq.com", "api.ashbyhq.com", "cdn.ashbyprd.com", "app.ashbyhq.com"})

    @classmethod
    def apply_url(cls, canonical_url: str) -> str:
        base = canonical_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return base if base.endswith("/application") else base + "/application"
