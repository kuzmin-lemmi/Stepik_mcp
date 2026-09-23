"""MCP-сервер Stepik для AI Course Workspace.

По умолчанию транспорт stdio (Claude Code, Claude Desktop, OpenCode);
флаг --http включает Streamable HTTP для удалённых клиентов.

Только чтение: ни один инструмент не делает пишущих запросов к Stepik.
Инструменты stepik_cache_* пишут только в локальный workspace.
"""

import os
import json

from mcp.server.mcpserver import MCPServer

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
        "Только чтение на Stepik; stepik_cache_lesson записывает рабочий Markdown локально в C:\\Courses. "
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
