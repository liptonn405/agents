"""Timeout configuration tests with fake dependencies, no slow models/sockets."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coder_support import CoderCase
from coder_agent import bootstrap
from coder_agent.sandbox import BubblewrapSandbox


class ModelChannelTests(CoderCase):
    def runtime_executable(self, name):
        path = self.root / "runtime" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted test fixture, never executed\n")
        path.chmod(0o755)

    def test_sandbox_passes_bounded_channel_timeout(self):
        self.runtime_executable(self.config.python_binary)
        self.runtime_executable(self.config.opencode_binary)
        workspace = SimpleNamespace(root=self.root / "workspace")
        for configured, expected in ((180, "180"), (1800, "600")):
            config = self.config.model_copy(update={"model_request_timeout_seconds": configured})
            with self.subTest(configured=configured), \
                    patch("coder_agent.sandbox.shutil.which", return_value="/usr/bin/bwrap"), \
                    patch("coder_agent.sandbox.os.getuid", return_value=1000):
                command = BubblewrapSandbox(config).command(
                    workspace, self.root / "control", ["src/app.py"], ["/usr/bin/opencode", "run"])
            position = command.index("/control/bootstrap.py")
            self.assertEqual(command[position + 4], expected)
            self.assertEqual(command[position + 5:], ["/usr/bin/opencode", "run"])
            self.assertIn("--unshare-all", command)
            self.assertIn("--die-with-parent", command)

    def test_bootstrap_applies_channel_timeout_and_keeps_resource_limits(self):
        server = Mock()
        argv = ["bootstrap.py", "300", "256", "33554432", "180", "/usr/bin/opencode", "run"]
        with patch.object(bootstrap.sys, "argv", argv), \
                patch.object(bootstrap.os, "getuid", return_value=65534), \
                patch.object(bootstrap.os.path, "exists", return_value=True), \
                patch.object(bootstrap.resource, "setrlimit") as limits, \
                patch.object(bootstrap, "Server", return_value=server), \
                patch.object(bootstrap.threading, "Thread") as worker, \
                patch.object(bootstrap.subprocess, "call", return_value=0) as execute:
            self.assertEqual(bootstrap.main(), 0)
        self.assertEqual(server.upstream_timeout, 180)
        self.assertEqual(limits.call_count, 5)
        worker.return_value.start.assert_called_once()
        execute.assert_called_once_with(["/usr/bin/opencode", "run"], stdin=subprocess.DEVNULL)

    def test_bridge_uses_configured_timeout_for_socket_and_select(self):
        request = Mock()
        upstream = Mock()
        bridge = bootstrap.Bridge.__new__(bootstrap.Bridge)
        bridge.request = request
        bridge.server = SimpleNamespace(upstream_timeout=180)
        socket = Mock()
        socket.__enter__ = Mock(return_value=upstream)
        socket.__exit__ = Mock(return_value=False)
        with patch.object(bootstrap.socket, "socket", return_value=socket), \
                patch.object(bootstrap.select, "select", return_value=([], [], [])) as select:
            bridge.handle()
        upstream.settimeout.assert_called_once_with(180)
        upstream.connect.assert_called_once_with("/tmp/model.sock")
        select.assert_called_once_with((request, upstream), [], [], 180)

    def test_bootstrap_rejects_invalid_channel_timeout_before_starting_process(self):
        for value in ("0", "-1", "nan", "inf", "7201"):
            argv = ["bootstrap.py", "300", "256", "33554432", value, "/usr/bin/opencode"]
            with self.subTest(value=value), patch.object(bootstrap.sys, "argv", argv), \
                    patch.object(bootstrap.subprocess, "call") as execute:
                self.assertEqual(bootstrap.main(), 125)
                execute.assert_not_called()
