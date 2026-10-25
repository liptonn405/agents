"""Linux Bubblewrap boundary. There is deliberately no unsandboxed fallback."""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from .policy import Config, Rejected
from .workspace import Workspace


class BubblewrapSandbox:
    def __init__(self, config: Config):
        self.config = config

    def command(self, workspace: Workspace, control: Path, writes: list[str], argv: list[str]) -> list[str]:
        config = self.config
        _, _, runtime = config.host_paths()
        binary = shutil.which(config.bwrap_binary)
        if not binary:
            raise Rejected("BLOCKED", "Bubblewrap is not installed")
        if os.getuid() == 0:
            raise Rejected("BLOCKED", "Run CoderAgent as an unprivileged host user")
        for executable in (config.python_binary, config.opencode_binary):
            path = runtime / executable.lstrip("/")
            if not path.is_file() or not os.access(path, os.X_OK):
                raise Rejected("BLOCKED", f"Runtime executable missing: {executable}")
            if runtime not in path.resolve().parents:
                raise Rejected("BLOCKED", "Runtime executable escapes runtime root")
        command = [str(Path(binary).resolve()), "--unshare-all", "--unshare-user", "--disable-userns",
                   "--die-with-parent", "--new-session", "--uid", "65534", "--gid", "65534",
                   "--cap-drop", "ALL", "--clearenv", "--ro-bind", str(runtime), "/",
                   "--proc", "/proc", "--dev", "/dev",
                   "--size", "268435456", "--tmpfs", "/tmp",
                   "--size", "268435456", "--tmpfs", "/home",
                   "--dir", "/home/agent", "--ro-bind", str(control), "/control",
                   "--ro-bind", str(workspace.root), "/workspace"]
        # Individual bind mounts enforce exact write authority at the OS level.
        # Atomic replace/unlink is intentionally disallowed for these mountpoints.
        for name in writes:
            command += ["--bind", str(workspace.root / name), f"/workspace/{name}"]
        if config.model_socket:
            socket_path = Path(config.model_socket)
            if (not socket_path.is_absolute() or socket_path.is_symlink()
                    or not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode)
                    or socket_path.name == "docker.sock"):
                raise Rejected("BLOCKED", "A dedicated model gateway Unix socket is required")
            command += ["--ro-bind", str(socket_path), "/tmp/model.sock"]
        env = {
            "HOME": "/home/agent", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "XDG_CONFIG_HOME": "/home/agent/.config", "XDG_DATA_HOME": "/home/agent/.local/share",
            "XDG_CACHE_HOME": "/home/agent/.cache", "TMPDIR": "/tmp",
            "OPENCODE_CONFIG": "/control/opencode.json", "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true", "CI": "1",
        }
        for key, value in env.items():
            command += ["--setenv", key, value]
        return [*command, "--chdir", "/workspace", config.python_binary, "-I", "-B",
                "/control/bootstrap.py", str(config.cpu_seconds), str(config.process_limit),
                str(max(config.policy.max_file_bytes, 33_554_432)), *argv]

    def prepare_control(self, control: Path, opencode_config: dict) -> None:
        control.mkdir(mode=0o700)
        (control / "opencode.json").write_text(json.dumps(opencode_config), encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("bootstrap.py"), control / "bootstrap.py")
