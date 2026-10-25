"""Durable receipts, atomic reports and OS-released per-operation locks."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .contracts import Result, Task
from .policy import Rejected


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def operation_key(task: Task) -> str:
    return hashlib.sha256(json.dumps([task.workspace_id, task.task_id]).encode()).hexdigest()


class ReportStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        for name in ("runs", "locks", "receipts"):
            (root / name).mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def lock(self, key: str):
        with (self.root / "locks" / key).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected("BLOCKED", "This task/workspace is already running") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def begin(self, run_id: str, key: str | None = None) -> Path:
        directory = self.root / "runs" / run_id
        directory.mkdir(mode=0o700)
        atomic_json(directory / "state.json", {"run_id": run_id, "state": "RUNNING", "operation_key": key})
        return directory

    def finish(self, result: Result) -> Result:
        directory = self.root / "runs" / result.run_id
        atomic_json(directory / "report.json", result.model_dump())
        atomic_json(directory / "state.json", {"run_id": result.run_id, "state": "FINISHED"})
        return result

    def previous(self, task: Task, digest: str) -> Result | None:
        path = self.root / "receipts" / f"{operation_key(task)}-{task.attempt}.json"
        if not path.exists():
            return None
        receipt = json.loads(path.read_text())
        if receipt["digest"] != digest:
            raise Rejected("NEEDS_CLARIFICATION", "Attempt identity was reused with different instructions")
        report = self.root / "runs" / receipt["run_id"] / "report.json"
        if report.exists():
            return Result.model_validate_json(report.read_bytes())
        raise Rejected("BLOCKED", "Attempt was interrupted; inspect its state and use a new attempt number")

    def claim(self, task: Task, digest: str, run_id: str) -> None:
        atomic_json(self.root / "receipts" / f"{operation_key(task)}-{task.attempt}.json",
                    {"digest": digest, "run_id": run_id})

    def inspect(self, run_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid run identifier")
        directory = self.root / "runs" / run_id
        if (directory / "report.json").exists():
            return {"state": "FINISHED", "result": json.loads((directory / "report.json").read_text())}
        state = json.loads((directory / "state.json").read_text())
        if state.get("operation_key"):
            try:
                with self.lock(state["operation_key"]):
                    state["state"] = "INTERRUPTED"
            except Rejected:
                state["state"] = "RUNNING"
        else:
            state["state"] = "INTERRUPTED"
        return {"run_id": run_id, "state": state["state"]}
