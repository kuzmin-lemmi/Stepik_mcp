"""Публикация текстовых шагов и картинок из локального урока на Stepik.

Два шага: plan_push строит план и diff, ничего не отправляя; apply_push после
подтверждения автора проверяет, что ни файл, ни урок на Stepik не изменились,
сохраняет снимок, загружает картинки и меняет шаги. Запись разрешена только
в курсы из ALLOWED_WRITE_COURSES (настройка сервера, не файлов курса); «*»
разрешает любой курс, где аккаунт токена — автор или преподаватель.

Правка шага определяется сравнением с базой: исходником .stepik/steps/<id>.json,
сохранённым при скачивании (или текущей версией Stepik, если базы нет). Если шаг
изменён и локально, и на Stepik после скачивания, он не публикуется.

Публикуются текстовые шаги и задания choice, string, number, matching, sorting,
free-answer и code: правка YAML накладывается на исходник, задание проверяется
до отправки, у задачи на код по желанию автора локально прогоняется решение.
Если шаг уже решали ученики, применение требует явного согласия на этот шаг.
Неизменённые шаги на Stepik не отправляются, поэтому конвертация HTML -> Markdown
с потерями их не портит.

Stepik не сдвигает следующие шаги, когда шаг создаётся в середине урока, поэтому
после записи apply_push сверяет порядок шагов с файлом и выставляет позиции сам.
План, который только переставляет шаги, тоже проходит через plan/apply.

Удаление шага — метка <!-- delete_step: N --> вместо <!-- step_id: N -->. Применение
требует перечислить удаляемые шаги в confirm_delete; урок до удаления остаётся в снимке.

plan_rollback строит такой же план возврата шагов к снимку из snapshots/: изменённые
шаги восстанавливаются, удалённые создаются заново (с новым id).

plan_create_lesson строит план создания нового урока из локального черновика: apply_push
создаёт пустой урок, привязывает его к модулю и записывает ID в черновик; шаги черновика
публикуются затем обычным plan_push. plan_move_lesson и plan_delete_lesson переставляют
урок внутри модуля и удаляют его; Stepik не сдвигает соседние уроки сам, поэтому после
записи позиции уроков модуля выставляются явно. Название урока меняется через title:
во frontmatter файла и публикуется обычным plan_push.
"""

import difflib
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from . import steps
from .client import StepikError
from .solution import run_solution
from .render import STYLED_BLOCKS, html_to_md, md_to_html, render_step
from .workspace import (_atomic_write, _inside, course_structure, load_step_source, merge_structure,
                        save_index, save_step_sources, workspace_root)

IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

STEP_MARKER = re.compile(r"^<!--\s*(?:step_id:\s*(\d+)|(new_step)|delete_step:\s*(\d+))\s*-->\s*$")
IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
REMOTE_SRC = re.compile(r"^(?:https?:)?//|^/media/|^data:", re.IGNORECASE)


