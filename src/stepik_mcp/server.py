"""MCP-сервер Stepik для AI Course Workspace.

По умолчанию транспорт stdio (Claude Code, Claude Desktop, OpenCode);
флаг --http включает Streamable HTTP для удалённых клиентов.

Инструменты stepik_cache_* пишут только в локальный workspace.
На Stepik пишут только stepik_plan_push/stepik_apply_push (модуль publish)
и только в курсы из ALLOWED_WRITE_COURSES («*» — любой свой курс).
"""

import json
import os
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import (coursemap, learners, lessoncheck, lessontools, moderation, publish, refresh, roundtrip, staging,
               versions)
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
        "Три режима работы. Если пользователь не сказал, чем заняться, предложи выбор: "
        "1) скачать тексты уроков (stepik_cache_lesson, stepik_pull_course, stepik_cache_course_text_lessons) "
        "без рецензии и правок; 2) править курс вместе: один раз вызови stepik_start_editing и следуй правилам "
        "из его ответа и AGENTS.md папки курса; 3) пройти курс учеником-тестировщиком: stepik_start_editing с "
        "mode='student', уроки по порядку, отчёт о том, что мешает ученику, без правок. "
        "Если ответ называет last_handoff, прочитай передачу и предложи продолжить с того места. "
        "Длинный урок читай по шагам: stepik_lesson_outline даёт строки файла каждого шага. Перед правкой урока "
        "сохраняй версию stepik_save_version. "
        "В режиме правки читай карту курса COURSE_LOGIC.md перед проверкой урока, проверяй предложения по ней "
        "(stepik_check_lesson) и обновляй запись урока после правки. Замечания показывай в чате коротко: номер, "
        "место (шаг, цитата), суть и варианты исправления; полный текст — в отчёте. Урок меняй только после "
        "ответа автора, принятые пункты вноси все сразу. "
        "Запись на Stepik только через stepik_plan_push -> показ diff автору -> явное подтверждение -> stepik_apply_push. "
        "stepik_cache_lesson записывает рабочий Markdown в локальную папку курсов (COURSE_WORKSPACE_ROOT). "
        "Для получения урока по номеру используй stepik_cache_lesson: ответ содержит путь, а не полный текст. "
        "Затем читай и редактируй локальный файл только в нужном объёме. Не дублируй весь урок в чате. "
        "Для структуры курса используй stepik_course_outline; stepik_lesson нужен для явного вывода текста в чат. "
        "Комментарии, статистику, неверные решения и отзывы получай только по явному запросу пользователя, отдельно от текста. "
        "Для комментариев всего урока вызывай stepik_lesson_comments один раз, не по каждому шагу. "
        "Не вызывай stepik_lesson_bundle по умолчанию; только если пользователь явно запросил полный пакет с аналитикой."
    ),
)


# Только GET к Stepik; LOCAL_WRITE ещё создаёт файлы в папке курса, но не перезаписывает их.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                              open_world_hint=True)


HTTP_CODES = {400: "BAD_REQUEST", 401: "AUTH_FAILED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
              429: "RATE_LIMITED"}


def error_json(code, message):
    """Единый формат ошибки инструмента: {status, code, message}."""
    return json.dumps({"status": "error", "code": code, "message": message}, ensure_ascii=False, indent=2)


