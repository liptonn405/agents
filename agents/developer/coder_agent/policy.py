"""Authority comes from trusted host configuration, never the task envelope."""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field

from .contracts import StrictModel


class Rejected(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def safe_path(name: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_.\-/]+", name)
            or any(part in ("", ".", "..") for part in name.split("/"))
            or len(name) > 240):
        raise Rejected("SECURITY_VIOLATION", "Unsafe relative path")
    return name


def forbid_sensitive(name: str) -> None:
    parts = name.lower().split("/")
    denied = {".git", ".ssh", ".aws", ".codex", ".opencode", ".config",
              ".kube", ".gnupg", ".azure", ".docker", ".gcloud", ".netrc",
              ".npmrc", ".pypirc", ".gitconfig", ".bash_history", ".zsh_history",
              "agents.md", "claude.md", "opencode.json", "opencode.jsonc",
              "auth.json", "vk_auth.json", "docker.sock", "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa"}
    if any(p in denied or p.startswith(".env")
           or any(word in p for word in ("secret", "credential", "token"))
           or p.endswith((".key", ".pem", ".p12", ".sqlite", ".sqlite3", ".db"))
           for p in parts):
        raise Rejected("SECURITY_VIOLATION", f"Protected path: {name}")


class Policy(StrictModel):
    # Exact files deliberately: adding a directory/glob is not an authorization.
    read_files: list[str]
    write_files: list[str]
    max_file_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    max_total_bytes: int = Field(default=8_388_608, ge=1, le=67_108_864)

    def authorize(self, reads: list[str], writes: list[str]) -> None:
        for name in [*self.read_files, *self.write_files, *reads, *writes]:
            safe_path(name)
            forbid_sensitive(name)
        if not set(reads) <= set(self.read_files) or not set(writes) <= set(self.write_files):
            raise Rejected("SECURITY_VIOLATION", "Task exceeds trusted file allowlists")


class Config(StrictModel):
    repository: str
    runs_dir: str
    runtime_root: str
    policy: Policy
    # A dedicated HTTP-over-Unix model gateway, never a general-purpose proxy.
    model_socket: str | None = None
    model: str = Field(default="local-model", pattern=r"^[A-Za-z0-9_.:-]+$")
    opencode_binary: str = "/usr/bin/opencode"
    python_binary: str = "/usr/bin/python3"
    bwrap_binary: str = "bwrap"
    timeout_seconds: int = Field(default=600, ge=1, le=7200)
    max_steps: int = Field(default=20, ge=1, le=100)
    max_log_bytes: int = Field(default=4_194_304, ge=1024, le=16_777_216)
    cpu_seconds: int = Field(default=300, ge=1, le=7200)
    process_limit: int = Field(default=256, ge=16, le=1024)

    def host_paths(self) -> tuple[Path, Path, Path]:
        paths = [Path(p) for p in (self.repository, self.runs_dir, self.runtime_root)]
        if not all(p.is_absolute() for p in paths):
            raise Rejected("BLOCKED", "Configured host paths must be absolute")
        repo, runs, runtime = [p.resolve() for p in paths]
        if any(a == b or a in b.parents or b in a.parents
               for a, b in ((repo, runs), (repo, runtime), (runs, runtime))):
            raise Rejected("BLOCKED", "Repository, runs and runtime must be disjoint")
        if not repo.is_dir() or not (repo / ".git").exists():
            raise Rejected("BLOCKED", "Repository must be an existing Git root")
        if runtime == Path("/"):
            raise Rejected("BLOCKED", "A dedicated sanitized runtime root is required")
        for binary in (self.opencode_binary, self.python_binary):
            if not binary.startswith("/"):
                raise Rejected("BLOCKED", "Runtime binaries must be absolute")
            safe_path(binary[1:])
        return repo, runs, runtime
