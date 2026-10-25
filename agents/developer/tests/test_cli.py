import json
import subprocess
import sys
from pathlib import Path

from coder_support import CoderCase


class CliTests(CoderCase):
    def setUp(self):
        super().setUp()
        self.config_file = self.root / "config.json"
        self.config_file.write_text(self.config.model_dump_json())
        self.input_file = self.root / "task.json"
        self.input_file.write_text(json.dumps(self.payload))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "coder_agent", "--config", str(self.config_file),
                               *args], cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, timeout=5)

    def test_invalid_task_persists_report_and_output(self):
        self.input_file.write_text('{"incomplete": true}')
        output = self.root / "result.json"
        result = self.cli("--input", str(self.input_file), "--output", str(output))
        self.assertEqual(result.returncode, 1)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "INVALID_INPUT")
        self.assertTrue((self.root / "runs/runs" / report["run_id"] / "report.json").exists())
        state = self.cli("--status", report["run_id"])
        self.assertEqual(json.loads(state.stdout)["state"], "FINISHED")

    def test_output_inside_source_rejected(self):
        result = self.cli("--input", str(self.input_file), "--output", str(self.repo / "src/app.py"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "SECURITY_VIOLATION")
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")

    def test_existing_output_not_overwritten(self):
        self.input_file.write_text('{}')
        output = self.root / "result.json"
        output.write_text("existing result")
        result = self.cli("--input", str(self.input_file), "--output", str(output))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_text(), "existing result")
        self.assertEqual(json.loads(result.stdout)["status"], "INVALID_INPUT")

    def test_status_path_traversal(self):
        result = self.cli("--status", "../../repo/.env")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("TEST_SENTINEL", result.stdout)
