# agents

- [AnalystAgent: артефакт Архитектора → задание CoderAgent](agents/analyst/README.md)
- [CoderAgent: JSON-план → изолированный OpenCode](agents/developer/CODER_AGENT.md)
- [Legacy DeveloperAgent: Codex/Issue](agents/developer/README.md)

<!-- TEAM-STRUCTURE-NAVIGATION -->
## Командная разработка

- `agents/analyst/` — анализ задач (разработчик №1)
- `agents/developer/` — автоматическое исправление (разработчик №2)
- `agents/tester/` — автоматическое тестирование (разработчик №3)
- `orchestrator/` — управление конвейером
- `shared/contracts/` — общий формат сообщений между агентами
- `tests/integration/` — проверка взаимодействия компонентов

Подробнее: [структура и правила разработки](docs/REPOSITORY_STRUCTURE.md).
Проверить проект: `make check`.
