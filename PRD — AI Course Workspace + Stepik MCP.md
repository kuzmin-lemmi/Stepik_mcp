# PRD: AI Course Workspace + Stepik MCP

**Версия:** 0.1  
**Статус:** Draft  
**Тип продукта:** локальная система управления и развития курсов Stepik с помощью AI  
**Основной интерфейс:** OpenCode / AI-агент  
**Интеграция:** Stepik API через локальный MCP-сервер  
**Рабочее хранилище:** локальная файловая структура курса  
**Платформа публикации:** Stepik

---

# 1. Краткое описание продукта

AI Course Workspace — система для создания, редактирования, публикации и улучшения образовательных курсов Stepik с помощью AI.

Stepik используется как:

- платформа обучения;
- место публикации курса;
- система хранения прогресса учеников;
- источник статистики;
- источник комментариев;
- источник решений и попыток учеников.

Локальный AI Course Workspace используется как:

- рабочая среда автора;
- редактор курса;
- система контроля версий;
- инструмент анализа качества;
- среда AI-редактирования;
- система подготовки обновлений;
- слой безопасности между AI и Stepik.

Основная идея:

```text
Автор
  ↓
OpenCode / GPT
  ↓
Stepik MCP
  ↓
Stepik API
  ↓
Stepik
  ↓
Ученики
  ↓
Статистика / решения / комментарии
  ↓
Stepik MCP
  ↓
AI-анализ
  ↓
Предложения улучшений
  ↓
Автор
```

Система должна постепенно превратиться из инструмента загрузки уроков в полноценную среду управления жизненным циклом курса.

---

# 2. Проблема

Сейчас работа автора курса распределена между несколькими несвязанными процессами.

Автор:

- пишет уроки;
- вручную переносит их в Stepik;
- редактирует существующие уроки;
- просматривает комментарии;
- анализирует статистику;
- изучает ошибки учеников;
- пытается понять, где курс непонятен;
- исправляет проблемные уроки;
- создаёт новые курсы.

Большая часть этих действий выполняется вручную.

При этом AI может эффективно помогать с:

- написанием;
- редактированием;
- анализом;
- поиском проблем;
- генерацией заданий;
- проверкой структуры курса.

Но AI не имеет безопасного, структурированного и контролируемого доступа к Stepik.

---

# 3. Цель продукта

Создать систему, в которой автор может управлять курсами Stepik через естественный язык.

Примеры:

```text
Скачай урок 6.1.
```

```text
Проверь урок 6.1 и найди методические проблемы.
```

```text
Перепиши объяснение FSM, не сокращая материал.
```

```text
Покажи изменения.
```

```text
Загрузи новую версию на Stepik.
```

Или:

```text
Проанализируй весь курс.

Найди 10 шагов, на которых ученики испытывают
наибольшие трудности.
```

Или:

```text
Посмотри неправильные решения задачи 2498212.

Определи основные типы ошибок.
```

Или:

```text
Создай новый модуль курса по работе с API.

Сделай четыре урока.
```

---

# 4. Основной принцип

AI не должен напрямую работать с Stepik API.

Архитектура:

```text
AI
 ↓
разрешённые MCP tools
 ↓
Stepik MCP
 ↓
валидация
 ↓
контроль доступа
 ↓
Stepik API
```

AI никогда не получает:

```text
STEPIK_CLIENT_SECRET
access_token
refresh_token
пароли
```

Все секреты остаются внутри локального слоя интеграции.

---

# 5. Product Vision

Конечная версия продукта должна позволять пройти полный жизненный цикл курса:

```text
IDEA
 ↓
COURSE SPEC
 ↓
COURSE STRUCTURE
 ↓
LESSON CREATION
 ↓
AI REVIEW
 ↓
LOCAL VERSION
 ↓
STEPIC PREVIEW
 ↓
PUBLICATION
 ↓
STUDENTS
 ↓
ANALYTICS
 ↓
COMMENTS
 ↓
SOLUTIONS
 ↓
AI ANALYSIS
 ↓
COURSE ISSUES
 ↓
IMPROVEMENTS
 ↓
NEW VERSION
```

Таким образом курс становится не статичным набором уроков, а постоянно развивающимся продуктом.

---

# 6. Целевой пользователь

## Основной пользователь

Автор курсов Stepik.

Типичный сценарий:

- автор нескольких курсов;
- работает с большим количеством уроков;
- регулярно обновляет материалы;
- использует AI для написания и анализа;
- хочет автоматизировать рутинную работу.

