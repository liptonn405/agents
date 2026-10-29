import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from analyst_agent.contracts import ArchitectArtifact, CoderTask, decode_json


class ArchitectArtifactTests(unittest.TestCase):
    def test_minimal_artifact(self) -> None:
        artifact = ArchitectArtifact.model_validate(
            {
                "schema_version": "1.0",
                "artifact_id": "arch-1",
                "goal": "Сделать профиль",
                "requirements": ["Показать статистику"],
            }
        )
        self.assertEqual(artifact.artifact_id, "arch-1")
        self.assertEqual(artifact.acceptance_criteria, [])

    def test_extra_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ArchitectArtifact.model_validate(
                {
                    "schema_version": "1.0",
                    "artifact_id": "arch-1",
                    "goal": "Сделать профиль",
                    "requirements": ["Показать статистику"],
                    "unknown": True,
                }
            )

    def test_requirements_must_not_be_empty(self) -> None:
        with self.assertRaises(ValidationError):
            ArchitectArtifact.model_validate(
                {
                    "schema_version": "1.0",
                    "artifact_id": "arch-1",
                    "goal": "Сделать профиль",
                    "requirements": [],
                }
            )


class CoderTaskTests(unittest.TestCase):
    def test_task_is_strict(self) -> None:
        task = CoderTask.model_validate(
            {
                "schema_version": "1.0",
                "task_id": "arch-1",
                "workspace_id": "ws-arch-1",
                "attempt": 1,
                "instructions": [
                    {
                        "file": "front/src/miminet_auth.py",
                        "operation": "modify",
                        "target": "user_profile",
                        "requirements": ["Добавить статистику"],
                    }
                ],
                "read_files": ["front/src/miminet_auth.py"],
                "allowed_write_files": ["front/src/miminet_auth.py"],
                "acceptance_criteria": ["Статистика отображается"],
            }
        )
        self.assertEqual(task.instructions[0].operation, "modify")
        self.assertEqual(
            set(task.model_dump()),
            {
                "schema_version",
                "task_id",
                "workspace_id",
                "attempt",
                "instructions",
                "read_files",
                "allowed_write_files",
                "acceptance_criteria",
            },
        )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_json('{"a": 1, "a": 2}')


if __name__ == "__main__":
    unittest.main()
