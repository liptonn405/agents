import os

from coder_support import CoderCase
from coder_agent.policy import Rejected, forbid_sensitive, safe_path


class PolicyTests(CoderCase):
    def test_bad_paths(self):
        for path in ("../escape", "/etc/passwd", "src/../app.py", "src//app.py", "src/./app.py",
                     "src/*.py", "src/?", "src/[a]", "C:/secret", "src\\file", "src/file\n", "src/"):
            with self.subTest(path=path), self.assertRaises(Rejected):
                safe_path(path)

    def test_secret_and_agent_configuration_denylist(self):
        for path in (".env", "src/.env.dev", ".ssh/id_rsa", "src/client.key", "src/API_TOKEN.txt",
                     ".git/config", ".opencode/plugin.py", "opencode.json", "src/AGENTS.md",
                     ".aws/config", "vk_auth.json", "prod.sqlite3", ".netrc", ".kube/config",
                     ".docker/config.json", ".npmrc", ".pypirc", ".gnupg/pubring.kbx"):
            with self.subTest(path=path), self.assertRaises(Rejected):
                forbid_sensitive(path)

    def test_undeclared_instruction(self):
        self.payload["instructions"][0]["file"] = "src/other.py"
        self.assert_rejected("SECURITY_VIOLATION")

    def test_task_cannot_authorize_config_changes(self):
        self.payload["instructions"][0]["file"] = "config.json"
        self.payload["read_files"] = self.payload["allowed_write_files"] = ["config.json"]
        self.assert_rejected("SECURITY_VIOLATION")

    def test_secret_read_rejected(self):
        self.payload["read_files"].append(".env")
        self.assert_rejected("SECURITY_VIOLATION")

    def test_traversal_rejected(self):
        self.payload["read_files"].append("../outside.py")
        self.assert_rejected("SECURITY_VIOLATION")

    def test_file_symlink_rejected(self):
        (self.repo / "src/app.py").unlink()
        (self.repo / "src/app.py").symlink_to(self.repo / ".env")
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "SECURITY_VIOLATION")
        self.assertEqual(self.fake.calls, 0)

    def test_parent_symlink_rejected(self):
        (self.repo / "src/app.py").unlink()
        (self.repo / "src").rmdir()
        (self.repo / "src").symlink_to(self.root)
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "SECURITY_VIOLATION")
        self.assertEqual(self.fake.calls, 0)

    def test_hardlink_and_fifo_rejected(self):
        (self.repo / "src/app.py").unlink()
        os.link(self.repo / ".env", self.repo / "src/app.py")
        self.assertEqual(self.agent.run(self.payload).status, "SECURITY_VIOLATION")
        (self.repo / "src/app.py").unlink()
        os.mkfifo(self.repo / "src/app.py")
        self.payload["attempt"] = 2
        self.assertEqual(self.agent.run(self.payload).status, "SECURITY_VIOLATION")

    def test_conflicting_operations(self):
        self.payload["instructions"].append({"file": "src/app.py", "operation": "create",
                                             "target": "other", "requirements": ["Create this file"]})
        self.assert_rejected("NEEDS_CLARIFICATION")

    def test_ambiguous_targets(self):
        self.payload["instructions"].append(self.payload["instructions"][0].copy())
        self.assert_rejected("NEEDS_CLARIFICATION")

    def test_missing_modify_file(self):
        (self.repo / "src/app.py").unlink()
        result = self.agent.run(self.payload)
        self.assertEqual(result.status, "NEEDS_CLARIFICATION")
        self.assertEqual(self.fake.calls, 0)

    def test_unrequested_writable_file(self):
        self.payload["allowed_write_files"].append("new.py")
        self.assert_rejected("NEEDS_CLARIFICATION")