def _error(exc):
    if isinstance(exc, StepikError):
        # Код в начале сообщения («WRITE_NOT_ALLOWED: ...») главнее HTTP-статуса.
        match = re.match(r"([A-Z][A-Z_]{3,}): *(.*)", str(exc), re.DOTALL)
        if match:
            return error_json(match[1], match[2])
        return error_json(HTTP_CODES.get(exc.status, "STEPIK_ERROR"), str(exc))
    return error_json("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


def _guard():
    """Вместо стектрейса возвращаем человеку понятную причину."""
    missing = [
        name for name in ("STEPIK_CLIENT_ID", "STEPIK_CLIENT_SECRET")
        if not os.environ.get(name)
    ]
    if missing:
        return error_json("CONFIG_MISSING", "Не заданы переменные окружения: " + ", ".join(missing))
    return None


def _run(func, *args, **kwargs):
    problem = _guard()
    if problem:
        return problem
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return _error(exc)


def _local(func, *args):
    """Инструменты локальной папки: работают и без OAuth, если Stepik не нужен."""
    try:
        return json.dumps(func(CLIENT, *args), ensure_ascii=False, indent=2)
    except Exception as exc:
        return _error(exc)


@mcp.tool(annotations=READ_ONLY, description="Проверить связь с Stepik и показать, под каким аккаунтом работает токен.")
def stepik_whoami() -> str:
    def work():
        payload = CLIENT.get("stepics/1")
        user = (payload.get("users") or [{}])[0]
        return (
            f"Аккаунт: {user.get('full_name')} (id {user.get('id')})\n"
            f"Авторизация работает."
        )
    return _run(work)


@mcp.tool(annotations=READ_ONLY, description="Список курсов, где указанный аккаунт значится преподавателем. По умолчанию — текущий аккаунт.")
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


@mcp.tool(annotations=READ_ONLY, description="Структура курса: секции, уроки и id каждого урока. Отсюда берутся id для stepik_lesson.")
def stepik_course_outline(course_id: int) -> str:
    return _run(render_course, CLIENT, course_id)


@mcp.tool(annotations=READ_ONLY, description="Полный текст урока со всеми шагами в markdown. answers=False скрывает правильные ответы.")
def stepik_lesson(lesson_id: int, answers: bool = True) -> str:
    return _run(render_lesson, CLIENT, lesson_id, False, answers)


@mcp.tool(annotations=LOCAL_WRITE, description="Сохраняет один опубликованный урок в локальный Markdown в папке курсов, создавая папки всех модулей курса. lesson_position: номер вида 6.2. Возвращает только путь и краткие сведения, без текста и аналитики. Существующий файл не перезаписывается и не скачивается повторно. Только GET к Stepik, запись только в локальное workspace.")
def stepik_cache_lesson(course_id: int, lesson_position: str) -> str:
    # Cached files remain usable even without OAuth variables in this process.
    return _local(sw.cache_lesson, course_id, lesson_position)


@mcp.tool(annotations=LOCAL_WRITE, description="Сохраняет комментарии и ответы к обычным обсуждениям одного уже кэшированного урока в отдельный .comments.md рядом с уроком. lesson_position: номер вида 6.1. Возвращает только путь и краткие сведения. Существующий файл комментариев не перезаписывается; вкладка решений, статистика и текст урока не загружаются.")
def stepik_cache_lesson_comments(course_id: int, lesson_position: str) -> str:
    return _local(sw.cache_lesson_comments, course_id, lesson_position)


@mcp.tool(annotations=LOCAL_WRITE, description="Сохраняет все уроки курса в папку курсов, но в каждый Markdown помещает только шаги типа text. Задания, code, тесты, видео, комментарии и аналитика не сохраняются. Существующие локальные файлы не перезаписываются. Возвращает краткую сводку и пути, без текста уроков.")
def stepik_cache_course_text_lessons(course_id: int) -> str:
    return _local(sw.cache_course_text_lessons, course_id)


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
    description="Начать совместную работу над курсом. mode='edit' — править курс вместе, mode='student' — пройти "
    "курс учеником-тестировщиком. Готовит папку курса: правила (в том числе rules/STUDENT_TEST_RULES.md), "
    "DECISIONS.md и карту курса COURSE_LOGIC.md (о чём курс, модули, что ученик узнаёт в каждом уроке). Карта "
    "создаётся один раз по структуре курса и дальше не перезаписывается. Возвращает путь к карте, незаполненные и "
    "отсутствующие в ней уроки, последнюю передачу и правила режима, без текста уроков. Только GET к Stepik, и "
    "только если карты или структуры ещё нет.",
    annotations=LOCAL_WRITE,
)
def stepik_start_editing(course_id: int, mode: str = "edit", level: str = "novice") -> str:
    """level для mode='student': novice — полный новичок, experienced — ученик с опытом."""
    return _local(coursemap.start_editing, course_id, mode, level)


