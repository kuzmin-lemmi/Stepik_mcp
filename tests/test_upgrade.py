import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, refresh, steps, workspace as sw
from stepik_mcp.client import StepikClient

QUOTE_STEP = ('<p>Вступление</p><blockquote><p>Итог</p></blockquote><p>Середина</p>'
              '<pre style="background:#2d3436;"><code class="language-python">x = 1</code></pre><p>Конец</p>')


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root), "ALLOWED_WRITE_COURSES": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10]}],
            "sections": [{"id": 10, "course": 1, "title": "Module", "position": 1, "units": [100]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1}],
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [11, 12]}],
            "step-sources": [
                {"id": 11, "lesson": 1000, "update_date": "2000-01-01T00:00:00Z",
                 "block": {"name": "text", "text": QUOTE_STEP}},
                {"id": 12, "lesson": 1000, "update_date": "2000-01-01T00:00:00Z",
                 "block": {"name": "text", "text": "<p>Простой шаг</p>"}},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = self.client.put = self.client.delete = Mock(side_effect=AssertionError("write"))
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.lesson = next(self.folder.glob("*/01.01-lesson-1000.md"))
        self.make_old_format(2)

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": 7}]}
        rows = self.data[endpoint]
        if "ids" in params:
            return {endpoint: [row for row in rows if row["id"] in params["ids"]]}
        return {endpoint: [row for row in rows if row["lesson"] == params["lesson"]]}

    def make_old_format(self, fmt):
        """Файл, каким его записала бы старая версия сервера."""
        text = self.lesson.read_text(encoding="utf-8")
        for source in self.data["step-sources"]:
            text = text.replace(steps.to_markdown(source).strip(), publish.expected_body(source, fmt))
        text = re.sub(r"(?m)^format: \d+$", f"format: {fmt}", text)
        self.lesson.write_text(text, encoding="utf-8", newline="\n")
        self.old_text = text

    def edit(self, old, new):
        text = self.lesson.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, old)
        self.lesson.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    def upgrade(self, apply=False):
        return refresh.upgrade_lesson(self.client, 1, "1.1", apply=apply)

    def test_untouched_lesson_is_rewritten_in_new_format(self):
        self.assertIn("> \n\nИтог", self.old_text)  # формат 3: цитата пустая, текст под ней
        plan = self.upgrade()
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["steps"], [{"step_id": 11, "status": "UPGRADED"}])
        self.assertEqual(self.lesson.read_text(encoding="utf-8"), self.old_text)
        result = self.upgrade(apply=True)
        text = self.lesson.read_text(encoding="utf-8")
        self.assertIn(f"format: {steps.FORMAT}", text)
        self.assertIn("> Итог", text)
        self.assertIn('```python {style="background:#2d3436;"}', text)
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), self.old_text)
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")
        self.assertEqual(self.upgrade()["status"], "up_to_date")

    def test_edit_away_from_format_changes_is_merged(self):
        self.edit("\nКонец\n", "\nКонец, исправленный автором\n")
        self.edit("Простой шаг", "Простой шаг с правкой")
        result = self.upgrade(apply=True)
        self.assertEqual(result["steps"], [{"step_id": 11, "status": "EDITED_MERGED"},
                                           {"step_id": 12, "status": "EDITED_KEPT"}])
        text = self.lesson.read_text(encoding="utf-8")
        self.assertIn("> Итог", text)
        self.assertIn("Конец, исправленный автором", text)
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(len(plan["changes"]), 2)
        self.assertNotIn("будет потеряно", plan["changes"][0])

    def test_edit_inside_changed_lines_is_conflict(self):
        self.edit("\nИтог\n", "\nИтог автора\n")
        result = self.upgrade(apply=True)
        self.assertEqual(result["status"], "conflicts")
        self.assertEqual(result["conflicts"][0]["step_id"], 11)
        self.assertEqual(self.lesson.read_text(encoding="utf-8").count("Итог автора"), 1)
        self.assertIn("format: 2", self.lesson.read_text(encoding="utf-8"))

    def test_format_1_without_sources_uses_stepik(self):
        self.make_old_format(1)
        text = re.sub(r"(?m)^format: 1\n", "", self.lesson.read_text(encoding="utf-8"))
        self.lesson.write_text(text, encoding="utf-8", newline="\n")
        for path in (self.folder / ".stepik" / "steps").glob("*.json"):
            path.unlink()
        result = self.upgrade(apply=True)
        self.assertEqual(result["status"], "upgraded")
        self.assertIsNotNone(sw.load_step_source(self.folder, 11))
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_format_1_step_changed_on_stepik_is_conflict(self):
        for path in (self.folder / ".stepik" / "steps").glob("*.json"):
            path.unlink()
        self.data["step-sources"][1]["update_date"] = "2999-01-01T00:00:00Z"
        result = self.upgrade(apply=True)
        self.assertEqual(result["conflicts"][0]["step_id"], 12)


class MergeEditTests(unittest.TestCase):
    def test_insert_and_replace(self):
        old, new = "a\nb\nc", "A\nb\nc"
        self.assertEqual(refresh.merge_edit(old, "a\nb\nc\nd", new), "A\nb\nc\nd")
        self.assertEqual(refresh.merge_edit(old, "a\nB\nc", new), "A\nB\nc")
        self.assertIsNone(refresh.merge_edit(old, "a2\nb\nc", new))
