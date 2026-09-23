# История изменений

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии следуют [SemVer](https://semver.org/lang/ru/).

## [0.2.0] — 2026-09-23

### Изменено

- Код разложен в устанавливаемый пакет `stepik_mcp` (`client`, `render`, `feedback`, `analytics`, `workspace`, `server`). Команды запуска: `stepik-mcp` и `python -m stepik_mcp`.
- Тесты перенесены в `tests/` и работают на любой ОС через системную временную папку (`STEPIK_MCP_TEST_TMPDIR` для переопределения).
- Документация перенесена в `docs/`: PRD обновлён до версии 0.2, `plan.md` разделён на [дорожную карту](docs/ROADMAP.md) и [заметки о проектировании](docs/design-notes.md).

### Добавлено

- `pyproject.toml`, CI на GitHub Actions (Windows и Linux, Python 3.12–3.13), линтер ruff.
- README с подключением к Claude Code, Claude Desktop и OpenCode; `AGENTS.md` и `CLAUDE.md` для агентов, работающих над репозиторием.

### Удалено

- Старый HTTP-мост `stepik_bridge.py` с токеном в URL. Режим `--http` MCP-сервера сохранён.
- Резервные копии `*_backup.py` и архив `stepik_mcp_opencode_bundle.zip`. Они остались в первом коммите истории.

### Миграция

Если клиент запускал `python stepik_mcp.py`, замените команду на `python -m stepik_mcp` после `pip install -e .`, или на путь к `stepik-mcp` в `.venv`.

## [0.1.0] — 2026-09-08

Прототип без тега: 16 инструментов чтения Stepik, локальная папка курса, 55 тестов. Состояние описано в [заметках о проектировании](docs/design-notes.md).
