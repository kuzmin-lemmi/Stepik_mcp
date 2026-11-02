"""Обновление локального урока со Stepik без потери правок автора. Только GET к Stepik.

Каждый шаг сравнивается в трёх версиях: база (исходник на момент скачивания,
.stepik/steps/<id>.json), файл автора и текущий шаг на Stepik.

- изменён только на Stepik — тело шага в файле заменяется новой версией;
- изменён только в файле — остаётся как есть;
- изменён и там, и там — конфликт: файл не трогается, версия со Stepik
  сохраняется рядом с уроком в <урок>.stepik-<время>.md;
- новый шаг на Stepik вставляется после соседнего шага; удалённый на Stepik
  шаг убирается из файла, только если автор его не правил.

Без apply=True ничего не записывается. Перед записью прежний файл урока
сохраняется в snapshots/. resolved — step_id конфликтов, которые автор уже
перенёс в файл вручную: их базой становится текущая версия Stepik.
"""

from . import steps
from .client import StepikError
from .publish import (STEP_MARKER, _course_folder, _fence_after, _manifest_item, _now, _remote_state,
                      expected_body, file_format, parse_lesson)
from .workspace import _atomic_write, _inside, lesson_step_lines, load_step_source, save_step_sources


def _split_file(text):
    """Шапка до первой метки шага и куски файла, каждый со своей меткой."""
    head, chunks = [], []
    fence = None
    track_fences = file_format(text) >= 2
    for line in text.split("\n"):
        fence = _fence_after(fence, line) if track_fences else None
        if not fence and STEP_MARKER.match(line.rstrip()):
            chunks.append([line])
        elif chunks:
            chunks[-1].append(line)
        else:
            head.append(line)
    return head, chunks


def plan_refresh(client, course_id, lesson_position):
    folder = _course_folder(course_id)
    item, lesson_path = _manifest_item(folder, course_id, lesson_position)
    text = lesson_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    fmt = file_format(text)
    if fmt < 2:
        raise StepikError("Файл урока скачан до формата 2: обновление невозможно без исходников шагов. "
                          "Переименуйте файл и скачайте урок заново", status=409)
    entries = parse_lesson(text)
    head, chunks = _split_file(text)
    if len(chunks) != len(entries):
        raise StepikError("Метки шагов в файле разобраны неоднозначно; обновление отменено", status=409)
    step_ids, sources = _remote_state(client, item["lesson_id"])

    in_file = {entry["step_id"] for entry in entries if entry["step_id"]}
    result_chunks, changes, conflicts, update_bases = [], [], [], []
    for entry, chunk in zip(entries, chunks):
        sid = entry["step_id"]
        if sid is None or entry["delete"]:
            result_chunks.append(chunk)  # новый или удаляемый шаг — локальная работа автора
            continue
        base = load_step_source(folder, sid)
        remote = sources.get(sid)
        local_changed = base is None or entry["body"] != expected_body(base, fmt)
        if remote is None:
            if local_changed:
                conflicts.append({"step_id": sid, "reason": "шаг удалён на Stepik, а в файле его правили"})
                result_chunks.append(chunk)
            else:
                changes.append({"step_id": sid, "status": "REMOVED", "note": "шаг удалён на Stepik"})
            continue
        if base is not None and base.get("block") == remote.get("block"):
            result_chunks.append(chunk)
            continue
        if base is None and entry["body"] == expected_body(remote, fmt):
            update_bases.append(sid)  # исходника не было, но файл совпадает со Stepik
            result_chunks.append(chunk)
            continue
        if local_changed:
            reason = ("нет исходника шага на момент скачивания" if base is None
                      else "шаг изменён и в файле, и на Stepik")
            conflicts.append({"step_id": sid, "reason": reason, "remote": remote})
            result_chunks.append(chunk)
            continue
        heading = entry["heading"] or f"## Шаг | step_id: {sid} | {remote['block'].get('name')}"
        result_chunks.append([chunk[0], heading, "", steps.to_markdown(remote, fmt=fmt).strip(), ""])
        update_bases.append(sid)
        changes.append({"step_id": sid, "status": "UPDATED", "type": remote["block"].get("name")})

    # Шаги, которых нет в файле: вставляются после ближайшего предыдущего шага Stepik.
    for sid in [sid for sid in step_ids if sid not in in_file]:
        # Предыдущий шаг Stepik уже есть в файле: он там был или вставлен на прошлом проходе.
        anchor = step_ids.index(sid)
        before = step_ids[anchor - 1] if anchor else None
        index = 0
        for number, chunk in enumerate(result_chunks):
            match = STEP_MARKER.match(chunk[0].rstrip())
            if before is not None and match and int(match[1] or match[3] or 0) == before:
                index = number + 1
        lines = lesson_step_lines([sid], sources, fmt=fmt)
        result_chunks.insert(index, lines)
        update_bases.append(sid)
        changes.append({"step_id": sid, "status": "ADDED", "type": sources[sid]["block"].get("name"),
                        "position": step_ids.index(sid) + 1})

    new_text = "\n".join(head + [line for chunk in result_chunks for line in chunk])
    if not new_text.endswith("\n"):
        new_text += "\n"
    return {"folder": folder, "item": item, "lesson_path": lesson_path, "old_text": text, "new_text": new_text,
            "changes": changes, "conflicts": conflicts, "update_bases": update_bases, "sources": sources}


