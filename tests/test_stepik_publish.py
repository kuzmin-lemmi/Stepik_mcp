import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, server, steps, workspace as sw
from stepik_mcp.client import StepikClient, StepikError
from stepik_mcp.render import html_to_md, md_to_html


class MarkdownToHtmlTests(unittest.TestCase):
    def test_supported_markup(self):
        result = md_to_html(
            "## Заголовок\n\nТекст с **жирным**, *курсивом* и `a < b`.\n\n"
            "- один\n- два\n  - вложенный\n\n1. первый\n2. второй\n\n"
            "> цитата\n\n```python\nif a < b:\n    print(a)\n```\n\n![схема](https://x/y.png)"
        )
        self.assertIn("<h2>Заголовок</h2>", result)
        self.assertIn("<strong>жирным</strong>", result)
        self.assertIn("<em>курсивом</em>", result)
        self.assertIn("<code>a &lt; b</code>", result)
        self.assertIn("<ul><li>один</li><li>два<ul><li>вложенный</li></ul></li></ul>", result)
        self.assertIn("<ol><li>первый</li><li>второй</li></ol>", result)
        self.assertIn("<blockquote><p>цитата</p></blockquote>", result)
        self.assertIn('<pre><code class="language-python">if a &lt; b:\n    print(a)</code></pre>', result)
        self.assertIn('<img alt="схема" src="https://x/y.png">', result)

    def test_round_trip_through_loader_is_stable(self):
        markdown = ("## Тема\n\nАбзац с **жирным** и `кодом`.\n\n- раз\n- два\n\n"
                    "```python\nprint(1)\n```\n\n![подпись](https://stepik.org/media/a.png)")
        self.assertEqual(html_to_md(md_to_html(markdown)), markdown)

    def test_blockquote_with_line_break_survives_round_trip(self):
        # Загрузчик пишет строку после <br> без «>»: она продолжает цитату, а не начинает абзац.
        markdown = html_to_md("<blockquote><strong>🔑 Итог:</strong><br />\nтекст цитаты</blockquote><p>после</p>")
        self.assertEqual(md_to_html(markdown),
                         "<blockquote><p><strong>🔑 Итог:</strong><br>\nтекст цитаты</p></blockquote>\n<p>после</p>")
        self.assertEqual(md_to_html("> **A:**  \n> b\n\nc"),
                         "<blockquote><p><strong>A:</strong><br>\nb</p></blockquote>\n<p>c</p>")

    def test_html_block_passes_through(self):
        self.assertEqual(md_to_html("<table><tr><td>1</td></tr></table>"),
                         "<table><tr><td>1</td></tr></table>")

    def test_raw_html_in_text_is_escaped(self):
        self.assertEqual(md_to_html("a <script> b"), "<p>a &lt;script&gt; b</p>")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("STEPIK_MCP_TEST_TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Courses"
        environment = patch.dict(os.environ, {"COURSE_WORKSPACE_ROOT": str(self.root),
                                              "ALLOWED_WRITE_COURSES": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.data = {
            "courses": [{"id": 1, "title": "Course", "sections": [10], "authors": [7], "instructors": [7], "owner": 7}],
            "sections": [{"id": 10, "course": 1, "title": "Module", "position": 1, "units": [100]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1}],
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [11, 12, 13]}],
            "step-sources": [
                {"id": 11, "lesson": 1000, "position": 1, "cost": 0,
                 "block": {"name": "text", "text": "<p>Theory</p>"}},
                {"id": 12, "lesson": 1000, "position": 2, "cost": 1,
                 "block": {"name": "string", "text": "<p>Question</p>",
                           "source": {**steps.DEFAULT_SOURCES["string"], "pattern": "42"}}},
                {"id": 13, "lesson": 1000, "position": 3, "cost": 0,
                 "block": {"name": "text", "text": '<p>See <a href="https://x">docs</a></p>'}},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = Mock(side_effect=self.post)
        self.client.put = Mock(side_effect=self.put)
        self.client.delete = Mock(side_effect=self.delete)
        self.client.upload = Mock(return_value={"attachments": [{"id": 5, "file": "/media/attachments/lesson/1000/s.png"}]})
        # Настоящий Stepik при вставке в середину урока не сдвигает следующие шаги:
        # у нового и старого шага оказывается одна позиция, и новый встаёт после старого.
        self.shift_on_insert = True
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.lesson = next(self.folder.glob("*/01.01-lesson-1000.md"))

    def get(self, endpoint, **params):
        if endpoint == "stepics/1":
            return {"users": [{"id": 7}]}
        rows = self.data[endpoint]
        if "ids" in params:
            return {endpoint: [row for row in rows if row["id"] in params["ids"]]}
        self.assertEqual((endpoint, set(params)), ("units", {"lesson"}))
        return {endpoint: [row for row in rows if row["lesson"] == params["lesson"]]}

    def post(self, endpoint, payload):
        self.assertEqual(endpoint, "step-sources")
        new_id = max([98] + [row["id"] for row in self.data["step-sources"]]) + 1
        created = {"id": new_id, **payload["stepSource"]}
        order = self.data["lessons"][0]["steps"]
        if self.shift_on_insert:
            for row in self.data["step-sources"]:
                if row.get("position", 0) >= created["position"]:
                    row["position"] += 1
            order.insert(created["position"] - 1, new_id)
        else:
            after = [index for index, sid in enumerate(order) if self.remote(sid).get("position", 0) <= created["position"]]
            order.insert(after[-1] + 1 if after else 0, new_id)
        self.data["step-sources"].append(created)
        return {"step-sources": [created]}

    def put(self, endpoint, payload):
        row = self.remote(int(endpoint.rsplit("/", 1)[1]))
        row["block"] = payload["stepSource"]["block"]
        if payload["stepSource"].get("position") not in (None, row.get("position")):
            row["position"] = payload["stepSource"]["position"]
            order = self.data["lessons"][0]["steps"]
            order.sort(key=lambda sid: self.remote(sid).get("position", 0))
        return {"step-sources": [payload["stepSource"]]}

    def delete(self, endpoint):
        sid = int(endpoint.rsplit("/", 1)[1])
        self.data["step-sources"] = [row for row in self.data["step-sources"] if row["id"] != sid]
        self.data["lessons"][0]["steps"].remove(sid)
        return {}

    def remote(self, sid):
        return next(row for row in self.data["step-sources"] if row["id"] == sid)

    def edit(self, old, new):
        text = self.lesson.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.lesson.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    def assert_no_writes(self):
        self.client.post.assert_not_called()
        self.client.put.assert_not_called()
        self.client.upload.assert_not_called()
        self.client.delete.assert_not_called()

    def test_course_outside_allowlist_is_refused(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": "241862"}):
            with self.assertRaisesRegex(StepikError, "WRITE_NOT_ALLOWED"):
                publish.plan_push(self.client, 1, "1.1")
            with self.assertRaisesRegex(StepikError, "WRITE_NOT_ALLOWED"):
                publish.apply_push(self.client, 1, "20260101-000000-abcdef")
        self.assert_no_writes()

    def test_any_course_means_own_courses_only(self):
        self.edit("Theory\n", "Theory 2\n")
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": "*"}):
            self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "planned")
            self.data["courses"][0].update(authors=[8], instructors=[8], owner=8)
            with self.assertRaisesRegex(StepikError, "не автор"):
                publish.plan_push(self.client, 1, "1.1")
        self.assert_no_writes()

    def test_download_saves_step_sources_without_statistics(self):
        saved = json.loads((self.folder / ".stepik" / "steps" / "11.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["block"], {"name": "text", "text": "<p>Theory</p>"})
        self.assertIn(f"format: {steps.FORMAT}", self.lesson.read_text(encoding="utf-8"))

    def test_statistics_are_not_saved(self):
        self.data["step-sources"].append({"id": 31, "lesson": 1000, "viewed_by": 5, "passed_by": 2,
                                          "block": {"name": "text", "text": "x"}})
        sw.save_step_sources(self.folder, [self.remote(31)])
        saved = json.loads((self.folder / ".stepik" / "steps" / "31.json").read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"id", "lesson", "block"})

    def test_remote_edit_after_download_is_not_overwritten(self):
        self.remote(11)["block"]["text"] = "<p>Edited on Stepik</p>"
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")
        self.edit("Theory\n", "Theory local\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertIn("и на Stepik", result["skipped"][0]["reason"])
        self.assert_no_writes()

    def test_published_version_becomes_new_base(self):
        self.edit("Theory\n", "Theory 2\n")
        publish.apply_push(self.client, 1, publish.plan_push(self.client, 1, "1.1")["plan_id"])
        saved = json.loads((self.folder / ".stepik" / "steps" / "11.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["block"]["text"], "<p>Theory 2</p>")
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_string_answer_change_is_published(self):
        self.edit("answer: '42'", "answer: '43'")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["changes"], [{"step": "step_id 12, позиция 2", "status": "MODIFIED",
                                            "type": "string", "поля": ["source.pattern"]}])
        self.assertNotIn("confirm_steps", plan)
        publish.apply_push(self.client, 1, plan["plan_id"])
        sent = self.client.put.call_args.args[1]["stepSource"]
        self.assertEqual(sent["block"]["source"], {**steps.DEFAULT_SOURCES["string"], "pattern": "43"})
        self.assertEqual(sent["cost"], 1)
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_learners_who_solved_step_need_explicit_consent(self):
        self.remote(12)["passed_by"] = 3
        self.edit("answer: '42'", "answer: '43'")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["confirm_steps"], [12])
        self.assertEqual(plan["changes"][0]["решили учеников"], 3)
        with self.assertRaisesRegex(StepikError, "LEARNER_IMPACT"):
            publish.apply_push(self.client, 1, plan["plan_id"])
        self.assert_no_writes()
        self.assertEqual(publish.apply_push(self.client, 1, plan["plan_id"], confirm_steps=[12])["status"],
                         "published")

    def test_condition_only_change_needs_no_consent(self):
        self.remote(12)["passed_by"] = 3
        self.edit("Question", "Better question")
        self.assertNotIn("confirm_steps", publish.plan_push(self.client, 1, "1.1"))

    def test_invalid_task_is_not_planned(self):
        self.edit("answer: '42'", "answer: ''")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertIn("answer пустой", result["skipped"][0]["reason"])

    def test_new_choice_step_is_created(self):
        self.edit("Theory\n", "Theory\n\n<!-- new_step -->\n## Тест\n\nСколько будет 2+2?\n\n"
                  "```yaml stepik\ntype: choice\noptions:\n- text: '4'\n  correct: true\n- text: '5'\n```\n")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["changes"][0], {"step": "новый, позиция 2", "status": "NEW", "type": "choice"})
        publish.apply_push(self.client, 1, plan["plan_id"])
        created = self.client.post.call_args.args[1]["stepSource"]
        self.assertEqual((created["position"], created["cost"], created["block"]["name"]), (2, 1, "choice"))
        self.assertEqual(created["block"]["text"], "<p>Сколько будет 2+2?</p>")
        self.assertEqual(created["block"]["source"]["sample_size"], 2)
        self.assertEqual([option["is_correct"] for option in created["block"]["source"]["options"]], [True, False])
        self.assertIn("## Шаг | step_id: 99 | choice", self.lesson.read_text(encoding="utf-8"))
        # В файле осталась короткая запись YAML без show: это не правка.
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_new_invalid_choice_is_skipped(self):
        self.edit("Theory\n", "Theory\n\n<!-- new_step -->\nQ\n\n```yaml stepik\ntype: choice\n"
                  "options:\n- text: a\n```\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertIn("хотя бы два варианта", result["skipped"][0]["reason"])

    def add_code_step(self, solution_code):
        (self.lesson.parent / "solutions").mkdir(exist_ok=True)
        (self.lesson.parent / "solutions" / "sum.py").write_text(solution_code, encoding="utf-8")
        self.edit("Theory\n", "Theory\n\n<!-- new_step -->\nСложите два числа.\n\n```yaml stepik\ntype: code\n"
                  "templates:\n  python3: ''\ntests:\n- input: \"1 2\\n\"\n  output: \"3\\n\"\n"
                  "- input: \"5 5\\n\"\n  output: \"10\\n\"\nsolution: solutions/sum.py\n```\n")

    def test_code_step_with_passing_solution(self):
        self.add_code_step("a, b = map(int, input().split())\nprint(a + b)\n")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["changes"][0]["решение"]["status"], "passed")
        self.assertEqual(plan["changes"][0]["решение"]["passed"], 2)
        publish.apply_push(self.client, 1, plan["plan_id"])
        block = self.client.post.call_args.args[1]["stepSource"]["block"]
        self.assertEqual(block["source"]["test_cases"], [["1 2\n", "3\n"], ["5 5\n", "10\n"]])
        self.assertEqual(block["source"]["templates_data"], "::python3\n")
        self.assertNotIn("solution", json.dumps(block))

    def test_code_step_with_failing_solution_is_skipped(self):
        self.add_code_step("print(0)\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertIn("Эталонное решение не прошло тесты (0/2)", result["skipped"][0]["reason"])

    def test_rollback_restores_snapshot(self):
        self.edit("Theory\n", "Theory 2\n")
        publish.apply_push(self.client, 1, publish.plan_push(self.client, 1, "1.1")["plan_id"])
        self.assertEqual(self.remote(11)["block"]["text"], "<p>Theory 2</p>")
        plan = publish.plan_rollback(self.client, 1, "1.1")
        self.assertEqual(plan["changes"][0]["status"], "RESTORE")
        self.assertIn("-Theory 2", Path(plan["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(publish.apply_push(self.client, 1, plan["plan_id"])["status"], "published")
        self.assertEqual(self.remote(11)["block"]["text"], "<p>Theory</p>")
        self.assertEqual(publish.plan_rollback(self.client, 1, "1.1", plan["snapshot"])["status"], "nothing_to_restore")

    def test_rollback_rejects_foreign_snapshot_names(self):
        for name in ("../x-lesson-1000.json", "2020-lesson-2000.json"):
            with self.subTest(name), self.assertRaises(StepikError):
                publish.plan_rollback(self.client, 1, "1.1", name)

    def test_old_format_task_edit_asks_for_fresh_download(self):
        text = self.lesson.read_text(encoding="utf-8").replace(f"format: {steps.FORMAT}\n", "")
        self.lesson.write_text(text.replace("Question", "Changed"), encoding="utf-8", newline="\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertIn("до появления YAML", next(item for item in result["skipped"] if item.get("step_id") == 12)["reason"])

    def test_old_format_file_with_broken_lists(self):
        self.remote(11)["block"]["text"] = "<ul><li><p>a</p></li></ul>"
        self.remote(11)["update_date"] = "2000-01-01T00:00:00Z"
        (self.folder / ".stepik" / "steps" / "11.json").unlink()
        text = self.lesson.read_text(encoding="utf-8").replace(f"format: {steps.FORMAT}\n", "")
        self.lesson.write_text(text.replace("\nTheory\n", "\n- \n\na\n"), encoding="utf-8", newline="\n")
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")
        self.edit("\na\n", "\nb\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertIn("старой версией", result["skipped"][0]["reason"])

    def test_old_file_with_unclosed_fence_keeps_all_steps(self):
        text = self.lesson.read_text(encoding="utf-8").replace(f"format: {steps.FORMAT}\n", "")
        self.lesson.write_text(text.replace("\nTheory\n", "\nTheory\n\n```\n"), encoding="utf-8", newline="\n")
        self.assertEqual([entry["step_id"] for entry in publish.parse_lesson(self.lesson.read_text(encoding="utf-8"))],
                         [11, 12, 13])

    def test_marker_inside_code_block_is_text_in_new_format(self):
        entries = publish.parse_lesson("---\nformat: 2\n---\n<!-- step_id: 1 -->\n```\n<!-- step_id: 2 -->\n```\n")
        self.assertEqual([entry["step_id"] for entry in entries], [1])

    def test_old_file_without_base_skips_steps_edited_on_stepik_later(self):
        (self.folder / ".stepik" / "steps" / "11.json").unlink()
        self.remote(11)["block"]["text"] = "<p>Theory</p><p>Picture added on Stepik</p>"
        self.remote(11)["update_date"] = "2099-01-01T00:00:00Z"
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertIn("после скачивания", result["skipped"][0]["reason"])
        self.remote(11)["update_date"] = "2000-01-01T00:00:00Z"
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "planned")

    def test_step_deletion_needs_explicit_list(self):
        self.edit("<!-- step_id: 13 -->", "<!-- delete_step: 13 -->")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["changes"], [{"step": "step_id 13, позиция 3", "status": "DELETE", "type": "text"}])
        self.assertEqual(plan["confirm_delete"], [13])
        self.assertIn("- See [docs]", Path(plan["diff_path"]).read_text(encoding="utf-8"))
        for wrong in (None, [], [12], [13, 12]):
            with self.subTest(wrong), self.assertRaisesRegex(StepikError, "DELETE_NOT_CONFIRMED"):
                publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=wrong)
        self.assert_no_writes()
        result = publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[13])
        self.assertEqual(result["status"], "published")
        self.client.delete.assert_called_once_with("step-sources/13")
        text = self.lesson.read_text(encoding="utf-8")
        self.assertNotIn("step_id: 13", text)
        self.assertNotIn("See [docs]", text)
        self.assertIn("<!-- step_id: 12 -->", text)
        self.assertFalse((self.folder / ".stepik" / "steps" / "13.json").exists())
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_deleting_solved_step_also_needs_learner_consent(self):
        self.remote(12)["passed_by"] = 4
        self.edit("<!-- step_id: 12 -->", "<!-- delete_step: 12 -->")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual((plan["confirm_steps"], plan["confirm_delete"]), ([12], [12]))
        with self.assertRaisesRegex(StepikError, "LEARNER_IMPACT"):
            publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[12])
        publish.apply_push(self.client, 1, plan["plan_id"], confirm_steps=[12], confirm_delete=[12])
        self.client.delete.assert_called_once_with("step-sources/12")

    def test_create_runs_before_delete(self):
        self.edit("<!-- step_id: 11 -->", "<!-- delete_step: 11 -->")
        self.edit("<!-- step_id: 12 -->", "<!-- new_step -->\nNew\n\n<!-- step_id: 12 -->")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual([item["status"] for item in plan["changes"]], ["DELETE", "NEW"])
        publish.apply_push(self.client, 1, plan["plan_id"], confirm_delete=[11])
        self.assertEqual(self.client.post.call_args.args[1]["stepSource"]["position"], 2)
        self.assertEqual(self.data["lessons"][0]["steps"], [99, 12, 13])

    def test_already_deleted_step_is_reported(self):
        self.edit("<!-- step_id: 13 -->", "<!-- delete_step: 13 -->")
        self.delete("step-sources/13")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["skipped"], [{"step_id": 13, "reason": "шага уже нет на Stepik"}])

    def test_rollback_recreates_deleted_step(self):
        self.edit("<!-- step_id: 12 -->", "<!-- delete_step: 12 -->")
        publish.apply_push(self.client, 1, publish.plan_push(self.client, 1, "1.1")["plan_id"], confirm_delete=[12])
        plan = publish.plan_rollback(self.client, 1, "1.1")
        self.assertEqual(plan["changes"][0]["status"], "RECREATE")
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        created = self.client.post.call_args.args[1]["stepSource"]
        self.assertEqual((created["position"], created["cost"], created["block"]["name"]), (2, 1, "string"))
        self.assertIn("скачайте урок заново", result["note"])

    def test_format_2_file_with_quotes(self):
        # Файлы формата 2 показывали врезку пустой цитатой с текстом под ней.
        self.remote(11)["block"]["text"] = "<blockquote><p>Итог</p></blockquote>"
        self.remote(11)["update_date"] = "2000-01-01T00:00:00Z"
        (self.folder / ".stepik" / "steps" / "11.json").unlink()
        text = self.lesson.read_text(encoding="utf-8").replace(f"format: {steps.FORMAT}\n", "format: 2\n")
        self.lesson.write_text(text.replace("\nTheory\n", "\n> \n\nИтог\n"), encoding="utf-8", newline="\n")
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")
        self.edit("\nИтог\n", "\nИтог 2\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertIn("врезки", result["skipped"][0]["reason"])
        self.assert_no_writes()

    def test_format_3_file_with_table(self):
        # Файлы формата 3 показывали таблицу текстом ячеек.
        self.remote(11)["block"]["text"] = "<table><tr><td>Итог</td></tr></table>"
        self.remote(11)["update_date"] = "2000-01-01T00:00:00Z"
        (self.folder / ".stepik" / "steps" / "11.json").unlink()
        text = self.lesson.read_text(encoding="utf-8").replace(f"format: {steps.FORMAT}\n", "format: 3\n")
        self.lesson.write_text(text.replace("\nTheory\n", "\nИтог\n"), encoding="utf-8", newline="\n")
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")
        self.edit("\nИтог\n", "\nИтог 2\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertIn("скачайте урок заново", result["skipped"][0]["reason"])
        self.assert_no_writes()

    def test_structure_change_is_reported(self):
        html = "<ul><li><blockquote><p>x</p></blockquote></li></ul>"
        self.assertTrue(publish.structure_changes(html))
        self.assertFalse(publish.structure_changes("<ul><li><p>a</p></li></ul><blockquote><p>b</p></blockquote>"))
        self.assertIn("структура блоков", publish._edit_losses(html)[-1])

    def test_tables_and_details_are_not_lost(self):
        html = ('<table style="width:100%"><tr><td><a href="x">a</a></td></tr></table>'
                '<details open><summary>S</summary><p>b</p></details>')
        self.assertEqual(publish._edit_losses(html), [])
        self.assertTrue(publish._lossy_features("<details><summary class='s'>S</summary></details>"))

    def test_empty_allowlist_by_default(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": ""}):
            self.assertEqual(publish.allowed_write_courses(), set())

    def test_unchanged_lesson_has_nothing_to_push(self):
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assert_no_writes()

    def test_plan_and_apply_text_edit_new_step_and_image(self):
        images = self.lesson.parent / "images"
        images.mkdir()
        (images / "s.png").write_bytes(b"\x89PNG fake")
        self.edit("Theory\n", "Theory, **updated**\n\n![схема](images/s.png)\n\n"
                  "<!-- new_step -->\n## Новый шаг\n\nBrand new step\n")

        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["status"], "planned")
        self.assertEqual([item["status"] for item in plan["changes"]], ["MODIFIED", "NEW"])
        self.assertEqual(plan["changes"][1]["step"], "новый, позиция 2")
        self.assertEqual(plan["images_to_upload"], ["01-Module-10/images/s.png"])
        diff = Path(plan["diff_path"]).read_text(encoding="utf-8")
        self.assertIn("+Theory, **updated**", diff)
        self.assertIn("Brand new step", diff)
        self.assert_no_writes()

        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(result["status"], "published")
        self.client.upload.assert_called_once()
        self.assertEqual(self.client.upload.call_args.args[1], {"lesson": 1000})
        endpoint, payload = self.client.put.call_args.args
        self.assertEqual(endpoint, "step-sources/11")
        sent = payload["stepSource"]
        self.assertEqual(sent["cost"], 0)
        self.assertIn("<strong>updated</strong>", sent["block"]["text"])
        self.assertIn('src="https://stepik.org/media/attachments/lesson/1000/s.png"', sent["block"]["text"])
        created = self.client.post.call_args.args[1]["stepSource"]
        self.assertEqual((created["lesson"], created["position"], created["block"]["name"]), (1000, 2, "text"))

        text = self.lesson.read_text(encoding="utf-8")
        self.assertIn("<!-- step_id: 99 -->", text)
        self.assertNotIn("new_step", text)
        self.assertIn("images/s.png", text)
        self.assertTrue(Path(result["snapshot"]).is_file())
        log = (self.folder / "logs" / "stepik-write.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["operation"] for line in log], ["upload_image", "update", "create"])
        self.assertNotIn("secret", "".join(log).lower())
        with self.assertRaisesRegex(StepikError, "уже применён"):
            publish.apply_push(self.client, 1, plan["plan_id"])

    def test_uploaded_image_is_not_uploaded_again(self):
        images = self.lesson.parent / "images"
        images.mkdir()
        (images / "s.png").write_bytes(b"\x89PNG fake")
        self.edit("Theory\n", "Theory\n\n![схема](images/s.png)\n")
        publish.apply_push(self.client, 1, publish.plan_push(self.client, 1, "1.1")["plan_id"])
        # Stepik now holds the published version; local Markdown keeps the local path.
        self.data["step-sources"][0]["block"]["text"] = self.client.put.call_args.args[1]["stepSource"]["block"]["text"]
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_remote_change_after_plan_blocks_apply(self):
        self.edit("Theory\n", "Theory 2\n")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.data["step-sources"][0]["block"]["text"] = "<p>Edited on Stepik</p>"
        with self.assertRaisesRegex(StepikError, "REMOTE_CHANGED"):
            publish.apply_push(self.client, 1, plan["plan_id"])
        self.assert_no_writes()

    def test_local_change_after_plan_blocks_apply(self):
        self.edit("Theory\n", "Theory 2\n")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.edit("Theory 2\n", "Theory 3\n")
        with self.assertRaisesRegex(StepikError, "LOCAL_CHANGED"):
            publish.apply_push(self.client, 1, plan["plan_id"])
        self.assert_no_writes()

    def test_task_condition_edit_changes_only_text(self):
        self.edit("Question", "Changed question")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["changes"][0]["поля"], ["text"])

    def test_unsupported_task_type_is_skipped(self):
        self.remote(12)["block"] = {"name": "table", "text": "<p>Question</p>", "source": {"rows": []}}
        sw.save_step_sources(self.folder, [self.remote(12)])
        text = self.lesson.read_text(encoding="utf-8")
        start = text.index("<!-- step_id: 12 -->")
        end = text.index("<!-- step_id: 13 -->")
        body = steps.to_markdown(self.remote(12)).replace("Question", "Changed")
        self.lesson.write_text(text[:start] + f"<!-- step_id: 12 -->\n## Шаг 2\n\n{body}\n\n" + text[end:],
                               encoding="utf-8", newline="\n")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["skipped"][0]["reason"], "шаги типа table пока не публикуются")

    def test_link_is_kept_and_extra_attributes_reported(self):
        # С формата 5 адрес ссылки хранится в Markdown: правка шага его не теряет.
        self.assertIn("See [docs](https://x)", self.lesson.read_text(encoding="utf-8"))
        self.edit("See [docs]", "See the [docs]")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertNotIn("будет потеряно", result["changes"][0])
        self.assertIn("+See the [docs](https://x)", Path(result["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual(publish._lossy_features('<a href="x" rel="nofollow">a</a>'), ["атрибуты оформления (a rel)"])

    def test_image_outside_course_folder_is_refused(self):
        self.edit("Theory\n", "Theory\n\n![x](../../secret.png)\n")
        with self.assertRaises(StepikError):
            publish.plan_push(self.client, 1, "1.1")

    def test_missing_step_is_refused(self):
        text = self.lesson.read_text(encoding="utf-8")
        self.lesson.write_text(text.replace("step_id: 11 -->", "step_id: 77 -->"), encoding="utf-8")
        with self.assertRaisesRegex(StepikError, "Перенос шагов не поддерживается"):
            publish.plan_push(self.client, 1, "1.1")

    def test_new_step_in_the_middle_keeps_file_order(self):
        self.shift_on_insert = False
        self.edit("<!-- step_id: 12 -->", "<!-- new_step -->\nNew\n\n<!-- step_id: 12 -->")
        plan = publish.plan_push(self.client, 1, "1.1")
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.data["lessons"][0]["steps"], [11, 99, 12, 13])
        self.assertEqual([self.remote(sid)["position"] for sid in [11, 99, 12, 13]], [1, 2, 3, 4])
        self.assertEqual([op["step_id"] for op in result["done"] if op["op"] == "move"], [12, 13])
        log = (self.folder / "logs" / "stepik-write.log").read_text(encoding="utf-8")
        self.assertEqual(log.count('"move"'), 2)

    def test_reorder_only_plan_fixes_order_on_stepik(self):
        self.shift_on_insert = False
        self.data["lessons"][0]["steps"] = [12, 11, 13]
        self.remote(11)["position"], self.remote(12)["position"] = 2, 1
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(plan["changes"], [{"step": "порядок шагов", "status": "MOVED", "порядок": [11, 12, 13]}])
        self.assert_no_writes()
        publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(self.data["lessons"][0]["steps"], [11, 12, 13])
        self.assertEqual(publish.plan_push(self.client, 1, "1.1")["status"], "nothing_to_push")

    def test_reorder_with_new_steps_is_refused(self):
        self.data["lessons"][0]["steps"] = [12, 11, 13]
        self.edit("<!-- step_id: 13 -->", "<!-- new_step -->\nNew\n\n<!-- step_id: 13 -->")
        with self.assertRaisesRegex(StepikError, "Порядок шагов"):
            publish.plan_push(self.client, 1, "1.1")

    def test_lesson_from_another_course_is_refused(self):
        self.edit("Theory\n", "Theory 2\n")
        self.data["sections"][0]["course"] = 2
        with self.assertRaisesRegex(StepikError, "не входит в курс"):
            publish.plan_push(self.client, 1, "1.1")

    def test_partial_failure_reports_done_operations(self):
        self.edit("Theory\n", "<!-- new_step -->\nFirst new\n\n<!-- step_id: 11 -->\nTheory 2\n")
        text = self.lesson.read_text(encoding="utf-8")
        # The original marker of step 11 now precedes the new block; drop the duplicate.
        self.lesson.write_text(text.replace("<!-- step_id: 11 -->\n", "", 1), encoding="utf-8")
        plan = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual([item["status"] for item in plan["changes"]], ["NEW", "MODIFIED"])
        self.client.put.side_effect = StepikError("boom")
        result = publish.apply_push(self.client, 1, plan["plan_id"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["done"], [{"op": "create", "position": 1, "type": "text", "step_id": 99}])
        self.assertEqual(result["failed"], {"op": "update", "step_id": 11})
        self.assertIn("<!-- step_id: 99 -->", self.lesson.read_text(encoding="utf-8"))


class ServerRegistrationTests(unittest.TestCase):
    def test_write_tool_is_marked_destructive(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        self.assertTrue(tools["stepik_plan_push"].annotations.read_only_hint)
        self.assertTrue(tools["stepik_apply_push"].annotations.destructive_hint)


if __name__ == "__main__":
    unittest.main()
