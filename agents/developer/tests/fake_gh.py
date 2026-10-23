"""A subprocess-level gh fixture; never invokes the real GitHub CLI."""

import json
import sys
from pathlib import Path


def write_fake_gh(directory: Path, *, stdout: str, stderr: str = "", code: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "response.json").write_text(
        json.dumps({"stdout": stdout, "stderr": stderr, "code": code}), encoding="utf-8",
    )
    executable = directory / "gh"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "(root / 'invocation.json').write_text(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'debug': os.environ.get('GH_DEBUG'),\n"
        "    'legacy_debug': os.environ.get('DEBUG'),\n"
        "    'prompt_disabled': os.environ.get('GH_PROMPT_DISABLED'),\n"
        "    'token_available': bool(os.environ.get('GH_TOKEN')),\n"
        "    'stdin': sys.stdin.read(),\n"
        "}))\n"
        "response = json.loads((root / 'response.json').read_text())\n"
        "sys.stdout.write(response['stdout'])\n"
        "sys.stderr.write(response['stderr'])\n"
        "sys.exit(response['code'])\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable
