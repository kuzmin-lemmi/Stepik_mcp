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
import math
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


# --------------------------------------------------------------------------
# Feedback and analytics (read-only)
# --------------------------------------------------------------------------


def _first_value(obj, *names):
    """Return the first non-None field from a dict."""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return None


def _fmt_number(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        if 0 <= value <= 1:
            return f"{value * 100:.1f}%"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _collect_limited(client, endpoint, key=None, max_items=1000, max_pages=50, **params):
    """Paginated GET with a hard item cap so a tool call cannot explode in size."""
    key = key or endpoint
    out = []
    page = 1
    truncated = False
    while page <= max_pages:
        payload = client.get(endpoint, page=page, **params)
        batch = payload.get(key, []) or []
        room = max(max_items - len(out), 0)
        if room <= 0:
            truncated = True
            break
        out.extend(batch[:room])
        if len(batch) > room:
            truncated = True
            break
        meta = payload.get("meta", {}) or {}
        if not meta.get("has_next"):
            break
        page += 1
    if page > max_pages:
        truncated = True
    return out, truncated


def _user_id(item):
    return _first_value(item, "user", "user_id", "userId", "author", "author_id")


def _load_users(client, items):
    ids = []
    for item in items:
        uid = _user_id(item)
        if isinstance(uid, int) or (isinstance(uid, str) and uid.isdigit()):
            ids.append(int(uid))
    users = client.by_ids("users", sorted(set(ids)), key="users") if ids else []
    return {u.get("id"): u for u in users}


def _display_user(item, users):
    uid = _user_id(item)
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        uid_int = None
    user = users.get(uid_int, {}) if uid_int is not None else {}
    name = user.get("full_name") or user.get("first_name") or user.get("username")
    if name and uid_int is not None:
        return f"{name} (id {uid_int})"
    if name:
        return str(name)
    if uid_int is not None:
        return f"user {uid_int}"
    return "неизвестный пользователь"


def _comments_for_steps(client, step_ids):
    """Load default discussions and their replies, batching across lesson steps."""
    steps = {s["id"]: s for s in client.by_ids("steps", step_ids)}
    for step_id in step_ids:
        if step_id not in steps:
            raise StepikError(f"Шаг {step_id} не найден или недоступен", status=404)
        if not steps[step_id].get("discussion_proxy"):
            raise StepikError(f"API не вернул discussion_proxy для шага {step_id}")

    # The default proxy excludes the separate shared-solutions thread.
    proxy_ids = list(dict.fromkeys(steps[sid]["discussion_proxy"] for sid in step_ids))
    proxies = {p["id"]: p for p in client.by_ids("discussion-proxies", proxy_ids)}
    pending = {}
    groups = {sid: [] for sid in step_ids}
    for step_id in step_ids:
        proxy = proxies.get(steps[step_id]["discussion_proxy"])
        if proxy is None or not isinstance(proxy.get("discussions"), list):
            raise StepikError(f"API не вернул список обсуждений для шага {step_id}")
        for comment_id in proxy["discussions"]:
            pending[comment_id] = step_id

    seen = set()
    while pending:
        rows = {c["id"]: c for c in client.by_ids("comments", list(pending))}
        following = {}
        for comment_id, step_id in pending.items():
            comment = rows.get(comment_id)
            if comment is None:
                raise StepikError(
                    f"Комментарий {comment_id} недоступен; не удалось получить полное обсуждение."
                )
            if str(comment.get("target")) != str(step_id):
                raise StepikError(f"API вернул комментарий {comment_id} от другого шага")
            replies = comment.get("replies")
            if not isinstance(replies, list):
                raise StepikError(f"API не вернул список ответов для комментария {comment_id}")
            groups[step_id].append(comment)
            seen.add(comment_id)
            for reply_id in replies:
                if reply_id not in seen and reply_id not in pending:
                    following[reply_id] = step_id
        pending = following

    comments = [c for group in groups.values() for c in group]
    return groups, _load_users(client, comments)


def step_comments(client, step_id):
    """All default-thread comments attached to a step, including replies. GET only."""
    groups, users = _comments_for_steps(client, [step_id])
    return groups[step_id], users


def _render_comments(comments, users):
    lines = []
    for c in comments:
        author = _display_user(c, users)
        date = _first_value(c, "time", "create_date", "created_at", "date") or ""
        text = clean(_first_value(c, "text", "body", "message") or "", raw=False)
        parent = _first_value(c, "parent", "parent_id")
        meta = [f"comment {c['id']}", author]
        if date:
            meta.append(str(date))
        if parent:
            meta.append(f"ответ на comment {parent}")
        lines += ["### " + " · ".join(meta), "", text or "_(без текста)_", ""]
    return lines


def render_step_comments(client, step_id):
    comments, users = step_comments(client, step_id)
    lines = [f"# Комментарии к шагу {step_id}", ""]
    lines += _render_comments(comments, users)
    if not comments:
        lines.append("Комментариев в основном обсуждении шага нет.")
    return "\n".join(lines)


def render_lesson_comments(client, lesson_id):
    lessons = client.get("lessons", ids=[lesson_id]).get("lessons", [])
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден или недоступен", status=404)
    step_ids = lessons[0].get("steps", []) or []
    lines = [f"# Комментарии к уроку {lesson_id}", ""]
    groups, users = _comments_for_steps(client, step_ids)
    total = 0
    for pos, step_id in enumerate(step_ids, 1):
        comments = groups[step_id]
        if not comments:
            continue
        total += len(comments)
        lines += [f"## Шаг {pos} · step {step_id}", ""]
        lines += _render_comments(comments, users)
    if total == 0:
        lines.append("Комментариев в основных обсуждениях урока нет.")
    return "\n".join(lines)


def _parse_reply(reply):
    """Normalize Stepik submission reply while preserving code/text."""
    value = reply
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                pass
    return value


def _submission_text(submission, max_chars=8000):
    value = _parse_reply(submission.get("reply"))
    language = _first_value(submission, "language", "lang")
    if isinstance(value, dict):
        language = _first_value(value, "language", "lang") or language
        body = _first_value(value, "code", "text", "answer", "reply")
        if body is None:
            body = json.dumps(value, ensure_ascii=False, indent=2)
    elif isinstance(value, (list, tuple)):
        body = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        body = "" if value is None else str(value)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n… [обрезано]"
    return body, language


def step_submissions(client, step_id, max_items=2000):
    return _collect_limited(
        client, "submissions", key="submissions", max_items=max_items, step=step_id,
        max_pages=max(1, min(200, (max_items + 19) // 20 + 2)),
    )


def _status_counts(submissions):
    counts = {}
    for s in submissions:
        status = str(s.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _metric_scan(obj, terms, path="", depth=0, max_depth=4):
    """Find small scalar/list statistics in API payloads without dumping content."""
    found = []
    if depth > max_depth:
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else key
            low = key.lower()
            if any(term in low for term in terms):
                if isinstance(value, (str, int, float, bool)) or value is None:
                    found.append((here, value))
                elif isinstance(value, list) and len(value) <= 10 and all(
                    isinstance(x, (str, int, float, bool, type(None))) for x in value
                ):
                    found.append((here, value))
                elif isinstance(value, dict) and len(value) <= 10 and all(
                    isinstance(x, (str, int, float, bool, type(None))) for x in value.values()
                ):
                    found.append((here, value))
            if isinstance(value, (dict, list)):
                found.extend(_metric_scan(value, terms, here, depth + 1, max_depth))
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:20]):
            if isinstance(value, (dict, list)):
                found.extend(_metric_scan(value, terms, f"{path}[{i}]", depth + 1, max_depth))
    # preserve order but remove exact duplicates
    result = []
    seen = set()
    for item in found:
        sig = (item[0], repr(item[1]))
        if sig not in seen:
            seen.add(sig)
            result.append(item)
    return result


def _course_units_for_lesson(client, course_id, lesson_id):
    courses = client.get("courses", ids=[course_id]).get("courses", []) or []
    if not courses:
        raise StepikError(f"Курс {course_id} не найден или недоступен", status=404)
    section_ids = courses[0].get("sections")
    if not isinstance(section_ids, list):
        raise StepikError(f"API не вернул структуру курса {course_id}")
    sections = client.by_ids("sections", section_ids, key="sections")
    if set(section_ids) - {s.get("id") for s in sections}:
        raise StepikError(f"Не все секции курса {course_id} доступны; принадлежность урока не проверена")
    unit_ids = []
    for section in sections:
        if not isinstance(section.get("units"), list):
            raise StepikError(f"API не вернул список уроков секции {section.get('id')}")
        unit_ids.extend(section["units"])
    units = client.by_ids("units", unit_ids, key="units")
    if set(unit_ids) - {u.get("id") for u in units}:
        raise StepikError(f"Не все units курса {course_id} доступны; принадлежность урока не проверена")
    return [u for u in units if str(u.get("lesson")) == str(lesson_id)]


def lesson_statistics(client, lesson_id, course_id=None):
    """Lesson-level aggregates; a course only validates the lesson's placement."""
    payload = client.get("lessons", ids=[lesson_id])
    lessons = payload.get("lessons", []) or []
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден или недоступен", status=404)
    lesson = lessons[0]
    units = _course_units_for_lesson(client, course_id, lesson_id) if course_id is not None else []
    if course_id is not None and not units:
        raise StepikError(f"Урок {lesson_id} не найден в структуре курса {course_id}", status=404)

    metrics = {}
    unavailable = {}
    for field in ("viewed_by", "passed_by", "time_to_complete"):
        value = lesson.get(field)
        metrics[field] = None
        if field not in lesson:
            unavailable[field] = "поле отсутствует в ответе API"
        elif value is None:
            unavailable[field] = "API вернул null"
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            unavailable[field] = f"неожиданный тип API: {type(value).__name__}"
        elif value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            unavailable[field] = "API вернул некорректное числовое значение"
        elif field != "time_to_complete" and isinstance(value, float) and not value.is_integer():
            unavailable[field] = "API вернул нецелое количество"
        else:
            # Official Stepik clients interpret time_to_complete as seconds.
            metrics[field] = value if field == "time_to_complete" else int(value)
    return {
        "lesson": lesson,
        "course_id": course_id,
        "units": units,
        "metrics": metrics,
        "unavailable": unavailable,
    }


def render_lesson_stats(client, lesson_id, course_id=None):
    data = lesson_statistics(client, lesson_id, course_id=course_id)
    lesson = data["lesson"]
    lines = [f"# Статистика урока: {lesson.get('title', lesson_id)}", ""]
    lines += [f"- **lesson id:** {lesson_id}"]
    if course_id is not None:
        lines.append(f"- **course id:** {course_id}")
        unit_ids = ", ".join(f"`{unit['id']}`" for unit in data["units"])
        lines.append(f"- **Принадлежность курсу подтверждена:** unit {unit_ids}")
    lines += ["", f"**Источник:** GET `/api/lessons?ids[]={lesson_id}`.",
              "**Область данных:** агрегаты самого урока (lesson). "
              "Разбиение этих показателей по курсам не получено.", ""]
    for field, label in (
        ("viewed_by", "Просмотры урока по данным Stepik"),
        ("passed_by", "Студенты, полностью прошедшие урок"),
        ("time_to_complete", "Среднее время прохождения по данным Stepik"),
    ):
        value = data["metrics"][field]
        if value is None:
            formatted = f"недоступно ({data['unavailable'][field]})"
        elif field == "time_to_complete":
            formatted = f"{value} с ({value / 60:.2f} мин)"
        else:
            formatted = str(value)
        lines.append(f"- **{label}:** {formatted} · `lesson.{field}`")
    lines += ["", "**Ограничения интерпретации:**",
              "- Уникальность просмотров `viewed_by` не подтверждена документацией API. "
              "Долю завершивших не рассчитываем: число уникальных начавших неизвестно.",
              "- `time_to_complete` приходит в секундах; минуты вычислены делением на 60. "
              "Stepik называет показатель средним временем, но формула и состав выборки не подтверждены."]
    return "\n".join(lines)


def _rating_metrics(client, step_id, step_payload):
    metrics = _metric_scan(step_payload, ("rating", "rate", "reaction", "vote", "smile", "like", "dislike"))
    if metrics:
        return metrics, None
    # Endpoint names around reactions have changed over Stepik's lifetime.
    # Probe read-only candidates and keep the first one that exists and returns data.
    for endpoint in ("step-ratings", "step-reactions", "step-votes"):
        try:
            payload = client.get(endpoint, step=step_id)
        except StepikError:
            continue
        key = endpoint
        rows = payload.get(key, []) or []
        if rows:
            return _metric_scan(payload, ("rating", "rate", "reaction", "vote", "value", "score")), endpoint
    return [], None


def step_statistics(client, step_id, max_submissions=2000):
    step_payload = client.get("steps", ids=[step_id])
    steps = step_payload.get("steps", []) or []
    if not steps:
        raise StepikError(f"Шаг {step_id} не найден или недоступен", status=404)
    step = steps[0]
    submissions, truncated = step_submissions(client, step_id, max_items=max_submissions)
    counts = _status_counts(submissions)
    correct = counts.get("correct", 0)
    wrong = counts.get("wrong", 0)
    settled = correct + wrong
    calculated_ratio = (correct / settled) if settled else None
    official_ratio = _first_value(step, "correct_ratio", "success_rate", "correct_rate")
    passed_by = _first_value(step, "passed_by", "solved_by", "correct_by")
    official_attempts = _first_value(step, "submissions_count", "attempts_count", "submission_count", "attempt_count")
    if not isinstance(official_attempts, (int, float)):
        official_attempts = None
    rating_metrics, rating_endpoint = _rating_metrics(client, step_id, step_payload)
    return {
        "step": step,
        "submissions": submissions,
        "truncated": truncated,
        "counts": counts,
        "attempts_loaded": len(submissions),
        "official_attempts": official_attempts,
        "official_ratio": official_ratio,
        "calculated_ratio": calculated_ratio,
        "passed_by": passed_by,
        "rating_metrics": rating_metrics,
        "rating_endpoint": rating_endpoint,
        "raw_metrics": _metric_scan(step_payload, ("correct_ratio", "passed_by", "submission", "attempt", "view", "time")),
    }


def render_step_stats(client, step_id, max_submissions=2000):
    data = step_statistics(client, step_id, max_submissions=max_submissions)
    counts = data["counts"]
    ratio = data["official_ratio"] if data["official_ratio"] is not None else data["calculated_ratio"]
    lines = [f"# Статистика шага {step_id}", ""]
    attempts = data["official_attempts"] if data["official_attempts"] is not None else data["attempts_loaded"]
    attempts_note = ""
    if data["official_attempts"] is None and data["truncated"]:
        attempts_note = " (минимум; выборка обрезана)"
    lines += [
        f"- **Попытки/отправленные решения:** {_fmt_number(attempts)}{attempts_note}",
        f"- **Загружено решений для локального анализа:** {data['attempts_loaded']}",
        f"- **Успешность:** {_fmt_number(ratio)}",
        f"- **Решили шаг:** {_fmt_number(data['passed_by'])}",
        f"- **Статусы решений:** {json.dumps(counts, ensure_ascii=False)}",
    ]
    if data["truncated"]:
        lines += ["", "_Важно: когда решений больше лимита, количество попыток выше показанного. Для точного процента предпочтительно поле `correct_ratio`, если Stepik его вернул._"]
    lines += ["", "## Оценки / реакции шага", ""]
    if data["rating_metrics"]:
        for path, value in data["rating_metrics"][:30]:
            lines.append(f"- `{path}` = `{value}`")
        if data["rating_endpoint"]:
            lines.append(f"- источник реакций: `{data['rating_endpoint']}`")
    else:
        lines.append("Stepik не вернул распределение оценок через доступные GET-поля/API для этого шага.")
    if data["raw_metrics"]:
        lines += ["", "## Дополнительные метрики API", ""]
        for path, value in data["raw_metrics"][:25]:
            lines.append(f"- `{path}` = `{value}`")
    return "\n".join(lines)


def render_wrong_submissions(client, step_id, limit=10, scan_limit=1000):
    limit = max(1, min(int(limit), 50))
    submissions, truncated = step_submissions(client, step_id, max_items=max(scan_limit, limit))
    wrong = [s for s in submissions if str(s.get("status") or "").lower() == "wrong"]
    # Prefer recent-looking rows when timestamps are available.
    wrong.sort(key=lambda s: str(_first_value(s, "time", "submitted_at", "create_date", "date") or ""), reverse=True)
    selected = wrong[:limit]
    lines = [f"# Выборка неверных решений шага {step_id}", "",
             "_Пользователи намеренно обезличены: имена, email и user_id не выводятся._", ""]
    if not selected:
        lines.append("Неверных решений в загруженной выборке не найдено.")
        return "\n".join(lines)
    for idx, s in enumerate(selected, 1):
        body, language = _submission_text(s)
        date = _first_value(s, "time", "submitted_at", "create_date", "date") or ""
        lines += [f"## Неверное решение {idx}" + (f" · {date}" if date else ""), ""]
        if language:
            lines.append(f"Язык: `{language}`")
            lines.append("")
        fence = str(language or "text").replace("`", "")
        lines += [f"```{fence}", body or "(пустой ответ)", "```", ""]
        hint = _first_value(s, "hint", "feedback", "reply_feedback")
        if hint:
            lines += ["**Обратная связь Stepik:**", "", clean(str(hint), raw=False), ""]
    if truncated:
        lines.append(f"_Для поиска выборки просмотрены первые {max(scan_limit, limit)} решений; список на Stepik больше._")
    return "\n".join(lines)


def course_reviews(client, course_id, max_items=200):
    return _collect_limited(
        client, "course-reviews", key="course-reviews", max_items=max_items, course=course_id
    )


def render_course_reviews(client, course_id, max_items=100):
    reviews, truncated = course_reviews(client, course_id, max_items=max_items)
    users = _load_users(client, reviews)
    lines = [f"# Отзывы о курсе {course_id}", ""]
    # Try an optional read-only summary endpoint, but never fail the whole tool on it.
    try:
        summary_payload = client.get("course-review-summaries", course=course_id)
        summaries = summary_payload.get("course-review-summaries", []) or []
    except StepikError:
        summaries = []
    if summaries:
        lines += ["## Сводка", "", "```json", json.dumps(summaries[:5], ensure_ascii=False, indent=2), "```", ""]
    if not reviews:
        lines.append("Отзывы не найдены или недоступны этому аккаунту.")
        return "\n".join(lines)
    lines += ["## Отзывы", ""]
    for r in reviews:
        author = _display_user(r, users)
        date = _first_value(r, "time", "create_date", "created_at", "date") or ""
        score = _first_value(r, "score", "rating", "rate")
        text = clean(_first_value(r, "text", "body", "review") or "", raw=False)
        head = author
        if score is not None:
            head += f" · оценка {score}"
        if date:
            head += f" · {date}"
        lines += [f"### {head}", "", text or "_(без текста)_", ""]
    if truncated:
        lines.append(f"_Показаны первые {max_items} отзывов; список обрезан._")
    return "\n".join(lines)


def render_lesson_bundle(client, lesson_id, answers=True, comments=True, stats=True,
                         wrong_sample=5, max_submissions=1000, course_id=None):
    """One AI-editor payload: lesson + comments + lesson stats + task stats/wrong samples."""
    parts = [render_lesson(client, lesson_id, raw=False, answers=answers)]
    if comments:
        parts += ["\n---\n", render_lesson_comments(client, lesson_id)]
    if stats:
        parts += ["\n---\n", render_lesson_stats(client, lesson_id, course_id=course_id)]
        lessons = client.get("lessons", ids=[lesson_id]).get("lessons", [])
        step_ids = (lessons[0].get("steps", []) if lessons else []) or []
        sources = client.by_ids("step-sources", step_ids, key="step-sources")
        by_id = {s.get("id"): s for s in sources}
        for pos, step_id in enumerate(step_ids, 1):
            kind = ((by_id.get(step_id, {}).get("block") or {}).get("name") or "")
            if kind in ("text", "video"):
                continue
            parts += ["\n---\n", f"# Шаг {pos}: аналитика ({kind})\n", render_step_stats(client, step_id, max_submissions=max_submissions)]
            if wrong_sample and kind == "code":
                parts += ["\n", render_wrong_submissions(client, step_id, limit=wrong_sample, scan_limit=max_submissions)]
    return "\n".join(parts)


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