---

# 7. Основные сценарии

## 7.1 Загрузка существующего урока

Автор:

```text
Скачай урок 6.1.
```

Система:

1. определяет course;
2. определяет section;
3. определяет lesson;
4. получает lesson;
5. получает все steps;
6. преобразует содержимое;
7. сохраняет локально;
8. записывает Stepik IDs;
9. сообщает путь.

Пример:

```text
C:\Courses\course-266999\
06-Память-бота-FSM\
06.01-lesson-2498603.md
```

---

# 8. Локальная структура курса

Предлагаемая структура:

```text
course-266999/
│
├── .course.json
├── COURSE_MAP.md
├── AGENTS.md
├── COURSE_STYLE.md
├── PRD.md
│
├── modules/
│   │
│   ├── 01-introduction/
│   │   ├── 01.01-lesson.md
│   │   ├── 01.02-lesson.md
│   │   └── ...
│   │
│   └── ...
│
├── analytics/
│
├── comments/
│
├── solutions/
│
├── issues/
│
├── reports/
│
├── snapshots/
│
└── drafts/
```

На первом этапе допускается сохранение текущей структуры проекта без каталога `modules`.

---

# 9. Метаданные курса

`.course.json` должен содержать:

```json
{
  "course_id": 266999,
  "title": "...",
  "stepik_url": "...",
  "allowed_write": false,
  "last_sync": "...",
  "modules": []
}
```

Для lesson:

```json
{
  "lesson_id": 2498603,
  "unit_id": 123,
  "section_id": 456,
  "position": 2
}
```

Для step:

```json
{
  "step_id": 123456,
  "position": 4,
  "type": "code"
}
```

Stepik IDs должны сохраняться и считаться стабильными идентификаторами.

---

# 10. Функциональные модули

# 10.1 Content Reader

Статус:

**частично реализован.**

Назначение:

получение контента из Stepik.

Инструменты:

```text
stepik_whoami

stepik_resolve

stepik_get_course

stepik_get_section

stepik_get_lesson

stepik_get_step

stepik_cache_lesson

stepik_cache_course
```

Система должна поддерживать:

- text;
- video metadata;
- choice;
- multiple choice;
- number;
- string;
- sorting;
- matching;
- code;
- другие поддерживаемые Stepik block types.

Неизвестный тип блока не должен приводить к потере данных.

---

# 10.2 Content Writer

Назначение:

создание и изменение контента.

Предполагаемые tools:

```text
stepik_create_section

stepik_create_lesson

stepik_create_step

stepik_update_lesson

stepik_update_step

stepik_move_step

stepik_delete_step
```

Операции удаления должны иметь повышенный уровень защиты.

---

# 11. Push workflow

AI не должен автоматически менять живой курс после редактирования файла.

Используется схема:

```text
EDIT
 ↓
VALIDATE
 ↓
DIFF
 ↓
CONFIRM
 ↓
PUSH
```

Команда:

```text
Подготовь урок 6.2 к загрузке.
```

Система показывает:

```text
Lesson 6.2

Changed steps:

Step 3
TEXT MODIFIED

Step 5
CODE TASK MODIFIED

Step 7
NEW STEP
```

После подтверждения производится push.

---

# 12. Diff Engine

Система должна уметь сравнивать:

```text
LOCAL
vs
STEPIK
```

Результат:

```text
UNCHANGED
MODIFIED
NEW
DELETED
MOVED
```

Сравнение должно выполняться по Stepik ID, а не только по позиции.

---

# 13. Snapshot System

Перед каждой write-операцией система должна сохранять предыдущую версию.

Структура:

```text
snapshots/

2026-09-23_153000/
    lesson-2498603.json
```

Это позволит реализовать:

```text
Откати урок 6.2 к версии до последней загрузки.
```

---

# 14. Rollback

Команда:

```text
stepik_rollback_lesson
```

или пользовательский запрос:

```text
Верни предыдущую версию урока.
```

Перед rollback система должна показать diff.

---

# 15. Security Model

Это один из ключевых элементов системы.

## 15.1 Secrets

AI не имеет доступа к:

```text
client_secret
access_token
refresh_token
```

Секреты загружает только MCP-процесс.

---

# 15.2 Course allowlist

В конфигурации:

```text
ALLOWED_READ_COURSES=...
ALLOWED_WRITE_COURSES=...
```

Например:

```text
ALLOWED_WRITE_COURSES=382910,382911
```

Попытка:

```text
update course 266999
```

возвращает:

