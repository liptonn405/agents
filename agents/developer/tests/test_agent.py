import hashlib
import json
import threading

from coder_support import CoderCase, FakeEngine
from coder_agent import CoderAgent
from coder_agent.engine import EngineOutcome
from coder_agent.reports import operation_key


class CoderAgentTests(CoderCase):
    def test_completed_and_source_repository_unchanged(self):
        before = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertEqual(result.changed_files, ["src/app.py"])
        self.assertEqual((self.workspace(result) / "src/app.py").read_text(), "VALUE = 2\n")
        after = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.workspace(result) / ".git").exists())
        self.assertFalse((self.workspace(result) / ".env").exists())
        self.assertFalse((self.workspace(result) / "readme.txt").exists())
        self.assertTrue(all(value.startswith(result.run_id + "/") for value in result.artifacts.values()))
        self.assertEqual(self.agent.status(result.run_id)["state"], "FINISHED")

    def test_completed_attempt_is_idempotent(self):
        first = self.agent.run(self.payload)
        self.assertEqual(self.agent.run(self.payload), first)
        self.assertEqual(self.fake.calls, 1)

    def test_payload_collision_is_not_replayed(self):
        self.agent.run(self.payload)
        self.payload["instructions"][0]["requirements"] = ["Do something else"]
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "NEEDS_CLARIFICATION")
        self.assertEqual(self.fake.calls, 1)

    def test_new_attempt_starts_from_source_not_partial_edits(self):
        def fail(task, workspace, run_dir):
            self.assertEqual((workspace.root / "src/app.py").read_text(), "VALUE = 1\n")
            (workspace.root / "src/app.py").write_text("PARTIAL = True\n")
            return EngineOutcome("FAILED", "Crashed")
        self.fake.action = fail
        one = self.agent.run(self.payload)
        self.assertNotIn("files", one.artifacts)
        self.assertEqual(self.agent.run(self.payload), one)
        self.payload["attempt"] = 2
        two = self.agent.run(self.payload)
        self.assertEqual(two.status, "FAILED")
        self.assertNotEqual(one.run_id, two.run_id)
        self.assertEqual(self.fake.calls, 2)

    def test_concurrent_attempts_are_blocked(self):
        ready, release = threading.Event(), threading.Event()
        def waiting(task, workspace, run_dir):
            ready.set()
            release.wait(5)
            return EngineOutcome("NEEDS_CLARIFICATION", "Need a decision")
        self.fake.action = waiting
        worker = threading.Thread(target=self.agent.run, args=(self.payload,))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(6)))
        self.assertTrue(ready.wait(2))
        self.payload = dict(self.payload, attempt=2)
        second_agent = CoderAgent(self.config, engine=FakeEngine())
        result = second_agent.run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(second_agent.engine.calls, 0)
        active = [p for p in (self.root / "runs/runs").iterdir() if not (p / "report.json").exists()]
        self.assertEqual(self.agent.status(active[0].name)["state"], "RUNNING")
        release.set()
        worker.join(5)

    def test_interrupted_attempt_is_not_resumed(self):
        from coder_agent import Task
        task = Task.model_validate(self.payload)
        run_id = "a" * 32
        self.agent.reports.begin(run_id, operation_key(task))
        digest = hashlib.sha256(json.dumps(task.model_dump(), sort_keys=True).encode()).hexdigest()
        self.agent.reports.claim(task, digest, run_id)
        self.assertEqual(self.agent.status(run_id)["state"], "INTERRUPTED")
        self.assertEqual(self.agent.run(self.payload).status, "BLOCKED")
        self.assertEqual(self.fake.calls, 0)

    def test_additional_file_requested(self):
        self.fake.action = lambda *args: EngineOutcome("NEEDS_CLARIFICATION", "Needs configuration entry", ("config.json",))
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "NEEDS_CLARIFICATION")
        self.assertTrue(result.requires_clarification)
        self.assertIn("config.json", result.error)
        self.assertNotIn("files", result.artifacts)

    def test_exception_does_not_leak_secret(self):
        def crash(*args):
            raise RuntimeError("test-private-value")
        self.fake.action = crash
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("test-private-value", result.model_dump_json())

    def test_no_change_needs_clarification(self):
        self.fake.action = lambda *args: EngineOutcome("COMPLETED", "Done")
        self.assertEqual(self.agent.run(self.payload).status, "NEEDS_CLARIFICATION")

    def test_status_cannot_traverse(self):
        with self.assertRaises(ValueError):
            self.agent.status("../../other")
