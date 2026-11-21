# Руководство разработчика и ИИ-агента

> Правила изменений, навигатор по репозиторию, стиль, конфигурация и сборка.
> Практическое развёртывание окружения — в [CONTRIBUTING.md](../CONTRIBUTING.md),
> тесты — в [TESTING.md](TESTING.md).

## 1. Навигатор

| Путь | Назначение |
| :--- | :--- |
| `ics_log_analyzer.py` | Tkinter GUI и точка входа (`LogAnalyzerApp`) |
| `logt_adapter.py` | ClickHouse, схема, запуск LogT, запросы вкладок |
| `logt_schema.py` | Таблицы/колонки и рендеринг DDL |
| `ics_log_parser.py` | Разбор `.log` в память |
| `ics_log_stats.py` | Агрегации в памяти |
| `ics_log_constants.py` | Общее определение ошибочных событий |
| `ics_logging.py` | Единая настройка `logging` (`ICS_LOG_LEVEL`) |
| `ics_events_registry.py` | Реестр событий из `docs/events.json` |
| `import_progress.py` | Модель прогресса импорта (`ImportStage`, `ImportProgress`, `ProgressReporter`) |
| `logt_progress.py` | Парсер stdout/stderr LogT → `ImportProgress` |
| `import_errors.py` | Модель/форматтер/диалог ошибки импорта |
| `export.py` | CSV/JSON/XLSX экспорт, лимиты, атомарная запись (Tk-free) |
| `presets.py` | Пресеты фильтров в `config/presets.json` (Tk-free) |
| `docs/ARCHITECTURE.md` | Слои, модули, API, потоки данных |
| `docs/CLICKHOUSE.md` | Интеграция, настройка, ошибки |
| `docs/TESTING.md` | Тесты и окружение |
| `GUI-test.md` | Чек-лист кнопок GUI и повторная проверка |
| `docs/ISSUES.md` | Баги, производительность, техдолг |
| `docs/ROADMAP.md` | План развития |
| `docs/events.md` | Справочник событий 1С |
| `vendor/logt/` | Vendored LogT (git-ignored, разворачивается вручную) |
| `1C_Log_Analyzer.spec`, `build.bat` | Сборка PyInstaller |
| `requirements.txt`, `requirements-dev.txt` | Runtime- и dev/CI-зависимости |
| `.github/workflows/ci.yml` | CI: offline-тесты (Linux/Windows) и сборка `.exe` |

## 2. Правила изменений

1. Одна задача — один логический набор изменений.
2. При изменении API или состава модулей обновляйте `docs/ARCHITECTURE.md`.
3. Не нарушайте слои: GUI без SQL и парсинга; парсер не знает о статистике.
4. Новые зависимости — по согласованию, с версией в `requirements.txt` и (если
   импорт ленивый) в `hiddenimports` спеки.
5. Не глотайте исключения: для неожиданных ошибок используйте `logging` и
   `logger.exception(...)` (DEBT-003); пользовательские ошибки — через
   `messagebox` в GUI. `ics_logging.configure_logging` вызывается один раз в
   `main`, уровень задаётся `ICS_LOG_LEVEL`.
6. Баги и техдолг — в `docs/ISSUES.md`.
7. Коммит: `<тип>: <описание>`, тип — `feat`, `fix`, `docs`, `refactor`, `test`,
   `build`, `chore`.

### 2.1. Политика коммитов

1. **Коммит после каждого значительного результата**, а не в конце работы.
   Значительный результат — завершённый шаг задачи с зелёными тестами
   (например, P1-00a, затем P1-01), а не каждое сохранение файла.
2. **Один коммит — одно логическое изменение.** Не смешивайте рефакторинг,
   новое поведение и правки документации, если они не относятся к одной
   задаче.
3. **Не оставляйте готовую работу в рабочем дереве.** Перед началом
   следующей задачи закоммитьте текущую; в отчёте укажите хеш коммита.
4. **Каждый коммит оставляет offline-набор зелёным.** Перед коммитом
   запустите `python -m pytest -q -p no:cacheprovider` (или релевантный
   набор, если полный прогон объективно недоступен, — укажите это в отчёте).
