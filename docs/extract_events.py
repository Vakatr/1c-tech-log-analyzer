"""
Generate docs/events.md and docs/events.json with 1C Tech Log event reference.

Self-contained: does NOT read any HTML. Event data is embedded below.
Run:
    python docs/extract_events.py

Re-run anytime to regenerate both output files.
"""

import json
import datetime
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

EVENTS = [
    {
        "code": "EXCP",
        "title": "Исключительная ситуация",
        "description": "Возникает при ошибке в работе платформы или прикладного кода. Содержит текст исключения, контекст и стек вызовов.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Текст ошибки"},
            {"name": "Context", "type": "String", "meaning": "Контекст выполнения"},
            {"name": "Usr", "type": "String", "meaning": "Пользователь"},
        ],
        "examples": [
            "13.09.2026 15:00:08.234\nN=EXCP, p:processName=rphost, t:process=12346, Usr=Ivanov, AppID=1CV8, Descr=Ошибка при чтении документа: Ссылка не найдена"
        ],
    },
    {
        "code": "EXCPCNTX",
        "title": "Контекст исключения",
        "description": "Дополнительный контекст к событию EXCP. Идёт отдельной строкой после основного EXCP.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Строка контекста"},
        ],
        "examples": [
            "13.09.2026 15:00:08.250\nN=EXCPCNTX, p:processName=rphost, Descr=Документ.РеализацияТоваров.Форма.Модуль(125)"
        ],
    },
    {
        "code": "DBMSSQL",
        "title": "Запрос к MS SQL Server",
        "description": "Выполнение SQL-запроса к базе данных Microsoft SQL Server. Регистрируется при превышении порога длительности (обычно 3 секунды).",
        "fields": [
            {"name": "sql", "type": "Text", "meaning": "Текст SQL-запроса"},
            {"name": "Rows", "type": "Int", "meaning": "Количество возвращённых строк"},
            {"name": "RowsAffected", "type": "Int", "meaning": "Количество изменённых строк"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность в микросекундах"},
        ],
        "examples": [
            "13.09.2026 15:00:02.456\nN=DBMSSQL, p:processName=rphost, t:process=12345, Usr=Admin, AppID=1CV8, Duration=45000"
        ],
    },
    {
        "code": "DBPOSTGRS",
        "title": "Запрос к PostgreSQL",
        "description": "Аналог DBMSSQL для СУБД PostgreSQL.",
        "fields": [
            {"name": "sql", "type": "Text", "meaning": "Текст SQL-запроса"},
            {"name": "plan", "type": "Text", "meaning": "План выполнения"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [],
    },
    {
        "code": "CALL",
        "title": "Вызов серверного метода",
        "description": "Регистрируется при вызове серверного метода. Позволяет измерять длительность выполнения процедур и функций на сервере.",
        "fields": [
            {"name": "Event", "type": "String", "meaning": "Имя метода"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность в микросекундах"},
            {"name": "Memory", "type": "Int", "meaning": "Потребление памяти"},
        ],
        "examples": [
            "13.09.2026 15:00:01.123\nN=CALL, p:processName=rphost, t:process=12345, Usr=Admin, AppID=1CV8, Event=OpenForm, Duration=12500"
        ],
    },
    {
        "code": "SCALL",
        "title": "Серверный вызов (клиент-сервер)",
        "description": "Регистрирует вызовы между клиентом и сервером. Показывает, что именно клиент запросил у сервера.",
        "fields": [
            {"name": "Event", "type": "String", "meaning": "Тип операции"},
            {"name": "Duration", "type": "Int", "meaning": "Полное время, включая сеть"},
        ],
        "examples": [],
    },
    {
        "code": "SCOM",
        "title": "Соединение с внешней компонентой",
        "description": "Регистрирует работу с внешними компонентами (AddIn) и COM-объектами.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Описание операции или ошибки"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [
            "13.09.2026 15:00:22.123\nN=SCOM, p:processName=rphost, Usr=Admin, AppID=1CV8, Descr=Внешняя компонента загружена"
        ],
    },
    {
        "code": "SDBL",
        "title": "Язык запросов 1С",
        "description": "Выполнение запроса на встроенном языке запросов. Регистрируется по порогу длительности.",
        "fields": [
            {"name": "sql", "type": "Text", "meaning": "Текст запроса 1С"},
            {"name": "plan", "type": "Text", "meaning": "План выполнения"},
            {"name": "Rows", "type": "Int", "meaning": "Количество строк"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [],
    },
    {
        "code": "TLOCK",
        "title": "Ожидание блокировки",
        "description": "Регистрирует ожидание управляемой блокировки 1С, превысившее порог. Ключевой инструмент диагностики конфликтов.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Описание блокировки"},
            {"name": "Regions", "type": "Text", "meaning": "Регионы блокировки"},
            {"name": "Duration", "type": "Int", "meaning": "Время ожидания"},
            {"name": "WaitConnections", "type": "Int", "meaning": "Ожидающие соединения"},
        ],
        "examples": [
            "13.09.2026 15:00:45.789\nN=TLOCK, p:processName=rphost, Usr=Petrov, Duration=30000000, Descr=Превышение времени блокировки: 30 сек"
        ],
    },
    {
        "code": "TDEADLOCK",
        "title": "Взаимоблокировка",
        "description": "Возникает, когда два или более соединений ждут ресурсы друг друга. СУБД выбирает жертву и разрывает её транзакцию.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Описание конфликта"},
            {"name": "Connections", "type": "Text", "meaning": "Участники"},
        ],
        "examples": [
            "13.09.2026 16:00:03.400\nN=TDEADLOCK, p:processName=rphost, Usr=Sidorova, Descr=Ошибка SQL: Deadlock detected"
        ],
    },
    {
        "code": "TTIMEOUT",
        "title": "Таймаут",
        "description": "Превышение лимита времени на выполнение операции.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Что именно истекло"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [],
    },
    {
        "code": "TCONN",
        "title": "Соединения",
        "description": "Регистрирует подключения и отключения клиентов. Позволяет отслеживать количество активных сессий.",
        "fields": [
            {"name": "Event", "type": "String", "meaning": "Connect / Disconnect"},
            {"name": "Usr", "type": "String", "meaning": "Пользователь"},
            {"name": "ConnectionID", "type": "String", "meaning": "ID соединения"},
        ],
        "examples": [],
    },
    {
        "code": "CONF",
        "title": "Конфигурация",
        "description": "События, связанные с изменением конфигурации: обновление, изменение, загрузка расширений.",
        "fields": [
            {"name": "Event", "type": "String", "meaning": "Тип операции"},
            {"name": "Descr", "type": "String", "meaning": "Описание"},
        ],
        "examples": [],
    },
    {
        "code": "ADMIN",
        "title": "Административные действия",
        "description": "Регистрирует действия администратора: блокировка пользователей, завершение сессий, изменение настроек кластера.",
        "fields": [
            {"name": "Usr", "type": "String", "meaning": "Администратор"},
            {"name": "Descr", "type": "String", "meaning": "Действие"},
        ],
        "examples": [],
    },
    {
        "code": "TIME",
        "title": "Замеры времени",
        "description": "Служебное событие для замеров времени выполнения в коде.",
        "fields": [
            {"name": "name", "type": "String", "meaning": "Имя замера"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [],
    },
    {
        "code": "HASP",
        "title": "Лицензии HASP",
        "description": "События работы с аппаратными и программными лицензиями HASP.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Состояние лицензии"},
        ],
        "examples": [],
    },
    {
        "code": "ATTENTION",
        "title": "Внимание",
        "description": "События, требующие внимания администратора: серьёзные сбои, превышения критических порогов.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Описание проблемы"},
        ],
        "examples": [],
    },
    {
        "code": "FTEXT",
        "title": "Полнотекстовый поиск",
        "description": "Работа механизма полнотекстового поиска: индексация, поиск, обновление индекса.",
        "fields": [
            {"name": "Event", "type": "String", "meaning": "Тип операции"},
            {"name": "Duration", "type": "Int", "meaning": "Длительность"},
        ],
        "examples": [],
    },
    {
        "code": "SDGC",
        "title": "Сборка мусора",
        "description": "Сборка мусора в памяти серверного процесса. Помогает диагностировать утечки и паузы.",
        "fields": [
            {"name": "Duration", "type": "Int", "meaning": "Длительность сборки"},
            {"name": "Memory", "type": "Int", "meaning": "Объём памяти"},
        ],
        "examples": [],
    },
    {
        "code": "LEAKS",
        "title": "Утечки памяти",
        "description": "Подозрение на утечку памяти. Показывает объекты, которые долго не освобождаются.",
        "fields": [
            {"name": "Descr", "type": "String", "meaning": "Описание утечки"},
            {"name": "Memory", "type": "Int", "meaning": "Объём памяти"},
        ],
        "examples": [],
    },
]


