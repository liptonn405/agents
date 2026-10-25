"""Standalone CLI; trusted configuration is separate from analyst input."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from pydantic import ValidationError

from .agent import CoderAgent
from .contracts import Result, decode_json
from .policy import Config, Rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one validated plan in isolated OpenCode")
    parser.add_argument("--config", type=Path, default=os.environ.get("CODER_AGENT_CONFIG"),
                        help="Trusted operator config, or CODER_AGENT_CONFIG")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--status", metavar="RUN_ID")
    parser.add_argument("--output", type=Path, help="JSON output file (stdout if omitted)")
    args = parser.parse_args(argv)
    try:
        if args.config is None:
            raise Rejected("BLOCKED", "Provide --config or CODER_AGENT_CONFIG")
        config = Config.model_validate(decode_json(Path(args.config).read_bytes()))
        repository, _, runtime = config.host_paths()
        if args.output:
            output = args.output.resolve()
            if any(output == root or root in output.parents for root in (repository, runtime)):
                raise Rejected("SECURITY_VIOLATION", "CLI output must be outside source repository and runtime")
            if output == Path(args.config).resolve() or (args.input and output == args.input.resolve()):
                raise Rejected("SECURITY_VIOLATION", "Output must not overwrite config or input")
        agent = CoderAgent(config)
        if args.status:
            data = agent.status(args.status)
            code = 0
        else:
            with args.input.open("rb") as stream:
                payload = stream.read(65_537)
            result = agent.run(payload)
            data = result.model_dump()
            code = 0 if result.status == "COMPLETED" else 1
    except (Rejected, OSError, ValueError, ValidationError) as exc:
        status = exc.status if isinstance(exc, Rejected) else "BLOCKED"
        message = str(exc) if isinstance(exc, Rejected) else f"Configuration/input/report unavailable ({type(exc).__name__})"
        data = Result(run_id=uuid.uuid4().hex, status=status, error=message).model_dump()
        # Do not honor an output path which has failed the above validation.
        print(json.dumps(data, ensure_ascii=False), file=sys.stdout)
        return 2
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        try:
            # O_NOFOLLOW + exclusive create prevent overwriting existing artifacts.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
        except OSError:
            print(encoded, end="")
            print("Could not create output file; result emitted to stdout", file=sys.stderr)
            return 2
    else:
        print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
