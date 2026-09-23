import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import stepik_workspace as sw


class TextCourseClient:
    base_url = "https://stepik.org"

    def __init__(self):
        self.rows = {
            "courses": [{"id": 7, "title": "Course", "sections": [70]}],
            "sections": [{"id": 70, "title": "Module", "position": 1, "units": [700, 701]}],
            "units": [{"id": 700, "lesson": 7000, "section": 70, "position": 1},
                      {"id": 701, "lesson": 7001, "section": 70, "position": 2}],
            "lessons": [{"id": 7000, "title": "Text lesson", "steps": [1, 2]},
                        {"id": 7001, "title": "Task lesson", "steps": [3]}],
            "step-sources": [
                {"id": 1, "lesson": 7000, "block": {"name": "text", "text": "<p>Keep me</p>"}},
                {"id": 2, "lesson": 7000, "block": {"name": "code", "text": "<p>Do not keep me</p>"}},
                {"id": 3, "lesson": 7001, "block": {"name": "choice", "text": "<p>Also omit</p>"}},
            ],
        }

    def by_ids(self, endpoint, ids, key=None):
        return [row for row in self.rows[endpoint] if row["id"] in ids]


class TextCourseCacheTests(unittest.TestCase):
    def test_all_lessons_are_saved_but_only_text_steps_are_rendered(self):
        with tempfile.TemporaryDirectory(dir=r"C:\Temp\opencode") as temporary:
            root = Path(temporary) / "Courses"
            with patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(root)}):
                result = sw.cache_course_text_lessons(TextCourseClient(), 7)
            self.assertEqual(result["lesson_count"], 2)
            self.assertEqual(result["saved_count"], 2)
            self.assertEqual(result["text_step_count"], 1)
            files = sorted(root.rglob("*-lesson-*.md"))
            self.assertEqual(len(files), 2)
            text = "\n".join(path.read_text(encoding="utf-8") for path in files)
            self.assertIn("Keep me", text)
            self.assertNotIn("Do not keep me", text)
            self.assertNotIn("Also omit", text)
            self.assertIn('content_mode: "text_only"', text)

    def test_existing_text_only_files_are_not_overwritten(self):
        client = TextCourseClient()
        with tempfile.TemporaryDirectory(dir=r"C:\Temp\opencode") as temporary:
            root = Path(temporary) / "Courses"
            with patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(root)}):
                sw.cache_course_text_lessons(client, 7)
                target = next(root.rglob("*-lesson-7000.md"))
                target.write_text("Local edit", encoding="utf-8")
                result = sw.cache_course_text_lessons(client, 7)
            self.assertEqual(result["existing_count"], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "Local edit")


if __name__ == "__main__":
    unittest.main()