```text
WRITE DENIED

Course 266999 is not in write allowlist.
```

---

# 15.3 Запрещённые операции

На первых версиях MCP не должен позволять:

- менять цену;
- публиковать курс;
- удалять курс;
- изменять выплаты;
- управлять платёжными настройками;
- менять владельца курса;
- удалять пользователей;
- изменять критические настройки курса.

Для этих действий tools вообще не создаются.

---

# 16. Dry Run

Любая write-операция должна поддерживать:

```text
dry_run=true
```

Пример:

```text
stepik_push_lesson(
    lesson=2498603,
    dry_run=true
)
```

Результат:

```text
Would update:

2 text steps
1 code task

No changes have been sent to Stepik.
```

---

# 17. Comments Module

Система должна получать комментарии.

Tools:

```text
stepik_get_comments

stepik_get_lesson_comments

stepik_get_step_comments
```

Комментарии должны группироваться:

```text
LESSON
  STEP 1
    comment
    comment

  STEP 2
    comment
```

---

# 18. AI Comments Analysis

AI должен классифицировать комментарии.

Категории:

```text
QUESTION
CONTENT_ERROR
TECHNICAL_ERROR
UNCLEAR_EXPLANATION
TASK_ERROR
TYPO
FEATURE_REQUEST
POSITIVE_FEEDBACK
OTHER
```

---

# 19. Course Issues

Повторяющиеся проблемы превращаются в issues.

Например:

```text
issues/
ISSUE-0042.md
```

Содержание:

```text
# ISSUE-0042

Lesson: 6.2
Step: 4

Type:
UNCLEAR_EXPLANATION

Evidence:
5 similar student comments.

AI hypothesis:
Students do not understand why state must be cleared.

Recommendation:
Add diagram before code example.
```

---

# 20. Solutions Module

Получение решений учеников.

Tools:

```text
stepik_get_submissions

stepik_get_attempts

stepik_get_incorrect_solutions
```

Данные должны обрабатываться с учётом приватности.

По возможности локальные отчёты не должны хранить лишнюю персональную информацию.

---

# 21. Incorrect Solutions Analyzer

Одна из ключевых функций продукта.

Запрос:

```text
Проанализируй ошибки задачи 6.2.4.
```

Система:

1. получает выборку неправильных решений;
2. удаляет ненужные пользовательские данные;
3. передаёт код AI;
4. группирует решения;
5. выявляет основные ошибки;
6. формирует отчёт.

Пример:

```text
82 incorrect submissions analysed.

38%
forgot await

24%
used incorrect FSM context

18%
did not clear state

11%
incorrect callback handling

9%
other
```

---

# 22. Analytics Module

Необходимые показатели:

- просмотры;
- started;
- completed;
- completion rate;
- attempts;
- correct attempts;
- incorrect attempts;
- average attempts;
- success rate;
- time spent;
- drop-off;
- grades;
- activity.

Конкретный набор зависит от доступности данных Stepik API.

---

# 23. Course Health Report

Команда:

```text
stepik_course_health
```

Результат:

```text
COURSE HEALTH

Students: ...
Active: ...

Potential problems: 11

CRITICAL

Lesson 4.3 / Step 7
success rate: 21%

Lesson 6.2 / Step 4
average attempts: 5.8

Lesson 3.1
high dropout
```

---

# 24. AI Course Reviewer

Это один из главных конечных продуктов системы.

Reviewer получает:

- содержимое курса;
- структуру;
- статистику;
- комментарии;
- неправильные решения;
- правила курса.

На выходе создаётся:

```text
COURSE_REVIEW.md
```

---

# 25. Типы проблем

Reviewer должен искать:

## Методические

- слишком большой скачок сложности;
- отсутствует объяснение;
- новый термин используется до объяснения;
- слишком сложное задание;
- задание не связано с теорией;
- недостаточно практики.

## Технические

- ошибочный код;
- устаревшая библиотека;
- неправильный API;
- битая ссылка;
- ошибка HTML.

## Структурные

- слишком длинный шаг;
- слишком большой урок;
- нарушение последовательности;
- повтор материала.

## Реальные проблемы учеников

- высокий процент неправильных решений;
- много попыток;
- большое время;
- массовые комментарии;
- высокий drop-off.

---

# 26. Course Quality Score

В будущем возможно рассчитывать условный показатель:

```text
Course Quality Score
```

Например:

```text
Content quality       89
Task quality          82
Student success       74
Student feedback      91
Technical health      96

TOTAL                 85
```

