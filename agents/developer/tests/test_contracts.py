import json
import unittest

from pydantic import ValidationError

from coder_support import CoderCase, task
from coder_agent.contracts import Task, decode_json


class ContractTests(unittest.TestCase):
    def test_valid_contract(self):
        self.assertEqual(Task.model_validate(task()).task_id, "543")

    def test_required_fields_strict_types_and_schema(self):
        for field in task():
            value = task()
            del value[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Task.model_validate(value)
        for field, item in [("attempt", True), ("attempt", "1"), ("attempt", 0),
                            ("schema_version", "2.0"), ("task_id", 543),
                            ("instructions", []), ("acceptance_criteria", []), ("extra", "bad")]:
            value = task()
            value[field] = item
            with self.subTest(field=field, item=item), self.assertRaises(ValidationError):
                Task.model_validate(value)

    def test_unknown_operation_and_empty_requirements(self):
        for field, value in [("operation", "delete"), ("requirements", []), ("target", " ")]:
            data = task()
            data["instructions"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Task.model_validate(data)

    def test_ambiguous_json(self):
        for value in ['{"a":1,"a":2}', '{"a":NaN}', "x" * 65537]:
            with self.assertRaises(ValueError):
                decode_json(value)


class InvalidInputTests(CoderCase):
    def test_incomplete_input_does_not_start_engine(self):
        del self.payload["task_id"]
        result = self.assert_rejected("INVALID_INPUT")
        self.assertIsNone(result.task_id)
        self.assertTrue((self.workspace(result).parent / "report.json").exists())

    def test_invalid_operation_does_not_start_engine(self):
        self.payload["instructions"][0]["operation"] = "delete"
        self.assert_rejected("INVALID_INPUT")

    def test_invalid_json(self):
        result = self.agent.run('{"schema_version":')
        self.assertEqual(result.status, "INVALID_INPUT")
        self.assertEqual(self.fake.calls, 0)

    def test_python_and_json_interfaces(self):
        result = self.agent.run(json.dumps(self.payload))
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(self.agent.run(Task.model_validate(self.payload)), result)
