import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, workspace as sw
from stepik_mcp.client import StepikClient, StepikError


class ModuleFixture(unittest.TestCase):
    """Курс из двух модулей и фейковый Stepik, который не сдвигает соседей сам."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root),
                                              "ALLOWED_WRITE_COURSES": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10, 20]}],
            "sections": [{"id": 10, "course": 1, "title": "Первый", "position": 1, "units": [100, 101]},
                         {"id": 20, "course": 1, "title": "Второй", "position": 2, "units": [200]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1},
                      {"id": 101, "lesson": 1001, "section": 10, "position": 2},
                      {"id": 200, "lesson": 2000, "section": 20, "position": 1}],
            "lessons": [{"id": 1000, "title": "A", "steps": [11]}, {"id": 1001, "title": "B", "steps": []},
                        {"id": 2000, "title": "C", "steps": []}],
            "step-sources": [{"id": 11, "lesson": 1000, "block": {"name": "text", "text": "<p>Theory</p>"}}],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = Mock(side_effect=self.post)
        self.client.put = Mock(side_effect=self.put)
        self.client.delete = Mock(side_effect=self.delete)
        sw.pull_course(self.client, 1)
        self.folder = self.root / "course-1"

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": 7}]}
        if "lesson" in params:
            return {endpoint: [row for row in self.data[endpoint] if row["lesson"] == params["lesson"]]}
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def section(self, sid):
        return next(row for row in self.data["sections"] if row["id"] == sid)

    def resort(self):
        rows = {row["id"]: row for row in self.data["sections"]}
        self.data["courses"][0]["sections"].sort(key=lambda sid: (rows[sid]["position"], sid))
        units = {row["id"]: row for row in self.data["units"]}
        for section in self.data["sections"]:
            section["units"] = sorted((uid for uid, row in units.items() if row["section"] == section["id"]),
                                      key=lambda uid: (units[uid]["position"], uid))

    def post(self, endpoint, payload):
        self.assertEqual(endpoint, "sections")
        row = {"id": 30, "units": [], **payload["section"]}
        self.data["sections"].append(row)
        self.data["courses"][0]["sections"].append(30)
        self.resort()
        return {"sections": [row]}

    def put(self, endpoint, payload):
        kind, number = endpoint.split("/")
        rows = self.data[kind]
        row = next(row for row in rows if row["id"] == int(number))
        row.update(payload["unit" if kind == "units" else "section"])
        self.resort()
        return {kind: [row]}

    def delete(self, endpoint):
        kind, number = endpoint.split("/")
        self.assertEqual(kind, "sections")
        self.data["sections"] = [row for row in self.data["sections"] if row["id"] != int(number)]
        self.data["courses"][0]["sections"].remove(int(number))
        return {}

    def modules(self):
        return [(self.section(sid)["title"], self.section(sid)["position"]) for sid in self.data["courses"][0]["sections"]]

    def manifest(self):
        return json.loads((self.folder / ".course.json").read_text(encoding="utf-8"))


class ModuleTests(ModuleFixture):
    def test_create_module_at_start(self):
        plan = publish.plan_create_module(self.client, 1, "  Введение  ", module_position=1)
        self.assertIn("сдвинутся", plan["note"])
        self.client.post.assert_not_called()
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.modules(), [("Введение", 1), ("Первый", 2), ("Второй", 3)])
        self.assertEqual(result["module_position"], 1)
        self.assertTrue((self.folder / result["path"]).is_dir())
        manifest = self.manifest()
        self.assertEqual(manifest["lessons"]["2.1"]["lesson_id"], 1000)
        self.assertTrue(manifest["lessons"]["2.1"]["path"].startswith("01-"))  # файл урока не переехал
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])

    def test_new_module_accepts_new_lesson(self):
        plan = publish.plan_create_module(self.client, 1, "Третий")
        path = publish.apply_push(self.client, 1, plan["plan_id"])["path"]
        draft = self.folder / path / "draft.md"
        draft.write_text("# Новый\n\n<!-- new_step -->\nТекст\n", encoding="utf-8")
        lesson_plan = publish.plan_create_lesson(self.client, 1, f"{path}/draft.md", 3)
        self.assertEqual(lesson_plan["lesson_position"], "3.1")

    def test_rename_module(self):
        self.assertEqual(publish.plan_rename_module(self.client, 1, 2, "Второй")["status"], "nothing_to_push")
        plan = publish.plan_rename_module(self.client, 1, 2, "Циклы")
        publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.modules(), [("Первый", 1), ("Циклы", 2)])
        self.assertEqual(self.manifest()["modules"][1]["title"], "Циклы")

    def test_move_module(self):
        plan = publish.plan_move_module(self.client, 1, 2, 1)
        publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.modules(), [("Второй", 1), ("Первый", 2)])
        manifest = self.manifest()
        self.assertEqual(manifest["lessons"]["1.1"]["lesson_id"], 2000)
        self.assertEqual(manifest["lessons"]["2.2"]["lesson_id"], 1001)

    def test_delete_only_empty_module_with_confirmation(self):
        with self.assertRaises(StepikError):
            publish.plan_delete_module(self.client, 1, 2)
        self.data["units"] = [row for row in self.data["units"] if row["id"] != 200]
        self.resort()
        plan = publish.plan_delete_module(self.client, 1, 2)
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])
        self.client.delete.assert_not_called()
        publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[20])
        self.assertEqual(self.modules(), [("Первый", 1)])

    def test_course_changed_after_plan(self):
        plan = publish.plan_move_module(self.client, 1, 2, 1)
        publish.apply_push(self.client, 1, publish.plan_create_module(self.client, 1, "X")["plan_id"])
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])

    def test_not_allowed_course(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": ""}):
            with self.assertRaises(StepikError):
                publish.plan_create_module(self.client, 1, "X")


class MoveLessonBetweenModulesTests(ModuleFixture):
    def order(self, sid):
        units = {row["id"]: row for row in self.data["units"]}
        return [(units[uid]["lesson"], units[uid]["position"]) for uid in self.section(sid)["units"]]

    def test_move_to_other_module(self):
        plan = publish.plan_move_lesson(self.client, 1, "1.1", 1, module_position=2)
        self.assertEqual(plan["move_to"], "2.1")
        self.client.put.assert_not_called()
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(result["lesson_position"], "2.1")
        self.assertEqual(self.order(10), [(1001, 1)])
        self.assertEqual(self.order(20), [(1000, 1), (2000, 2)])
        manifest = self.manifest()
        self.assertEqual(manifest["lessons"]["2.1"]["section_id"], 20)
        self.assertTrue(manifest["lessons"]["2.1"]["path"].endswith("01.01-lesson-1000.md"))

    def test_move_to_end_by_default(self):
        plan = publish.plan_move_lesson(self.client, 1, "2.1", module_position=1)
        self.assertEqual(plan["move_to"], "1.3")
        publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.order(10), [(1000, 1), (1001, 2), (2000, 3)])
        self.assertEqual(self.order(20), [])

    def test_target_changed_after_plan(self):
        plan = publish.plan_move_lesson(self.client, 1, "1.1", module_position=2)
        self.data["units"].append({"id": 201, "lesson": 1001, "section": 20, "position": 2})
        self.resort()
        with self.assertRaises(StepikError):
            publish.apply_push(self.client, 1, plan["plan_id"])
