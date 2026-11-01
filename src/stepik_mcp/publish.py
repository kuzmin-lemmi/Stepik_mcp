"""Публикация текстовых шагов и картинок из локального урока на Stepik.

Два шага: plan_push строит план и diff, ничего не отправляя; apply_push после
подтверждения автора проверяет, что ни файл, ни урок на Stepik не изменились,
сохраняет снимок, загружает картинки и меняет шаги. Запись разрешена только
в курсы из ALLOWED_WRITE_COURSES (настройка сервера, не файлов курса).

Публикуются только шаги типа text. Неизменённые шаги на Stepik не отправляются,
поэтому конвертация HTML -> Markdown с потерями их не портит.
"""

import difflib
import hashlib
import html
import json
import os
import re
import secrets
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .client import StepikError
from .render import render_step
from .workspace import _atomic_write, _inside

IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

STEP_MARKER = re.compile(r"^<!--\s*(?:step_id:\s*(\d+)|(new_step))\s*-->\s*$")
IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
REMOTE_SRC = re.compile(r"^(?:https?:)?//|^/media/|^data:", re.IGNORECASE)


# --------------------------------------------------------------------------
# Markdown -> HTML
# --------------------------------------------------------------------------

def _inline(text):
    """Строчная разметка: `код`, картинки, ссылки, **жирный**, *курсив*."""
    out = []
    for index, chunk in enumerate(re.split(r"`([^`]+)`", text)):
        if index % 2:
            out.append(f"<code>{html.escape(chunk, quote=False)}</code>")
            continue
        chunk = html.escape(chunk, quote=False)
        chunk = re.sub(
            r"!\[([^\]]*)\]\(([^)\s]+)\)",
            lambda m: f'<img alt="{html.escape(m[1])}" src="{html.escape(m[2])}">', chunk,
        )
        chunk = re.sub(
            r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)",
            lambda m: f'<a href="{html.escape(m[2])}">{m[1]}</a>', chunk,
        )
        chunk = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", chunk)
        chunk = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<em>\1</em>", chunk)
        out.append(chunk)
    return "".join(out)


def _paragraph(lines):
    parts = []
    for number, line in enumerate(lines):
        last = number == len(lines) - 1
        hard_break = line.endswith("  ") and not last
        parts.append(_inline(line.strip()) + ("<br>" if hard_break else ""))
    return "<p>" + "\n".join(parts) + "</p>"


LIST_ITEM = re.compile(r"^( *)([-*+]|\d+\.) +(.*)$")


def _list(lines):
    """Вложенность по отступу в 2 пробела, как пишет html_to_md."""
    out = []
    stack = []  # [(тег, глубина)]
    for line in lines:
        match = LIST_ITEM.match(line)
        if not match:
            # Продолжение предыдущего пункта.
            out[-1] = out[-1][:-len("</li>")] + " " + _inline(line.strip()) + "</li>"
            continue
        depth = len(match[1]) // 2
        tag = "ol" if match[2][0].isdigit() else "ul"
        while stack and (stack[-1][1] > depth or (stack[-1][1] == depth and stack[-1][0] != tag)):
            out.append(f"</{stack.pop()[0]}>")
            if stack:
                out.append("</li>")
        if not stack or stack[-1][1] < depth:
            if stack and out[-1].endswith("</li>"):
                out[-1] = out[-1][:-len("</li>")]
            out.append(f"<{tag}>")
            stack.append((tag, depth))
        out.append(f"<li>{_inline(match[3])}</li>")
    while stack:
        out.append(f"</{stack.pop()[0]}>")
        if stack:
            out.append("</li>")
    return "".join(out)


