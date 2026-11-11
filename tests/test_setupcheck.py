import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import server, setupcheck
from stepik_mcp.client import StepikError

SECRET = "very-secret-value-123"


class SetupCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        self.client = Mock()
        self.client.get.return_value = {"users": [{"full_name": "Автор", "id": 7}]}

    def run_check(self, **env):
        values = {"COURSE_WORKSPACE_ROOT": str(self.root), **env}
        with patch.dict(os.environ, values, clear=True):
            return setupcheck.setup_check(self.client)

    def test_missing_keys_are_reported_without_calling_stepik(self):
        result = self.run_check()
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["keys"], {"STEPIK_CLIENT_ID": "не задан", "STEPIK_CLIENT_SECRET": "не задан"})
        self.assertEqual(result["auth"]["status"], "not_checked")
        self.client.get.assert_not_called()
        self.assertTrue(any("ALLOWED_WRITE_COURSES" in problem for problem in result["problems"]))
        self.assertTrue(any("скопируй" in step for step in result["next"]), "Suggests moving the old folder")

    def test_ready_setup_lists_courses_and_never_shows_the_secret(self):
        course = self.root / "course-5"
        (course / "sessions").mkdir(parents=True)
        (course / "m").mkdir()
        (course / "m" / "a.md").write_text("x", encoding="utf-8")
        (course / ".course.json").write_text(json.dumps({"course_id": 5, "title": "Курс", "lessons": {
            "1.1": {"path": "m/a.md"}, "1.2": {"path": "m/b.md"}}}), encoding="utf-8")
        (course / "COURSE_LOGIC.md").write_text("# map", encoding="utf-8")
        (course / "sessions" / "2026-11-05-handoff.md").write_text("h", encoding="utf-8")

        result = self.run_check(STEPIK_CLIENT_ID="id", STEPIK_CLIENT_SECRET=SECRET, ALLOWED_WRITE_COURSES="*")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["auth"], {"status": "ok", "account": "Автор", "user_id": 7})
        self.assertEqual(result["workspace"]["courses"], [{
            "course_id": 5, "title": "Курс", "lessons": 2, "downloaded": 1, "course_logic": True,
            "last_handoff": "sessions/2026-11-05-handoff.md"}])
        self.assertTrue(result["publishing"]["enabled"])
        self.assertIn("stepik_start", result["next"][0])
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))

    def test_failed_auth_is_a_problem(self):
        self.client.get.side_effect = StepikError("HTTP 401: invalid_client", status=401)
        result = self.run_check(STEPIK_CLIENT_ID="id", STEPIK_CLIENT_SECRET=SECRET, ALLOWED_WRITE_COURSES="*")
        self.assertEqual(result["auth"]["status"], "error")
        self.assertEqual(result["auth"]["http_status"], 401)
        self.assertTrue(any("Авторизация" in problem for problem in result["problems"]))

    def test_config_missing_error_points_to_setup_check(self):
        with patch.dict(os.environ, {}, clear=True):
            answer = json.loads(server.stepik_whoami())
        self.assertEqual(answer["code"], "CONFIG_MISSING")
        self.assertIn("stepik_setup_check", answer["message"])


if __name__ == "__main__":
    unittest.main()
