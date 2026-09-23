"""Преобразование HTML шагов Stepik в Markdown и рендер курса, урока и шага."""

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

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.list_stack = []      # 'ul' | 'ol'
        self.counters = []
        self.in_pre = False
        self.pre_lang = ""

    # -- вывод -------------------------------------------------------------

    def _emit(self, text):
        if text:
            self.parts.append(text)

    def _newline(self, count=1):
        # Не плодим пустые строки на границах блоков.
        while self.parts and self.parts[-1] == "\n":
            self.parts.pop()
        if self.parts:
            self._emit("\n" * count)

    # -- теги --------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br":
            self._emit("  \n")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.in_pre:
            self._emit("`")
        elif tag == "pre":
            self.in_pre = True
            self.pre_lang = ""
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
            self._newline(2)
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
        elif tag == "blockquote":
            self._newline(2)
            self._emit("> ")
        elif tag in self.BLOCK:
            self._newline(2)

        if tag == "code" and self.in_pre:
            # Язык обычно висит на <code class="language-python">.
            cls = attrs.get("class", "")
            match = re.search(r"(?:language|lang)-([\w+#-]+)", cls)
            if match:
                self.pre_lang = match.group(1)

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.in_pre:
            self._emit("`")
        elif tag == "pre":
            self.in_pre = False
            self._newline(1)
            self._emit("```\n")
        elif tag == "a":
            self._emit("]")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.counters.pop()
            self._newline(2)
        elif tag == "li":
            self._newline(1)
        elif tag in self.BLOCK:
            self._newline(2)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.in_pre:
            if not any(part.startswith("```") for part in self.parts[-2:]):
                self._emit("```" + self.pre_lang + "\n")
            self._emit(data)
        else:
            # Внутри обычного текста схлопываем пробелы, но не съедаем всё.
            text = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", " ", data)
            text = re.sub(r"[ \t]{2,}", " ", text)
            if text.strip() or (self.parts and not self.parts[-1].endswith(("\n", " "))):
                self._emit(text)

    def result(self):
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(source):
    if not source:
        return ""
    parser = _Md()
    parser.feed(source)
    parser.close()
    return parser.result()


def clean(source, raw=False):
    return html.unescape(source or "") if raw else html_to_md(source)


# --------------------------------------------------------------------------
# Рендер шагов
# --------------------------------------------------------------------------

def render_step(step_source, raw=False, answers=True):
    """step-source -> markdown. Понимает и теорию, и все типы тестов."""
    block = step_source.get("block") or {}
    kind = block.get("name", "unknown")
    lines = [f"### Шаг {step_source.get('id')} · `{kind}`", ""]

    text = clean(block.get("text"), raw)
    if text:
        lines += [text, ""]

    src = block.get("source") or {}

    if kind in ("choice", "sorting", "matching"):
        options = src.get("options") or src.get("pairs") or []
        if options:
            lines.append("**Варианты:**")
            for index, option in enumerate(options, 1):
                if "first" in option:  # matching
                    left = clean(option.get("first"), raw)
                    right = clean(option.get("second"), raw)
                    lines.append(f"{index}. {left} → {right}")
                else:
                    label = clean(option.get("text"), raw)
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
