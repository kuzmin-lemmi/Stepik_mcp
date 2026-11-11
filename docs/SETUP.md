# Установка на новом компьютере

Пошаговая инструкция для человека и для ИИ-агента. Агент, которого попросили «установи сервер Stepik» или «подключи MCP Stepik», выполняет шаги по порядку и после каждого проверяет результат.

## Правила для агента

- **Ключи Stepik не проходят через чат.** Не просите пользователя прислать `Client secret` в сообщении. Пишите конфиг с заполнителями `<client_id>` и `<client_secret>` и попросите пользователя вставить значения самому.
- **Чужой конфиг не затирается.** Если файл настроек клиента уже есть, сначала сделайте его копию рядом (`*.bak`), затем добавьте только раздел `stepik`, не трогая остальные серверы.
- **Папка курсов не перезаписывается.** Если в `COURSE_WORKSPACE_ROOT` уже есть папки `course-*`, ничего в них не меняйте и не скачивайте курс заново поверх.
- После установки вызовите `stepik_setup_check`: он покажет, чего не хватает, без секретов в ответе.

## 1. Python и Git

Нужен Python 3.12 или новее и Git. Проверка:

```bash
python --version
```

```bash
git --version
```

## 2. Скачать и установить сервер

```bash
git clone https://github.com/kuzmin-lemmi/Stepik_mcp.git
```

```bash
cd Stepik_mcp
```

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Linux и macOS:

```bash
.venv/bin/python -m pip install -e .
```

Проверка: сервер загружается и регистрирует инструменты.

```powershell
.\.venv\Scripts\python.exe -c "import asyncio, stepik_mcp.server as s; print(len(asyncio.run(s.mcp.list_tools())), 'tools')"
```

Команда запуска сервера для клиента: `<папка>\Stepik_mcp\.venv\Scripts\stepik-mcp.exe` в Windows или `<папка>/Stepik_mcp/.venv/bin/stepik-mcp` в Linux и macOS. Нужен абсолютный путь.

## 3. Ключи Stepik

Подойдут ключи со старого компьютера: они записаны в конфиге клиента там (например, в `claude_desktop_config.json`). Новые ключи создаются на [stepik.org/oauth2/applications](https://stepik.org/oauth2/applications/):

- **Client type:** `Confidential`
- **Authorization grant type:** `Client credentials`

## 4. Подключить сервер к клиенту

Сервер получает четыре переменные окружения:

| Переменная | Значение |
| --- | --- |
| `STEPIK_CLIENT_ID` | Client ID |
| `STEPIK_CLIENT_SECRET` | Client secret |
| `COURSE_WORKSPACE_ROOT` | Абсолютный путь к папке курсов, например `C:\Courses` |
| `ALLOWED_WRITE_COURSES` | `*` — публиковать в любой свой курс; пусто — публикация запрещена |

**Claude Desktop** — `%APPDATA%\Claude\claude_desktop_config.json` в Windows, `~/Library/Application Support/Claude/claude_desktop_config.json` в macOS:

```json
{
  "mcpServers": {
    "stepik": {
      "command": "C:\\путь\\к\\Stepik_mcp\\.venv\\Scripts\\stepik-mcp.exe",
      "env": {
        "STEPIK_CLIENT_ID": "<client_id>",
        "STEPIK_CLIENT_SECRET": "<client_secret>",
        "COURSE_WORKSPACE_ROOT": "C:\\Courses",
        "ALLOWED_WRITE_COURSES": "*"
      }
    }
  }
}
```

**OpenCode** — `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "stepik": {
      "type": "local",
      "command": ["C:\\путь\\к\\Stepik_mcp\\.venv\\Scripts\\stepik-mcp.exe"],
      "enabled": true,
      "environment": {
        "STEPIK_CLIENT_ID": "<client_id>",
        "STEPIK_CLIENT_SECRET": "<client_secret>",
        "COURSE_WORKSPACE_ROOT": "C:\\Courses",
        "ALLOWED_WRITE_COURSES": "*"
      }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add stepik --scope user -e STEPIK_CLIENT_ID=<client_id> -e STEPIK_CLIENT_SECRET=<client_secret> -e COURSE_WORKSPACE_ROOT=C:\Courses -e ALLOWED_WRITE_COURSES=* -- C:\путь\к\Stepik_mcp\.venv\Scripts\stepik-mcp.exe
```

**DeepSeek Harness** — сервер добавляется командой `/mcp` или во вкладке настроек серверов: та же команда запуска и те же четыре переменные. Формат этого окна в документации DeepSeek пока не описан, проверьте его в своей версии.

После изменения конфига перезапустите клиент.

## 5. Перенести папку курсов

Карта курса, решения автора, отчёты, версии уроков и неопубликованные правки хранятся только в папке курсов на старом компьютере. Чтобы продолжить с того же места, скопируйте её целиком (например, `C:\Courses`) в путь из `COURSE_WORKSPACE_ROOT`.

Что лежит в папке курса и зачем:

- `COURSE_LOGIC.md`, `DECISIONS.md`, `COURSE_STYLE.md`, `rules/` — карта курса, решения и правила автора;
- `sessions/` — рецензии, отчёты ученика-тестировщика, передачи работы;
- `snapshots/`, `snapshots/local/`, `metrics/` — снимки перед публикацией, версии уроков, показатели;
- файлы уроков `*.md` — в том числе правки, ещё не опубликованные на Stepik.

Без переноса курс можно скачать заново (`stepik_pull_course`), но будет только опубликованное на Stepik.

## 6. Проверить

Попросите агента:

1. «Проверь установку» — инструмент `stepik_setup_check`: ключи заданы, авторизация работает, папка курсов найдена, публикация разрешена.
2. «С чего начать?» или команда `stepik_start` — агент предложит режим: скачать уроки, править курс вместе или пройти его учеником.

## Обновление

```bash
git pull
```

Затем перезапустите клиент. Если в `pyproject.toml` появились новые зависимости, повторите `pip install -e .` из шага 2.
