"""Classify form questions the system must never answer on the user's behalf."""

import re

from jobpilot.util.textmatch import has_phrase

_DECLARATION = (
    "certify",
    "i agree",
    "agree to",
    "acknowledge",
    "i confirm",
    "consent",
    "attest",
    "i declare",
    "terms of service",
    "terms and conditions",
    "terms of use",
    "privacy policy",
    "privacy notice",
    "background check",
    "true and accurate",
    "true and complete",
    "accurate and complete",
    "false information",
    "false statements",
)
_AUTHORIZATION = (
    "authorized to work",
    "authorised to work",
    "eligible to work",
    "able to work",
    "permitted to work",
    "right to work",
    "work permit",
    "work authorization",
    "work authorisation",
    "work eligibility",
    "visa",
    "sponsor",
    "sponsors",
    "sponsorship",
    "sponsored",
    "h 1b",
    "h1b",
    "citizen",
    "citizens",
    "citizenship",
    "national",
    "nationality",
)
_AUTHORIZATION_RE = (
    re.compile(r"\blegally (?:able|eligible|permitted|entitled|authori[sz]ed)\b"),
    re.compile(r"\b(?:authori[sz]ed|eligible|able|permitted|legally)\s+(?:to\s+)?work\s+in\b"),
)
_DEMOGRAPHIC = (
    "gender",
    "sex",
    "pronoun",
    "pronouns",
    "race",
    "racial",
    "ethnicity",
    "ethnic",
    "hispanic",
    "latino",
    "latina",
    "latinx",
    "veteran",
    "military status",
    "disability",
    "disabled",
    "sexual orientation",
    "lgbt",
    "lgbtq",
    "transgender",
    "caste",
    "religion",
    "marital",
    "age",
    "18 or older",
    "over 18",
    "at least 18",
    "years of age",
    "date of birth",
    "dob",
    "birthday",
    "birth date",
    "how do you identify",
    "self identify",
    "identify as",
)
_DEMOGRAPHIC_OPTIONS = (
    "prefer not to say",
    "decline to self identify",
    "i don t wish to answer",
    "male",
    "female",
    "non binary",
    "veteran",
    "disability",
)
_COMPENSATION = (
    "salary",
    "ctc",
    "compensation",
    "pay range",
    "pay expectation",
    "pay expectations",
    "expected pay",
    "expected salary",
    "remuneration",
)

_NON_WORD = re.compile(r"[\W_]+")


def _normalize(text: str) -> str:
    text = _NON_WORD.sub(" ", text.casefold()).strip()
    return re.sub(r"\s*\brequired$", "", text)


def _any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(has_phrase(text, p) for p in phrases)


def classify_sensitive(
    label: str, help_text: str = "", options: list[str] | None = None
) -> str | None:
    """Return ``declaration``, ``authorization``, ``demographic``, ``compensation`` or ``None``."""
    text = f"{_normalize(label)} {_normalize(help_text)}"
    opts = _normalize(" | ".join(options)) if options else ""
    if _any(text, _DECLARATION):
        return "declaration"
    if _any(text, _AUTHORIZATION) or any(r.search(text) for r in _AUTHORIZATION_RE):
        return "authorization"
    if _any(text, _DEMOGRAPHIC) or (opts and _any(opts, _DEMOGRAPHIC_OPTIONS)):
        return "demographic"
    if _any(text, _COMPENSATION):
        return "compensation"
    return None
