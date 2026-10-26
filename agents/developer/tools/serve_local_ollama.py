"""Run an operator-installed Ollama privately, without systemd or cloud access.

The original HOME value is preserved, but that path is a new private directory
inside a mount namespace. Only the dedicated .ollama state is persisted there;
the user's real home, credentials, source repo and Docker socket are absent.
Network is shared for registry pulls and the loopback API; generated code never
runs in this model process. OpenCode keeps its separate closed-network sandbox.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
from pathlib import Path


def command(root: Path, context: int, port: int) -> list[str]:
    if root.is_symlink():
        raise ValueError("Installation root must not be a symbolic link")
    root = root.resolve()
    installation = root / "ollama"
    executable = installation / "bin/ollama"
    if installation.is_symlink() or not installation.is_dir():
        raise ValueError("A private real Ollama installation directory is required")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Extract the trusted Ollama bin/lib archive to <root>/ollama first")
    if installation not in executable.resolve().parents:
        raise ValueError("Ollama executable escapes its installation")
    if os.getuid() == 0:
        raise ValueError("Use an unprivileged host account")
    binary = shutil.which("bwrap")
    if not binary:
        raise ValueError("Bubblewrap is required")
    if not 1024 <= context <= 32768 or not 1024 <= port <= 65535:
        raise ValueError("Invalid context or loopback port")
    state, models = root / "state", root / "models"
    for path in (state, models):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("State and model directories must be private real directories")
    root.chmod(0o700)
    for path in (state, models):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    original_home = Path.home()
    if not original_home.is_absolute() or original_home == Path("/"):
        raise ValueError("An ordinary user home is required")
    args = [binary, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--disable-userns", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--uid", "65534", "--gid", "65534", "--clearenv"]
    for path in ("/usr", "/lib", "/lib64"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    args += ["--ro-bind", str(root / "ollama"), "/opt/ollama",
             "--bind", str(models), "/models", "--dir", str(original_home),
             "--bind", str(state), str(original_home / ".ollama"),
             "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev"]
    for path in ("/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/sys"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    # If NVIDIA becomes visible later, expose only its device nodes. No device
    # nodes are created and no drivers/services are installed by this launcher.
    for path in sorted(Path("/dev").glob("nvidia*")):
        if stat.S_ISCHR(path.stat().st_mode):
            args += ["--dev-bind", str(path), str(path)]
    if Path("/dev/nvidia-caps").is_dir():
        args += ["--dev-bind", "/dev/nvidia-caps", "/dev/nvidia-caps"]
    env = {
        "HOME": str(original_home), "PATH": "/opt/ollama/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "OLLAMA_HOST": f"127.0.0.1:{port}", "OLLAMA_MODELS": "/models", "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_CONTEXT_LENGTH": str(context), "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_KEEP_ALIVE": "5m",
    }
    for key, value in env.items():
        args += ["--setenv", key, value]
    return [*args, "--chdir", "/tmp", "/opt/ollama/bin/ollama", "serve"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Private installation/state/models directory")
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()
    argv = command(args.root, args.context, args.port)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