# Только локальные файлы: без запросов к Stepik.
LOCAL_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL_ONLY_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=False)


def _offline(func, *args):
    return _local(lambda _client, *rest: func(*rest), *args)


@mcp.tool(
    description="Оглавление локального урока: шаги с типом, заголовком, подзаголовками, числом слов, блоков кода и "
    "картинок и строками файла (lines). Нужен, чтобы читать длинный урок по шагам, а не целиком. Без запросов к Stepik.",
    annotations=LOCAL_READ,
)
def stepik_lesson_outline(course_id: int, lesson_position: str) -> str:
    return _offline(lessontools.lesson_outline, course_id, lesson_position)


@mcp.tool(
    description="Сохранить локальную версию файла урока перед правкой (snapshots/local/<урок>/). Повторный вызов без "
    "изменений новую версию не создаёт. На Stepik ничего не отправляет.",
    annotations=LOCAL_ONLY_WRITE,
)
def stepik_save_version(course_id: int, lesson_position: str, note: str = "") -> str:
    return _offline(versions.save_version, course_id, lesson_position, note)


@mcp.tool(
    description="Список локальных версий файла урока: время, заметка, сколько строк отличается от текущего файла.",
    annotations=LOCAL_READ,
)
def stepik_list_versions(course_id: int, lesson_position: str) -> str:
    return _offline(versions.list_versions, course_id, lesson_position)


@mcp.tool(
    description="Вернуть локальную версию урока целиком или только один шаг (step_id). Без apply=True только "
    "показывает diff. С apply=True текущий файл сначала сохраняется новой версией, затем заменяется. "
    "Применять только после явного согласия автора. На Stepik ничего не отправляет.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                open_world_hint=False),
)
def stepik_restore_version(course_id: int, lesson_position: str, version: str, step_id: int | None = None,
                           apply: bool = False) -> str:
    return _offline(versions.restore_version, course_id, lesson_position, version, step_id, apply)


@mcp.tool(
    description="Запустить примеры ```python из локального урока и сравнить вывод с блоком ```text после примера. "
    "Код автора курса выполняется на этом компьютере: отдельный процесс, временная папка, без секретов сервера, "
    "пустой ввод, до 5 секунд. step — номер шага, чтобы проверить только его. Вызывать по запросу автора или в "
    "режимах правки и проверки учеником. Возвращает только проблемные примеры и сводку.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=False),
)
def stepik_run_examples(course_id: int, lesson_position: str, step: int | None = None) -> str:
    return _offline(lessontools.run_examples, course_id, lesson_position, step)


@mcp.tool(
    description="Проверка всех скачанных уроков курса тем же способом, что stepik_check_lesson: сводка по курсу "
    "(где больше забегания вперёд, отсылок, «очевидно», длинных шагов, заданий без теории, какие уроки длиннее "
    "остальных) и таблица в новом файле sessions/ГГГГ-ММ-ДД-course-check.md. Без запросов к Stepik.",
    annotations=LOCAL_ONLY_WRITE,
)
def stepik_check_course(course_id: int) -> str:
    return _offline(lessoncheck.check_course, course_id)


@mcp.tool(
    description="Словарь терминов курса из карты COURSE_LOGIC.md (какой термин в каком уроке введён, что введено "
    "дважды) в генерируемый GLOSSARY.md и поиск нежелательных имён из таблицы «Термины и имена» в COURSE_STYLE.md "
    "по скачанным урокам. Без запросов к Stepik.",
    annotations=LOCAL_ONLY_WRITE,
)
def stepik_glossary(course_id: int) -> str:
    return _offline(lessoncheck.glossary, course_id)


