import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import moderation, workspace as sw
from stepik_mcp.client import StepikClient, StepikError

ME = 7


class DeleteCommentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root), "ALLOWED_WRITE_COURSES": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10]}],
            "sections": [{"id": 10, "course": 1, "title": "M", "position": 2, "units": [100, 200]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1},
                      {"id": 200, "lesson": 2000, "section": 10, "position": 2}],
            "lessons": [{"id": 1000, "title": "L1", "steps": [11, 12]}, {"id": 2000, "title": "L2", "steps": [21]}],
            "step-sources": [{"id": 11, "lesson": 1000, "block": {"name": "text", "text": "x"}}],
            "comments": [
                {"id": 1, "user": ME, "target": 11, "text": "<p>!</p>", "reply_count": 0, "parent": None},
                {"id": 2, "user": ME, "target": 21, "text": " ! ", "reply_count": 0, "parent": None},
                {"id": 3, "user": ME, "target": 12, "text": "<p>!</p>", "reply_count": 2, "parent": None},
                {"id": 4, "user": 8, "target": 12, "text": "<p>!</p>", "reply_count": 0, "parent": None},
                {"id": 5, "user": ME, "target": 12, "text": "<p>Спасибо за вопрос!</p>", "reply_count": 0},
                {"id": 6, "user": ME, "target": 999, "text": "<p>!</p>", "reply_count": 0},  # шаг другого курса
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.delete = Mock(side_effect=self.delete)

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": ME}]}
        rows = self.data[endpoint]
        if "ids" in params:
            return {endpoint: [row for row in rows if row["id"] in params["ids"]]}
        self.assertEqual((endpoint, params.get("course")), ("comments", 1))
        return {endpoint: rows, "meta": {"has_next": False}}

    def delete(self, endpoint):
        cid = int(endpoint.rsplit("/", 1)[1])
        next(row for row in self.data["comments"] if row["id"] == cid)["is_deleted"] = True
        return {}

    def test_plan_selects_own_exact_matches_without_replies(self):
        plan = moderation.plan_delete_comments(self.client, 1, text="!")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["confirm_count"], 2)
        self.assertEqual(plan["by_lesson"], {"2.1": 1, "2.2": 1})
        self.assertEqual(plan["skipped"], [{"id": 3, "position": "2.1.2", "reason": "есть ответы (2)"}])
        listing = Path(plan["list_path"]).read_text(encoding="utf-8")
        self.assertIn("comment 1 · шаг 2.1.1", listing)
        self.client.delete.assert_not_called()

    def test_apply_requires_exact_count_and_deletes(self):
        plan = moderation.plan_delete_comments(self.client, 1, text="!")
        for wrong in (0, 1, 3):
            with self.subTest(wrong), self.assertRaisesRegex(StepikError, "DELETE_NOT_CONFIRMED"):
                moderation.apply_delete_comments(self.client, 1, plan["plan_id"], wrong)
        self.client.delete.assert_not_called()
        result = moderation.apply_delete_comments(self.client, 1, plan["plan_id"], 2)
        self.assertEqual((result["status"], result["deleted"]), ("deleted", 2))
        self.assertEqual([call.args[0] for call in self.client.delete.call_args_list], ["comments/1", "comments/2"])
        backup = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual([row["id"] for row in backup], [1, 2])
        log = (self.root / "course-1" / "logs" / "stepik-write.log").read_text(encoding="utf-8")
        self.assertEqual(log.count('"delete_comment"'), 2)
        with self.assertRaisesRegex(StepikError, "уже применён"):
            moderation.apply_delete_comments(self.client, 1, plan["plan_id"], 2)

    def test_changed_comment_is_not_deleted(self):
        plan = moderation.plan_delete_comments(self.client, 1, text="!")
        self.data["comments"][0]["text"] = "<p>! важное уточнение</p>"
        self.data["comments"][1]["reply_count"] = 1
        result = moderation.apply_delete_comments(self.client, 1, plan["plan_id"], 2)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual([item["reason"] for item in result["skipped"]], ["изменился после плана", "появились ответы"])
        self.client.delete.assert_not_called()

    def test_filters(self):
        self.assertEqual(moderation.plan_delete_comments(self.client, 1, text="!", lesson_position="2.2")["confirm_count"], 1)
        self.assertEqual(moderation.plan_delete_comments(self.client, 1, contains="СПАСИБО")["confirm_count"], 1)
        other = moderation.plan_delete_comments(self.client, 1, text="!", user_id=8)
        self.assertEqual(other["confirm_count"], 1)
        self.assertIn("другого пользователя", other["warning"])
        self.assertEqual(moderation.plan_delete_comments(self.client, 1, text="!", include_replied=True)["confirm_count"], 3)
        with self.assertRaisesRegex(StepikError, "Нужен фильтр"):
            moderation.plan_delete_comments(self.client, 1)

    def test_all_in_lesson_takes_every_author_replies_and_empty_text(self):
        self.data["comments"] += [
            {"id": 7, "user": 8, "target": 12, "text": "<p>ответ</p>", "reply_count": 0, "parent": 3},
            {"id": 8, "user": 9, "target": 11, "text": '<p><img src="https://x/1.png"></p>', "reply_count": 0},
        ]
        plan = moderation.plan_delete_comments(self.client, 1, lesson_position="2.1", all_in_lesson=True)
        self.assertEqual(plan["confirm_count"], 6)
        self.assertEqual(plan["skipped_count"], 0)
        self.assertEqual(plan["by_lesson"], {"2.1": 6})
        self.assertEqual(plan["by_user"], {ME: 3, 8: 2, 9: 1})
        self.assertIn("всех авторов", plan["warning"])
        result = moderation.apply_delete_comments(self.client, 1, plan["plan_id"], 6)
        self.assertEqual((result["status"], result["deleted"]), ("deleted", 6))
        order = [int(call.args[0].rsplit("/", 1)[1]) for call in self.client.delete.call_args_list]
        self.assertLess(order.index(7), order.index(3))  # сначала ответы, потом корень ветки
        self.assertNotIn(2, order)  # другой урок не тронут

    def test_all_in_lesson_requires_lesson(self):
        with self.assertRaisesRegex(StepikError, "lesson_position"):
            moderation.plan_delete_comments(self.client, 1, all_in_lesson=True)

    def test_course_outside_allowlist(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": "2"}), self.assertRaisesRegex(StepikError, "WRITE_NOT_ALLOWED"):
            moderation.plan_delete_comments(self.client, 1, text="!")

    def test_service_folder_does_not_block_later_download(self):
        moderation.plan_delete_comments(self.client, 1, text="!")
        self.data["step-sources"].append({"id": 12, "lesson": 1000, "block": {"name": "text", "text": "y"}})
        self.assertEqual(sw.cache_lesson(self.client, 1, "2.1")["status"], "saved")


if __name__ == "__main__":
    unittest.main()
