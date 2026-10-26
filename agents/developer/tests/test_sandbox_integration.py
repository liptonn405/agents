"""Actual Bubblewrap integration; runnable separately from test_engine.py."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from coder_support import CoderCase
from coder_agent import CoderAgent
from coder_agent.build_runtime import build_runtime
from sandbox_support import FAKE_OPENCODE, require_sandbox


class IsolatedOpenCodeTests(CoderCase):
    @classmethod
    def setUpClass(cls):
        require_sandbox()
        cls.runtime_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.runtime_tmp.cleanup)
        root = Path(cls.runtime_tmp.name)
        binary = root / "fake-opencode"
        binary.write_text(FAKE_OPENCODE)
        binary.chmod(0o755)
        cls.runtime = root / "runtime"
        build_runtime(cls.runtime, binary)

    def setUp(self):
        super().setUp()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.root / "model.sock"))
        self.addCleanup(self.sock.close)
        self.config = self.config.model_copy(update={"runtime_root": str(self.runtime),
                                                    "model_socket": str(self.root / "model.sock"),
                                                    "timeout_seconds": 5})
        self.agent = CoderAgent(self.config)

    def test_fake_opencode_in_real_namespace_sandbox(self):
        self.payload["read_files"].append("readme.txt")
        with patch.dict(os.environ, {"TEST_HOST_SECRET": "sentinel"}):
            result = self.agent.run(self.payload)
        log = self.workspace(result).parent / "engine.log"
        self.assertEqual(result.status, "COMPLETED", (result.error, log.read_text() if log.exists() else "no log"))
        self.assertEqual(result.changed_files, ["src/app.py"])
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")
        self.assertEqual((self.repo / "readme.txt").read_text(), "Read only context\n")
        probe = next(json.loads(line) for line in log.read_text().splitlines() if 'sandbox_probe' in line)
        self.assertNotEqual(probe["netns"], os.readlink("/proc/self/ns/net"))
        self.assertEqual(probe["uid"], 65534)

    def test_isolated_create(self):
        self.payload["instructions"].append({"file": "new.py", "operation": "create",
                                             "target": "module", "requirements": ["Set NEW = 1"]})
        self.payload["allowed_write_files"].append("new.py")
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertFalse((self.repo / "new.py").exists())

    def test_isolated_timeout(self):
        self.payload["task_id"] = "timeout"
        self.config = self.config.model_copy(update={"timeout_seconds": 2})
        result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "FAILED", result.error)
        self.assertIn("timed out", result.error)
        self.assertNotIn("files", result.artifacts)
        time.sleep(2)
        self.assertEqual((self.workspace(result) / "src/app.py").read_text(), "VALUE = 1\n")

    def test_isolated_crash(self):
        self.payload["task_id"] = "crash"
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "FAILED", result.error)
        self.assertIn("code 9", result.error)

    def test_isolated_clarification(self):
        self.payload["task_id"] = "clarify"
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "NEEDS_CLARIFICATION", result.error)
        self.assertIn("config.json", result.error)

    def test_missing_gateway_is_blocked(self):
        config = self.config.model_copy(update={"model_socket": None})
        result = CoderAgent(config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("gateway", result.error)

    def test_cli_uses_same_boundary(self):
        config_file = self.root / "config.json"
        task_file = self.root / "task.json"
        output_file = self.root / "result.json"
        config_file.write_text(self.config.model_dump_json())
        task_file.write_text(json.dumps(self.payload))
        result = subprocess.run([sys.executable, "-m", "coder_agent", "--config", str(config_file),
                                 "--input", str(task_file), "--output", str(output_file)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(output_file.read_text())["status"], "COMPLETED")

    def test_only_model_channel_crosses_network_namespace(self):
        self.payload["task_id"] = "channel"
        self.sock.listen(1)
        self.sock.settimeout(5)
        requests = []

        def reply():
            connection, _ = self.sock.accept()
            with connection:
                connection.settimeout(3)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += connection.recv(4096)
                requests.append(data)
                connection.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')

        worker = threading.Thread(target=reply, daemon=True)
        worker.start()
        result = self.agent.run(self.payload)
        worker.join(5)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertTrue(requests[0].startswith(b"POST /v1/chat/completions "))

    def negative_probe(self, kind):
        self.payload["task_id"] = "negative-" + kind
        self.payload["read_files"].append("readme.txt")
        canary = self.root / "private-home/.ssh/id_ed25519"
        canary.parent.mkdir(parents=True, mode=0o700)
        canary.write_text("FAKE_PRIVATE_CANARY_NOT_A_REAL_KEY\n")
        self.payload["acceptance_criteria"].append("HOST_CANARY=" + str(canary))

        def fingerprints():
            return {p.relative_to(self.repo).as_posix():
                    (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode)
                    for p in self.repo.rglob("*") if p.is_file()}

        before = fingerprints()
        result = self.agent.run(self.payload)
        log = self.workspace(result).parent / "engine.log"
        self.assertEqual(result.status, "COMPLETED", (result.error, log.read_text() if log.exists() else "no log"))
        self.assertEqual(result.changed_files, ["src/app.py"])
        probes = [json.loads(line) for line in log.read_text().splitlines() if 'negative_probe' in line]
        self.assertTrue(probes, "The actual isolated process did not run its negative checks")
        self.assertTrue(all(probe["errno"] for probe in probes))
        self.assertEqual(fingerprints(), before)
        self.assertEqual(canary.read_text(), "FAKE_PRIVATE_CANARY_NOT_A_REAL_KEY\n")
        self.assertNotIn("FAKE_PRIVATE_CANARY", log.read_text())
        self.assertNotIn("FAKE_PRIVATE_CANARY", result.model_dump_json())

    def test_atomic_replace_from_private_tmp_is_blocked(self):
        self.negative_probe("atomic-tmp")

    def test_neighbor_temp_and_rename_are_blocked(self):
        self.negative_probe("atomic-neighbor")

    def test_symlink_cannot_change_source_or_control_authority(self):
        self.negative_probe("symlink")

    def test_hardlink_cannot_change_source_authority(self):
        self.negative_probe("hardlink")

    def test_context_control_and_undeclared_sources_are_read_only(self):
        self.negative_probe("read-only")

    def test_host_secret_canary_is_inaccessible_even_through_proc(self):
        self.negative_probe("host-canary")