def md_to_html(markdown):
    """Markdown урока -> HTML для Stepik.

    Поддержаны заголовки, абзацы, списки, цитаты, блоки кода с языком,
    картинки, ссылки, жирный, курсив и `код`. Блок, который начинается
    с HTML-тега, передаётся как есть: так можно вставить таблицу или iframe.
    """
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = re.match(r"^```\s*([\w+#-]*)\s*$", line)
        if fence:
            body = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            lang = f' class="language-{fence[1]}"' if fence[1] else ""
            blocks.append(f"<pre><code{lang}>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
            continue
        heading = re.match(r"^(#{1,6}) +(.*)$", line)
        if heading:
            level = len(heading[1])
            blocks.append(f"<h{level}>{_inline(heading[2].strip())}</h{level}>")
            index += 1
            continue
        group = []
        if line.startswith(">"):
            while index < len(lines) and lines[index].startswith(">"):
                group.append(lines[index][1:].strip())
                index += 1
            inner = [item for item in group if item]
            blocks.append("<blockquote>" + (_paragraph(inner) if inner else "") + "</blockquote>")
            continue
        if LIST_ITEM.match(line):
            while index < len(lines) and lines[index].strip() and not lines[index].startswith("```"):
                group.append(lines[index])
                index += 1
            blocks.append(_list(group))
            continue
        while index < len(lines) and lines[index].strip():
            if group and (lines[index].startswith(("```", "#", ">")) or LIST_ITEM.match(lines[index])):
                break
            group.append(lines[index])
            index += 1
        if re.match(r"^<[a-zA-Z!]", group[0].lstrip()):
            blocks.append("\n".join(group))
        else:
            blocks.append(_paragraph(group))
    return "\n".join(blocks)


class _Features(HTMLParser):
    """Что из HTML шага пропадёт при публикации из Markdown."""

    KNOWN = {"p", "br", "strong", "b", "em", "i", "code", "pre", "img", "h1", "h2", "h3",
             "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "div", "span"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.found.add("ссылки (в Markdown урока нет адресов ссылок)")
        elif tag == "table":
            self.found.add("таблицы")
        elif tag in ("iframe", "video", "audio", "script", "object", "embed"):
            self.found.add(f"встроенные элементы <{tag}>")
        elif tag not in self.KNOWN:
            self.found.add(f"теги <{tag}>")
        for name, _ in attrs:
            if (tag, name) in (("img", "src"), ("img", "alt"), ("a", "href")):
                continue
            if tag == "code" and name == "class":
                continue
            self.found.add("атрибуты оформления (style, class, размеры картинок)")


def _lossy_features(source_html):
    parser = _Features()
    parser.feed(source_html or "")
    return sorted(parser.found)


# --------------------------------------------------------------------------
# Файл урока
# --------------------------------------------------------------------------

def parse_lesson(text):
    """Делит урок на шаги по меткам <!-- step_id: N --> и <!-- new_step -->.

    Заголовок шага (первая строка «## ...» после метки) в текст шага не входит.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    entries = []
    current = None
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else STEP_MARKER.match(line.strip())
        if match:
            current = {"step_id": int(match[1]) if match[1] else None, "lines": [], "heading": None}
            entries.append(current)
            continue
        if current is None:
            continue
        if current["heading"] is None and not current["lines"] and line.startswith("## "):
            current["heading"] = line
            continue
        if current["heading"] is None and not line.strip() and not current["lines"]:
            continue
        current["lines"].append(line)
    for entry in entries:
        entry["body"] = "\n".join(entry.pop("lines")).strip()
    return entries


def _remote_body(source):
    """Тот же Markdown, что пишет загрузчик для этого шага."""
    return render_step(source, raw=False, answers=True).partition("\n")[2].strip()


# --------------------------------------------------------------------------
# Окружение
# --------------------------------------------------------------------------

def allowed_write_courses():
    raw = os.environ.get("ALLOWED_WRITE_COURSES", "")
    allowed = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if part:
            if not part.isdigit():
                raise StepikError("ALLOWED_WRITE_COURSES: ожидаются ID курсов через запятую")
            allowed.add(int(part))
    return allowed


def _check_allowed(course_id):
    if course_id not in allowed_write_courses():
        raise StepikError(
            f"WRITE_NOT_ALLOWED: курса {course_id} нет в ALLOWED_WRITE_COURSES. "
            "Список задаётся в настройках MCP-сервера.", status=403,
        )


def _course_folder(course_id):
    root = Path(os.environ.get("COURSE_WORKSPACE_ROOT", r"C:\Courses"))
    if not root.is_absolute():
        raise StepikError("COURSE_WORKSPACE_ROOT must be an absolute path")
    if any(path.is_symlink() or path.is_junction() for path in (root, *root.parents)):
        raise StepikError("Workspace root must not pass through symlinks or junctions")
    folder = _inside(root, f"course-{course_id}")
    if not _inside(folder, ".course.json").is_file():
        raise StepikError("Папка курса не создана: сначала скачайте урок через stepik_cache_lesson", status=404)
    return folder


def _manifest_item(folder, course_id, lesson_position):
    try:
        manifest = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
        if manifest["version"] != 1 or manifest["course_id"] != course_id:
            raise ValueError("version or course ID mismatch")
        item = manifest["lessons"][lesson_position]
        path = _inside(folder, item["path"])
    except KeyError as exc:
        raise StepikError(f"Урока {lesson_position} нет в .course.json", status=404) from exc
    except (ValueError, TypeError) as exc:
        raise StepikError("Некорректный .course.json") from exc
    if not path.is_file():
        raise StepikError("Локальный файл урока не найден: сначала stepik_cache_lesson", status=404)
    return item, path


def _verify_lesson_in_course(client, lesson_id, course_id):
    """Принадлежность урока курсу проверяется по API, а не по файлам, которые может править агент."""
    units = client.get("units", lesson=lesson_id).get("units", [])
    section_ids = [unit.get("section") for unit in units if unit.get("lesson") == lesson_id]
    sections = client.by_ids("sections", section_ids) if section_ids else []
    if not any(section.get("course") == course_id for section in sections):
        raise StepikError(f"WRITE_NOT_ALLOWED: урок {lesson_id} не входит в курс {course_id}", status=403)


def _remote_state(client, lesson_id):
    lessons = client.by_ids("lessons", [lesson_id])
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден", status=404)
    step_ids = lessons[0].get("steps")
    if not isinstance(step_ids, list):
        raise StepikError("API не вернул список шагов урока")
    sources = {row["id"]: row for row in client.by_ids("step-sources", step_ids)}
    if set(step_ids) - sources.keys():
        raise StepikError("API вернул не все шаги урока")
    return step_ids, sources


def _fingerprint(step_ids, sources):
    data = [[sid, sources[sid].get("block")] for sid in step_ids]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _load_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _local_images(folder, lesson_path, body):
    """Локальные картинки шага: (src из Markdown, путь от папки курса)."""
    found = []
    lesson_dir = lesson_path.parent.relative_to(folder)
    for match in IMAGE_MD.finditer(body):
        src = match[2]
        if REMOTE_SRC.match(src):
            continue
        relative = os.path.normpath(str(lesson_dir / src.replace("%20", " ")))
        path = _inside(folder, relative)
        if path.suffix.lower() not in IMAGE_TYPES:
            raise StepikError(f"Картинка {src}: поддерживаются {', '.join(sorted(IMAGE_TYPES))}", status=400)
        if not path.is_file():
            raise StepikError(f"Картинка не найдена: {src}", status=404)
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise StepikError(f"Картинка {src} больше 10 МБ", status=400)
        found.append((src, Path(relative).as_posix()))
    return found


def _with_urls(body, images, uploaded):
    """Подставляет адреса Stepik вместо локальных путей картинок."""
    mapping = {src: uploaded[sha] for src, sha in images if sha in uploaded}
    return IMAGE_MD.sub(lambda m: f"![{m[1]}]({mapping.get(m[2], m[2])})", body)


# --------------------------------------------------------------------------
# План
# --------------------------------------------------------------------------

def plan_push(client, course_id, lesson_position):
    _check_allowed(course_id)
    folder = _course_folder(course_id)
    item, lesson_path = _manifest_item(folder, course_id, lesson_position)
    lesson_id = item["lesson_id"]
    data = lesson_path.read_bytes()
    entries = parse_lesson(data.decode("utf-8"))
    if not entries:
        raise StepikError("В файле нет меток шагов <!-- step_id: N --> или <!-- new_step -->", status=400)

    _verify_lesson_in_course(client, lesson_id, course_id)
    step_ids, sources = _remote_state(client, lesson_id)
    position_of = {sid: number for number, sid in enumerate(step_ids, 1)}
    uploaded = _load_json(_inside(folder, ".stepik/images.json"), {})

    operations, summary, skipped, diff_parts, images = [], [], [], [], {}
    last_anchor = 0
    created = 0
    for entry in entries:
        sid = entry["step_id"]
        step_images = [(src, rel) for src, rel in _local_images(folder, lesson_path, entry["body"])]
        image_shas = []
        for src, rel in step_images:
            sha = _sha(_inside(folder, rel).read_bytes())
            image_shas.append((src, sha))
            if sha not in uploaded:
                images[sha] = rel
        if sid is None:
            if not entry["body"]:
                continue
            position = last_anchor + created + 1
            created += 1
            operations.append({"op": "create", "position": position, "markdown": entry["body"],
                               "images": image_shas})
            summary.append({"step": f"новый, позиция {position}", "status": "NEW"})
            diff_parts.append(f"=== Новый шаг (позиция {position}) ===\n{entry['body']}\n")
            continue
        if sid not in sources:
            raise StepikError(f"Шага {sid} нет в уроке {lesson_id} на Stepik. Удаление и перенос шагов не поддерживаются",
                              status=409)
        if position_of[sid] <= last_anchor:
            raise StepikError("Порядок шагов в файле отличается от Stepik. Перемещение шагов не поддерживается",
                              status=409)
        last_anchor = position_of[sid]
        source = sources[sid]
        remote_md = _remote_body(source)
        local_md = _with_urls(entry["body"], image_shas, uploaded)
        if local_md == remote_md:
            continue
        kind = source["block"].get("name")
        if kind != "text":
            skipped.append({"step_id": sid, "position": position_of[sid], "type": kind,
                            "reason": "изменён, но публикуются только текстовые шаги"})
            continue
        operations.append({"op": "update", "step_id": sid, "markdown": entry["body"], "images": image_shas})
        warnings = _lossy_features(source["block"].get("text"))
        summary.append({"step": f"step_id {sid}, позиция {position_of[sid]}", "status": "MODIFIED",
                        **({"будет потеряно": warnings} if warnings else {})})
        diff = difflib.unified_diff(remote_md.splitlines(), local_md.splitlines(),
                                    f"Stepik, шаг {position_of[sid]}", f"локально, шаг {position_of[sid]}",
                                    lineterm="")
        diff_parts.append("\n".join(diff) + "\n")

    if not operations:
        return {"status": "nothing_to_push", "lesson_id": lesson_id, "skipped": skipped,
                "note": "Текстовые шаги совпадают со Stepik. Ничего не отправлено."}

    plan_id = _now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    plans = _inside(folder, ".stepik/plans")
    plans.mkdir(parents=True, exist_ok=True)
    plan = {
        "version": 1, "plan_id": plan_id, "created_at": _now().isoformat(),
        "course_id": course_id, "lesson_position": lesson_position, "lesson_id": lesson_id,
        "lesson_path": lesson_path.relative_to(folder).as_posix(), "file_sha256": _sha(data),
        "remote_fingerprint": _fingerprint(step_ids, sources),
        "images": images, "operations": operations,
    }
    diff_path = _inside(plans, f"{plan_id}.diff")
    _atomic_write(diff_path, "\n".join(diff_parts))
    _atomic_write(_inside(plans, f"{plan_id}.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "planned", "plan_id": plan_id, "lesson_id": lesson_id, "course_id": course_id,
        "changes": summary, "images_to_upload": sorted(images.values()), "skipped": skipped,
        "diff_path": str(diff_path),
        "note": "На Stepik ничего не отправлено. Покажите автору diff и вызывайте stepik_apply_push "
                "только после его явного подтверждения.",
    }


# --------------------------------------------------------------------------
# Применение
# --------------------------------------------------------------------------

def _log(folder, **record):
    logs = _inside(folder, "logs")
    logs.mkdir(exist_ok=True)
    with _inside(logs, "stepik-write.log").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"time": _now().isoformat(), **record}, ensure_ascii=False) + "\n")


def _attachment_url(client, response):
    rows = response.get("attachments") or []
    url = rows[0].get("file") if rows and isinstance(rows[0], dict) else None
    if not isinstance(url, str) or not url:
        raise StepikError("Stepik не вернул адрес загруженной картинки")
    return client.base_url + url if url.startswith("/") else url


def _record_new_ids(folder, lesson_path, created_ids):
    """Заменяет метки <!-- new_step --> на настоящие step_id, по порядку."""
    lines = lesson_path.read_text(encoding="utf-8").split("\n")
    pending = list(created_ids)
    in_fence = False
    for number, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if not pending or in_fence:
            continue
        match = STEP_MARKER.match(line.strip())
        if match and match[2]:
            sid = pending.pop(0)
            lines[number] = f"<!-- step_id: {sid} -->"
            following = number + 1
            if following < len(lines) and lines[following].startswith("## "):
                lines[following] = f"## Шаг | step_id: {sid} | text"
    _atomic_write(lesson_path, "\n".join(lines), replace=True)


def apply_push(client, course_id, plan_id):
    _check_allowed(course_id)
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", plan_id or ""):
        raise StepikError("Некорректный plan_id", status=400)
    folder = _course_folder(course_id)
    plan_path = _inside(folder, f".stepik/plans/{plan_id}.json")
    if not plan_path.is_file():
        raise StepikError("План не найден. Сначала вызовите stepik_plan_push", status=404)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("applied_at"):
        raise StepikError("План уже применён. Для новых правок постройте новый план", status=409)
    if plan["course_id"] != course_id:
        raise StepikError("План относится к другому курсу", status=400)

    lock = _inside(folder, ".stepik-push.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Публикация уже идёт. Если процесс упал, проверьте .stepik-push.lock") from None
    try:
        lesson_path = _inside(folder, plan["lesson_path"])
        if _sha(lesson_path.read_bytes()) != plan["file_sha256"]:
            raise StepikError("LOCAL_CHANGED: файл урока изменился после плана. Постройте новый план", status=409)
        lesson_id = plan["lesson_id"]
        _verify_lesson_in_course(client, lesson_id, course_id)
        step_ids, sources = _remote_state(client, lesson_id)
        if _fingerprint(step_ids, sources) != plan["remote_fingerprint"]:
            raise StepikError("REMOTE_CHANGED: урок на Stepik изменился после плана. Постройте новый план",
                              status=409)

        stamp = _now().strftime("%Y-%m-%d_%H%M%S")
        snapshots = _inside(folder, "snapshots")
        snapshots.mkdir(exist_ok=True)
        snapshot = _inside(snapshots, f"{stamp}-lesson-{lesson_id}.json")
        _atomic_write(snapshot, json.dumps({"lesson_id": lesson_id, "steps": step_ids,
                                            "step_sources": [sources[sid] for sid in step_ids]},
                                           ensure_ascii=False, indent=2) + "\n")

        images_path = _inside(folder, ".stepik/images.json")
        uploaded = _load_json(images_path, {})
        done, created_ids = [], []
        current = None
        try:
            for sha, relative in plan["images"].items():
                current = {"op": "upload_image", "path": relative}
                path = _inside(folder, relative)
                content = path.read_bytes()
                if _sha(content) != sha:
                    raise StepikError(f"LOCAL_CHANGED: картинка {relative} изменилась после плана")
                response = client.upload("attachments", {"lesson": lesson_id}, path.name, content,
                                         IMAGE_TYPES[path.suffix.lower()])
                uploaded[sha] = _attachment_url(client, response)
                _atomic_write(images_path, json.dumps(uploaded, ensure_ascii=False, indent=2) + "\n", replace=True)
                _log(folder, course_id=course_id, lesson_id=lesson_id, operation="upload_image",
                     path=relative, new_hash=sha, result="ok")
                done.append(current)

            for operation in plan["operations"]:
                text = md_to_html(_with_urls(operation["markdown"], operation["images"], uploaded))
                new_hash = _sha(text.encode())
                if operation["op"] == "update":
                    sid = operation["step_id"]
                    current = {"op": "update", "step_id": sid}
                    source = dict(sources[sid])
                    source["block"] = {**source["block"], "text": text}
                    client.put(f"step-sources/{sid}", {"stepSource": source})
                    old_hash = _sha((sources[sid]["block"].get("text") or "").encode())
                else:
                    current = {"op": "create", "position": operation["position"]}
                    response = client.post("step-sources", {"stepSource": {
                        "lesson": lesson_id, "position": operation["position"],
                        "block": {"name": "text", "text": text},
                    }})
                    rows = response.get("step-sources") or []
                    if not rows or not rows[0].get("id"):
                        raise StepikError("Stepik не вернул id созданного шага")
                    sid = rows[0]["id"]
                    created_ids.append(sid)
                    current["step_id"] = sid
                    old_hash = None
                _log(folder, course_id=course_id, lesson_id=lesson_id, step_id=sid, operation=operation["op"],
                     old_hash=old_hash, new_hash=new_hash, result="ok")
                done.append(current)
        except StepikError as exc:
            _log(folder, course_id=course_id, lesson_id=lesson_id, operation=current and current["op"],
                 result="error", error=str(exc))
            if created_ids:
                _record_new_ids(folder, lesson_path, created_ids)
            plan["applied_at"] = _now().isoformat()
            plan["result"] = "partial"
            _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
            return {
                "status": "partial", "plan_id": plan_id, "done": done, "failed": current, "error": str(exc),
                "snapshot": str(snapshot),
                "note": "Запись прервана. Выполненные операции перечислены в done; урок до записи сохранён в snapshot.",
            }

        if created_ids:
            _record_new_ids(folder, lesson_path, created_ids)
        plan["applied_at"] = _now().isoformat()
        plan["result"] = "ok"
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
        return {
            "status": "published", "plan_id": plan_id, "lesson_id": lesson_id, "done": done,
            "snapshot": str(snapshot), "source_url": f"{client.base_url}/lesson/{lesson_id}",
            "note": "Изменения опубликованы. Новые step_id записаны в файл урока." if created_ids
                    else "Изменения опубликованы.",
        }
    finally:
        lock.rmdir()
