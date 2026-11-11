"""Проверка установки при первом запуске: ключи, авторизация, папка курсов, публикация.

Значения секретов в ответ не попадают: только «задан / не задан». К Stepik идёт
один GET (тот же, что у stepik_whoami), и только если ключи заданы.
"""

import json
import os
import sys
from pathlib import Path

from . import __version__
from . import workspace as sw
from .client import StepikError

SETUP_DOC = "https://github.com/kuzmin-lemmi/Stepik_mcp/blob/main/docs/SETUP.md"
MAX_COURSES = 20


def _courses(root):
    found = []
    for folder in sorted(root.glob("course-*")):
        index = folder / ".course.json"
        if not index.is_file():
            continue
        try:
            manifest = json.loads(index.read_text(encoding="utf-8"))
            lessons = manifest.get("lessons") or {}
            downloaded = sum(1 for item in lessons.values() if (folder / item["path"]).is_file())
        except (ValueError, KeyError, TypeError, OSError):
            found.append({"folder": folder.name, "status": "повреждён .course.json"})
            continue
        sessions = folder / "sessions"
        handoffs = sorted(path.name for path in sessions.glob("*handoff*.md")) if sessions.is_dir() else []
        found.append({
            "course_id": manifest.get("course_id"), "title": manifest.get("title", ""),
            "lessons": len(lessons), "downloaded": downloaded,
            "course_logic": (folder / "COURSE_LOGIC.md").is_file(),
            **({"last_handoff": f"sessions/{handoffs[-1]}"} if handoffs else {}),
        })
    return found


def setup_check(client):
    problems, steps = [], []
    keys = {name: bool(os.environ.get(name)) for name in ("STEPIK_CLIENT_ID", "STEPIK_CLIENT_SECRET")}
    auth = {"status": "not_checked"}
    if not all(keys.values()):
        missing = ", ".join(name for name, present in keys.items() if not present)
        problems.append(f"Не заданы ключи Stepik: {missing}.")
        steps.append("Попроси пользователя вписать Client ID и Client secret в конфиг клиента (раздел env сервера "
                     "stepik) и перезапустить клиент. Ключи не присылают в чат.")
    else:
        try:
            payload = client.get("stepics/1")
            user = (payload.get("users") or [{}])[0]
            auth = {"status": "ok", "account": user.get("full_name"), "user_id": user.get("id")}
        except StepikError as exc:
            auth = {"status": "error", "http_status": exc.status, "message": str(exc)[:200]}
            problems.append("Авторизация в Stepik не прошла: проверь Client ID и Client secret и тип приложения "
                            "(Confidential, Client credentials).")
        except Exception as exc:
            auth = {"status": "error", "message": f"{type(exc).__name__}"}
            problems.append("Не удалось связаться со Stepik: проверь интернет.")

    root = sw.workspace_root()
    explicit = bool(os.environ.get("COURSE_WORKSPACE_ROOT"))
    courses = _courses(root) if root.is_dir() else []
    if not root.is_dir():
        steps.append(f"Папки курсов {root} ещё нет. Если на старом компьютере была папка курсов, скопируй её сюда "
                     "целиком, чтобы сохранить карту курса, отчёты и неопубликованные правки; иначе она "
                     "создастся при первом скачивании урока.")
    elif not courses:
        steps.append(f"В {root} нет скачанных курсов. Перенеси папку курсов со старого компьютера или скачай курс "
                     "(stepik_my_courses, затем stepik_pull_course).")

    allowed = os.environ.get("ALLOWED_WRITE_COURSES", "").strip()
    if not allowed:
        problems.append("ALLOWED_WRITE_COURSES не задан: публикация на Stepik запрещена (чтение работает).")
        steps.append("Чтобы публиковать в свои курсы, добавь в env сервера ALLOWED_WRITE_COURSES=* (или ID курсов "
                     "через запятую) и перезапусти клиент.")
    if not problems and courses:
        steps.append("Всё готово. Предложи пользователю режим работы (подсказка stepik_start); если у курса есть "
                     "last_handoff — продолжить с последней передачи.")
    elif not problems:
        steps.append("Ключи и публикация настроены. Дальше — скачать курс или перенести папку курсов.")

    return {
        "status": "ok" if not problems else "needs_attention",
        "server": {"version": __version__, "python": sys.version.split()[0],
                   "package_path": str(Path(__file__).resolve().parents[1])},
        "keys": {name: "задан" if present else "не задан" for name, present in keys.items()},
        "auth": auth,
        "workspace": {"path": str(root), "exists": root.is_dir(),
                      "from": "COURSE_WORKSPACE_ROOT" if explicit else "по умолчанию",
                      "courses": courses[:MAX_COURSES], "course_count": len(courses)},
        "publishing": {"allowed_write_courses": allowed or "не задан", "enabled": bool(allowed)},
        "problems": problems,
        "next": steps,
        "setup_guide": SETUP_DOC,
    }
