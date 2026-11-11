"""Локальные версии файла урока: сохранить перед правкой, посмотреть, вернуть целиком или один шаг.

Версии лежат в snapshots/local/<урок>/ и не отправляются на Stepik. Восстановление
без apply=True только показывает изменения; с apply=True текущий файл сначала
сохраняется новой версией, потом заменяется.
"""

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone

from . import workspace as sw
from .client import StepikError
from .lessontools import lesson_file, step_segments

MAX_DIFF_LINES = 60
VERSION_ID = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")


def _dir(folder, lesson_position):
    return sw._inside(folder, f"snapshots/local/{lesson_position}")


def _index(directory):
    path = directory / "index.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except ValueError as exc:
        raise StepikError(f"Повреждён {path}; версии не изменены") from exc


def _save(folder, lesson_position, text, note):
    directory = _dir(folder, lesson_position)
    directory.mkdir(parents=True, exist_ok=True)
    versions = _index(directory)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if versions and versions[-1]["sha256"] == digest:
        return versions[-1], False
    base = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    version_id, suffix = base, 1
    while (directory / f"{version_id}.md").exists():
        suffix += 1
        version_id = f"{base}-{suffix}"
    sw._atomic_write(directory / f"{version_id}.md", text)
    entry = {"version": version_id, "note": str(note or "")[:200], "sha256": digest,
             "saved_at": datetime.now(timezone.utc).isoformat()}
    versions.append(entry)
    sw._atomic_write(directory / "index.json", json.dumps(versions, ensure_ascii=False, indent=2) + "\n",
                     replace=True)
    return entry, True


def save_version(course_id, lesson_position, note=""):
    folder, _, path = lesson_file(course_id, lesson_position)
    entry, created = _save(folder, lesson_position, path.read_text(encoding="utf-8"), note)
    return {"status": "saved" if created else "unchanged", "lesson": lesson_position, **entry,
            "note_ru": "Версия сохранена." if created else "Файл не менялся с последней версии; новая не создана."}


def _changed(old, new):
    return sum(1 for line in difflib.ndiff(old.split("\n"), new.split("\n")) if line[:1] in "+-")


def list_versions(course_id, lesson_position):
    folder, _, path = lesson_file(course_id, lesson_position)
    current = path.read_text(encoding="utf-8")
    directory = _dir(folder, lesson_position)
    rows = []
    for entry in _index(directory):
        version = directory / f"{entry['version']}.md"
        if version.is_file():
            rows.append({**{key: entry[key] for key in ("version", "note", "saved_at")},
                         "changed_lines_vs_current": _changed(version.read_text(encoding="utf-8"), current)})
    return {"status": "ok", "lesson": lesson_position, "versions": rows[-30:], "count": len(rows)}


def _step_text(text, step_id):
    lines, segments = step_segments(text)
    for segment in segments:
        if segment["step_id"] == step_id and not segment["delete"]:
            return lines, segment
    return lines, None


def restore_version(course_id, lesson_position, version, step_id=None, apply=False):
    if not isinstance(version, str) or not VERSION_ID.match(version):
        raise StepikError("version: идентификатор из stepik_list_versions, например 20261111-120000", status=400)
    folder, _, path = lesson_file(course_id, lesson_position)
    source = _dir(folder, lesson_position) / f"{version}.md"
    if not source.is_file():
        raise StepikError(f"Версии {version} нет", status=404)
    current = path.read_text(encoding="utf-8")
    old = source.read_text(encoding="utf-8")
    if step_id is None:
        result = old
    else:
        old_lines, old_segment = _step_text(old, step_id)
        new_lines, new_segment = _step_text(current, step_id)
        if old_segment is None or new_segment is None:
            raise StepikError(f"Шага step_id {step_id} нет в версии или в текущем файле", status=404)
        result = "\n".join(new_lines[:new_segment["start"]] + old_lines[old_segment["start"]:old_segment["end"]]
                           + new_lines[new_segment["end"]:])
    diff = list(difflib.unified_diff(current.split("\n"), result.split("\n"), "сейчас", f"версия {version}",
                                     lineterm="", n=1))
    report = {"lesson": lesson_position, "version": version, **({"step_id": step_id} if step_id else {}),
              "changed_lines": sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))),
              "diff": diff[:MAX_DIFF_LINES], **({"diff_truncated": True} if len(diff) > MAX_DIFF_LINES else {})}
    if result == current:
        return {"status": "unchanged", **report}
    if not apply:
        return {"status": "planned", **report,
                "note": "Файл не изменён. Покажи изменения автору; после его «да» вызови снова с apply=True."}
    backup, _ = _save(folder, lesson_position, current, f"перед восстановлением {version}")
    sw._atomic_write(path, result, replace=True)
    return {"status": "restored", **report, "backup_version": backup["version"],
            "note": "Прежний файл сохранён версией backup_version. На Stepik ничего не отправлено."}
