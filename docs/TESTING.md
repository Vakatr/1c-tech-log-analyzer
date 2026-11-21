# Тестирование

> `pytest`; тесты лежат в корне репозитория. Offline-набор не требует
> ClickHouse; DB- и live-наборы включаются явно через `CLICKHOUSE_TEST=1`.
> Покрытие измеряется в CI и контролируется порогом.

## 1. Кратко

| Что | Значение |
| :--- | :--- |
| Фреймворк | `pytest` (тесты написаны на `unittest.TestCase`) |
| Расположение | корень, `test_*.py`; каталог `tests/` не создавать |
| Всего | `608 tests collected` (снимок 12.11.2026, после v0.3) |
| Offline-прогон (Windows, без ClickHouse) | `586 passed + 3 skipped` + 19 deselected (6 live + 13 db) + 150 subtests |
| GUI-набор (`-m gui`) | `101 passed` |
| Live-набор (`CLICKHOUSE_TEST=1 -m live`) | `6 passed` при доступном ClickHouse; в прогоне 12.11.2026 сервер не был запущен → 6 skipped |
| Полный прогон (`CLICKHOUSE_TEST=1`) | Требует запущенного ClickHouse; при недоступном сервере db-тесты падают по подключению, а не пропускаются |
| Покрытие offline | **90 %** branch; `export.py` — 88 %, `presets.py` — 84 %, `logt_adapter` — 89 % |
| CI-порог | `coverage report --fail-under=90` по объединённым данным |

Числа — снимок на Windows с локальным ClickHouse. На Linux состав skip
отличается: POSIX-тесты выполняются, Windows DACL пропускается. Ориентируйтесь
на фактический вывод pytest.

## 2. Окружение

- Python 3.10+ (проверено на 3.13), Tkinter входит в стандартную поставку.
- `pip install -r requirements-dev.txt` — runtime-зависимости, `pytest` и
  `pytest-cov`.
- Если `clickhouse_driver` распакован в `.deps/`, добавьте его в `PYTHONPATH`:

```powershell
$env:PYTHONPATH = "$PWD\.deps"
```

- GUI-тесты на Windows идут напрямую; на Linux — под `xvfb-run -a`
  (CI ставит `xvfb` и `python3-tk`).
- При ограниченной записи в проект: `python -m pytest -q -p no:cacheprovider`.
- На portable Python задайте `TCL_LIBRARY` и `TK_LIBRARY`.

## 3. Маркеры и команды

| Маркер | Что означает |
| :--- | :--- |
| `gui` | Тест создаёт Tk-окно (`test_ics_log_analyzer`, `test_gui_buttons`, `test_gui_error_paths`) |
| `db` | Нужен запущенный ClickHouse; включается `CLICKHOUSE_TEST=1` |
| `live` | E2E через приватный `vendor/logt` и `.tmp/fixture`; только локально |
| `windows`, `posix` | Платформенная специфика |
| `unit`, `slow` | Зарезервированы для явной фильтрации |

`pytest.ini` включает `--strict-markers`: неизвестный маркер — ошибка.

```powershell
$env:PYTHONPATH = "$PWD\.deps"

# Обычный offline-набор (GUI включён)
python -m pytest -q -m "not live and not db" -p no:cacheprovider

# Offline + покрытие
python -m pytest -q -m "not live and not db" --cov --cov-branch

# Только GUI
python -m pytest -q -m gui -p no:cacheprovider

# Запросы к ClickHouse без LogT (создаёт и удаляет временную БД)
$env:CLICKHOUSE_TEST = "1"
python -m pytest -q -m db -p no:cacheprovider

# E2E через LogT (нужны креды, .tmp/fixture и vendor/logt)
python -m pytest -q -m live -p no:cacheprovider
```

`CLICKHOUSE_TEST=1` — единственный способ включить `db`/`live`. Наличие
`config/clickhouse.local.json` само по себе недостаточно: обычный прогон не
ходит в сеть и не создаёт dataset'ы.

## 4. Наборы

