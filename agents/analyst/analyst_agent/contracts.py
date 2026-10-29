"""Versioned contracts for the AnalystAgent handoff.

The output task intentionally mirrors CoderAgent's Task 1.0 schema.  The report
is a sidecar artifact: it carries traceability and planning metadata without
making the task unacceptable to the strict CoderAgent validator.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)
]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
]
PathText = Annotated[str, StringConstraints(min_length=1, max_length=240)]
ReportStatus = Literal["READY", "NEEDS_CLARIFICATION"]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class IssueContext(StrictModel):
    """Optional issue text supplied by the Architect."""

    title: Text
    body: str = ""


class ArchitectArtifact(StrictModel):
    """Input produced by the ArchitectAgent."""

    schema_version: Literal["1.0"]
    artifact_id: Identifier
    goal: Text
    requirements: Annotated[list[Text], Field(min_length=1, max_length=100)]
    acceptance_criteria: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    constraints: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    affected_areas: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    non_goals: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    context: Text | None = None
    issue: IssueContext | None = None

    @property
    def retrieval_text(self) -> str:
        """Text used for Code RAG, including optional issue context."""
        parts = [self.goal, *self.requirements, *self.constraints, *self.affected_areas]
        if self.context:
            parts.append(self.context)
        if self.issue:
            parts.extend([self.issue.title, self.issue.body])
        return "\n".join(part for part in parts if part.strip())


class Instruction(StrictModel):
    """A single CoderAgent instruction (Task 1.0 compatible)."""

    file: PathText
    operation: Literal["create", "modify"]
    target: Text
    requirements: Annotated[list[Text], Field(min_length=1, max_length=50)]


class CoderTask(StrictModel):
    """Strict handoff artifact consumed by CoderAgent."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: Identifier
    workspace_id: Identifier
    attempt: Annotated[int, Field(ge=1, le=1_000_000)]
    instructions: Annotated[list[Instruction], Field(min_length=1, max_length=100)]
    read_files: Annotated[list[PathText], Field(max_length=100)]
    allowed_write_files: Annotated[list[PathText], Field(min_length=1, max_length=100)]
    acceptance_criteria: Annotated[list[Text], Field(min_length=1, max_length=100)]


class DraftInstruction(StrictModel):
    """LLM draft instruction before repository validation."""

    file: PathText
    operation: Literal["create", "modify"]
    target: Text
    requirements: Annotated[list[Text], Field(min_length=1, max_length=50)]
    requirement_refs: Annotated[list[int], Field(min_length=1, max_length=100)]


class AnalystDraft(StrictModel):
    """LLM response schema; not a handoff artifact by itself."""

    instructions: Annotated[list[DraftInstruction], Field(min_length=1, max_length=100)]
    acceptance_criteria: Annotated[list[Text], Field(min_length=1, max_length=100)]
    test_plan: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    risks: Annotated[list[Text], Field(max_length=100)] = Field(default_factory=list)
    open_questions: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )
    blocking_questions: Annotated[list[Text], Field(max_length=100)] = Field(
        default_factory=list
    )


class TraceLink(StrictModel):
    requirement_index: Annotated[int, Field(ge=0)]
    requirement: Text
    files: Annotated[list[PathText], Field(max_length=100)]


class AnalystReport(StrictModel):
    """Traceability and planning report kept beside the Coder task."""

    schema_version: Literal["1.0"] = "1.0"
    artifact_id: Identifier
    source_artifact_id: Identifier
    status: ReportStatus
    goal: Text
    scope_in: Annotated[list[Text], Field(max_length=100)]
    scope_out: Annotated[list[Text], Field(max_length=100)]
    affected_components: Annotated[list[Text], Field(max_length=100)]
    test_plan: Annotated[list[Text], Field(max_length=100)]
    risks: Annotated[list[Text], Field(max_length=100)]
    open_questions: Annotated[list[Text], Field(max_length=100)]
    blocking_questions: Annotated[list[Text], Field(max_length=100)]
    traceability: Annotated[list[TraceLink], Field(max_length=100)]
    context_files: Annotated[list[PathText], Field(max_length=100)]


class AnalystOutput(StrictModel):
    """Complete bundle for orchestration; `task` is the Coder input."""

    task: CoderTask
    report: AnalystReport


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
