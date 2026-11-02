"""Удаление комментариев под шагами курса через план и подтверждение.

plan_delete_comments находит комментарии по фильтру и сохраняет план, ничего
не удаляя. apply_delete_comments удаляет ровно комментарии из плана, если
автор назвал их точное число, и только те, что не изменились после плана.
По умолчанию в план попадают только комментарии аккаунта токена и только
без ответов: чужие комментарии и ветки с ответами — по явному флагу.
Полные копии удалённых комментариев остаются в snapshots/.
"""

import hashlib
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .client import StepikError, collect_limited
from .publish import _check_allowed, _log
from .workspace import _atomic_write, _inside

MAX_COMMENTS = 5000
PREVIEW = 120


def plain_text(value):
    """Текст комментария без HTML и лишних пробелов: по нему работает фильтр."""
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(text).split())


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _service_folder(course_id):
    """Папка курса для планов и журнала; создаётся, если курс ещё не скачивали."""
    root = Path(os.environ.get("COURSE_WORKSPACE_ROOT", r"C:\Courses"))
    if not root.is_absolute():
        raise StepikError("COURSE_WORKSPACE_ROOT must be an absolute path")
    if any(path.is_symlink() or path.is_junction() for path in (root, *root.parents)):
        raise StepikError("Workspace root must not pass through symlinks or junctions")
    folder = _inside(root, f"course-{course_id}")
    _inside(folder, ".stepik/plans").mkdir(parents=True, exist_ok=True)
    return folder


def _course_steps(client, course_id):
    """step_id -> позиция «модуль.урок.шаг» для всех шагов курса."""
    course = client.by_ids("courses", [course_id])
    if not course:
        raise StepikError(f"Курс {course_id} не найден или недоступен", status=404)
    sections = client.by_ids("sections", course[0].get("sections") or [])
    units = client.by_ids("units", [unit for section in sections for unit in section.get("units") or []])
    lessons = {lesson["id"]: lesson for lesson in client.by_ids("lessons", [unit.get("lesson") for unit in units])}
    section_position = {section["id"]: section.get("position") for section in sections}
    positions = {}
    for unit in units:
        lesson = lessons.get(unit.get("lesson")) or {}
        for number, step_id in enumerate(lesson.get("steps") or [], 1):
            positions[step_id] = f"{section_position.get(unit.get('section'))}.{unit.get('position')}.{number}"
    return positions


