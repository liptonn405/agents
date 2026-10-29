# AnalystAgent

**Ответственный:** разработчик №1.

Агент получает артефакт от ArchitectAgent, проверяет его выполнимость по
реальному коду Miminet и формирует строгое задание для CoderAgent. Код агент
не изменяет, тесты не запускает и ничего не коммитит.

## Пайплайн

```text
ArchitectArtifact
      ↓
AnalystAgent: Code RAG + проверка требований и путей
      ↓
CoderTask 1.0  +  AnalystReport (sidecar)
      ↓
CoderAgent
```

`CoderTask` совместим со строгой схемой `agents/developer/coder_agent/contracts.py`
(версия `1.0`). `AnalystReport` хранит трассировку требований, тест-план, риски
и открытые вопросы; он не передаётся CoderAgent как часть задания.

## Входной артефакт

Пример: `examples/architect_artifact.json`.

```json
{
  "schema_version": "1.0",
  "artifact_id": "arch-001",
  "goal": "Что нужно сделать",
  "requirements": ["Проверяемое требование"],
  "acceptance_criteria": ["Критерий готовности"],
  "constraints": ["Ограничение"],
  "affected_areas": ["Затронутая область"],
  "non_goals": ["Что не входит в задачу"],
  "context": "Необязательные заметки",
  "issue": {"title": "Заголовок", "body": "Описание"}
}
```

Обязательные поля: `schema_version`, `artifact_id`, `goal`, `requirements`.
Остальные поля необязательны. Лишние поля и нестрогие типы отклоняются.

## Что делает агент

1. Проверяет схему артефакта Архитектора.
2. Индексирует `front/src` и `back/src` репозитория Miminet.
3. Ищет код через гибридный Code RAG: хеш-эмбеддинги + IDF + совпадения путей
   + русско-английские подсказки + определения явно упомянутых классов/функций.
4. Формирует инструкции для CoderAgent через LLM или офлайн-эвристику.
5. Проверяет, что:
   - `modify` применяется только к найденным существующим файлам;
   - `create` не перезаписывает файл, а родительский каталог существует;
   - пути относительные, нормализованные и не выходят за репозиторий;
   - секреты, `.env`, `.git`, `.ssh` и служебные файлы запрещены;
   - каждое требование Архитектора покрыто хотя бы одной инструкцией.
6. Собирает `CoderTask` и `AnalystReport`.

Если проверка не проходит, задание Кодеру не выдаётся: CLI завершается с кодом
`1` и структурированной ошибкой.

## Установка

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ./agents/analyst
```

## Запуск

```bash
.venv/bin/python -m analyst_agent \
  --artifact agents/analyst/examples/architect_artifact.json \
  --repo ../miminet \
  --output /tmp/coder-task.json \
  --report /tmp/analyst-report.json \
  --offline
```

Без `--output` задание печатается в stdout. `--bundle` выводит task и report
одним JSON. `--offline` не требует API-ключа и использует детерминированный план.

## Настройки LLM

Секреты можно положить в `agents/analyst/.env` (файл в `.gitignore`):

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=deepseek/deepseek-v4.1-flash
```

Переменные окружения имеют приоритет над `.env`. Путь к `.env` можно задать
через `ANALYST_ENV_FILE`. Поиск репозитория: `--repo`, затем `ANALYST_REPO`,
затем `MIMINET_REPO`, затем соседний `../miminet`.

## Тесты

```bash
python3 -m unittest discover -s agents/analyst/tests -v
```

## Границы ответственности

- **Делает:** анализ артефакта Архитектора, поиск по коду, проверку путей,
  формирование задания Кодеру, трассировку и тест-план.
- **Не делает:** не пишет код, не запускает тесты Miminet, не делает ревью,
  не коммитит, не пушит и не деплоит.