class _Features(HTMLParser):
    """Что из HTML шага пропадёт при публикации из Markdown."""

    KNOWN = {"p", "br", "strong", "b", "em", "i", "code", "pre", "img", "h1", "h2", "h3",
             "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "div", "span", "details", "summary", "a"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = set()
        self.raw = []     # таблица или блок со стилем переходят в файл урока HTML-ом как есть
        self.lists = 0

    def handle_endtag(self, tag):
        if self.raw and tag == self.raw[-1]:
            self.raw.pop()
        elif tag in ("ul", "ol") and self.lists:
            self.lists -= 1

    def handle_starttag(self, tag, attrs):
        if self.raw:
            self.raw += [tag] if tag == self.raw[0] else []
            return
        if tag == "table" or (tag in STYLED_BLOCKS and dict(attrs).get("style") and not self.lists):
            self.raw.append(tag)
            return
        if tag in ("ul", "ol"):
            self.lists += 1
        if tag in ("iframe", "video", "audio", "script", "object", "embed"):
            self.found.add(f"встроенные элементы <{tag}>")
        elif tag not in self.KNOWN:
            self.found.add(f"теги <{tag}>")
        for name, _ in attrs:
            # Markdown урока передаёт адрес ссылки, атрибуты картинки, язык и стиль блока кода.
            if (tag, name) in (("a", "href"), ("code", "class"), ("pre", "style")) or tag in ("img", "details"):
                continue
            self.found.add(f"атрибуты оформления ({tag} {name})")


def _lossy_features(source_html):
    parser = _Features()
    parser.feed(source_html or "")
    return sorted(parser.found)


# Теги, которые Markdown урока передаёт; остальные уже названы в _lossy_features.
_STRUCTURE_TAGS = {"p", "br", "strong", "em", "code", "pre", "img", "h1", "h2", "h3", "h4", "h5", "h6",
                   "ul", "ol", "li", "blockquote"}
_SAME_TAG = {"b": "strong", "i": "em", "div": "p"}


def _tag_sequence(source_html):
    out = []
    lists = quotes = 0
    for slash, name in re.findall(r"<(/?)(\w+)", source_html or ""):
        name = _SAME_TAG.get(name.lower(), name.lower())
        if name in ("ul", "ol"):
            lists += -1 if slash else 1
        if name in ("blockquote", "details"):
            quotes += -1 if slash else 1
        # <p> внутри пунктов списка, цитат и спойлеров ставит редактор Stepik; без него блок выглядит так же.
        if name in _STRUCTURE_TAGS and not (name == "p" and (lists > 0 or quotes > 0)):
            out.append(slash + name)
    return out


def structure_changes(source_html):
    """True, если HTML, собранный обратно из Markdown, вложен иначе, чем исходный."""
    return _tag_sequence(md_to_html(html_to_md(source_html))) != _tag_sequence(source_html)


def _edit_losses(source_html):
    """Что потеряется при правке условия: элементы без Markdown и изменения вложенности блоков."""
    found = _lossy_features(source_html)
    if source_html and structure_changes(source_html):
        found.append("структура блоков: после сборки из Markdown вложенность изменится")
    return found


def _old_render_differs(source_html, fmt):
    """Файл старого формата показывает этот шаг иначе, чем текущий загрузчик."""
    old = html_to_md(source_html, fmt=fmt)
    return old != html_to_md(source_html)


# --------------------------------------------------------------------------
# Файл урока
# --------------------------------------------------------------------------

def parse_lesson(text):
    """Делит урок на шаги по меткам <!-- step_id: N --> и <!-- new_step -->.

    Заголовок шага (первая строка «## ...» после метки) в текст шага не входит.
    Метка стоит в начале строки. В старом формате ограды кода бывают незакрыты
    (ошибка прежнего загрузчика), поэтому там метки ищутся без учёта оград.
    """
    text = text.replace("\r\n", "\n")
    track_fences = file_format(text) >= 2
    entries = []
    current = None
    fence = None
    for line in text.split("\n"):
        fence = _fence_after(fence, line) if track_fences else None
        match = None if fence else STEP_MARKER.match(line.rstrip())
        if match:
            number = match[1] or match[3]
            current = {"step_id": int(number) if number else None, "delete": bool(match[3]),
                       "lines": [], "heading": None}
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


def _fence_after(fence, line):
    """Открытая ограда кода после строки: закрывается только такой же или длиннее."""
    run = re.match(r"^\s*(`{3,})(.*)$", line)
    if fence is None:
        return run[1] if run else None
    if run and len(run[1]) >= len(fence) and not run[2].strip():
        return None
    return fence


def file_format(text):
    """Версия формата урока из frontmatter: 2 — с YAML-блоками; без поля — старый формат."""
    value = _frontmatter_value(text, "format")
    return value if isinstance(value, int) else 1


def _frontmatter_value(text, key):
    head = text.split("\n---", 1)[0] if text.startswith("---") else ""
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", head)
    if not match:
        return None
    try:
        return json.loads(match[1])
    except ValueError:
        return match[1]


def _changed_after(update_date, fetched_at):
    """True, если шаг на Stepik менялся позже скачивания файла (или это нельзя исключить)."""
    try:
        updated = datetime.fromisoformat(str(update_date).replace("Z", "+00:00"))
        fetched = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    return updated > fetched


def expected_body(source, fmt):
    """Тело шага в файле, который скачан из этого исходника и не правился."""
    if fmt >= 2:
        return steps.to_markdown(source, fmt=fmt).strip()
    # Старые файлы: прежний рендер и прежний разбор списков.
    return render_step(source, raw=False, answers=True, legacy=True).partition("\n")[2].strip()


# --------------------------------------------------------------------------
# Окружение
# --------------------------------------------------------------------------

ANY_COURSE = "*"


def allowed_write_courses():
    """ID курсов из ALLOWED_WRITE_COURSES; «*» означает любой свой курс."""
    raw = os.environ.get("ALLOWED_WRITE_COURSES", "")
    allowed = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if part == ANY_COURSE:
            allowed.add(ANY_COURSE)
        elif part:
            if not part.isdigit():
                raise StepikError("CONFIG_INVALID: ALLOWED_WRITE_COURSES — ожидаются ID курсов через запятую или «*»")
            allowed.add(int(part))
    return allowed


def _check_allowed(client, course_id):
    allowed = allowed_write_courses()
    if course_id in allowed:
        return
    if ANY_COURSE not in allowed:
        raise StepikError(
            f"WRITE_NOT_ALLOWED: курса {course_id} нет в ALLOWED_WRITE_COURSES. "
            "Список задаётся в настройках MCP-сервера.", status=403,
        )
    # «*» не открывает чужие курсы: аккаунт должен быть автором или преподавателем.
    me = ((client.get("stepics/1").get("users") or [{}])[0]).get("id")
    courses = client.by_ids("courses", [course_id])
    staff = set()
    for course in courses:
        staff.update(course.get("authors") or [])
        staff.update(course.get("instructors") or [])
        staff.add(course.get("owner"))
    if me is None or me not in staff:
        raise StepikError(
            f"WRITE_NOT_ALLOWED: аккаунт не автор и не преподаватель курса {course_id}", status=403,
        )


def _course_folder(course_id):
    root = workspace_root()
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
    _check_allowed(client, course_id)
    folder = _course_folder(course_id)
    item, lesson_path = _manifest_item(folder, course_id, lesson_position)
    lesson_id = item["lesson_id"]
    data = lesson_path.read_bytes()
    text = data.decode("utf-8")
    fmt = file_format(text)
    fetched_at = _frontmatter_value(text, "fetched_at")
    entries = parse_lesson(text)
    if not entries:
        raise StepikError("В файле нет меток шагов <!-- step_id: N --> или <!-- new_step -->", status=400)

    _verify_lesson_in_course(client, lesson_id, course_id)
    step_ids, sources = _remote_state(client, lesson_id)
    position_of = {sid: number for number, sid in enumerate(step_ids, 1)}
    uploaded = _load_json(_inside(folder, ".stepik/images.json"), {})

    operations, summary, skipped, diff_parts, images = [], [], [], [], {}
    last_anchor = 0
    created = 0
    reordered = False
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
            kind = "text"
            extra = {}
            if steps.split_body(entry["body"])[1] is not None:
                try:
                    block = steps.new_block(_with_urls(entry["body"], image_shas, uploaded))
                    steps.validate(block)
                    kind = block["name"]
                    extra = _check_solution(folder, lesson_path, block, entry["body"])
                except StepikError as exc:
                    skipped.append({"step": f"новый после позиции {last_anchor}", "reason": str(exc)})
                    continue
            position = last_anchor + created + 1
            created += 1
            operations.append({"op": "create", "position": position, "kind": kind, "markdown": entry["body"],
                               "images": image_shas})
            summary.append({"step": f"новый, позиция {position}", "status": "NEW", "type": kind, **extra})
            diff_parts.append(f"=== Новый шаг {kind} (позиция {position}) ===\n{entry['body']}\n")
            continue
        if sid not in sources:
            if entry["delete"]:
                skipped.append({"step_id": sid, "reason": "шага уже нет на Stepik"})
                continue
            raise StepikError(f"Шага {sid} нет в уроке {lesson_id} на Stepik. Перенос шагов не поддерживается; "
                              "чтобы удалить шаг, замените его метку на <!-- delete_step: N -->", status=409)
        if position_of[sid] <= last_anchor:
            reordered = True
        last_anchor = max(last_anchor, position_of[sid])
        if entry["delete"]:
            source = sources[sid]
            kind = source["block"].get("name")
            learners = source.get("passed_by") or 0
            operations.append({"op": "delete", "step_id": sid, "kind": kind, "confirm": learners > 0})
            summary.append({"step": f"step_id {sid}, позиция {position_of[sid]}", "status": "DELETE", "type": kind,
                            **({"решили учеников": learners, "нужно согласие": True} if learners else {})})
            diff_parts.append(f"=== Удаление шага {position_of[sid]} (step_id {sid}, {kind}) ===\n"
                              + "\n".join("- " + line for line in steps.to_markdown(source).splitlines()) + "\n")
            continue
        source = sources[sid]
        base = load_step_source(folder, sid)
        base_note = {}
        if base is None:
            base = source
            base_note = {"база": "исходника при скачивании нет, сравнение с текущей версией Stepik"}
        remote_md = expected_body(base, fmt)
        local_md = _with_urls(entry["body"], image_shas, uploaded)
        if local_md == remote_md:
            continue
        if fmt >= 2:
            # Текст может отличаться без изменения сути: например, YAML нового задания
            # короче полного (без show и checker). Сравниваем собранные блоки.
            try:
                if steps.build_block(base["block"], local_md) == base["block"]:
                    continue
            except StepikError:
                pass  # ошибку в YAML покажет сборка ниже
        kind = source["block"].get("name")
        where = {"step_id": sid, "position": position_of[sid], "type": kind}
        if base_note and _changed_after(source.get("update_date"), fetched_at):
            # Без исходника отличие может быть правкой на Stepik, а не локальной: не затираем её.
            skipped.append({**where, "reason": f"шаг изменён на Stepik ({source.get('update_date')}) после скачивания "
                                               "файла, а исходника на момент скачивания нет; скачайте урок заново "
                                               "и перенесите правку"})
            continue
        if base["block"] != source["block"]:
            skipped.append({**where, "reason": "изменён и локально, и на Stepik после скачивания; "
                                               "перенесите правку в свежую копию урока"})
            continue
        if kind != "text" and kind not in steps.PUBLISHABLE:
            skipped.append({**where, "reason": f"шаги типа {kind} пока не публикуются"})
            continue
        if kind != "text" and fmt < 2:
            skipped.append({**where, "reason": "файл скачан до появления YAML-блоков; скачайте урок заново, "
                                               "чтобы править задание"})
            continue
        if fmt < steps.FORMAT and _old_render_differs(source["block"].get("text"), fmt):
            skipped.append({**where, "reason": "файл скачан старой версией, и вёрстка этого шага в нём искажена "
                                               "(списки, врезки, таблицы или спойлеры); скачайте урок заново и перенесите правку"})
            continue
        try:
            block = steps.build_block(source["block"], local_md)
            if kind != "text":
                steps.validate(block)
            extra = _check_solution(folder, lesson_path, block, entry["body"]) if kind == "code" else {}
        except StepikError as exc:
            skipped.append({**where, "reason": str(exc)})
            continue
        fields = steps.changed_paths(source["block"], block)
        learners = source.get("passed_by") or 0
        confirm = kind != "text" and learners > 0 and any(not path.startswith("text") for path in fields)
        operations.append({"op": "update", "step_id": sid, "kind": kind, "markdown": entry["body"],
                           "images": image_shas, "confirm": confirm})
        warnings = _edit_losses(source["block"].get("text")) if "text" in fields else []
        summary.append({"step": f"step_id {sid}, позиция {position_of[sid]}", "status": "MODIFIED", "type": kind,
                        **({"поля": fields} if kind != "text" else {}),
                        **({"будет потеряно": warnings} if warnings else {}),
                        **({"решили учеников": learners, "нужно согласие": True} if confirm else {}),
                        **extra, **base_note})
        # У задания сравниваем полные настройки до и после: поля, не выписанные
        # в YAML, остаются как на Stepik и в diff не должны выглядеть удалёнными.
        before, after = remote_md, local_md
        if kind != "text":
            before, after = steps.to_markdown(source), steps.to_markdown({"block": block})
        diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    f"Stepik, шаг {position_of[sid]}", f"будет на Stepik, шаг {position_of[sid]}",
                                    lineterm="")
        diff_parts.append("\n".join(diff) + "\n")

    if reordered:
        if any(op["op"] in ("create", "delete") for op in operations):
            raise StepikError("Порядок шагов в файле отличается от Stepik. Сначала опубликуйте перестановку "
                              "отдельно, без новых и удаляемых шагов", status=409)
        order = [entry["step_id"] for entry in entries if entry["step_id"] and not entry["delete"]]
        operations.append({"op": "reorder", "order": order})
        summary.append({"step": "порядок шагов", "status": "MOVED", "порядок": order})
        diff_parts.append("=== Порядок шагов ===\n"
                          + "\n".join(f"- шаг {position_of[sid]} (step_id {sid})" for sid in step_ids) + "\n"
                          + "\n".join(f"+ шаг {number} (step_id {sid})" for number, sid in enumerate(order, 1))
                          + "\n")
    rename = _plan_rename(client, lesson_id, item, text)
    if rename and rename.get("op"):
        operations.insert(0, rename)
        summary.insert(0, {"step": "название урока", "status": "RENAME", "было": rename["old_title"],
                           "станет": rename["title"]})
        diff_parts.insert(0, f"=== Название урока ===\n- {rename['old_title']}\n+ {rename['title']}\n")
    elif rename:
        skipped.append(rename)
    if not operations:
        return {"status": "nothing_to_push", "lesson_id": lesson_id, "skipped": skipped,
                "note": "Шаги совпадают со Stepik. Ничего не отправлено."}
    return _save_plan(folder, course_id, lesson_position, lesson_id, lesson_path, _sha(data),
                      step_ids, sources, images, operations, summary, skipped, diff_parts)


