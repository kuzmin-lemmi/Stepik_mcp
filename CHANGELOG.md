# История изменений

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии следуют [SemVer](https://semver.org/lang/ru/).

## [Unreleased]

### Добавлено

- Публикация текстовых шагов и картинок: `stepik_plan_push` строит план и diff, ничего не отправляя, а `stepik_apply_push` публикует его после подтверждения автора. Можно менять существующие текстовые шаги и добавлять новые (метка `<!-- new_step -->`). Локальные картинки загружаются на Stepik как вложения урока.
- Защита записи: список курсов `ALLOWED_WRITE_COURSES` (по умолчанию пуст), проверка принадлежности урока курсу по API, отказ при изменении файла или урока на Stepik после плана, снимок урока в `snapshots/`, журнал `logs/stepik-write.log`, предупреждение о ссылках и оформлении, которые потеряются при публикации.
- Аннотации MCP: `stepik_apply_push` помечен как разрушающий, `stepik_plan_push` — как только читающий Stepik.
- 18 тестов публикации и Markdown → HTML. Публикация проверена на тестовом курсе 241862.

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
