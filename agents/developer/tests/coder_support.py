from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coder_agent import CoderAgent, Config, Policy
from coder_agent.engine import EngineOutcome


def task():
    return {
        "schema_version": "1.0", "task_id": "543", "workspace_id": "workspace-543", "attempt": 1,
        "instructions": [{"file": "src/app.py", "operation": "modify", "target": "VALUE",
                          "requirements": ["Set VALUE to 2; preserve the rest"]}],
        "read_files": ["src/app.py"], "allowed_write_files": ["src/app.py"],
        "acceptance_criteria": ["VALUE is 2"],
    }


class FakeEngine:
    def __init__(self, action=None):
        self.calls = 0
        self.action = action

    def execute(self, task, workspace, run_dir):
        self.calls += 1
        (run_dir / "engine.log").write_text("fake engine\n")
        if self.action:
            return self.action(task, workspace, run_dir)
        (workspace.root / "src/app.py").write_text("VALUE = 2\n")
        return EngineOutcome("COMPLETED", "Edited")


class CoderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/app.py").write_text("VALUE = 1\n")
        (self.repo / "readme.txt").write_text("Read only context\n")
        (self.repo / ".env").write_text("TEST_SENTINEL=not-a-real-secret\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "src/app.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.test", "commit", "-qm", "fixture"], check=True)
        self.config = Config(repository=str(self.repo), runs_dir=str(self.root / "runs"),
                             runtime_root=str(self.root / "runtime"),
                             policy=Policy(read_files=["src/app.py", "readme.txt", "config.json"],
                                           write_files=["src/app.py", "new.py", "src/other.py"]))
        self.payload = copy.deepcopy(task())
        self.fake = FakeEngine()
        self.agent = CoderAgent(self.config, engine=self.fake)

    def workspace(self, result):
        return self.root / "runs/runs" / result.run_id / "workspace"

    def assert_rejected(self, status):
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, status, result.error)
        self.assertEqual(self.fake.calls, 0)
        self.assertFalse(self.workspace(result).exists())
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")
        return result
