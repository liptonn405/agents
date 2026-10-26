"""Durable receipts, sealed artifacts and OS-released per-operation locks."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import Result, Task, decode_json
from .policy import Rejected, safe_path
from .workspace import Workspace, read_source

RUN_ID = re.compile(r"[0-9a-f]{32}")
KEY = re.compile(r"[0-9a-f]{64}")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict, *, exclusive: bool = False) -> None:
    """Publish a completely written file; receipts are never replaced.

    An I/O failure after publication can leave the new file present. Callers
    fail closed and preserve that evidence, never delete/retry the claim.
    """
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, *, limit: int = 65_536) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Invalid metadata file")
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("Oversized metadata")
        if limit <= 65_536:
            value = decode_json(payload)
        else:
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("Duplicate metadata key")
                    value[key] = item
                return value
            value = json.loads(payload, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError("Metadata is not an object")
        return value
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise Rejected("BLOCKED", f"Stored {path.name} is unreadable or corrupt") from None


def operation_key(task: Task) -> str:
    return hashlib.sha256(json.dumps([task.workspace_id, task.task_id]).encode()).hexdigest()


def file_digest(path: Path, limit: int, *, sync: bool = False) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Rejected("BLOCKED", "Artifact is not a private bounded regular file")
        for chunk in iter(lambda: stream.read(65_536), b""):
            size += len(chunk)
            if size > limit:
                raise Rejected("BLOCKED", "Artifact size limit exceeded")
            digest.update(chunk)
        if sync:
            os.fsync(stream.fileno())
    return {"sha256": digest.hexdigest(), "size": size}


class ReportStore:
    def __init__(self, root: Path):
        self.root = root
        created = []
        cursor = root
        while not cursor.exists():
            created.append(cursor)
            cursor = cursor.parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        for name in ("runs", "locks", "receipts"):
            path = root / name
            path.mkdir(exist_ok=True, mode=0o700)
            if path.is_symlink() or not path.is_dir():
                raise Rejected("BLOCKED", "Run store contains an unsafe directory")
            path.chmod(0o700)
        sync_directory(root)
        for path in created:
            sync_directory(path.parent)

    @contextmanager
    def lock(self, key: str, owner: str | None = None):
        if not KEY.fullmatch(key) or (owner is not None and not RUN_ID.fullmatch(owner)):
            raise Rejected("BLOCKED", "Invalid execution lock identifier")
        descriptor = os.open(self.root / "locks" / key,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(descriptor, "r+b") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Rejected("BLOCKED", "Unsafe execution lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected("BLOCKED", "This task/workspace is already running") from None
            try:
                if owner is not None:
                    lock.seek(0)
                    lock.truncate()
                    lock.write(owner.encode("ascii"))
                    lock.flush()
                    os.fsync(lock.fileno())
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def begin(self, run_id: str, key: str | None = None) -> Path:
        if not RUN_ID.fullmatch(run_id) or (key is not None and not KEY.fullmatch(key)):
            raise Rejected("BLOCKED", "Invalid run/state identifier")
        directory = self.root / "runs" / run_id
        directory.mkdir(mode=0o700)
        sync_directory(directory.parent)
        atomic_json(directory / "state.json", {"run_id": run_id, "state": "RUNNING", "operation_key": key})
        return directory

    def state(self, run_id: str) -> dict:
        state = read_json(self.root / "runs" / run_id / "state.json")
        if (set(state) != {"run_id", "state", "operation_key"} or state["run_id"] != run_id
                or state["state"] not in ("RUNNING", "FINISHED")
                or (state["operation_key"] is not None and (
                    not isinstance(state["operation_key"], str) or not KEY.fullmatch(state["operation_key"])))):
            raise Rejected("BLOCKED", "Stored execution state is corrupt")
        return state

    def seal(self, result: Result, workspace: Workspace) -> None:
        """Sync output bytes and directories before publishing COMPLETED."""
        directory = self.root / "runs" / result.run_id
        files = {}
        for name in sorted(workspace.baseline):
            source = read_source(workspace.root, name, workspace.policy.max_file_bytes)
            if source is None:
                raise Rejected("BLOCKED", "Output disappeared before sealing")
            record = file_digest(workspace.root / name, workspace.policy.max_file_bytes, sync=True)
            files[name] = {**record, "mode": source.mode}
        for current, _, _ in os.walk(workspace.root, topdown=False, followlinks=False):
            sync_directory(Path(current))
        patch = file_digest(directory / "changes.patch", workspace.policy.max_total_bytes * 4 + 65_536, sync=True)
        atomic_json(directory / "outputs.json", {
            "schema_version": "1.0", "run_id": result.run_id,
            "changed_files": result.changed_files, "files": files, "patch": patch,
            "max_file_bytes": workspace.policy.max_file_bytes,
            "max_total_bytes": workspace.policy.max_total_bytes,
        })

    def verify_outputs(self, result: Result, task: Task) -> None:
        directory = self.root / "runs" / result.run_id
        try:
            manifest = read_json(directory / "outputs.json", limit=1_048_576)
            if (set(manifest) != {"schema_version", "run_id", "changed_files", "files", "patch",
                                  "max_file_bytes", "max_total_bytes"}
                    or manifest["schema_version"] != "1.0" or manifest["run_id"] != result.run_id
                    or manifest["changed_files"] != result.changed_files
                    or not isinstance(manifest["files"], dict)
                    or set(manifest["files"]) != set(task.read_files) | set(task.allowed_write_files)
                    or type(manifest["max_file_bytes"]) is not int
                    or not 1 <= manifest["max_file_bytes"] <= 16_777_216
                    or type(manifest["max_total_bytes"]) is not int
                    or not 1 <= manifest["max_total_bytes"] <= 67_108_864):
                raise ValueError("Invalid output manifest")
            if result.changed_files != sorted(set(task.allowed_write_files)):
                raise ValueError("Planned/output files differ")
            expected_files = set(manifest["files"])
            expected_dirs = {str(parent) for name in expected_files for parent in Path(name).parents
                             if str(parent) != "."}
            actual = set()
            root = directory / "workspace"
            if root.is_symlink() or not root.is_dir():
                raise ValueError("Missing/unsafe workspace")
            for current, dirs, names in os.walk(root, followlinks=False):
                for name in dirs:
                    path = Path(current) / name
                    if path.is_symlink() or path.relative_to(root).as_posix() not in expected_dirs:
                        raise ValueError("Unexpected output directory")
                actual.update((Path(current) / name).relative_to(root).as_posix() for name in names)
            if actual != expected_files:
                raise ValueError("Output files were removed or added")
            total = 0
            for name, record in manifest["files"].items():
                safe_path(name)
                source = read_source(root, name, manifest["max_file_bytes"])
                if source is None or not isinstance(record, dict) or set(record) != {"sha256", "size", "mode"}:
                    raise ValueError("Invalid output file")
                if (not isinstance(record["sha256"], str) or not KEY.fullmatch(record["sha256"])
                        or type(record["size"]) is not int or type(record["mode"]) is not int):
                    raise ValueError("Invalid output record types")
                if record != {"sha256": hashlib.sha256(source.data).hexdigest(),
                              "size": len(source.data), "mode": source.mode}:
                    raise ValueError("Output content or mode changed")
                total += len(source.data)
            if total > manifest["max_total_bytes"]:
                raise ValueError("Output size limit exceeded")
            patch = file_digest(directory / "changes.patch", manifest["max_total_bytes"] * 4 + 65_536)
            if manifest["patch"] != patch:
                raise ValueError("Patch changed")
        except (OSError, ValueError, TypeError, KeyError, Rejected):
            raise Rejected("BLOCKED", f"Completed artifacts are missing, corrupt or changed; inspect run {result.run_id}") from None

    def finish(self, result: Result) -> Result:
        directory = self.root / "runs" / result.run_id
        state = self.state(result.run_id)
        if result.status == "COMPLETED":
            task = Task.model_validate(read_json(directory / "task.json"))
            self.verify_outputs(result, task)
        # The final report is the publication point. A crash before it remains
        # interrupted and the receipt prevents blind replay.
        atomic_json(directory / "state.json", {**state, "state": "FINISHED"})
        atomic_json(directory / "report.json", result.model_dump())
        return result

    def _report(self, run_id: str, task: Task | None = None) -> Result:
        try:
            self.state(run_id)
            result = Result.model_validate(read_json(self.root / "runs" / run_id / "report.json"))
            if result.run_id != run_id:
                raise ValueError("Run identity mismatch")
            allowed_artifacts = {name: f"{run_id}/{name}" for name in (
                "report", "log", "capabilities", "manifest", "patch", "files")}
            if any(allowed_artifacts.get(name) != value for name, value in result.artifacts.items()):
                raise ValueError("Unsafe artifact identifier")
            if result.status != "COMPLETED" and {"patch", "files"} & set(result.artifacts):
                raise ValueError("Failed result exposed completed artifacts")
            if task is not None and (result.task_id, result.workspace_id, result.attempt) != (
                    task.task_id, task.workspace_id, task.attempt):
                raise ValueError("Task identity mismatch")
            if result.status == "COMPLETED":
                stored_task = Task.model_validate(read_json(self.root / "runs" / run_id / "task.json"))
                if task is not None and stored_task != task:
                    raise ValueError("Stored task changed")
                self.verify_outputs(result, stored_task)
            return result
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise Rejected("BLOCKED", f"Stored report is corrupt; inspect run {run_id}") from None

    def previous(self, task: Task, digest: str, *, authority: str | None = None) -> Result | None:
        path = self.root / "receipts" / f"{operation_key(task)}-{task.attempt}.json"
        try:
            receipt = read_json(path)
        except FileNotFoundError:
            return None
        if (set(receipt) not in ({"digest", "run_id"}, {"digest", "run_id", "authority"})
                or not isinstance(receipt["digest"], str)
                or not KEY.fullmatch(receipt["digest"]) or not isinstance(receipt["run_id"], str)
                or not RUN_ID.fullmatch(receipt["run_id"])
                or ("authority" in receipt and (not isinstance(receipt["authority"], str)
                                               or not KEY.fullmatch(receipt["authority"])))):
            raise Rejected("BLOCKED", "Stored attempt receipt is corrupt; do not reuse this attempt")
        if receipt["digest"] != digest:
            raise Rejected("NEEDS_CLARIFICATION", f"Attempt identity was reused with different instructions; inspect run {receipt['run_id']}")
        if authority is not None and receipt.get("authority") != authority:
            raise Rejected("BLOCKED", f"Attempt belongs to a different or unbound trusted repository/policy; inspect run {receipt['run_id']}")
        try:
            return self._report(receipt["run_id"], task)
        except FileNotFoundError:
            raise Rejected("BLOCKED", f"Attempt was interrupted; inspect run {receipt['run_id']} and use a new attempt number") from None

    def claim(self, task: Task, digest: str, run_id: str, *, authority: str | None = None) -> None:
        if (not KEY.fullmatch(digest) or not RUN_ID.fullmatch(run_id)
                or (authority is not None and not KEY.fullmatch(authority))):
            raise Rejected("BLOCKED", "Invalid attempt claim")
        try:
            receipt = {"digest": digest, "run_id": run_id}
            if authority is not None:
                receipt["authority"] = authority
            atomic_json(self.root / "receipts" / f"{operation_key(task)}-{task.attempt}.json",
                        receipt, exclusive=True)
        except FileExistsError:
            raise Rejected("BLOCKED", "Attempt receipt already exists; refusing to replace it") from None

    def inspect(self, run_id: str) -> dict:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run identifier")
        try:
            try:
                return {"state": "FINISHED", "result": self._report(run_id).model_dump()}
            except FileNotFoundError:
                pass
            state = self.state(run_id)
            if state["operation_key"] is None:
                return {"run_id": run_id, "state": "INTERRUPTED"}
            try:
                with self.lock(state["operation_key"]):
                    # Completion can race the initial report lookup.
                    try:
                        return {"state": "FINISHED", "result": self._report(run_id).model_dump()}
                    except FileNotFoundError:
                        return {"run_id": run_id, "state": "INTERRUPTED"}
            except Rejected as exc:
                if str(exc) != "This task/workspace is already running":
                    raise
                path = self.root / "locks" / state["operation_key"]
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as lock:
                    owner = lock.read(33).decode("ascii")
                return {"run_id": run_id, "state": "RUNNING" if owner == run_id else "INTERRUPTED"}
        except (OSError, ValueError, TypeError, KeyError):
            raise Rejected("BLOCKED", f"Execution state is unavailable or corrupt; inspect run {run_id}") from None
