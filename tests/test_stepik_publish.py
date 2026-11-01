import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from stepik_mcp import publish, server, workspace as sw
from stepik_mcp.client import StepikClient, StepikError
from stepik_mcp.render import html_to_md


class MarkdownToHtmlTests(unittest.TestCase):
    def test_supported_markup(self):
        result = publish.md_to_html(
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
        self.assertEqual(html_to_md(publish.md_to_html(markdown)), markdown)

    def test_html_block_passes_through(self):
        self.assertEqual(publish.md_to_html("<table><tr><td>1</td></tr></table>"),
                         "<table><tr><td>1</td></tr></table>")

    def test_raw_html_in_text_is_escaped(self):
        self.assertEqual(publish.md_to_html("a <script> b"), "<p>a &lt;script&gt; b</p>")


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
            "courses": [{"id": 1, "title": "Course", "sections": [10]}],
            "sections": [{"id": 10, "course": 1, "title": "Module", "position": 1, "units": [100]}],
            "units": [{"id": 100, "lesson": 1000, "section": 10, "position": 1}],
            "lessons": [{"id": 1000, "title": "Lesson", "steps": [11, 12, 13]}],
            "step-sources": [
                {"id": 11, "lesson": 1000, "position": 1, "cost": 0,
                 "block": {"name": "text", "text": "<p>Theory</p>"}},
                {"id": 12, "lesson": 1000, "position": 2, "cost": 1,
                 "block": {"name": "string", "text": "<p>Question</p>", "source": {"answer": "42"}}},
                {"id": 13, "lesson": 1000, "position": 3, "cost": 0,
                 "block": {"name": "text", "text": '<p>See <a href="https://x">docs</a></p>'}},
            ],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)
        self.client.post = Mock(side_effect=self.post)
        self.client.put = Mock(side_effect=self.put)
        self.client.upload = Mock(return_value={"attachments": [{"id": 5, "file": "/media/attachments/lesson/1000/s.png"}]})
        sw.cache_lesson(self.client, 1, "1.1")
        self.folder = self.root / "course-1"
        self.lesson = next(self.folder.glob("*/01.01-lesson-1000.md"))

    def get(self, endpoint, **params):
        rows = self.data[endpoint]
        if "ids" in params:
            return {endpoint: [row for row in rows if row["id"] in params["ids"]]}
        self.assertEqual((endpoint, set(params)), ("units", {"lesson"}))
        return {endpoint: [row for row in rows if row["lesson"] == params["lesson"]]}

    def post(self, endpoint, payload):
        self.assertEqual(endpoint, "step-sources")
        return {"step-sources": [{"id": 99, **payload["stepSource"]}]}

    def put(self, endpoint, payload):
        return {"step-sources": [payload["stepSource"]]}

    def edit(self, old, new):
        text = self.lesson.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.lesson.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    def assert_no_writes(self):
        self.client.post.assert_not_called()
        self.client.put.assert_not_called()
        self.client.upload.assert_not_called()

    def test_course_outside_allowlist_is_refused(self):
        with patch.dict(os.environ, {"ALLOWED_WRITE_COURSES": "241862"}):
            with self.assertRaisesRegex(StepikError, "WRITE_NOT_ALLOWED"):
                publish.plan_push(self.client, 1, "1.1")
            with self.assertRaisesRegex(StepikError, "WRITE_NOT_ALLOWED"):
                publish.apply_push(self.client, 1, "20260101-000000-abcdef")
        self.assert_no_writes()

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

    def test_non_text_step_edit_is_skipped(self):
        self.edit("Question", "Changed question")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["status"], "nothing_to_push")
        self.assertEqual(result["skipped"][0]["step_id"], 12)

    def test_lossy_html_is_reported(self):
        self.edit("See [docs]", "See the [docs]")
        result = publish.plan_push(self.client, 1, "1.1")
        self.assertEqual(result["changes"][0]["будет потеряно"], ["ссылки (в Markdown урока нет адресов ссылок)"])

    def test_image_outside_course_folder_is_refused(self):
        self.edit("Theory\n", "Theory\n\n![x](../../secret.png)\n")
        with self.assertRaises(StepikError):
            publish.plan_push(self.client, 1, "1.1")

    def test_missing_step_and_reordering_are_refused(self):
        text = self.lesson.read_text(encoding="utf-8")
        self.lesson.write_text(text.replace("step_id: 11 -->", "step_id: 77 -->"), encoding="utf-8")
        with self.assertRaisesRegex(StepikError, "Удаление и перенос"):
            publish.plan_push(self.client, 1, "1.1")
        self.data["lessons"][0]["steps"] = [77, 12, 13]
        self.data["step-sources"].append({"id": 77, "lesson": 1000, "block": {"name": "text", "text": ""}})
        self.data["lessons"][0]["steps"] = [12, 77, 13]
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
        self.assertEqual(result["done"], [{"op": "create", "position": 1, "step_id": 99}])
        self.assertEqual(result["failed"], {"op": "update", "step_id": 11})
        self.assertIn("<!-- step_id: 99 -->", self.lesson.read_text(encoding="utf-8"))


class ServerRegistrationTests(unittest.TestCase):
    def test_write_tool_is_marked_destructive(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        self.assertTrue(tools["stepik_plan_push"].annotations.read_only_hint)
        self.assertTrue(tools["stepik_apply_push"].annotations.destructive_hint)


if __name__ == "__main__":
    unittest.main()
