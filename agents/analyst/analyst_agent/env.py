"""Minimal .env loader without an extra runtime dependency."""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs without overriding existing environment values."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def load_default_env() -> Path | None:
    """Load ANALYST_ENV_FILE or agents/analyst/.env if present."""
    explicit = os.getenv("ANALYST_ENV_FILE")
    path = Path(explicit).expanduser() if explicit else Path(__file__).resolve().parents[1] / ".env"
    load_env_file(path)
    return path if path.is_file() else None
