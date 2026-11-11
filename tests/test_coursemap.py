import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import coursemap, lessoncheck, server
from stepik_mcp.client import StepikClient, StepikError


class CourseFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Python", "sections": [10, 20]}],
            "sections": [
                {"id": 10, "title": "Basics", "position": 1, "units": [100, 101]},
                {"id": 20, "title": "Loops", "position": 2, "units": [200]},
            ],
            "units": [
                {"id": 100, "lesson": 1000, "section": 10, "position": 1},
                {"id": 101, "lesson": 1001, "section": 10, "position": 2},
                {"id": 200, "lesson": 2000, "section": 20, "position": 1},
            ],
            "lessons": [
                {"id": 1000, "title": "Variables"},
                {"id": 1001, "title": "Input"},
                {"id": 2000, "title": "While"},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.folder = self.root / "course-1"

    def get(self, endpoint, **params):
        self.assertIn(endpoint, self.data, "Unexpected endpoint")
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}


class StartEditingTests(CourseFixture):
    def test_first_call_creates_map_with_every_module_and_lesson(self):
        result = coursemap.start_editing(self.client, 1)
        text = (self.folder / "COURSE_LOGIC.md").read_text(encoding="utf-8")
        self.assertEqual(result["course_logic"]["status"], "created")
        for line in ("# Карта курса: Python", "## Модуль 1. Basics", "### 1.2. Input", "### 2.1. While"):
            self.assertIn(line, text)
        self.assertEqual(result["lessons_without_summary"], ["1.1", "1.2", "2.1"])
        self.assertEqual(result["lessons_missing_in_map"], [])
        self.assertTrue((self.folder / "DECISIONS.md").is_file())
        self.assertTrue((self.folder / ".course.json").is_file())
        self.assertIn("Варианты", (self.folder / "rules/REVIEW_RULES.md").read_text(encoding="utf-8"))
        self.assertNotIn("Variables", json.dumps(result["rules"], ensure_ascii=False))

    def test_existing_map_is_never_overwritten_and_gaps_are_reported(self):
        coursemap.start_editing(self.client, 1)
        logic = self.folder / "COURSE_LOGIC.md"
        edited = logic.read_text(encoding="utf-8").replace(
            "### 1.1. Variables\n\n- **Что ученик узнаёт:**",
            "### 1.1. Variables\n\n- **Что ученик узнаёт:** имена и присваивание",
        ).replace("### 2.1. While", "### Удалён автором")
        logic.write_text(edited, encoding="utf-8")
        calls = self.client.get.call_count

        result = coursemap.start_editing(self.client, 1)

        self.assertEqual(logic.read_text(encoding="utf-8"), edited)
        self.assertEqual(self.client.get.call_count, calls, "Second call must not hit Stepik")
        self.assertEqual(result["course_logic"]["status"], "existing")
        self.assertEqual(result["lessons_without_summary"], ["1.2"])
        self.assertEqual(result["lessons_missing_in_map"], ["2.1"])

    def test_foreign_folder_without_index_is_refused(self):
        self.folder.mkdir(parents=True)
        (self.folder / "notes.md").write_text("mine", encoding="utf-8")
        with self.assertRaises(StepikError):
            coursemap.start_editing(self.client, 1)
        self.assertFalse((self.folder / "COURSE_LOGIC.md").exists())
        self.assertFalse((self.folder / ".stepik-cache.lock").exists())

    def test_gaps_ignore_bold_markers_and_other_headings(self):
        text = "### 1.1. A\n- **Что ученик узнаёт:** **\n## Модуль 2\n- **Что ученик узнаёт:** x\n### 2.1. B\n"
        self.assertEqual(coursemap.logic_gaps(text), (["1.1", "2.1"], ["1.1", "2.1"]))


    def test_student_mode_returns_student_rules_and_last_handoff(self):
        coursemap.start_editing(self.client, 1)
        sessions = self.folder / "sessions"
        (sessions / "2026-11-01-handoff.md").write_text("old", encoding="utf-8")
        (sessions / "2026-11-05-handoff-5.2.md").write_text("new", encoding="utf-8")

        result = coursemap.start_editing(self.client, 1, "student")

        self.assertEqual(result["mode"], "student")
        self.assertEqual(result["last_handoff"], "sessions/2026-11-05-handoff-5.2.md")
        self.assertIn("rules/STUDENT_TEST_RULES.md", result["read_first"])
        self.assertTrue(any("ученик" in rule for rule in result["rules"]))
        self.assertIn("с потолка", (self.folder / "rules/STUDENT_TEST_RULES.md").read_text(encoding="utf-8"))
        with self.assertRaises(StepikError):
            coursemap.start_editing(self.client, 1, "teacher")


