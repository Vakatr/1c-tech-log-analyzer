# ClickHouse и LogT

> Импорт логов, слой запросов, настройка сервера и подключения, безопасный сброс
> схемы и разбор ошибок. Транспорт — native-протокол (9000) через
> `clickhouse-driver`; HTTP (8123) использует только vendored LogT при импорте.

## 1. Схема работы

```text
папка логов ──► LogT (subprocess) ──► ClickHouse / tracelog
                                         │
      вкладки ◄── query_overview / query_errors /
                   query_performance / query_users
```

- ClickHouse — источник правды для вкладок «Обзор», «Ошибки»,
  «Производительность», «По пользователям».
- Разбор в память (`ics_log_parser`) остаётся резервным режимом; кнопка
  «Загрузить и распарсить» может включить его в любой момент, в том числе после
  успешного импорта в ClickHouse.
- Модули: `logt_adapter.py` (соединение, импорт, запросы), `logt_schema.py`
  (таблицы и DDL), `ics_log_analyzer.py` (GUI).

## 2. Импорт

0. Схему готовит сам GUI: перед каждым импортом он вызывает идемпотентную
   `ensure_schema()`. Она создаёт отсутствующие таблицы
   (`CREATE TABLE IF NOT EXISTS`) и не изменяет существующие. Отдельная ручная
   команда нужна только для проверки или подготовки базы без запуска GUI
   (необязательный шаг 4 в README). Если структура существующих таблиц
   несовместима, `ensure_schema()` возвращает `ready=False`, и импорт
   прерывается до запуска LogT — данные не изменяются.
1. Пользователь выбирает папку и dataset, нажимает
   «Загрузить в ClickHouse (LogT)». LogT перед загрузкой очищает строки этого
   dataset во всех четырёх таблицах (`clear_dataset`), поэтому повторный импорт
   заменяет dataset, а не дополняет его.
2. `run_logt_import` копирует `main.py` из `vendor/logt/src/` во временный
   каталог и генерирует там `config.json`.
3. LogT запускается subprocess-ом с `cwd` = staging-каталог; vendored checkout
   не изменяется.
4. stdout/stderr читаются параллельно двумя daemon-потоками: вывод нужен
   только для диагностики и прогресса. tqdm-тики stderr отфильтровываются
   (`is_tqdm_line`) и не вытесняют реальные ошибки из `stderr_tail`.
5. Прогресс типизирован (`P1-03`): `run_logt_import(..., on_progress=call)`
   отдаёт `import_progress.ImportProgress` по стадиям `PREPARING → SCANNING →
   PROCESSING → INSERTING → COUNTING → DONE` (10 событий/с, проценты не
   убывают); `DONE` отправляется только после успешного подсчёта строк.
   Прежний `progress_callback(str)` продолжает получать строки stdout.
6. `processing.count_lines_for_progress` теперь настраивается через
   `build_logt_config(..., count_lines_for_progress=...)` и
   `run_logt_import(..., count_lines_for_progress=...)`, по умолчанию `True`:
   LogT сначала считает строки, поэтому полоса прогресса имеет `total`
   (старт чуть медленнее; в GUI отключается чекбоксом в P1-04).
7. После завершения `count_tables` спрашивает ClickHouse количество строк.
   Ноль строк — ошибка, даже если LogT вернул код 0.
8. При успехе GUI переключается на ClickHouse и обновляет все вкладки.

## 3. Запросы вкладок

| Функция | Результат |
| :--- | :--- |
| `query_overview(config, filters, top_n=10)` | `totals` (events/users/event_types/datasets/errors), `time_range`, `top_events`, `top_users`, `datasets` |
| `count_errors(config, filters)` | число ошибок |
| `query_errors(config, filters, page, page_size)` | страница ошибок |
| `query_performance(config, filters, limit)` | самые долгие события |
| `query_performance_by_event(...)` | `count` / `sum` / `avg` / `max` / `p95` по типам |
| `query_users(config, filters)` | статистика по пользователям |
| `query_user_events(user, config, filters, limit)` | события пользователя |

