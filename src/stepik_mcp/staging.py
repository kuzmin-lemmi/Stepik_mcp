"""Перенос урока в тестовый курс перед публикацией в настоящий.

Создаёт в папке тестового курса новый черновик drafts/<время>-from-<курс>-<урок>.md:
шаги с метками <!-- new_step -->, название урока, YAML-блоки и локальные картинки.
На Stepik ничего не отправляет: дальше обычный путь stepik_plan_create_lesson →
stepik_apply_push → stepik_plan_push → stepik_apply_push в тестовый курс.
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from . import coursemap
from . import workspace as sw
from .client import StepikError
from .lessontools import lesson_file, step_segments
from .publish import IMAGE_MD, REMOTE_SRC, _draft_title


def stage_lesson(course_id, lesson_position, test_course_id):
    if test_course_id == course_id:
        raise StepikError("Тестовый курс должен отличаться от исходного", status=400)
    folder, item, path = lesson_file(course_id, lesson_position)
    test_folder = coursemap.course_folder(test_course_id)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    lines, segments = step_segments(text)
    if not segments:
        raise StepikError("В уроке нет меток шагов", status=400)
    title = item.get("title") or _draft_title(text) or lesson_position
    body = []
    for segment in segments:
        if segment["delete"]:
            continue
        chunk = lines[segment["start"] + 1:segment["end"]]
        body += ["<!-- new_step -->", *chunk]
    draft = "\n".join(["---", f"title: {json.dumps(title, ensure_ascii=False)}",
                       f'staged_from: "{course_id}/{lesson_position}"', "---", "",
                       f"# {title}", "", *body]).rstrip("\n") + "\n"

    drafts = sw._inside(test_folder, "drafts")
    drafts.mkdir(exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-from-{course_id}-{lesson_position}.md"
    target = sw._inside(test_folder, f"drafts/{name}")
    if target.exists():
        raise StepikError("Черновик с этим именем уже есть: повторите через секунду", status=409)

    copied, lesson_dir = [], path.parent
    for match in IMAGE_MD.finditer(draft):
        src = match[2]
        if REMOTE_SRC.match(src):
            continue
        source = sw._inside(folder, os.path.normpath(str(lesson_dir.relative_to(folder) / src.replace("%20", " "))))
        if not source.is_file():
            raise StepikError(f"Картинка не найдена: {src}", status=404)
        destination = sw._inside(test_folder, os.path.normpath(str(Path("drafts") / src.replace("%20", " "))))
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise StepikError(f"В тестовом курсе уже есть другая картинка {src}; переименуйте её", status=409)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied.append(src)
    sw._atomic_write(target, draft)
    return {
        "status": "staged", "course_id": course_id, "lesson": lesson_position, "test_course_id": test_course_id,
        "draft_path": f"drafts/{name}", "steps": sum(1 for segment in segments if not segment["delete"]),
        "copied_images": copied,
        "next": [
            f"stepik_plan_create_lesson(course_id={test_course_id}, draft_path='drafts/{name}', module_position=N)",
            "показать план автору и stepik_apply_push после «да»",
            f"stepik_plan_push(course_id={test_course_id}, lesson_position=<позиция из ответа>) и stepik_apply_push",
            "автор смотрит урок на Stepik в тестовом курсе; затем публикация в настоящий курс обычным путём",
        ],
        "note": "На Stepik ничего не отправлено. Черновик — копия: правки в нём не возвращаются в исходный урок.",
    }
