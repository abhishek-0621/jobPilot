"""Greenhouse application-form adapter (prepare only; never submits)."""

from typing import ClassVar

from jobpilot.portals.playwright_form import PlaywrightFormAdapter


class GreenhouseAdapter(PlaywrightFormAdapter):
    name: ClassVar[str] = "greenhouse"
    form_roots: ClassVar[tuple[str, ...]] = (
        "#application_form", "form#application-form", 'form[action*="applications"]')
