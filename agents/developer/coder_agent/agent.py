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
        # Only trusted Python integrations may replace the isolated engine.
        self.engine = engine if engine is not None else OpenCodeEngine(config)

    def run(self, value: str | bytes | dict | Task) -> Result:
        run_id = uuid.uuid4().hex
        task = None
        directory = None
        begin_attempted = False

        def result(status: str, error: str | None = None, changed: list[str] | None = None,
                   artifacts: dict[str, str] | None = None) -> Result:
            return Result(
                task_id=task.task_id if task else None, workspace_id=task.workspace_id if task else None,
                attempt=task.attempt if task else None, run_id=run_id, status=status,
                error=error, changed_files=changed or [], requires_clarification=status == "NEEDS_CLARIFICATION",
                artifacts=artifacts or {},
            )

        def storage_failure(exc: Exception) -> Result:
            # Storage failures cannot reliably be stored in the same location.
            # Return the full identity and no usable artifacts to the caller;
            # any existing receipt/evidence is preserved, never removed/retried.
            return result("BLOCKED", f"Execution/report storage unavailable ({type(exc).__name__}); inspect run {run_id}")

        def begin(key: str | None = None) -> None:
            nonlocal directory, begin_attempted
            begin_attempted = True
            directory = self.reports.begin(run_id, key)

        def finish(status: str, error: str | None = None, changed: list[str] | None = None,
                   workspace: Workspace | None = None) -> Result:
            try:
                if directory is None:
                    if begin_attempted:
                        return storage_failure(OSError("Run creation failed"))
                    begin()
                artifacts = {"report": f"{run_id}/report"}
                for name, file in (("log", "engine.log"), ("capabilities", "capabilities.json"),
                                   ("manifest", "manifest.json")):
                    if (directory / file).exists():
                        artifacts[name] = f"{run_id}/{name}"
                if status == "COMPLETED":
                    artifacts.update({"patch": f"{run_id}/patch", "files": f"{run_id}/files"})
                report = result(status, error, changed, artifacts)
                if status == "COMPLETED":
                    if workspace is None:
                        raise Rejected("BLOCKED", "Cannot publish unsealed output")
                    self.reports.seal(report, workspace)
                return self.reports.finish(report)
            except (Rejected, OSError, ValueError, TypeError, KeyError) as exc:
                return storage_failure(exc)

        try:
            if isinstance(value, Task):
                value = value.model_dump()
            if isinstance(value, (str, bytes)):
                value = decode_json(value)
            if len(json.dumps(value, ensure_ascii=True)) > 65_536:
                raise ValueError("Task too large")
            task = Task.model_validate(value)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return finish("INVALID_INPUT", "Invalid task JSON: check version, required fields, strict types and operations")

        digest = hashlib.sha256(json.dumps(task.model_dump(), sort_keys=True).encode()).hexdigest()
        authority = hashlib.sha256(json.dumps({
            "repository": str(self.repository), "policy": self.config.policy.model_dump(),
        }, sort_keys=True).encode()).hexdigest()
        try:
            # Saved results do not restore permissions revoked by the operator.
            self.config.policy.authorize(task.read_files, task.allowed_write_files)
            for instruction in task.instructions:
                self.config.policy.authorize([], [instruction.file])
            with self.reports.lock(operation_key(task), owner=run_id):
                previous = self.reports.previous(task, digest, authority=authority)
                if previous is not None:
                    return previous
                begin(operation_key(task))
                self.reports.claim(task, digest, run_id, authority=authority)
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
                    return finish("COMPLETED", changed=changed, workspace=workspace)
                except Rejected as exc:
                    return finish(exc.status, str(exc))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    return finish("BLOCKED", f"Workspace/report unavailable ({type(exc).__name__}); partial output quarantined")
        except Rejected as exc:
            return finish(exc.status, str(exc))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Covers lock, receipt, initial state and claim failures as well as
            # damaged saved results. Never invoke the engine after such errors.
            return finish("BLOCKED", f"Execution state unavailable ({type(exc).__name__}); do not blindly repeat this attempt")

    def status(self, run_id: str) -> dict:
        """Inspect one execution; never restart or resume an edit."""
        try:
            return self.reports.inspect(run_id)
        except Rejected as exc:
            return {"run_id": run_id, "state": "BLOCKED", "error": str(exc)}
