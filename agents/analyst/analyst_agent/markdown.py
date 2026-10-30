"""Markdown adapter for the AnalystAgent.

The Architect may send a human-readable Markdown document instead of JSON.
The analyst still validates and hands the Coder a strict task, but the default
human-facing artifact is Markdown with an embedded JSON block.
"""
from __future__ import annotations

import hashlib
import json
import re

from .contracts import AnalystOutput, ArchitectArtifact, IssueContext

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_ARTIFACT_ID_RE = re.compile(
    r"^\s*(?:artifact[\s_-]*id|artifact|id|идентификатор)\s*[:\-]\s*(\S+)\s*$",
    re.IGNORECASE,
)

_SECTION_ALIASES: dict[str, set[str]] = {
    "goal": {"goal", "цель", "задача", "цель задачи", "назначение", "суть"},
    "requirements": {
        "requirements",
        "requirement",
        "требования",
        "требование",
        "функциональные требования",
        "что сделать",
    },
    "acceptance_criteria": {
        "acceptance criteria",
        "acceptance",
        "критерии готовности",
        "критерии приёмки",
        "критерии приемки",
        "критерии",
        "definition of done",
    },
    "constraints": {"constraints", "constraint", "ограничения", "ограничение"},
    "non_goals": {
        "non-goals",
        "non goals",
        "вне scope",
        "вне рамок",
        "не входит",
        "не входит в задачу",
        "не цели",
    },
    "affected_areas": {
        "affected areas",
        "компоненты",
        "затронутые компоненты",
        "затрагиваемые области",
        "области",
    },
    "context": {"context", "контекст", "дополнительно", "заметки", "примечания"},
    "issue": {"issue", "github issue", "задача github"},
}


def _normalize_heading(value: str) -> str:
    value = value.strip().lower().strip("#:*- ")
    value = re.sub(r"\s+", " ", value)
    return value


def _match_section(title: str) -> str | None:
    normalized = _normalize_heading(title)
    for key, aliases in _SECTION_ALIASES.items():
        if normalized in aliases:
            return key
    return None


def _sanitize_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip(".-")
    cleaned = cleaned[:80]
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"arch-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:8]}"
    return cleaned


def parse_architect_markdown(text: str) -> ArchitectArtifact:
    """Parse a free-form Architect Markdown document into the strict contract."""
    sections: dict[str, list[str]] = {key: [] for key in _SECTION_ALIASES}
    sections["preamble"] = []
    headings: list[str] = []
    all_bullets: list[str] = []
    artifact_id: str | None = None
    current: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(2).strip()
            headings.append(title)
            current = _match_section(title)
            continue

        identifier = _ARTIFACT_ID_RE.match(line)
        if identifier and artifact_id is None:
            artifact_id = identifier.group(1).strip("`'\"")
            continue

        content = line.strip()
        if not content:
            continue
        bullet = _BULLET_RE.match(content)
        value = bullet.group(1).strip() if bullet else content
        if bullet:
            all_bullets.append(value)
        if current:
            sections[current].append(value)
        else:
            sections["preamble"].append(value)

    goal_parts = sections["goal"]
    goal = " ".join(goal_parts).strip()
    if not goal and headings:
        goal = headings[0].strip()
    if not goal and sections["preamble"]:
        goal = sections["preamble"][0].strip()
    if not goal:
        goal = "Задача без явной цели"

    requirements = sections["requirements"] or all_bullets
    if not requirements:
        # Последний шанс: режем свободный текст на предложения.
        requirements = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", text)
            if len(sentence.strip()) >= 20
        ][:20]
    if not requirements:
        raise ValueError(
            "В Markdown-артефакте не найдено требований. "
            "Добавьте раздел «Требования» со списком."
        )

    issue_lines = sections["issue"]
    issue = None
    if issue_lines:
        issue = IssueContext(
            title=issue_lines[0], body="\n".join(issue_lines[1:])
        )

    payload = {
        "schema_version": "1.0",
        "artifact_id": _sanitize_identifier(
            artifact_id or f"arch-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
        ),
        "goal": goal,
        "requirements": requirements,
        "acceptance_criteria": sections["acceptance_criteria"],
        "constraints": sections["constraints"],
        "affected_areas": sections["affected_areas"],
        "non_goals": sections["non_goals"],
        "context": " ".join(sections["context"]).strip() or None,
        "issue": issue.model_dump() if issue else None,
    }
    return ArchitectArtifact.model_validate(payload)


def _bullets(values: list[str]) -> str:
    if not values:
        return "—\n"
    return "\n".join(f"- {value}" for value in values) + "\n"


def render_analyst_markdown(output: AnalystOutput) -> str:
    """Render a human-readable Markdown artifact with an embedded Coder task."""
    report = output.report
    task = output.task
    lines: list[str] = []
    lines.append("# Артефакт AnalystAgent\n")
    lines.append(f"- Артефакт: `{report.artifact_id}`")
    lines.append(f"- Источник Архитектора: `{report.source_artifact_id}`")
    lines.append(f"- Статус: `{report.status}`")
    lines.append(f"- Цель: {report.goal}\n")

    lines.append("## Область работ\n")
    lines.append("### Входит\n")
    lines.append(_bullets(report.scope_in))
    lines.append("### Не входит\n")
    lines.append(_bullets(report.scope_out))

    lines.append("## Затронутые компоненты\n")
    lines.append(_bullets(report.affected_components))

    lines.append("## Трассировка требований\n")
    lines.append("| # | Требование | Файлы |")
    lines.append("| --- | --- | --- |")
    for link in report.traceability:
        files = ", ".join(f"`{path}`" for path in link.files) or "—"
        requirement = link.requirement.replace("|", "\\|")
        lines.append(f"| {link.requirement_index} | {requirement} | {files} |")
    lines.append("")

    if report.status == "READY":
        lines.append("## Задание Кодеру\n")
        for item in task.instructions:
            lines.append(f"### `{item.file}` — {item.operation}\n")
            lines.append(f"**Цель:** {item.target}\n")
            lines.append("**Требования:**\n")
            lines.append(_bullets(item.requirements))
            lines.append("")
        lines.append("### Файлы для чтения\n")
        lines.append(_bullets(task.read_files))
        lines.append("### Файлы для записи\n")
        lines.append(_bullets(task.allowed_write_files))
    else:
        lines.append("## Задание Кодеру\n")
        lines.append(
            "Задание не выдано: есть блокирующие вопросы к Архитектору.\n"
        )

    lines.append("## Критерии готовности\n")
    lines.append(_bullets(task.acceptance_criteria))

    lines.append("## Тест-план\n")
    lines.append(_bullets(report.test_plan))

    lines.append("## Риски\n")
    lines.append(_bullets(report.risks))

    lines.append("## Открытые вопросы\n")
    lines.append(_bullets(report.open_questions))

    if report.blocking_questions:
        lines.append("## Блокирующие вопросы\n")
        lines.append(_bullets(report.blocking_questions))

    if report.status == "READY":
        lines.append("## Машиночитаемое задание (CoderTask)\n")
        lines.append("```json")
        lines.append(
            json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )
        lines.append("```")

    return "\n".join(lines) + "\n"