LESSON = """---
format: 6
---
# Lesson

<!-- step_id: 11 -->
## Шаг 1

Сначала решите задачу. Очевидно, что ответ 4.

```yaml stepik
type: string
answer: "4"
```

<!-- step_id: 12 -->
## Шаг 2

Переменная хранит значение. Как мы уже говорили в уроке 1.1, имена важны.
Позже мы узнаем про циклы и функции. Вызов `print()` выводит текст.

```python
for i in range(3):
    print(i)
```

<!-- step_id: 13 -->
## Шаг 3

Напишите цикл, который выводит числа.

```yaml stepik
type: code
solution: solutions/loop.py
```
"""


class PreparedCourse(CourseFixture):
    def prepare(self):
        coursemap.start_editing(self.client, 1)
        logic = self.folder / "COURSE_LOGIC.md"
        text = logic.read_text(encoding="utf-8")
        text = text.replace("### 1.1. Variables\n\n- **Что ученик узнаёт:**",
                            "### 1.1. Variables\n\n- **Что ученик узнаёт:** имена")
        text = text.replace("### 1.2. Input\n\n- **Что ученик узнаёт:**\n- **Опирается на:**\n- **Новые термины:**",
                            "### 1.2. Input\n\n- **Что ученик узнаёт:**\n- **Опирается на:**\n"
                            "- **Новые термины:** переменная, `print()`")
        text = text.replace("### 2.1. While\n\n- **Что ученик узнаёт:**\n- **Опирается на:**\n- **Новые термины:**",
                            "### 2.1. While\n\n- **Что ученик узнаёт:**\n- **Опирается на:**\n"
                            "- **Новые термины:** цикл (loop), функция, `for`, переменная")
        logic.write_text(text, encoding="utf-8")
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        path = self.folder / manifest["lessons"]["1.2"]["path"]
        path.write_text(LESSON, encoding="utf-8")
        return path


