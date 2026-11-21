# Архитектура 1C Log Analyzer

> Настольное приложение на Python/Tkinter для анализа технологических логов 1С.
> Основной путь: логи → LogT → ClickHouse → вкладки GUI. Разбор в память сохранён
> как резервный режим без ClickHouse.

## 1. Модули и публичный API

| Слой | Модуль | Ответственность |
| :--- | :--- | :--- |
| Presentation | `ics_log_analyzer.py` | Tkinter GUI: вкладки, фильтры, экспорт, фоновые задачи |
| Integration | `logt_adapter.py` | ClickHouse-соединение, схема, импорт через LogT, запросы вкладок |
| Integration | `logt_schema.py` | `TABLE_SPECS` — эталон структуры для валидации; DDL рендерится из вендорного SQL (если он есть) либо из `TABLE_SPECS` с удалением `DROP` |
| Domain (memory) | `ics_log_parser.py` | Разбор `.log` (JSON / text) в события; файлы читаются параллельно |
| Domain (memory) | `ics_log_stats.py` | Агрегации в памяти для offline-режима |
| Domain (memory) | `ics_log_constants.py` | Общее определение ошибочных событий (`EXCP`, `EXCPCNTX`) |
| Domain (import) | `import_progress.py` | `ImportStage`/`ImportProgress` и `ProgressReporter`: единый контракт прогресса, монотонность, троттлинг; без Tk |
| Domain (import) | `logt_progress.py` | Парсер stdout/stderr LogT (`[INFO]`, `tqdm`, multi) → `ImportProgress`; без Tk и ClickHouse |
| Presentation | `import_errors.py` | `ImportFailure`, `format_import_failure`, `find_latest_logt_log` и немодальный `ImportErrorDialog`; ClickHouse-импортов нет, адаптер используется duck-typed |
| Domain (export) | `export.py` | Tk-free сериализация CSV/JSON/XLSX, атомарная запись, лимиты, отмена, дефьюзинг формул; `openpyxl` импортируется лениво |
| Domain (filters) | `presets.py` | Пресеты фильтров в `config/presets.json`, allow-list, валидация, атомарная запись; Tk-free, без `logt_adapter` |
| Infrastructure | `ics_events_registry.py` | Реестр кодов событий из `docs/events.json` |
| Infrastructure | `ics_logging.py` | Единая настройка `logging` (DEBT-003): один обработчик, уровень из `ICS_LOG_LEVEL` |

| Модуль | Публичный API (основное) |
| :--- | :--- |
| `ics_log_analyzer` | `LogAnalyzerApp` (включая `run_async`), `format_description` |
| `logt_adapter` | `resolve_config`, `check_clickhouse_connection`, `ensure_schema`, `validate_schema`, `reset_schema`, `count_tables`, `build_logt_config`, `run_logt_import`, `resolve_logt_python`, `check_logt_runtime`, `redact`, `build_description_expressions`, `query_overview`, `count_errors`, `query_errors`, `query_performance`, `query_performance_by_event`, `query_users`, `query_user_events`, `Filters` |
| `logt_schema` | `TABLE_SPECS`, `TableSpec`, `render_create_statements`, `compare_vendored_schema`, `expected_columns`, `schema_mismatches`, `schema_warnings`, `migration_hint`, `qualified_table` |
| `ics_log_parser` | `parse_log_directory`, `parse_log_file`, `parse_event_block`, `detect_format` |
| `ics_log_stats` | `aggregate_by_event`, `aggregate_by_user`, `find_errors`, `top_slowest`, `timeline_by_hour` |
| `ics_log_constants` | `ERROR_EVENT_NAMES`, `is_error_event`, `is_error_event_name` |
| `import_progress` | `ImportStage`, `ImportProgress`, `ProgressReporter` |
| `logt_progress` | `LogTProgressParser`, `is_tqdm_line` |
| `import_errors` | `ImportFailure`, `ImportErrorDialog`, `find_latest_logt_log`, `format_import_failure` |
| `export` | `ExportFormat`, `ExportResult`, `export_rows`, `iter_records`, `iter_paged_rows`, `defuse_formula`, `openpyxl_available`, `suggest_filename`, `filetypes`, исключения `ExportError`/`ExportCancelled`/`ExportLimitExceeded`/`ExportDependencyError` |
| `presets` | `PresetStore`, `filters_to_dict`, `dict_to_filter_kwargs`, `DEFAULT_PRESETS_PATH`, `PresetsError` |
| `ics_events_registry` | `load_registry`, `registry_error`, `events_path`, `all_event_codes`, `get_event`, `known_field_names` |
| `ics_logging` | `configure_logging`, `resolve_level`, `is_configured` |

