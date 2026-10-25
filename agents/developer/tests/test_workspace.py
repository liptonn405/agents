from coder_support import CoderCase
from coder_agent.engine import EngineOutcome


class WorkspaceTests(CoderCase):
    def mutate(self, action):
        def change(task, workspace, run_dir):
            action(workspace.root)
            return EngineOutcome("COMPLETED", "Done")
        self.fake.action = change
        return self.agent.run(self.payload)

    def test_unexpected_file_is_rejected_and_quarantined(self):
        result = self.mutate(lambda root: (root / "unrequested.py").write_text("bad\n"))
        self.assertEqual(result.status, "SECURITY_VIOLATION")
        self.assertNotIn("files", result.artifacts)
        self.assertNotIn("patch", result.artifacts)
        self.assertTrue((self.workspace(result) / "unrequested.py").exists())

    def test_read_only_context_change(self):
        self.payload["read_files"].append("readme.txt")
        result = self.mutate(lambda root: (root / "readme.txt").write_text("bad"))
        self.assertEqual(result.status, "SECURITY_VIOLATION")

    def test_delete_rejected(self):
        self.assertEqual(self.mutate(lambda root: (root / "src/app.py").unlink()).status, "SECURITY_VIOLATION")

    def test_mode_change_rejected(self):
        self.assertEqual(self.mutate(lambda root: (root / "src/app.py").chmod(0o755)).status, "SECURITY_VIOLATION")

    def test_output_symlink_not_followed(self):
        def change(root):
            (root / "src/app.py").unlink()
            (root / "src/app.py").symlink_to(self.repo / ".env")
        result = self.mutate(change)
        self.assertEqual(result.status, "SECURITY_VIOLATION")
        self.assertNotIn("TEST_SENTINEL", result.model_dump_json())

    def test_new_empty_directory_rejected(self):
        result = self.mutate(lambda root: (root / "unexpected").mkdir())
        self.assertEqual(result.status, "SECURITY_VIOLATION")

    def test_create_file(self):
        self.payload["instructions"].append({"file": "new.py", "operation": "create",
                                             "target": "module", "requirements": ["Define NEW = 1"]})
        self.payload["allowed_write_files"].append("new.py")
        def change(root):
            (root / "src/app.py").write_text("VALUE = 2\n")
            (root / "new.py").write_text("NEW = 1\n")
        result = self.mutate(change)
        self.assertEqual(result.status, "COMPLETED", result.error)
        self.assertEqual(result.changed_files, ["new.py", "src/app.py"])
        self.assertIn("--- /dev/null", (self.workspace(result).parent / "changes.patch").read_text())
        self.assertFalse((self.repo / "new.py").exists())

    def test_source_drift_blocks_stale_output(self):
        def change(root):
            (root / "src/app.py").write_text("VALUE = 2\n")
            (self.repo / "src/app.py").write_text("EXTERNAL_EDIT = 1\n")
        result = self.mutate(change)
        self.assertEqual(result.status, "BLOCKED")
        self.assertNotIn("files", result.artifacts)

    def test_patch_preserves_missing_newlines(self):
        (self.repo / "src/app.py").write_text("VALUE = 1")
        result = self.mutate(lambda root: (root / "src/app.py").write_text("VALUE = 2"))
        self.assertEqual(result.status, "COMPLETED")
        patch = (self.workspace(result).parent / "changes.patch").read_text()
        self.assertEqual(patch.count("\\ No newline at end of file"), 2)

    def test_output_size_and_encoding_policy(self):
        for attempt, data in enumerate((b"x" * (self.config.policy.max_file_bytes + 1), b"\xff"), start=1):
            self.payload["attempt"] = attempt
            result = self.mutate(lambda root: (root / "src/app.py").write_bytes(data))
            self.assertEqual(result.status, "SECURITY_VIOLATION")
