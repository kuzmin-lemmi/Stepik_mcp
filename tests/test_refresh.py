import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, refresh, steps, workspace as sw
from stepik_mcp.client import StepikClient


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10]}],
            "sections": [{"id": 10, "course": 1, "title": "Module", "position": 1, "units": [100]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1}],
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [11, 12, 13]}],
            "step-sources": [
                {"id": 11, "lesson": 1000, "block": {"name": "text", "text": "<p>Theory</p>"}},
                {"id": 12, "lesson": 1000, "block": {"name": "string", "text": "<p>Question</p>",
                                                     "source": {**steps.DEFAULT_SOURCES["string"], "pattern": "42"}}},
                {"id": 13, "lesson": 1000, "block": {"name": "text", "text": "<p>Outro</p>"}},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = self.client.put = self.client.delete = Mock(side_effect=AssertionError("write"))
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.lesson = next(self.folder.glob("*/01.01-lesson-1000.md"))

    def get(self, endpoint, **params):
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def remote(self, sid):
        return next(row for row in self.data["step-sources"] if row["id"] == sid)

    def text(self):
        return self.lesson.read_text(encoding="utf-8")

    def edit(self, old, new):
        self.lesson.write_text(self.text().replace(old, new), encoding="utf-8", newline="\n")

    def run_refresh(self, **kwargs):
        return refresh.refresh_lesson(self.client, 1, "1.1", **kwargs)

    def test_fresh_download_has_nothing_to_refresh(self):
        self.assertEqual(self.run_refresh()["status"], "nothing_to_refresh")

    def test_step_changed_only_on_stepik_is_updated(self):
        self.remote(11)["block"]["text"] = "<p>Theory v2</p>"
        before = self.text()
        plan = self.run_refresh()
        self.assertEqual(plan["changes"], [{"step_id": 11, "status": "UPDATED", "type": "text"}])
        self.assertEqual(self.text(), before)  # без apply ничего не записано
        result = self.run_refresh(apply=True)
        self.assertEqual(result["status"], "refreshed")
        self.assertIn("Theory v2", self.text())
        self.assertNotIn("\nTheory\n", self.text())
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), before)
        self.assertEqual(self.run_refresh()["status"], "nothing_to_refresh")
        self.assertEqual(sw.load_step_source(self.folder, 11)["block"]["text"], "<p>Theory v2</p>")

    def test_local_edit_is_kept(self):
        self.edit("\nTheory\n", "\nTheory edited\n")
        self.assertEqual(self.run_refresh(apply=True)["status"], "nothing_to_refresh")
        self.assertIn("Theory edited", self.text())

    def test_conflict_keeps_local_and_saves_stepik_version(self):
        self.edit("\nTheory\n", "\nTheory local\n")
        self.remote(11)["block"]["text"] = "<p>Theory remote</p>"
        result = self.run_refresh(apply=True)
        self.assertEqual(result["conflicts"][0]["step_id"], 11)
        self.assertIn("Theory local", self.text())
        self.assertIn("Theory remote", Path(result["conflicts_file"]).read_text(encoding="utf-8"))
        self.assertEqual(sw.load_step_source(self.folder, 11)["block"]["text"], "<p>Theory</p>")
        # После ручного переноса автор принимает версию Stepik как базу: публикуется его правка.
        self.run_refresh(apply=True, resolved=[11])
        self.assertEqual(sw.load_step_source(self.folder, 11)["block"]["text"], "<p>Theory remote</p>")
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": "1"}):
            self.client.get = Mock(side_effect=self.get_with_units)
            plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["status"], "planned", plan)
        self.assertEqual([change["step"] for change in plan["changes"]], ["step_id 11, позиция 1"])
        self.assertEqual(plan["skipped"], [])

    def get_with_units(self, endpoint, **params):
        if "lesson" in params:
            return {endpoint: [row for row in self.data[endpoint] if row["lesson"] == params["lesson"]]}
        return self.get(endpoint, **params)

    def test_new_stepik_step_is_inserted_in_place(self):
        self.data["step-sources"].append({"id": 14, "lesson": 1000, "block": {"name": "text", "text": "<p>Inserted</p>"}})
        self.data["lessons"][0]["steps"] = [11, 14, 12, 13]
        result = self.run_refresh(apply=True)
        self.assertEqual(result["changes"][0]["status"], "ADDED")
        ids = [entry["step_id"] for entry in publish.parse_lesson(self.text())]
        self.assertEqual(ids, [11, 14, 12, 13])
        self.assertIsNotNone(sw.load_step_source(self.folder, 14))

    def test_step_deleted_on_stepik(self):
        self.data["lessons"][0]["steps"] = [11, 12]
        self.data["step-sources"] = [row for row in self.data["step-sources"] if row["id"] != 13]
        result = self.run_refresh(apply=True)
        self.assertEqual(result["changes"], [{"step_id": 13, "status": "REMOVED", "note": "шаг удалён на Stepik"}])
        self.assertNotIn("Outro", self.text())

    def test_deleted_on_stepik_but_edited_locally_is_conflict(self):
        self.edit("\nOutro\n", "\nOutro edited\n")
        self.data["lessons"][0]["steps"] = [11, 12]
        self.data["step-sources"] = [row for row in self.data["step-sources"] if row["id"] != 13]
        result = self.run_refresh(apply=True)
        self.assertEqual(result["conflicts"][0]["step_id"], 13)
        self.assertIn("Outro edited", self.text())

    def test_new_local_step_survives(self):
        self.lesson.write_text(self.text() + "\n<!-- new_step -->\nNew theory\n", encoding="utf-8", newline="\n")
        self.remote(11)["block"]["text"] = "<p>Theory v2</p>"
        self.run_refresh(apply=True)
        self.assertIn("<!-- new_step -->\nNew theory", self.text())

    def test_unknown_resolved_step_is_rejected(self):
        with self.assertRaises(Exception):
            self.run_refresh(apply=True, resolved=[11])


class PullCourseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10], "authors": [7],
                         "summary": "Коротко", "description": "<p>Длинное <strong>описание</strong></p>"}],
            "users": [{"id": 7, "full_name": "Автор"}],
            "sections": [{"id": 10, "title": "Module", "position": 1, "units": [100, 200]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1},
                      {"id": 200, "lesson": 2000, "section": 10, "position": 2}],
            "lessons": [{"id": 1000, "title": "One", "steps": [11]}, {"id": 2000, "title": "Two", "steps": [21]}],
            "step-sources": [
                {"id": 11, "lesson": 1000, "block": {"name": "text", "text": "<p>Theory</p>"}},
                {"id": 21, "lesson": 2000, "block": {"name": "string", "text": "<p>Q</p>",
                                                     "source": {**steps.DEFAULT_SOURCES["string"], "pattern": "42"}}},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=lambda endpoint, **params: {
            endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]})
        self.folder = self.root / "course-1"

    def test_pull_saves_all_lessons_and_course_info(self):
        result = sw.pull_course(self.client, 1)
        self.assertEqual(result["saved"], ["1.1", "1.2"])
        two = next(self.folder.glob("*/01.02-lesson-2000.md")).read_text(encoding="utf-8")
        self.assertIn("```yaml stepik\ntype: string", two)
        info = (self.folder / "COURSE_INFO.md").read_text(encoding="utf-8")
        self.assertIn("Авторы: Автор", info)
        self.assertIn("Длинное **описание**", info)

    def test_pull_keeps_existing_files_and_adds_new_lessons(self):
        sw.cache_lesson(self.client, 1, "1.1")
        path = next(self.folder.glob("*/01.01-lesson-1000.md"))
        path.write_text("local edits", encoding="utf-8")
        self.data["units"].append({"id": 300, "lesson": 3000, "section": 10, "position": 3})
        self.data["sections"][0]["units"].append(300)
        self.data["lessons"].append({"id": 3000, "title": "Three", "steps": []})
        result = sw.pull_course(self.client, 1)
        self.assertEqual(result["existing"], ["1.1"])
        self.assertEqual(result["saved"], ["1.2", "1.3"])
        self.assertEqual(path.read_text(encoding="utf-8"), "local edits")

    def test_pull_keeps_paths_after_section_rename(self):
        sw.pull_course(self.client, 1)
        self.data["sections"][0]["title"] = "Renamed"
        self.data["units"][1]["position"], self.data["units"][0]["position"] = 1, 2
        result = sw.pull_course(self.client, 1)
        self.assertEqual(result["moved_on_stepik"], ["1.1", "1.2"])
        self.assertEqual(len(list(self.folder.glob("*/*.md"))), 2)
        self.assertFalse(any("Renamed" in path.name for path in self.folder.iterdir()))
