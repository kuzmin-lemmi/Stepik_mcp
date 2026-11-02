"""Преобразование HTML шагов Stepik в Markdown и обратно, рендер курса, урока и шага."""

import html
import json
import re
from html.parser import HTMLParser

from .client import BASE_URL, StepikError


# --------------------------------------------------------------------------
# HTML -> markdown
# --------------------------------------------------------------------------

class _Md(HTMLParser):
    """Компактный конвертер того подмножества HTML, что реально встречается
    в шагах Stepik. Всё незнакомое разворачивается в свой текст."""

    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
             "ul", "ol", "li", "pre", "blockquote", "table", "tr"}

    def __init__(self, legacy=False):
        super().__init__(convert_charrefs=True)
        # legacy=True повторяет вывод до исправления списков: так скачаны старые файлы.
        self.legacy = legacy
        self.item_fresh = False   # только что выведен маркер пункта списка
        self.parts = []
        self.list_stack = []      # 'ul' | 'ol'
        self.counters = []
        self.in_pre = False
        self.pre_lang = ""
        self.pre_text = []
        self.in_code = False

    # -- вывод -------------------------------------------------------------

    def _emit(self, text):
        if text:
            self.parts.append(text)
            self.item_fresh = False

    def _newline(self, count=1):
        # Не плодим пустые строки на границах блоков.
        while self.parts and self.parts[-1] == "\n":
            self.parts.pop()
        if self.parts:
            self._emit("\n" * count)

    # -- теги --------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.in_pre and not self.legacy and tag not in ("pre", "code"):
            # Внутри блока кода разметки нет: только текст и переносы строк.
            if tag == "br":
                self.pre_text.append("\n")
            return
        if tag == "br":
            self._emit("  \n")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.in_pre:
            self._emit("`")
            self.in_code = True
        elif tag == "pre":
            self.in_pre = True
            self.pre_lang = ""
            self.pre_text = []
            self._newline(2)
        elif tag == "a":
            self._emit("[")
        elif tag == "img":
            src = attrs.get("src", "")
            alt = attrs.get("alt", "изображение")
            self._emit(f"![{alt}]({src})")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline(2)
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            # Вложенный список идёт сразу под пунктом, без пустой строки.
            self._newline(1 if self.list_stack and not self.legacy else 2)
            self.list_stack.append(tag)
            self.counters.append(0)
        elif tag == "li":
            self._newline(1)
            depth = max(len(self.list_stack) - 1, 0)
            indent = "  " * depth
            if self.list_stack and self.list_stack[-1] == "ol":
                self.counters[-1] += 1
                self._emit(f"{indent}{self.counters[-1]}. ")
            else:
                self._emit(f"{indent}- ")
            self.item_fresh = True
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag in ("p", "div") and self.list_stack and not self.legacy:
            # Редактор Stepik оборачивает текст пункта в <p>: первый абзац идёт
            # сразу за маркером, следующие продолжают тот же пункт.
            if not self.item_fresh:
                self._emit("  \n" + "  " * len(self.list_stack))
        elif tag in self.BLOCK:
            self._newline(2)

        if tag == "code" and self.in_pre:
            # Язык обычно висит на <code class="language-python">.
            cls = attrs.get("class", "")
            match = re.search(r"(?:language|lang)-([\w+#-]+)", cls)
            if match:
                self.pre_lang = match.group(1)

    def handle_endtag(self, tag):
        if self.in_pre and not self.legacy and tag not in ("pre", "code"):
            return
        if tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.in_pre:
            self._emit("`")
            self.in_code = False
        elif tag == "pre":
            self.in_pre = False
            if self.legacy:
                self._newline(1)
                self._emit("```\n")
            else:
                # Блок кода выводится целиком: ограда открывается всегда и длиннее
                # любых ``` внутри кода.
                code = "".join(self.pre_text).rstrip("\n")
                fence = code_fence(code)
                self._emit(f"{fence}{self.pre_lang}\n{code}\n{fence}\n")
        elif tag == "a":
            self._emit("]")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.counters.pop()
            self._newline(1 if self.list_stack and not self.legacy else 2)
        elif tag == "li":
            self._newline(1)
        elif tag in ("p", "div") and self.list_stack and not self.legacy:
            pass
        elif tag in self.BLOCK:
            self._newline(2)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.in_pre and not self.legacy:
            self.pre_text.append(data)
        elif self.in_pre:
            # Прежний вывод: у второго блока кода подряд терялась открывающая ограда.
            if not any(part.startswith("```") for part in self.parts[-2:]):
                self._emit("```" + self.pre_lang + "\n")
            self._emit(data)
        else:
            # Внутри обычного текста схлопываем пробелы, но не съедаем всё.
            text = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", " ", data)
            text = re.sub(r"[ \t]{2,}", " ", text)
            if not self.legacy and not self.in_code:
                # Буквальные ` в тексте не должны открывать код в Markdown.
                text = text.replace("`", "\\`")
            if text.strip() or (self.parts and not self.parts[-1].endswith(("\n", " "))):
                self._emit(text)

    def result(self):
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def code_fence(text):
    """Ограда из обратных кавычек длиннее любой их цепочки внутри текста."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def html_to_md(source, legacy=False):
    if not source:
        return ""
    parser = _Md(legacy=legacy)
    parser.feed(source)
    parser.close()
    return parser.result()


# --------------------------------------------------------------------------
# Markdown -> HTML
# --------------------------------------------------------------------------

def _inline(text):
    """Строчная разметка: `код`, картинки, ссылки, **жирный**, *курсив*."""
    out = []
    # \` — буквальная обратная кавычка, а не начало кода.
    text = text.replace("\\`", "\x00")
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
    return "".join(out).replace("\x00", "`")


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
        fence = re.match(r"^(`{3,})\s*([\w+#-]*)\s*$", line)
        if fence:
            body = []
            index += 1
            while index < len(lines) and not (
                    re.fullmatch(r"`{3,}", lines[index].strip()) and len(lines[index].strip()) >= len(fence[1])):
                body.append(lines[index])
                index += 1
            index += 1
            lang = f' class="language-{fence[2]}"' if fence[2] else ""
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


def clean(source, raw=False, legacy=False):
    return html.unescape(source or "") if raw else html_to_md(source, legacy)


# --------------------------------------------------------------------------
# Рендер шагов
# --------------------------------------------------------------------------

def render_step(step_source, raw=False, answers=True, legacy=False):
    """step-source -> markdown. Понимает и теорию, и все типы тестов."""
    block = step_source.get("block") or {}
    kind = block.get("name", "unknown")
    lines = [f"### Шаг {step_source.get('id')} · `{kind}`", ""]

    text = clean(block.get("text"), raw, legacy)
    if text:
        lines += [text, ""]

    src = block.get("source") or {}

    if kind in ("choice", "sorting", "matching"):
        options = src.get("options") or src.get("pairs") or []
        if options:
            lines.append("**Варианты:**")
            for index, option in enumerate(options, 1):
                if "first" in option:  # matching
                    left = clean(option.get("first"), raw, legacy)
                    right = clean(option.get("second"), raw, legacy)
                    lines.append(f"{index}. {left} → {right}")
                else:
                    label = clean(option.get("text"), raw, legacy)
                    if answers and "is_correct" in option:
                        mark = "[x]" if option["is_correct"] else "[ ]"
                        lines.append(f"{index}. {mark} {label}")
                    else:
                        lines.append(f"{index}. {label}")
            lines.append("")

    elif kind in ("string", "number", "math", "free-answer"):
        if answers and src.get("answer") is not None:
            lines += [f"**Ответ:** `{src['answer']}`", ""]
        if src.get("max_error") is not None:
            lines += [f"**Допуск:** {src['max_error']}", ""]

    elif kind == "code":
        # В code-шагах Stepik пользовательские шаблоны и примеры лежат
        # в block["options"]. block["source"]["code"] — это строка с
        # авторским checker-кодом, поэтому вызывать на ней .get() нельзя.
        options = block.get("options") or {}

        templates = options.get("code_templates") or {}
        for lang, body in templates.items():
            lines += [f"**Шаблон ({lang}):**", "```" + lang, body or "", "```", ""]

        samples = options.get("samples") or []
        if answers and samples:
            lines.append("**Примеры / открытые тесты:**")
            for test in samples[:20]:
                if isinstance(test, (list, tuple)) and len(test) >= 2:
                    lines += ["```text", str(test[0]), "```", "**Ожидаемый вывод:**",
                              "```text", str(test[1]), "```", ""]

        # В авторском step-source дополнительные тесты могут лежать в source.test_cases.
        # Не дублируем их, если они совпадают с samples.
        test_cases = src.get("test_cases") or []
        if answers and test_cases and test_cases != samples:
            lines.append("**Авторские тесты:**")
            for test in test_cases[:20]:
                if isinstance(test, (list, tuple)) and len(test) >= 2:
                    lines += ["```text", str(test[0]), "```", "**Ожидаемый вывод:**",
                              "```text", str(test[1]), "```", ""]

    elif kind == "video":
        lines += ["_(видео-шаг, текста нет)_", ""]

    if raw and src and kind not in ("text", "video"):
        lines += ["<details><summary>source</summary>", "",
                  "```json", json.dumps(src, ensure_ascii=False, indent=2), "```",
                  "", "</details>", ""]

    return "\n".join(lines)


def render_lesson(client, lesson_id, raw=False, answers=True):
    lessons = client.get("lessons", **{"ids": [lesson_id]}).get("lessons", [])
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден или недоступен", status=404)
    lesson = lessons[0]

    out = [f"# {lesson.get('title', 'Без названия')}",
           f"_lesson {lesson_id} · {BASE_URL}/lesson/{lesson_id}_", ""]

    step_ids = lesson.get("steps", [])
    if not step_ids:
        out.append("_(в уроке нет шагов)_")
        return "\n".join(out)

    sources = client.by_ids("step-sources", step_ids, key="step-sources")
    order = {sid: pos for pos, sid in enumerate(step_ids)}
    sources.sort(key=lambda s: order.get(s.get("id"), 1e9))

    for source in sources:
        out += [render_step(source, raw=raw, answers=answers), ""]
    return "\n".join(out)


def render_course(client, course_id):
    courses = client.get("courses", **{"ids": [course_id]}).get("courses", [])
    if not courses:
        raise StepikError(f"Курс {course_id} не найден или недоступен", status=404)
    course = courses[0]

    out = [f"# {course.get('title')}",
           f"_course {course_id} · {BASE_URL}/course/{course_id}_", ""]

    sections = client.by_ids("sections", course.get("sections", []), key="sections")
    for section in sections:
        out.append(f"## {section.get('position')}. {section.get('title')}")
        units = client.by_ids("units", section.get("units", []), key="units")
        units.sort(key=lambda u: u.get("position", 0))
        lessons = {
            lesson["id"]: lesson
            for lesson in client.by_ids(
                "lessons", [u.get("lesson") for u in units], key="lessons"
            )
        }
        for unit in units:
            lesson = lessons.get(unit.get("lesson"))
            if not lesson:
                continue
            steps = len(lesson.get("steps", []))
            out.append(
                f"- **{lesson.get('title')}** — `lesson/{lesson['id']}`, "
                f"шагов: {steps}"
            )
        out.append("")
    return "\n".join(out)


STEPIK_URL_RE = re.compile(
    r"stepik\.org/(?:course/(?P<course>\d+)|lesson/(?P<lesson>\d+)(?:/step/(?P<step>\d+))?)"
)


def resolve_url(client, url, raw=False, answers=True):
    """Принимает URL, скопированный из адресной строки Stepik."""
    match = STEPIK_URL_RE.search(url)
    if not match:
        raise StepikError(f"Не разобрал ссылку: {url}", status=400)
    if match.group("course"):
        return render_course(client, int(match.group("course")))

    lesson_id = int(match.group("lesson"))
    if not match.group("step"):
        return render_lesson(client, lesson_id, raw, answers)

    # В URL позиция шага (1, 2, 3...), а не его id.
    lessons = client.get("lessons", **{"ids": [lesson_id]}).get("lessons", [])
    if not lessons:
        raise StepikError(f"Урок {lesson_id} недоступен", status=404)
    position = int(match.group("step"))
    step_ids = lessons[0].get("steps", [])
    if not 1 <= position <= len(step_ids):
        raise StepikError(f"В уроке {lesson_id} нет шага {position}", status=404)
    source = client.by_ids("step-sources", [step_ids[position - 1]], key="step-sources")
    if not source:
        raise StepikError("Шаг недоступен", status=404)
    return render_step(source[0], raw=raw, answers=answers)
