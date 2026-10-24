"""Whole-word / whole-phrase matching that also works for symbols like ``c++`` and ``.net``."""

import re


def has_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive match of ``phrase`` not embedded in a longer word."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None
