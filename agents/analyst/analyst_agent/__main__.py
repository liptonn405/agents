"""CLI for the AnalystAgent."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .agent import AnalystAgent, PlanningError
from .contracts import ArchitectArtifact, decode_json

logger = logging.getLogger("analyst_agent")


def _read_artifact(path: Path) -> ArchitectArtifact:
    payload = sys.stdin.buffer.read(65_537) if str(path) == "-" else path.read_bytes()
    return ArchitectArtifact.model_validate(decode_json(payload))


def _write_new(path: Path, payload: str, repo: Path) -> None:
    target = path.expanduser().resolve()
    root = repo.resolve()
    if target == root or root in target.parents:
        raise PlanningError("Выходной файл должен быть вне репозитория Miminet.")
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AnalystAgent: Architect artifact -> validated Coder task"
    )
    parser.add_argument(
        "--artifact", required=True, type=Path,
        help="JSON артефакта Архитектора или '-' для stdin",
    )
    parser.add_argument("--repo", help="Путь к репозиторию Miminet")
    parser.add_argument("--output", type=Path, help="Куда записать Coder task JSON")
    parser.add_argument("--report", type=Path, help="Куда записать Analyst report JSON")
    parser.add_argument(
        "--bundle", action="store_true",
        help="Вывести task и report одним JSON в stdout",
    )
    parser.add_argument("--task-id", help="task_id для CoderAgent")
    parser.add_argument("--workspace-id", help="workspace_id для CoderAgent")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--model", help="Идентификатор модели (иначе OPENAI_MODEL)")
    parser.add_argument("--base-url", help="Base URL API (иначе OPENAI_BASE_URL)")
    parser.add_argument(
        "--offline", action="store_true",
        help="Без LLM: детерминированный план для тестов",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.bundle and (args.output or args.report):
        parser.error("--bundle нельзя сочетать с --output/--report")

    try:
        artifact = _read_artifact(args.artifact)
        agent = AnalystAgent(
            repo_path=args.repo,
            top_k=args.top_k,
            offline=args.offline,
            model=args.model,
            base_url=args.base_url,
        )
        output = agent.analyze(
            artifact,
            task_id=args.task_id,
            workspace_id=args.workspace_id,
            attempt=args.attempt,
        )
        task_payload = json.dumps(
            output.task.model_dump(mode="json"), ensure_ascii=False, indent=2
        ) + "\n"
        report_payload = json.dumps(
            output.report.model_dump(mode="json"), ensure_ascii=False, indent=2
        ) + "\n"
        bundle_payload = json.dumps(
            output.model_dump(mode="json"), ensure_ascii=False, indent=2
        ) + "\n"

        if args.bundle:
            print(bundle_payload, end="")
        elif args.output:
            _write_new(args.output, task_payload, agent.index.repo_path)
        else:
            print(task_payload, end="")

        if args.report:
            _write_new(args.report, report_payload, agent.index.repo_path)
    except (PlanningError, RuntimeError, OSError, ValidationError, ValueError) as exc:
        logger.error("%s", exc)
        print(
            json.dumps(
                {"status": "NEEDS_CLARIFICATION", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
