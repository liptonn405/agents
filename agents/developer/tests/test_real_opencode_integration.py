"""Opt-in real CLI + real sandbox + scripted local API, NOT an actual AI model.

Set CODER_TEST_OPENCODE to an operator-trusted standalone binary. No credentials,
public model calls or Miminet checkout are used. The model fixture issues real
tool calls, so permissions, event format and writes exercise OpenCode itself.
"""
from __future__ import annotations

import http.server
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from coder_support import CoderCase
from coder_agent import CoderAgent
from coder_agent.build_runtime import build_runtime
from coder_agent.gateway import Gateway, Handler
from sandbox_support import require_sandbox


class ScriptedModel(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        mode = self.server.mode
        if mode in ("auth", "missing-model"):
            self.send_response(401 if mode == "auth" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"fake-provider-private-sentinel"}}')
            return
        tools = {item.get("function", {}).get("name") for item in body.get("tools", [])}
        calls = [call for message in body.get("messages", []) for call in message.get("tool_calls", [])]
        used = [call["function"]["name"] for call in calls]
        tool, arguments = None, None
        if tools & {"read", "edit", "write"}:
            if mode == "escape" and not calls:
                tool = "write"
                arguments = {"filePath": "/workspace/undeclared.py", "content": "UNAPPROVED = 1\n"}
            elif mode != "escape" and "read" not in used:
                tool, arguments = "read", {"filePath": "/workspace/src/app.py"}
            elif mode != "escape" and "edit" not in used:
                tool, arguments = "edit", {"filePath": "/workspace/src/app.py",
                    "oldString": "VALUE = 1", "newString": "VALUE = 2", "replaceAll": False}
            text = json.dumps({"status": "COMPLETED", "message": "Prescribed edit finished", "required_files": []})
        else:
            text = "Edit the value"  # OpenCode's own title/summary request.
        if tool:
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"fixture-{len(calls)}",
                "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
        else:
            delta = {"role": "assistant", "content": text}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            message = dict(delta)
            for call in message.get("tool_calls", []):
                call.pop("index", None)
            self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "local-model", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if tool else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode())
            return
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for content, reason in ((delta, None), ({}, "tool_calls" if tool else "stop")):
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                     "model": "local-model", "choices": [{"index": 0, "delta": content, "finish_reason": reason}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class RealOpenCodeTests(CoderCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get("CODER_TEST_OPENCODE")
        if not path:
            raise unittest.SkipTest("Set CODER_TEST_OPENCODE for real CLI integration (scripted model)")
        binary = Path(path)
        if not binary.is_absolute() or not binary.is_file():
            raise AssertionError("CODER_TEST_OPENCODE must be an existing absolute trusted binary")
        require_sandbox()
        cls.runtime_tmp = tempfile.TemporaryDirectory(prefix="coder-real-runtime-")
        cls.addClassCleanup(cls.runtime_tmp.cleanup)
        cls.runtime = Path(cls.runtime_tmp.name) / "runtime"
        build_runtime(cls.runtime, binary)

    def setUp(self):
        super().setUp()
        self.model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ScriptedModel)
        self.model.requests = []
        self.model.mode = "edit"
        self.gateway = Gateway(str(self.root / "model.sock"),
                               f"http://127.0.0.1:{self.model.server_port}", "local-model")
        self.request_summaries = []
        original_post = Handler.do_POST
        summaries = self.request_summaries
        def observed_post(handler):
            raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
            body = json.loads(raw)
            summaries.append({key: body.get(key) for key in ("model", "n", "max_tokens", "max_completion_tokens")})
            handler.rfile = io.BytesIO(raw)
            return original_post(handler)
        observation = patch.object(Handler, "do_POST", observed_post)
        observation.start()
        self.addCleanup(observation.stop)
        for server in (self.model, self.gateway):
            self.addCleanup(server.server_close)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 3)
            self.addCleanup(server.shutdown)
        self.config = self.config.model_copy(update={"runtime_root": str(self.runtime),
            "model_socket": str(self.root / "model.sock"), "timeout_seconds": 60, "max_steps": 5})
        self.agent = CoderAgent(self.config)

    def execute(self, expected):
        result = self.agent.run(self.payload)
        log = self.workspace(result).parent / "engine.log"
        diagnostic = (result.error, log.read_text(errors="replace")[-8000:] if log.exists() else "no log",
                      {"model_requests": len(self.model.requests), "gateway_requests": self.request_summaries})
        self.assertEqual(result.status, expected, diagnostic)
        self.assertEqual((self.repo / "src/app.py").read_text(), "VALUE = 1\n")
        self.assertFalse((self.repo / "undeclared.py").exists())
        return result, log

    def test_real_cli_executes_read_and_edit_with_scripted_model(self):
        result, _ = self.execute("COMPLETED")
        self.assertEqual(result.changed_files, ["src/app.py"])
        self.assertEqual((self.workspace(result) / "src/app.py").read_text(), "VALUE = 2\n")
        self.assertGreaterEqual(len(self.model.requests), 3)
        self.assertTrue(all(request.get("tools") for request in self.model.requests),
                        "A fixed session title should avoid title-only model requests")

    def test_real_cli_rejects_unapproved_model_tool_call(self):
        self.model.mode = "escape"
        result, _ = self.execute("SECURITY_VIOLATION")
        self.assertFalse((self.workspace(result) / "undeclared.py").exists())
        self.assertEqual((self.workspace(result) / "src/app.py").read_text(), "VALUE = 1\n")

    def test_real_cli_auth_failure_is_blocked_and_sanitized(self):
        self.model.mode = "auth"
        result, log = self.execute("BLOCKED")
        self.assertIn("authentication", result.error)
        self.assertNotIn("fake-provider-private-sentinel", result.model_dump_json() + log.read_text())

    def test_real_cli_missing_model_is_blocked(self):
        self.model.mode = "missing-model"
        result, _ = self.execute("BLOCKED")
        self.assertIn("model", result.error.lower())

    def test_real_cli_passes_reasoning_effort_to_model(self):
        self.agent = CoderAgent(self.config.model_copy(update={"model_reasoning_effort": "none"}))
        self.execute("COMPLETED")
        self.assertTrue(self.model.requests)
        self.assertTrue(all(request.get("reasoning_effort") == "none" for request in self.model.requests))

    def test_real_cli_passes_zero_temperature_to_model(self):
        self.agent = CoderAgent(self.config.model_copy(update={"model_temperature": 0.0}))
        self.execute("COMPLETED")
        self.assertTrue(self.model.requests)
        self.assertTrue(all(request.get("temperature") == 0.0 for request in self.model.requests))
