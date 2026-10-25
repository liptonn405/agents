"""Build an explicit runtime from trusted local standalone OpenCode and Python.

No downloads, secrets, home directories, shell, Git, system config or services.
ldd is only suitable for operator-trusted binaries. The result is deployment
input, outside both the source repo and the run store; review before use.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


def build_runtime(root: Path, opencode: Path) -> None:
    if not opencode.is_file() or not os.access(opencode, os.X_OK):
        raise ValueError("Supply an existing trusted standalone OpenCode executable")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied = set()

    def copy(source: Path, destination: Path | None = None):
        destination = destination or source
        target = root / destination.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.resolve(), target)
        target.chmod(source.stat().st_mode & 0o777)

    def libraries(binary: Path):
        result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, timeout=15,
                                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        if "not found" in result.stdout:
            raise ValueError(f"Missing dynamic library for {binary.name}")
        for name in re.findall(r"(?:=>\s+|^\s*)(/[^\s]+)", result.stdout, re.MULTILINE):
            if name not in copied:
                copied.add(name)
                copy(Path(name))

    python = Path(sys.executable).resolve()
    copy(python, Path("/usr/bin/python3"))
    libraries(python)
    stdlib = Path(sysconfig.get_path("stdlib"))
    destination = root / stdlib.relative_to("/")
    shutil.copytree(stdlib, destination, ignore=shutil.ignore_patterns(
        "site-packages", "dist-packages", "__pycache__", "test", "tests", "ensurepip", "idlelib", "tkinter"))
    for library in stdlib.glob("lib-dynload/*.so"):
        libraries(library)
    copy(opencode, Path("/usr/bin/opencode"))
    libraries(opencode)
    for name in ("tmp", "home", "proc", "dev", "workspace", "control"):
        (root / name).mkdir(exist_ok=True)
    (root / "etc").mkdir(exist_ok=True)
    (root / "etc/passwd").write_text("agent:x:65534:65534:Coder:/home/agent:/nonexistent\n")
    (root / "etc/group").write_text("agent:x:65534:\n")
    (root / "runtime-manifest.json").write_text(json.dumps({
        "python": sys.version, "opencode_source": str(opencode),
        "note": "Built from trusted local files; validate CLI with isolated preflight",
    }, indent=2))


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--opencode", required=True, type=Path)
    args = parser.parse_args(argv)
    build_runtime(args.root.resolve(), args.opencode.resolve())


if __name__ == "__main__":
    main()
