"""MCP-сервер Stepik для AI Course Workspace.

По умолчанию транспорт stdio (Claude Code, Claude Desktop, OpenCode);
флаг --http включает Streamable HTTP для удалённых клиентов.

Инструменты stepik_cache_* пишут только в локальный workspace.
На Stepik пишут только stepik_plan_push/stepik_apply_push (модуль publish)
и только в курсы из ALLOWED_WRITE_COURSES («*» — любой свой курс).
"""

import os
import json

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import moderation, publish, refresh, roundtrip
from . import workspace as sw
from .analytics import render_lesson_bundle, render_lesson_stats, render_step_stats
from .client import StepikClient, StepikError
from .feedback import (
    render_course_reviews,
    render_lesson_comments,
    render_step_comments,
    render_wrong_submissions,
)
from .render import render_course, render_lesson, render_step, resolve_url

CLIENT = StepikClient(
    os.environ.get("STEPIK_CLIENT_ID", ""),
    os.environ.get("STEPIK_CLIENT_SECRET", ""),
)

mcp = MCPServer(
    name="stepik",
    instructions=(
        "Запись на Stepik только через stepik_plan_push -> показ diff автору -> явное подтверждение -> stepik_apply_push. "
        "stepik_cache_lesson записывает рабочий Markdown локально в C:\\Courses. "
        "Для получения урока по номеру используй stepik_cache_lesson: ответ содержит путь, а не полный текст. "
        "Затем читай и редактируй локальный файл только в нужном объёме. Не дублируй весь урок в чате. "
        "Для структуры курса используй stepik_course_outline; stepik_lesson нужен для явного вывода текста в чат. "
        "Комментарии, статистику, неверные решения и отзывы получай только по явному запросу пользователя, отдельно от текста. "
        "Для комментариев всего урока вызывай stepik_lesson_comments один раз, не по каждому шагу. "
        "Не вызывай stepik_lesson_bundle по умолчанию; только если пользователь явно запросил полный пакет с аналитикой."
    ),
)


def _guard():
    """Вместо стектрейса возвращаем человеку понятную причину."""
    missing = [
        name for name in ("STEPIK_CLIENT_ID", "STEPIK_CLIENT_SECRET")
        if not os.environ.get(name)
    ]
    if missing:
        return "Не заданы переменные окружения: " + ", ".join(missing)
    return None


def _run(func, *args, **kwargs):
    problem = _guard()
    if problem:
        return problem
    try:
        return func(*args, **kwargs)
    except StepikError as exc:
        return f"Ошибка Stepik: {exc}"
    except Exception as exc:
        return f"Ошибка MCP ({type(exc).__name__}): {exc}"


@mcp.tool(description="Проверить связь с Stepik и показать, под каким аккаунтом работает токен.")
def stepik_whoami() -> str:
    def work():
        payload = CLIENT.get("stepics/1")
        user = (payload.get("users") or [{}])[0]
        return (
            f"Аккаунт: {user.get('full_name')} (id {user.get('id')})\n"
            f"Авторизация работает."
        )
    return _run(work)


@mcp.tool(description="Список курсов, где указанный аккаунт значится преподавателем. По умолчанию — текущий аккаунт.")
def stepik_my_courses(user_id: int | None = None) -> str:
    def work():
        uid = user_id
        if uid is None:
            payload = CLIENT.get("stepics/1")
            uid = (payload.get("users") or [{}])[0].get("id")
        courses = CLIENT.collect("courses", teacher=uid)
        if not courses:
            return f"У пользователя {uid} нет курсов с правами преподавателя."
        lines = [f"Курсы аккаунта {uid}:", ""]
        for course in courses:
            lines.append(f"- **{course.get('title')}** — `course/{course.get('id')}`")
        return "\n".join(lines)
    return _run(work)


@mcp.tool(description="Структура курса: секции, уроки и id каждого урока. Отсюда берутся id для stepik_lesson.")
def stepik_course_outline(course_id: int) -> str:
    return _run(render_course, CLIENT, course_id)


@mcp.tool(description="Полный текст урока со всеми шагами в markdown. answers=False скрывает правильные ответы.")
def stepik_lesson(lesson_id: int, answers: bool = True) -> str:
    return _run(render_lesson, CLIENT, lesson_id, False, answers)


@mcp.tool(description="Сохраняет один опубликованный урок в локальный Markdown в C:\\Courses, создавая папки всех модулей курса. lesson_position: номер вида 6.2. Возвращает только путь и краткие сведения, без текста и аналитики. Существующий файл не перезаписывается и не скачивается повторно. Только GET к Stepik, запись только в локальное workspace.")
def stepik_cache_lesson(course_id: int, lesson_position: str) -> str:
    # Cached files remain usable even without OAuth variables in this process.
    try:
        return json.dumps(sw.cache_lesson(CLIENT, course_id, lesson_position), ensure_ascii=False, indent=2)
    except StepikError as exc:
        return f"Ошибка Stepik Workspace: {exc}"
    except Exception as exc:
        return f"Ошибка Workspace ({type(exc).__name__}): {exc}"


