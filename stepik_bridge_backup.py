#!/usr/bin/env python3
"""
stepik_bridge.py — read-only мост между Stepik и LLM-чатом.

Ходит в Stepik API с твоим OAuth-токеном и отдаёт содержимое курса/урока/шага
обычным текстом (markdown) по публичной ссылке, защищённой секретом в пути.

Запуск:
    export STEPIK_CLIENT_ID=...
    export STEPIK_CLIENT_SECRET=...
    export BRIDGE_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
    python3 stepik_bridge.py

Точки:
    GET /<TOKEN>/course/<id>     — структура курса: секции, уроки, шаги, все id
    GET /<TOKEN>/lesson/<id>     — все шаги урока целиком
    GET /<TOKEN>/step/<id>       — один шаг
    GET /<TOKEN>/resolve?url=... — принимает вставленный URL со Stepik
    GET /<TOKEN>/health          — проверка токена и связи с API

Параметры:
    ?raw=1     — не конвертировать HTML в markdown, отдать как есть
    ?json=1    — сырой ответ Stepik API
    ?answers=0 — не включать правильные ответы в вывод тестов

Только чтение. Ни одного POST/PUT/DELETE в сторону Stepik здесь нет.
"""

import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_URL = os.environ.get("STEPIK_BASE_URL", "https://stepik.org").rstrip("/")
CLIENT_ID = os.environ.get("STEPIK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STEPIK_CLIENT_SECRET", "")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")
TIMEOUT = int(os.environ.get("STEPIK_TIMEOUT", "30"))

# Ограничение на число id в одном запросе: Stepik отбивает слишком длинный заголовок.
IDS_CHUNK = 20


# --------------------------------------------------------------------------
# Stepik API
# --------------------------------------------------------------------------

class StepikError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class StepikClient:
    """Минимальный клиент Stepik API. Только GET."""

    def __init__(self, client_id, client_secret, base_url=BASE_URL):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self._token = None
        self._expires_at = 0.0

    # -- авторизация -------------------------------------------------------

    def _fetch_token(self):
        creds = f"{self.client_id}:{self.client_secret}".encode()
        req = urllib.request.Request(
            f"{self.base_url}/oauth2/token/",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": "Basic " + base64.b64encode(creds).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise StepikError(
                f"Stepik отклонил client_credentials ({exc.code}). "
                "Проверь CLIENT_ID/CLIENT_SECRET и что у приложения "
                "Client type = Confidential, Grant type = Client credentials.",
                status=401,
            ) from exc
        except urllib.error.URLError as exc:
            raise StepikError(f"Не достучаться до Stepik: {exc.reason}") from exc

        self._token = payload["access_token"]
        # Обновляемся заранее, чтобы не поймать протухший токен на середине обхода.
        self._expires_at = time.time() + int(payload.get("expires_in", 36000)) - 300

    def token(self):
        if not self._token or time.time() >= self._expires_at:
            self._fetch_token()
        return self._token

    # -- запросы -----------------------------------------------------------

    def get(self, endpoint, **params):
        """Один GET. Возвращает распарсенный JSON целиком (с meta)."""
        query = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                query.extend((f"{key}[]", str(item)) for item in value)
            else:
                query.append((key, str(value)))
        url = f"{self.base_url}/api/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + self.token()}
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise StepikError(
                    f"403 на {endpoint}. Скорее всего у аккаунта, которому "
                    "принадлежит OAuth-приложение, нет авторских прав на этот курс.",
                    status=403,
                ) from exc
            raise StepikError(f"Stepik вернул {exc.code} на {endpoint}", status=502) from exc
        except urllib.error.URLError as exc:
            raise StepikError(f"Сеть до Stepik: {exc.reason}") from exc

    def collect(self, endpoint, key=None, max_pages=50, **params):
        """GET с проходом по всем страницам. Возвращает плоский список объектов."""
        key = key or endpoint
        out = []
        page = 1
        while page <= max_pages:
            payload = self.get(endpoint, page=page, **params)
            out.extend(payload.get(key, []))
            if not payload.get("meta", {}).get("has_next"):
                break
            page += 1
        return out

    def by_ids(self, endpoint, ids, key=None):
        """Батч-запрос по списку id. Пагинации в запросах с ids[] нет."""
        key = key or endpoint
        ids = [i for i in ids if i]
        out = []
        for start in range(0, len(ids), IDS_CHUNK):
            chunk = ids[start:start + IDS_CHUNK]
            out.extend(self.get(endpoint, ids=chunk).get(key, []))
        return out


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
        template = (src.get("code") or {}).get("code_templates") or {}
        for lang, body in template.items():
            lines += [f"**Шаблон ({lang}):**", "```" + lang, body or "", "```", ""]
        tests = (src.get("code") or {}).get("samples") or src.get("samples") or []
        if answers and tests:
            lines.append("**Тесты:**")
            for test in tests[:20]:
                if isinstance(test, (list, tuple)) and len(test) >= 2:
                    lines.append(f"- вход `{test[0]}` → выход `{test[1]}`")
            lines.append("")

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


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

