"""Совместная работа над курсом: карта курса COURSE_LOGIC.md, режимы и состояние работ.

COURSE_MAP.md генерирует загрузчик (список файлов), а COURSE_LOGIC.md — рабочий
документ автора: о чём курс, модули и уроки, на что опирается каждый урок.
Файл создаётся один раз и дальше не перезаписывается; сервер только читает его
и сообщает, каких уроков в карте нет и что не заполнено.
"""

import json
import re

from .client import StepikError
from . import workspace as sw

LOGIC_FILE = "COURSE_LOGIC.md"
FIELDS = {
    "learns": "Что ученик узнаёт:",
    "depends": "Опирается на:",
    "terms": "Новые термины:",
    "tasks": "Задания:",
    "state": "Состояние:",
}
LEARNS = FIELDS["learns"]
LESSON_HEADING = re.compile(r"^###\s+(\d+\.\d+)\.?\s*(.*)$")
REPORT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(review|student|second)-(\d+\.\d+)\b.*\.md$")
HANDOFF_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}.*handoff.*\.md$")
MODES = ("edit", "student")
LIMIT = 30

# Сводка правил для ответа инструмента: старые папки курса хранят прежний AGENTS.md.
COMMON_RULES = [
    "Если в ответе есть last_handoff, прочитай эту передачу и предложи автору продолжить с того места.",
    "Решения автора записывай в DECISIONS.md, чтобы не предлагать их заново.",
    "В чат пиши коротко: номер замечания, где, суть одной строкой и буквы вариантов. Полный текст — в файле отчёта.",
    "Что делать дальше по курсу, показывает stepik_course_status.",
    "В конце сессии напиши передачу в sessions/ по sessions/HANDOFF_TEMPLATE.md.",
]
EDIT_RULES = [
    "Перед проверкой или правкой урока прочитай COURSE_LOGIC.md, COURSE_STYLE.md, DECISIONS.md и нужные файлы rules/.",
    "Пустые места карты заполняй по тексту уроков и показывай автору; цели курса и модулей подтверждает автор.",
    "Перед рецензией вызови stepik_check_lesson: он находит термины из будущих уроков, отсылки и «очевидно».",
    "REVIEW: замечания с местом (шаг, step_id, цитата) и вариантами A/B/C пиши в sessions/ГГГГ-ММ-ДД-review-<урок>.md, "
    "урок не меняй. Если есть отчёт ученика (sessions/*-student-<урок>.md), начни с его находок.",
    "Жди ответа автора вида «1A, 2 — свой вариант, 3 пропусти». Принятые пункты вноси все сразу, отметь их "
    "в отчёте, затем один stepik_plan_push и один общий diff.",
    "Задание опирается только на то, что было в этом уроке и в предыдущих по карте. Для задачи на код добавь "
    "эталонное решение (solution: в YAML-блоке) — stepik_plan_push прогонит его на тестах.",
    "После правки урока обнови его запись в COURSE_LOGIC.md (термины, задания, «Состояние: исправлен»).",
    "Публикация: stepik_plan_push → показать diff → явное «да» → stepik_apply_push.",
]
STUDENT_RULES = [
    "Ты ученик, а не редактор: проходи уроки по порядку и знай только то, что было в пройденных уроках.",
    "Критерии проверки — rules/STUDENT_TEST_RULES.md. Карту COURSE_LOGIC.md используй, чтобы понять, "
    "что ученик уже прошёл и что вводится позже, а не чтобы додумывать урок.",
    "Задания решай сам только по материалу урока. Не смог решить или пришлось догадываться — это находка.",
    "stepik_check_lesson подскажет термины из будущих уроков, отсылки и «очевидно»; проверь их глазами ученика.",
    "Отчёт пиши в sessions/ГГГГ-ММ-ДД-student-<урок>.md: номер, где (шаг и цитата), что случилось с учеником, "
    "тяжесть (блокер / мешает / мелочь), критерий. Урок и карту не меняй.",
    "После отчёта предложи автору перейти к правке: режим «править курс вместе» начнёт с находок ученика.",
]


def _lesson_key(key):
    return tuple(int(part) for part in key.split("."))


def logic_skeleton(manifest, titles):
    """Заготовка карты по структуре курса. titles: {lesson_id: название}."""
    lines = [
        f"# Карта курса: {manifest['title']}", "",
        "> Ведут агент и автор вместе. Агент читает карту перед проверкой или правкой урока",
        "> и обновляет запись урока после правки. Цели курса и модулей подтверждает автор.",
        "> Новые термины пишите через запятую, код — в обратных кавычках: `print()`, `for`.",
        "> Сервер этот файл не перезаписывает.", "",
        "## О курсе", "",
        "- **Цель курса:**",
        "- **Для кого:**",
        "- **Что ученик умеет в конце:**",
        "- **Сквозной пример или проект:**", "",
    ]
    for module in manifest["modules"]:
        lines += [f"## Модуль {module['position']}. {module['title']}", "",
                  "- **Зачем модуль:**",
                  "- **Что ученик умеет после модуля:**", ""]
        keys = sorted((key for key, item in manifest["lessons"].items()
                       if item["section_id"] == module["section_id"]), key=_lesson_key)
        for key in keys:
            item = manifest["lessons"][key]
            title = titles.get(item["lesson_id"]) or item.get("title") or f"урок {item['lesson_id']}"
            lines += [f"### {key}. {title}", ""]
            lines += [f"- **{label}**" for name, label in FIELDS.items() if name != "state"]
            lines += [f"- **{FIELDS['state']}** не проверен", ""]
    return "\n".join(lines)