## 2. Зависимости

```text
ics_log_analyzer.py
├── ics_log_parser.py ──► ics_events_registry.py
├── ics_log_stats.py ──► ics_log_constants.py
├── ics_log_constants.py
├── ics_events_registry.py
├── ics_logging.py
└── logt_adapter.py ──► logt_schema.py
                       └► ics_log_constants.py

logt_progress.py ──► import_progress.py
logt_adapter.py ──► import_progress.py, logt_progress.py

ics_log_analyzer.py ──► import_errors.py (ImportFailure, ImportErrorDialog)
ics_log_analyzer.py ──► export.py (ExportFormat, export_rows, iter_paged_rows)
ics_log_analyzer.py ──► presets.py (PresetStore, filters_to_dict)
```

Граф ациклический. `logt_adapter` не импортирует Tkinter и пригоден для
headless-тестов; `ics_log_stats`, `ics_log_constants` и `ics_events_registry`
независимы. `import_progress.py` и `logt_progress.py` также не зависят от Tk
и ClickHouse. С P1-03 адаптер связан с ними: `logt_adapter` импортирует
`import_progress` и `logt_progress` (обратных зависимостей нет), маршалинг
`ImportProgress` в Tk остаётся задачей GUI (P1-04).
`ProgressReporter` вызывает callback в потоке вызывающего.
`import_errors.py` импортирует только stdlib и Tkinter: `ImportResult`
передаётся как duck-typed объект, поэтому у модуля нет зависимости от
`logt_adapter` и ClickHouse.

`export.py` — stdlib плюс ленивый `openpyxl` внутри `_import_openpyxl()`;
Tkinter не импортируется. `presets.py` — stdlib, фильтры передаются
duck-typed (mapping или объект с атрибутами), поэтому зависимости от
`logt_adapter` нет. Оба модуля не знают о GUI и ClickHouse-соединении.

## 3. Потоки данных

**Основной режим — ClickHouse** (источник правды для всех вкладок):

```text
папка логов ──► LogT (subprocess) ──► ClickHouse / tracelog
                                         │
        вкладки ◄── query_overview / count_errors+query_errors /
                     query_performance* / query_users
```

- Импорт выполняет `run_logt_import`; успех подтверждает `count_tables`, а не код
  возврата LogT.
- Перед импортом GUI вызывает `check_logt_runtime()`: в собранном `.exe`
  интерпретатор и `vendor/logt` внешние, поэтому отсутствие `LOGT_ROOT` или
  Python 3.11+ (`LOGT_PYTHON`, иначе `python`/`python3`/`py` из `PATH`)
  показывается диалогом до создания потока и временных файлов (BUG-006).
  `resolve_logt_python()` — единственное место, где выбирается интерпретатор;
  в режиме исходников это `sys.executable`, поведение не меняется.
- Перед импортом GUI вызывает `ensure_schema`, которая идемпотентно создаёт
  отсутствующие таблицы (`CREATE TABLE IF NOT EXISTS`); при несовпадении схемы
  импорт прерывается (BUG-004 закрыт).
- После импорта `self.source = "clickhouse"`, фильтры ограничены dataset,
  агрегация выполняется в ClickHouse.
- LogT перед загрузкой удаляет строки выбранного dataset (`clear_dataset`), так
  что повторный импорт заменяет dataset; схема и другие dataset'ы не меняются.
- Ошибки (`EXCP`, `EXCPCNTX`): описание извлекается из `event_string` общим
  SQL-хелпером `build_description_expressions` (формы `Descr="..."`,
  `Descr='...'`, `Descr=...`, иначе 200 символов); `description_is_excerpt`
  помечает сокращение, отдельной колонки в схеме нет (DEBT-007).
- Фильтры v0.3 (`Filters`): `process_name` сравнивается с `db_name`
  (значение `casefold()`, колонка уже нормализована LogT); `app_id`
  извлекается из `event_string` константным паттерном `AppID=([^,]+)` и
  сравнивается через `lowerUTF8`; `min_duration_ms` превращается в
  `duration_us >= {f_min_duration_us:UInt64}`. Все значения — только
  query-параметры. `query_user_events` пересобирает `Filters` вручную и обязан
  переносить все три поля.

**Резервный режим — память** (без ClickHouse):