CLIENT = StepikClient(CLIENT_ID, CLIENT_SECRET)


class Handler(BaseHTTPRequestHandler):
    server_version = "stepik-bridge"
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200, content_type="text/plain; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # Секрет не должен попадать в логи.
        line = fmt % args
        if BRIDGE_TOKEN:
            line = line.replace(BRIDGE_TOKEN, "<TOKEN>")
        sys.stderr.write(f"{self.address_string()} - {line}\n")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)

        # Секрет сверяем всегда первым и в постоянное время.
        if not parts or not BRIDGE_TOKEN:
            return self._send("not found", 404)
        supplied = parts[0]
        if len(supplied) != len(BRIDGE_TOKEN) or not _constant_eq(supplied, BRIDGE_TOKEN):
            return self._send("not found", 404)

        raw = query.get("raw", ["0"])[0] == "1"
        as_json = query.get("json", ["0"])[0] == "1"
        answers = query.get("answers", ["1"])[0] != "0"
        rest = parts[1:]

        try:
            if rest == ["health"]:
                me = CLIENT.get("stepics/1")
                user = (me.get("users") or [{}])[0]
                return self._send(
                    f"ok\naccount: {user.get('full_name')} (id {user.get('id')})\n"
                )

            if len(rest) == 2 and rest[0] == "course":
                if as_json:
                    return self._json(CLIENT.get("courses", ids=[int(rest[1])]))
                return self._send(render_course(CLIENT, int(rest[1])))

            if len(rest) == 2 and rest[0] == "lesson":
                if as_json:
                    return self._json(CLIENT.get("lessons", ids=[int(rest[1])]))
                return self._send(render_lesson(CLIENT, int(rest[1]), raw, answers))

            if len(rest) == 2 and rest[0] == "step":
                sources = CLIENT.by_ids("step-sources", [int(rest[1])], key="step-sources")
                if not sources:
                    return self._send("шаг не найден", 404)
                if as_json:
                    return self._json({"step-sources": sources})
                return self._send(render_step(sources[0], raw, answers))

            if rest == ["resolve"]:
                url = query.get("url", [""])[0]
                return self._send(resolve_url(CLIENT, url, raw, answers))

            return self._send(
                "точки: /<token>/course/<id>, /<token>/lesson/<id>, "
                "/<token>/step/<id>, /<token>/resolve?url=..., /<token>/health\n",
                404,
            )
        except StepikError as exc:
            return self._send(f"ошибка: {exc}\n", exc.status)
        except ValueError:
            return self._send("id должен быть числом\n", 400)

    def _json(self, payload):
        self._send(
            json.dumps(payload, ensure_ascii=False, indent=2),
            content_type="application/json; charset=utf-8",
        )


def _constant_eq(a, b):
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


def main():
    missing = [
        name for name, value in (
            ("STEPIK_CLIENT_ID", CLIENT_ID),
            ("STEPIK_CLIENT_SECRET", CLIENT_SECRET),
            ("BRIDGE_TOKEN", BRIDGE_TOKEN),
        ) if not value
    ]
    if missing:
        sys.exit("Не заданы переменные окружения: " + ", ".join(missing))
    if len(BRIDGE_TOKEN) < 24:
        sys.exit("BRIDGE_TOKEN слишком короткий — нужно минимум 24 символа.")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"stepik-bridge слушает http://{HOST}:{PORT}/{BRIDGE_TOKEN[:6]}.../", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
