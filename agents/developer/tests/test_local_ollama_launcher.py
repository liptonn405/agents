"""Private Ollama launch plan checks; never start Ollama or a model."""
from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


LOCATION = Path(__file__).resolve().parents[1] / "tools/serve_local_ollama.py"
SPEC = importlib.util.spec_from_file_location("coder_private_ollama_launcher", LOCATION)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def mount_rules(command, flag):
    return [(command[index + 1], command[index + 2])
            for index, value in enumerate(command) if value == flag]


def environment(command):
    return {command[index + 1]: command[index + 2]
            for index, value in enumerate(command) if value == "--setenv"}


class PrivateOllamaLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coder-ollama-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "ollama/bin/ollama"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("trusted fixture, never executed\n")
        self.binary.chmod(0o755)
        self.private_home = Path("/home/coder-launcher-fixture")
        self.uid = patch.object(launcher.os, "getuid", return_value=1000)
        self.which = patch.object(launcher.shutil, "which", return_value="/usr/bin/bwrap")
        self.home = patch.object(launcher.Path, "home", return_value=self.private_home)
        for mocked in (self.uid, self.which, self.home):
            mocked.start()
            self.addCleanup(mocked.stop)

    def command(self, context=8192, port=11434):
        return launcher.command(self.root, context, port)

    def test_only_private_state_and_models_are_writable_host_mounts(self):
        command = self.command()
        writes = mount_rules(command, "--bind")
        self.assertEqual(writes, [(str(self.root / "models"), "/models"),
            (str(self.root / "state"), str(self.private_home / ".ollama"))])
        reads = mount_rules(command, "--ro-bind")
        self.assertIn((str(self.root / "ollama"), "/opt/ollama"), reads)
        sources = [Path(source) for source, _ in reads + writes]
        for forbidden in (self.private_home, self.private_home / ".ssh",
                          self.private_home / ".config", self.private_home / ".ollama",
                          Path("/var/run/docker.sock"), Path("/run"), Path("/")):
            self.assertNotIn(forbidden, sources)
        self.assertIn("--dir", command)
        self.assertEqual(command[command.index("--dir") + 1], str(self.private_home))
        for path in (self.root / "state", self.root / "models"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_cloud_is_disabled_api_is_loopback_and_original_home_is_preserved(self):
        original_home = os.environ.get("HOME")
        with patch.dict(os.environ, {"GH_TOKEN": "fake-private-token",
                                    "OPENAI_API_KEY": "fake-private-key",
                                    "AWS_SECRET_ACCESS_KEY": "fake-private-access"}):
            command = self.command(context=16384, port=12434)
        self.assertEqual(os.environ.get("HOME"), original_home)
        configured = environment(command)
        self.assertEqual(configured["HOME"], str(self.private_home))
        self.assertEqual(configured["OLLAMA_HOST"], "127.0.0.1:12434")
        self.assertEqual(configured["OLLAMA_MODELS"], "/models")
        self.assertEqual(configured["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(configured["OLLAMA_CONTEXT_LENGTH"], "16384")
        self.assertEqual(configured["OLLAMA_NUM_PARALLEL"], "1")
        self.assertEqual(configured["OLLAMA_MAX_LOADED_MODELS"], "1")
        self.assertIn("--clearenv", command)
        self.assertTrue({"GH_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"}.isdisjoint(configured))
        self.assertNotIn("fake-private", " ".join(command))
        self.assertEqual(command[-2:], ["/opt/ollama/bin/ollama", "serve"])

    def test_process_stays_unprivileged_and_has_no_terminal_or_parent_escape(self):
        command = self.command()
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                     "--disable-userns", "--die-with-parent", "--new-session"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--uid") + 1], "65534")
        self.assertEqual(command[command.index("--gid") + 1], "65534")
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        # Model inference has its own loopback server and registry downloads.
        # The generated-code process retains a separate closed-network sandbox.
        self.assertNotIn("--unshare-net", command)

    def test_missing_binary_or_bubblewrap_is_reported_without_starting_anything(self):
        with patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Bubblewrap"):
                self.command()
        self.binary.unlink()
        with self.assertRaisesRegex(ValueError, "trusted Ollama"):
            self.command()
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "models").exists())

    def test_root_account_is_rejected_before_creating_state(self):
        with patch.object(launcher.os, "getuid", return_value=0):
            with self.assertRaisesRegex(ValueError, "unprivileged"):
                self.command()
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "models").exists())

    def test_invalid_limits_are_rejected_before_creating_state(self):
        for context, port in ((1023, 11434), (32769, 11434), (8192, 1023), (8192, 65536)):
            with self.subTest(context=context, port=port), self.assertRaisesRegex(ValueError, "Invalid context"):
                self.command(context=context, port=port)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "models").exists())

    def test_only_present_nvidia_character_nodes_and_caps_directory_are_exposed(self):
        nodes = {Path("/dev/nvidia0"): stat.S_IFCHR | 0o600,
                 Path("/dev/nvidiactl"): stat.S_IFCHR | 0o600,
                 Path("/dev/nvidia-not-a-device"): stat.S_IFREG | 0o600}
        original_glob, original_stat, original_is_dir = Path.glob, Path.stat, Path.is_dir

        def devices(path, pattern):
            if path == Path("/dev") and pattern == "nvidia*":
                return iter(nodes)
            return original_glob(path, pattern)

        def node_stat(path, *args, **kwargs):
            if path in nodes:
                return SimpleNamespace(st_mode=nodes[path])
            return original_stat(path, *args, **kwargs)

        def directory(path):
            return path == Path("/dev/nvidia-caps") or original_is_dir(path)

        with patch.object(Path, "glob", devices), patch.object(Path, "stat", node_stat), \
                patch.object(Path, "is_dir", directory):
            command = self.command()
        self.assertEqual(set(mount_rules(command, "--dev-bind")), {
            ("/dev/nvidia0", "/dev/nvidia0"), ("/dev/nvidiactl", "/dev/nvidiactl"),
            ("/dev/nvidia-caps", "/dev/nvidia-caps")})

    def test_no_optional_device_mount_when_nvidia_is_absent(self):
        original_is_dir = Path.is_dir
        def directory(path):
            return False if path == Path("/dev/nvidia-caps") else original_is_dir(path)
        with patch.object(Path, "glob", return_value=iter(())), patch.object(Path, "is_dir", directory):
            command = self.command()
        self.assertNotIn("--dev-bind", command)

    def test_existing_root_permissions_are_made_private(self):
        self.root.chmod(0o755)
        self.command()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_installation_root_symlink_is_rejected(self):
        alias = self.root / "installation-root-link"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "root must not"):
            launcher.command(alias, 8192, 11434)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "models").exists())

    def test_installation_symlink_and_escaped_executable_are_rejected(self):
        installation = self.root / "ollama"
        original = self.root / "original-installation"
        installation.rename(original)
        installation.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "private real Ollama"):
            self.command()
        installation.unlink()
        original.rename(installation)
        self.binary.unlink()
        outside = self.root / "outside-executable"
        outside.write_text("another trusted fixture; must remain outside runtime\n")
        outside.chmod(0o755)
        self.binary.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes its installation"):
            self.command()
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "models").exists())

    def test_state_or_models_symlink_does_not_expose_or_chmod_external_directory(self):
        outside = self.root / "external-private-state-fixture"
        outside.mkdir(mode=0o755)
        canary = outside / "canary.txt"
        canary.write_text("not a credential; preserve this directory\n")
        for name in ("state", "models"):
            path = self.root / name
            path.symlink_to(outside, target_is_directory=True)
            try:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "private real directories"):
                    self.command()
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
                self.assertEqual(canary.read_text(), "not a credential; preserve this directory\n")
                other = self.root / ("models" if name == "state" else "state")
                self.assertFalse(other.exists())
            finally:
                path.unlink()

    def test_existing_state_file_is_preserved_and_rejected(self):
        state = self.root / "state"
        state.write_text("existing operator file\n")
        with self.assertRaisesRegex(ValueError, "private real directories"):
            self.command()
        self.assertEqual(state.read_text(), "existing operator file\n")
        self.assertFalse((self.root / "models").exists())