```text
*.log ──► parse_log_directory ──► self.events ──► ics_log_stats.* ──► вкладки
```

- Парсер в память читает `*.log` из выбранной папки; если на верхнем уровне
  файлов нет, он рекурсивно обходит вложенные каталоги. LogT всегда обходит
  `rphost_*` / `rmngr_*` рекурсивно.
- Агрегаты для вкладок кэшируются до замены списка событий (`_memory_data`);
  вычисление и срез страницы ошибок выполняются в worker-потоке, а деревья
  обновляются в главном потоке (PERF-003).
- Определение ошибки единое для памяти и ClickHouse: `EXCP` / `EXCPCNTX`
  (`ics_log_constants.ERROR_EVENT_NAMES`).

**Прогресс импорта (P1-03)**

```text
LogT stdout ──┐
              ├─► LogTProgressParser ─► ProgressReporter ─► on_progress(ImportProgress)
LogT stderr ──┘   (Lock только вокруг     (монотонность,        (поток pump-а;
                   парсера)                10 событий/с)        Tk-маршалинг — GUI)
```

- `run_logt_import(..., *, on_progress=None, count_lines_for_progress=True,
  progress_interval=0.1)`: `on_progress` получает типизированный
  `ImportProgress`; прежний `progress_callback(str)` не меняется и получает
  строки stdout, как раньше.
- Стадии: `PREPARING` — начало импорта; `SCANNING`/`PROCESSING`/`INSERTING` —
  из разбора stdout и stderr; `COUNTING` (95 %) — перед `count_tables`;
  `DONE` (100 %) — только после успешного подсчёта строк. При отмене, ошибке
  или нуле строк `DONE` не отправляется. `SCHEMA` эмитит GUI (P1-04).
- stdout и stderr читаются двумя pump-потоками; парсер защищён одним
  `threading.Lock`, а callback вызывается вне него, поэтому медленный GUI не
  блокирует второй поток.
- `message` и `detail` маскируются через `redact(..., password)`; исключение
  в `on_progress` ловится и не влияет на импорт.
- Тики tqdm не попадают в `stderr_parts`/`stderr_tail` (`is_tqdm_line`),
  поэтому в хвосте ошибки остаются реальные сообщения.
- `build_logt_config(..., count_lines_for_progress=True)` пробрасывает
  `processing.count_lines_for_progress`: предварительный подсчёт строк делает
  прогресс детерминированным ценой более медленного старта (в GUI — чекбокс,
  P1-04).

**Поток ошибки импорта (P1-05)**

```text
work() — этап в локальном dict ──► исключение в runner() (worker)
        │                                   │
        │                                   ▼
        │                    ImportFailure(stage, redact(exc), dataset, log_dir,
        │                                  log_path=find_latest_logt_log(log_dir))
        │                                   │ dispatcher.post("import_failed")
        │                                   ▼
        │                    _on_import_failed → _show_import_error (Tk main)
        ▼
ImportResult(success=False) ─► import_done ─► ImportFailure.from_result
                                             └► ImportErrorDialog (немодальный)
```

- Этап («Подготовка», «Подготовка схемы ClickHouse», «Импорт LogT») хранится в
  локальном `dict` внутри `load_via_logt`, поэтому worker не пишет в Tk-виджеты.
- `find_latest_logt_log` вызывается в worker-потоке: ищется самый новый
  `trace-vrs-*.log` в `<log_dir>/.logt_logs` (ошибки ФС → `None`); в главном
  потоке каталоги не обходятся.
- `ImportErrorDialog` — `Toplevel` без `grab_set`/`wait_window`; текст только
  для чтения, но выделяемый, с кнопкой «Скопировать детали». `Esc` и
  `WM_DELETE_WINDOW` закрывают окно; новый отчёт заменяет старый.
- Весь показанный и скопированный текст проходит через
  `logt_adapter.redact(..., password)`, включая хвосты `stderr`/`stdout`.
- `messagebox.showerror` для импорта не используется; остальные ошибки
  (подключение, схема по меню, экспорт) сохраняют прежние диалоги.

**Экспорт (v0.3)**

```text
текущая таблица (Tk tree) ──────────────► export.export_rows ─► CSV/JSON/XLSX
                                              (main thread, атомарно)

«Все ошибки»: count_errors ─► iter_paged_rows(offset, 500)
              └► query_errors(page=offset/500) ─┐
«Пользователи»: query_users ───────────────────┴► export_rows (worker)
                                                     │
             export_progress/done/cancelled/failed ─► dispatcher ─► Tk
```

