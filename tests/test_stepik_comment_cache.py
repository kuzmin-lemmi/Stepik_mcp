import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stepik_mcp import workspace as sw


class FakeClient:
    base_url = "https://stepik.org"

    def __init__(self):
        self.calls = []
        self.rows = {
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [10, 20]}],
            "steps": [{"id": 10, "discussion_proxy": "proxy-10"},
                       {"id": 20, "discussion_proxy": "proxy-20"}],
            "discussion-proxies": [{"id": "proxy-10", "discussions": [1]},
                                   {"id": "proxy-20", "discussions": []}],
            "comments": [{"id": 1, "target": 10, "parent": None,
                          "replies": [2], "user": 7, "text": "First"},
                         {"id": 2, "target": 10, "parent": 1,
                          "replies": [], "user": 7, "text": "Reply"}],
            "users": [{"id": 7, "full_name": "Author"}],
        }

    def by_ids(self, endpoint, ids, key=None):
        self.calls.append((endpoint, tuple(ids)))
        return [row for row in self.rows[endpoint] if row["id"] in ids]


class CommentCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Courses"
        self.folder = self.root / "course-1"
        module = self.folder / "01-module-10"
        module.mkdir(parents=True)
        self.lesson_path = module / "01.01-lesson-1000.md"
        self.lesson_path.write_text("Local lesson edits\n", encoding="utf-8")
        manifest = {
            "version": 1, "course_id": 1, "modules": [
                {"section_id": 10, "position": 1, "title": "Module", "path": "01-module-10"}
            ],
            "lessons": {"1.1": {"lesson_id": 1000, "unit_id": 100,
                                  "section_id": 10, "path": "01-module-10/01.01-lesson-1000.md"}},
        }
        (self.folder / ".course.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.client = FakeClient()
        self.environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_saves_comments_separately_with_replies(self):
        result = sw.cache_lesson_comments(self.client, 1, "1.1")
        path = Path(result["path"])
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["comment_count"], 2)
        self.assertIn("comment 1", path.read_text(encoding="utf-8"))
        self.assertIn("comment 2", path.read_text(encoding="utf-8"))
        self.assertEqual(self.lesson_path.read_text(encoding="utf-8"), "Local lesson edits\n")
        self.assertNotIn("solutions", path.read_text(encoding="utf-8"))

    def test_existing_comments_are_preserved_without_api_requests(self):
        result = sw.cache_lesson_comments(self.client, 1, "1.1")
        path = Path(result["path"])
        path.write_text("My comment notes\n", encoding="utf-8")
        self.client.calls.clear()
        self.client.by_ids = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API call"))
        repeat = sw.cache_lesson_comments(self.client, 1, "1.1")
        self.assertEqual(repeat["status"], "existing")
        self.assertEqual(path.read_text(encoding="utf-8"), "My comment notes\n")

    def test_no_comments_still_creates_a_meaningful_file(self):
        self.client.rows["discussion-proxies"][0]["discussions"] = []
        result = sw.cache_lesson_comments(self.client, 1, "1.1")
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertEqual(result["comment_count"], 0)
        self.assertIn("Комментариев в основном обсуждении урока нет", text)


if __name__ == "__main__":
    unittest.main()
