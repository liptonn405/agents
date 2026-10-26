from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coder_support import CoderCase, task
from coder_agent import CoderAgent, Task
from coder_agent.engine import ProcessResult, opencode_config, parse_events, run_process
from coder_agent.policy import Rejected


def event(reply):
    return json.dumps({"type": "text", "part": {"text": json.dumps(reply)}}).encode() + b"\n"


class EventTests(unittest.TestCase):
    def test_structured_completion(self):
        data = event({"status": "COMPLETED", "message": "Edited", "required_files": []})
        self.assertEqual(parse_events(data, 10).status, "COMPLETED")

    def test_earlier_completion_cannot_replace_missing_final_reply(self):
        early = event({"status": "COMPLETED", "message": "Premature"})
        for later in ({"type": "step_start"}, {"type": "tool_use", "part": {
                "tool": "edit", "state": {"input": {"filePath": "/workspace/src/app.py"}}}}):
            with self.subTest(later=later):
                data = early + json.dumps(later).encode() + b'\n{"type":"step_finish"}\n'
                self.assertEqual(parse_events(data, 10, Task.model_validate(task())).status, "FAILED")

    def test_final_reply_after_earlier_completion_is_used(self):
        data = event({"status": "COMPLETED", "message": "Premature"})
        data += b'{"type":"step_start"}\n'
        data += event({"status": "NEEDS_CLARIFICATION", "message": "Need a decision"})
        reply = parse_events(data, 10)
        self.assertEqual(reply.status, "NEEDS_CLARIFICATION")
        self.assertEqual(reply.message, "Need a decision")

    def test_zero_exit_is_not_enough(self):
        for data in (b"", b"not JSON", b"[]", b'{"type":"text","part":{}}',
                     b'{"type":"error","error":"bad"}',
                     event({"status": "COMPLETED", "message": "", "unknown": True})):
            with self.subTest(data=data):
                self.assertEqual(parse_events(data, 10).status, "FAILED")

    def test_extra_file_forces_clarification(self):
        data = event({"status": "COMPLETED", "message": "Need config", "required_files": ["config.json"]})
        self.assertEqual(parse_events(data, 10).status, "NEEDS_CLARIFICATION")

    def test_excess_steps_fail(self):
        data = b'{"type":"step_finish"}\n' * 3 + event({"status": "COMPLETED", "message": "Done"})
        self.assertEqual(parse_events(data, 1).status, "FAILED")

    def test_unapproved_tool_attempt_rejected(self):
        plan = Task.model_validate(task())
        for tool, inputs in (("bash", {"command": "git push"}),
                             ("read", {"filePath": "/home/agent/.env"}),
                             ("edit", {"filePath": "/workspace/config.json"}),
                             ("apply_patch", {"patchText": "*** Delete File: src/app.py\n"})):
            data = json.dumps({"type": "tool_use", "part": {"tool": tool, "state": {"input": inputs}}}).encode()
            with self.subTest(tool=tool):
                self.assertEqual(parse_events(data, 10, plan).status, "SECURITY_VIOLATION")

    def test_declared_tool_attempt_accepted(self):
        data = json.dumps({"type": "tool_use", "part": {"tool": "edit", "state": {
            "input": {"filePath": "/workspace/src/app.py"}}}}).encode() + b"\n"
        data += event({"status": "COMPLETED", "message": "Done"})
        self.assertEqual(parse_events(data, 10, Task.model_validate(task())).status, "COMPLETED")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_python(self, code, timeout=2, max_bytes=1024):
        return run_process([sys.executable, "-c", code], self.root / "engine.log",
                           timeout=timeout, max_bytes=max_bytes)

    def test_crash(self):
        result = self.run_python("raise SystemExit(7)")
        self.assertEqual(result.returncode, 7)

    def test_timeout(self):
        start = time.monotonic()
        result = self.run_python("import time; time.sleep(5)", timeout=0.15)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - start, 2)

    def test_child_process_group_is_killed(self):
        result = self.run_python(
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
            "print(p.pid,flush=True); time.sleep(20)", timeout=0.2)
        self.assertTrue(result.timed_out)
        child = int(result.output.strip())
        # A killed child may briefly be a zombie until its parent/reaper collects it.
        path = Path(f"/proc/{child}/stat")
        for _ in range(30):
            try:
                dead = path.read_text().split()[2] == "Z"
            except (FileNotFoundError, ProcessLookupError):
                dead = True
            if dead:
                break
            time.sleep(0.02)
        else:
            os.kill(child, signal.SIGKILL)
            self.fail("Child survived process group termination")

    def test_log_is_bounded(self):
        result = self.run_python("import sys; sys.stdout.write('x'*100000)", max_bytes=1024)
        self.assertTrue(result.overflow)
        self.assertEqual((self.root / "engine.log").stat().st_size, 1024)

    def test_no_inherited_secrets(self):
        with patch.dict(os.environ, {"TEST_HOST_SECRET": "sentinel"}):
            result = self.run_python("import os; print(os.environ.get('TEST_HOST_SECRET','absent'))")
        self.assertEqual(result.output.strip(), b"absent")


