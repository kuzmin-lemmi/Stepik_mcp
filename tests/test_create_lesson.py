import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, workspace as sw
from stepik_mcp.client import StepikClient, StepikError


class LessonFixture(unittest.TestCase):
    """Курс с одним модулем и фейковый Stepik, который не сдвигает соседние уроки."""

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
        self.client.put = Mock(side_effect=self.put)
        self.client.delete = Mock(side_effect=self.delete)
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.module = next(self.folder.glob("01-*"))
        self.draft = self.module / "draft.md"
        self.draft.write_text("# Новый урок\n\n<!-- new_step -->\nТеория нового урока\n", encoding="utf-8")
        self.draft_path = self.draft.relative_to(self.folder).as_posix()

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": 7}]}
        if "lesson" in params:
            return {endpoint: [row for row in self.data[endpoint] if row["lesson"] == params["lesson"]]}
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def resort(self):
        # Как настоящий Stepik: соседей не сдвигает, при равной позиции новый юнит идёт позже.
        section = self.data["sections"][0]
        rows = {row["id"]: row for row in self.data["units"]}
        section["units"] = sorted(section["units"], key=lambda uid: (rows[uid]["position"], uid))

    def put(self, endpoint, payload):
        self.assertTrue(endpoint.startswith("units/"), endpoint)
        row = next(row for row in self.data["units"] if row["id"] == int(endpoint.split("/")[1]))
        row["position"] = payload["unit"]["position"]
        self.resort()
        return {"units": [row]}

    def delete(self, endpoint):
        kind, number = endpoint.split("/")
        if kind == "units":
            self.data["units"] = [row for row in self.data["units"] if row["id"] != int(number)]
            self.data["sections"][0]["units"].remove(int(number))
        else:
            self.assertEqual(kind, "lessons")
            self.data["lessons"] = [row for row in self.data["lessons"] if row["id"] != int(number)]
        return {}

    def add_lesson(self, lesson_id, unit_id):
        """Второй урок модуля, скачанный локально."""
        self.data["lessons"].append({"id": lesson_id, "title": f"Lesson {lesson_id}", "steps": []})
        self.data["units"].append({"id": unit_id, "lesson": lesson_id, "section": 10,
                                   "position": len(self.data["units"]) + 1})
        self.data["sections"][0]["units"].append(unit_id)

    def post(self, endpoint, payload):
        if endpoint == "lessons":
            lesson = {"id": 2000, "steps": [], **payload["lesson"]}
            self.data["lessons"].append(lesson)
            return {"lessons": [lesson]}
        self.assertEqual(endpoint, "units")
        unit = {"id": 200, **payload["unit"]}
        self.data["sections"][0]["units"].append(unit["id"])
        self.data["units"].append(unit)
        self.resort()
        return {"units": [unit]}


class CreateLessonTests(LessonFixture):
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


class MoveDeleteLessonTests(LessonFixture):
    """Перестановка и удаление уроков на фейковом Stepik, который не сдвигает соседей."""

    def setUp(self):
        super().setUp()
        self.add_lesson(3000, 300)
        self.add_lesson(4000, 400)
        sw.pull_course(self.client, 1)

    def order(self):
        rows = {row["id"]: row for row in self.data["units"]}
        return [(rows[uid]["lesson"], rows[uid]["position"]) for uid in self.data["sections"][0]["units"]]

    def test_move_lesson(self):
        plan = publish.plan_move_lesson(self.client, 1, "1.3", 1)
        self.assertEqual(plan["move_to"], "1.1")
        self.client.put.assert_not_called()
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(result["lesson_position"], "1.1")
        self.assertEqual(self.order(), [(4000, 1), (1000, 2), (3000, 3)])
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["lessons"]["1.2"]["lesson_id"], 1000)
        self.assertTrue(manifest["lessons"]["1.2"]["path"].endswith("01.01-lesson-1000.md"))

    def test_move_to_same_place_is_noop(self):
        self.assertEqual(publish.plan_move_lesson(self.client, 1, "1.2", 2)["status"], "nothing_to_push")

    def test_delete_lesson_requires_exact_confirmation(self):
        plan = publish.plan_delete_lesson(self.client, 1, "1.1")
        self.assertEqual(plan["confirm_delete"], [1000])
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[1000, 3000])
        self.client.delete.assert_not_called()
        result = publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[1000])
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.order(), [(3000, 1), (4000, 2)])
        snapshot = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(snapshot["step_sources"][0]["block"]["text"], "<p>Theory</p>")
        self.assertTrue(list(self.folder.glob("*/01.01-lesson-1000.md")))  # локальный файл остался
        manifest = json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["lessons"]), ["1.1", "1.2"])

    def test_insert_in_the_middle_sets_positions(self):
        plan = publish.plan_create_lesson(self.client, 1, self.draft_path, 1, lesson_position=2)
        publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.order(), [(1000, 1), (2000, 2), (3000, 3), (4000, 4)])

    def test_module_changed_after_plan(self):
        plan = publish.plan_move_lesson(self.client, 1, "1.3", 1)
        self.add_lesson(5000, 500)
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])
