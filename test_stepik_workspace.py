import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import stepik_bridge as sb
import stepik_workspace as sw


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=r"C:\Temp\opencode")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [20, 10]}],
            "sections": [
                {"id": 10, "title": 'CON / ../ : | * ? .', "position": 1, "units": [100]},
                {"id": 20, "title": "Second module", "position": 2, "units": [200]},
            ],
            "units": [
                {"id": 100, "lesson": 1000, "section": 10, "position": 1},
                {"id": 200, "lesson": 2000, "section": 20, "position": 1},
            ],
            "lessons": [
                {"id": 1000, "title": "Lesson one", "steps": [11, 12]},
                {"id": 2000, "title": "Lesson two", "steps": [21]},
            ],
            "step-sources": [
                {"id": 12, "lesson": 1000, "block": {"name": "code", "text": "<p>SPECIAL_BODY</p>",
                    "source": {"code": "checker as string"},
                    "options": {"code_templates": {"python3": "print(input())"}, "samples": [["1", "1"]]}}},
                {"id": 11, "lesson": 1000, "block": {"name": "text", "text": "<p>Theory</p>"}},
                {"id": 21, "lesson": 2000, "block": {"name": "text", "text": "<p>Other</p>"}},
            ],
        }
        self.client = sb.StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)

    def get(self, endpoint, **params):
        self.assertIn(endpoint, self.data, "Unexpected endpoint (analytics forbidden)")
        self.assertEqual(set(params), {"ids"})
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def load(self, position="1.1"):
        return sw.cache_lesson(self.client, 1, position)

    def test_first_load_creates_all_modules_but_only_requested_lesson(self):
        result = self.load()
        folder = self.root / "course-1"
        manifest = json.loads((folder / ".course.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["module_count"], 2)
        for module in manifest["modules"]:
            self.assertTrue((folder / module["path"]).is_dir())
        self.assertEqual(len(list(folder.rglob("*-lesson-*.md"))), 1)
        self.assertTrue((folder / "AGENTS.md").is_file())
        self.assertIn("local working file", (folder / "COURSE_MAP.md").read_text(encoding="utf-8"))
        lesson_calls = [c for c in self.client.get.call_args_list if c.args[0] == "lessons"]
        self.assertEqual([c.kwargs["ids"] for c in lesson_calls], [[1000]])

    def test_markdown_contains_all_steps_in_order_and_code(self):
        result = self.load()
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertLess(text.index("<!-- step_id: 11 -->"), text.index("<!-- step_id: 12 -->"))
        self.assertEqual(text.count("<!-- step_id:"), 2)
        self.assertIn("print(input())", text)
        self.assertIn("SPECIAL_BODY", text)
        self.assertIn("lesson_id: 1000", text)
        self.assertIn('status: "working_copy"', text)

    def test_result_does_not_contain_lesson_body(self):
        result = self.load()
        self.assertNotIn("SPECIAL_BODY", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 1500)

    def test_repeat_preserves_edited_file_without_api_requests(self):
        result = self.load()
        path = Path(result["path"])
        path.write_text("My revised lesson\n", encoding="utf-8")
        before = path.stat().st_mtime_ns
        self.client.get.reset_mock()
        self.client.get.side_effect = AssertionError("Cache hit must not call API")
        repeat = self.load()
        self.assertEqual(repeat["status"], "existing")
        self.assertEqual(path.read_text(encoding="utf-8"), "My revised lesson\n")
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.client.get.assert_not_called()

    def test_second_lesson_uses_structure_snapshot(self):
        self.load()
        self.client.get.reset_mock()
        result = self.load("2.1")
        self.assertEqual(result["lesson_id"], 2000)
        self.assertEqual([c.args[0] for c in self.client.get.call_args_list], ["lessons", "step-sources"])

    def test_partial_steps_do_not_leave_a_lesson_and_retry_succeeds(self):
        source = self.data["step-sources"].pop(0)
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(list(self.root.rglob("*-lesson-*.md")), [])
        self.assertEqual(list(self.root.rglob(".stepik-cache.lock")), [])
        self.data["step-sources"].append(source)
        self.assertEqual(self.load()["status"], "saved")

    def test_api_error_does_not_leave_partial_lesson(self):
        original = self.get
        def failing(endpoint, **params):
            if endpoint == "step-sources":
                raise sb.StepikError("Network error")
            return original(endpoint, **params)
        self.client.get.side_effect = failing
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(list(self.root.rglob("*-lesson-*.md")), [])

    def test_wrong_source_lesson_is_rejected(self):
        self.data["step-sources"][0]["lesson"] = 9999
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(list(self.root.rglob("*-lesson-*.md")), [])

    def test_incomplete_structure_is_rejected(self):
        self.data["sections"].pop()
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(list(self.root.rglob("*.md")), [])

    def test_invalid_position_does_not_call_api(self):
        for position in ("../1", "0.1", "1", "1.0", "1.1/../../", "1.1 "):
            with self.subTest(position=position), self.assertRaises(sb.StepikError):
                self.load(position)
        self.client.get.assert_not_called()

    def test_unknown_position_does_not_download_any_lesson(self):
        with self.assertRaises(sb.StepikError):
            self.load("9.9")
        self.assertFalse(any(c.args[0] == "lessons" for c in self.client.get.call_args_list))

    def test_unsafe_manifest_path_is_rejected(self):
        self.load()
        index = self.root / "course-1" / ".course.json"
        manifest = json.loads(index.read_text(encoding="utf-8"))
        for unsafe in ("../../outside.md", r"C:\outside.md", r"\outside.md", "file.md:stream"):
            with self.subTest(path=unsafe):
                manifest["lessons"]["1.1"]["path"] = unsafe
                index.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(sb.StepikError):
                    self.load()

    def test_corrupt_manifest_is_not_replaced(self):
        self.load()
        index = self.root / "course-1" / ".course.json"
        index.write_text("invalid json", encoding="utf-8")
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(index.read_text(encoding="utf-8"), "invalid json")

    def test_atomic_write_never_replaces_existing_file(self):
        path = Path(self.temporary.name) / "existing.md"
        path.write_text("Local edits", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            sw._atomic_write(path, "Remote text")
        self.assertEqual(path.read_text(encoding="utf-8"), "Local edits")
        self.assertEqual(list(path.parent.glob(".stepik-*.tmp")), [])

    def test_failed_atomic_publish_cleans_temporary_file(self):
        path = Path(self.temporary.name) / "lesson.md"
        with patch.object(sw.os, "link", side_effect=OSError("Disk error")):
            with self.assertRaises(OSError):
                sw._atomic_write(path, "Remote text")
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.glob(".stepik-*.tmp")), [])

    def test_initialization_failure_is_recoverable(self):
        original = sw._atomic_write
        def failing(path, text, **kwargs):
            if path.name == "AGENTS.md":
                raise OSError("Disk error")
            original(path, text, **kwargs)
        with patch.object(sw, "_atomic_write", side_effect=failing):
            with self.assertRaises(OSError):
                self.load()
        self.assertTrue((self.root / "course-1" / ".course.json").is_file())
        self.assertEqual(self.load()["status"], "saved")

    def test_cache_hit_repairs_missing_map_without_changing_lesson(self):
        result = self.load()
        folder = self.root / "course-1"
        (folder / "COURSE_MAP.md").unlink()
        self.client.get.side_effect = AssertionError("Unexpected API request")
        before = Path(result["path"]).read_bytes()
        self.assertEqual(self.load()["status"], "existing")
        self.assertTrue((folder / "COURSE_MAP.md").exists())
        self.assertEqual(Path(result["path"]).read_bytes(), before)

    def test_busy_cache_is_not_modified(self):
        lock = self.root / "course-1" / ".stepik-cache.lock"
        lock.mkdir(parents=True)
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertTrue(lock.is_dir())
        self.client.get.assert_not_called()

    def test_unmanaged_course_folder_is_not_adopted(self):
        folder = self.root / "course-1"
        folder.mkdir(parents=True)
        existing = folder / "notes.md"
        existing.write_text("Author notes", encoding="utf-8")
        with self.assertRaises(sb.StepikError):
            self.load()
        self.assertEqual(existing.read_text(encoding="utf-8"), "Author notes")

    def test_root_junction_is_rejected(self):
        with patch.object(Path, "is_junction", return_value=True):
            with self.assertRaises(sb.StepikError):
                self.load()
        self.client.get.assert_not_called()

    def test_mcp_wrapper_returns_small_json_and_supports_offline_hit(self):
        import stepik_mcp
        with patch.object(sb, "CLIENT", self.client):
            result = json.loads(stepik_mcp.stepik_cache_lesson(1, "1.1"))
            self.assertEqual(result["status"], "saved")
            self.client.get.side_effect = AssertionError("Unexpected API request")
            self.assertEqual(json.loads(stepik_mcp.stepik_cache_lesson(1, "1.1"))["status"], "existing")


if __name__ == "__main__":
    unittest.main()