Это не должно использоваться как абсолютная оценка качества.

Показатель нужен для отслеживания изменений курса во времени.

---

# 27. COURSE_STYLE.md

У каждого курса должен существовать набор редакторских правил.

Например:

```text
COURSE_STYLE.md
```

Он определяет:

- тон;
- размер шагов;
- структуру объяснений;
- формат примеров;
- оформление кода;
- допустимое количество теории;
- сложность заданий;
- правила терминологии;
- использование изображений;
- использование аналогий;
- требования к итоговым заданиям.

AI Reviewer обязан учитывать этот файл.

---

# 28. AGENTS.md

Содержит правила работы AI.

Например:

```text
Never upload changes without diff.

Never delete Stepik steps automatically.

Never change pricing.

Never change publishing settings.

Never modify Stepik IDs manually.
```

---

# 29. Создание нового курса

AI Course Workspace должен поддерживать разработку курса с нуля.

Запрос:

```text
Создай структуру курса по Mojo для начинающих.
```

AI создаёт:

```text
COURSE_SPEC.md

MODULE 1
MODULE 2
MODULE 3

LESSON ...
```

Сначала всё существует локально.

После проверки:

```text
push course skeleton
```

создаются модули и уроки Stepik.

---

# 30. Course Factory

В перспективе:

```text
course init
```

создаёт:

```text
PRD.md
COURSE_SPEC.md
COURSE_STYLE.md
AGENTS.md
COURSE_MAP.md
.course.json
```

После чего AI может разработать новый курс в единой структуре.

---

# 31. Staging Course

Рекомендуется поддержать отдельный Stepik-курс для тестирования.

Схема:

```text
LOCAL
 ↓
STAGING STEPIK
 ↓
manual review
 ↓
PRODUCTION STEPIK
```

Это особенно важно для:

- HTML;
- изображений;
- code tasks;
- сложных интерактивных шагов.

---

# 32. Promotion

В будущем:

```text
stepik_promote_lesson
```

может копировать подтверждённый урок:

```text
STAGING → PRODUCTION
```

При этом Stepik IDs staging и production будут разными, поэтому необходима отдельная карта соответствий.

---

# 33. External Grader

Отдельное направление развития.

Используется для языков или задач, которых нет в стандартном Stepik grader.

Пример:

```text
Stepik
 ↓
External Grader
 ↓
our server
 ↓
Docker sandbox
 ↓
Mojo compiler
 ↓
tests
 ↓
result
 ↓
Stepik
```

Это позволит создавать курсы по новым технологиям, даже если Stepik их напрямую не поддерживает.

---

# 34. Telegram Integration

Не является MVP.

В будущем автор может получать уведомления:

```text
За сутки:

+93 registrations

17 comments

3 comments marked CONTENT_ERROR

Lesson 6.2 has unusual failure rate.
```

Telegram используется только как дополнительный интерфейс уведомлений.

---

# 35. Авторский Dashboard

В будущем возможно создание локальной веб-панели.

Например:

```text
http://localhost:8765
```

Dashboard:

```text
Courses

Python Bots
Health 86%

Lua
Health 91%

Mojo
Draft
```

Внутри курса:

```text
Students
Completion
Problem lessons
Comments
Recent issues
Pending changes
```

---

# 36. MCP архитектура

Предлагается разделить MCP на логические подсистемы.

```text
stepik-mcp/

core/
    auth
    api
    permissions
    errors

content/
    courses
    sections
    lessons
    steps

sync/
    pull
    push
    diff
    snapshots

feedback/
    comments
    solutions

analytics/
    lessons
    tasks
    course_health

workspace/
    cache
    metadata
    maps
```

---

# 37. Tool naming

Все MCP tools должны иметь единообразный префикс:

```text
stepik_
```

Пример:

```text
stepik_pull_lesson
stepik_push_lesson
stepik_diff_lesson
```

---

# 38. Tool philosophy

Tool должен быть:

- маленьким;
- предсказуемым;
- безопасным;
- идемпотентным, где возможно;
- легко тестируемым.

Не следует создавать:

```text
stepik_do_everything
```

---

# 39. Error handling

Ошибки должны возвращаться структурированно.

Пример:

```json
{
  "status": "error",
  "code": "WRITE_NOT_ALLOWED",
  "message": "Course 266999 is not in write allowlist."
}
```

AI должен понимать причину ошибки.

---

# 40. Logging

Все write operations логируются.

```text
logs/stepik-write.log
```

Пример:

