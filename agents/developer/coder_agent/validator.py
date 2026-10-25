"""Deterministic validation before any engine process starts."""
from pathlib import Path

from .contracts import Task
from .policy import Policy, Rejected
from .workspace import SourceFile, read_source


def validate_plan(task: Task, repository: Path, policy: Policy) -> dict[str, SourceFile | None]:
    policy.authorize(task.read_files, task.allowed_write_files)
    for name in [i.file for i in task.instructions]:
        policy.authorize([], [name])
    if (len(set(task.read_files)) != len(task.read_files)
            or len(set(task.allowed_write_files)) != len(task.allowed_write_files)):
        raise Rejected("NEEDS_CLARIFICATION", "Duplicate file entries")
    targets = set()
    operations = {}
    for instruction in task.instructions:
        if instruction.file not in task.allowed_write_files:
            raise Rejected("SECURITY_VIOLATION", "Instruction file is outside allowed_write_files")
        key = (instruction.file, instruction.target)
        if key in targets or operations.get(instruction.file, instruction.operation) != instruction.operation:
            raise Rejected("NEEDS_CLARIFICATION", "Overlapping targets or contradictory file operations")
        targets.add(key)
        operations[instruction.file] = instruction.operation
        if instruction.operation == "modify" and instruction.file not in task.read_files:
            raise Rejected("NEEDS_CLARIFICATION", f"Modification requires read_files entry: {instruction.file}")
    if set(operations) != set(task.allowed_write_files):
        raise Rejected("NEEDS_CLARIFICATION", "Every writable file needs an instruction")
    paths = set(task.read_files) | set(task.allowed_write_files)
    if any(parent in paths for name in paths for parent in
           ("/".join(name.split("/")[:i]) for i in range(1, len(name.split("/"))))):
        raise Rejected("NEEDS_CLARIFICATION", "A file is also used as a parent directory")
    baseline, total = {}, 0
    for name in sorted(paths):
        source = read_source(repository, name, policy.max_file_bytes)
        if operations.get(name) == "create":
            if source is not None or name in task.read_files:
                raise Rejected("NEEDS_CLARIFICATION", f"Create conflicts with existing/read file: {name}")
        elif source is None:
            raise Rejected("NEEDS_CLARIFICATION", f"Required file does not exist: {name}")
        if source is not None:
            if source.mode & 0o7000:
                raise Rejected("SECURITY_VIOLATION", f"Special file mode: {name}")
            total += len(source.data)
        baseline[name] = source
    if total > policy.max_total_bytes:
        raise Rejected("BLOCKED", "Input exceeds total size limit")
    return baseline