class AdapterTests(CoderCase):
    def runtime_executable(self, name):
        path = Path(self.config.runtime_root) / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture executable; never started\n")
        path.chmod(0o755)
        return path

    def test_missing_opencode_is_blocked(self):
        # This test owns every other dependency: it is independent of the host.
        self.runtime_executable(self.config.python_binary)
        with patch("coder_agent.sandbox.shutil.which", return_value="/usr/bin/bwrap"), \
                patch("coder_agent.sandbox.os.getuid", return_value=1000):
            result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn(self.config.opencode_binary, result.error)
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")

    def test_missing_bubblewrap_is_blocked(self):
        self.runtime_executable(self.config.python_binary)
        self.runtime_executable(self.config.opencode_binary)
        with patch("coder_agent.sandbox.shutil.which", return_value=None):
            result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("Bubblewrap", result.error)

    def test_root_host_user_is_blocked(self):
        self.runtime_executable(self.config.python_binary)
        self.runtime_executable(self.config.opencode_binary)
        with patch("coder_agent.sandbox.shutil.which", return_value="/usr/bin/bwrap"), \
                patch("coder_agent.sandbox.os.getuid", return_value=0):
            result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("unprivileged", result.error)

    def test_namespace_start_failure_is_blocked(self):
        self.runtime_executable(self.config.python_binary)
        self.runtime_executable(self.config.opencode_binary)
        with patch("coder_agent.sandbox.shutil.which", return_value="/usr/bin/bwrap"), \
                patch("coder_agent.sandbox.os.getuid", return_value=1000), \
                patch("coder_agent.engine.run_process", return_value=ProcessResult(1, b"namespace denied")):
            result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")

    def test_process_start_error_is_blocked(self):
        with patch("coder_agent.engine.subprocess.Popen", side_effect=FileNotFoundError("private path")):
            with self.assertRaises(Rejected) as caught:
                run_process(["missing-executable"], self.root / "start.log", timeout=1, max_bytes=1024)
        self.assertEqual(caught.exception.status, "BLOCKED")
        self.assertNotIn("private path", str(caught.exception))

    def test_permissions_deny_shell_delegation_and_tests(self):
        config = opencode_config(Task.model_validate(task()), self.config)
        permission = config["agent"]["coder"]["permission"]
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(set(permission), {"*", "read", "edit"})
        self.assertEqual(permission["edit"], {"*": "deny", "src/app.py": "allow",
            "workspace/src/app.py": "allow", "/workspace/src/app.py": "allow"})
        self.assertEqual(config["agent"]["coder"]["steps"], self.config.max_steps)
        self.assertFalse(config["lsp"])
        self.assertFalse(config["formatter"])
