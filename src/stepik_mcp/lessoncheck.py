"""Проверка урока по карте курса глазами ученика. Только локальные файлы.

Находит подсказки для агента, а не готовые замечания:
- термины, которые карта относит к более поздним урокам («забегание вперёд»);
- отсылки к другим урокам и к будущему материалу;
- слова, которыми пропускают объяснение («очевидно», «как известно»);
- длинные шаги и задания без теории перед ними.
Поиск эвристический: русские слова сравниваются по основе, агент проверяет каждую находку.
"""

import re
from datetime import datetime

from . import coursemap
from . import workspace as sw
from .client import StepikError
from .publish import _manifest_item, parse_lesson
from .steps import load_yaml, split_body

QUOTE = 90
MAX_ITEMS = 15
LONG_STEP_WORDS = 350
VOWEL_ENDINGS = "аеёиоуыэюяйь"

REFERENCES = [
    r"\b(?:в|из|на)\s+(?:прошл|предыдущ|следующ|будущ)\w*\s+(?:урок|модул|тем|глав)\w*",
    r"\b(?:позже|далее|потом|скоро|дальше)\s+(?:мы\s+)?(?:узна|разбер|поговор|увид|изуч|рассмотр|познаком)\w*",
    r"\bкак\s+(?:мы\s+)?(?:уже\s+)?(?:помните|говорили|видели|знаете|изучали|обсуждали|разбирали)\b",
    r"\b(?:в\s+начале\s+курса|в\s+конце\s+курса|об\s+этом\s+(?:позже|потом|далее))",
]
HAND_WAVY = [
    r"\bочевидно\b", r"\bкак\s+известно\b", r"\bнесложно\s+(?:заметить|догадаться|понять|видеть)\b",
    r"\bлегко\s+(?:видеть|заметить|понять|догадаться)\b", r"\bразумеется\b", r"\bсамо\s+собой\b",
    r"\bдогадайтесь\b", r"\bподумайте\s+сами\b", r"\bне\s+будем\s+(?:останавливаться|углубляться)\b",
    r"\bи\s+так\s+(?:понятно|ясно)\b", r"\bэто\s+элементарно\b",
]
# «урок 5.1», «модуле 3»: отсылка, если номер не текущий урок или модуль.
NUMBERED = re.compile(r"\b(урок|модул)\w*\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
CODE_BLOCK = re.compile(r"(?ms)^\s*(`{3,})[^\n]*\n.*?^\s*\1\s*$")
TAG = re.compile(r"<[^>]+>")


def _prose(condition):
    """Текст шага без блоков кода, HTML-тегов и подзаголовков: то, что ученик читает как объяснение."""
    text = TAG.sub(" ", CODE_BLOCK.sub(" ", condition))
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))


def _stem(word):
    """Основа русского слова для поиска форм: «функция» -> «функц», «цикл» -> «цикл»."""
    if re.fullmatch(r"[а-яё]+", word) and len(word) >= 4:
        trimmed = word.rstrip(VOWEL_ENDINGS)
        return trimmed if len(trimmed) >= 3 and len(word) - len(trimmed) <= 2 else word
    return None


def term_pattern(term):
    """Регулярное выражение для термина карты; код в обратных кавычках ищется как есть."""
    plain = term.strip().strip("`").strip()
    if not plain:
        return None
    if term.strip().startswith("`") or not re.fullmatch(r"[\w\s-]+", plain):
        call = re.fullmatch(r"([\w.]+)\(\)", plain)
        if call:
            return re.compile(rf"(?<![\w.]){re.escape(call[1])}\s*\(")
        if re.fullmatch(r"\w+", plain):
            return re.compile(rf"(?<![\w.]){re.escape(plain)}(?!\w)")
        return re.compile(re.escape(plain))
    parts = []
    for word in plain.lower().split():
        stem = _stem(word)
        parts.append(rf"{re.escape(stem)}\w{{0,3}}" if stem else re.escape(word))
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE)


