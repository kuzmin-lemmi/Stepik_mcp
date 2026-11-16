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

    def test_modules_create_move_delete(self):
        """Два временных модуля и пустой урок: создание, перенос, переименование, удаление."""
        def modules():
            return [(sid, title, pos, len(units)) for sid, title, pos, units in publish._course_sections(self.client, COURSE)]

        def apply(plan, **kw):
            return publish.apply_push(self.client, COURSE, plan["plan_id"], **kw)

        before = modules()
        n = len(before)
        created = {"sections": [], "lesson": None}
        self.addCleanup(self._remove_live_objects, created)
        with self.env(self.root_a):
            sw.cache_lesson(self.client, COURSE, self.position)
            folder = self.root_a / f"course-{COURSE}"
            a = apply(publish.plan_create_module(self.client, COURSE, "MCP live A"))
            created["sections"].append(a["section_id"])
            (folder / a["path"] / "draft.md").write_text("# MCP live\n\n<!-- new_step -->\nТекст\n", encoding="utf-8")
            lesson = apply(publish.plan_create_lesson(self.client, COURSE, f"{a['path']}/draft.md", n + 1))
            created["lesson"] = lesson["lesson_id"]
            b = apply(publish.plan_create_module(self.client, COURSE, "MCP live B", module_position=n + 1))
            created["sections"].append(b["section_id"])
            self.assertEqual([m[1] for m in modules()[-2:]], ["MCP live B", "MCP live A"])
            self.assertEqual([m[2] for m in modules()], list(range(1, n + 3)))
            moved = apply(publish.plan_move_lesson(self.client, COURSE, f"{n + 2}.1", module_position=n + 1))
            self.assertEqual(moved["lesson_position"], f"{n + 1}.1")
            apply(publish.plan_rename_module(self.client, COURSE, n + 1, "MCP live B2"))
            apply(publish.plan_move_module(self.client, COURSE, n + 1, n + 2))
            self.assertEqual([m[1:] for m in modules()[-2:]], [("MCP live A", n + 1, 0), ("MCP live B2", n + 2, 1)])
            apply(publish.plan_delete_lesson(self.client, COURSE, f"{n + 2}.1"), confirm_delete=[created["lesson"]])
            created["lesson"] = None
            for position in (n + 2, n + 1):
                plan = publish.plan_delete_module(self.client, COURSE, position)
                apply(plan, confirm_delete=[plan["section_id"]])
                created["sections"].remove(plan["section_id"])
        self.assertEqual(modules(), before)

    def _remove_live_objects(self, created):
        if created["lesson"]:
            for unit in self.client.get("units", lesson=created["lesson"]).get("units", []):
                self.client.delete(f"units/{unit['id']}")
            self.client.delete(f"lessons/{created['lesson']}")
        for sid in created["sections"]:
            for uid in (self.client.by_ids("sections", [sid]) or [{}])[0].get("units") or []:
                self.client.delete(f"units/{uid}")
            self.client.delete(f"sections/{sid}")

    def rollback(self):
        """Возвращает урок к снимку перед последней публикацией из копии b."""
        if getattr(self, "_rolled_back", False):
            return
        with self.env(self.root_b):
            plan = publish.plan_rollback(self.client, COURSE, self.position)
            if plan["status"] == "planned":
                publish.apply_push(self.client, COURSE, plan["plan_id"])
        self._rolled_back = True
