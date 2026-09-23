#!/usr/bin/env python3
"""
stepik_mcp.py — локальный MCP-сервер для чтения курсов Stepik.

Кладётся рядом с stepik_bridge.py и переиспользует его клиент и рендер.
Транспорт — stdio, то есть работает только как дочерний процесс Claude Desktop.

Только чтение: ни один инструмент не делает пишущих запросов к Stepik.
"""

import os

from mcp.server.mcpserver import MCPServer

import stepik_bridge as sb

mcp = MCPServer(
    name="stepik",
    instructions=(
        "Чтение курсов, уроков и шагов Stepik от имени владельца OAuth-приложения. "
        "Начинай с stepik_course_outline, чтобы получить id уроков, затем "
        "запрашивай stepik_lesson по нужному id."
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
    except sb.StepikError as exc:
        return f"Ошибка Stepik: {exc}"


@mcp.tool(description="Проверить связь с Stepik и показать, под каким аккаунтом работает токен.")
def stepik_whoami() -> str:
    def work():
        payload = sb.CLIENT.get("stepics/1")
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
            payload = sb.CLIENT.get("stepics/1")
            uid = (payload.get("users") or [{}])[0].get("id")
        courses = sb.CLIENT.collect("courses", teacher=uid)
        if not courses:
            return f"У пользователя {uid} нет курсов с правами преподавателя."
        lines = [f"Курсы аккаунта {uid}:", ""]
        for course in courses:
            lines.append(f"- **{course.get('title')}** — `course/{course.get('id')}`")
        return "\n".join(lines)
    return _run(work)


@mcp.tool(description="Структура курса: секции, уроки и id каждого урока. Отсюда берутся id для stepik_lesson.")
def stepik_course_outline(course_id: int) -> str:
    return _run(sb.render_course, sb.CLIENT, course_id)


@mcp.tool(description="Полный текст урока со всеми шагами в markdown. answers=False скрывает правильные ответы.")
def stepik_lesson(lesson_id: int, answers: bool = True) -> str:
    return _run(sb.render_lesson, sb.CLIENT, lesson_id, False, answers)


@mcp.tool(description="Один шаг по его внутреннему id (не по номеру в уроке).")
def stepik_step(step_id: int, answers: bool = True) -> str:
    def work():
        sources = sb.CLIENT.by_ids("step-sources", [step_id], key="step-sources")
        if not sources:
            return f"Шаг {step_id} не найден или недоступен."
        return sb.render_step(sources[0], raw=False, answers=answers)
    return _run(work)


@mcp.tool(description="Принимает ссылку, скопированную из адресной строки Stepik: на курс, урок или конкретный шаг.")
def stepik_resolve(url: str, answers: bool = True) -> str:
    return _run(sb.resolve_url, sb.CLIENT, url, False, answers)


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


def _main():
    import sys

    if "--http" in sys.argv:
        import uvicorn

        port = int(os.environ.get("PORT", "8000"))
        uvicorn.run(_http_app(), host="127.0.0.1", port=port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    _main()