- «Все отфильтрованные» доступно только для ошибок и пользователей; обзор и
  производительность — агрегаты top-N, поэтому кнопка объясняет ограничение.
- Ошибки читаются страницами по 500 строк через `count_errors` +
  `query_errors`, а не «всей таблицей»; лимит 100 000 строк и подтверждение
  ≥10 000 строк задаёт GUI.
- Запись атомарная (`.part` + `os.replace`), поэтому ошибка или отмена не
  портит существующий файл. `ExportResult.truncated` сообщает об усечении.
- JSON содержит `schema_version`, `exported_at`, `row_count`, `truncated`,
  `columns`, `rows` и метаданные источника; XLSX — листы «Данные» и
  «Метаданные», формулы принудительно текстом.
- Пресеты фильтров хранятся в `config/presets.json`; allow-list исключает
  пароль, host и log_dir. `last_used` восстанавливает поля при старте, но
  запрос не выполняется до нажатия «Применить».
- Worker экспорта ничего не трогает в Tk: прогресс, завершение, отмена и ошибка
  идут через `dispatcher` видами `export_progress`/`export_done`/
  `export_cancelled`/`export_failed`.

Подробности интеграции — [CLICKHOUSE.md](CLICKHOUSE.md).

## 4. Потокобезопасность

- Главный поток Tk не выполняет сетевой I/O, разбор логов и агрегации режима
  памяти: импорт, запросы и агрегации идут в worker-потоках. Экспорт текущей
  таблицы синхронный; экспорт «все» — worker с `threading.Event` для отмены.
  Oба пути пишут через `export.py` атомарно.
- Диагностика идёт через `logging`; `configure_logging` вызывается в точке
  входа, ошибки Tk-колбэков логируются и показываются пользователю (DEBT-003).
- Результаты передаются через `queue.Queue`; GUI разбирает её в `root.after`
  каждые 100 мс.
- Прогресс импорта (P1-04) идёт по тому же пути, но отдельным видом сообщения:
  1. Адаптер вызывает `on_progress(ImportProgress)` из pump-потоков LogT
     (троттлинг 0,1 с, монотонные стадии и процент — P1-01/P1-03).
  2. GUI-обёртка `load_via_logt` кладёт снимок в `ResultDispatcher` как
     `("import_progress", ImportProgress)`; worker не трогает Tk.
  3. `ResultDispatcher.drain(..., coalesce_kinds={"import_progress"})`
     схлопывает серию снимков до последнего и доставляет его **до** любого
     другого сообщения, в частности до `import_done`. Поэтому поздний прогресс
     не перерисовывает уже завершённый или отменённый импорт.
  4. `_on_import_progress` применяет снимок только при `self.busy`:
     `percent=None` → indeterminate и «Файлов: —», известный процент →
     determinate (`maximum=total` либо 100, `value=current/percent`), подписи
     «X/Y» и «P %». Строковый payload поддерживается для совместимости и
     показывается в статус-баре после очистки от `[INFO]`/`\r`/переводов строк.
  5. Перед `ensure_schema` GUI эмитит `ImportProgress(SCHEMA)`; остальные
     стадии приходят из адаптера. Чекбокс «Точный прогресс…» читается в
     главном потоке до старта worker и уходит в `count_lines_for_progress`.
- На время импорта элементы загрузки блокируются, доступна отмена (terminate
  subprocess). Нажатие «Отменить» блокирует кнопку, меняет этап на «Остановка
  LogT…» и статус на «Отмена запрошена…»; итоговое состояние рисует
  `_on_import_done` (`_mark_import_done`/`_mark_import_cancelled`/
  `_mark_import_failed`).
- Закрытие окна отменяет импорт, ждёт поток импорта до 5 с и завершает Tk;
  остальные worker-потоки объявлены daemon и не блокируют выход.

## 5. Принципы

- GUI не содержит SQL и логики парсинга; зависимости направлены сверху вниз.
- Запросы приложения к ClickHouse идут только через `clickhouse-driver` (native,
  порт 9000); vendored LogT при импорте использует HTTP (8123).
- Схема — только `CREATE TABLE IF NOT EXISTS`; `DROP` возможен лишь в
  `reset_schema(confirm=True)`.
- Имена таблиц и ключевых колонок — из `logt_schema.py`, включая
  `DB_NAME_COLUMN`.
- Пользовательский ввод — только через серверные query-параметры; идентификаторы
  валидируются.
