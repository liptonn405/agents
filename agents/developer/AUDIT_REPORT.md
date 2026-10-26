# Технический аудит CoderAgent — 26 октября 2026

**Итог: READY_FOR_INTEGRATION для проверенного локального профиля.**
**176 тестов прошли, 0 упавших, 0 пропущенных.** Бесплатная `qwen3.5:9b` через
Ollama и настоящий OpenCode 1.19.4 выполнила две задачи в Bubblewrap и отдельный
CLI-запуск через постоянный шлюз. Точные изменения, сохранность исходников и
независимые проверки Python прошли. Текущий запуск использует CUDA на
RTX 4060 Laptop, 8 ГБ VRAM; профиль проверен с контекстом 8192 и output 2048.
Подробности и команды: [LOCAL_MODEL.md](LOCAL_MODEL.md).
Ниже сохранены результаты первоначального аудита (149 тестов); итог подключения
реальной модели приведён в последнем разделе.

## Исходное состояние

- Репозиторий `liptonn405/agents`, исходный HEAD `5131276`.
- До изменений рабочее дерево было чистым, текущая ветка `feature/coder-agent`.
- Создана отдельная ветка `feature/coder-agent-audit`.
- Commit, push, merge, PR в рамках аудита не выполнялись; main не изменялась.
- Python 3.14.4, Pydantic 2.14.1, Bubblewrap 0.11.1.
- Глобально установленного OpenCode не было. Официальный standalone-релиз
  [v1.19.4](https://github.com/anomalyco/opencode/releases/tag/v1.19.4) загружен
  в игнорируемый `.audit-artifacts/opencode-v1.19.4/`, без системной установки.
- SHA-256 архива совпал с `digest` официального GitHub release asset:
  `849bd6b3d959a1d2ef200ed1634a51c79a45ca236aa26665d75debd694805cce`.

Базовая команда `.venv/bin/python -m unittest discover -s agents/developer/tests -v`
в ограниченной внешней среде: `Ran 88`, `OK (skipped=5)` — OS-группа и локальные
сокеты недоступны. Команда
`CODER_REQUIRE_SANDBOX=1 .venv/bin/python scripts/check_repo.py` с разрешёнными
namespaces: **96 прошли, 0 падений/пропусков**, структура PASS.
Прежний результат «85/88, 2 skip, 1 failure» текущим baseline не воспроизведён;
зависимость теста отсутствия OpenCode от наличия Bubblewrap действительно была
в коде и устранена независимо от успешного baseline на данной машине.

## Найденные проблемы и исправления

| Приоритет | Причина | Исправление и подтверждение |
| --- | --- | --- |
| P1 | Настоящий OpenCode проверял пути относительно worktree, а permissions содержали только абсолютные пути. В снимке без `.git` глобальный worktree равен `/`; разрешённые read/edit отклонялись. | Точные aliases `src/app.py`, `workspace/src/app.py`, `/workspace/src/app.py` для одного смонтированного файла. Никаких wildcard-allow. Настоящие read/edit теперь успешны, незаявленный write отклонён. |
| P1 | Без `provider.models.<model>.limit.output` OpenCode мог запросить больше токенов, чем допускает шлюз; фактический запрос получал 400 до модели. | Добавлены доверенные context/output limits, defaults 131072/8192, проверка output<=context; реальный запрос ограничен 8192. Шлюз не ослаблялся. Несогласованная конфигурация возвращает BLOCKED. |
| P1 | IO/corrupt receipt/ошибка begin/claim/report могли выйти исключением из `run()` либо привести к ненадёжной публикации результата. | Структурированный BLOCKED с идентичностью задания, без успешных артефактов при сбое хранения. Receipt создаётся эксклюзивно, прежняя попытка не перезаписывается. Проверены IO/fsync fault injection. |
| P1 | Сохранённый COMPLETED не подтверждал целостность output/patch; отчёт был durable раньше самих артефактов. | Fsync файлов и каталогов, `outputs.json` с SHA-256/size/mode и digest patch; проверка перед публикацией и replay. Порча/удаление/лишний файл → BLOCKED без повторного engine. |
| P1 | Cached результат мог возвращаться после отзыва разрешений/смены репозитория. | Проверка текущей Policy до replay и привязка receipt к fingerprint доверенного repository/policy. Проверены отзыв read permission и смена source root. |
| P1 | Ошибки авторизации/модели/канала классифицировались как общий FAILED; upstream мог отразить ключ в error body. | Известные ошибки → фиксированные безопасные BLOCKED; шлюз не пересылает upstream error body/headers. Проверены реальные CLI 401/404 и fake-secret sentinel. |
| P2 | Синхронный шлюз мог зависнуть до чтения HTTP-заголовков; конкурентный бюджет не был защищён. | Тайм-аут до parsing, до 16 соединений, lock для счётчика, только `n=1`. Проверены idle/partial headers и конкурентные запросы. |
| P2 | Лимит логов применялся отдельно к трём subprocess; stderr мог повредить поток JSON. Настоящий `run --help` пишет в stderr. | Общий бюджет попытки; раздельное чтение stdout/stderr в единый ограниченный журнал; capabilities учитывают оба потока. Проверены реальные help/events и overflow. |
| P2 | Статус старого interrupted run зависел от активности другой попытки того же задания; имелся race с публикацией отчёта. | Lock хранит UUID владельца; inspect сверяет конкретный run и перечитывает report под lock. Проверено двумя настоящими процессами и SIGKILL. |
| P2 | Тест missing OpenCode раньше первым обнаруживал missing Python/Bubblewrap, зависел от окружения. | Fake runtime Python, mock доступного bwrap/non-root UID; отдельные missing bwrap/root/namespace/start failure tests. Unit и настоящая OS-интеграция разделены. |
| P2 | Перенос Python из `/usr/local` в `/usr/bin` ломал поиск stdlib/RPATH. | Реальное расположение Python сохраняется, `/usr/bin/python3` — относительный alias. Prefix под приватными mountpoints отклоняется. Unit layout и настоящий runtime проверены. |
| P2 | Atomic rename, symlink/hardlink внутри реального namespace не покрывались тестами. | Добавлены настоящие отрицательные операции temp+replace, соседний temp, unlink/rename mountpoint, alias RO context/control, chmod/truncate, host-canary и `/proc/1/root`. Все 14 OS-тестов прошли. |

Поведение относительных путей дополнительно сверено с исходниками выбранного
релиза: [read](https://github.com/anomalyco/opencode/blob/v1.19.4/packages/opencode/src/tool/read.ts),
[edit](https://github.com/anomalyco/opencode/blob/v1.19.4/packages/opencode/src/tool/edit.ts).
Для разрешённых инструментов там используется `path.relative(instance.worktree, filePath)`.
CLI/config проверены не только по [документации](https://opencode.ai/docs/cli/),
но и фактическим запуском выбранной версии.

## Полный перечень исправленных/добавленных файлов

Все пути ниже относительно `agents/developer/`.

| Файл | Изменение |
| --- | --- |
| `.gitignore` | Исключён `.audit-artifacts/`: бинарник, архив и локальные журналы не попадают в Git. |
| `CODER_AGENT.md` | Актуализированы ограничения, команды отдельных групп, лимиты, replay/seal, реальный CLI и неподтверждённая LLM. |
| `AUDIT_REPORT.md` | Этот отчёт: исходное состояние, проблемы, доказательства, ограничения и статус. |
| `coder_agent/agent.py` | Безопасные IO-fallback, проверка текущих полномочий до replay, authority binding, публикация sealed output. |
| `coder_agent/reports.py` | Эксклюзивные receipts, проверка metadata, owner-aware state, fsync/seal/replay integrity. |
| `coder_agent/engine.py` | Правильные permissions/token limits, stdout/stderr, общий log budget и безопасная классификация ошибок. |
| `coder_agent/gateway.py` | Очищенные ошибки, ранний timeout, ограниченная параллельность, атомарный бюджет, n=1. |
| `coder_agent/policy.py` | Доверенные context/output token limits с валидацией. |
| `coder_agent/build_runtime.py` | Согласованный Python prefix/stdlib и относительный alias, ранний отказ для скрываемых mountpoints. |
| `examples/config.json` | Явные defaults context/output, согласованные с примером запуска шлюза. |
| `tests/test_engine.py` | Детерминированные unit-предусловия, отдельные missing dependency/root/start tests; OS-тесты перенесены. |
| `tests/test_engine_failures.py` | Новые проверки provider/auth/model/канала, stderr/help, token limits и суммарного журнала. |
| `tests/test_gateway.py` | Добавлены ошибки с credential sentinel, idle/partial headers, n/budget/concurrency и redirects. |
| `tests/test_recovery.py` | Новые IO/corruption/sealing/authority tests, настоящий subprocess lock/SIGKILL/replay. |
| `tests/test_runtime.py` | Новые проверки layout `/usr/local`, скрытого prefix и сохранности существующего destination. |
| `tests/sandbox_support.py` | Общий fake OpenCode и обязательная/пропускаемая проверка namespace capabilities. |
| `tests/test_sandbox_integration.py` | Прежние OS-тесты перенесены, добавлены настоящие atomic/symlink/hardlink/read-only/host probes. |
| `tests/test_real_opencode_integration.py` | Opt-in настоящий CLI с scripted API: edit, forbidden write, auth, missing model. |

`contracts.py`, `validator.py`, `workspace.py`, `sandbox.py`, `bootstrap.py` изучены;
исправления в них не понадобились. Их границы проверены новыми тестами.
Legacy DeveloperAgent/main.py, Analyst/Tester, orchestrator, shared, Makefile и
scripts/check_repo.py не изменены.

## Команды и результаты

| Проверка | Результат |
| --- | --- |
| Baseline, внешняя ограниченная среда: `.venv/bin/python -m unittest discover -s agents/developer/tests -v` | unittest `Ran 88`, `OK (skipped=5)`. |
| Baseline с namespaces: `CODER_REQUIRE_SANDBOX=1 .venv/bin/python scripts/check_repo.py` | 96 passed, 0 failed, 0 skipped; структура PASS. |
| Исправленный unit engine: `.venv/bin/python -m unittest discover -s agents/developer/tests -p test_engine.py -v` | 17 passed. |
| Engine failure/limits: та же команда с `-p test_engine_failures.py` | 11 passed. |
| Recovery: та же команда с `-p test_recovery.py` | 18 passed, включая subprocess/SIGKILL. |
| Runtime layout: та же команда с `-p test_runtime.py` | 3 passed. |
| Настоящий Bubblewrap: `CODER_REQUIRE_SANDBOX=1 .venv/bin/python -m unittest discover -s agents/developer/tests -p test_sandbox_integration.py -v` | 14 passed, 0 failed/skipped. |
| Исправленный полный fake/unit/OS: `CODER_REQUIRE_SANDBOX=1 .venv/bin/python scripts/check_repo.py` | unittest `Ran 145`, `OK (skipped=1)`; пропущен setUpClass opt-in реального CLI (4 сценария без CODER_TEST_OPENCODE). |
| Итоговый полный прогон, команда ниже | **149 passed, 0 failed, 0 skipped**, структура PASS. |
| `git diff --check` | PASS. |

Финальная команда из корня репозитория (нужны разрешённые OS namespaces):

```bash
CODER_REQUIRE_SANDBOX=1 \
CODER_TEST_OPENCODE="$PWD/agents/developer/.audit-artifacts/opencode-v1.19.4/bin/opencode" \
  .venv/bin/python scripts/check_repo.py
```

Финальный прогон занял 25.297 s; журнал сохранён локально в
`.audit-artifacts/full-tests.log`. Падавшие промежуточные реальные эксперименты
не скрывались: сначала выявлены лимит модели, stderr help и неверные path aliases,
затем причины исправлены и полная группа прошла. Тесты не отключались ради успеха.

## Что сделал настоящий OpenCode

`RealOpenCodeTests` создаёт маленький временный Git-репозиторий с `src/app.py`
(`VALUE = 1`) и разрешает изменить только его. Настоящий OpenCode v1.19.4
запускается через тот же OpenCodeEngine/Bubblewrap, что используется в CLI/API.
Локальный API выдаёт корректные streaming chat-completion tool calls:

1. `read('/workspace/src/app.py')`.
2. `edit(... oldString='VALUE = 1', newString='VALUE = 2')`.
3. Финальный JSON `COMPLETED`.

Получен **COMPLETED**, `changed_files=["src/app.py"]`, файл снимка `VALUE = 2`,
оригинальный репозиторий `VALUE = 1`. В другом сценарии API требует
`write('/workspace/undeclared.py')`: OpenCode отклоняет инструментальный вызов,
CoderAgent возвращает **SECURITY_VIOLATION**, посторонний файл не создаётся.
Ответы upstream 401/404 дают **BLOCKED** с безопасной диагностикой; fake private
sentinel отсутствует в Result и engine.log.

**Это настоящий OpenCode с имитатором API, а не выполнение настоящей LLM.**
Рабочие ключи, пользовательская авторизация, внешняя модель и базы Miminet
не использовались. Возможность реальной модели следовать плану пока не доказана.

## Проверенная безопасность и оставшиеся ограничения

Практически подтверждены: непривилегированный UID, отдельный network namespace,
отсутствие исходного `.git`/секретов/Docker socket, закрытый внешний TCP,
выделенный модельный Unix-канал, RO context/control, точные writable mounts,
запрет обхода через symlink/hardlink/temp+rename, сохранность source repository,
ограниченный журнал, завершение detached child при timeout, блокировки между
процессами и отсутствие слепого replay после SIGKILL/повреждения артефактов.

Остаются явные границы:

- Semantic `target` внутри файла не доказывается автоматически. Изменение
  посторонней функции в том же разрешённом файле требует независимой проверки;
  гарантируется файловая граница, а не весь смысл аналитического задания.
- Атомарная замена mountpoint через rename намеренно запрещена ОС. OpenCode
  1.19.4 edit пишет совместимым способом; для другой версии нужна новая проверка.
- Нет общего жёсткого лимита RAM, совокупного CPU или денежных расходов. Для них
  нужны внешние cgroup/job/provider budgets. RLIMIT и token/request limits не
  заменяют эти механизмы.
- Нет полного syscall-аудита и защиты от root/уязвимостей ядра/злонамеренного
  оператора. Каталог runs и runtime — доверенные приватные каталоги.
- Сокет шлюза доверенно выбирает оператор; проверка имени/типа сокета не доказывает
  назначение произвольного переименованного Unix-сервиса.
- Секрет внутри явно разрешённого обычного source-файла не распознаётся по
  содержимому. Его текст может попасть модели/в её вывод. Ошибки upstream очищены,
  host credentials не наследуются; абсолютная очистка произвольного текста модели
  от неизвестных секретов не заявляется.
- Fsync/hash/replay проверены fault injection и порчей файлов. Испытание реальным
  отключением питания/отказом диска не проводилось; нужны локальная POSIX FS и
  корректные гарантии fsync хранилища.

## Совместимость и условие разблокирования

Сохранены `CoderAgent.run`, `status`, Engine Protocol, CLI
`python -m coder_agent --config ... --input ... --output ...`, схема задания 1.0
и прежний отдельный DeveloperAgent/Codex CLI. Все 26 legacy-тестов проходят.
Новые Config-поля имеют defaults. Изменение persistence документировано:
старый unbound receipt или COMPLETED без `outputs.json` не считается проверенным
результатом; нужен ручной осмотр и новый attempt, без автоматической миграции.

На завершение первоначального аудита статус был **BLOCKED**: 149 тестов ещё
не подтверждали реальную модель. Условие разблокирования выполнено проверками ниже.

## Подключение бесплатной локальной модели

Установлена Ollama v0.40.11 из проверенного официального архива, без sudo,
системных служб, платных провайдеров и пользовательских API-ключей. Выбрана
основная кандидатура `qwen3.5:9b` Q4_K_M, reasoning `none`, температура 0.
OpenCode доступен через подготовленный runtime, его внешняя сеть закрыта;
выделенный Unix-шлюз обращается только к локальной Ollama.

В ходе настоящих запусков дополнительно исправлены:

- OpenCode игнорировал `agent.coder.temperature`, пока у custom-модели не была
  объявлена `provider.models.<model>.temperature=true`. Capability включается
  при заданном доверенном `model_temperature`; настоящий API-тест подтверждает 0.
- Сгенерированный заголовок сессии требовал лишнего обращения к модели.
  Если установленный CLI поддерживает `--title`, передаётся фиксированный заголовок.
- Старый JSON `COMPLETED` мог пережить последующие шаги без нового финального
  ответа. Разбор событий теперь сбрасывает такой ответ; добавлены два теста.
- Таймауты gateway/bridge настраиваются для холодного запуска локальной модели,
  а reasoning выбирается по действительным `thinking.values` Ollama.
  Файловые полномочия и запрет внешней сети OpenCode сохранены.

Добавлены `tools/serve_local_ollama.py`, `tools/probe_local_model.py`,
`tools/smoke_local_coder.py`, `tests/test_model_channel.py`,
`tests/test_local_ollama_launcher.py`; расширены engine/gateway/реальные CLI-тесты,
Config и инструкции. `bootstrap.py` и `sandbox.py` получили передачу ограниченного
таймаута модельного канала после первоначального аудита.

| Проверка | Фактический результат |
| --- | --- |
| API `qwen3.5:9b` | Список моделей, обычный ответ, streaming с завершением, настоящий вызов инструмента и ответ на его результат — PASS. |
| Точная правка одного файла | `COMPLETED`, 32,469 s, `VALUE = 1` → `VALUE = 2`, прочие байты сохранены. |
| Python-поручение `clamp()` | `COMPLETED`, 10,312 s; независимая read-only песочница подтвердила 8 числовых случаев, 2 исключения ValueError и сохранность `untouched()`. |
| Replay и отрицательный план | Проверенный результат возвращён повторно; незаявленный файл отклонён с SECURITY_VIOLATION и не создан. |
| Отдельный CLI и постоянный шлюз | Exit 0, `COMPLETED`, точная правка подтверждена; `status()` сообщает FINISHED/COMPLETED. |
| Полная регрессия после исправлений | 176 PASS за 45,287 s, 0 failed/skipped. |

Нынешний `nvidia-smi` подтверждает RTX 4060 Laptop, 8188 MiB и драйвер 595.99.02.
Журнал Ollama подтверждает CUDA; `/api/ps` показывает около 4,96 GiB VRAM для
выбранной модели. Ранняя проверка оборудования и старые API-эксперименты видели
только CPU; они сохранены как исторические, к успешным текущим запускам это
ограничение не относится. Драйвер в рамках задачи не устанавливался и не менялся.

Доказательства находятся в игнорируемом `.audit-artifacts/local-llm/`:
`qwen3.5-9b-fixed-smoke.json`, `qwen3.5-9b-python-smoke.json`,
`cli-result.json`, `cli-verification.json`, `final-hardware.json`,
`final-loaded-models.json`, `final-tests.log`. Доверенные `config.json`/`task.json`
подготовлены для демонстрационного репозитория, шлюз и Ollama запущены обычными
процессами. После перезагрузки их нужно запустить снова.

Статус относится к проверенному профилю и небольшим поручениям. Большой контекст,
произвольные будущие задачи и полная семантическая корректность требуют отдельных
проверок. Исходный репозиторий не изменялся моделью; сам CoderAgent не выполняет
commit/push/merge. Публикация изменений компонента выполняется оператором отдельно.
