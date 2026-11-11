import json
from unittest.mock import patch

from stepik_mcp import coursemap, learners, lessoncheck, lessontools, publish, staging, versions
from stepik_mcp.client import StepikError

from test_coursemap import LESSON, PreparedCourse

EXAMPLES = """---
format: 6
---
# Examples

<!-- step_id: 31 -->
## Шаг 1

```python
print(2 + 2)
```

Вывод:

```text
4
```

```python
print(1)
```

```text
2
```

<!-- step_id: 32 -->
## Шаг 2

```python
1 / 0
```

```python
name = input()
```

```python
>>> 1 + 1
2
```

```python
import no_such_module_for_stepik_tests
```

```python
print("без ожидаемого вывода")
```
"""


class LessonToolsTests(PreparedCourse):
    def test_outline_gives_file_lines_for_each_step(self):
        path = self.prepare()
        result = lessontools.lesson_outline(1, "1.2")
        self.assertEqual([step["type"] for step in result["steps"]], ["string", "text", "code"])
        self.assertEqual(result["steps"][1]["code_blocks"], 1)
        start, end = map(int, result["steps"][1]["lines"].split("-"))
        lines = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[start - 1], "<!-- step_id: 12 -->")
        self.assertEqual(lines[end], "<!-- step_id: 13 -->")

    def test_versions_save_list_and_restore_whole_file_or_one_step(self):
        path = self.prepare()
        first = versions.save_version(1, "1.2", "до правки")
        self.assertEqual(first["status"], "saved")
        self.assertEqual(versions.save_version(1, "1.2")["status"], "unchanged")
        edited = LESSON.replace("Переменная хранит значение.", "Переменная — это имя.").replace(
            "Напишите цикл", "Напишите программу")
        path.write_text(edited, encoding="utf-8")
        listed = versions.list_versions(1, "1.2")
        self.assertEqual(listed["count"], 1)
        self.assertGreater(listed["versions"][0]["changed_lines_vs_current"], 0)

        plan = versions.restore_version(1, "1.2", first["version"], step_id=12)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(path.read_text(encoding="utf-8"), edited)

        done = versions.restore_version(1, "1.2", first["version"], step_id=12, apply=True)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(done["status"], "restored")
        self.assertIn("Переменная хранит значение.", text)
        self.assertIn("Напишите программу", text, "Other steps keep the author's edits")
        self.assertEqual(len(versions.list_versions(1, "1.2")["versions"]), 2, "Backup saved before restore")

        versions.restore_version(1, "1.2", first["version"], apply=True)
        self.assertEqual(path.read_text(encoding="utf-8"), LESSON)
        with self.assertRaises(StepikError):
            versions.restore_version(1, "1.2", "../../etc")

    def test_run_examples_compares_output_and_reports_problems(self):
        path = self.prepare()
        path.write_text(EXAMPLES, encoding="utf-8")
        result = lessontools.run_examples(1, "1.2")
        self.assertEqual(result["counts"], {"ok": 1, "differs": 1, "error": 1, "needs_input": 1, "repl": 1,
                                            "missing_module": 1, "no_expected_output": 1})
        differs = next(item for item in result["problems"] if item["status"] == "differs")
        self.assertEqual((differs["expected"], differs["got"]), ("2", "1"))
        self.assertIn("ZeroDivisionError", next(item for item in result["problems"]
                                                if item["status"] == "error")["error"])
        self.assertEqual(lessontools.run_examples(1, "1.2", step=1)["examples"], 2)

    def test_preview_writes_html_next_to_lesson_without_answers(self):
        path = self.prepare()
        result = lessontools.preview_lesson(1, "1.2")
        page = (path.parent / (path.name[:-3] + ".preview.html")).read_text(encoding="utf-8")
        self.assertEqual(result["steps"], 3)
        self.assertIn("Шаг 1 из 3", page)
        self.assertIn("Задание: string", page)
        self.assertIn("<textarea", page)
        self.assertNotIn('answer: "4"', page)
        self.assertEqual(path.read_text(encoding="utf-8"), LESSON)
        lessontools.preview_lesson(1, "1.2")

    def test_course_check_summarises_lessons_and_writes_report(self):
        path = self.prepare()
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        (self.folder / manifest["lessons"]["1.1"]["path"]).write_text(
            "---\nformat: 6\n---\n# A\n\n<!-- step_id: 1 -->\n## Шаг 1\n\nПросто текст.\n", encoding="utf-8")
        self.assertTrue(path.is_file())
        result = lessoncheck.check_course(1)
        self.assertEqual(result["lessons_checked"], 2)
        self.assertEqual(result["top"]["ahead"], [{"lesson": "1.2", "ahead": 3}])
        report = (self.folder / result["report"]).read_text(encoding="utf-8")
        self.assertIn("| 1.2 |", report)
        self.assertIn("цикл", report)
        second = lessoncheck.check_course(1)
        self.assertNotEqual(second["report"], result["report"], "Reports are never overwritten")

    def test_glossary_lists_terms_and_finds_names_to_avoid(self):
        self.prepare()
        style = self.folder / "COURSE_STYLE.md"
        style.write_text(style.read_text(encoding="utf-8").replace(
            "| --- | --- | --- |", "| --- | --- | --- |\n| идентификатор | имена, ярлык | 1.1 |"), encoding="utf-8")
        result = lessoncheck.glossary(1)
        glossary = (self.folder / "GLOSSARY.md").read_text(encoding="utf-8")
        self.assertIn("| цикл | 2.1 | — |", glossary)
        self.assertEqual(result["introduced_twice"], [{"term": "переменная", "lessons": ["1.2", "2.1"]}])
        self.assertEqual([(item["found"], item["lesson"], item["step"]) for item in result["wrong_names"]],
                         [("имена", "1.2", 2)])

    def test_comment_digest_counts_questions_without_names(self):
        path = self.prepare()
        path.with_name(path.name[:-3] + ".comments.md").write_text(
            "# Комментарии\n\n## Шаг 2 · step 12\n\n### comment 1 · Иван Петров · 2026\n\nПочему так?\n\n"
            "### comment 2 · Мария · 2026\n\nСпасибо\n\n### comment 3 · Автор · 2026 · ответ на comment 1\n\n"
            "Потому что\n\n## Шаг 3 · step 13\n\n### comment 4 · Олег\n\nВсё ясно\n", encoding="utf-8")
        result = learners.comment_digest(1, "1.2")
        self.assertEqual(result["comments"], 3)
        first = result["steps"][0]
        self.assertEqual((first["step"], first["comments"], first["questions"], first["confusion"]), (2, 2, 1, 1))
        self.assertEqual(first["samples"], ["Почему так?"])
        self.assertNotIn("Иван", json.dumps(result, ensure_ascii=False))

    def test_task_mistakes_group_wrong_answers(self):
        rows = [{"status": "wrong", "reply": '{"text": "5"}'}, {"status": "wrong", "reply": '{"text": " 5 "}'},
                {"status": "wrong", "reply": '{"code": "x"}', "hint": "Failed test #3"},
                {"status": "wrong", "reply": '{"code": "y"}', "hint": "Failed test #3"},
                {"status": "wrong", "reply": '{"text": "6"}'}, {"status": "correct", "reply": '{"text": "4"}'}]
        with patch.object(learners, "step_submissions", return_value=(rows, False)):
            result = learners.task_mistakes(None, 13)
        self.assertEqual((result["scanned"], result["wrong"], result["groups"]), (6, 5, 3))
        self.assertEqual(result["top"][0]["count"], 2)
        self.assertEqual({group.get("feedback") for group in result["top"]}, {None, "Failed test #3"})

    def test_metrics_snapshots_compare_before_and_after(self):
        self.prepare()
        stats = {"attempts_loaded": 10, "truncated": False, "counts": {"correct": 4, "wrong": 6},
                 "calculated_ratio": 0.4, "official_ratio": None, "passed_by": 3}
        with patch.object(learners, "step_statistics", return_value=stats) as fake:
            first = learners.save_metrics(None, 1, "1.2")
        self.assertEqual(fake.call_count, 2, "Only task steps are fetched")
        directory = self.folder / "metrics" / "1.2"
        (directory / f"{first['snapshot']}.json").rename(directory / "20200101-000000.json")
        better = {**stats, "attempts_loaded": 20, "calculated_ratio": 0.7}
        with patch.object(learners, "step_statistics", return_value=better):
            learners.save_metrics(None, 1, "1.2")
        result = learners.compare_metrics(1, "1.2")
        self.assertEqual(result["before"], "20200101-000000")
        self.assertEqual([row["success_change"] for row in result["steps"]], [0.3, 0.3])

    def test_stage_lesson_creates_new_draft_in_test_course(self):
        path = self.prepare()
        image = path.parent / "img" / "a.png"
        image.parent.mkdir()
        image.write_bytes(b"png")
        path.write_text(LESSON.replace("Напишите цикл", "![](img/a.png)\n\nНапишите цикл"), encoding="utf-8")
        test = self.root / "course-2"
        test.mkdir()
        (test / ".course.json").write_text(json.dumps(
            {"version": 1, "course_id": 2, "title": "Test", "modules": [], "lessons": {}}), encoding="utf-8")

        result = staging.stage_lesson(1, "1.2", 2)

        draft = (test / result["draft_path"]).read_text(encoding="utf-8")
        self.assertNotIn("step_id", draft)
        self.assertEqual(draft.count("<!-- new_step -->"), 3)
        self.assertEqual(publish._draft_title(draft), "Lesson")
        self.assertIn("```yaml stepik", draft)
        self.assertEqual((test / "drafts" / "img" / "a.png").read_bytes(), b"png")
        self.assertEqual(result["copied_images"], ["img/a.png"])
        with self.assertRaises(StepikError):
            staging.stage_lesson(1, "1.2", 1)

    def test_student_level_changes_the_first_rule(self):
        result = coursemap.start_editing(self.client, 1, "student", "experienced")
        self.assertEqual(result["level"], "experienced")
        self.assertIn("с опытом", result["rules"][0])
        with self.assertRaises(StepikError):
            coursemap.start_editing(self.client, 1, "student", "expert")
