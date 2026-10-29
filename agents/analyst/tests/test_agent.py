import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_agent.agent import AnalystAgent, PlanningError
from analyst_agent.contracts import AnalystDraft, ArchitectArtifact, DraftInstruction
from analyst_agent.retrieval import ContextSnippet


FILES = {
    "front/src/miminet_auth.py": (
        "from flask import render_template\n\n"
        "def user_profile(user_id):\n"
        "    user = load_user(user_id)\n"
        "    return render_template('auth/profile.html', user=user)\n\n"
        "def user_profile_view(user_id):\n"
        "    return user_profile(user_id)\n"
    ),
    "front/src/miminet_model.py": (
        "class Network:\n"
        "    id: int\n"
        "    author_id: int\n"
        "    name: str\n"
    ),
    "front/src/templates/auth/profile.html": (
        "<h1>Профиль {{ user.username }}</h1>\n"
        "{% for network in networks %}\n"
        "  <p>{{ network.name }}</p>\n"
        "{% endfor %}\n"
    ),
    "back/src/network_schema.py": (
        "class NetworkSchema:\n"
        "    id: int\n"
        "    name: str\n"
    ),
}


def make_repo(root: Path) -> Path:
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "back" / "src").mkdir(parents=True, exist_ok=True)
    return root


def artifact() -> ArchitectArtifact:
    return ArchitectArtifact.model_validate(
        {
            "schema_version": "1.0",
            "artifact_id": "arch-001",
            "goal": "Добавить статистику сетей в профиль пользователя",
            "requirements": [
                "Показывать количество сетей пользователя",
                "Показывать дату последней сети",
                "Использовать существующий user_profile",
            ],
            "acceptance_criteria": ["Статистика видна на странице профиля"],
            "constraints": ["Не дублировать запросы к БД"],
            "affected_areas": ["профиль", "Network"],
        }
    )


class OfflineAgentTests(unittest.TestCase):
    def test_builds_valid_coder_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            agent = AnalystAgent(repo_path=repo, offline=True)
            output = agent.analyze(artifact())

            task = output.task
            self.assertEqual(task.schema_version, "1.0")
            self.assertEqual(task.task_id, "arch-001")
            self.assertEqual(task.attempt, 1)
            self.assertGreaterEqual(len(task.instructions), 1)
            self.assertGreaterEqual(len(task.acceptance_criteria), 1)
            self.assertEqual(
                set(task.allowed_write_files),
                {item.file for item in task.instructions},
            )
            self.assertTrue(
                set(task.read_files).issubset(set(task.allowed_write_files))
            )
            for item in task.instructions:
                self.assertTrue((repo / item.file).is_file())

            report = output.report
            self.assertEqual(report.status, "READY")
            self.assertEqual(report.source_artifact_id, "arch-001")
            self.assertEqual(len(report.traceability), 3)
            self.assertTrue(all(link.files for link in report.traceability))
            self.assertTrue(report.context_files)


    def test_parse_draft_repairs_integer_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = AnalystAgent(repo_path=make_repo(Path(tmp)), offline=True)
            payload = (
                '{"instructions": [{"file": "front/src/miminet_auth.py", '
                '"operation": "modify", "target": "profile", '
                '"requirements": [0, 1, 2], "requirement_refs": [0, 1, 2]}], '
                '"acceptance_criteria": ["Готово"]}'
            )
            draft = agent._parse_draft(payload, artifact())
            self.assertEqual(
                draft.instructions[0].requirements,
                list(artifact().requirements),
            )

    def test_duplicate_file_instructions_are_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            agent = AnalystAgent(repo_path=repo, offline=True)
            snippets = [
                ContextSnippet(
                    file_path="front/src/miminet_auth.py",
                    content="def user_profile(): pass",
                    relevance=1.0,
                )
            ]
            draft = AnalystDraft(
                instructions=[
                    DraftInstruction(
                        file="front/src/miminet_auth.py",
                        operation="modify",
                        target="Первая часть",
                        requirements=["Первое"],
                        requirement_refs=[0, 1],
                    ),
                    DraftInstruction(
                        file="./front/src/miminet_auth.py",
                        operation="modify",
                        target="Вторая часть",
                        requirements=["Второе"],
                        requirement_refs=[2],
                    ),
                ],
                acceptance_criteria=["Готово"],
            )
            output = agent._build_output(
                artifact(),
                draft,
                snippets,
                task_id=None,
                workspace_id=None,
                attempt=1,
            )
            self.assertEqual(len(output.task.instructions), 1)
            self.assertEqual(
                len(output.task.instructions[0].requirements), 2
            )
            self.assertIn("Первая часть", output.task.instructions[0].target)
            self.assertIn("Вторая часть", output.task.instructions[0].target)

    def test_rejects_uncovered_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            agent = AnalystAgent(repo_path=repo, offline=True)
            snippets = [
                ContextSnippet(
                    file_path="front/src/miminet_auth.py",
                    content="def user_profile(): pass",
                    relevance=1.0,
                )
            ]
            draft = AnalystDraft(
                instructions=[
                    DraftInstruction(
                        file="front/src/miminet_auth.py",
                        operation="modify",
                        target="user_profile",
                        requirements=["Изменить профиль"],
                        requirement_refs=[0],
                    )
                ],
                acceptance_criteria=["Готово"],
            )
            with self.assertRaises(PlanningError):
                agent._build_output(
                    artifact(),
                    draft,
                    snippets,
                    task_id=None,
                    workspace_id=None,
                    attempt=1,
                )

    def test_rejects_modify_outside_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            agent = AnalystAgent(repo_path=repo, offline=True)
            snippets = [
                ContextSnippet(
                    file_path="front/src/miminet_auth.py",
                    content="x",
                    relevance=1.0,
                )
            ]
            draft = AnalystDraft(
                instructions=[
                    DraftInstruction(
                        file="front/src/miminet_model.py",
                        operation="modify",
                        target="Network",
                        requirements=["Изменить модель"],
                        requirement_refs=[0, 1, 2],
                    )
                ],
                acceptance_criteria=["Готово"],
            )
            with self.assertRaises(PlanningError):
                agent._build_output(
                    artifact(),
                    draft,
                    snippets,
                    task_id=None,
                    workspace_id=None,
                    attempt=1,
                )


class PathValidationTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = AnalystAgent(repo_path=make_repo(Path(tmp)), offline=True)
            for bad in ("../secret.py", "/etc/passwd", "a/../../b.py", ".env", ".git/config"):
                with self.assertRaises(PlanningError):
                    agent._normalize_path(bad)

    def test_create_existing_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            agent = AnalystAgent(repo_path=repo, offline=True)
            with self.assertRaises(PlanningError):
                agent._validate_create("front/src/miminet_auth.py")


if __name__ == "__main__":
    unittest.main()
