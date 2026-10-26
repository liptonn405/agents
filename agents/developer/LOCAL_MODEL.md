# Бесплатная локальная модель для CoderAgent

Проверка от 26 октября 2026 года. Ollama установлена в пользовательский каталог,
без sudo, systemd, платных API и изменений драйверов. Подключение сохраняет
существующую цепочку: `CoderAgent → OpenCode → Unix-шлюз → Ollama`.

**READY_FOR_INTEGRATION для проверенного профиля 8192 токена и небольших задач.**
Выбрана `qwen3.5:9b`, reasoning `none`, temperature `0`.
После исправления настройки температуры настоящий CoderAgent успешно изменил
проверочный файл за 32,469 s; исходник сохранён, точные байты результата,
replay и отказ для незаявленного файла проверены. Содержательное Python-поручение
также выполнено за 10,312 s и прошло независимые проверки поведения. Отдельный
обычный CLI через постоянный шлюз завершился с кодом 0 и `COMPLETED`.
Успешные финальные запуски используют NVIDIA RTX 4060 Laptop через CUDA.
Успех небольшого smoke не подтверждает качество всех будущих поручений.

## Фактическое оборудование

Финальная проверка `nvidia-smi` вне внешней песочницы обнаружила **NVIDIA RTX
4060 Laptop, 8188 MiB VRAM, рабочий драйвер 595.99.02**. Журнал запуска Ollama
подтверждает CUDA и эту GPU. У загруженной `qwen3.5:9b` Q4_K_M с контекстом 8192
API сообщает `size_vram=5325266615` байт, около **4,96 GiB VRAM**, и общий размер
`6260512518` байт, около 5,83 GiB; часть памяти модели размещена вне VRAM.
Успешные финальные smoke и CLI выполнялись с GPU.

Оперативная память — около 14,8 GiB; при финальной проверке `MemAvailable` около
9,125 GB (8,50 GiB). Swap — 4 GiB, свободно около 3,4 GB; свободное место на
диске — 247158820864 байта, около 230 GiB. Доказательства сохранены в
`final-hardware.json`, `final-loaded-models.json` и `ollama.log`.

Первоначальная проверка действительно не видела NVIDIA: отсутствовали
`nvidia-smi` и `/dev/nvidia*`, а PCI показывал Intel UHD/i915. Тогда доступно
было около 6 GiB RAM и 251 GiB диска; начальные API-пробы сообщили VRAM 0 и
работали на CPU. Это исторический результат `hardware.json`, а не описание
финальной среды. Причина различия между проверками не устанавливалась.

## Установленные компоненты и границы

Все команды ниже выполняются из `/home/adam/agents-team`:

```bash
cd /home/adam/agents-team
LOCAL_LLM_ROOT="$PWD/agents/developer/.audit-artifacts/local-llm"
LOCAL_OPENCODE="$PWD/agents/developer/.audit-artifacts/opencode-v1.19.4/bin/opencode"
```

- Ollama v0.40.11: `$LOCAL_LLM_ROOT/ollama/bin/ollama`; официальный Linux-архив
  установлен вручную и проверен по SHA-256 релиза.
- OpenCode v1.19.4: `$LOCAL_OPENCODE`; standalone-бинарник проверен по SHA-256.
- Отдельный runtime OpenCode/Python: `$LOCAL_LLM_ROOT/runtime`.
- Веса моделей: `$LOCAL_LLM_ROOT/models`; служебное состояние Ollama:
  `$LOCAL_LLM_ROOT/state`.

Каталог `.audit-artifacts/` исключён из Git. Launcher запускает Ollama в отдельном
mount/PID/user namespace: настоящий домашний каталог, его ключи, исходный проект
и Docker socket не монтируются. Значение HOME сохраняется, но соответствующий
путь внутри namespace приватный. Модельный процесс имеет сеть для загрузки весов,
слушает только `127.0.0.1:11434`, работает с `OLLAMA_NO_CLOUD=1`. Это отключение
облачных функций Ollama; загрузка весов всё равно требует интернета.

OpenCode сохраняет собственный закрытый network namespace и получает только
выделенный Unix-сокет шлюза. Платные провайдеры и настоящие API-ключи не нужны.
Код, сгенерированный моделью, внутри процесса Ollama не исполняется.