| Файл | Тестов | Что проверяет |
| :--- | ---: | :--- |
| `test_ics_log_stats.py` | 49 | Агрегации в памяти, единое определение ошибок, временная шкала |
| `test_logt_schema.py` | 46 | DDL, идемпотентность, отсутствие `DROP`, нормализация типов, fallback без vendor DDL |
| `test_logt_adapter_errors.py` | 45 | Классификация сетевых ошибок, `execute`/`ping`, config/workdir, `count_tables`, failure-пути импорта |
| `test_logt_sql_safety.py` | 44 | Параметризация, инъекции, валидация идентификаторов, границы пагинации, process/AppID/duration |
| `test_logt_progress.py` | 42 | `tqdm`/ANSI/`\r`, multi-process, числа, монотонность и fuzz |
| `test_export.py` | 38 | CSV/JSON/XLSX, кодировки, атомарность, формулы, лимиты, отмена, `iter_paged_rows` |
| `test_ics_log_analyzer.py` | 61 | GUI smoke: вкладки, фильтры, экспорт, схема, кэш, прогресс/отмена импорта |
| `test_gui_buttons.py` | 30 | Каждая кнопка GUI через Tk: загрузка, фильтры, пресеты, пагинация, экспорт, меню |
| `test_ics_log_parser.py` | 28 | Text/JSON-разбор, нестандартные форматы, параллельность, edge/fuzz |
| `test_logt_credentials.py` | 28 | Секреты, приоритет настроек, отсутствие утечек, PATH/LOGT_ROOT |
| `test_presets.py` | 27 | Путь, round-trip, валидация, битый файл, атомарность, allow-list, `last_used` |
| `test_import_progress.py` | 22 | Модель прогресса, throttling, монотонность, потокобезопасность |
| `test_import_errors.py` | 20 | `ImportFailure`, форматирование, журнал LogT, диалог ошибки импорта |
| `test_logt_import.py` | 20 | Отмена за ≤2 с, drain-потоки, CR-прогресс, реальные дочерние процессы |
| `test_logt_progress_integration.py` | 15 | Связка `LogTProgressParser` + `ImportProgress` на скриптованном дочернем процессе |
| `test_logt_description.py` | 14 | SQL-извлечение `Descr`, флаг сокращения, рендеринг `…` |
| `test_logt_clickhouse_db.py` | 13 | `query_*`, `ensure_schema`, `count_tables`, metadata на реальном ClickHouse |
| `test_build_spec.py` | 12 | `hiddenimports`/`collect_all`, openpyxl, отсутствие абсолютных путей |
| `test_gui_error_paths.py` | 10 | Отмена импорта/экспорта, preflight/parse/CSV-ошибки, memory user events, закрытие окна |
| `test_ics_events_registry.py` | 10 | Загрузка, кэш и ошибки `events.json` |
| `test_ics_logging.py` | 10 | Логирование: уровень, один обработчик, ошибки парсера/реестра/колбэков |
| `test_logt_permissions.py` | 9 | DACL Windows и режим `0o600`; отказ при невозможности защитить файл |
| `test_logt_runtime.py` | 9 | Preflight LogT и внешний Python для frozen-сборки |
| `test_logt_integration.py` | 6 | Live E2E: импорт через LogT и все вкладки |

Вспомогательные файлы:

- `gui_test_support.py` — общий `AppTestBase`, `FakeAdapter`, `pump/wait_until`;
  pytest его не собирает, тестовые модули не импортируют друг друга;
- `testdata/logs/` — версионируемые синтетические фикстуры 1С.

## 5. Покрытие

Конфигурация — `.coveragerc`:

- `branch = True`, `source = .`, `relative_files = True`;
- исключены `test_*.py`, `gui_test_support.py`, `vendor/`, `.deps/`, `.venv/`,
  `build/`, `dist/`, `docs/`, `examples/`, `skills/`;
- `show_missing = True`.

| Модуль | Offline |
| :--- | ---: |
| `ics_log_constants.py` | 100 % |
| `ics_log_stats.py` | 100 % |
| `ics_log_parser.py` | 97 % |
| `ics_logging.py` | 95 % |
| `logt_schema.py` | 95 % |
| `ics_events_registry.py` | 94 % |
| `logt_progress.py` | 93 % |
| `import_progress.py` | 91 % |
| `import_errors.py` | 90 % |
| `ics_log_analyzer.py` | 90 % |
| `logt_adapter.py` | 89 % |
| `export.py` | 88 % |
| `presets.py` | 84 % |
| **Итого** | **90 %** |

`logt_adapter` offline ниже, потому что запросы к ClickHouse выполняются только
в DB-наборе. Колонка «Offline + DB» в прошлых снимках давала 93 %; для v0.3
DB-прогон не выполнялся (локальный ClickHouse не был запущен). CI объединяет
данные unit- и DB-job'ов через `coverage combine`, поэтому порог 90 % считается
по объединённым данным. Новый код (`export.py`, `presets.py`) превышает
целевые 80 %.

Правило: `# pragma: no cover` допустим для защитных `except`, Win32-веток и
best-effort очистки, но не для бизнес-логики импорта, ошибок БД и безопасности.

## 6. DB-набор (без LogT)

`test_logt_clickhouse_db.py` запускается только при `CLICKHOUSE_TEST=1` и:

1. создаёт временную БД `pytest_db_<uuid8>` (приложение само БД не создаёт);
2. вызывает `ensure_schema` и проверяет идемпотентность;
3. вставляет детерминированные строки напрямую через `clickhouse-driver`;
4. проверяет `query_overview`, `query_errors` (пагинация/описания),
   `query_performance`, `query_performance_by_event`, `query_users`,
   `query_user_events`, `count_tables`, `read_columns`, `table_engines`,
   `ping`, изоляцию dataset'ов и SQL-инъекцию;
5. удаляет БД в `finally`, даже если тест упал.

Если `CLICKHOUSE_TEST=1`, но сервер недоступен, тесты **падают**, а не
пропускаются: сломанный CI-сервис не должен выглядеть зелёным. Набор не
требует `vendor/logt` и поэтому выполняется в CI.

