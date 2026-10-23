"""Offline Issue retrieval tests using a fake gh executable on PATH."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from developer_agent import AgentError, fetch_issue
from fake_gh import write_fake_gh


class FetchIssueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bin = Path(temporary.name)
        self.issue = {"number": 543, "title": "Убрать поля", "body": "Описание\nзадачи"}
        self.secret = "fake-github-token-must-not-leak"
        self.environment = patch.dict(os.environ, {
            "PATH": str(self.bin), "GH_TOKEN": self.secret,
            "GH_HOST": "unrelated.example", "GH_DEBUG": "api", "DEBUG": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fake(self, **kwargs):
        return write_fake_gh(self.bin, **kwargs)

    def test_authenticated_request_and_only_issue_fields_returned(self):
        self.fake(stdout=json.dumps({**self.issue, "auth_debug": self.secret}),
                  stderr=self.secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = fetch_issue(543)
        self.assertEqual(result, self.issue)
        self.assertEqual(output.getvalue(), "")
        invocation = json.loads((self.bin / "invocation.json").read_text())
        self.assertEqual(invocation["argv"], [
            "api", "repos/mimi-net/miminet/issues/543",
            "--hostname", "github.com", "--method", "GET",
        ])
        self.assertTrue(invocation["token_available"])
        self.assertIsNone(invocation["debug"])
        self.assertIsNone(invocation["legacy_debug"])
        self.assertEqual(invocation["prompt_disabled"], "1")
        self.assertEqual(invocation["stdin"], "")
        self.assertNotIn(self.secret, json.dumps(invocation))

    def test_null_or_missing_body_becomes_empty_text(self):
        for body in ({"body": None}, {}):
            with self.subTest(body=body):
                self.fake(stdout=json.dumps({"number": 543, "title": "Title", **body}))
                self.assertEqual(fetch_issue(543)["body"], "")

    def test_missing_gh(self):
        with self.assertRaisesRegex(AgentError, "gh.*not found"):
            fetch_issue(543)

    def test_gh_not_executable(self):
        executable = self.fake(stdout="")
        executable.chmod(0o644)
        with self.assertRaisesRegex(AgentError, "executable permissions"):
            fetch_issue(543)

    def test_authentication_failure(self):
        for code, diagnostic in [(4, ""), (1, "Bad credentials (HTTP 401)"),
                                 (1, "please run: gh auth login")]:
            with self.subTest(code=code, diagnostic=diagnostic):
                self.fake(code=code, stdout=self.secret, stderr=diagnostic + self.secret)
                with self.assertRaisesRegex(AgentError, "gh auth login") as caught:
                    fetch_issue(543)
                self.assertNotIn(self.secret, str(caught.exception))

    def test_api_errors_do_not_expose_diagnostics(self):
        for diagnostic, expected in [
            ("API rate limit exceeded (HTTP 403)", "rate limit exceeded"),
            ("Forbidden (HTTP 403)", "access denied"),
            ("Not Found (HTTP 404)", "not found or inaccessible"),
            ("Server error (HTTP 500)", "gh exit 1"),
            ("connection refused", "connectivity"),
        ]:
            with self.subTest(diagnostic=diagnostic):
                self.fake(code=1, stdout=self.secret, stderr=diagnostic + self.secret)
                with self.assertRaisesRegex(AgentError, expected) as caught:
                    fetch_issue(543)
                self.assertNotIn(self.secret, str(caught.exception))

    def test_invalid_json_does_not_expose_response(self):
        self.fake(stdout=self.secret)
        with self.assertRaisesRegex(AgentError, "invalid JSON"):
            fetch_issue(543)

    def test_invalid_response_shape(self):
        responses = [[], None, "text", 123, {},
                     {**self.issue, "title": None},
                     {**self.issue, "number": 544},
                     {**self.issue, "number": "543"},
                     {**self.issue, "number": True},
                     {**self.issue, "body": {"unexpected": "object"}}]
        for response in responses:
            with self.subTest(response=response):
                self.fake(stdout=json.dumps(response))
                with self.assertRaises(AgentError):
                    fetch_issue(543)

    def test_pull_request_is_rejected(self):
        self.fake(stdout=json.dumps({**self.issue, "pull_request": {}}))
        with self.assertRaisesRegex(AgentError, "pull request"):
            fetch_issue(543)

    def test_invalid_number_does_not_start_gh(self):
        self.fake(stdout=json.dumps(self.issue))
        for number in [0, -1, True, "543", 1.5]:
            with self.subTest(number=number), self.assertRaises(AgentError):
                fetch_issue(number)
        self.assertFalse((self.bin / "invocation.json").exists())

    def test_timeout_does_not_leak_captured_output_or_exception_chain(self):
        timeout = subprocess.TimeoutExpired("gh", 30, output=self.secret, stderr=self.secret)
        with patch("developer_agent.subprocess.run", side_effect=timeout):
            try:
                fetch_issue(543)
            except AgentError as error:
                self.assertIn("timed out", str(error))
                self.assertNotIn(self.secret, traceback.format_exc())
            else:
                self.fail("Expected AgentError")


if __name__ == "__main__":
    unittest.main()
