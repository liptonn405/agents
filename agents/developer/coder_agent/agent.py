"""One plan in, one report out. No retries, tests, ticket lifecycle or commits."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import ValidationError

from .contracts import Result, Task, decode_json
from .engine import Engine, EngineOutcome, OpenCodeEngine
from .policy import Config, Rejected
from .reports import ReportStore, atomic_json, operation_key
from .validator import validate_plan
from .workspace import Workspace, read_source


class CoderAgent:
    def __init__(self, config: Config, *, engine: Engine | None = None):
        self.config = config
        self.repository, runs, _ = config.host_paths()
        self.reports = ReportStore(runs)
        # Injection is a trusted Python integration seam, not exposed to JSON/CLI.
        # Custom engines must supply the same isolation guarantees themselves.
        self.engine = engine if engine is not None else OpenCodeEngine(config)

    def run(self, value: str | bytes | dict | Task) -> Result:
        run_id = uuid.uuid4().hex
        task = None
        directory = None

        def finish(status: str, error: str | None = None, changed: list[str] | None = None) -> Result:
            nonlocal directory
            if directory is None:
                directory = self.reports.begin(run_id)
            artifacts = {"report": f"{run_id}/report"}
            for name, file in (("log", "engine.log"), ("capabilities", "capabilities.json"),
                               ("manifest", "manifest.json")):
                if (directory / file).exists():
                    artifacts[name] = f"{run_id}/{name}"
            if status == "COMPLETED":
                artifacts.update({"patch": f"{run_id}/patch", "files": f"{run_id}/files"})
            return self.reports.finish(Result(
                task_id=task.task_id if task else None, workspace_id=task.workspace_id if task else None,
                attempt=task.attempt if task else None, run_id=run_id, status=status,
                error=error, changed_files=changed or [], requires_clarification=status == "NEEDS_CLARIFICATION",
                artifacts=artifacts,
            ))

        try:
            if isinstance(value, Task):
                value = value.model_dump()
            if isinstance(value, (str, bytes)):
                value = decode_json(value)
            # Bound Python invocations as well as CLI input.
            if len(json.dumps(value, ensure_ascii=True)) > 65_536:
                raise ValueError("Task too large")
            task = Task.model_validate(value)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return finish("INVALID_INPUT", "Invalid task JSON: check version, required fields, strict types and operations")

        digest = hashlib.sha256(json.dumps(task.model_dump(), sort_keys=True).encode()).hexdigest()
        try:
            with self.reports.lock(operation_key(task)):
                previous = self.reports.previous(task, digest)
                if previous is not None:
                    return previous
                directory = self.reports.begin(run_id, operation_key(task))
                self.reports.claim(task, digest, run_id)
                try:
                    baseline = validate_plan(task, self.repository, self.config.policy)
                    workspace = Workspace(directory / "workspace", baseline, self.config.policy)
                    workspace.prepare()
                    atomic_json(directory / "task.json", task.model_dump())
                    atomic_json(directory / "manifest.json", {name: {
                        "sha256": hashlib.sha256(source.data).hexdigest() if source else None,
                        "mode": source.mode if source else None,
                    } for name, source in baseline.items()})
                    try:
                        outcome = self.engine.execute(task, workspace, directory)
                    except Rejected as exc:
                        outcome = EngineOutcome(exc.status, str(exc))
                    except Exception as exc:
                        # Do not echo exception messages containing host paths or credentials.
                        outcome = EngineOutcome("FAILED", f"Engine failure ({type(exc).__name__}); partial edits quarantined")
                    changed, patch = workspace.audit(task.allowed_write_files)
                    for name, source in baseline.items():
                        if read_source(self.repository, name, self.config.policy.max_file_bytes) != source:
                            raise Rejected("BLOCKED", "Source changed concurrently; discard stale output and replan")
                    if outcome.status != "COMPLETED":
                        error = outcome.message
                        if outcome.required_files:
                            error += "; required files: " + ", ".join(outcome.required_files)
                        return finish(outcome.status, error, changed)
                    if not changed:
                        return finish("NEEDS_CLARIFICATION", "Engine reported completion without any changes")
                    missing = set(task.allowed_write_files) - set(changed)
                    if missing:
                        return finish("NEEDS_CLARIFICATION", "No edits for planned files: " + ", ".join(sorted(missing)), changed)
                    (directory / "changes.patch").write_text(patch, encoding="utf-8")
                    return finish("COMPLETED", changed=changed)
                except Rejected as exc:
                    return finish(exc.status, str(exc))
                except (OSError, ValueError) as exc:
                    return finish("FAILED", f"Workspace/report error ({type(exc).__name__})")
        except Rejected as exc:
            return finish(exc.status, str(exc))

    def status(self, run_id: str) -> dict:
        """Read-only lifecycle inspection; never restart or resume an edit."""
        return self.reports.inspect(run_id)