```text
2026-09-23

ACTION:
update_step

course:
382910

lesson:
812991

step:
9812881

result:
success
```

Секреты никогда не записываются в лог.

---

# 41. Audit log

Для каждого изменения желательно хранить:

```text
timestamp
course_id
lesson_id
step_id
operation
old_hash
new_hash
result
```

---

# 42. MVP

Первая полезная версия не должна пытаться реализовать всю систему.

## MVP 1 — Reliable Reader

Необходимо:

- course resolution;
- lesson resolution;
- полноценная загрузка steps;
- корректная обработка code steps;
- cache lesson;
- cache course;
- стабильные metadata;
- повторное открытие без Stepik request.

---

# 43. MVP 2 — Feedback Reader

Добавить:

- comments;
- attempts;
- incorrect solutions;
- базовую statistics.

Цель:

AI может анализировать курс, но пока не может его менять.

---

# 44. MVP 3 — Safe Writer

Добавить:

- create lesson;
- create step;
- update text step;
- update code step;
- dry run;
- diff;
- snapshot;
- allowlist.

На этом этапе можно сделать первый реальный тестовый курс.

---

# 45. MVP 4 — Course Reviewer

Добавить:

```text
stepik_analyze_lesson

stepik_analyze_course

stepik_analyze_task
```

AI использует:

```text
CONTENT
+
COMMENTS
+
SOLUTIONS
+
ANALYTICS
```

---

# 46. MVP 5 — Full Sync

Появляется:

```text
pull
diff
push
rollback
```

Локальный workspace становится основным местом работы.

---

# 47. MVP 6 — New Course Creation

AI может создавать:

```text
modules
lessons
steps
tasks
```

с нуля.

---

# 48. Roadmap

## Phase 1

Reliable Stepik Reader

```text
READ
CACHE
RESOLVE
```

## Phase 2

Feedback

```text
COMMENTS
SOLUTIONS
ATTEMPTS
ANALYTICS
```

## Phase 3

AI Analysis

```text
COURSE REVIEW
TASK REVIEW
ISSUES
```

## Phase 4

Safe Write

```text
CREATE
UPDATE
DIFF
PUSH
SNAPSHOT
```

## Phase 5

Full Workspace

```text
PULL
EDIT
REVIEW
PUSH
ROLLBACK
```

## Phase 6

Course Factory

```text
CREATE NEW COURSE
```

## Phase 7

External tools

```text
External Grader
Dashboard
Telegram
```

---

# 49. Приоритет разработки

Приоритет следует строить не по эффектности функции, а по полезности.

### P0

```text
reliable lesson download
code steps
course metadata
```

### P1

```text
comments
incorrect solutions
attempts
analytics
```

### P2

```text
AI analysis
course issues
course reports
```

### P3

```text
diff
safe update
push
```

### P4

```text
new course creation
```

### P5

```text
dashboard
telegram
external grader
```

---

# 50. Почему analytics раньше write

Хотя возможность AI автоматически выкладывать уроки выглядит впечатляюще, для существующих курсов гораздо большую ценность даёт анализ поведения реальных учеников.

Поэтому рекомендуемый порядок:

```text
READ
 ↓
ANALYZE
 ↓
UNDERSTAND
 ↓
WRITE
```

а не:

```text
READ
 ↓
WRITE
```

---

# 51. Основной AI workflow существующего курса

```text
stepik_pull_course
       ↓
local workspace
       ↓
stepik_pull_analytics
       ↓
stepik_pull_comments
       ↓
stepik_pull_solutions
       ↓
AI Course Reviewer
       ↓
COURSE_REVIEW.md
       ↓
issues
       ↓
author selects issue
       ↓
AI edits lesson
       ↓
diff
       ↓
author approves
       ↓
push
       ↓
new analytics
```

---

# 52. Цикл улучшения

После обновления система должна иметь возможность сравнить показатели.

Например:

```text
BEFORE

success rate
42%

average attempts
4.7
```

После изменения:

```text
AFTER

success rate
67%

average attempts
2.8
```

Так система сможет оценивать эффективность изменений.

---

# 53. Change Impact Analysis

В будущем AI должен отвечать:

```text
Изменение объяснения в шаге 4 улучшило
успешность задания с 42% до 67%.
```

Таким образом появляется возможность проводить образовательные эксперименты.

---

# 54. A/B Testing

Долгосрочная экспериментальная функция.

Разным группам учеников можно показывать разные варианты объяснения, если это позволяет Stepik или внешний слой.

Система сравнивает:

