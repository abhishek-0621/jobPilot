import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?i)password|passwd|token|secret|cookie|authorization|api[_-]?key|session"
)
_SENSITIVE_VALUE = re.compile(
    r"Bearer\s+\S+"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+"
    r"|(?<!sha256:)(?<![A-Fa-f0-9])[A-Fa-f0-9]{32,}"
)


def redact(details: Any) -> Any:
    if isinstance(details, dict):
        return {
            k: REDACTED if _SENSITIVE_KEY.search(str(k)) else redact(v)
            for k, v in details.items()
        }
    if isinstance(details, (list, tuple)):
        return [redact(v) for v in details]
    if isinstance(details, str):
        return _SENSITIVE_VALUE.sub(REDACTED, details)
    return details
