#!/usr/bin/env python3
"""Lightweight, offline team-repository structure check."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "agents/analyst/README.md",
    "agents/tester/README.md",
    "orchestrator/README.md",
    "shared/contracts/README.md",
    "tests/integration/README.md",
    "docs/REPOSITORY_STRUCTURE.md",
]


def main() -> int:
    missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
    if missing:
        for rel in missing:
            print(f"MISSING: {rel}", file=sys.stderr)
        return 1
    print("Repository structure: PASS")
    developer = ROOT / "agents" / "developer"
    if (developer / "main.py").is_file():
        print("DeveloperAgent found; running unit tests...")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=developer,
            check=False,
        )
        return proc.returncode
    print("DeveloperAgent is not on this branch yet; its tests will run after PR merge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
