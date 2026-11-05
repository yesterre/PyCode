"""Shared submission contract, independent of HTTP and execution adapters."""
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    question: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=8000)]
    model: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)] | None = None
    snapshot_id: UUID | None = None
    target: str | None = None

    @field_validator('target')
    @classmethod
    def target_value(cls, value):
        from pycode.question import safe_target
        return safe_target(value)
