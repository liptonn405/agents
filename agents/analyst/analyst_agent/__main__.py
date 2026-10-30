"""CLI for the AnalystAgent."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .agent import AnalystAgent, PlanningError
from .contracts import ArchitectArtifact, decode_json
from .markdown import parse_architect_markdown, render_analyst_markdown

logger = logging.getLogger("analyst_agent")


def _read_bytes(path: Path) -> bytes:
    if str(path) == "-":
        return sys.stdin.buffer.read(65_537)
    return path.read_bytes()


def _read_artifact(path: Path) -> ArchitectArtifact:
    payload = _read_bytes(path)
    stripped = payload.lstrip()
    is_json = stripped.startswith(b"{") or path.suffix.lower() == ".json"
    if is_json:
        return ArchitectArtifact.model_validate(decode_json(payload))
    text = payload.decode("utf-8")
    return parse_architect_markdown(text)


def _infer_format(path: Path | None, fallback: str) -> str:
    if path is None:
        return fallback
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "md"
    if suffix == ".json":
        return "json"
    return fallback


def _default_output_dir() -> Path:
    explicit = os.getenv("ANALYST_OUTPUT_DIR")
    if explicit:
        return Path(explicit).expanduser()

    for parent in Path(__file__).resolve().parents:
        if parent.name == "sem2":
            return parent / "analyst_artifacts"
    if (Path.cwd().parent / "miminet").is_dir():
        return Path.cwd().parent / "analyst_artifacts"
    return Path.cwd() / "analyst_artifacts"


def _ensure_output_dir(path: Path) -> Path:
    target = path.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


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
        "--artifact",
        required=True,
        type=Path,
        help="Markdown или JSON артефакта Архитектора, либо '-' для stdin",
    )
    parser.add_argument("--repo", help="Путь к репозиторию Miminet")
    parser.add_argument(
        "--output",
        type=Path,
        help="Куда записать артефакт: .md — Markdown, .json — строгий CoderTask",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Каталог автосохранения, если не заданы --output/--report/--task-json "
            "(по умолчанию sem2/analyst_artifacts)"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Куда записать отчёт: .md или .json",
    )
    parser.add_argument(
        "--task-json",
        type=Path,
        help="Дополнительно записать строгий CoderTask в JSON",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json"],
        help="Формат вывода в stdout (по умолчанию md)",
    )
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="Вывести task и report одним JSON в stdout",
    )
    parser.add_argument("--task-id", help="task_id для CoderAgent")
    parser.add_argument("--workspace-id", help="workspace_id для CoderAgent")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--model", help="Идентификатор модели (иначе OPENAI_MODEL)")
    parser.add_argument("--base-url", help="Base URL API (иначе OPENAI_BASE_URL)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Без LLM: детерминированный план для тестов",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.bundle and (args.output or args.report or args.task_json):
        parser.error("--bundle нельзя сочетать с --output/--report/--task-json")

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
        markdown_payload = render_analyst_markdown(output)

        auto_save = not (
            args.output or args.report or args.task_json or args.bundle
        )
        auto_paths: dict[str, Path] = {}
        if auto_save:
            out_dir = _ensure_output_dir(args.output_dir or _default_output_dir())
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            artifact_id = output.report.artifact_id
            auto_paths = {
                "plan": out_dir / f"plan-{artifact_id}-{stamp}.md",
                "report": out_dir / f"report-{artifact_id}-{stamp}.md",
                "task": out_dir / f"task-{artifact_id}-{stamp}.json",
            }

        if output.report.status == "NEEDS_CLARIFICATION":
            # Блокирующие вопросы: задание Кодеру не выдаём, возвращаем отчёт.
            if auto_save:
                _write_new(
                    auto_paths["plan"], markdown_payload, agent.index.repo_path
                )
                _write_new(
                    auto_paths["report"], markdown_payload, agent.index.repo_path
                )
                print(f"План: {auto_paths['plan']}", file=sys.stderr)
                print(f"Отчёт: {auto_paths['report']}", file=sys.stderr)
            if args.output:
                payload = (
                    markdown_payload
                    if _infer_format(args.output, "md") == "md"
                    else report_payload
                )
                _write_new(args.output, payload, agent.index.repo_path)
            if args.report:
                payload = (
                    markdown_payload
                    if _infer_format(args.report, "md") == "md"
                    else report_payload
                )
                _write_new(args.report, payload, agent.index.repo_path)
            if not args.output and not args.report and not auto_save:
                print(markdown_payload, end="", file=sys.stderr)
            return 1

        if args.bundle:
            print(bundle_payload, end="")
        elif args.output:
            payload = (
                markdown_payload
                if _infer_format(args.output, "md") == "md"
                else task_payload
            )
            _write_new(args.output, payload, agent.index.repo_path)
        elif auto_save:
            _write_new(auto_paths["plan"], markdown_payload, agent.index.repo_path)
            _write_new(auto_paths["report"], markdown_payload, agent.index.repo_path)
            _write_new(auto_paths["task"], task_payload, agent.index.repo_path)
            print(f"План: {auto_paths['plan']}", file=sys.stderr)
            print(f"Отчёт: {auto_paths['report']}", file=sys.stderr)
            print(f"Задание: {auto_paths['task']}", file=sys.stderr)
        else:
            payload = markdown_payload if (args.format or "md") == "md" else task_payload
            print(payload, end="")

        if args.task_json:
            _write_new(args.task_json, task_payload, agent.index.repo_path)

        if args.report:
            payload = (
                markdown_payload
                if _infer_format(args.report, "md") == "md"
                else report_payload
            )
            _write_new(args.report, payload, agent.index.repo_path)
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