def write_markdown(events, path):
    lines = [
        "# События технологического журнала 1С",
        "",
        "Источник: документация 1С (ИТС) и публичные материалы; формулировки переработаны, примеры синтетические.",
        f"Составлено: {datetime.date.today().isoformat()}",
        f"Всего событий: {len(events)}",
        "",
        "---",
        "",
    ]
    for e in events:
        lines.append(f"## {e['code']} — {e['title']}")
        lines.append("")
        lines.append(f"**Описание:** {e['description'] or '(описание не найдено в источнике)'}")
        lines.append("")
        if e["fields"]:
            lines.append("**Поля:**")
            lines.append("")
            lines.append("| Поле | Тип | Значение |")
            lines.append("|------|-----|----------|")
            for f in e["fields"]:
                lines.append(f"| {f['name']} | {f.get('type', '')} | {f['meaning']} |")
            lines.append("")
        if e["examples"]:
            lines.append("**Пример:**")
            lines.append("")
            lines.append("```")
            for ex in e["examples"]:
                lines.append(ex)
            lines.append("```")
            lines.append("")
        lines.append("---")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(events, path):
    data = {
        "source": "1C ITS documentation and public materials; rewritten summaries, synthetic examples",
        "extracted_at": datetime.datetime.now().isoformat(),
        "event_count": len(events),
        "events": events,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    md_path = OUT_DIR / "events.md"
    json_path = OUT_DIR / "events.json"

    write_markdown(EVENTS, md_path)
    write_json(EVENTS, json_path)

    print(f"Written {md_path} ({len(EVENTS)} events)")
    print(f"Written {json_path} ({len(EVENTS)} events)")


if __name__ == "__main__":
    main()