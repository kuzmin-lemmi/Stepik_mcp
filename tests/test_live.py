"""Проверки на настоящем Stepik. По умолчанию пропускаются.

Запуск: STEPIK_LIVE=1 и ключи в .env в корне репозитория (STEPIK_CLIENT_ID,
STEPIK_CLIENT_SECRET). Пишут только в тестовый курс STEPIK_LIVE_COURSE
(по умолчанию 241862) и только в урок STEPIK_LIVE_LESSON (по умолчанию
2676604); каждая правка в конце откатывается к снимку. Папка курса —
временная, рабочие файлы в COURSE_WORKSPACE_ROOT не затрагиваются.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stepik_mcp import publish, refresh, roundtrip, workspace as sw
from stepik_mcp.client import StepikClient

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
LIVE = os.environ.get("STEPIK_LIVE") == "1" and ENV_FILE.is_file()
COURSE = int(os.environ.get("STEPIK_LIVE_COURSE", "241862"))
LESSON = int(os.environ.get("STEPIK_LIVE_LESSON", "2676604"))


def _keys():
    keys = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            keys[name.strip()] = value.strip().strip("'\"")
    return keys


@unittest.skipUnless(LIVE, "живые проверки: STEPIK_LIVE=1 и .env с ключами Stepik")
class LiveStepikTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        keys = _keys()
        cls.client = StepikClient(keys["STEPIK_CLIENT_ID"], keys["STEPIK_CLIENT_SECRET"])
        structure = sw.course_structure(cls.client, COURSE)
        cls.position = next(key for key, item in structure["lessons"].items() if item["lesson_id"] == LESSON)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root_a = Path(self.temporary.name) / "a"
        self.root_b = Path(self.temporary.name) / "b"

    def env(self, root):
        return patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(root), "ALLOWED_WRITE_COURSES": str(COURSE)})

    def lesson_file(self, root):
        manifest = json.loads((root / f"course-{COURSE}" / ".course.json").read_text(encoding="utf-8"))
        return root / f"course-{COURSE}" / manifest["lessons"][self.position]["path"]

    def remote_texts(self):
        lesson = self.client.by_ids("lessons", [LESSON])[0]
        return {row["id"]: row["block"].get("text", "") for row in self.client.by_ids("step-sources", lesson["steps"])}

    def test_authorization(self):
        user = self.client.get("stepics/1")["users"][0]
        self.assertTrue(user.get("id"))

    def test_round_trip_of_test_course(self):
        course = self.client.by_ids("courses", [COURSE])
        sections = self.client.by_ids("sections", course[0]["sections"])
        units = self.client.by_ids("units", [unit for section in sections for unit in section["units"]])
        lessons = self.client.by_ids("lessons", [unit["lesson"] for unit in units])
        sources = self.client.by_ids("step-sources", [sid for lesson in lessons for sid in lesson["steps"]])
        report = roundtrip.check_sources(sources)
        self.assertEqual(report["failed"], 0, report["failures"])

    def test_edit_publish_refresh_rollback(self):
        before = self.remote_texts()
        marker = "Живая проверка " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.env(self.root_a):
            sw.cache_lesson(self.client, COURSE, self.position)
        with self.env(self.root_b):
            sw.cache_lesson(self.client, COURSE, self.position)
            path = self.lesson_file(self.root_b)
            text = path.read_text(encoding="utf-8")
            first = publish.parse_lesson(text)[0]["body"]
            path.write_text(text.replace(first, f"{first}\n\n{marker}", 1), encoding="utf-8", newline="\n")
            plan = publish.plan_push(self.client, COURSE, self.position)
            self.assertEqual([change["status"] for change in plan["changes"]], ["MODIFIED"])
            self.assertNotIn("будет потеряно", plan["changes"][0])
            self.addCleanup(self.rollback)
            self.assertEqual(publish.apply_push(self.client, COURSE, plan["plan_id"])["status"], "published")
        self.assertTrue(any(marker in text for text in self.remote_texts().values()))

        with self.env(self.root_a):
            result = refresh.refresh_lesson(self.client, COURSE, self.position, apply=True)
            self.assertEqual([change["status"] for change in result["changes"]], ["UPDATED"])
            self.assertIn(marker, self.lesson_file(self.root_a).read_text(encoding="utf-8"))

        self.rollback()
        self.assertEqual(self.remote_texts(), before)

    def rollback(self):
        """Возвращает урок к снимку перед последней публикацией из копии b."""
        if getattr(self, "_rolled_back", False):
            return
        with self.env(self.root_b):
            plan = publish.plan_rollback(self.client, COURSE, self.position)
            if plan["status"] == "planned":
                publish.apply_push(self.client, COURSE, plan["plan_id"])
        self._rolled_back = True
