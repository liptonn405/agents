"""Selective snapshots; never checkout, stage or write the source repository."""
from __future__ import annotations

import difflib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy, Rejected, safe_path


@dataclass(frozen=True)
class SourceFile:
    data: bytes
    mode: int


def read_source(root: Path, name: str, limit: int) -> SourceFile | None:
    """Walk with directory fds + O_NOFOLLOW to close symlink/parent races."""
    safe_path(name)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Rejected("SECURITY_VIOLATION", f"Not a private regular file: {name}")
            if before.st_size > limit:
                raise Rejected("BLOCKED", f"File size limit exceeded: {name}")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise Rejected("BLOCKED", f"File changed during snapshot: {name}")
            try:
                data.decode("utf-8")
            except UnicodeError:
                raise Rejected("NEEDS_CLARIFICATION", f"Only UTF-8 source files supported: {name}") from None
            return SourceFile(data, stat.S_IMODE(before.st_mode))
    except FileNotFoundError:
        return None
    except OSError:
        raise Rejected("SECURITY_VIOLATION", f"Unsafe or inaccessible file: {name}") from None
    finally:
        os.close(directory)


class Workspace:
    def __init__(self, root: Path, baseline: dict[str, SourceFile | None], policy: Policy):
        self.root, self.baseline, self.policy = root, baseline, policy

    def prepare(self) -> None:
        self.root.mkdir(mode=0o700)
        for name, source in self.baseline.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Creates are empty mount targets. The baseline still records absence.
            path.write_bytes(source.data if source else b"")
            path.chmod(source.mode & 0o777 if source else 0o644)

    def audit(self, writes: list[str]) -> tuple[list[str], str]:
        found: set[str] = set()
        expected_dirs = {str(p) for name in self.baseline
                         for p in Path(name).parents if str(p) != "."}
        for current, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs:
                path = Path(current) / name
                rel = path.relative_to(self.root).as_posix()
                if path.is_symlink() or rel not in expected_dirs:
                    raise Rejected("SECURITY_VIOLATION", f"Unexpected directory: {rel}")
            for name in files:
                found.add((Path(current) / name).relative_to(self.root).as_posix())
        if found != set(self.baseline):
            raise Rejected("SECURITY_VIOLATION", "Files created or deleted outside the mounted plan")
        changed, patches, total = [], [], 0
        for name, before in self.baseline.items():
            try:
                after = read_source(self.root, name, self.policy.max_file_bytes)
            except Rejected:
                raise Rejected("SECURITY_VIOLATION", f"Output violates file integrity/size/text policy: {name}") from None
            if after is None:
                raise Rejected("SECURITY_VIOLATION", f"File deleted: {name}")
            total += len(after.data)
            if total > self.policy.max_total_bytes:
                raise Rejected("SECURITY_VIOLATION", "Output exceeds total size limit")
            if before != after:
                if name not in writes or after.mode != (before.mode if before else 0o644):
                    raise Rejected("SECURITY_VIOLATION", f"Unapproved content or mode change: {name}")
                changed.append(name)
                lines = difflib.unified_diff(
                    (before.data.decode() if before else "").splitlines(keepends=True),
                    after.data.decode().splitlines(keepends=True),
                    fromfile=f"a/{name}" if before else "/dev/null", tofile=f"b/{name}",
                )
                for line in lines:
                    patches.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return sorted(changed), "".join(patches)