def refresh_lesson(client, course_id, lesson_position, apply=False, resolved=None):
    plan = plan_refresh(client, course_id, lesson_position)
    folder, lesson_path, sources = plan["folder"], plan["lesson_path"], plan["sources"]
    conflicts = plan["conflicts"]
    resolved = set(resolved or [])
    conflict_ids = {conflict["step_id"] for conflict in conflicts}
    if resolved - conflict_ids:
        raise StepikError(f"resolved: шагов {sorted(resolved - conflict_ids)} нет среди конфликтов", status=400)
    summary = {
        "status": "nothing_to_refresh" if not plan["changes"] and not conflicts else "planned",
        "path": str(lesson_path), "changes": plan["changes"],
        "conflicts": [{key: value for key, value in conflict.items() if key != "remote"} for conflict in conflicts],
    }
    if not apply or summary["status"] == "nothing_to_refresh":
        summary["note"] = ("Ничего не записано. Чтобы обновить файл, повторите с apply=True. "
                           "Конфликтные шаги в файле не меняются." if summary["status"] == "planned"
                           else "Файл совпадает со Stepik или изменён только локально.")
        return summary

    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    written = {}
    if plan["new_text"] != plan["old_text"]:
        snapshot_dir = _inside(folder, "snapshots")
        snapshot_dir.mkdir(exist_ok=True)
        backup = _inside(snapshot_dir, f"{lesson_path.stem}.local-{stamp}.md")
        _atomic_write(backup, plan["old_text"])
        if lesson_path.read_text(encoding="utf-8").replace("\r\n", "\n") != plan["old_text"]:
            raise StepikError("Файл урока изменился во время обновления; ничего не записано", status=409)
        _atomic_write(lesson_path, plan["new_text"], replace=True)
        written["backup"] = str(backup)
    open_conflicts = [conflict for conflict in conflicts if conflict["step_id"] not in resolved]
    remote_conflicts = [conflict for conflict in open_conflicts if conflict.get("remote")]
    if remote_conflicts:
        fmt = file_format(plan["new_text"])
        lines = [f"# Версия Stepik для конфликтных шагов урока {lesson_position}", "",
                 "Файл урока не изменён в этих шагах. Перенесите нужное вручную, затем вызовите "
                 "stepik_refresh_lesson с resolved=[step_id], чтобы принять версию Stepik как базу.", ""]
        for conflict in remote_conflicts:
            remote = conflict["remote"]
            lines += [f"<!-- stepik_version: {remote['id']} -->", f"## step_id: {remote['id']}", "",
                      steps.to_markdown(remote, fmt=fmt).strip(), ""]
        conflict_path = lesson_path.with_name(f"{lesson_path.stem}.stepik-{stamp}.md")
        _inside(folder, str(conflict_path.relative_to(folder)))
        _atomic_write(conflict_path, "\n".join(lines))
        written["conflicts_file"] = str(conflict_path)
    bases = plan["update_bases"] + sorted(sid for sid in resolved if sid in sources)
    save_step_sources(folder, [sources[sid] for sid in bases])
    summary.update(status="refreshed", **written, resolved=sorted(resolved),
                   note="Файл урока обновлён; прежняя версия в backup. Конфликтные шаги не тронуты.")
    return summary