@mcp.tool(description="Сохраняет комментарии и ответы к обычным обсуждениям одного уже кэшированного урока в отдельный .comments.md рядом с уроком. lesson_position: номер вида 6.1. Возвращает только путь и краткие сведения. Существующий файл комментариев не перезаписывается; вкладка решений, статистика и текст урока не загружаются.")
def stepik_cache_lesson_comments(course_id: int, lesson_position: str) -> str:
    try:
        return json.dumps(sw.cache_lesson_comments(CLIENT, course_id, lesson_position), ensure_ascii=False, indent=2)
    except StepikError as exc:
        return f"Ошибка Stepik Workspace: {exc}"
    except Exception as exc:
        return f"Ошибка Workspace ({type(exc).__name__}): {exc}"


@mcp.tool(description="Сохраняет все уроки курса в C:\\Courses, но в каждый Markdown помещает только шаги типа text. Задания, code, тесты, видео, комментарии и аналитика не сохраняются. Существующие локальные файлы не перезаписываются. Возвращает краткую сводку и пути, без текста уроков.")
def stepik_cache_course_text_lessons(course_id: int) -> str:
    try:
        return json.dumps(sw.cache_course_text_lessons(CLIENT, course_id), ensure_ascii=False, indent=2)
    except StepikError as exc:
        return f"Ошибка Stepik Workspace: {exc}"
    except Exception as exc:
        return f"Ошибка Workspace ({type(exc).__name__}): {exc}"


