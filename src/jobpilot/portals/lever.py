"""Lever application-form adapter (prepare only; never submits).

Lever uses one ``name`` field for the full name, ``urls[...]`` for links and
``cards[...]`` for custom questions; all are addressed by their ``name`` attribute.
"""

from typing import ClassVar

from jobpilot.portals.playwright_form import PlaywrightFormAdapter


class LeverAdapter(PlaywrightFormAdapter):
    name: ClassVar[str] = "lever"
    form_roots: ClassVar[tuple[str, ...]] = ('form[action*="/apply"]', ".application-form")