## Запуск и проверка API

В первом терминале, в переднем плане:

```bash
.venv/bin/python agents/developer/tools/serve_local_ollama.py \
  --root "$LOCAL_LLM_ROOT" --context 8192
```

Во втором терминале повторите определения `LOCAL_LLM_ROOT` и `LOCAL_OPENCODE`,
затем проверьте основную модель:

```bash
.venv/bin/python agents/developer/tools/probe_local_model.py \
  --model qwen3.5:9b --max-tokens 2048 \
  --output "$LOCAL_LLM_ROOT/qwen3.5-9b-probe.json"
```

Проверка использует локальный `http://127.0.0.1:11434/v1`: список моделей,
обычный ответ, streaming с `stop` и `[DONE]`, настоящий вызов
`record_value(value=7)` и последующий ответ на результат инструмента.
Модели `qwen3.5:9b`, `qwen2.5-coder:7b` и `qwen3:4b` уже скачаны. Для новой
установки добавьте `--pull`. API-проба не запускает OpenCode и не заменяет
проверку редактирования. Явный nonthinking-тег
`qwen3:4b-instruct-2507-q4_K_M` скачан и сохранён как резервный непроверенный кандидат,
а не как выбранная модель.

## Настоящий CoderAgent и OpenCode

При работающей Ollama:

```bash
.venv/bin/python agents/developer/tools/smoke_local_coder.py \
  --root "$LOCAL_LLM_ROOT" --opencode "$LOCAL_OPENCODE" \
  --model qwen3.5:9b \
  --output "$LOCAL_LLM_ROOT/coder-live-smoke.json"
```

Скрипт создаёт новый маленький Git-репозиторий без commit, отдельный снимок и
временный шлюз. Настоящая модель должна прочитать `app.py` и изменить только
`VALUE = 1` на `VALUE = 2`, сохранив функцию `untouched()` побайтно. Успех требует
фактического изменения снимка и сохранности исходника. После успеха проверяются
replay результата и отказ для незаявленного файла. Последний отказ проверяет
валидацию плана до модели; он не означает отдельную adversarial-проверку LLM.

Для отдельного Python-поручения доступен `--scenario python`: модель реализует
`clamp()`, сохраняя `untouched()`. Независимые операторские проверки выполняются
после работы CoderAgent в новой read-only песочнице без сети; сам CoderAgent
тесты не запускает. Настоящая модель выполнила сценарий за 10,312 s: восемь
числовых случаев, два исключения `ValueError` для перевёрнутых диапазонов и
побайтовая сохранность `untouched()` проверены успешно.

Результаты находятся в `$LOCAL_LLM_ROOT/smoke/<id>/`: `config.json`, `task.json`,
`smoke.json`, каталог `runs` с отчётом, журналом OpenCode и артефактами. Шлюз smoke
закрывается после проверки; его сохранённый `config.json` нельзя использовать
как постоянный канал без запуска нового шлюза и замены `model_socket`.

Доверенный профиль для этих небольших экспериментов:

| Параметр | Значение |
| --- | --- |
| `model_context_tokens` | 8192 |
| `model_output_tokens` | 2048 |
| `timeout_seconds` | 1800 |
| `model_request_timeout_seconds` | 600 |
| `model_temperature` | 0 |
| `model_reasoning_effort` для `qwen3.5:9b` | `none` |
| `max_steps` | 8 |
| Шлюз: `--upstream-timeout`, `--max-tokens` | 600, 2048 |

Контекст Ollama задаётся launcher-параметром `--context`; лимит в Config сообщает
его OpenCode, но сам не меняет сервер Ollama. `model_reasoning_effort` — необязательная
доверенная настройка; она передаётся через действующий параметр модели OpenCode
`options.reasoningEffort`. Скрипты выбирают режим по `thinking.values`: `none`,
если допускается false, и `low` для thinking-only моделей. Текущий `qwen3:4b`
содержит Qwen3-4B-Thinking-2507 и использует `low`; лимита ответа 2048 оказалось
недостаточно в двух проверенных поручениях CoderAgent.
Таймаут канала ограничивается общим сроком задачи. Файловые полномочия и бюджеты
запросов при этом не расширяются.

