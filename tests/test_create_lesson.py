import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, workspace as sw
from stepik_mcp.client import StepikClient, StepikError


class CreateLessonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root),
                                              "ALLOWED_WRITE_COURSES": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10]}],
            "sections": [{"id": 10, "course": 1, "title": "Module", "position": 1, "units": [100]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1}],
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [11]}],
            "step-sources": [{"id": 11, "lesson": 1000, "block": {"name": "text", "text": "<p>Theory</p>"}}],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = Mock(side_effect=self.post)
        self.client.put = self.client.delete = Mock(side_effect=AssertionError("unexpected write"))
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.module = next(self.folder.glob("01-*"))
        self.draft = self.module / "draft.md"
        self.draft.write_text("# Новый урок\n\n<!-- new_step -->\nТеория нового урока\n", encoding="utf-8")
        self.draft_path = self.draft.relative_to(self.folder).as_posix()

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": 7}]}
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def post(self, endpoint, payload):
        if endpoint == "lessons":
            lesson = {"id": 2000, "steps": [], **payload["lesson"]}
            self.data["lessons"].append(lesson)
            return {"lessons": [lesson]}
        self.assertEqual(endpoint, "units")
        unit = {"id": 200, **payload["unit"]}
        section = self.data["sections"][0]
        for row in self.data["units"]:
            if row["position"] >= unit["position"]:
                row["position"] += 1
        section["units"].insert(unit["position"] - 1, unit["id"])
        self.data["units"].append(unit)
        return {"units": [unit]}

    def test_plan_sends_nothing(self):
        plan = publish.plan_create_lesson(self.client, 1, self.draft_path, 1)
        self.assertEqual(plan["title"], "Новый урок")
        self.assertEqual(plan["lesson_position"], "1.2")
        self.assertEqual(plan["new_steps_in_draft"], 1)
        self.client.post.assert_not_called()

    def test_apply_creates_lesson_and_links_draft(self):
        plan = publish.plan_create_lesson(self.client, 1, self.draft_path, 1)
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual((result["status"], result["lesson_id"], result["lesson_position"]), ("applied", 2000, "1.2"))
        self.assertEqual(self.client.post.call_args_list[1].args,
                         ("units", {"unit": {"section": 10, "lesson": 2000, "position": 2}}))
        text = self.draft.read_text(encoding="utf-8")
        self.assertIn("lesson_id: 2000", text)
        self.assertIn("<!-- new_step -->\nТеория нового урока", text)
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["lessons"]["1.2"]["path"], self.draft_path)
        self.assertEqual(manifest["lessons"]["1.1"]["lesson_id"], 1000)
        self.assertTrue(list((self.folder / "snapshots").glob("draft.draft-*.md")))
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])  # план применяется один раз

    def test_insert_before_existing_lesson_keeps_its_file(self):
        plan = publish.plan_create_lesson(self.client, 1, self.draft_path, 1, lesson_position=1)
        self.assertIn("сдвинутся", plan["note"])
        publish.apply_push(self.client, 1, plan["plan_id"])
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["lessons"]["1.1"]["lesson_id"], 2000)
        self.assertEqual(manifest["lessons"]["1.2"]["lesson_id"], 1000)
        self.assertTrue(manifest["lessons"]["1.2"]["path"].endswith("01.01-lesson-1000.md"))

    def test_draft_changed_after_plan(self):
        plan = publish.plan_create_lesson(self.client, 1, self.draft_path, 1)
        self.draft.write_text("# Другое\n", encoding="utf-8")
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])
        self.client.post.assert_not_called()

    def test_rejects_linked_files_and_foreign_step_ids(self):
        lesson = next(self.folder.glob("*/01.01-lesson-1000.md")).relative_to(self.folder).as_posix()
        with self.assertRaises(StepikError):
            publish.plan_create_lesson(self.client, 1, lesson, 1)
        self.draft.write_text("# X\n\n<!-- step_id: 11 -->\nA\n", encoding="utf-8")
        with self.assertRaises(StepikError):
            publish.plan_create_lesson(self.client, 1, self.draft_path, 1)
        with self.assertRaises(StepikError):
            publish.plan_create_lesson(self.client, 1, "../outside.md", 1)

    def test_not_allowed_course(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": ""}):
            with self.assertRaises(StepikError):
                publish.plan_create_lesson(self.client, 1, self.draft_path, 1)
