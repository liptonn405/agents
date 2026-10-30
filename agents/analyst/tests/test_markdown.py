import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_agent.__main__ import main
from analyst_agent.contracts import CoderTask
from analyst_agent.markdown import (
    parse_architect_markdown,
    render_analyst_markdown,
)
from test_agent import make_repo

MARKDOWN = """# Артефакт Архитектора
artifact_id: arch-md-001

## Цель
Добавить в профиль статистику по сетям.

## Требования
- Показать количество сетей пользователя
- Показать дату последней созданной сети
- Использовать существующий обработчик user_profile

## Критерии готовности
- Статистика видна на странице профиля

## Ограничения
- Не дублировать запросы к БД
"""


class MarkdownParserTests(unittest.TestCase):
    def test_parses_sections(self) -> None:
        artifact = parse_architect_markdown(MARKDOWN)
        self.assertEqual(artifact.artifact_id, "arch-md-001")
        self.assertIn("статистику", artifact.goal)
        self.assertEqual(len(artifact.requirements), 3)
        self.assertEqual(artifact.constraints, ["Не дублировать запросы к БД"])
        self.assertEqual(
            artifact.acceptance_criteria, ["Статистика видна на странице профиля"]
        )

    def test_parses_free_text_without_sections(self) -> None:
        artifact = parse_architect_markdown(
            "Нужно добавить статистику сетей в профиль пользователя. "
            "Показывать количество сетей и дату последней сети."
        )
        self.assertTrue(artifact.requirements)
        self.assertTrue(artifact.artifact_id.startswith("arch-"))

    def test_render_contains_embedded_task(self) -> None:
        from test_agent import artifact

        with tempfile.TemporaryDirectory() as tmp:
            from analyst_agent.agent import AnalystAgent

            agent = AnalystAgent(repo_path=make_repo(Path(tmp)), offline=True)
            output = agent.analyze(artifact())
            rendered = render_analyst_markdown(output)
            self.assertIn("# Артефакт AnalystAgent", rendered)
            self.assertIn("```json", rendered)
            match = re.search(r"```json\s*(\{.*?\})\s*```", rendered, re.DOTALL)
            self.assertIsNotNone(match)
            task = CoderTask.model_validate(json.loads(match.group(1)))
            self.assertEqual(task.task_id, output.task.task_id)


class MarkdownCliTests(unittest.TestCase):
    def test_markdown_input_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.md"
            artifact_path.write_text(MARKDOWN, encoding="utf-8")
            output = Path(out_tmp) / "plan.md"
            with redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--artifact", str(artifact_path),
                        "--repo", str(repo),
                        "--offline",
                        "--output", str(output),
                    ]
                )
            self.assertEqual(code, 0)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("# Артефакт AnalystAgent", rendered)
            match = re.search(r"```json\s*(\{.*?\})\s*```", rendered, re.DOTALL)
            self.assertIsNotNone(match)
            task = CoderTask.model_validate(json.loads(match.group(1)))
            self.assertEqual(task.instructions[0].operation, "modify")

    def test_task_json_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.md"
            artifact_path.write_text(MARKDOWN, encoding="utf-8")
            output = Path(out_tmp) / "plan.md"
            task_json = Path(out_tmp) / "task.json"
            with redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--artifact", str(artifact_path),
                        "--repo", str(repo),
                        "--offline",
                        "--output", str(output),
                        "--task-json", str(task_json),
                    ]
                )
            self.assertEqual(code, 0)
            task = CoderTask.model_validate(
                json.loads(task_json.read_text(encoding="utf-8"))
            )
            self.assertEqual(task.schema_version, "1.0")


if __name__ == "__main__":
    unittest.main()
