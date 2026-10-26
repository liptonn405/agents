"""Runtime layout unit tests; do not require Bubblewrap or installed OpenCode."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import coder_support
from coder_agent.build_runtime import build_runtime


class RuntimeLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "trusted-opencode"
        self.binary.write_text("trusted fixture\n")
        self.binary.chmod(0o755)

    def test_usr_local_python_keeps_prefix_and_relative_alias(self):
        runtime = self.root / "runtime"
        python = Path("/usr/local/bin/python3.audit-fixture")
        stdlib = Path("/usr/local/lib/python3.audit-fixture")
        original_stat = Path.stat

        def fixture_stat(path, *args, **kwargs):
            if path == python:
                return SimpleNamespace(st_mode=0o100755)
            return original_stat(path, *args, **kwargs)

        def fixture_copy(source, destination):
            Path(destination).write_text("copied " + str(source))

        def fixture_tree(source, destination, **kwargs):
            self.assertEqual(source, stdlib)
            Path(destination).mkdir(parents=True)
            (Path(destination) / "os.py").write_text("stdlib fixture")

        with patch("coder_agent.build_runtime.sys.executable", str(python)), \
                patch("coder_agent.build_runtime.sysconfig.get_path", return_value=str(stdlib)), \
                patch.object(Path, "stat", fixture_stat), \
                patch("coder_agent.build_runtime.shutil.copyfile", side_effect=fixture_copy), \
                patch("coder_agent.build_runtime.shutil.copytree", side_effect=fixture_tree), \
                patch("coder_agent.build_runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            build_runtime(runtime, self.binary)

        actual_python = runtime / python.relative_to("/")
        alias = runtime / "usr/bin/python3"
        self.assertTrue(actual_python.is_file())
        self.assertTrue(alias.is_symlink())
        self.assertFalse(Path(os.readlink(alias)).is_absolute())
        self.assertEqual(alias.resolve(), actual_python)
        self.assertTrue((runtime / stdlib.relative_to("/") / "os.py").is_file())

    def test_python_hidden_by_private_mounts_is_rejected_before_writes(self):
        for prefix in ("/home/test/.pyenv", "/tmp/installed-python", "/workspace/venv"):
            runtime = self.root / "not-created"
            with self.subTest(prefix=prefix), \
                    patch("coder_agent.build_runtime.sys.executable", prefix + "/bin/python3"), \
                    patch("coder_agent.build_runtime.sysconfig.get_path", return_value=prefix + "/lib/python3.14"):
                with self.assertRaisesRegex(ValueError, "hidden by sandbox mounts"):
                    build_runtime(runtime, self.binary)
            self.assertFalse(runtime.exists())

    def test_existing_destination_not_modified(self):
        runtime = self.root / "existing"
        runtime.mkdir()
        canary = runtime / "user-file"
        canary.write_text("preserve")
        with self.assertRaises(FileExistsError):
            build_runtime(runtime, self.binary)
        self.assertEqual(canary.read_text(), "preserve")
