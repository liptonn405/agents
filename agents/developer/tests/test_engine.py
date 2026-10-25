from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coder_support import CoderCase, task
from coder_agent import CoderAgent, Config, Policy, Task
from coder_agent.build_runtime import build_runtime
from coder_agent.engine import OpenCodeEngine, opencode_config, parse_events, run_process


def event(reply):
    return json.dumps({"type": "text", "part": {"text": json.dumps(reply)}}).encode() + b"\n"


class EventTests(unittest.TestCase):
    def test_structured_completion(self):
        data = event({"status": "COMPLETED", "message": "Edited", "required_files": []})
        self.assertEqual(parse_events(data, 10).status, "COMPLETED")

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
    def test_missing_opencode_is_blocked(self):
        result = CoderAgent(self.config).run(self.payload)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("executable missing", result.error)
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")

    def test_permissions_deny_shell_delegation_and_tests(self):
        config = opencode_config(Task.model_validate(task()), self.config)
        permission = config["agent"]["coder"]["permission"]
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(set(permission), {"*", "read", "edit"})
        self.assertEqual(permission["edit"], {"*": "deny", "/workspace/src/app.py": "allow"})
        self.assertEqual(config["agent"]["coder"]["steps"], self.config.max_steps)
        self.assertFalse(config["lsp"])
        self.assertFalse(config["formatter"])


FAKE_OPENCODE = '''#!/usr/bin/python3
import http.client,json,os,pathlib,socket,subprocess,sys,time
if sys.argv[1:] == ['--version']:
    print('fake-opencode-1.0')
    raise SystemExit(0)
if sys.argv[1:] == ['run','--help']:
    print('opencode run --format --model --agent')
    raise SystemExit(0)
assert sys.argv[1:8] == ['run','--format','json','--model','gateway/local-model','--agent','coder']
assert sys.argv[8] == '--'
task=json.loads(sys.argv[9].split('Validated task JSON:\\n',1)[1])
assert os.getuid() == 65534
assert 'TEST_HOST_SECRET' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/workspace/.git').exists()
assert not pathlib.Path('/workspace/.env').exists()
assert not pathlib.Path('/home/adam/.ssh').exists()
assert not pathlib.Path('/usr/bin/git').exists()
assert not pathlib.Path('/bin/sh').exists()
print(json.dumps({'type':'sandbox_probe','uid':os.getuid(),'netns':os.readlink('/proc/self/ns/net')}))
for target in ['/escape','/workspace/undeclared.py','/control/opencode.json']:
    try:
        pathlib.Path(target).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('write was allowed: '+target)
if pathlib.Path('/workspace/readme.txt').exists():
    try:
        pathlib.Path('/workspace/readme.txt').write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('read-only context was writable')
with socket.socket() as s:
    s.settimeout(.2)
    try:
        s.connect(('192.0.2.1',443))
    except OSError:
        pass
    else:
        raise AssertionError('outbound network available')
if task['task_id'] == 'timeout':
    subprocess.Popen(['/usr/bin/python3','-c',
        "import pathlib,time; time.sleep(3); pathlib.Path('/workspace/src/app.py').write_text('SURVIVED')"],
        start_new_session=True)
    time.sleep(10)
if task['task_id'] == 'crash':
    raise SystemExit(9)
if task['task_id'] == 'channel':
    connection = http.client.HTTPConnection('127.0.0.1',8765,timeout=3)
    connection.request('POST','/v1/chat/completions',json.dumps({'model':'local-model'}))
    response = connection.getresponse()
    assert response.status == 200 and response.read() == b'{"ok":true}'
    connection.close()
if task['task_id'] == 'clarify':
    reply={'status':'NEEDS_CLARIFICATION','message':'Need a config decision','required_files':['config.json']}
else:
    pathlib.Path('/workspace/src/app.py').write_text('VALUE = 2\\n')
    if 'new.py' in task['allowed_write_files']:
        pathlib.Path('/workspace/new.py').write_text('NEW = 1\\n')
    reply={'status':'COMPLETED','message':'Prescribed edit finished','required_files':[]}
print(json.dumps({'type':'text','part':{'text':json.dumps(reply)}}))
'''


class IsolatedOpenCodeTests(CoderCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        if not shutil.which("bwrap"):
            raise unittest.SkipTest("Bubblewrap is not installed")
        probe = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr",
                                "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                                "/usr/bin/true"], capture_output=True)
        if probe.returncode:
            if os.environ.get("CODER_REQUIRE_SANDBOX") == "1":
                raise AssertionError(probe.stderr.decode())
            raise unittest.SkipTest("Kernel/supervisor denies Bubblewrap namespaces")
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
