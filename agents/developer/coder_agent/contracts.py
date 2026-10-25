"""Versioned, strict wire contracts; no orchestration dependencies."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
PathText = Annotated[str, StringConstraints(min_length=1, max_length=240)]
Status = Literal["COMPLETED", "NEEDS_CLARIFICATION", "INVALID_INPUT", "FAILED", "BLOCKED", "SECURITY_VIOLATION"]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Instruction(StrictModel):
    file: PathText
    operation: Literal["create", "modify"]
    target: Text
    requirements: Annotated[list[Text], Field(min_length=1, max_length=50)]


class Task(StrictModel):
    schema_version: Literal["1.0"]
    task_id: Identifier
    workspace_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=1_000_000)]
    instructions: Annotated[list[Instruction], Field(min_length=1, max_length=100)]
    read_files: Annotated[list[PathText], Field(max_length=100)]
    allowed_write_files: Annotated[list[PathText], Field(min_length=1, max_length=100)]
    acceptance_criteria: Annotated[list[Text], Field(min_length=1, max_length=100)]


class Result(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str | None = None
    workspace_id: str | None = None
    attempt: int | None = None
    run_id: str
    status: Status
    changed_files: list[str] = Field(default_factory=list)
    error: str | None = None
    requires_clarification: bool = False
    artifacts: dict[str, str] = Field(default_factory=dict)


class EngineReply(StrictModel):
    status: Literal["COMPLETED", "NEEDS_CLARIFICATION"]
    message: Text
    required_files: list[PathText] = Field(default_factory=list)


def decode_json(value: str | bytes) -> object:
    """Reject duplicate keys, non-finite numbers and oversized envelopes."""
    if len(value) > 65_536:
        raise ValueError("JSON exceeds 64 KiB")

    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def constant(_: str) -> None:
        raise ValueError("Non-finite JSON number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
