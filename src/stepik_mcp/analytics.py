"""Статистика урока и шага, пакет для AI-редактора. Только чтение."""

import json
import math

from .client import StepikError, first_value
from .feedback import (
    render_lesson_comments,
    render_wrong_submissions,
    step_submissions,
)
from .render import render_lesson


def _fmt_number(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        if 0 <= value <= 1:
            return f"{value * 100:.1f}%"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _status_counts(submissions):
    counts = {}
    for s in submissions:
        status = str(s.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _metric_scan(obj, terms, path="", depth=0, max_depth=4):
    """Find small scalar/list statistics in API payloads without dumping content."""
    found = []
    if depth > max_depth:
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else key
            low = key.lower()
            if any(term in low for term in terms):
                if isinstance(value, (str, int, float, bool)) or value is None:
                    found.append((here, value))
                elif isinstance(value, list) and len(value) <= 10 and all(
                    isinstance(x, (str, int, float, bool, type(None))) for x in value
                ):
                    found.append((here, value))
                elif isinstance(value, dict) and len(value) <= 10 and all(
                    isinstance(x, (str, int, float, bool, type(None))) for x in value.values()
                ):
                    found.append((here, value))
            if isinstance(value, (dict, list)):
                found.extend(_metric_scan(value, terms, here, depth + 1, max_depth))
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:20]):
            if isinstance(value, (dict, list)):
                found.extend(_metric_scan(value, terms, f"{path}[{i}]", depth + 1, max_depth))
    # preserve order but remove exact duplicates
    result = []
    seen = set()
    for item in found:
        sig = (item[0], repr(item[1]))
        if sig not in seen:
            seen.add(sig)
            result.append(item)
    return result


def _course_units_for_lesson(client, course_id, lesson_id):
    courses = client.get("courses", ids=[course_id]).get("courses", []) or []
    if not courses:
        raise StepikError(f"Курс {course_id} не найден или недоступен", status=404)
    section_ids = courses[0].get("sections")
    if not isinstance(section_ids, list):
        raise StepikError(f"API не вернул структуру курса {course_id}")
    sections = client.by_ids("sections", section_ids, key="sections")
    if set(section_ids) - {s.get("id") for s in sections}:
        raise StepikError(f"Не все секции курса {course_id} доступны; принадлежность урока не проверена")
    unit_ids = []
    for section in sections:
        if not isinstance(section.get("units"), list):
            raise StepikError(f"API не вернул список уроков секции {section.get('id')}")
        unit_ids.extend(section["units"])
    units = client.by_ids("units", unit_ids, key="units")
    if set(unit_ids) - {u.get("id") for u in units}:
        raise StepikError(f"Не все units курса {course_id} доступны; принадлежность урока не проверена")
    return [u for u in units if str(u.get("lesson")) == str(lesson_id)]


def lesson_statistics(client, lesson_id, course_id=None):
    """Lesson-level aggregates; a course only validates the lesson's placement."""
    payload = client.get("lessons", ids=[lesson_id])
    lessons = payload.get("lessons", []) or []
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден или недоступен", status=404)
    lesson = lessons[0]
    units = _course_units_for_lesson(client, course_id, lesson_id) if course_id is not None else []
    if course_id is not None and not units:
        raise StepikError(f"Урок {lesson_id} не найден в структуре курса {course_id}", status=404)

    metrics = {}
    unavailable = {}
    for field in ("viewed_by", "passed_by", "time_to_complete"):
        value = lesson.get(field)
        metrics[field] = None
        if field not in lesson:
            unavailable[field] = "поле отсутствует в ответе API"
        elif value is None:
            unavailable[field] = "API вернул null"
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            unavailable[field] = f"неожиданный тип API: {type(value).__name__}"
        elif value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            unavailable[field] = "API вернул некорректное числовое значение"
        elif field != "time_to_complete" and isinstance(value, float) and not value.is_integer():
            unavailable[field] = "API вернул нецелое количество"
        else:
            # Official Stepik clients interpret time_to_complete as seconds.
            metrics[field] = value if field == "time_to_complete" else int(value)
    return {
        "lesson": lesson,
        "course_id": course_id,
        "units": units,
        "metrics": metrics,
        "unavailable": unavailable,
    }


def render_lesson_stats(client, lesson_id, course_id=None):
    data = lesson_statistics(client, lesson_id, course_id=course_id)
    lesson = data["lesson"]
    lines = [f"# Статистика урока: {lesson.get('title', lesson_id)}", ""]
    lines += [f"- **lesson id:** {lesson_id}"]
    if course_id is not None:
        lines.append(f"- **course id:** {course_id}")
        unit_ids = ", ".join(f"`{unit['id']}`" for unit in data["units"])
        lines.append(f"- **Принадлежность курсу подтверждена:** unit {unit_ids}")
    lines += ["", f"**Источник:** GET `/api/lessons?ids[]={lesson_id}`.",
              "**Область данных:** агрегаты самого урока (lesson). "
              "Разбиение этих показателей по курсам не получено.", ""]
    for field, label in (
        ("viewed_by", "Просмотры урока по данным Stepik"),
        ("passed_by", "Студенты, полностью прошедшие урок"),
        ("time_to_complete", "Среднее время прохождения по данным Stepik"),
    ):
        value = data["metrics"][field]
        if value is None:
            formatted = f"недоступно ({data['unavailable'][field]})"
        elif field == "time_to_complete":
            formatted = f"{value} с ({value / 60:.2f} мин)"
        else:
            formatted = str(value)
        lines.append(f"- **{label}:** {formatted} · `lesson.{field}`")
    lines += ["", "**Ограничения интерпретации:**",
              "- Уникальность просмотров `viewed_by` не подтверждена документацией API. "
              "Долю завершивших не рассчитываем: число уникальных начавших неизвестно.",
              "- `time_to_complete` приходит в секундах; минуты вычислены делением на 60. "
              "Stepik называет показатель средним временем, но формула и состав выборки не подтверждены."]
    return "\n".join(lines)