def _plan_rename(client, lesson_id, item, text):
    """Операция переименования урока по title: во frontmatter, пропуск с причиной или None."""
    title = _frontmatter_value(text, "title")
    remote = (client.by_ids("lessons", [lesson_id]) or [{}])[0].get("title")
    if not isinstance(title, str) or not title.strip() or title.strip() == remote:
        return None
    title, base = title.strip(), item.get("title")
    if base is not None and remote != base:
        # На Stepik урок переименовали после скачивания: не затираем это название.
        return {"step": "название урока", "reason": f"урок переименован на Stepik («{remote}») после скачивания; "
                                                    "поправьте title: в файле или обновите урок"}
    return {"op": "rename", "title": title, "old_title": remote}


def _check_solution(folder, lesson_path, block, body):
    """Прогон эталонного решения, если автор указал его в YAML (solution: путь)."""
    solution = steps.local_settings(body).get("solution")
    if block.get("name") != "code" or solution is None:
        return {}
    report = run_solution(folder, lesson_path, block, solution)
    if report["status"] == "failed":
        raise StepikError(f"Эталонное решение не прошло тесты ({report['passed']}/{report['total']}): "
                          + json.dumps(report["failures"], ensure_ascii=False), status=400)
    return {"решение": report}


def _save_plan(folder, course_id, lesson_position, lesson_id, lesson_path, file_sha, step_ids, sources,
               images, operations, summary, skipped, diff_parts, kind="push"):
    plan_id = _now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    plans = _inside(folder, ".stepik/plans")
    plans.mkdir(parents=True, exist_ok=True)
    plan = {
        "version": 1, "plan_id": plan_id, "kind": kind, "created_at": _now().isoformat(),
        "course_id": course_id, "lesson_position": lesson_position, "lesson_id": lesson_id,
        "lesson_path": lesson_path.relative_to(folder).as_posix(), "file_sha256": file_sha,
        "remote_fingerprint": _fingerprint(step_ids, sources),
        "images": images, "operations": operations,
    }
    diff_path = _inside(plans, f"{plan_id}.diff")
    _atomic_write(diff_path, "\n".join(diff_parts))
    _atomic_write(_inside(plans, f"{plan_id}.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    confirm = [op["step_id"] for op in operations if op.get("confirm")]
    deletes = [op["step_id"] for op in operations if op["op"] == "delete"]
    return {
        "status": "planned", "plan_id": plan_id, "lesson_id": lesson_id, "course_id": course_id,
        "changes": summary, "images_to_upload": sorted(images.values()), "skipped": skipped,
        "diff_path": str(diff_path),
        **({"confirm_steps": confirm} if confirm else {}),
        **({"confirm_delete": deletes} if deletes else {}),
        "note": "На Stepik ничего не отправлено. Покажите автору diff и вызывайте stepik_apply_push "
                "только после его явного подтверждения."
                + (" Шаги из confirm_steps уже решали ученики: передайте их в confirm_steps, только если "
                   "автор согласился изменить именно их." if confirm else "")
                + (" Удаление нельзя отменить на Stepik (откат создаст шаг заново, но без комментариев и "
                   "решений): передайте confirm_delete, только если автор подтвердил удаление этих шагов."
                   if deletes else ""),
    }


# --------------------------------------------------------------------------
# Откат к снимку
# --------------------------------------------------------------------------

SNAPSHOT_NAME = re.compile(r"[\w.-]+-lesson-(\d+)\.json")


def plan_rollback(client, course_id, lesson_position, snapshot="latest"):
    """План возврата шагов урока к снимку. Ничего не отправляет."""
    _check_allowed(client, course_id)
    folder = _course_folder(course_id)
    item, lesson_path = _manifest_item(folder, course_id, lesson_position)
    lesson_id = item["lesson_id"]
    directory = _inside(folder, "snapshots")
    if snapshot == "latest":
        names = sorted(path.name for path in directory.glob(f"*-lesson-{lesson_id}.json")) if directory.is_dir() else []
        if not names:
            raise StepikError(f"Снимков урока {lesson_id} нет", status=404)
        snapshot = names[-1]
    match = SNAPSHOT_NAME.fullmatch(snapshot or "")
    if not match or int(match[1]) != lesson_id:
        raise StepikError("snapshot: имя файла из snapshots/ этого урока или latest", status=400)
    path = _inside(directory, snapshot)
    if not path.is_file():
        raise StepikError(f"Снимок не найден: {snapshot}", status=404)
    saved = json.loads(path.read_text(encoding="utf-8"))

    _verify_lesson_in_course(client, lesson_id, course_id)
    step_ids, sources = _remote_state(client, lesson_id)
    position_of = {sid: number for number, sid in enumerate(step_ids, 1)}
    operations, summary, skipped, diff_parts = [], [], [], []
    for number, old in enumerate(saved.get("step_sources") or [], 1):
        sid = old["id"]
        if sid not in sources:
            kind = old["block"].get("name")
            operations.append({"op": "recreate", "step_id": sid, "kind": kind, "position": number,
                               "block": old["block"], "cost": old.get("cost", 0)})
            summary.append({"step": f"удалённый step_id {sid}, позиция {number}", "status": "RECREATE", "type": kind,
                            "note": "шаг вернётся с новым id; комментарии и решения учеников не восстанавливаются"})
            diff_parts.append(f"=== Шаг {number} будет создан заново (был step_id {sid}) ===\n"
                              + steps.to_markdown(old) + "\n")
            continue
        current = sources[sid]
        if current["block"] == old["block"]:
            continue
        kind = current["block"].get("name")
        fields = steps.changed_paths(current["block"], old["block"])
        learners = current.get("passed_by") or 0
        confirm = kind != "text" and learners > 0 and any(not field.startswith("text") for field in fields)
        operations.append({"op": "restore", "step_id": sid, "kind": kind, "block": old["block"], "confirm": confirm})
        summary.append({"step": f"step_id {sid}, позиция {position_of[sid]}", "status": "RESTORE", "type": kind,
                        "поля": fields, **({"решили учеников": learners, "нужно согласие": True} if confirm else {})})
        diff = difflib.unified_diff(steps.to_markdown(current).splitlines(), steps.to_markdown(old).splitlines(),
                                    f"Stepik сейчас, шаг {position_of[sid]}", f"снимок {snapshot}", lineterm="")
        diff_parts.append("\n".join(diff) + "\n")
    newer = [sid for sid in step_ids if sid not in {old["id"] for old in saved.get("step_sources") or []}]
    if newer:
        skipped.append({"step_ids": newer, "reason": "созданы после снимка; откат их не удаляет"})
    if not operations:
        return {"status": "nothing_to_restore", "lesson_id": lesson_id, "snapshot": snapshot, "skipped": skipped,
                "note": "Шаги на Stepik совпадают со снимком. Ничего не отправлено."}
    result = _save_plan(folder, course_id, lesson_position, lesson_id, lesson_path, None, step_ids, sources,
                        {}, operations, summary, skipped, diff_parts, kind="rollback")
    result["snapshot"] = snapshot
    result["note"] += (" Локальный файл урока откат не меняет: если в нём осталась откатываемая правка, "
                       "следующий stepik_plan_push предложит её снова.")
    return result


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


def _block_hash(block):
    return _sha(json.dumps(block, sort_keys=True, ensure_ascii=False).encode())


def _record_new_ids(folder, lesson_path, created_ids):
    """Заменяет метки <!-- new_step --> на настоящие step_id, по порядку. created_ids: [(id, тип)]."""
    text = lesson_path.read_text(encoding="utf-8")
    track_fences = file_format(text) >= 2
    lines = text.split("\n")
    pending = list(created_ids)
    fence = None
    for number, line in enumerate(lines):
        fence = _fence_after(fence, line) if track_fences else None
        if not pending or fence:
            continue
        match = STEP_MARKER.match(line.rstrip())
        if match and match[2]:
            sid, kind = pending.pop(0)
            lines[number] = f"<!-- step_id: {sid} -->"
            following = number + 1
            if following < len(lines) and lines[following].startswith("## "):
                lines[following] = f"## Шаг | step_id: {sid} | {kind}"
    _atomic_write(lesson_path, "\n".join(lines), replace=True)


def _fix_order(client, folder, course_id, lesson_id, lesson_path):
    """Выставляет на Stepik позиции шагов по порядку файла. Возвращает операции move.

    Stepik не сдвигает соседей, когда шаг создаётся в середине урока или ему меняют
    позицию, поэтому позиции проверяются заново, пока не совпадут с файлом.
    """
    desired = [entry["step_id"] for entry in parse_lesson(lesson_path.read_text(encoding="utf-8"))
               if entry["step_id"] and not entry["delete"]]
    moves = []
    for _ in range(3):
        step_ids, sources = _remote_state(client, lesson_id)
        if set(step_ids) != set(desired):
            raise StepikError("Шаги урока на Stepik не совпадают с файлом: порядок не исправлен")
        positions = [sources[sid].get("position") for sid in desired]
        if step_ids == desired and len(set(positions)) == len(positions):
            return moves
        for number, sid in enumerate(desired, 1):
            if sources[sid].get("position") == number:
                continue
            source = dict(sources[sid])
            source["position"] = number
            client.put(f"step-sources/{sid}", {"stepSource": source})
            _log(folder, course_id=course_id, lesson_id=lesson_id, step_id=sid, operation="move",
                 old_position=sources[sid].get("position"), new_position=number, result="ok")
            moves.append({"op": "move", "step_id": sid, "position": number})
    step_ids, _ = _remote_state(client, lesson_id)
    if step_ids != desired:
        raise StepikError("Порядок шагов на Stepik не совпал с файлом после перестановки")
    return moves


def _remember_title(folder, lesson_position, title):
    """Новое название — база для следующего плана, как исходники шагов."""
    index = _inside(folder, ".course.json")
    manifest = json.loads(index.read_text(encoding="utf-8"))
    manifest["lessons"][lesson_position]["title"] = title
    save_index(folder, manifest)


def _refresh_bases(client, folder, done):
    """Опубликованная версия шагов становится новой базой для следующих правок."""
    touched = [operation["step_id"] for operation in done
               if operation.get("step_id") and operation["op"] != "delete"]
    if touched:
        save_step_sources(folder, client.by_ids("step-sources", touched))
    for operation in done:
        if operation["op"] == "delete":
            _inside(folder, f".stepik/steps/{operation['step_id']}.json").unlink(missing_ok=True)


def _remove_deleted_steps(folder, lesson_path, deleted_ids):
    """Убирает из файла урока разделы шагов с меткой <!-- delete_step: N -->, удалённых на Stepik."""
    text = lesson_path.read_text(encoding="utf-8")
    track_fences = file_format(text) >= 2
    kept = []
    dropping = False
    fence = None
    for line in text.split("\n"):
        fence = _fence_after(fence, line) if track_fences else None
        match = None if fence else STEP_MARKER.match(line.rstrip())
        if match:
            dropping = bool(match[3]) and int(match[3]) in deleted_ids
        if not dropping:
            kept.append(line)
    _atomic_write(lesson_path, "\n".join(kept), replace=True)


def apply_push(client, course_id, plan_id, confirm_steps=None, confirm_delete=None):
    _check_allowed(client, course_id)
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
    if plan.get("kind") == "create_lesson":
        return _apply_create_lesson(client, folder, plan_path, plan)
    if plan.get("kind") in ("move_lesson", "delete_lesson"):
        return _apply_lesson_plan(client, folder, plan_path, plan, confirm_delete)
    needed = {op["step_id"] for op in plan["operations"] if op.get("confirm")}
    missing = sorted(needed - set(confirm_steps or []))
    if missing:
        raise StepikError(f"LEARNER_IMPACT: шаги {missing} уже решали ученики. Передайте их в confirm_steps, "
                          "только если автор явно согласился изменить именно эти шаги", status=409)
    deletes = sorted(op["step_id"] for op in plan["operations"] if op["op"] == "delete")
    if deletes != sorted(set(confirm_delete or [])):
        raise StepikError(f"DELETE_NOT_CONFIRMED: план удаляет шаги {deletes}. Передайте ровно их в confirm_delete, "
                          "только если автор подтвердил удаление", status=409)

    lock = _inside(folder, ".stepik-push.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Публикация уже идёт. Если процесс упал, проверьте .stepik-push.lock") from None
    try:
        lesson_path = _inside(folder, plan["lesson_path"])
        if plan["file_sha256"] is not None and _sha(lesson_path.read_bytes()) != plan["file_sha256"]:
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
        # Часть plan_id в имени: два применения в одну секунду не столкнутся.
        snapshot = _inside(snapshots, f"{stamp}-{plan_id[-6:]}-lesson-{lesson_id}.json")
        _atomic_write(snapshot, json.dumps({"lesson_id": lesson_id, "steps": step_ids,
                                            "step_sources": [sources[sid] for sid in step_ids]},
                                           ensure_ascii=False, indent=2) + "\n")

        images_path = _inside(folder, ".stepik/images.json")
        uploaded = _load_json(images_path, {})
        done, created_ids, deleted_ids = [], [], []
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

            # Удаления в конце: позиции новых шагов в плане посчитаны при ещё не удалённых шагах.
            ordered = ([op for op in plan["operations"] if op["op"] != "delete"]
                       + [op for op in plan["operations"] if op["op"] == "delete"])
            for operation in ordered:
                if operation["op"] == "reorder":
                    continue  # порядок выставляется после записи шагов
                if operation["op"] == "rename":
                    current = {"op": "rename", "title": operation["title"]}
                    remote_title = (client.by_ids("lessons", [lesson_id]) or [{}])[0].get("title")
                    if remote_title != operation["old_title"]:
                        raise StepikError("REMOTE_CHANGED: название урока на Stepik изменилось после плана")
                    client.put(f"lessons/{lesson_id}", {"lesson": {"title": operation["title"]}})
                    _log(folder, course_id=course_id, lesson_id=lesson_id, operation="rename",
                         old_title=operation["old_title"], new_title=operation["title"], result="ok")
                    _remember_title(folder, plan["lesson_position"], operation["title"])
                    done.append(current)
                    continue
                markdown = _with_urls(operation.get("markdown", ""), operation.get("images", []), uploaded)
                if operation["op"] == "delete":
                    sid = operation["step_id"]
                    current = {"op": "delete", "step_id": sid}
                    client.delete(f"step-sources/{sid}")
                    deleted_ids.append(sid)
                    old_hash, new_hash = _block_hash(sources[sid]["block"]), None
                elif operation["op"] in ("update", "restore"):
                    sid = operation["step_id"]
                    current = {"op": operation["op"], "step_id": sid}
                    source = dict(sources[sid])
                    if operation["op"] == "restore":
                        source["block"] = operation["block"]
                    else:
                        source["block"] = steps.build_block(source["block"], markdown)
                        if source["block"].get("name") != "text":
                            steps.validate(source["block"])
                    new_hash = _block_hash(source["block"])
                    client.put(f"step-sources/{sid}", {"stepSource": source})
                    old_hash = _block_hash(sources[sid]["block"])
                else:
                    kind = operation.get("kind", "text")
                    if operation["op"] == "recreate":
                        block = operation["block"]
                    elif kind == "text":
                        block = {"name": "text", "text": md_to_html(markdown)}
                    else:
                        block = steps.new_block(markdown)
                        steps.validate(block)
                    new_hash = _block_hash(block)
                    current = {"op": operation["op"], "position": operation["position"], "type": kind}
                    step = {"lesson": lesson_id, "position": operation["position"], "block": block}
                    if operation["op"] == "recreate":
                        step["cost"] = operation.get("cost", 0)
                    elif kind != "text":
                        step["cost"] = 1  # как у задания, созданного в редакторе Stepik
                    response = client.post("step-sources", {"stepSource": step})
                    rows = response.get("step-sources") or []
                    if not rows or not rows[0].get("id"):
                        raise StepikError("Stepik не вернул id созданного шага")
                    sid = rows[0]["id"]
                    if operation["op"] == "create":
                        created_ids.append((sid, kind))
                    current["step_id"] = sid
                    old_hash = None
                _log(folder, course_id=course_id, lesson_id=lesson_id, step_id=sid, operation=operation["op"],
                     old_hash=old_hash, new_hash=new_hash, result="ok")
                done.append(current)
            if created_ids:
                _record_new_ids(folder, lesson_path, created_ids)
                created_ids = []
            if deleted_ids:
                _remove_deleted_steps(folder, lesson_path, deleted_ids)
                deleted_ids = []
            if plan.get("kind", "push") == "push":  # у отката порядок задаёт снимок, а не файл
                current = {"op": "move"}
                done.extend(_fix_order(client, folder, course_id, lesson_id, lesson_path))
        except StepikError as exc:
            _log(folder, course_id=course_id, lesson_id=lesson_id, operation=current and current["op"],
                 result="error", error=str(exc))
            if created_ids:
                _record_new_ids(folder, lesson_path, created_ids)
            if deleted_ids:
                _remove_deleted_steps(folder, lesson_path, deleted_ids)
            try:
                _refresh_bases(client, folder, done)
            except StepikError:
                pass  # Не заслоняем исходную ошибку; база обновится при следующем скачивании.
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
        if deleted_ids:
            _remove_deleted_steps(folder, lesson_path, deleted_ids)
        _refresh_bases(client, folder, done)
        plan["applied_at"] = _now().isoformat()
        plan["result"] = "ok"
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
        return {
            "status": "published", "plan_id": plan_id, "lesson_id": lesson_id, "done": done,
            "snapshot": str(snapshot), "source_url": f"{client.base_url}/lesson/{lesson_id}",
            "note": "Изменения опубликованы."
                    + (" Новые step_id записаны в файл урока." if created_ids else "")
                    + (" Удалённые шаги убраны из файла урока; их содержимое осталось в snapshot." if deleted_ids else "")
                    + (" Восстановленные шаги получили новые id: скачайте урок заново." if any(
                        op["op"] == "recreate" for op in plan["operations"]) else ""),
        }
    finally:
        lock.rmdir()


# --------------------------------------------------------------------------
# Новый урок из черновика
# --------------------------------------------------------------------------

def _draft_title(text):
    title = _frontmatter_value(text, "title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    match = re.search(r"(?m)^# +(.+?)\s*$", text)
    return match[1].strip() if match else None


def plan_create_lesson(client, course_id, draft_path, module_position, lesson_position=None):
    """План создания урока из локального черновика. Ничего не отправляет.

    Черновик — Markdown в папке курса, ещё не связанный с уроком Stepik: название
    во frontmatter title: или в заголовке «# ...», шаги — метки <!-- new_step -->.
    После применения шаги публикуются обычным plan_push.
    """
    _check_allowed(client, course_id)
    folder = _course_folder(course_id)
    path = _inside(folder, draft_path)
    if path.suffix != ".md" or not path.is_file():
        raise StepikError("Черновик должен быть существующим .md файлом в папке курса", status=400)
    relative = path.relative_to(folder).as_posix()
    manifest = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
    if any(item["path"] == relative for item in manifest["lessons"].values()):
        raise StepikError("Этот файл уже связан с уроком Stepik; для правок используйте stepik_plan_push", status=409)
    data = path.read_bytes()
    text = data.decode("utf-8")
    if _frontmatter_value(text, "lesson_id") is not None:
        raise StepikError("В черновике уже есть lesson_id: это не новый урок", status=409)
    title = _draft_title(text)
    if not title:
        raise StepikError("У черновика нет названия: добавьте title: во frontmatter или строку «# Название»",
                          status=400)
    entries = parse_lesson(text)
    if any(entry["step_id"] for entry in entries):
        raise StepikError("В черновике есть метки step_id других уроков; у нового урока шаги — <!-- new_step -->",
                          status=400)

    course = client.by_ids("courses", [course_id])
    sections = client.by_ids("sections", course[0].get("sections") or []) if course else []
    section = next((row for row in sections if row.get("position") == module_position), None)
    if section is None:
        raise StepikError(f"В курсе {course_id} нет модуля {module_position}", status=404)
    units = section.get("units") or []
    position = len(units) + 1 if lesson_position is None else lesson_position
    if type(position) is not int or not 1 <= position <= len(units) + 1:
        raise StepikError(f"Позиция урока в модуле {module_position}: от 1 до {len(units) + 1}", status=400)

    plan_id = _now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    plans = _inside(folder, ".stepik/plans")
    plans.mkdir(parents=True, exist_ok=True)
    plan = {
        "version": 1, "plan_id": plan_id, "kind": "create_lesson", "created_at": _now().isoformat(),
        "course_id": course_id, "lesson_path": relative, "file_sha256": _sha(data), "title": title,
        "section_id": section["id"], "module_position": module_position, "position": position,
        "section_units": units, "operations": [],
    }
    _atomic_write(_inside(plans, f"{plan_id}.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "planned", "plan_id": plan_id, "course_id": course_id, "kind": "create_lesson",
        "title": title, "module": f"{module_position}. {section.get('title', '')}",
        "lesson_position": f"{module_position}.{position}", "draft": relative,
        "new_steps_in_draft": sum(1 for entry in entries if entry["body"]),
        "note": "На Stepik ничего не отправлено. После подтверждения автора stepik_apply_push создаст пустой урок, "
                "привяжет его к модулю и запишет ID в черновик. Шаги затем публикуются через stepik_plan_push."
                + (" Урок встанет в середину модуля: номера следующих уроков сдвинутся."
                   if position <= len(units) else ""),
    }


def _apply_create_lesson(client, folder, plan_path, plan):
    path = _inside(folder, plan["lesson_path"])
    data = path.read_bytes()
    if _sha(data) != plan["file_sha256"]:
        raise StepikError("LOCAL_CHANGED: черновик изменился после плана. Постройте новый план", status=409)
    sections = client.by_ids("sections", [plan["section_id"]])
    if not sections or (sections[0].get("units") or []) != plan["section_units"]:
        raise StepikError("REMOTE_CHANGED: уроки модуля изменились на Stepik после плана. Постройте новый план",
                          status=409)
    course_id = plan["course_id"]
    lock = _inside(folder, ".stepik-cache.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Папка курса занята другой операцией (.stepik-cache.lock)") from None
    try:
        lesson = client.post("lessons", {"lesson": {"title": plan["title"]}})["lessons"][0]
        _log(folder, course_id=course_id, lesson_id=lesson["id"], operation="create_lesson", result="ok")
        unit = client.post("units", {"unit": {"section": plan["section_id"], "lesson": lesson["id"],
                                              "position": plan["position"]}})["units"][0]
        _log(folder, course_id=course_id, lesson_id=lesson["id"], unit_id=unit["id"], operation="create_unit",
             section_id=plan["section_id"], position=plan["position"], result="ok")

        # Stepik не сдвигает соседние уроки при вставке в середину модуля (проверено на 241862).
        desired = list(plan["section_units"])
        desired.insert(plan["position"] - 1, unit["id"])
        _fix_unit_order(client, folder, course_id, plan["section_id"], desired)

        snapshots = _inside(folder, "snapshots")
        snapshots.mkdir(exist_ok=True)
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        _atomic_write(_inside(snapshots, f"{path.stem}.draft-{stamp}.md"), data.decode("utf-8"))

        old = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
        manifest, _, _ = merge_structure(old, course_structure(client, course_id))
        key = next((key for key, item in manifest["lessons"].items() if item["lesson_id"] == lesson["id"]), None)
        if key is None:
            raise StepikError("Урок создан, но не найден в структуре курса; проверьте модуль на Stepik")
        text = data.decode("utf-8").replace("\r\n", "\n")
        if text.startswith("---\n") and "\n---" in text[4:]:
            text = text[4:].split("\n---", 1)[1].lstrip("\n")  # прежний frontmatter черновика заменяется
        fetched_at = _now().isoformat()
        metadata = {
            "course_id": course_id, "lesson_id": lesson["id"], "unit_id": unit["id"], "position": key,
            "title": plan["title"], "source_url": f"{client.base_url}/lesson/{lesson['id']}",
            "fetched_at": fetched_at, "status": "working_copy", "format": steps.FORMAT,
        }
        head = ["---"] + [f"{name}: {json.dumps(value, ensure_ascii=False)}" for name, value in metadata.items()]
        _atomic_write(path, "\n".join(head + ["---", "", text]), replace=True)
        manifest["lessons"][key].update(path=plan["lesson_path"], title=plan["title"], step_count=0,
                                        fetched_at=fetched_at)
        save_index(folder, manifest)
        plan["applied_at"] = _now().isoformat()
        plan["lesson_id"] = lesson["id"]
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
        expected = f"{plan['module_position']}.{plan['position']}"
        return {
            "status": "applied", "kind": "create_lesson", "lesson_id": lesson["id"], "unit_id": unit["id"],
            "lesson_position": key, "path": str(path),
            **({} if key == expected else {"warning": f"Stepik поставил урок на позицию {key}, а не {expected}"}),
            "note": f"Урок создан. Шаги черновика публикуются через stepik_plan_push(lesson_position=\"{key}\").",
        }
    except StepikError as exc:
        _log(folder, course_id=course_id, operation="create_lesson", result="error", error=str(exc))
        raise
    finally:
        lock.rmdir()


# --------------------------------------------------------------------------
# Порядок, перемещение и удаление уроков
# --------------------------------------------------------------------------

def _section_units(client, section_id):
    """Юниты модуля в порядке Stepik: [(unit_id, lesson_id, position)]."""
    sections = client.by_ids("sections", [section_id])
    if not sections:
        raise StepikError(f"Модуль {section_id} не найден", status=404)
    unit_ids = sections[0].get("units") or []
    rows = {row["id"]: row for row in client.by_ids("units", unit_ids)} if unit_ids else {}
    return [(uid, rows[uid]["lesson"], rows[uid].get("position")) for uid in unit_ids]


def _fix_unit_order(client, folder, course_id, section_id, desired):
    """Выставляет позиции уроков модуля по списку unit_id. Stepik не сдвигает соседей сам."""
    moves = []
    for _ in range(3):
        units = _section_units(client, section_id)
        if [uid for uid, _, _ in units] == desired and [pos for _, _, pos in units] == list(range(1, len(desired) + 1)):
            return moves
        if {uid for uid, _, _ in units} != set(desired):
            raise StepikError("Уроки модуля на Stepik не совпадают с планом: порядок не исправлен")
        lessons = {uid: lesson for uid, lesson, _ in units}
        positions = {uid: pos for uid, _, pos in units}
        for number, uid in enumerate(desired, 1):
            if positions[uid] == number:
                continue
            client.put(f"units/{uid}", {"unit": {"section": section_id, "lesson": lessons[uid], "position": number}})
            _log(folder, course_id=course_id, lesson_id=lessons[uid], unit_id=uid, operation="move_lesson",
                 old_position=positions[uid], new_position=number, result="ok")
            moves.append({"op": "move_lesson", "lesson_id": lessons[uid], "position": number})
    raise StepikError("Порядок уроков модуля на Stepik не совпал с планом после перестановки")


def _refresh_structure(client, folder, course_id):
    """Перечитывает структуру курса; у известных уроков остаются прежние пути."""
    old = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
    manifest, _, _ = merge_structure(old, course_structure(client, course_id))
    save_index(folder, manifest)
    return manifest


def _lesson_plan(client, course_id, lesson_position, kind, **extra):
    _check_allowed(client, course_id)
    folder = _course_folder(course_id)
    manifest = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
    item = manifest["lessons"].get(lesson_position)
    if item is None:
        raise StepikError(f"Урока {lesson_position} нет в .course.json", status=404)
    _verify_lesson_in_course(client, item["lesson_id"], course_id)
    units = _section_units(client, item["section_id"])
    if item["unit_id"] not in [uid for uid, _, _ in units]:
        raise StepikError("Урок не найден в своём модуле на Stepik: обновите структуру через stepik_pull_course",
                          status=409)
    plan_id = _now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    plans = _inside(folder, ".stepik/plans")
    plans.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "plan_id": plan_id, "kind": kind, "created_at": _now().isoformat(),
            "course_id": course_id, "lesson_position": lesson_position, "lesson_id": item["lesson_id"],
            "unit_id": item["unit_id"], "section_id": item["section_id"],
            "section_units": [uid for uid, _, _ in units], "operations": [], **extra}
    _atomic_write(_inside(plans, f"{plan_id}.json"), json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return plan, item, units


def plan_move_lesson(client, course_id, lesson_position, new_position):
    """План перестановки урока внутри его модуля. Ничего не отправляет."""
    module = lesson_position.split(".")[0]
    folder = _course_folder(course_id)
    manifest = json.loads(_inside(folder, ".course.json").read_text(encoding="utf-8"))
    count = sum(1 for key in manifest["lessons"] if key.split(".")[0] == module)
    if type(new_position) is not int or not 1 <= new_position <= count:
        raise StepikError(f"Новая позиция урока в модуле {module}: от 1 до {count}", status=400)
    plan, item, units = _lesson_plan(client, course_id, lesson_position, "move_lesson", new_position=new_position)
    if [uid for uid, _, _ in units].index(item["unit_id"]) + 1 == new_position:
        _inside(folder, f".stepik/plans/{plan['plan_id']}.json").unlink()
        return {"status": "nothing_to_push", "note": f"Урок уже стоит на позиции {module}.{new_position}."}
    return {"status": "planned", "plan_id": plan["plan_id"], "kind": "move_lesson",
            "lesson": f"{lesson_position} {item.get('title', '')}".strip(),
            "move_to": f"{module}.{new_position}",
            "note": "На Stepik ничего не отправлено. Номера уроков модуля между старой и новой позицией сдвинутся. "
                    "Применяется stepik_apply_push после подтверждения автора."}


def plan_delete_lesson(client, course_id, lesson_position):
    """План удаления урока со Stepik. Ничего не отправляет."""
    plan, item, _ = _lesson_plan(client, course_id, lesson_position, "delete_lesson")
    step_ids, sources = _remote_state(client, item["lesson_id"])
    learners = max([sources[sid].get("passed_by") or 0 for sid in step_ids] or [0])
    plan["remote_fingerprint"] = _fingerprint(step_ids, sources)
    folder = _course_folder(course_id)
    _atomic_write(_inside(folder, f".stepik/plans/{plan['plan_id']}.json"),
                  json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
    return {"status": "planned", "plan_id": plan["plan_id"], "kind": "delete_lesson",
            "lesson": f"{lesson_position} {item.get('title', '')}".strip(), "lesson_id": item["lesson_id"],
            "step_count": len(step_ids), **({"решили учеников (до)": learners} if learners else {}),
            "confirm_delete": [item["lesson_id"]],
            "note": "На Stepik ничего не отправлено. Удаление урока на Stepik не отменить: пропадут шаги, решения и "
                    "комментарии учеников. Шаги сохранятся в snapshots/, локальный файл урока не удаляется. "
                    "Передайте confirm_delete=[lesson_id] в stepik_apply_push, только если автор подтвердил удаление."}


def _apply_lesson_plan(client, folder, plan_path, plan, confirm_delete):
    course_id, lesson_id, section_id = plan["course_id"], plan["lesson_id"], plan["section_id"]
    if plan["kind"] == "delete_lesson" and sorted(set(confirm_delete or [])) != [lesson_id]:
        raise StepikError(f"DELETE_NOT_CONFIRMED: план удаляет урок {lesson_id}. Передайте confirm_delete=[{lesson_id}], "
                          "только если автор подтвердил удаление", status=409)
    if [uid for uid, _, _ in _section_units(client, section_id)] != plan["section_units"]:
        raise StepikError("REMOTE_CHANGED: уроки модуля изменились на Stepik после плана. Постройте новый план",
                          status=409)
    lock = _inside(folder, ".stepik-cache.lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise StepikError("Папка курса занята другой операцией (.stepik-cache.lock)") from None
    try:
        done = []
        desired = [uid for uid in plan["section_units"] if uid != plan["unit_id"]]
        if plan["kind"] == "move_lesson":
            desired.insert(plan["new_position"] - 1, plan["unit_id"])
            snapshot = None
        else:
            step_ids, sources = _remote_state(client, lesson_id)
            if _fingerprint(step_ids, sources) != plan["remote_fingerprint"]:
                raise StepikError("REMOTE_CHANGED: урок на Stepik изменился после плана. Постройте новый план",
                                  status=409)
            title = (client.by_ids("lessons", [lesson_id]) or [{}])[0].get("title")
            snapshots = _inside(folder, "snapshots")
            snapshots.mkdir(exist_ok=True)
            snapshot = _inside(snapshots, f"{_now().strftime('%Y-%m-%d_%H%M%S')}-{plan['plan_id'][-6:]}"
                                          f"-deleted-lesson-{lesson_id}.json")
            _atomic_write(snapshot, json.dumps({"lesson_id": lesson_id, "title": title, "section_id": section_id,
                                                "position": plan["section_units"].index(plan["unit_id"]) + 1,
                                                "steps": step_ids, "step_sources": [sources[s] for s in step_ids]},
                                               ensure_ascii=False, indent=2) + "\n")
            client.delete(f"units/{plan['unit_id']}")
            _log(folder, course_id=course_id, lesson_id=lesson_id, unit_id=plan["unit_id"], operation="delete_unit",
                 result="ok")
            client.delete(f"lessons/{lesson_id}")
            _log(folder, course_id=course_id, lesson_id=lesson_id, operation="delete_lesson", result="ok")
            done.append({"op": "delete_lesson", "lesson_id": lesson_id})
        done += _fix_unit_order(client, folder, course_id, section_id, desired)
        manifest = _refresh_structure(client, folder, course_id)
        plan["applied_at"] = _now().isoformat()
        plan["result"] = "ok"
        _atomic_write(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", replace=True)
        key = next((key for key, item in manifest["lessons"].items() if item["lesson_id"] == lesson_id), None)
        return {"status": "published", "kind": plan["kind"], "lesson_id": lesson_id, "done": done,
                **({"lesson_position": key} if key else {}),
                **({"snapshot": str(snapshot)} if snapshot else {}),
                "note": ("Урок перемещён; номера уроков модуля в .course.json обновлены." if plan["kind"] == "move_lesson"
                         else "Урок удалён со Stepik. Шаги сохранены в snapshot, локальный файл урока остался на месте.")}
    except StepikError as exc:
        _log(folder, course_id=course_id, lesson_id=lesson_id, operation=plan["kind"], result="error", error=str(exc))
        raise
    finally:
        lock.rmdir()
