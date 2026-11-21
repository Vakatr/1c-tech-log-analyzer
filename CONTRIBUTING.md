# Участие в разработке 1C Log Analyzer

> Практическое руководство: как развернуть окружение, запустить приложение, прогнать тесты, собрать `.exe` и внести изменения. Правила для ИИ-агентов и детальные соглашения по стилю — в [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md).

---

## 1. Что это за проект

Настольное приложение на Python/Tkinter для анализа технологических логов 1С. Логи загружаются в ClickHouse через **LogT** — внешний инструмент, который не входит в публичный репозиторий. После импорта вкладки «Обзор», «Ошибки», «Производительность» и «По пользователям» строятся запросами к ClickHouse. Режим разбора логов в память (без ClickHouse) сохранён и работает офлайн.

Архитектура, модули и API — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), интеграция с ClickHouse — [`docs/CLICKHOUSE.md`](docs/CLICKHOUSE.md).

---

## 2. Требования

| Компонент | Версия / примечание |
| :--- | :--- |
| Python | 3.10+ (проверено на **3.13**) |
| Tkinter | входит в стандартную поставку Python для Windows |
| ClickHouse | только для режима ClickHouse; native **9000** (приложение) и HTTP **8123** (LogT при импорте) |
| Docker | опционально, для локального сервера |
| PyInstaller | только для сборки `.exe` (ставится `build.bat` автоматически) |

---

## 3. Развёртывание окружения

### 3.1. Код и зависимости

```bash
git clone <repo-url>
cd 1c-tech-log-analyzer
python -m pip install -r requirements-dev.txt   # runtime-зависимости + pytest
```

### 3.2. Внешний LogT (нужен только для импорта в ClickHouse)

`vendor/logt` — **внешний инструмент**, он не входит в публичный репозиторий и находится в `.gitignore`, потому что его `src/config.json` может содержать реальные креды и локальные пути. Разворачивается вручную:

```bash
git clone <LogT-repository> vendor/logt
# Для импорта через приложение config.json не нужен: run_logt_import стейджит
# собственную копию. Если запускаете vendor/logt/src/main.py напрямую и в
# чекауте есть шаблон, создайте файл из него:
cp vendor/logt/src/config.json.example vendor/logt/src/config.json
```

Приложение **не читает** `vendor/logt/src/config.json` — оно стейджит собственную временную копию конфига. Путь можно переопределить переменной `LOGT_ROOT`.

> Не добавляйте `vendor/` в git и не «исправляйте» это: каталог исключён намеренно.

### 3.3. ClickHouse

```bash
docker compose up -d
# Пароль по умолчанию из docker-compose.yml — "1".
docker exec -it 1c-clickhouse clickhouse-client --password 1 \
  --query "CREATE DATABASE IF NOT EXISTS tracelog"
```

Таблицы GUI создаёт сам: перед каждым импортом вызывается идемпотентная
`ensure_schema()` (только `CREATE TABLE IF NOT EXISTS`); при несовместимой схеме
импорт прерывается. Ручная команда ниже необязательна — она нужна для проверки
или подготовки базы без запуска GUI (после настройки кредов в §3.4):

```bash
python -c "from logt_adapter import ensure_schema; s = ensure_schema(); print(s.message); raise SystemExit(0 if s.ready else 1)"
```

Подробно (включая безопасный сброс схемы и разбор ошибок) — [`docs/CLICKHOUSE.md`](docs/CLICKHOUSE.md).

### 3.4. Креды

Приоритет: значения из GUI → переменные окружения → `config/clickhouse.local.json` → значения по умолчанию.

```powershell
$env:CLICKHOUSE_PASSWORD = "<пароль>"
```

```bash
cp config/clickhouse.example.json config/clickhouse.local.json   # затем вписать пароль
```

`config/clickhouse.local.json` в `.gitignore` — **никогда не коммитьте его**.

---

## 4. Запуск и тесты

```bash
python ics_log_analyzer.py
```

```bash
python -m pytest -q -m "not live and not db"     # offline-набор (без ClickHouse)
python -m pytest -q --cov --cov-branch           # offline + покрытие
$env:CLICKHOUSE_TEST="1"; python -m pytest -m db # запросы к ClickHouse
$env:CLICKHOUSE_TEST="1"; python -m pytest -m live # E2E через LogT
```