def plan_delete_comments(client, course_id, text=None, contains=None, lesson_position=None, user_id=None,
                         include_replied=False):
    _check_allowed(client, course_id)
    if not text and not contains:
        raise StepikError("Нужен фильтр: text (весь текст комментария) или contains (часть текста)", status=400)
    if lesson_position is not None and not re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", lesson_position):
        raise StepikError("lesson_position должен быть вида 6.2", status=400)
    me = ((client.get("stepics/1").get("users") or [{}])[0]).get("id")
    author = me if user_id is None else user_id
    positions = _course_steps(client, course_id)
    rows, truncated = collect_limited(client, "comments", max_items=MAX_COMMENTS, course=course_id)

    wanted_text = plain_text(text) if text else None
    needle = contains.casefold() if contains else None
    matched, skipped = [], []
    for row in rows:
        position = positions.get(row.get("target"))
        if position is None:
            continue  # комментарий не под шагом этого курса
        if lesson_position and not position.startswith(lesson_position + "."):
            continue
        if row.get("user") != author or row.get("is_deleted"):
            continue
        body = plain_text(row.get("text"))
        if wanted_text is not None and body != wanted_text:
            continue
        if needle is not None and needle not in body.casefold():
            continue
        item = {"id": row["id"], "step_id": row.get("target"), "position": position, "user": row.get("user"),
                "time": row.get("time"), "parent": row.get("parent"), "replies": row.get("reply_count") or 0,
                "text_sha256": _sha(row.get("text")), "preview": body[:PREVIEW]}
        if item["replies"] and not include_replied:
            skipped.append({"id": row["id"], "position": position, "reason": f"есть ответы ({item['replies']})"})
            continue
        matched.append(item)

    summary = {"course_id": course_id, "found": len(matched), "scanned": len(rows),
               **({"truncated": True} if truncated else {}),
               "skipped": skipped[:20], "skipped_count": len(skipped)}
    if not matched:
        return {"status": "nothing_to_delete", **summary, "note": "Подходящих комментариев нет. Ничего не удалено."}

    folder = _service_folder(course_id)
    plan_id = _now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    plans = _inside(folder, ".stepik/plans")
    plan = {"version": 1, "plan_id": plan_id, "kind": "delete_comments", "created_at": _now().isoformat(),
            "course_id": course_id, "author": author, "include_replied": include_replied, "comments": matched}
    lines = [f"Удаление комментариев курса {course_id}: {len(matched)} шт.", ""]
    for item in matched:
        reply = f", ответ на {item['parent']}" if item["parent"] else ""
        lines.append(f"- comment {item['id']} · шаг {item['position']} · user {item['user']} · {item['time']}{reply}: "
                     f"{item['preview']}")
    listing = _inside(plans, f"{plan_id}.comments.txt")
    _atomic_write(listing, "\n".join(lines) + "\n")
    _atomic_write(_inside(plans, f"{plan_id}.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    by_lesson = {}
    for item in matched:
        lesson = item["position"].rsplit(".", 1)[0]
        by_lesson[lesson] = by_lesson.get(lesson, 0) + 1
    others = author != me
    return {
        "status": "planned", "plan_id": plan_id, **summary, "confirm_count": len(matched),
        "by_lesson": by_lesson, "examples": [item["preview"] for item in matched[:5]],
        "list_path": str(listing),
        **({"warning": f"Это комментарии другого пользователя ({author}), не автора курса"} if others else {}),
        "note": "Ничего не удалено. Покажите автору список и вызывайте stepik_apply_delete_comments с "
                "confirm_count, только если автор подтвердил удаление. Удаление на Stepik не отменить; "
                "копии комментариев сохранятся в snapshots/.",
    }


def apply_delete_comments(client, course_id, plan_id, confirm_count):
    _check_allowed(client, course_id)
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", plan_id or ""):
        raise StepikError("Некорректный plan_id", status=400)
    folder = _service_folder(course_id)
    plan_path = _inside(folder, f".stepik/plans/{plan_id}.json")
    if not plan_path.is_file():
        raise StepikError("План не найден. Сначала вызовите stepik_plan_delete_comments", status=404)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("kind") != "delete_comments" or plan["course_id"] != course_id:
        raise StepikError("Это не план удаления комментариев этого курса", status=400)
    if plan.get("applied_at"):
        raise StepikError("План уже применён", status=409)
    planned = plan["comments"]
    if confirm_count != len(planned):
        raise StepikError(f"DELETE_NOT_CONFIRMED: план удаляет {len(planned)} комментариев; передайте это число "
                          "в confirm_count, только если автор подтвердил удаление", status=409)

    lock = _inside(folder, ".stepik-comments-delete.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Удаление уже идёт. Если процесс упал, проверьте .stepik-comments-delete.lock") from None
    try:
        current = {row["id"]: row for row in client.by_ids("comments", [item["id"] for item in planned])}
        snapshots = _inside(folder, "snapshots")
        snapshots.mkdir(exist_ok=True)
        snapshot = _inside(snapshots, f"{_now().strftime('%Y-%m-%d_%H%M%S')}-{plan_id[-6:]}-comments.json")
        _atomic_write(snapshot, json.dumps([current[item["id"]] for item in planned if item["id"] in current],
                                           ensure_ascii=False, indent=2) + "\n")
        deleted, skipped = [], []
        for item in planned:
            row = current.get(item["id"])
            reason = None
            if row is None or row.get("is_deleted"):
                reason = "уже удалён"
            elif row.get("user") != item["user"] or _sha(row.get("text")) != item["text_sha256"]:
                reason = "изменился после плана"
            elif (row.get("reply_count") or 0) and not plan.get("include_replied"):
                reason = "появились ответы"
            if reason:
                skipped.append({"id": item["id"], "reason": reason})
                continue
            try:
                client.delete(f"comments/{item['id']}")
            except StepikError as exc:
                _log(folder, course_id=course_id, comment_id=item["id"], step_id=item["step_id"],
                     operation="delete_comment", result="error", error=str(exc))
                skipped.append({"id": item["id"], "reason": str(exc)})
                break  # не продолжаем вслепую после ошибки API
            _log(folder, course_id=course_id, comment_id=item["id"], step_id=item["step_id"],
                 operation="delete_comment", old_hash=item["text_sha256"], result="ok")
            deleted.append(item["id"])
        plan["applied_at"] = _now().isoformat()
        plan["result"] = {"deleted": len(deleted), "skipped": len(skipped)}
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
        return {
            "status": "deleted" if len(deleted) == len(planned) else "partial", "plan_id": plan_id,
            "deleted": len(deleted), "planned": len(planned), "skipped": skipped[:20], "snapshot": str(snapshot),
            "note": "Копии удалённых комментариев сохранены в snapshot; на Stepik их не вернуть.",
        }
    finally:
        lock.rmdir()
