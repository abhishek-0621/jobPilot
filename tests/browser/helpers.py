from typing import Any

from jobpilot.contracts.enums import FieldDecision
from jobpilot.contracts.models import FieldAnswer


def answer(field_id: str, value: Any, field_type: str = "text", *, label: str = "x",
           decision: FieldDecision = FieldDecision.AUTO_FILL,
           source: str | None = "fact_identity_email") -> FieldAnswer:
    return FieldAnswer(field_id=field_id, label=label, field_type=field_type,
                       proposed_value=value, decision=decision, source_fact_id=source,
                       confidence=0.9, rationale="test")