`ClickHouseClient.errors()` в GUI объединяет `count_errors` (общее число для
пагинации) и `query_errors` (одна страница).

`Filters` содержит `dataset`, `date_from`, `date_to`, `user`, `event_name`,
`search`, а с v0.3 — `process_name` (`db_name`), `app_id` (`event_string`) и
`min_duration_ms` (`duration_us >= N*1000`). Все значения передаются серверными
параметрами (`{name:Type}`), поэтому SQL-инъекция невозможна. `app_id`
извлекается константным паттерном `AppID=([^,]+)` и сравнивается через
`lowerUTF8`; отдельной колонки AppID нет. Пагинация — `LIMIT`/`OFFSET`,
`MAX_PAGE_SIZE = 500`.

Описание ошибки формируется из `event_string` общим SQL-хелпером
`build_description_expressions`: поддерживаются `Descr="..."`, `Descr='...'` и
`Descr=...` до запятой/перевода строки, с раскрытием экранированных кавычек;
если поля нет — первые 200 символов. `query_errors` и `query_user_events`
возвращают `description_is_excerpt`, а GUI помечает сокращённое описание
символом `…`. Отдельной колонки описания в схеме LogT нет (DEBT-007).

## 4. Схема

Четыре таблицы: `operations`, `events`, `event_stats`, `calls`; база по умолчанию
— `tracelog`.

- `ensure_schema` идемпотентна: только `CREATE TABLE IF NOT EXISTS`, существующие
  таблицы не изменяются. GUI вызывает её перед каждым импортом и прерывает
  импорт, если схема несовместима.
- DDL рендерится из `vendor/logt/src/create_table_clickhouse.sql`, если файл
  присутствует; иначе — из `TABLE_SPECS`. Все `DROP`/`DROP DATABASE`
  отбрасываются, `CREATE DATABASE` не выполняется.
- `compare_vendored_schema()` сравнивает `TABLE_SPECS` с вендорным DDL целиком
  (колонки, типы, кодеки, engine, partition/order, индексы, настройки). Это
  проверяет `TestVendoredSchemaContract` в `test_logt_schema.py`; при наличии
  `vendor/logt` расхождение валит тест, поэтому обновление схемы LogT не
  проскочит незамеченным (DEBT-006).
- Отсутствующая база не создаётся — приложение сообщает об ошибке.
- Несовпадение структуры возвращает `ready=False` и подсказку по миграции;
  данные не изменяются.
- Лишние колонки (например, `operations.vrs_session`) — предупреждения, не
  блокируют работу.
- `DROP` есть только в `reset_schema(confirm=True)`, доступной из меню
  «ClickHouse → Сбросить схему ClickHouse…» после двух подтверждений.
- Перед каждой загрузкой LogT выполняет `clear_dataset`: удаляет строки
  выбранного dataset из всех четырёх таблиц. Это не меняет схему и не трогает
  другие dataset'ы, но повторный импорт полностью заменяет данные dataset'а.

## 5. Настройка подключения

Приоритет (первое непустое значение): GUI → переменные окружения →
`config/clickhouse.local.json` → значения по умолчанию.

| Переменная | По умолчанию |
| :--- | :--- |
| `CLICKHOUSE_HOST` | `127.0.0.1` |
| `CLICKHOUSE_PORT` | `9000` |
| `CLICKHOUSE_DATABASE` | `tracelog` |
| `CLICKHOUSE_USER` | `default` |
| `CLICKHOUSE_PASSWORD` | — |
| `LOGT_ROOT` | `vendor/logt` |
| `LOGT_WORK_DIR` | системный temp; если он не writable — `<repo>/.logt_local` |

`config/clickhouse.local.json` (в `.gitignore`) содержит поля `host`, `port`,
`database`, `user`, `password`, `secure`; шаблон — `config/clickhouse.example.json`.

## 6. Запуск сервера

```bash
docker compose up -d
# docker-compose.yml задаёт пользователя default с паролем "1"; для своего
# сервера подставьте реальный пароль.
docker exec -it 1c-clickhouse clickhouse-client --password 1 \
  --query "CREATE DATABASE IF NOT EXISTS tracelog"
```

