import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from stepik_mcp import server, workspace as sw
from stepik_mcp.client import StepikError


class ErrorFormatTests(unittest.TestCase):
    def test_code_from_message_prefix(self):
        error = json.loads(server._error(StepikError("WRITE_NOT_ALLOWED: курса 5 нет в списке", status=403)))
        self.assertEqual(error, {"status": "error", "code": "WRITE_NOT_ALLOWED", "message": "курса 5 нет в списке"})

    def test_code_from_http_status(self):
        self.assertEqual(json.loads(server._error(StepikError("Урок 1 не найден", status=404)))["code"], "NOT_FOUND")
        self.assertEqual(json.loads(server._error(StepikError("сбой")))["code"], "STEPIK_ERROR")
        self.assertEqual(json.loads(server._error(ValueError("x")))["code"], "INTERNAL_ERROR")

    def test_missing_credentials(self):
        with patch.dict(os.environ, {"STEPIK_CLIENT_ID": "", "STEPIK_CLIENT_SECRET": ""}):
            error = json.loads(server.stepik_whoami())
        self.assertEqual(error["code"], "CONFIG_MISSING")
        self.assertIn("STEPIK_CLIENT_ID", error["message"])

    def test_local_tool_error_is_structured(self):
        with patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": "relative/path"}):
            error = json.loads(server.stepik_cache_lesson(1, "1.1"))
        self.assertEqual(error["status"], "error")
        self.assertIn("COURSE_WORKSPACE_ROOT", error["message"])


class WorkspaceRootTests(unittest.TestCase):
    def test_default_root_depends_on_os(self):
        with patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": ""}):
            root = sw.workspace_root()
        if os.name == "nt":
            self.assertEqual(root, Path(r"C:\Courses"))
        else:
            self.assertEqual(root, Path.home() / "Courses")

    def test_env_overrides_default(self):
        target = Path.home() / "SomeCourses"
        with patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(target)}):
            self.assertEqual(sw.workspace_root(), target)