@mcp.tool(
    description="Скачивает весь курс в локальную папку: все уроки со всеми типами шагов (условие в Markdown, "
    "настройки заданий в YAML-блоке) и описание курса в COURSE_INFO.md. Структура курса обновляется: новые уроки "
    "добавляются, у известных остаются прежние пути. Существующие файлы уроков не перезаписываются — для них "
    "stepik_refresh_lesson. Только GET к Stepik. Возвращает сводку и пути, без текста уроков.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_pull_course(course_id: int) -> str:
    return _run(lambda: json.dumps(sw.pull_course(CLIENT, course_id), ensure_ascii=False, indent=2))


@mcp.tool(
    description="Обновляет локальный файл урока со Stepik, не затирая правки автора. Шаг, изменённый только на "
    "Stepik, заменяется новой версией; изменённый только в файле — остаётся; изменённый и там, и там — конфликт: "
    "файл в этом шаге не меняется, версия Stepik сохраняется рядом в <урок>.stepik-<время>.md. Новые шаги Stepik "
    "вставляются на свои места. Без apply=True только показывает изменения. С apply=True прежний файл сохраняется "
    "в snapshots/. resolved — step_id конфликтов, которые автор уже перенёс вручную: их базой станет версия Stepik. "
    "Только GET к Stepik; пишет только в локальную папку курса.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
)
def stepik_refresh_lesson(course_id: int, lesson_position: str, apply: bool = False,
                          resolved: list[int] | None = None) -> str:
    return _run(lambda: json.dumps(refresh.refresh_lesson(CLIENT, course_id, lesson_position, apply, resolved),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="План публикации урока из локального файла на Stepik. Ничего не отправляет. "
    "Сравнивает шаги файла (метки <!-- step_id: N -->) с исходниками на момент скачивания и с уроком на Stepik. "
    "Публикует текстовые шаги и задания choice, string, number, matching, sorting, free-answer, code: настройки "
    "задания правятся в блоке ```yaml stepik в конце шага. Новый шаг — строка <!-- new_step -->, для задания с "
    "YAML-блоком. Удалить шаг — заменить его метку на <!-- delete_step: N -->. Задания проверяются до отправки; у задачи на код ключ solution: путь/к/решению.py запускает "
    "решение локально на всех тестах. Картинки — локальные файлы: ![подпись](images/схема.png). "
    "Возвращает plan_id, изменения, путь к diff, confirm_steps — шаги, которые уже решали ученики, и "
    "confirm_delete — удаляемые шаги. "
    "Покажите diff автору до stepik_apply_push. Работает только для курсов из ALLOWED_WRITE_COURSES "
    "(«*» — любой курс, где аккаунт автор или преподаватель).",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_plan_push(course_id: int, lesson_position: str) -> str:
    return _run(lambda: json.dumps(publish.plan_push(CLIENT, course_id, lesson_position), ensure_ascii=False, indent=2))


@mcp.tool(
    description="План отката урока к снимку из snapshots/ (снимок сохраняется перед каждой публикацией). "
    "Изменённые шаги восстанавливаются, удалённые создаются заново с новым id (без комментариев и решений). "
    "Ничего не отправляет. snapshot — имя файла снимка или latest (снимок перед последней публикацией, в том числе перед предыдущим откатом). Шаги, созданные после снимка, не удаляются; "
    "локальный файл урока не меняется. Применяется тем же stepik_apply_push после подтверждения автора.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_rollback_lesson(course_id: int, lesson_position: str, snapshot: str = "latest") -> str:
    return _run(lambda: json.dumps(publish.plan_rollback(CLIENT, course_id, lesson_position, snapshot),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="План создания нового урока на Stepik из локального черновика. Ничего не отправляет. "
    "draft_path — путь к .md от папки курса (например «02-Модуль-123/новый-урок.md»); название — title: во "
    "frontmatter или строка «# Название»; шаги — метки <!-- new_step -->. module_position — номер модуля, "
    "lesson_position — место урока в модуле (по умолчанию в конец). Применяется stepik_apply_push после "
    "подтверждения автора: создаётся пустой урок, привязывается к модулю, ID записываются в черновик и "
    ".course.json. Шаги затем публикуются через stepik_plan_push. Только для курсов из ALLOWED_WRITE_COURSES.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_plan_create_lesson(course_id: int, draft_path: str, module_position: int,
                              lesson_position: int | None = None) -> str:
    return _run(lambda: json.dumps(publish.plan_create_lesson(CLIENT, course_id, draft_path, module_position,
                                                              lesson_position), ensure_ascii=False, indent=2))


@mcp.tool(
    description="Публикует на Stepik план из stepik_plan_push, stepik_rollback_lesson или "
    "stepik_plan_create_lesson: загружает картинки, меняет, добавляет или восстанавливает шаги, создаёт урок. Вызывать только после того, как автор увидел diff и явно "
    "подтвердил публикацию. confirm_steps — step_id из плана, которые уже решали ученики; передавайте их, только "
    "если автор согласился изменить именно эти шаги. confirm_delete — ровно те step_id, которые план удаляет; "
    "передавайте их, только если автор подтвердил удаление. Перед записью проверяет, что файл и урок на Stepik не "
    "менялись, и сохраняет снимок урока в snapshots/.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                open_world_hint=True),
)
def stepik_apply_push(course_id: int, plan_id: str, confirm_steps: list[int] | None = None,
                      confirm_delete: list[int] | None = None) -> str:
    return _run(lambda: json.dumps(publish.apply_push(CLIENT, course_id, plan_id, confirm_steps, confirm_delete),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="План удаления комментариев под шагами курса. Ничего не удаляет. Фильтр обязателен (кроме all_in_lesson): text — весь "
    "текст комментария без HTML (например «!»), или contains — часть текста. По умолчанию только комментарии "
    "аккаунта токена (user_id — другой пользователь) и только без ответов (include_replied=True — с ответами). "
    "lesson_position (вид 6.2) ограничивает урок. all_in_lesson=True с lesson_position — все комментарии урока: "
    "любых авторов, включая учеников, с ответами и без фильтра по тексту. Возвращает plan_id, число комментариев "
    "(confirm_count), разбивку по урокам и авторам, примеры и путь к полному списку. Покажите автору до "
    "stepik_apply_delete_comments.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_plan_delete_comments(course_id: int, text: str | None = None, contains: str | None = None,
                                lesson_position: str | None = None, user_id: int | None = None,
                                include_replied: bool = False, all_in_lesson: bool = False) -> str:
    return _run(lambda: json.dumps(moderation.plan_delete_comments(
        CLIENT, course_id, text, contains, lesson_position, user_id, include_replied, all_in_lesson),
        ensure_ascii=False, indent=2))


@mcp.tool(
    description="Удаляет на Stepik комментарии из плана stepik_plan_delete_comments. confirm_count — число "
    "комментариев из плана; передавайте его, только если автор подтвердил удаление. Комментарии, которые "
    "изменились или получили ответы после плана, пропускаются. Копии удалённых сохраняются в snapshots/. "
    "Удаление на Stepik не отменить.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                open_world_hint=True),
)
def stepik_apply_delete_comments(course_id: int, plan_id: str, confirm_count: int) -> str:
    return _run(lambda: json.dumps(moderation.apply_delete_comments(CLIENT, course_id, plan_id, confirm_count),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="Проверка «скачать → собрать обратно» для всех шагов курса: каждый шаг переводится в Markdown с "
    "YAML-блоком, разбирается обратно и сравнивается с оригиналом Stepik до последнего поля. Только чтение. "
    "Возвращает сводку по типам шагов и до 20 расхождений.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_check_roundtrip(course_id: int) -> str:
    return _run(lambda: json.dumps(roundtrip.check_course(CLIENT, course_id), ensure_ascii=False, indent=2))


@mcp.tool(description="Один шаг по его внутреннему id (не по номеру в уроке).")
def stepik_step(step_id: int, answers: bool = True) -> str:
    def work():
        sources = CLIENT.by_ids("step-sources", [step_id], key="step-sources")
        if not sources:
            return f"Шаг {step_id} не найден или недоступен."
        return render_step(sources[0], raw=False, answers=answers)
    return _run(work)


@mcp.tool(description="Принимает ссылку, скопированную из адресной строки Stepik: на курс, урок или конкретный шаг.")
def stepik_resolve(url: str, answers: bool = True) -> str:
    return _run(resolve_url, CLIENT, url, False, answers)


@mcp.tool(description="Все обычные комментарии и ответы к одному шагу с именами авторов, без вкладки решений. Для всего урока используй stepik_lesson_comments. Только чтение.")
def stepik_step_comments(step_id: int) -> str:
    return _run(render_step_comments, CLIENT, step_id)


@mcp.tool(description="Все обычные комментарии и ответы по шагам урока, без вкладки решений. Пакетная загрузка одним вызовом, группировка по номеру шага, без лимита количества.")
def stepik_lesson_comments(lesson_id: int) -> str:
    return _run(render_lesson_comments, CLIENT, lesson_id)


@mcp.tool(description="Агрегаты самого урока: просмотры, число полностью прошедших, среднее время (API: секунды, также выводятся минуты). Указывает источники и недоступные поля. course_id проверяет принадлежность урока курсу, но не фильтрует статистику по его ученикам.")
def stepik_lesson_stats(lesson_id: int, course_id: int | None = None) -> str:
    return _run(render_lesson_stats, CLIENT, lesson_id, course_id)


@mcp.tool(description="Статистика задания/шага: попытки, успешность, число решивших, оценки/реакции если API их возвращает.")
def stepik_step_stats(step_id: int, max_submissions: int = 2000) -> str:
    max_submissions = max(20, min(max_submissions, 10000))
    return _run(render_step_stats, CLIENT, step_id, max_submissions)


@mcp.tool(description="Обезличенная выборка неверных решений учащихся для анализа типичных ошибок. Имена/email/user_id не выводятся.")
def stepik_wrong_submissions(step_id: int, limit: int = 10, scan_limit: int = 1000) -> str:
    limit = max(1, min(limit, 50))
    scan_limit = max(limit, min(scan_limit, 10000))
    return _run(render_wrong_submissions, CLIENT, step_id, limit, scan_limit)


@mcp.tool(description="Отзывы учащихся по всему курсу Stepik; при наличии API также показывает сводку оценок.")
def stepik_course_reviews(course_id: int, limit: int = 100) -> str:
    limit = max(1, min(limit, 500))
    return _run(render_course_reviews, CLIENT, course_id, limit)


@mcp.tool(description="Пакет для AI-редактора: текст урока + программные задачи + комментарии + статистика + выборка неверных code-решений.")
def stepik_lesson_bundle(
    lesson_id: int,
    answers: bool = True,
    comments: bool = True,
    stats: bool = True,
    wrong_sample: int = 3,
    max_submissions: int = 1000,
    course_id: int | None = None,
) -> str:
    wrong_sample = max(0, min(wrong_sample, 20))
    max_submissions = max(20, min(max_submissions, 5000))
    return _run(
        render_lesson_bundle,
        CLIENT,
        lesson_id,
        answers,
        comments,
        stats,
        wrong_sample,
        max_submissions,
        course_id,
    )


# --------------------------------------------------------------------------
# Режимы запуска
# --------------------------------------------------------------------------

def _http_app():
    """Streamable HTTP + проверка Bearer-токена.

    Нужен для клиентов, которые не умеют запускать локальный процесс
    (ChatGPT и прочие) и ходят только по HTTPS.
    """
    import secrets

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from mcp.server.transport_security import TransportSecuritySettings

    bearer = os.environ.get("MCP_BEARER_TOKEN", "")
    if len(bearer) < 24:
        raise SystemExit(
            "Задай MCP_BEARER_TOKEN длиной от 24 символов — сервер будет "
            "доступен из интернета, без него его найдут за часы."
        )

    class RequireBearer(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else ""
            if not secrets.compare_digest(supplied, bearer):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = mcp.streamable_http_app(
        stateless_http=True,
        # За туннелем Host приходит чужой, и защита от rebinding режет запрос.
        # Вместо неё дверь стережёт Bearer-токен выше.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.add_middleware(RequireBearer)
    return app


def main():
    import sys

    if "--http" in sys.argv:
        import uvicorn

        port = int(os.environ.get("PORT", "8000"))
        uvicorn.run(_http_app(), host="127.0.0.1", port=port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