@mcp.tool(
    description="Предпросмотр локального урока в браузере: файл <урок>.preview.html рядом с уроком, шаги и задания "
    "без ответов, примерно как их увидит ученик. Файл создаётся заново при каждом вызове; урок не меняется. "
    "Без запросов к Stepik.",
    annotations=LOCAL_ONLY_WRITE,
)
def stepik_preview_lesson(course_id: int, lesson_position: str) -> str:
    return _offline(lessontools.preview_lesson, course_id, lesson_position)


@mcp.tool(
    description="Сводка комментариев учеников по шагам урока из сохранённого .comments.md: сколько комментариев, "
    "вопросов и жалоб («не понял», «почему», «не работает»), короткие примеры без имён. Без запросов к Stepik; "
    "комментарии сохраняет stepik_cache_lesson_comments. Только по запросу автора или в режимах правки и проверки.",
    annotations=LOCAL_READ,
)
def stepik_comment_digest(course_id: int, lesson_position: str) -> str:
    return _offline(learners.comment_digest, course_id, lesson_position)


@mcp.tool(
    description="Типичные ошибки учеников в задании: неверные решения шага сгруппированы по отзыву Stepik или по "
    "одинаковому ответу, с долей и примером, без имён. Только GET к Stepik; только по явному запросу автора. "
    "Данные API ещё не сверены с интерфейсом Stepik.",
    annotations=READ_ONLY,
)
def stepik_task_mistakes(step_id: int, scan_limit: int = 1000) -> str:
    return _run(lambda: json.dumps(learners.task_mistakes(CLIENT, step_id, scan_limit), ensure_ascii=False,
                                   indent=2))


