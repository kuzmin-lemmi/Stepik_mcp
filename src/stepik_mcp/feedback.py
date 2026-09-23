"""Обратная связь учеников: комментарии, решения и отзывы. Только чтение."""

import json

from .client import StepikError, collect_limited, first_value
from .render import clean


def _user_id(item):
    return first_value(item, "user", "user_id", "userId", "author", "author_id")


def load_users(client, items):
    ids = []
    for item in items:
        uid = _user_id(item)
        if isinstance(uid, int) or (isinstance(uid, str) and uid.isdigit()):
            ids.append(int(uid))
    users = client.by_ids("users", sorted(set(ids)), key="users") if ids else []
    return {u.get("id"): u for u in users}


def display_user(item, users):
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


def comments_for_steps(client, step_ids):
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
    return groups, load_users(client, comments)


def step_comments(client, step_id):
    """All default-thread comments attached to a step, including replies. GET only."""
    groups, users = comments_for_steps(client, [step_id])
    return groups[step_id], users


def render_comments(comments, users):
    lines = []
    for c in comments:
        author = display_user(c, users)
        date = first_value(c, "time", "create_date", "created_at", "date") or ""
        text = clean(first_value(c, "text", "body", "message") or "", raw=False)
        parent = first_value(c, "parent", "parent_id")
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
    lines += render_comments(comments, users)
    if not comments:
        lines.append("Комментариев в основном обсуждении шага нет.")
    return "\n".join(lines)


def render_lesson_comments(client, lesson_id):
    lessons = client.get("lessons", ids=[lesson_id]).get("lessons", [])
    if not lessons:
        raise StepikError(f"Урок {lesson_id} не найден или недоступен", status=404)
    step_ids = lessons[0].get("steps", []) or []
    lines = [f"# Комментарии к уроку {lesson_id}", ""]
    groups, users = comments_for_steps(client, step_ids)
    total = 0
    for pos, step_id in enumerate(step_ids, 1):
        comments = groups[step_id]
        if not comments:
            continue
        total += len(comments)
        lines += [f"## Шаг {pos} · step {step_id}", ""]
        lines += render_comments(comments, users)
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
    language = first_value(submission, "language", "lang")
    if isinstance(value, dict):
        language = first_value(value, "language", "lang") or language
        body = first_value(value, "code", "text", "answer", "reply")
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
    return collect_limited(
        client, "submissions", key="submissions", max_items=max_items, step=step_id,
        max_pages=max(1, min(200, (max_items + 19) // 20 + 2)),
    )


def render_wrong_submissions(client, step_id, limit=10, scan_limit=1000):
    limit = max(1, min(int(limit), 50))
    submissions, truncated = step_submissions(client, step_id, max_items=max(scan_limit, limit))
    wrong = [s for s in submissions if str(s.get("status") or "").lower() == "wrong"]
    # Prefer recent-looking rows when timestamps are available.
    wrong.sort(key=lambda s: str(first_value(s, "time", "submitted_at", "create_date", "date") or ""), reverse=True)
    selected = wrong[:limit]
    lines = [f"# Выборка неверных решений шага {step_id}", "",
             "_Пользователи намеренно обезличены: имена, email и user_id не выводятся._", ""]
    if not selected:
        lines.append("Неверных решений в загруженной выборке не найдено.")
        return "\n".join(lines)
    for idx, s in enumerate(selected, 1):
        body, language = _submission_text(s)
        date = first_value(s, "time", "submitted_at", "create_date", "date") or ""
        lines += [f"## Неверное решение {idx}" + (f" · {date}" if date else ""), ""]
        if language:
            lines.append(f"Язык: `{language}`")
            lines.append("")
        fence = str(language or "text").replace("`", "")
        lines += [f"```{fence}", body or "(пустой ответ)", "```", ""]
        hint = first_value(s, "hint", "feedback", "reply_feedback")
        if hint:
            lines += ["**Обратная связь Stepik:**", "", clean(str(hint), raw=False), ""]
    if truncated:
        lines.append(f"_Для поиска выборки просмотрены первые {max(scan_limit, limit)} решений; список на Stepik больше._")
    return "\n".join(lines)


def course_reviews(client, course_id, max_items=200):
    return collect_limited(
        client, "course-reviews", key="course-reviews", max_items=max_items, course=course_id
    )


def render_course_reviews(client, course_id, max_items=100):
    reviews, truncated = course_reviews(client, course_id, max_items=max_items)
    users = load_users(client, reviews)
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
        author = display_user(r, users)
        date = first_value(r, "time", "create_date", "created_at", "date") or ""
        score = first_value(r, "score", "rating", "rate")
        text = clean(first_value(r, "text", "body", "review") or "", raw=False)
        head = author
        if score is not None:
            head += f" · оценка {score}"
        if date:
            head += f" · {date}"
        lines += [f"### {head}", "", text or "_(без текста)_", ""]
    if truncated:
        lines.append(f"_Показаны первые {max_items} отзывов; список обрезан._")
    return "\n".join(lines)
