"""Проверка урока по карте курса глазами ученика. Только локальные файлы.

Находит подсказки для агента, а не готовые замечания:
- термины, которые карта относит к более поздним урокам («забегание вперёд»);
- отсылки к другим урокам и к будущему материалу;
- слова, которыми пропускают объяснение («очевидно», «как известно»);
- длинные шаги и задания без теории перед ними.
Поиск эвристический: русские слова сравниваются по основе, агент проверяет каждую находку.
"""

import re

from . import coursemap
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


def check_lesson(course_id, lesson_position):
    folder = coursemap.course_folder(course_id)
    item, path = _manifest_item(folder, course_id, lesson_position)
    logic = coursemap.read_logic(folder)
    steps = _steps(path.read_text(encoding="utf-8"))
    current = coursemap._lesson_key(lesson_position)

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
    no_theory = []
    for step in tasks:
        before = steps[:step["number"] - 1]
        if not any(s["kind"] == "text" for s in before):
            no_theory.append(step["number"])
    words = {step["number"]: len(_prose(step["condition"]).split()) for step in steps}
    long_steps = [{"step": number, "words": count} for number, count in words.items() if count > LONG_STEP_WORDS]
    entry = logic.get(lesson_position)
    references = _find(REFERENCES, steps, lesson_position)
    hand_waving = _find(HAND_WAVY, steps)
    return {
        "status": "ok", "course_id": course_id, "lesson": lesson_position, "path": str(path),
        "step_count": len(steps), "in_map": entry is not None,
        "map_terms": coursemap.split_terms(entry.get("terms")) if entry else [],
        "ahead_of_map": ahead[:MAX_ITEMS], "ahead_of_map_count": len(ahead),
        "references": references[:MAX_ITEMS], "references_count": len(references),
        "hand_waving": hand_waving[:MAX_ITEMS], "hand_waving_count": len(hand_waving),
        "long_steps": long_steps,
        "tasks": [{"step": s["number"], "type": s["kind"], **({"solution": True} if s["solution"] else {})}
                  for s in tasks],
        "tasks_without_theory_before": no_theory,
        "note": "Эвристика: каждую находку проверь по тексту. Термины берутся из «Новые термины» карты; "
                "у урока без терминов в карте забегание вперёд не найти.",
    }
