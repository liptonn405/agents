"""AnalystAgent: Architect artifact -> validated Coder task + report."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath

from openai import OpenAI
from pydantic import ValidationError

from .contracts import (
    AnalystDraft,
    AnalystOutput,
    AnalystReport,
    ArchitectArtifact,
    CoderTask,
    DraftInstruction,
    Instruction,
    TraceLink,
)
from .env import load_default_env
from .retrieval import CodeIndex, ContextSnippet

logger = logging.getLogger("analyst_agent")

SYSTEM_PROMPT = """Ты — AnalystAgent в конвейере разработки сайта «Миминет».
На вход ты получаешь артефакт Архитектора и фрагменты реального кода, найденные через Code RAG.
Твоя задача — проверить выполнимость требований и подготовить точное задание для CoderAgent.

Ты НЕ пишешь код, НЕ меняешь файлы и НЕ запускаешь тесты.

Верни СТРОГО один JSON-объект без markdown и пояснений:
{
  "instructions": [
    {
      "file": "относительный/путь.py",
      "operation": "modify",
      "target": "что именно изменить",
      "requirements": ["конкретное требование к изменению"],
      "requirement_refs": [0]
    }
  ],
  "acceptance_criteria": ["проверяемый критерий готовности"],
  "test_plan": ["что проверить Тестировщику"],
  "risks": ["риск или ограничение"],
  "open_questions": ["вопрос, если данных не хватает"]
}