5. **Тесты и документация — в том же коммите**, что и изменение поведения.
   Исключение: отдельный docs-коммит, если документация синхронизируется
   постфактум по нескольким задачам.
6. Если задача разбита на шаги, каждый шаг — отдельный коммит с собственным
   сообщением. Незавершённые WIP-коммиты допустимы только в локальной ветке.
7. Не коммитьте артефакты сборки, `.tmp`, логи, временные конфиги и секреты.
8. Сообщение: `<тип>(<область>): <краткое императивное описание>`; при
   необходимости — тело с причиной и ссылкой на ID задачи/issue.

## 3. Стиль

- PEP 8: 4 пробела, ≤100 символов, snake_case / PascalCase / UPPER_CASE.
- Импорты: сначала stdlib, затем внутренние модули; без `import *`.
- Публичные функции — с аннотациями типов.
- Docstring — Google style:

```python
def parse_log_directory(path: str) -> list[dict]:
    """Парсит все .log-файлы в каталоге.

    Args:
        path: Путь к каталогу с логами.

    Returns:
        Список событий.
    """
```

- Markdown: иерархия заголовков без пропусков, таблицы для перечислений,
  относительные ссылки.

## 4. Конфигурация и данные

| Путь | Назначение |
| :--- | :--- |
| `docs/events.json` | Реестр событий: `{source, extracted_at, event_count, events[]}` |
| `docs/events.md` | Человекочитаемый справочник событий |
| `docs/extract_events.py` | Генерация `events.*` из встроенного массива `EVENTS` (HTML не читает) |
| `config/clickhouse.example.json` | Шаблон подключения; рабочий файл в `.gitignore` |
| `docker-compose.yml` | Локальный ClickHouse (только для разработки) |

Реестр загружается лениво и кэшируется (`load_registry`); `known_field_names()`
расширяет маппинг полей парсера. При отсутствии или повреждении `events.json`
возвращается пустой реестр, причина доступна через `registry_error()`, а GUI
показывает предупреждение во вкладке «Справочник» и в статус-баре.

Настройки ClickHouse и переменные окружения — в [CLICKHOUSE.md](CLICKHOUSE.md) §5.

## 5. Запуск, сборка, тесты

```bash
python ics_log_analyzer.py
build.bat          # -> dist\1C_Log_Analyzer\1C_Log_Analyzer.exe
python -m pytest -q -m "not live and not db"   # offline
python -m pytest -q --cov --cov-branch         # offline + покрытие
```

- Сборка one-folder; все опции — в `1C_Log_Analyzer.spec`. `build.bat` проверяет
  Python, при необходимости ставит PyInstaller и `requirements.txt`, затем
  вызывает спецификацию.
- `clickhouse_driver` импортируется лениво, поэтому объявлен в `hiddenimports`;
  контракт проверяет `test_build_spec.py`.
- `ensure_schema` создаёт отсутствующие таблицы идемпотентно; GUI вызывает её
  перед каждым импортом, поэтому пустую базу готовить отдельной командой не
  нужно.
- Тесты: корень репозитория; маркеры `gui`, `db`, `live`; `CLICKHOUSE_TEST=1`
  включает DB/live. Покрытие — `.coveragerc`, CI-порог 90 % — см.
  [TESTING.md](TESTING.md).

## 6. Чек-лист перед завершением

- [ ] PEP 8, docstring-и, аннотации типов.
- [ ] Слои и граф зависимостей не нарушены ([ARCHITECTURE.md](ARCHITECTURE.md)).
- [ ] Документация обновлена.
- [ ] Новые проблемы занесены в [ISSUES.md](ISSUES.md).
- [ ] `python -m pytest -q -m "not live and not db"` зелёный.
- [ ] Покрытие не снизилось: `python -m pytest -q --cov --cov-branch`.
- [ ] Приложение запускается; при изменении `.spec` сборка проверена.
- [ ] Значимый результат закоммичен по правилам §2.1, хеш указан в отчёте.
