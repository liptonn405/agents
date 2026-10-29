import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_agent.__main__ import main
from analyst_agent.contracts import (
    AnalystOutput,
    AnalystReport,
    CoderTask,
    Instruction,
    TraceLink,
)
from test_agent import FILES, artifact


def make_repo(root: Path) -> Path:
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "back" / "src").mkdir(parents=True, exist_ok=True)
    return root


class CliTests(unittest.TestCase):
    def test_offline_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.json"
            artifact_path.write_text(artifact().model_dump_json(), encoding="utf-8")
            output = Path(out_tmp) / "task.json"
            report = Path(out_tmp) / "report.json"

            with contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--artifact", str(artifact_path),
                        "--repo", str(repo),
                        "--offline",
                        "--output", str(output),
                        "--report", str(report),
                    ]
                )

            self.assertEqual(code, 0)
            task = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(task["schema_version"], "1.0")
            self.assertEqual(task["task_id"], "arch-001")
            self.assertTrue(task["instructions"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["status"], "READY"
            )

    def test_bundle_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.json"
            artifact_path.write_text(artifact().model_dump_json(), encoding="utf-8")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--artifact", str(artifact_path),
                        "--repo", str(repo),
                        "--offline",
                        "--bundle",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stream.getvalue())
            self.assertIn("task", payload)
            self.assertIn("report", payload)

    def test_refuses_output_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.json"
            artifact_path.write_text(artifact().model_dump_json(), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    [
                        "--artifact", str(artifact_path),
                        "--repo", str(repo),
                        "--offline",
                        "--output", str(repo / "task.json"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertFalse((repo / "task.json").exists())

    def test_blocking_report_prevents_task_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = make_repo(Path(repo_tmp))
            artifact_path = Path(out_tmp) / "architect.json"
            artifact_path.write_text(artifact().model_dump_json(), encoding="utf-8")
            output = Path(out_tmp) / "task.json"

            task = CoderTask(
                task_id="arch-001",
                workspace_id="ws-arch-001",
                attempt=1,
                instructions=[
                    Instruction(
                        file="front/src/miminet_auth.py",
                        operation="modify",
                        target="user_profile",
                        requirements=["Изменить профиль"],
                    )
                ],
                read_files=["front/src/miminet_auth.py"],
                allowed_write_files=["front/src/miminet_auth.py"],
                acceptance_criteria=["Готово"],
            )
            report = AnalystReport(
                artifact_id="analyst-arch-001",
                source_artifact_id="arch-001",
                status="NEEDS_CLARIFICATION",
                goal="goal",
                scope_in=["req"],
                scope_out=[],
                affected_components=[],
                test_plan=[],
                risks=[],
                open_questions=[],
                blocking_questions=["В модели нет поля с датой создания"],
                traceability=[
                    TraceLink(
                        requirement_index=0,
                        requirement="req",
                        files=["front/src/miminet_auth.py"],
                    )
                ],
                context_files=["front/src/miminet_auth.py"],
            )
            fake_output = AnalystOutput(task=task, report=report)

            class FakeIndex:
                repo_path = repo

            class FakeAgent:
                def __init__(self, *args, **kwargs):
                    self.index = FakeIndex()

                def analyze(self, *args, **kwargs):
                    return fake_output

            stderr = io.StringIO()
            with patch("analyst_agent.__main__.AnalystAgent", FakeAgent):
                with contextlib.redirect_stderr(stderr):
                    code = main(
                        [
                            "--artifact", str(artifact_path),
                            "--repo", str(repo),
                            "--offline",
                            "--output", str(output),
                        ]
                    )
            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
            self.assertIn("NEEDS_CLARIFICATION", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