В этом окружении драйвер лежит в `.deps/`:

```powershell
$env:PYTHONPATH = "$PWD\.deps"
```

Полное описание окружения, фикстур и типовых ошибок — [`docs/TESTING.md`](docs/TESTING.md).

---

## 5. Сборка `.exe`

```bash
build.bat
```

Результат: `dist\1C_Log_Analyzer\1C_Log_Analyzer.exe` (one-folder сборка).

**Все опции сборки — в `1C_Log_Analyzer.spec`.** `build.bat` проверяет Python, при необходимости ставит PyInstaller и зависимости из `requirements.txt`, затем вызывает спецификацию. Отдельно важно: `clickhouse_driver` импортируется **лениво** (внутри `logt_adapter._import_driver`), поэтому он объявлен в `hiddenimports` явно. Если добавите новую лениво импортируемую зависимость и забудете про `.spec`, в исходниках всё будет работать, а в собранном `.exe` — упадёт. Этот контракт зафиксирован в `test_build_spec.py`.

Проверка результата:

```powershell
Test-Path dist\1C_Log_Analyzer\_internal\clickhouse_driver   # должно быть True
```

---

## 6. Процесс внесения изменений

### 6.1. Ветки

- `main` — стабильное состояние.
- Рабочая ветка на задачу: `feature/<тема>`, `fix/<тема>`, `docs/<тема>`.
- Одна задача — одна ветка; не смешивайте рефакторинг, фикс и новую функциональность.

### 6.2. Перед правкой модуля

Прочитайте раздел о модуле в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): важно не нарушить слои и ациклический граф зависимостей (GUI не содержит логики парсинга, парсер не знает о статистике).

### 6.3. Коммиты

Формат: `<тип>: <краткое описание>`, тип — `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `chore`.

```text
feat: query ClickHouse for overview, errors, performance and users tabs
fix: make schema bootstrap idempotent and non-destructive
docs: add ClickHouse setup guide and update integration docs
```

### 6.4. Ревью

1. Откройте PR из рабочей ветки.
2. Убедитесь, что offline-набор зелёный и, если менялся код ClickHouse, — live-набор тоже.
3. Для изменений схемы/сборки приложите доказательство: вывод теста или содержимое `dist/`.
4. Ревьюер сверяет изменения с чек-листом [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md) §6.

### 6.5. Чек-лист перед коммитом

- [ ] Код соответствует PEP 8 (4 пробела, ≤100 символов, snake_case/PascalCase), публичные функции имеют docstring в Google style.
- [ ] Слои и граф зависимостей не нарушены.
- [ ] Документация обновлена (`docs/ARCHITECTURE.md` при изменении API или состава модулей).
- [ ] Тесты проходят: `python -m pytest -q -m "not live and not db"`.
- [ ] Новые проблемы/долг занесены в [`docs/ISSUES.md`](docs/ISSUES.md) по шаблону.
- [ ] Приложение запускается; при изменении `.spec` или состава зависимостей — сборка проверена.
- [ ] В коммит не попали секреты, `config/clickhouse.local.json`, `vendor/`, артефакты `build/`/`dist/`.

---

## 7. Чего делать не нужно

| Не делайте | Почему |
| :--- | :--- |
| Добавлять `vendor/` в git | Там реальные креды и локальные пути; каталог исключён намеренно. |
| Коммитить `config/clickhouse.local.json` | Содержит пароль. |
| Коммитить `build/`, `dist/`, `*.spec`-артефакты | Артефакты сборки; сам `1C_Log_Analyzer.spec` при этом нужен (см. §5). |
| Добавлять `DROP` на автоматические пути | Схема создаётся только через `CREATE TABLE IF NOT EXISTS`; единственный путь, удаляющий таблицы, — пункт меню «Сбросить схему…». Строки dataset удаляет сам LogT перед импортом. |
| Заводить каталог `tests/` | Тесты лежат в корне; см. [`docs/TESTING.md`](docs/TESTING.md). |
| Передавать пароль в URL/argv/лог | Транспорт — native-протокол; для LogT пишется временный конфиг во временном каталоге. |
