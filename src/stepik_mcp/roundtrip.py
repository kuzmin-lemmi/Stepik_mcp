"""Проверка «скачать → собрать обратно» для всех шагов курса.

Каждый шаг проходит тот же путь, что и при работе автора: step-source ->
тело шага в файле урока -> разбор файла -> блок для Stepik. Блок должен
совпасть с исходным до последнего поля. Только GET к Stepik.
"""

from collections import Counter

from . import steps
from .publish import _edit_losses, _fence_after, parse_lesson, structure_changes

MAX_FAILURES = 20


def check_sources(sources):
    totals, passed = Counter(), Counter()
    failures = []
    lossy = 0
    restructured = 0
    hidden_templates = 0
    for source in sources:
        block = source.get("block") or {}
        kind = block.get("name", "unknown")
        totals[kind] += 1
        body = steps.to_markdown(source)
        lesson = f"---\nformat: {steps.FORMAT}\n---\n\n<!-- step_id: {source['id']} -->\n## Шаг | {kind}\n\n{body}\n"
        try:
            fence = None
            for line in body.split("\n"):
                fence = _fence_after(fence, line)
            if fence:
                raise ValueError("в теле шага не закрыт блок кода")
            entries = parse_lesson(lesson)
            if len(entries) != 1 or entries[0]["body"] != body.strip():
                raise ValueError("файл урока разобран не так, как записан")
            rebuilt = steps.build_block(block, entries[0]["body"])
            diff = steps.changed_paths(block, rebuilt)
        except Exception as exc:  # отчёт должен назвать шаг, а не упасть на первом
            diff = [f"{type(exc).__name__}: {exc}"]
        if diff:
            if len(failures) < MAX_FAILURES:
                failures.append({"step_id": source["id"], "lesson_id": source.get("lesson"), "type": kind,
                                 "fields": diff[:5]})
        else:
            passed[kind] += 1
        if _edit_losses(block.get("text")):
            lossy += 1
        if block.get("text") and structure_changes(block.get("text")):
            restructured += 1
        if kind == "code" and "templates" not in steps.settings(block):
            hidden_templates += 1
    failed = sum(totals.values()) - sum(passed.values())
    return {
        "step_count": sum(totals.values()), "passed": sum(passed.values()), "failed": failed,
        "by_type": {kind: f"{passed[kind]}/{count}" for kind, count in totals.most_common()},
        "without_schema": sorted(kind for kind in totals if kind != "text" and not steps.has_schema(kind)),
        "failures": failures,
        "steps_losing_formatting_on_edit": lossy,
        "steps_changing_structure_on_edit": restructured,
        "code_steps_with_raw_templates": hidden_templates,
    }


def check_course(client, course_id):
    course = client.by_ids("courses", [course_id])
    if not course:
        return {"course_id": course_id, "status": "error", "message": "Курс не найден или недоступен"}
    sections = client.by_ids("sections", course[0].get("sections") or [])
    units = client.by_ids("units", [unit for section in sections for unit in section.get("units") or []])
    lessons = client.by_ids("lessons", [unit.get("lesson") for unit in units])
    sources = client.by_ids("step-sources", [sid for lesson in lessons for sid in lesson.get("steps") or []])
    report = check_sources(sources)
    return {
        "course_id": course_id, "lesson_count": len(lessons),
        "status": "ok" if report["failed"] == 0 else "mismatch", **report,
        "note": "Только чтение. passed — шаг собирается обратно без единого изменённого поля. "
                "without_schema — типы, у которых в YAML весь source без удобных полей. "
                "steps_losing_formatting_on_edit — шаги, у которых правка условия потеряет ссылки, таблицы "
                "или оформление.",
    }