@mcp.tool(
    description="Снимок показателей заданий урока (попытки, успешность по выборке решений, комментарии) в "
    "metrics/<урок>/<время>.json, чтобы потом сравнить «до и после» правки. Только GET к Stepik, запись только "
    "в папку курса. Только по явному запросу автора.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=True),
)
def stepik_save_metrics(course_id: int, lesson_position: str, max_submissions: int = 300, note: str = "") -> str:
    return _run(lambda: json.dumps(learners.save_metrics(CLIENT, course_id, lesson_position, max_submissions, note),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="Сравнить два снимка показателей урока (по умолчанию два последних): успешность и попытки по "
    "заданиям, комментарии по шагам. Без запросов к Stepik.",
    annotations=LOCAL_READ,
)
def stepik_compare_metrics(course_id: int, lesson_position: str, before: str | None = None,
                           after: str | None = None) -> str:
    return _offline(learners.compare_metrics, course_id, lesson_position, before, after)


@mcp.tool(
    description="Подготовить урок к проверке в тестовом курсе: копия урока новым черновиком drafts/… в папке "
    "тестового курса (шаги <!-- new_step -->, задания, локальные картинки). На Stepik ничего не отправляет; "
    "дальше stepik_plan_create_lesson и stepik_plan_push в тестовый курс с подтверждением автора. Папка "
    "тестового курса должна существовать (stepik_start_editing или stepik_cache_lesson для него).",
    annotations=LOCAL_ONLY_WRITE,
)
def stepik_stage_lesson(course_id: int, lesson_position: str, test_course_id: int) -> str:
    return _offline(staging.stage_lesson, course_id, lesson_position, test_course_id)


@mcp.tool(
    description="Проверка локального урока по карте курса COURSE_LOGIC.md: термины, которые карта относит к более "
    "поздним урокам (забегание вперёд, отдельно — в заданиях), отсылки к другим урокам и будущему материалу, "
    "«очевидно» и «как известно» вместо объяснения, длинные шаги, задания без теории перед ними. Эвристика: "
    "находки проверяет агент. Только локальные файлы, ответ — номера шагов и короткие цитаты.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def stepik_check_lesson(course_id: int, lesson_position: str) -> str:
    return _offline(lessoncheck.check_lesson, course_id, lesson_position)


@mcp.tool(
    description="Что сделано по курсу и что делать дальше: состояние уроков по карте, отчёты рецензий, ученика и "
    "второго взгляда в sessions/, нескачанные уроки, уроки с наибольшим числом сохранённых комментариев и последняя "
    "передача. Только локальные файлы, без запросов к Stepik.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def stepik_course_status(course_id: int) -> str:
    return _offline(coursemap.course_status, course_id)


@mcp.prompt(name="stepik_start", title="Stepik: с чего начать",
            description="Выбрать режим: скачать уроки, править курс вместе или пройти его учеником.")
def prompt_start() -> str:
    return (
        "Если я назвал курс и он уже есть в папке курсов, сначала вызови stepik_course_status: если есть "
        "последняя передача, прочитай её и предложи продолжить с того места.\n"
        "Предложи мне выбор из трёх режимов и дождись ответа:\n"
        "1. Скачать тексты уроков — только скачать нужные уроки или весь курс в локальную папку, без рецензии и правок.\n"
        "2. Править курс вместе — карта курса, поиск ошибок и недочётов с вариантами исправления, "
        "правка только после моего одобрения, публикация через diff.\n"
        "3. Пройти курс учеником — агент-тестировщик проходит уроки по порядку и пишет, что мешает ученику; "
        "уроки не меняет, после отчёта можно перейти к правке.\n"
        "Спроси номер курса, если я его не назвал. Мои курсы можно показать через stepik_my_courses."
    )


@mcp.prompt(name="stepik_student", title="Stepik: пройти курс учеником",
            description="Агент-тестировщик проходит уроки по порядку и ищет, что мешает ученику.")
def prompt_student(course_id: str = "", lessons: str = "", level: str = "novice") -> str:
    target = f"курса {course_id}" if course_id else "курса (спроси у меня номер)"
    which = f"уроки {lessons}" if lessons else "уроки по порядку с первого (или те, что я назову)"
    who = "ученик с опытом" if level == "experienced" else "полный новичок"
    level = "experienced" if level == "experienced" else "novice"
    return (
        f"Режим «ученик-тестировщик» ({who}) для {target}. Вызови stepik_start_editing с mode='student' и "
        f"level='{level}' и следуй правилам из ответа. Пройди {which}: скачай урок, прочитай его как ученик, "
        "который знает только пройденные уроки, реши задания сам по материалу урока, вызови stepik_check_lesson и "
        "stepik_run_examples. Критерии — rules/STUDENT_TEST_RULES.md. "
        "Отчёт запиши в sessions/ГГГГ-ММ-ДД-student-<урок>.md, а мне в чате коротко: номер, где, что случилось "
        "с учеником, тяжесть. Уроки и карту не меняй. В конце предложи перейти к правке по находкам."
    )


@mcp.prompt(name="stepik_second_opinion", title="Stepik: второй взгляд",
            description="Другая модель проверяет рецензию урока: согласна ли и что пропущено.")
def prompt_second_opinion(course_id: str = "", lesson: str = "") -> str:
    target = f"урока {lesson} курса {course_id}" if lesson and course_id else "урока (спроси у меня курс и урок)"
    return (
        f"Второй взгляд на рецензию {target}. Найди в sessions/ папки курса последний отчёт review или student по "
        "этому уроку, прочитай его, сам урок, COURSE_LOGIC.md и правила курса. По каждому пункту отчёта ответь: "
        "согласен / не согласен и почему / уточнение. Затем перечисли, что первый рецензент пропустил. Запиши "
        "результат в sessions/ГГГГ-ММ-ДД-second-<урок>.md. Урок не меняй. Мне в чате — короткая сводка."
    )


@mcp.prompt(name="stepik_download", title="Stepik: скачать уроки",
            description="Скачать тексты уроков или весь курс без правок.")
def prompt_download(course_id: str = "", lessons: str = "") -> str:
    target = f"курса {course_id}" if course_id else "курса (спроси у меня номер)"
    which = f"уроки {lessons}" if lessons else "уроки, которые я назову, или весь курс"
    return (
        f"Режим «скачать тексты уроков». Скачай {which} {target}: один урок — stepik_cache_lesson, "
        "весь курс — stepik_pull_course, только текстовые шаги — stepik_cache_course_text_lessons. "
        "Не рецензируй и не правь уроки, не выводи их текст в чат: покажи пути к файлам и краткую сводку."
    )


@mcp.prompt(name="stepik_edit", title="Stepik: править курс вместе",
            description="Редакторский режим с картой курса, вариантами исправлений и подтверждением.")
def prompt_edit(course_id: str = "", lesson: str = "") -> str:
    target = f"курса {course_id}" if course_id else "курса (спроси у меня номер)"
    start = f"Начнём с урока {lesson}." if lesson else "Спроси, с какого урока начать."
    return (
        f"Режим «править курс вместе» для {target}. Вызови stepik_start_editing и следуй правилам из ответа. "
        f"{start} Скачай урок, прочитай COURSE_LOGIC.md и правила курса, вызови stepik_check_lesson; если есть "
        "отчёт ученика по уроку, начни с его находок. Найди ошибки и недочёты и запиши рецензию в sessions/. "
        "Мне в чате покажи замечания коротко по номерам: где (шаг и цитата), что не так одной строкой и буквы "
        "вариантов A/B/C (замена текста, вставка, изменение задания); полный текст — в отчёте. Урок не меняй, "
        "пока я не отвечу; принятые пункты внеси все сразу. После правки обнови запись урока в COURSE_LOGIC.md; "
        "на Stepik публикуй только через stepik_plan_push (один общий diff) и моё «да»."
    )


@mcp.tool(
    description="Переводит локальный файл урока, скачанный старой версией сервера, в текущий формат (сохраняет "
    "стили блоков кода, таблицы, спойлеры, ссылки). Нетронутые шаги записываются заново; правки автора переносятся "
    "на новую запись шага (EDITED_MERGED), если не пересекаются со строками, которые меняет формат; иначе шаг "
    "попадает в conflicts и ничего не записывается. Без apply=True только показывает результат. С apply=True прежний "
    "файл сохраняется в snapshots/. Stepik нужен только для шагов без исходника (файлы формата 1), только GET.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
)
def stepik_upgrade_lesson(course_id: int, lesson_position: str, apply: bool = False) -> str:
    return _local(refresh.upgrade_lesson, course_id, lesson_position, apply)


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
    description="План перестановки урока внутри его модуля на Stepik. Ничего не отправляет. lesson_position — "
    "текущий номер вида 6.2, new_position — новое место в модуле (число). Применяется stepik_apply_push после "
    "подтверждения автора; номера соседних уроков сдвинутся, .course.json обновится, файлы уроков не переносятся.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_plan_move_lesson(course_id: int, lesson_position: str, new_position: int) -> str:
    return _run(lambda: json.dumps(publish.plan_move_lesson(CLIENT, course_id, lesson_position, new_position),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="План удаления урока со Stepik. Ничего не удаляет. Показывает число шагов и учеников. Применяется "
    "stepik_apply_push с confirm_delete=[lesson_id], только если автор явно подтвердил удаление: на Stepik его не "
    "отменить, пропадут решения и комментарии учеников. Шаги сохраняются в snapshots/, локальный файл не удаляется.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def stepik_plan_delete_lesson(course_id: int, lesson_position: str) -> str:
    return _run(lambda: json.dumps(publish.plan_delete_lesson(CLIENT, course_id, lesson_position),
                                   ensure_ascii=False, indent=2))


@mcp.tool(
    description="Публикует на Stepik план из stepik_plan_push, stepik_rollback_lesson или "
    "stepik_plan_create_lesson, stepik_plan_move_lesson, stepik_plan_delete_lesson: загружает картинки, меняет, добавляет или восстанавливает шаги, создаёт, переименовывает, переставляет и удаляет уроки. Вызывать только после того, как автор увидел diff и явно "
    "подтвердил публикацию. confirm_steps — step_id из плана, которые уже решали ученики; передавайте их, только "
    "если автор согласился изменить именно эти шаги. confirm_delete — ровно те step_id, которые план удаляет; "
    "передавайте их, только если автор подтвердил удаление (для удаления урока — [lesson_id]). Перед записью проверяет, что файл и урок на Stepik не "
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


@mcp.tool(annotations=READ_ONLY, description="Один шаг по его внутреннему id (не по номеру в уроке).")
def stepik_step(step_id: int, answers: bool = True) -> str:
    def work():
        sources = CLIENT.by_ids("step-sources", [step_id], key="step-sources")
        if not sources:
            return f"Шаг {step_id} не найден или недоступен."
        return render_step(sources[0], raw=False, answers=answers)
    return _run(work)


@mcp.tool(annotations=READ_ONLY, description="Принимает ссылку, скопированную из адресной строки Stepik: на курс, урок или конкретный шаг.")
def stepik_resolve(url: str, answers: bool = True) -> str:
    return _run(resolve_url, CLIENT, url, False, answers)


@mcp.tool(annotations=READ_ONLY, description="Все обычные комментарии и ответы к одному шагу с именами авторов, без вкладки решений. Для всего урока используй stepik_lesson_comments. Только чтение.")
def stepik_step_comments(step_id: int) -> str:
    return _run(render_step_comments, CLIENT, step_id)


@mcp.tool(annotations=READ_ONLY, description="Все обычные комментарии и ответы по шагам урока, без вкладки решений. Пакетная загрузка одним вызовом, группировка по номеру шага, без лимита количества.")
def stepik_lesson_comments(lesson_id: int) -> str:
    return _run(render_lesson_comments, CLIENT, lesson_id)


@mcp.tool(annotations=READ_ONLY, description="Агрегаты самого урока: просмотры, число полностью прошедших, среднее время (API: секунды, также выводятся минуты). Указывает источники и недоступные поля. course_id проверяет принадлежность урока курсу, но не фильтрует статистику по его ученикам.")
def stepik_lesson_stats(lesson_id: int, course_id: int | None = None) -> str:
    return _run(render_lesson_stats, CLIENT, lesson_id, course_id)


@mcp.tool(annotations=READ_ONLY, description="Статистика задания/шага: попытки, успешность, число решивших, оценки/реакции если API их возвращает.")
def stepik_step_stats(step_id: int, max_submissions: int = 2000) -> str:
    max_submissions = max(20, min(max_submissions, 10000))
    return _run(render_step_stats, CLIENT, step_id, max_submissions)


@mcp.tool(annotations=READ_ONLY, description="Обезличенная выборка неверных решений учащихся для анализа типичных ошибок. Имена/email/user_id не выводятся.")
def stepik_wrong_submissions(step_id: int, limit: int = 10, scan_limit: int = 1000) -> str:
    limit = max(1, min(limit, 50))
    scan_limit = max(limit, min(scan_limit, 10000))
    return _run(render_wrong_submissions, CLIENT, step_id, limit, scan_limit)


@mcp.tool(annotations=READ_ONLY, description="Отзывы учащихся по всему курсу Stepik; при наличии API также показывает сводку оценок.")
def stepik_course_reviews(course_id: int, limit: int = 100) -> str:
    limit = max(1, min(limit, 500))
    return _run(render_course_reviews, CLIENT, course_id, limit)


@mcp.tool(annotations=READ_ONLY, description="Пакет для AI-редактора: текст урока + программные задачи + комментарии + статистика + выборка неверных code-решений.")
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