class LessonCheckAndStatusTests(PreparedCourse):
    def test_check_finds_terms_from_later_lessons_references_and_hand_waving(self):
        self.prepare()
        calls = self.client.get.call_count

        result = lessoncheck.check_lesson(1, "1.2")

        self.assertEqual(self.client.get.call_count, calls, "Check must not hit Stepik")
        ahead = {item["term"]: item for item in result["ahead_of_map"]}
        self.assertEqual(set(ahead), {"цикл", "функция", "`for`"})
        self.assertEqual(ahead["цикл"]["steps"], [2, 3])
        self.assertTrue(ahead["цикл"]["in_tasks"])
        self.assertEqual(ahead["`for`"]["steps"], [2])
        self.assertFalse(ahead["`for`"]["in_tasks"])
        self.assertNotIn("переменная", ahead, "Term of the current lesson is not ahead")
        self.assertEqual(result["map_terms"], ["переменная", "`print()`"])
        self.assertEqual({item["step"] for item in result["references"]}, {2})
        self.assertEqual(len(result["references"]), 3)
        self.assertEqual([item["step"] for item in result["hand_waving"]], [1])
        self.assertEqual(result["tasks_without_theory_before"], [1])
        self.assertEqual(result["tasks"], [{"step": 1, "type": "string"},
                                           {"step": 3, "type": "code", "solution": True}])

    def test_check_without_map_still_checks_references(self):
        self.prepare()
        (self.folder / "COURSE_LOGIC.md").unlink()
        result = lessoncheck.check_lesson(1, "1.2")
        self.assertFalse(result["course_logic"])
        self.assertEqual(result["ahead_of_map"], [])
        self.assertEqual(result["references_count"], 3)

    def test_status_lists_reports_comments_and_next_steps(self):
        path = self.prepare()
        sessions = self.folder / "sessions"
        (sessions / "2026-11-02-review-1.1.md").write_text("r", encoding="utf-8")
        (sessions / "2026-11-03-student-1.2.md").write_text("s", encoding="utf-8")
        (sessions / "2026-11-04-handoff.md").write_text("h", encoding="utf-8")
        path.with_name(path.name[:-3] + ".comments.md").write_text(
            "# c\n\n### comment 1 · A\n\nx\n\n### comment 2 · B\n\ny\n", encoding="utf-8")
        calls = self.client.get.call_count

        result = coursemap.course_status(1)

        self.assertEqual(self.client.get.call_count, calls)
        self.assertEqual(result["by_state"], {"не проверен": ["1.1", "1.2", "2.1"]})
        self.assertEqual(result["reports"], {"1.1": {"review": "sessions/2026-11-02-review-1.1.md"},
                                             "1.2": {"student": "sessions/2026-11-03-student-1.2.md"}})
        self.assertEqual(result["most_commented"], [{"lesson": "1.2", "comments": 2}])
        self.assertEqual(result["not_downloaded"], ["1.1", "2.1"])
        self.assertEqual(result["lessons_without_summary"], ["1.2", "2.1"])
        self.assertEqual(result["last_handoff"], "sessions/2026-11-04-handoff.md")
        self.assertIn("1.2", result["next"][0])
        self.assertIn("2.1", result["next"][2])

    def test_references_skip_headings_and_the_current_lesson(self):
        steps = [{"number": 1, "condition": "### Шаг 1. Начало\nВ уроке 5.2 мы здесь. В модуле 5 тоже. "
                                            "Это появится в модуле 7, а в уроке 5.1 было.\n"
                                            "```\n# в следующем уроке\n```"}]
        quotes = [item["quote"] for item in lessoncheck._find(lessoncheck.REFERENCES, steps, "5.2")]
        self.assertEqual(len(quotes), 2)
        self.assertIn("модуле 7", quotes[0])
        self.assertIn("уроке 5.1", quotes[1])

    def test_term_patterns_match_word_forms_and_code(self):
        self.assertTrue(lessoncheck.term_pattern("функция").search("вызов функции"))
        self.assertTrue(lessoncheck.term_pattern("цикл").search("Циклы бывают"))
        self.assertFalse(lessoncheck.term_pattern("`for`").search("format"))
        self.assertTrue(lessoncheck.term_pattern("`len()`").search("x = len(s)"))
        self.assertFalse(lessoncheck.term_pattern("`len()`").search("s.blen(x)"))
        self.assertTrue(lessoncheck.term_pattern("loop").search("A Loop"))


class ModePromptTests(unittest.TestCase):
    def test_mode_prompts_and_tool_are_registered(self):
        prompts = {prompt.name for prompt in asyncio.run(server.mcp.list_prompts())}
        self.assertTrue({"stepik_start", "stepik_download", "stepik_edit", "stepik_student",
                         "stepik_second_opinion"} <= prompts)
        tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        self.assertTrue({"stepik_start_editing", "stepik_check_lesson", "stepik_course_status"} <= tools)

    def test_local_tools_answer_with_json(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": root}):
            answer = json.loads(server.stepik_course_status(5))
        self.assertEqual(answer["code"], "NOT_FOUND")

    def test_edit_prompt_names_course_and_lesson(self):
        text = server.prompt_edit("266999", "5.2")
        self.assertIn("266999", text)
        self.assertIn("5.2", text)
        self.assertIn("stepik_start_editing", text)


if __name__ == "__main__":
    unittest.main()