Проверка:

```bash
curl "http://127.0.0.1:8123/?query=SELECT%201"          # HTTP (LogT)
python -c "from logt_adapter import check_clickhouse_connection as c; print(c())"
```

В GUI: **ClickHouse → Проверить подключение**. Приложение работает с портом 9000,
а не 8123.

## 7. Безопасность кредов

- Приложение не передаёт пароль в URL, argv или лог; `redact()` маскирует его в
  сообщениях. LogT получает пароль из временного `config.json` и сам отправляет
  его в HTTP-параметрах к ClickHouse.
- Для LogT создаётся временный `config.json` в одноразовом каталоге; каталог
  удаляется в `finally`.
- На Windows DACL ставится напрямую через Win32 `SetFileSecurityW` (только SID
  текущего пользователя) и затем проверяется чтением дескриптора. Файл
  создаётся пустым, пароль записывается только после успешной проверки. Если
  права применить или подтвердить не удалось, файл удаляется, а импорт
  прерывается: пароль не остаётся на диске с неизвестными правами. На POSIX
  проверяется режим `0o600`.
- `config/clickhouse.local.json` и `vendor/` не коммитятся.

## 8. Типовые ошибки

| Симптом | Причина / решение |
| :--- | :--- |
| `Cannot connect ... 127.0.0.1:9000` | Сервер не запущен или порт не опубликован; проверьте `docker ps`, `docker logs`. Нужен 9000. |
| `Authentication failed` (Code 516) | Неверный пользователь или пароль. Пустое поле GUI не перекрывает `CLICKHOUSE_PASSWORD`. |
| `Database 'tracelog' does not exist` | Создайте базу вручную (см. §6). |
| `ClickHouse schema mismatch` | Структура таблицы отличается; данные не тронуты. Мигрируйте вручную или укажите новую базу. |
| LogT exit 0, но 0 событий | Нет файлов `rphost_*` / `rmngr_*`, неверный формат лога, не созданы таблицы (`ensure_schema`) или несовпадение схемы. |
| `No writable directory ...` | Задайте `LOGT_WORK_DIR` в доступный каталог. |
| `Cannot protect the temporary LogT config file` | Windows не дал выставить/проверить DACL (например, FAT32 или сетевой диск). Укажите `LOGT_WORK_DIR` на локальный NTFS-каталог. |
| `ModuleNotFoundError: clickhouse_driver` | Установите `requirements.txt` или добавьте `.deps` в `PYTHONPATH` (см. [TESTING.md](TESTING.md)). |

## 9. История ревью и остаточные риски

Ревью интеграции закрыло блокеры: `DROP` на автоматических путях, отсутствие
потока данных во вкладки, парсинг stdout LogT, пароль в argv/env, захардкоженные
пути. Контракты закреплены тестами `test_logt_schema.py`,
`test_logt_sql_safety.py`, `test_logt_credentials.py`.

Остаточные риски:

1. `vendor/logt` — внешний git-ignored репозиторий. При его обновлении выполните
   `python -m pytest test_logt_schema.py -q`: контрактный тест сравнит
   `TABLE_SPECS` с вендорным DDL и покажет расхождение (DEBT-006).
2. Описание ошибок по-прежнему производное от `event_string`, а не хранимое
   поле: экстрактор покрывает основные формы `Descr`, но схема LogT не даёт
   гарантированного отдельного поля (DEBT-007).
3. DACL на временный `config.json` ставится через Win32 API и проверяется
   (ровно один ACE для SID пользователя); при ошибке файл удаляется, импорт
   прерывается — пароль не остаётся в файле с неизвестными правами (DEBT-008).
4. Live-тесты требуют сервера и пропускаются без него.
5. Режим памяти и ClickHouse используют одно определение ошибок
   (`EXCP`/`EXCPCNTX`, `ics_log_constants.py`), поэтому вкладка «Ошибки»
   показывает одинаковый набор строк (BUG-005).

Тесты — [TESTING.md](TESTING.md).