def _quote(text, start, end):
    left = max(0, start - QUOTE // 2)
    snippet = " ".join(text[left:end + QUOTE // 2].split())
    return ("…" if left else "") + snippet[:QUOTE] + "…"


def _steps(text):
    """Шаги урока: номер, step_id, условие, тип, есть ли эталонное решение."""
    steps = []
    for entry in parse_lesson(text):
        if entry["delete"]:
            continue
        condition, yaml_text = split_body(entry["body"])
        kind, solution = "text", False
        if yaml_text is not None:
            try:
                data = load_yaml(yaml_text) or {}
            except Exception:
                data = {}
            kind = str(data.get("type", "unknown")) if isinstance(data, dict) else "unknown"
            solution = isinstance(data, dict) and bool(data.get("solution"))
        steps.append({"number": len(steps) + 1, "step_id": entry["step_id"], "condition": condition,
                      "kind": kind, "solution": solution})
    return steps


def _find(patterns, steps, lesson_position=None):
    """Совпадения в тексте шагов. С lesson_position ищет и «урок 5.1», кроме самого урока и его модуля."""
    found = []
    for step in steps:
        text = _prose(step["condition"])
        matches = [match for pattern in patterns for match in re.finditer(pattern, text, re.IGNORECASE)]
        if lesson_position:
            module = lesson_position.split(".")[0]
            matches += [match for match in NUMBERED.finditer(text)
                        if match[2] != (lesson_position if match[1].lower() == "урок" else module)]
        for match in sorted(matches, key=lambda item: item.start()):
            found.append({"step": step["number"], "quote": _quote(text, match.start(), match.end())})
    return found


def _logic_or_empty(folder):
    path = sw._inside(folder, coursemap.LOGIC_FILE)
    return coursemap.parse_logic(path.read_text(encoding="utf-8")) if path.is_file() else None


def _check(path, lesson_position, logic):
    steps = _steps(path.read_text(encoding="utf-8"))
    current = coursemap._lesson_key(lesson_position)
    logic = logic or {}
    known = {term.lower() for key, entry in logic.items() if coursemap._lesson_key(key) <= current
             for term in coursemap.split_terms(entry.get("terms"))}
    ahead = []
    for key, entry in logic.items():
        if coursemap._lesson_key(key) <= current:
            continue
        for term in coursemap.split_terms(entry.get("terms")):
            pattern = None if term.lower() in known else term_pattern(term)
            if pattern is None:
                continue
            hits = [(step, match) for step in steps for match in [pattern.search(step["condition"])] if match]
            if hits:
                step, match = hits[0]
                ahead.append({
                    "term": term, "introduced_in": key, "steps": [s["number"] for s, _ in hits],
                    "in_tasks": any(s["kind"] != "text" for s, _ in hits),
                    "quote": _quote(step["condition"], match.start(), match.end()),
                })

    tasks = [step for step in steps if step["kind"] != "text"]
    no_theory = [step["number"] for step in tasks
                 if not any(s["kind"] == "text" for s in steps[:step["number"] - 1])]
    words = {step["number"]: len(_prose(step["condition"]).split()) for step in steps}
    return {
        "steps": steps, "ahead": ahead, "tasks": tasks, "no_theory": no_theory, "words": words,
        "long_steps": [{"step": n, "words": c} for n, c in words.items() if c > LONG_STEP_WORDS],
        "references": _find(REFERENCES, steps, lesson_position), "hand_waving": _find(HAND_WAVY, steps),
    }


def check_lesson(course_id, lesson_position):
    folder = coursemap.course_folder(course_id)
    _, path = _manifest_item(folder, course_id, lesson_position)
    logic = _logic_or_empty(folder)
    found = _check(path, lesson_position, logic)
    entry = (logic or {}).get(lesson_position)
    return {
        "status": "ok", "course_id": course_id, "lesson": lesson_position, "path": str(path),
        "step_count": len(found["steps"]), "course_logic": logic is not None, "in_map": entry is not None,
        "map_terms": coursemap.split_terms(entry.get("terms")) if entry else [],
        "ahead_of_map": found["ahead"][:MAX_ITEMS], "ahead_of_map_count": len(found["ahead"]),
        "references": found["references"][:MAX_ITEMS], "references_count": len(found["references"]),
        "hand_waving": found["hand_waving"][:MAX_ITEMS], "hand_waving_count": len(found["hand_waving"]),
        "long_steps": found["long_steps"],
        "tasks": [{"step": s["number"], "type": s["kind"], **({"solution": True} if s["solution"] else {})}
                  for s in found["tasks"]],
        "tasks_without_theory_before": found["no_theory"],
        "note": ("Эвристика: каждую находку проверь по тексту. Термины берутся из «Новые термины» карты; "
                 "у урока без терминов в карте забегание вперёд не найти.")
        if logic is not None else
        "Карты курса нет (stepik_start_editing): забегание вперёд не проверялось, остальное — эвристика.",
    }


def _unique(directory, name):
    path = directory / f"{name}.md"
    number = 2
    while path.exists():
        path = directory / f"{name}-{number}.md"
        number += 1
    return path


COLUMNS = [("lesson", "Урок"), ("steps", "Шагов"), ("words", "Слов"), ("tasks", "Заданий"),
           ("ahead", "Вперёд"), ("references", "Отсылки"), ("hand_waving", "«Очевидно»"),
           ("long_steps", "Длинные шаги"), ("tasks_without_theory", "Задания без теории")]


def check_course(course_id):
    """check_lesson по всем скачанным урокам; сводка в ответе, таблица в sessions/."""
    folder = coursemap.course_folder(course_id)
    manifest = coursemap._read_manifest(folder, course_id)
    logic = _logic_or_empty(folder)
    rows = []
    for key in sorted(manifest["lessons"], key=coursemap._lesson_key):
        path = sw._inside(folder, manifest["lessons"][key]["path"])
        if not path.is_file():
            continue
        found = _check(path, key, logic)
        rows.append({
            "lesson": key, "steps": len(found["steps"]), "words": sum(found["words"].values()),
            "tasks": len(found["tasks"]), "ahead": len(found["ahead"]), "references": len(found["references"]),
            "hand_waving": len(found["hand_waving"]), "long_steps": len(found["long_steps"]),
            "tasks_without_theory": len(found["no_theory"]),
            "ahead_terms": sorted({item["term"] for item in found["ahead"]})[:8],
        })
    if not rows:
        raise StepikError("Нет скачанных уроков: сначала stepik_pull_course или stepik_cache_lesson", status=404)
    sessions = sw._inside(folder, "sessions")
    sessions.mkdir(exist_ok=True)
    report = _unique(sessions, datetime.now().strftime("%Y-%m-%d") + "-course-check")
    lines = [f"# Проверка курса {course_id}", "",
             "Эвристика stepik_check_course: числа показывают, где смотреть, а не готовые замечания.",
             "Подробности по уроку — stepik_check_lesson.", "",
             "| " + " | ".join(title for _, title in COLUMNS) + " |", "|" + " --- |" * len(COLUMNS)]
    lines += ["| " + " | ".join(str(row[name]) for name, _ in COLUMNS) + " |" for row in rows]
    ahead_rows = [row for row in rows if row["ahead_terms"]]
    if ahead_rows:
        lines += ["", "## Термины раньше, чем по карте", ""]
        lines += [f"- {row['lesson']}: {', '.join(row['ahead_terms'])}" for row in ahead_rows]
    sw._atomic_write(report, "\n".join(lines) + "\n")

    def top(column):
        ranked = sorted((row for row in rows if row[column]), key=lambda row: (-row[column], row["lesson"]))
        return [{"lesson": row["lesson"], column: row[column]} for row in ranked[:5]]

    words = sorted(row["words"] for row in rows)
    median = words[len(words) // 2]
    counted = [name for name, _ in COLUMNS[4:]]
    return {
        "status": "ok", "course_id": course_id, "lessons_checked": len(rows),
        "lessons_not_downloaded": len(manifest["lessons"]) - len(rows), "course_logic": logic is not None,
        "report": f"sessions/{report.name}",
        "totals": {name: sum(row[name] for row in rows) for name in counted},
        "top": {name: top(name) for name in counted},
        "longest_lessons": [{"lesson": row["lesson"], "words": row["words"]}
                            for row in sorted(rows, key=lambda row: -row["words"])[:3] if row["words"] > 2 * median],
        "note": f"Только локальные файлы. Длинные уроки — более чем вдвое длиннее медианы курса ({median} слов).",
    }


STYLE_FILE = "COURSE_STYLE.md"


def _style_names(folder):
    """Строки таблицы «Термины и имена» из COURSE_STYLE.md: (термин, [как не называть], с какого урока)."""
    path = sw._inside(folder, STYLE_FILE)
    if not path.is_file():
        return []
    rows, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = "Термин" in line
            continue
        if not inside or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: ") or cells[0].startswith("Термин в курсе"):
            continue
        avoid = [name.strip() for name in re.split(r"[,;]", cells[1]) if name.strip() not in ("", "—", "-")]
        rows.append((cells[0], avoid, cells[2] if len(cells) > 2 else ""))
    return rows


def glossary(course_id):
    """Словарь курса из карты и проверка имён по таблице «Термины и имена» в COURSE_STYLE.md."""
    folder = coursemap.course_folder(course_id)
    manifest = coursemap._read_manifest(folder, course_id)
    logic = coursemap.read_logic(folder)
    introduced = {}
    for key in sorted(logic, key=coursemap._lesson_key):
        for term in coursemap.split_terms(logic[key].get("terms")):
            introduced.setdefault(term.lower(), {"term": term, "lessons": []})["lessons"].append(key)
    style = _style_names(folder)

    lessons = [(key, sw._inside(folder, manifest["lessons"][key]["path"]))
               for key in sorted(manifest["lessons"], key=coursemap._lesson_key)]
    texts = [(key, _steps(path.read_text(encoding="utf-8"))) for key, path in lessons if path.is_file()]
    wrong = []
    for name, avoid, _ in style:
        for bad in avoid:
            pattern = term_pattern(bad)
            for key, steps in texts if pattern else []:
                for step in steps:
                    text = _prose(step["condition"])
                    match = pattern.search(text)
                    if match:
                        wrong.append({"use": name, "found": bad, "lesson": key, "step": step["number"],
                                      "quote": _quote(text, match.start(), match.end())})

    lines = [f"# Словарь курса {course_id}", "",
             "> Генерируется stepik_glossary из COURSE_LOGIC.md и COURSE_STYLE.md. Не редактируйте вручную:",
             "> термины меняются в карте курса, нежелательные имена — в таблице «Термины и имена».", "",
             "| Термин | Введён в уроке | Ещё раз в карте |", "| --- | --- | --- |"]
    for entry in sorted(introduced.values(), key=lambda item: coursemap._lesson_key(item["lessons"][0])):
        lines.append(f"| {entry['term']} | {entry['lessons'][0]} | {', '.join(entry['lessons'][1:]) or '—'} |")
    if style:
        lines += ["", "## Как называть", "", "| Термин | Как не называть | С какого урока |", "| --- | --- | --- |"]
        lines += [f"| {name} | {', '.join(avoid) or '—'} | {start or '—'} |" for name, avoid, start in style]
    target = sw._inside(folder, "GLOSSARY.md")
    sw._atomic_write(target, "\n".join(lines) + "\n", replace=True)
    duplicates = [{"term": entry["term"], "lessons": entry["lessons"]}
                  for entry in introduced.values() if len(entry["lessons"]) > 1]
    return {
        "status": "ok", "course_id": course_id, "path": str(target), "terms": len(introduced),
        "introduced_twice": duplicates[:MAX_ITEMS],
        "wrong_names": wrong[:MAX_ITEMS], "wrong_names_count": len(wrong), "lessons_searched": len(texts),
        "note": "Нежелательные имена берутся из столбца «Как не называть» таблицы «Термины и имена» в "
                "COURSE_STYLE.md." if style else
                "В COURSE_STYLE.md нет строк таблицы «Термины и имена»: имена не проверялись, только словарь.",
    }