def parse_logic(text):
    """Записи уроков карты: {"5.2": {"title": ..., "learns": ..., "terms": ..., ...}} в порядке файла."""
    lessons, current = {}, None
    for line in text.splitlines():
        heading = LESSON_HEADING.match(line)
        if heading:
            current = lessons.setdefault(heading[1], {"title": heading[2].strip()})
            continue
        if line.startswith("#"):
            current = None
        elif current is not None:
            for name, label in FIELDS.items():
                if label in line and name not in current:
                    current[name] = line.split(label, 1)[1].replace("**", "").strip()
    return lessons


def logic_gaps(text):
    """Уроки из карты и те, у которых не заполнено «Что ученик узнаёт»."""
    lessons = parse_logic(text)
    return list(lessons), [key for key, entry in lessons.items() if not entry.get("learns")]


def split_terms(value):
    """«список (list), `len()`; срез» -> ["список", "list", "`len()`", "срез"]."""
    terms = []
    for part in re.split(r"[,;]", value or ""):
        part = part.strip().rstrip(".")
        if part in ("", "-", "—", "нет"):
            continue
        if part.startswith("`"):
            terms.append(part)
            continue
        inner = re.findall(r"\(([^)]+)\)", part)
        outer = re.sub(r"\([^)]+\)", "", part).strip()
        for term in [outer, *inner]:
            if term.strip():
                terms.append(term.strip())
    return terms


def _read_manifest(folder, course_id):
    index = sw._inside(folder, ".course.json")
    try:
        manifest = json.loads(index.read_text(encoding="utf-8"))
        if manifest["version"] != 1 or manifest["course_id"] != course_id:
            raise ValueError("version or course ID mismatch")
        if not isinstance(manifest["modules"], list) or not isinstance(manifest["lessons"], dict):
            raise ValueError("invalid structure")
        for module in manifest["modules"]:
            sw._inside(folder, module["path"])
        for item in manifest["lessons"].values():
            sw._inside(folder, item["path"])
    except (ValueError, KeyError, TypeError) as exc:
        raise StepikError("Invalid .course.json; existing files were not overwritten") from exc
    return manifest