def _rating_metrics(client, step_id, step_payload):
    metrics = _metric_scan(step_payload, ("rating", "rate", "reaction", "vote", "smile", "like", "dislike"))
    if metrics:
        return metrics, None
    # Endpoint names around reactions have changed over Stepik's lifetime.
    # Probe read-only candidates and keep the first one that exists and returns data.
    for endpoint in ("step-ratings", "step-reactions", "step-votes"):
        try:
            payload = client.get(endpoint, step=step_id)
        except StepikError:
            continue
        key = endpoint
        rows = payload.get(key, []) or []
        if rows:
            return _metric_scan(payload, ("rating", "rate", "reaction", "vote", "value", "score")), endpoint
    return [], None


def step_statistics(client, step_id, max_submissions=2000):
    step_payload = client.get("steps", ids=[step_id])
    steps = step_payload.get("steps", []) or []
    if not steps:
        raise StepikError(f"Шаг {step_id} не найден или недоступен", status=404)
    step = steps[0]
    submissions, truncated = step_submissions(client, step_id, max_items=max_submissions)
    counts = _status_counts(submissions)
    correct = counts.get("correct", 0)
    wrong = counts.get("wrong", 0)
    settled = correct + wrong
    calculated_ratio = (correct / settled) if settled else None
    official_ratio = first_value(step, "correct_ratio", "success_rate", "correct_rate")
    passed_by = first_value(step, "passed_by", "solved_by", "correct_by")
    official_attempts = first_value(step, "submissions_count", "attempts_count", "submission_count", "attempt_count")
    if not isinstance(official_attempts, (int, float)):
        official_attempts = None
    rating_metrics, rating_endpoint = _rating_metrics(client, step_id, step_payload)
    return {
        "step": step,
        "submissions": submissions,
        "truncated": truncated,
        "counts": counts,
        "attempts_loaded": len(submissions),
        "official_attempts": official_attempts,
        "official_ratio": official_ratio,
        "calculated_ratio": calculated_ratio,
        "passed_by": passed_by,
        "rating_metrics": rating_metrics,
        "rating_endpoint": rating_endpoint,
        "raw_metrics": _metric_scan(step_payload, ("correct_ratio", "passed_by", "submission", "attempt", "view", "time")),
    }


def render_step_stats(client, step_id, max_submissions=2000):
    data = step_statistics(client, step_id, max_submissions=max_submissions)
    counts = data["counts"]
    ratio = data["official_ratio"] if data["official_ratio"] is not None else data["calculated_ratio"]
    lines = [f"# Статистика шага {step_id}", ""]
    attempts = data["official_attempts"] if data["official_attempts"] is not None else data["attempts_loaded"]
    attempts_note = ""
    if data["official_attempts"] is None and data["truncated"]:
        attempts_note = " (минимум; выборка обрезана)"
    lines += [
        f"- **Попытки/отправленные решения:** {_fmt_number(attempts)}{attempts_note}",
        f"- **Загружено решений для локального анализа:** {data['attempts_loaded']}",
        f"- **Успешность:** {_fmt_number(ratio)}",
        f"- **Решили шаг:** {_fmt_number(data['passed_by'])}",
        f"- **Статусы решений:** {json.dumps(counts, ensure_ascii=False)}",
    ]
    if data["truncated"]:
        lines += ["", "_Важно: когда решений больше лимита, количество попыток выше показанного. Для точного процента предпочтительно поле `correct_ratio`, если Stepik его вернул._"]
    lines += ["", "## Оценки / реакции шага", ""]
    if data["rating_metrics"]:
        for path, value in data["rating_metrics"][:30]:
            lines.append(f"- `{path}` = `{value}`")
        if data["rating_endpoint"]:
            lines.append(f"- источник реакций: `{data['rating_endpoint']}`")
    else:
        lines.append("Stepik не вернул распределение оценок через доступные GET-поля/API для этого шага.")
    if data["raw_metrics"]:
        lines += ["", "## Дополнительные метрики API", ""]
        for path, value in data["raw_metrics"][:25]:
            lines.append(f"- `{path}` = `{value}`")
    return "\n".join(lines)


def render_lesson_bundle(client, lesson_id, answers=True, comments=True, stats=True,
                         wrong_sample=5, max_submissions=1000, course_id=None):
    """One AI-editor payload: lesson + comments + lesson stats + task stats/wrong samples."""
    parts = [render_lesson(client, lesson_id, raw=False, answers=answers)]
    if comments:
        parts += ["\n---\n", render_lesson_comments(client, lesson_id)]
    if stats:
        parts += ["\n---\n", render_lesson_stats(client, lesson_id, course_id=course_id)]
        lessons = client.get("lessons", ids=[lesson_id]).get("lessons", [])
        step_ids = (lessons[0].get("steps", []) if lessons else []) or []
        sources = client.by_ids("step-sources", step_ids, key="step-sources")
        by_id = {s.get("id"): s for s in sources}
        for pos, step_id in enumerate(step_ids, 1):
            kind = ((by_id.get(step_id, {}).get("block") or {}).get("name") or "")
            if kind in ("text", "video"):
                continue
            parts += ["\n---\n", f"# Шаг {pos}: аналитика ({kind})\n", render_step_stats(client, step_id, max_submissions=max_submissions)]
            if wrong_sample and kind == "code":
                parts += ["\n", render_wrong_submissions(client, step_id, limit=wrong_sample, scan_limit=max_submissions)]
    return "\n".join(parts)