При настоящем запуске выявлено, что одного `agent.coder.temperature=0`
недостаточно: OpenCode пропускает температуру для custom-модели, если у неё не
объявлена возможность `provider.gateway.models.<model>.temperature=true`.
Теперь этот capability добавляется, только когда оператор явно задал
`model_temperature`. Шесть тестов с настоящим CLI подтвердили, в частности,
фактический `temperature: 0.0` в запросе к API. Первый запуск `qwen3:4b` был до
этого исправления, второй — после; оба без успешной правки.

Также исправлен разбор потока событий: ранний текст `COMPLETED` больше не
считается окончательным ответом, если после него были новые шаги без нового
финального ответа. Сценарий покрыт двумя unit-тестами. Побайтовая проверка
результата smoke использует `read_bytes()`, чтобы не скрыть изменения переводов
строк при чтении текста.

Контекст 8192 выбран из-за доступной RAM и достаточен только для маленького
проверочного поручения. [Интеграция Ollama с OpenCode](https://docs.ollama.com/integrations/opencode)
рекомендует существенно большее окно, от 64k; готовность к большим задачам здесь
не подтверждается.

## Постоянный шлюз для выбранной модели

Шлюз уже работает. Для повторного запуска после его остановки используйте
отдельный передний процесс:

```bash
install -d -m 700 "$LOCAL_LLM_ROOT/gateway"
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 "$PWD/.venv/bin/python" \
  -m coder_agent.gateway \
  --socket "$LOCAL_LLM_ROOT/gateway/ollama.sock" \
  --upstream http://127.0.0.1:11434/v1 --model qwen3.5:9b \
  --max-requests 100 --max-tokens 2048 --upstream-timeout 600
```

Доверенная конфигурация CoderAgent должна содержать тот же тег `model`, этот
`model_socket`, подготовленный `runtime_root` и точные списки файлов Policy.
Задача аналитика не может выбирать сервер, модель или расширять Policy.
Подготовленные `$LOCAL_LLM_ROOT/config.json` и `task.json` используют отдельный
демонстрационный исходник `demo/source/app.py`. Передний процесс шлюза для
`qwen3.5:9b` уже запущен с сокетом `gateway/ollama.sock`; `config.json` содержит
эту модель и reasoning `none`. Настоящий CLI успешно выполнил задачу: exit 0,
`COMPLETED`, run `f535c17ec04b4e41a75e5dccc540708f`. Точные байты результата,
сохранность исходника, список изменённых файлов и статус `FINISHED/COMPLETED`
проверены в `cli-verification.json`. Команда для повторного вызова:

```bash
.venv/bin/python -m coder_agent \
  --config "$LOCAL_LLM_ROOT/config.json" --input "$LOCAL_LLM_ROOT/task.json" \
  --output "$LOCAL_LLM_ROOT/new-result.json"
```

`--output` CLI должен указывать на новый файл. Завершайте запущенные передние
процессы через Ctrl+C в их терминалах. Пользовательский launcher не создаёт
автозапуск; после перезагрузки Ollama и шлюз нужно запустить снова. Существующие
модели, драйверы и системные службы не удалялись.

## Полученные доказательства

| Проверка | Результат |
| --- | --- |
| `qwen3.5:9b`: начальная API-проба — список, обычный ответ, streaming, tool/result loop | PASS, тогда локально на CPU, Q4_K_M, контекст 8192. |
| `qwen3.5:9b`: первый настоящий CoderAgent | 76,861 s, `NEEDS_CLARIFICATION`, правки нет. |
| `qwen3.5:9b`: второй настоящий CoderAgent | 99,638 s, модель заявила завершение без tool calls и правки; CoderAgent отклонил результат. |
| `qwen3.5:9b`, reasoning `low` | 418,422 s, read/edit/read успешны, но нет обязательного финального JSON; `FAILED`, исходник сохранён. |
| `qwen2.5-coder:7b`: API | Список/обычный ответ/streaming PASS. Вызов инструмента возвращён обычным текстом вместо `tool_calls`; проба отклонена. |
| `qwen2.5-coder:7b`: настоящий CoderAgent | 55,978 s, `FAILED`, правок нет, инструменты предложены модели, исходник сохранён. |
| `qwen3:4b`, thinking `low`, output 2048: API | Все четыре проверки PASS; обычный ответ 8,400 s, streaming 4,019 s, полный tool/result loop 45,134 s. CPU, RAM модели около 3,1 GiB, VRAM 0. |
| `qwen3:4b`: первый настоящий CoderAgent | 66,975 s, достигнут лимит ответа 2048, правок нет; температура ещё не передавалась OpenCode, исходник сохранён. |
| `qwen3:4b`: повтор после исправления температуры | 32,420 s, фактический `temperature: 0.0`, снова лимит ответа 2048, правок нет, исходник сохранён. |
| `qwen3.5:9b`: повтор после исправления температуры | **COMPLETED за 32,469 s, CUDA/GPU.** Три фактических API-запроса с `temperature: 0.0`, reasoning `none`; модели предложены read/edit/write. Точные байты правки, `changed_files`, сохранность исходника, replay и отказ `SECURITY_VIOLATION` для незаявленного файла — PASS. |
| `qwen3.5:9b`: содержательное Python-поручение | **COMPLETED за 10,312 s, CUDA/GPU.** Независимые `behavior_checks`: 8 числовых случаев clamp, 2 `ValueError` для перевёрнутых диапазонов и точные байты `untouched()` — PASS. Три API-запроса с temperature 0/reasoning none, сохранность исходника, `changed_files`, replay и отрицательный план — PASS. |
| `qwen3.5:9b`: отдельный CLI с постоянным шлюзом | **Exit 0, COMPLETED, CUDA/GPU.** Точные байты результата, исходник без изменений, `changed_files` и inspect `FINISHED/COMPLETED` — PASS. |
| `qwen3:4b-instruct-2507-q4_K_M` | Официальный nonthinking-тег, около 2,5 GB; скачан и сохранён как резерв. Live-проверка не проводилась, выбранной моделью не является. |
| Настоящий OpenCode: интеграционные проверки, включая температуру в API-запросе | 6 PASS. |
| Предыдущая полная регрессия CoderAgent с Bubblewrap и настоящим OpenCode | 173 PASS, 0 failed, 0 skipped, 45,678 s. |
| Полная регрессия после исправлений температуры, финального ответа и smoke | **176 PASS, 0 failed, 0 skipped, 45,287 s.** |

Веса `qwen3.5:9b` сохранены. Их выгрузка из RAM перед следующим кандидатом не
удаляет модель. Локальные доказательства: `qwen3.5-9b-probe.json`,
`qwen2.5-coder-7b-probe.json`, `qwen3-4b-final-probe.json`,
`qwen3-4b-value-smoke.json`, `qwen3-4b-value-retry.json`,
`qwen3.5-9b-fixed-smoke.json` (run `c6c06143cc8a4f63a71cb86d3cbffbb9`,
каталог `smoke/6fdb3cfd`),
`qwen3.5-9b-python-smoke.json` (run `81d7814c4efc485bbd0bc18d1f189ee9`,
каталог `smoke/66233637`),
`cli-result.json`, `cli-verification.json`, `final-hardware.json`,
`final-loaded-models.json`, `smoke/*/smoke.json`, `final-tests.log` внутри
`$LOCAL_LLM_ROOT`. Промежуточные неудачи сохраняются и не считаются успехом
интеграции. Автоматические тесты включают scripted API; реальное выполнение
модели подтверждается отдельно только live smoke.

Официальные источники: [установка Ollama на Linux](https://docs.ollama.com/linux),
[OpenAI-compatible API](https://docs.ollama.com/api/openai-compatibility),
[tool calling](https://docs.ollama.com/capabilities/tool-calling),
[qwen3.5:9b](https://ollama.com/library/qwen3.5:9b),
[qwen2.5-coder:7b](https://ollama.com/library/qwen2.5-coder:7b),
[qwen3:4b](https://ollama.com/library/qwen3:4b),
[явный instruct-тег](https://ollama.com/library/qwen3:4b-instruct-2507-q4_K_M).