```text
Variant A
Variant B
```

по:

- success rate;
- attempts;
- completion;
- time.

Не входит в ближайший roadmap.

---

# 55. Метрики продукта

Успех AI Course Workspace можно измерять.

## Авторские

Время:

```text
download lesson
edit lesson
publish lesson
```

Количество ручных операций.

## Качество курса

```text
completion rate
success rate
average attempts
drop-off
```

## AI usefulness

Количество AI-рекомендаций:

```text
suggested
accepted
rejected
```

---

# 56. Главная продуктовая метрика

Предлагаемая North Star Metric:

```text
Количество подтверждённых улучшений курса,
основанных на реальных данных учеников.
```

Не количество AI-запросов.

Не количество сгенерированных уроков.

Именно улучшения, которые автор реально принял.

---

# 57. Нефункциональные требования

Система должна:

- работать локально на Windows;
- поддерживать OpenCode;
- не зависеть от конкретного AI-провайдера;
- не хранить секреты в git;
- переживать сетевые ошибки;
- не портить локальный файл при частичной загрузке;
- не портить Stepik lesson при частичной записи;
- иметь понятные ошибки;
- обеспечивать повторяемость операций.

---

# 58. Git

Workspace рекомендуется хранить в Git.

Пример:

```text
git add .
git commit -m "Improve FSM lesson explanation"
```

Stepik IDs остаются в metadata.

Это даст вторую систему защиты кроме snapshots.

---

# 59. AI providers

Архитектура не должна зависеть от GPT.

Можно использовать:

```text
GPT
Claude
Gemini
Qwen
local models
```

Stepik MCP остаётся одинаковым.

---

# 60. Multi-agent workflow

В перспективе:

```text
AUTHOR AGENT
     ↓
REVIEWER AGENT
     ↓
TECHNICAL REVIEWER
     ↓
PUBLISHER
```

Publisher — не LLM с прямым API.

Publisher — ограниченный набор MCP tools.

---

# 61. Пример работы

Автор:

```text
Что сейчас самое слабое место курса?
```

AI:

```text
По статистике и комментариям наиболее
проблемным выглядит урок 6.2.

Step 4:
31% successful submissions.

Students made 412 failed attempts.

Most common problem:
incorrect FSM state clearing.

There are also 7 comments indicating that
the explanation before the task is unclear.
```

Автор:

```text
Предложи исправление.
```

AI создаёт diff.

Автор:

```text
Применяй.
```

AI изменяет локальный файл.

Автор:

```text
Выложи.
```

MCP:

```text
DRY RUN

1 Stepik step will be modified.

Confirm?
```

После подтверждения:

```text
PUSH SUCCESS
```

---

# 62. Что продукт НЕ должен делать

Система не должна превращаться в бесконтрольного AI-администратора Stepik.

Она не должна самостоятельно:

- менять цену;
- публиковать коммерческий курс;
- удалять курс;
- удалять большое количество уроков;
- принимать финансовые решения;
- менять владельцев;
- рассылать сообщения ученикам;
- принимать важные действия без подтверждения.

---

# 63. Главная архитектурная идея

Stepik не является основной рабочей средой автора.

Основная среда:

```text
LOCAL AI COURSE WORKSPACE
```

Stepik становится:

```text
DELIVERY PLATFORM
+
STUDENT DATA SOURCE
```

---

# 64. Конечное состояние продукта

Автор открывает OpenCode и пишет:

```text
Продолжим работу над курсом по ботам.
```

AI уже знает:

- структуру курса;
- правила;
- стиль;
- Stepik IDs;
- предыдущие изменения;
- проблемные уроки;
- комментарии учеников;
- статистику;
- список issues.

Автор может сказать:

```text
Давай сегодня исправим три самых
проблемных задания курса.
```

И система сама:

```text
1. получает свежие данные;
2. выбирает кандидатов;
3. объясняет причины;
4. показывает решения учеников;
5. предлагает изменения;
6. изменяет локальные уроки;
7. проводит review;
8. показывает diff;
9. после подтверждения загружает изменения.
```

---

# 65. Итоговая формула продукта

```text
STEPiK API
+
LOCAL WORKSPACE
+
MCP
+
AI
+
REAL STUDENT DATA
=
AI COURSE WORKSPACE
```

Главная ценность системы заключается не в том, что AI умеет писать уроки.

Главная ценность:

> **AI получает обратную связь от реальных учеников, помогает автору находить слабые места курса и превращает эти данные в конкретные контролируемые улучшения.**