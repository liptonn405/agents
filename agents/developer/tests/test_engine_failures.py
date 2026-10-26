"""Dependency-independent error classification and bounded journal tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coder_support import CoderCase, task
from coder_agent.contracts import Task
from coder_agent.engine import OpenCodeEngine, ProcessResult, opencode_config, parse_events, run_process
from pydantic import ValidationError


def failure_event(error):
    return json.dumps({"type": "error", "error": error}).encode() + b"\n"


class FailureEventTests(unittest.TestCase):
    def test_provider_environment_failures_are_blocked_without_echoing_details(self):
        sentinel = "fake-provider-private-detail"
        cases = (
            ({"name": "ProviderAuthError", "data": {"message": sentinel}}, "authentication"),
            ({"name": "APIError", "data": {"statusCode": 401, "responseBody": sentinel}}, "authentication"),
            ({"name": "APIError", "data": {"statusCode": 403, "responseBody": sentinel}}, "denied access"),
            ({"name": "ProviderModelNotFoundError", "data": {"modelID": sentinel}}, "model endpoint"),
            ({"name": "APIError", "data": {"statusCode": 404, "responseBody": sentinel}}, "model endpoint"),
            ({"name": "ProviderInitError", "data": {"message": sentinel}}, "initialize"),
            ({"name": "APIError", "data": {"statusCode": 429, "responseBody": sentinel}}, "budget"),
            ({"name": "APIError", "data": {"statusCode": 503, "responseBody": sentinel}}, "unreachable"),
            ({"name": "UnknownError", "data": {"message": "fetch failed " + sentinel}}, "unreachable"),
        )
        for error, reason in cases:
            with self.subTest(error=error):
                result = parse_events(failure_event(error), 20)
                self.assertEqual(result.status, "BLOCKED")
                self.assertIn(reason, result.message)
                self.assertNotIn(sentinel, result.message)

    def test_unknown_error_remains_failed(self):
        result = parse_events(failure_event({"name": "UnexpectedError", "data": {"message": "private-detail"}}), 20)
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("private-detail", result.message)


class AdapterFailureTests(CoderCase):
    def invoke_with_result(self, final, *, help_stderr=False, title_supported=False):
        self.config = self.config.model_copy(update={"model_socket": str(self.root / "not-used.sock")})
        engine = OpenCodeEngine(self.config)
        # This unit test deliberately has no external dependency preconditions:
        # it tests classification after an isolated process result is received.
        engine.sandbox = Mock()
        engine.sandbox.command.side_effect = lambda workspace, control, writes, argv: argv
        run_dir = self.root / "adapter-run"
        run_dir.mkdir()
        help_text = b"run --format --model --agent" + (b" --title" if title_supported else b"") + b"\n"
        with patch("coder_agent.engine.run_process", side_effect=[
                ProcessResult(0, b"1.19.4\n"),
                ProcessResult(0, b"", stderr=help_text) if help_stderr
                else ProcessResult(0, help_text), final]) as process:
            result = engine.execute(Task.model_validate(self.payload), Mock(), run_dir)
            self.adapter_argv = process.call_args.args[0]
            return result

    def test_cli_help_on_stderr_is_supported(self):
        result = self.invoke_with_result(ProcessResult(0, failure_event({"name": "APIError",
                                        "data": {"statusCode": 401}})), help_stderr=True)
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("authentication", result.message)

    def test_cli_sets_fixed_title_when_supported(self):
        self.invoke_with_result(ProcessResult(0, failure_event({"name": "UnexpectedError"})),
                                help_stderr=True, title_supported=True)
        position = self.adapter_argv.index("--title")
        self.assertEqual(self.adapter_argv[position:position + 2], ["--title", "CoderAgent"])
        self.assertLess(position, self.adapter_argv.index("--"))

    def test_cli_without_title_flag_keeps_compatible_arguments(self):
        self.invoke_with_result(ProcessResult(0, failure_event({"name": "UnexpectedError"})))
        self.assertNotIn("--title", self.adapter_argv)
        self.assertEqual(self.adapter_argv[:7],
                         [self.config.opencode_binary, "run", "--format", "json", "--model",
                          f"gateway/{self.config.model}", "--agent"])

    def test_model_limits_are_explicit_and_validated(self):
        config = opencode_config(Task.model_validate(self.payload), self.config)
        self.assertEqual(config["provider"]["gateway"]["models"][self.config.model]["limit"],
                         {"context": 131072, "output": 8192})
        values = self.config.model_dump()
        values.update(model_context_tokens=1024, model_output_tokens=2048)
        with self.assertRaises(ValidationError):
            type(self.config).model_validate(values)

    def test_reasoning_control_is_trusted_and_optional(self):
        plan = Task.model_validate(self.payload)
        original = opencode_config(plan, self.config)["provider"]["gateway"]["models"][self.config.model]
        self.assertNotIn("options", original)
        configured = self.config.model_copy(update={"model_reasoning_effort": "none"})
        model = opencode_config(plan, configured)["provider"]["gateway"]["models"][configured.model]
        self.assertEqual(model["options"], {"reasoningEffort": "none"})
        value = self.config.model_dump()
        value["model_reasoning_effort"] = "arbitrary"
        with self.assertRaises(ValidationError):
            type(self.config).model_validate(value)
        self.payload["model_reasoning_effort"] = "none"
        with self.assertRaises(ValidationError):
            Task.model_validate(self.payload)

    def test_model_temperature_is_bounded_trusted_configuration(self):
        plan = Task.model_validate(self.payload)
        self.assertNotIn("temperature", opencode_config(plan, self.config)["agent"]["coder"])
        self.assertNotIn("temperature", opencode_config(plan, self.config)["provider"]["gateway"]["models"][self.config.model])
        config = self.config.model_copy(update={"model_temperature": 0.0})
        self.assertEqual(opencode_config(plan, config)["agent"]["coder"]["temperature"], 0.0)
        self.assertIs(opencode_config(plan, config)["provider"]["gateway"]["models"][config.model]["temperature"], True)
        for value in (-1, 3, float("nan"), float("inf")):
            values = self.config.model_dump()
            values["model_temperature"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                type(self.config).model_validate(values)

    def test_nonzero_provider_error_is_blocked(self):
        result = self.invoke_with_result(ProcessResult(1, failure_event({
            "name": "ProviderModelNotFoundError", "data": {"modelID": "fake-secret-detail"}})))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("model endpoint", result.message)
        self.assertNotIn("fake-secret-detail", result.message)

    def test_nonzero_stderr_authentication_failure_is_blocked(self):
        result = self.invoke_with_result(ProcessResult(1, b"", stderr=b"ProviderAuthError: private credential details\n"))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("authentication", result.message)
        self.assertNotIn("private credential details", result.message)

    def test_nonzero_connection_failure_is_blocked(self):
        result = self.invoke_with_result(ProcessResult(1, b"Error: ECONNREFUSED at private-host\n"))
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("model channel", result.message)
        self.assertNotIn("private-host", result.message)

    def test_nonzero_unknown_failure_remains_failed(self):
        result = self.invoke_with_result(ProcessResult(7, b"unexpected private diagnostic\n"))
        self.assertEqual(result.status, "FAILED")
        self.assertIn("code 7", result.message)
        self.assertNotIn("private diagnostic", result.message)

    def test_zero_exit_error_event_is_blocked(self):
        result = self.invoke_with_result(ProcessResult(0, failure_event({"name": "APIError", "data": {"statusCode": 401}})))
        self.assertEqual(result.status, "BLOCKED")


class AttemptLogBudgetTests(unittest.TestCase):
    def test_stdout_events_are_separate_from_stderr_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "engine.log"
            command = [sys.executable, "-c", "import sys; print('event'); print('diagnostic',file=sys.stderr)"]
            result = run_process(command, log, timeout=3, max_bytes=1024)
            self.assertEqual(result.output, b"event\n")
            self.assertEqual(result.stderr, b"diagnostic\n")
            self.assertEqual(log.stat().st_size, len(result.output) + len(result.stderr))

    def test_multiple_invocations_share_one_log_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "engine.log"
            command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 600)"]
            first = run_process(command, log, timeout=3, max_bytes=1024)
            second = run_process(command, log, timeout=3, max_bytes=1024)
            self.assertEqual(first.returncode, 0)
            self.assertFalse(first.overflow)
            self.assertTrue(second.overflow)
            self.assertEqual(len(second.output), 424)
            self.assertEqual(log.stat().st_size, 1024)
            with patch.object(subprocess, "Popen") as popen:
                third = run_process(command, log, timeout=3, max_bytes=1024)
            self.assertTrue(third.overflow)
            popen.assert_not_called()