Правила:
1. Для operation="modify" используй ТОЛЬКО файлы из списка найденных файлов.
2. Для operation="create" файла ещё не должно существовать; родительский каталог должен быть реальным.
3. requirements — массив ТЕКСТОВЫХ строк с описанием изменений, а не массив чисел.
4. requirement_refs — массив целых индексов требований Архитектора (с нуля). Каждое требование должно быть покрыто хотя бы одним instruction.
5. Не выдумывай поля моделей, методы, маршруты и шаблоны. Если чего-то нет в коде — вынеси это в open_questions.
6. Сохраняй стиль проекта: Flask, SQLAlchemy, Jinja2.
7. Не предлагай изменять секреты, .env, .git, .ssh, конфигурации OpenCode и служебные файлы.
8. target и requirements должны быть конкретными и проверяемыми.
"""


class PlanningError(RuntimeError):
    """The Architect artifact cannot be safely converted into a Coder task."""


class AnalystAgent:
    """Read-only planner that validates an Architect artifact against the repo."""

    CREATE_SUFFIXES = {
        ".py", ".html", ".js", ".sql", ".json", ".toml", ".yml", ".yaml", ".css", ".md"
    }
    PROTECTED_NAMES = {
        ".git", ".ssh", ".aws", ".env", ".opencode", "node_modules", "AGENTS.md",
        "id_rsa", "id_ed25519", ".npmrc", ".pypirc",
    }
    PROTECTED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}

    def __init__(
        self,
        repo_path: str | Path | None = None,
        *,
        top_k: int = 5,
        offline: bool = False,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_attempts: int = 2,
        llm: OpenAI | None = None,
    ) -> None:
        load_default_env()
        self.offline = offline
        self.top_k = max(1, min(top_k, 100))
        self.max_attempts = max(1, max_attempts)
        self.repo_path = Path(repo_path).expanduser().resolve() if repo_path else None
        self.index = CodeIndex(self.repo_path)

        self.llm = llm
        if not self.offline and self.llm is None:
            key = api_key or os.getenv("OPENAI_API_KEY")
            if not key:
                raise RuntimeError(
                    "Не задан OPENAI_API_KEY. Передайте ключ, создайте "
                    "agents/analyst/.env или запустите с --offline."
                )
            self.base_url = (
                base_url
                or os.getenv("OPENAI_BASE_URL")
                or "https://api.deepseek.com/v1"
            )
            self.model = model or os.getenv("OPENAI_MODEL") or "deepseek-chat"
            self.llm = OpenAI(
                api_key=key,
                base_url=self.base_url,
                timeout=60.0,
                max_retries=2,
            )
        else:
            self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "")
            self.model = model or os.getenv("OPENAI_MODEL", "")

    def analyze(
        self,
        artifact: ArchitectArtifact,
        *,
        task_id: str | None = None,
        workspace_id: str | None = None,
        attempt: int = 1,
    ) -> AnalystOutput:
        """Run Code RAG, plan, validate paths and build the handoff artifacts."""
        snippets = self.index.retrieve(artifact.retrieval_text, self.top_k)
        if not snippets:
            raise PlanningError("Code RAG не нашёл ни одного файла для артефакта.")

        draft = (
            self._offline_draft(artifact, snippets)
            if self.offline
            else self._request_draft(artifact, snippets)
        )
        return self._build_output(
            artifact,
            draft,
            snippets,
            task_id=task_id,
            workspace_id=workspace_id,
            attempt=attempt,
        )

    def _offline_draft(
        self, artifact: ArchitectArtifact, snippets: list[ContextSnippet]
    ) -> AnalystDraft:
        """Deterministic fallback used by tests and keyless demonstrations."""
        files: list[str] = []
        for snippet in snippets:
            if snippet.file_path not in files:
                files.append(snippet.file_path)
            if len(files) >= 3:
                break
        if not files:
            raise PlanningError("Нет файлов для офлайн-плана.")

        per_file: dict[str, list[int]] = {path: [] for path in files}
        for index, requirement in enumerate(artifact.requirements):
            expanded = (
                f"{requirement} {self.index.embedder._expand_query(requirement)}"
            )
            requirement_features = self.index._lexical_features(expanded)
            best_path = files[0]
            best_score = -1.0
            for path in files:
                snippet_content = "\n".join(
                    snippet.content for snippet in snippets if snippet.file_path == path
                )
                path_words = (
                    path.replace("/", " ").replace(".", " ").replace("_", " ")
                )
                score = len(
                    requirement_features
                    & self.index._lexical_features(snippet_content)
                ) + 2 * len(
                    requirement_features
                    & self.index._lexical_features(path_words)
                )
                if score > best_score:
                    best_score = score
                    best_path = path
            per_file[best_path].append(index)

        instructions = [
            DraftInstruction(
                file=path,
                operation="modify",
                target=f"Реализовать требования для {path}",
                requirements=[
                    artifact.requirements[index] for index in indices
                ],
                requirement_refs=indices,
            )
            for path, indices in per_file.items()
            if indices
        ]
        acceptance = artifact.acceptance_criteria or [
            f"Выполнено требование: {requirement}"
            for requirement in artifact.requirements
        ]
        return AnalystDraft(
            instructions=instructions,
            acceptance_criteria=acceptance,
            test_plan=[
                "Проверить затронутые сценарии вручную.",
                "Убедиться, что существующее поведение не изменилось.",
            ],
            risks=[
                "Офлайн-режим использует эвристики и не проверяет бизнес-логику."
            ],
            open_questions=[],
        )

    def _request_draft(
        self, artifact: ArchitectArtifact, snippets: list[ContextSnippet]
    ) -> AnalystDraft:
        if self.llm is None:
            raise PlanningError("LLM-клиент не инициализирован.")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_prompt(artifact, snippets)},
        ]
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            logger.info(
                "Запрос к LLM (попытка %d/%d, модель %s)",
                attempt,
                self.max_attempts,
                self.model,
            )
            try:
                response = self.llm.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=4000,
                )
            except Exception as exc:
                raise PlanningError(
                    f"Ошибка вызова LLM (base_url={self.base_url}, "
                    f"model={self.model}): {exc}"
                ) from exc

            content = (response.choices[0].message.content or "").strip()
            content = self._strip_markdown(content)
            try:
                return self._parse_draft(content, artifact)
            except (ValidationError, ValueError, TypeError) as exc:
                last_error = exc
                logger.warning("Ответ LLM не прошёл валидацию: %s", exc)
                messages.extend(
                    [
                        {"role": "assistant", "content": content or "{}"},
                        {
                            "role": "user",
                            "content": (
                                "Предыдущий ответ не прошёл валидацию Pydantic:\n"
                                f"{str(exc)[:1500]}\n\n"
                                "Верни исправленный JSON строго по схеме. "
                                "requirements — текстовые строки, а не числа; "
                                "числа допустимы только в requirement_refs. "
                                "Без markdown и дополнительных полей."
                            ),
                        },
                    ]
                )

        raise PlanningError(
            f"Модель не вернула валидный JSON после {self.max_attempts} "
            f"попыток: {last_error}"
        )

    def _parse_draft(self, content: str, artifact: ArchitectArtifact) -> AnalystDraft:
        """Parse the draft and repair the common integer-in-requirements mistake."""
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("LLM вернула не JSON-объект.")

        for item in payload.get("instructions", []):
            if not isinstance(item, dict):
                continue
            requirements = item.get("requirements")
            refs = item.get("requirement_refs")
            if (
                isinstance(requirements, list)
                and requirements
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in requirements
                )
                and isinstance(refs, list)
            ):
                repaired = [
                    artifact.requirements[ref]
                    for ref in refs
                    if isinstance(ref, int)
                    and not isinstance(ref, bool)
                    and 0 <= ref < len(artifact.requirements)
                ]
                if repaired:
                    item["requirements"] = repaired

        return AnalystDraft.model_validate(payload)

    def _build_user_prompt(
        self, artifact: ArchitectArtifact, snippets: list[ContextSnippet]
    ) -> str:
        blocks = []
        for snippet in snippets:
            blocks.append(
                f"### FILE: {snippet.file_path} "
                f"({snippet.reason or 'без указания строк'}, "
                f"релевантность {snippet.relevance:.2f})\n"
                f"```text\n{self._truncate(snippet.content)}\n```"
            )
        return (
            "## Артефакт Архитектора\n\n"
            f"{json.dumps(artifact.model_dump(mode='json'), ensure_ascii=False, indent=2)}\n\n"
            "## Найденные файлы\n"
            + "\n".join(f"- {snippet.file_path}" for snippet in snippets)
            + "\n\n## Фрагменты кода\n\n"
            + "\n\n".join(blocks)
            + "\n\nСформируй JSON-задание для CoderAgent."
        )

    def _build_output(
        self,
        artifact: ArchitectArtifact,
        draft: AnalystDraft,
        snippets: list[ContextSnippet],
        *,
        task_id: str | None,
        workspace_id: str | None,
        attempt: int,
    ) -> AnalystOutput:
        allowed_modify = {snippet.file_path for snippet in snippets}
        instructions: list[Instruction] = []
        files_by_requirement: dict[int, set[str]] = {
            index: set() for index in range(len(artifact.requirements))
        }

        # LLM иногда дробит один файл на несколько инструкций.  Схема CoderAgent
        # ожидает один target на файл, поэтому объединяем совместимые записи.
        merged: dict[str, dict[str, object]] = {}
        order: list[str] = []
        for item in draft.instructions:
            path = self._normalize_path(item.file)
            if path not in merged:
                merged[path] = {
                    "operation": item.operation,
                    "target": item.target,
                    "requirements": list(item.requirements),
                    "refs": list(item.requirement_refs),
                }
                order.append(path)
                continue

            current = merged[path]
            if current["operation"] != item.operation:
                raise PlanningError(
                    f"Конфликт operation для одного файла: {path}"
                )
            current["requirements"] = list(  # type: ignore[union-attr]
                dict.fromkeys([*current["requirements"], *item.requirements])
            )
            current["refs"] = list(  # type: ignore[union-attr]
                dict.fromkeys([*current["refs"], *item.requirement_refs])
            )
            if current["target"] != item.target:
                combined = f"{current['target']}; {item.target}"
                current["target"] = combined[:4096]

        for path in order:
            item = merged[path]
            operation = item["operation"]
            if operation == "modify":
                if path not in allowed_modify:
                    raise PlanningError(
                        f"modify для файла вне результатов поиска: {path}"
                    )
                self._validate_existing(path)
            else:
                self._validate_create(path)

            for ref in item["refs"]:  # type: ignore[union-attr]
                if ref < 0 or ref >= len(artifact.requirements):
                    raise PlanningError(
                        f"requirement_refs содержит индекс вне диапазона: {ref}"
                    )
                files_by_requirement[ref].add(path)

            instructions.append(
                Instruction(
                    file=path,
                    operation=operation,
                    target=item["target"],
                    requirements=item["requirements"],
                )
            )

        missing = [
            index for index, files in files_by_requirement.items() if not files
        ]
        if missing:
            raise PlanningError(
                "Не покрыты требования Архитектора: "
                + ", ".join(str(index) for index in missing)
            )

        acceptance = draft.acceptance_criteria or artifact.acceptance_criteria
        if not acceptance:
            acceptance = [
                f"Выполнено требование: {requirement}"
                for requirement in artifact.requirements
            ]

        allowed_write_files = list(dict.fromkeys(item.file for item in instructions))
        read_files = list(
            dict.fromkeys(
                item.file for item in instructions if item.operation == "modify"
            )
        )
        resolved_task_id = self._task_id(task_id or artifact.artifact_id)
        resolved_workspace_id = self._workspace_id(
            workspace_id or f"ws-{resolved_task_id}"
        )

        task = CoderTask(
            task_id=resolved_task_id,
            workspace_id=resolved_workspace_id,
            attempt=attempt,
            instructions=instructions,
            read_files=read_files,
            allowed_write_files=allowed_write_files,
            acceptance_criteria=acceptance,
        )

        traceability = [
            TraceLink(
                requirement_index=index,
                requirement=requirement,
                files=sorted(files_by_requirement[index]),
            )
            for index, requirement in enumerate(artifact.requirements)
        ]
        report = AnalystReport(
            artifact_id=self._artifact_id(f"analyst-{resolved_task_id}"),
            source_artifact_id=artifact.artifact_id,
            status="READY",
            goal=artifact.goal,
            scope_in=list(artifact.requirements),
            scope_out=list(artifact.non_goals),
            affected_components=(
                list(artifact.affected_areas)
                or list(dict.fromkeys(snippet.file_path for snippet in snippets))
            ),
            test_plan=draft.test_plan
            or [
                "Проверить заявленные acceptance_criteria.",
                "Проверить отсутствие регрессий в затронутых сценариях.",
            ],
            risks=draft.risks
            or ["План основан на ограниченном контексте Code RAG."],
            open_questions=draft.open_questions,
            traceability=traceability,
            context_files=list(dict.fromkeys(s.file_path for s in snippets)),
        )
        return AnalystOutput(task=task, report=report)

    def _validate_existing(self, path: str) -> None:
        target = self._repo_file(path)
        if target.is_symlink():
            raise PlanningError(f"Symlink запрещён: {path}")
        if not target.is_file():
            raise PlanningError(f"modify требует существующий файл: {path}")

    def _validate_create(self, path: str) -> None:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in self.CREATE_SUFFIXES:
            raise PlanningError(f"create с запрещённым расширением: {path}")
        target = self._repo_file(path)
        if target.exists() or target.is_symlink():
            raise PlanningError(f"create требует отсутствующий файл: {path}")
        if not target.parent.is_dir():
            raise PlanningError(f"Нет родительского каталога для create: {path}")

    def _repo_file(self, path: str) -> Path:
        repo = self.index.repo_path.resolve()
        target = (repo / path).resolve()
        if target == repo or repo not in target.parents:
            raise PlanningError(f"Путь выходит за пределы репозитория: {path}")
        return target

    def _normalize_path(self, raw: str) -> str:
        path = raw.strip()
        while path.startswith("./"):
            path = path[2:]
        if not path or len(path) > 240:
            raise PlanningError(f"Некорректная длина пути: {raw!r}")
        if path.startswith("/") or "\\" in path:
            raise PlanningError(f"Путь должен быть относительным POSIX: {raw!r}")
        if path != str(PurePosixPath(path)):
            raise PlanningError(f"Путь должен быть нормализован: {raw!r}")
        parts = PurePosixPath(path).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise PlanningError(f"Недопустимый сегмент пути: {raw!r}")
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            raise PlanningError(f"Недопустимые символы в пути: {raw!r}")
        for part in parts:
            lowered = part.lower()
            if lowered in self.PROTECTED_NAMES or lowered.startswith(".env"):
                raise PlanningError(f"Защищённый путь: {path}")
            if PurePosixPath(part).suffix.lower() in self.PROTECTED_SUFFIXES:
                raise PlanningError(f"Защищённый файл: {path}")
        return path

    @staticmethod
    def _task_id(value: str) -> str:
        candidate = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", candidate):
            raise PlanningError(f"Некорректный task_id: {value!r}")
        return candidate

    @staticmethod
    def _workspace_id(value: str) -> str:
        candidate = value.strip()[:80]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", candidate):
            raise PlanningError(f"Некорректный workspace_id: {value!r}")
        return candidate

    @staticmethod
    def _artifact_id(value: str) -> str:
        candidate = value.strip()[:80]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", candidate):
            raise PlanningError(f"Некорректный artifact_id: {value!r}")
        return candidate

    @staticmethod
    def _truncate(text: str, limit: int = 1800) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n# ... фрагмент обрезан ..."

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else text