## 7. Live-набор (LogT)

`test_logt_integration.py` — 6 тестов, включаются только при всех условиях:

- `CLICKHOUSE_TEST=1`;
- есть креды (`CLICKHOUSE_PASSWORD` или `config/clickhouse.local.json`);
- сервер доступен;
- существует `.tmp/fixture` с логами `rphost_*`/`rmngr_*`;
- есть `vendor/logt` с `src/main.py`.

В `setUpClass` набор вызывает `ensure_schema` и импортирует фикстуру через LogT,
затем проверяет все вкладки.

> Live-тесты создают dataset `pytest_integration`. Не запускайте их на
> продакшн-сервере. В CI live-набор не запускается: `vendor/logt` и
> `.tmp/fixture` не входят в репозиторий.

## 8. Полный прогон перед коммитом

```powershell
$env:PYTHONPATH = "$PWD\.deps"

python -m pytest -q -m "not live and not db" -p no:cacheprovider
python -m pytest -q -m "not live and not db" --cov --cov-branch
$env:CLICKHOUSE_TEST = "1"
python -m pytest -q -m db -p no:cacheprovider
python -m pytest -q -m live -p no:cacheprovider
cmd /c build.bat
```

Ожидаемо на 12.11.2026 (v0.3): `608 tests collected`; offline — `586 passed +
3 skipped` + 19 deselected + 150 subtests; GUI — `101 passed`. Покрытие:
90 % offline. DB/live требуют запущенного ClickHouse и `vendor/logt`.
Сборка `.exe` проверена: `_internal/openpyxl` и `_internal/clickhouse_driver`
присутствуют, процесс жив после 8 с.

## 9. CI

`.github/workflows/ci.yml`:

1. **Unit matrix** — Ubuntu и Windows, Python 3.11/3.13. Linux запускает GUI
   под `xvfb-run`; Windows — напрямую. Команда:
   `pytest -m "not db and not live" --cov --cov-branch`.
2. **DB job** — Ubuntu + сервис `clickhouse/clickhouse-server:24.8`;
   `CLICKHOUSE_TEST=1`, `pytest -m db`, отдельный `COVERAGE_FILE`.
3. **Combined coverage** — скачивает артефакты, `coverage combine`,
   `coverage report --fail-under=90`, публикует XML/HTML.
4. **Build** — PyInstaller по `.spec`; проверяет, что
   `dist/1C_Log_Analyzer/_internal/clickhouse_driver` и
   `dist/1C_Log_Analyzer/_internal/openpyxl` существуют, и публикует
   артефакт.

Live-тесты в CI не запускаются.

## 10. Типовые проблемы

| Симптом | Решение |
| :--- | :--- |
| `No module named 'clickhouse_driver'` | `PYTHONPATH=.deps` или `pip install -r requirements.txt` |
| `PermissionError` на `.pytest_cache` | `-p no:cacheprovider` |
| DB/live «skipped» | Задать `CLICKHOUSE_TEST=1` |
| DB падает с `ClickHouseUnavailable` | Поднять сервер; см. [CLICKHOUSE.md](CLICKHOUSE.md) §6 |
| Live «not configured» | Задать `CLICKHOUSE_PASSWORD` или локальный конфиг |
| Live «fixture not found» | Подготовить `.tmp/fixture` (`rphost_*` / `rmngr_*`) |
| `TclError` / нет Tcl/Tk | Задать `TCL_LIBRARY`, `TK_LIBRARY`; Linux — `xvfb-run` |
| `--strict-markers` ругается на маркер | Зарегистрировать его в `pytest.ini` |

## 11. Правила добавления тестов

1. Файл в корне: `test_<модуль>.py`; общие GUI-хелперы — в `gui_test_support.py`.
2. Стиль — `unittest.TestCase` с docstring.
3. По умолчанию тест не требует сети/ClickHouse. DB — маркер `db` и
   `CLICKHOUSE_TEST=1`; E2E — `live`.
4. Временные файлы — `tempfile` или `.tmp/`; версионируемые фикстуры —
   `testdata/`.
5. Секреты в тестах — фиктивные; проверяйте отсутствие утечки.
6. Контракт сборки — только в `test_build_spec.py`.
7. Каждый исправленный баг получает regression-тест.
8. Не импортируйте один тестовый модуль из другого; используйте
   `gui_test_support.py`.
9. Не повышайте покрытие пустыми тестами и не скрывайте бизнес-логику
   `# pragma: no cover`.

## 12. Известные skip

- 2 POSIX-теста прав (`test_logt_permissions.py`) на Windows.
- 5 Windows DACL-тестов на Linux.
- 1 POSIX zombie reaping тест на Windows.
- 13 DB-тестов без `CLICKHOUSE_TEST=1`.
- 6 live-тестов без `CLICKHOUSE_TEST=1`/fixture/vendor.
- 3 контрактных теста `vendor/logt` при отсутствии checkout'а.