def _manifest(client, folder, course_id):
    index = sw._inside(folder, ".course.json")
    if index.exists():
        return _read_manifest(folder, course_id)
    lock = sw._inside(folder, ".stepik-cache.lock")
    if any(path != lock and path.name not in sw.SERVICE_ENTRIES for path in folder.iterdir()):
        raise StepikError("Course folder is not empty but has no .course.json; manual inspection required")
    manifest = sw.course_structure(client, course_id)
    sw._atomic_write(index, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def course_folder(course_id):
    """Папка уже скачанного курса; для локальных инструментов без запросов к Stepik."""
    if type(course_id) is not int or course_id < 1:
        raise StepikError("course_id must be a positive integer", status=400)
    folder = sw._inside(sw.workspace_root(), f"course-{course_id}")
    if not sw._inside(folder, ".course.json").is_file():
        raise StepikError("Папка курса не создана: сначала stepik_start_editing или stepik_cache_lesson", status=404)
    return folder


def read_logic(folder):
    path = sw._inside(folder, LOGIC_FILE)
    if not path.is_file():
        raise StepikError("Карты курса нет: сначала вызовите stepik_start_editing", status=404)
    return parse_logic(path.read_text(encoding="utf-8"))


def last_handoff(folder):
    sessions = sw._inside(folder, "sessions")
    if not sessions.is_dir():
        return None
    names = sorted(path.name for path in sessions.iterdir() if path.is_file() and HANDOFF_NAME.match(path.name))
    return f"sessions/{names[-1]}" if names else None


def start_editing(client, course_id, mode="edit"):
    """Готовит папку курса к совместной работе и возвращает, что в карте ещё не заполнено."""
    if type(course_id) is not int or course_id < 1:
        raise StepikError("course_id must be a positive integer", status=400)
    if mode not in MODES:
        raise StepikError(f"mode: {' или '.join(MODES)}", status=400)
    root = sw.workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = sw._inside(root, f"course-{course_id}")
    folder.mkdir(exist_ok=True)
    lock = sw._inside(folder, ".stepik-cache.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Course cache is busy. If a process crashed, inspect .stepik-cache.lock before removing it.") from None
    try:
        manifest = _manifest(client, folder, course_id)
        for module in manifest["modules"]:
            sw._inside(folder, module["path"]).mkdir(exist_ok=True)
        sw._write_instructions(folder)
        sw.save_index(folder, manifest)

        logic = sw._inside(folder, LOGIC_FILE)
        if logic.exists():
            status = "existing"
            text = logic.read_text(encoding="utf-8")
        else:
            status = "created"
            ids = [item["lesson_id"] for item in manifest["lessons"].values()]
            lessons = sw._objects(client, "lessons", ids) if ids else {}
            text = logic_skeleton(manifest, {lid: row.get("title", "") for lid, row in lessons.items()})
            sw._atomic_write(logic, text)

        present, empty = logic_gaps(text)
        order = sorted(manifest["lessons"], key=_lesson_key)
        missing = [key for key in order if key not in present]
        downloaded = [key for key in order if sw._inside(folder, manifest["lessons"][key]["path"]).is_file()]
        handoff = last_handoff(folder)
        if mode == "edit":
            read_first = [LOGIC_FILE, "COURSE_STYLE.md", "DECISIONS.md", "rules/REVIEW_RULES.md"]
            rules = EDIT_RULES + COMMON_RULES
            hint = "Спроси автора, с какого урока начать."
        else:
            read_first = ["rules/STUDENT_TEST_RULES.md", LOGIC_FILE]
            rules = STUDENT_RULES + COMMON_RULES
            hint = "Спроси автора, какие уроки пройти (по умолчанию — с первого по порядку)."
        return {
            "status": "ready", "mode": mode, "course_id": course_id, "course_path": str(folder),
            "course_logic": {"path": str(logic), "status": status},
            "lesson_count": len(order), "downloaded_lessons": len(downloaded),
            "lessons_without_summary": empty[:LIMIT], "lessons_without_summary_count": len(empty),
            "lessons_missing_in_map": missing[:LIMIT], "lessons_missing_in_map_count": len(missing),
            **({"last_handoff": handoff} if handoff else {}),
            "read_first": read_first,
            "rules": rules,
            "next": f"{hint} Уроки скачивай stepik_cache_lesson или весь курс stepik_pull_course; "
                    "пустые места карты заполняй по мере чтения уроков.",
        }
    finally:
        lock.rmdir()


def _comment_count(path):
    comments = path.with_name(path.name[:-3] + ".comments.md")
    if not comments.is_file():
        return None
    return sum(1 for line in comments.read_text(encoding="utf-8").splitlines() if line.startswith("### comment "))


def course_status(course_id):
    """Что сделано по курсу и что дальше. Только локальные файлы, без запросов к Stepik."""
    folder = course_folder(course_id)
    manifest = _read_manifest(folder, course_id)
    logic_path = sw._inside(folder, LOGIC_FILE)
    logic = parse_logic(logic_path.read_text(encoding="utf-8")) if logic_path.is_file() else {}

    reports = {}
    sessions = sw._inside(folder, "sessions")
    if sessions.is_dir():
        for path in sorted(sessions.iterdir()):
            match = REPORT_NAME.match(path.name)
            if match and path.is_file():
                reports.setdefault(match[3], {})[match[2]] = f"sessions/{path.name}"

    order = sorted(manifest["lessons"], key=_lesson_key)
    by_state, not_downloaded, commented = {}, [], []
    student_only, unchecked = [], []
    for key in order:
        path = sw._inside(folder, manifest["lessons"][key]["path"])
        if not path.is_file():
            not_downloaded.append(key)
        else:
            count = _comment_count(path)
            if count:
                commented.append((count, key))
        state = (logic.get(key, {}).get("state") or "нет в карте").lower()
        by_state.setdefault(state, []).append(key)
        done = reports.get(key, {})
        if "student" in done and "review" not in done:
            student_only.append(key)
        if not done and state in ("не проверен", "нет в карте"):
            unchecked.append(key)

    commented.sort(key=lambda pair: (-pair[0], _lesson_key(pair[1])))
    without_summary = [key for key in order if key in logic and not logic[key].get("learns")]
    suggestions = []
    if student_only:
        suggestions.append(f"Перейти к правке по отчётам ученика: {', '.join(student_only[:5])}")
    if commented:
        suggestions.append(f"Посмотреть уроки с комментариями: {', '.join(key for _, key in commented[:3])}")
    if unchecked:
        suggestions.append(f"Пройти учеником или проверить: {', '.join(unchecked[:5])}")
    if without_summary:
        suggestions.append(f"Заполнить карту для уроков: {', '.join(without_summary[:5])}")
    handoff = last_handoff(folder)
    return {
        "status": "ok", "course_id": course_id, "course_path": str(folder),
        "lesson_count": len(order), "course_logic": logic_path.is_file(),
        **({"last_handoff": handoff} if handoff else {}),
        "by_state": {state: keys[:LIMIT] for state, keys in by_state.items()},
        "not_downloaded": not_downloaded[:LIMIT], "not_downloaded_count": len(not_downloaded),
        "lessons_without_summary": without_summary[:LIMIT],
        "reports": {key: reports[key] for key in order if key in reports},
        "most_commented": [{"lesson": key, "comments": count} for count, key in commented[:5]],
        "next": suggestions or ["Все уроки проверены; можно пройти курс учеником ещё раз после правок."],
        "note": "Комментарии посчитаны только по сохранённым .comments.md (stepik_cache_lesson_comments).",
    }
