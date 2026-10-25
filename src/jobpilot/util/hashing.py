import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=UTC)
        # "Z" suffix matches pydantic's mode="json" output for BaseModel fields
        return obj.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Cannot canonicalize {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, default=_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
