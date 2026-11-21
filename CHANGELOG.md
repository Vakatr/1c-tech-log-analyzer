# Журнал изменений

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [0.3.0] — 12.11.2026

### Добавлено

- `export.py`: Tk-free экспорт CSV/JSON/XLSX, атомарная запись, лимиты строк,
  отмена, дефьюзинг формул Excel и ленивый `openpyxl`.
- JSON-экспорт с метаданными (`schema_version`, `exported_at`, `row_count`,
  `truncated`, `columns`, `rows`); XLSX с листами «Данные» и «Метаданные».
- Вкладка «Экспорт»: выбор формата и области, постраничная выгрузка всех
  ошибок и пользователей, прогресс и отмена, подтверждение больших выгрузок.
- `presets.py`: пресеты фильтров в `config/presets.json`, allow-list,
  атомарная запись, восстановление `last_used`, архив битого файла.
- GUI пресетов: загрузить, сохранить как, удалить, сбросить все.
- `Filters.process_name`, `Filters.app_id`, `Filters.min_duration_ms` и
  соответствующие поля/подсказки в GUI.

### Изменено

- `_write_csv` в GUI делегирует в `export.export_rows`; CSV остаётся
  `utf-8-sig`, тексты кнопок и имена функций не изменились.
- `requirements.txt`: `openpyxl==3.1.5`, `et-xmlfile==2.0.0`.
- `.spec`: `collect_all("openpyxl")` и build-guard; `build.bat` проверяет
  импорт драйвера ClickHouse и openpyxl.

### Тесты

- `608 tests collected`; offline — `586 passed, 3 skipped, 19 deselected,
  150 subtests`; GUI — `101 passed`; покрытие offline — `90 %`.
- Новые наборы: `test_export.py` (38), `test_presets.py` (27); расширены
  `test_logt_sql_safety.py`, `test_gui_buttons.py`, `test_ics_log_analyzer.py`,
  `test_gui_error_paths.py`, `test_build_spec.py`.
- `build.bat` собрал `.exe`; в `_internal` присутствуют `openpyxl` и
  `clickhouse_driver`, процесс жив после 8 с.

## [0.2.0] — 11.11.2026

### Добавлено

- Типизированный прогресс импорта (`import_progress.py`, `logt_progress.py`):
  этапы, determinate/indeterminate, `X/Y`, процент, отмена ≤2 с.
- Немодальный диалог ошибки импорта (`import_errors.py`) с копированием деталей,
  журналом LogT и хвостами stdout/stderr без пароля.
- Preflight LogT для `.exe` (BUG-006): понятное сообщение до старта импорта.

### Изменено

- GUI: этап/счётчик/процент импорта, чекбокс точного прогресса, отмена.
- `run_logt_import(..., on_progress=, count_lines_for_progress=)` и чтение
  stderr; тики `tqdm` не вытесняют ошибки из `stderr_tail`.
