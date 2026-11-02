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

upgrade_lesson переводит файл, скачанный старой версией, в текущий формат:
нетронутые шаги записываются заново, правки автора переносятся на новую
запись шага, если не пересекаются со строками, которые меняет формат.
"""

import difflib
import re

from . import steps
from .client import StepikError
from .publish import (STEP_MARKER, _changed_after, _course_folder, _fence_after, _frontmatter_value,
                      _manifest_item, _now, _remote_state, expected_body, file_format, parse_lesson)
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


# --------------------------------------------------------------------------
# Перевод файла урока в текущий формат
# --------------------------------------------------------------------------

def merge_edit(old, local, new):
    """Переносит правку автора (old -> local) на новую запись того же шага (old -> new).

    Построчно: каждая правленая область old должна лежать в строках, которые
    смена формата не тронула. Иначе None — правку нужно перенести вручную.
    """
    old_lines, local_lines, new_lines = old.split("\n"), local.split("\n"), new.split("\n")
    kept = {}  # строка old -> строка new для строк, которые формат не изменил
    for block in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            kept[block.a + offset] = block.b + offset
    result, position = [], 0  # position — следующая ещё не перенесённая строка new
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, local_lines, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        # Правка заменяет old[i1:i2]; эти строки и соседняя граница должны быть в new без изменений.
        if i1 < i2:
            if any(index not in kept for index in range(i1, i2)):
                return None
            if any(kept[index + 1] != kept[index] + 1 for index in range(i1, i2 - 1)):
                return None
            start, end = kept[i1], kept[i2 - 1] + 1
        else:  # вставка перед old[i1]
            if i1 < len(old_lines) and i1 in kept:
                start = end = kept[i1]
            elif i1 and i1 - 1 in kept:
                start = end = kept[i1 - 1] + 1
            else:
                return None
        if start < position:
            return None
        result += new_lines[position:start] + local_lines[j1:j2]
        position = end
    return "\n".join(result + new_lines[position:])


def upgrade_lesson(client, course_id, lesson_position, apply=False):
    """Переводит скачанный урок в текущий формат, не теряя правок автора. Только GET к Stepik."""
    folder = _course_folder(course_id)
    item, lesson_path = _manifest_item(folder, course_id, lesson_position)
    text = lesson_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    fmt = file_format(text)
    if fmt >= steps.FORMAT:
        return {"status": "up_to_date", "path": str(lesson_path), "format": fmt}
    fetched_at = _frontmatter_value(text, "fetched_at")
    entries = parse_lesson(text)
    head, chunks = _split_file(text)
    if len(chunks) != len(entries):
        raise StepikError("Метки шагов в файле разобраны неоднозначно; перевод отменён", status=409)
    remote = {}
    missing = [entry["step_id"] for entry in entries
               if entry["step_id"] and not entry["delete"] and load_step_source(folder, entry["step_id"]) is None]
    if missing:
        remote = {row["id"]: row for row in client.by_ids("step-sources", missing)}

    result_chunks, report, conflicts, new_bases = [], [], [], []
    for entry, chunk in zip(entries, chunks):
        sid = entry["step_id"]
        if sid is None or entry["delete"]:
            result_chunks.append(chunk)  # новый шаг пишется Markdown-ом, формат на него не влияет
            continue
        base = load_step_source(folder, sid)
        if base is None:
            base = remote.get(sid)
            if base is None:
                conflicts.append({"step_id": sid, "reason": "шага нет на Stepik и нет исходника на момент скачивания"})
                continue
            # Файл совпадает с текущим шагом Stepik — терять нечего. Иначе отличие может быть
            # правкой на Stepik после скачивания, а не автора: тогда переносить его нельзя.
            if entry["body"] != expected_body(base, fmt) and _changed_after(base.get("update_date"), fetched_at):
                conflicts.append({"step_id": sid, "reason": "шаг изменён на Stepik после скачивания, а исходника "
                                                            "на момент скачивания нет: правку не отличить от изменения на Stepik. "
                                                            "Если в файле этот шаг не правили, скачайте урок заново"})
                continue
            new_bases.append(base)
        old, new = expected_body(base, fmt), steps.to_markdown(base, fmt=steps.FORMAT).strip()
        body = entry["body"]
        if body == old:
            status, body = ("UPGRADED" if old != new else "UNCHANGED"), new
        elif old == new:
            status = "EDITED_KEPT"  # запись шага в формате не изменилась: правка остаётся как есть
        else:
            merged = merge_edit(old, body, new)
            if merged is None:
                conflicts.append({"step_id": sid, "reason": "правка затрагивает строки, которые меняет новый формат; "
                                                            "опубликуйте или уберите правку и повторите перевод"})
                continue
            status, body = "EDITED_MERGED", merged
        heading = entry["heading"] or f"## Шаг | step_id: {sid} | {base['block'].get('name')}"
        result_chunks.append([chunk[0], heading, "", body, ""])
        if status != "UNCHANGED":
            report.append({"step_id": sid, "status": status})

    summary = {"path": str(lesson_path), "from_format": fmt, "to_format": steps.FORMAT,
               "steps": report, "conflicts": conflicts}
    if conflicts:
        return {"status": "conflicts", **summary,
                "note": "Ничего не записано: у шагов из conflicts правку нельзя перенести автоматически."}
    new_head = "\n".join(head)
    new_head, count = re.subn(r"(?m)^format:\s*\d+\s*$", f"format: {steps.FORMAT}", new_head, count=1)
    if not count:
        new_head = re.sub(r"(?m)^---\s*$", f"---\nformat: {steps.FORMAT}", new_head, count=1) \
            if new_head.startswith("---") else f"---\nformat: {steps.FORMAT}\n---\n\n{new_head}"
    new_text = "\n".join([new_head] + [line for chunk in result_chunks for line in chunk])
    if not new_text.endswith("\n"):
        new_text += "\n"
    if not apply:
        return {"status": "planned", **summary,
                "note": "Ничего не записано. Чтобы перевести файл, повторите с apply=True. "
                        "EDITED_MERGED — ваша правка перенесена в новую запись шага, проверьте её."}
    snapshot_dir = _inside(folder, "snapshots")
    snapshot_dir.mkdir(exist_ok=True)
    backup = _inside(snapshot_dir, f"{lesson_path.stem}.format-{fmt}-{_now().strftime('%Y%m%dT%H%M%S%fZ')}.md")
    _atomic_write(backup, text)
    if lesson_path.read_text(encoding="utf-8").replace("\r\n", "\n") != text:
        raise StepikError("Файл урока изменился во время перевода; ничего не записано", status=409)
    _atomic_write(lesson_path, new_text, replace=True)
    if new_bases:
        save_step_sources(folder, new_bases)
    return {"status": "upgraded", **summary, "backup": str(backup),
            "note": "Файл переведён в новый формат; прежняя версия в backup."}
