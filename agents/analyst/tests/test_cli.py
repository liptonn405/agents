import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst_agent.__main__ import main
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


if __name__ == "__main__":
    unittest.main()
