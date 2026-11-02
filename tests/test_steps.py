import copy
import unittest

from stepik_mcp import steps
from stepik_mcp.client import StepikError


def source(block):
    return {"id": 1, "lesson": 10, "block": {"text": "<p>Условие</p>", "feedback_correct": "", "feedback_wrong": "",
                                              **block}}


CHOICE = source({
    "name": "choice", "options": {"is_multiple_choice": False},
    "source": {"is_multiple_choice": False, "is_always_correct": False, "sample_size": 3, "preserve_order": False,
               "is_html_enabled": True, "is_options_feedback": False,
               "options": [{"is_correct": False, "text": "да", "feedback": "", "extra": 1},
                           {"is_correct": True, "text": "нет", "feedback": ""}]},
})
CODE = source({
    "name": "code",
    "options": {"execution_time_limit": 5, "execution_memory_limit": 256, "code_templates": {"python3": "pass"}},
    "source": {"code": "def check(reply, clue):\n    return reply == clue\n", "execution_time_limit": 5,
               "execution_memory_limit": 256, "samples_count": 1, "is_run_user_code_allowed": True,
               "templates_data": "::python3\n::header\nimport sys\n::code\npass\n::footer\nmain()",
               "test_cases": [["1 2\n", "3\n"], ["```\n", "x"]], "manual_time_limits": []},
})
TABLE = source({"name": "table", "source": {"rows": [{"name": "r", "columns": [{"choice": True}]}],
                                            "columns": [{"name": "c"}]}})


class RoundTripTests(unittest.TestCase):
    def test_every_supported_type_round_trips(self):
        samples = [CHOICE, CODE, TABLE,
                   source({"name": "matching", "source": {"preserve_firsts_order": True, "is_html_enabled": True,
                                                          "pairs": [{"first": "a", "second": "b"}]}}),
                   source({"name": "sorting", "source": {"is_html_enabled": True, "options": [{"text": "1"}]}}),
                   source({"name": "string", "source": {"pattern": "yes", "use_re": False, "match_substring": False,
                                                        "case_sensitive": False, "code": "", "is_text_disabled": False,
                                                        "is_file_disabled": True}}),
                   source({"name": "number", "source": {"options": [{"answer": "2", "max_error": "0"}]}}),
                   source({"name": "free-answer", "source": {"is_attachments_enabled": False,
                                                             "is_html_enabled": True, "manual_scoring": False}}),
                   source({"name": "text", "text": "<ul><li><p>a</p></li></ul>", "source": {}})]
        for sample in samples:
            with self.subTest(kind=sample["block"]["name"]):
                body = steps.to_markdown(sample)
                self.assertEqual(steps.build_block(sample["block"], body), sample["block"])

    def test_yaml_shows_editable_settings(self):
        body = steps.to_markdown(CHOICE)
        self.assertTrue(body.startswith("Условие\n\n```yaml stepik\ntype: choice\n"))
        self.assertIn("show: 3", body)
        self.assertIn("- text: нет\n  correct: true", body)
        self.assertNotIn("feedback_wrong", body)  # значения по умолчанию не показываются
        code = steps.to_markdown(CODE)
        self.assertIn("    header: |\n      import sys", code)
        self.assertIn("checker: |", code)
        # В тестах есть ```, поэтому ограда YAML длиннее.
        self.assertIn("````yaml stepik", code)
        self.assertTrue(code.endswith("\n````"))


class EditTests(unittest.TestCase):
    def test_changing_one_answer_changes_only_that_field(self):
        body = steps.to_markdown(CHOICE).replace("- text: да\n", "- text: да\n  correct: true\n")
        block = steps.build_block(CHOICE["block"], body)
        self.assertEqual(steps.changed_paths(CHOICE["block"], block), ["source.options"])
        self.assertTrue(block["source"]["options"][0]["is_correct"])
        self.assertEqual(block["source"]["options"][0]["extra"], 1)  # лишний ключ варианта сохранён
        self.assertEqual(block["text"], "<p>Условие</p>")             # условие не тронуто

    def test_mirrored_setting_updates_both_places(self):
        body = steps.to_markdown(CHOICE).replace("show: 3\n", "show: 3\nmultiple: true\n")
        block = steps.build_block(CHOICE["block"], body)
        self.assertEqual(steps.changed_paths(CHOICE["block"], block), ["options.is_multiple_choice",
                                                                       "source.is_multiple_choice"])

    def test_code_tests_limits_and_templates(self):
        body = steps.to_markdown(CODE).replace("time_limit: 5", "time_limit: 8").replace("      pass\n", "      solve()\n", 1)
        block = steps.build_block(CODE["block"], body)
        self.assertEqual(block["source"]["execution_time_limit"], 8)
        self.assertEqual(block["options"]["execution_time_limit"], 8)
        self.assertEqual(block["source"]["templates_data"],
                         "::python3\n::header\nimport sys\n::code\nsolve()\n::footer\nmain()")
        self.assertEqual(block["source"]["manual_time_limits"], [])

    def test_changed_condition_becomes_html(self):
        body = steps.to_markdown(CHOICE).replace("Условие", "Новое **условие**", 1)
        self.assertEqual(steps.build_block(CHOICE["block"], body)["text"], "<p>Новое <strong>условие</strong></p>")

    def test_invalid_edits_are_refused(self):
        body = steps.to_markdown(CHOICE)
        cases = {
            "тип": body.replace("type: choice", "type: string"),
            "неизвестные": body.replace("show: 3", "show: 3\nshuffle: true"),
            "YAML": body.replace("show: 3", "show: [3"),
            "нет YAML": "Только условие",
            "нет поля text": body.replace("- text: да\n", "- correct: true\n"),
        }
        for reason, broken in cases.items():
            with self.subTest(reason), self.assertRaises(StepikError):
                steps.build_block(CHOICE["block"], broken)

    def test_generic_type_keeps_whole_source(self):
        body = steps.to_markdown(TABLE)
        self.assertIn("source:\n  rows:", body)
        changed = steps.build_block(TABLE["block"], body.replace("name: c", "name: d"))
        self.assertEqual(changed["source"]["columns"], [{"name": "d"}])


class TemplatesTests(unittest.TestCase):
    def test_parse_and_serialize(self):
        data = "::julia\n\n\n::python3\n::header\nh\n::code\nc\n"
        parsed = steps.parse_templates(data)
        self.assertEqual(parsed, {"julia": "\n\n", "python3": {"header": "h\n", "code": "c\n"}})
        self.assertEqual(steps.serialize_templates(parsed), data)

    def test_unusual_format_is_hidden_but_kept(self):
        block = copy.deepcopy(CODE["block"])
        block["source"]["templates_data"] = "\n\n::julia\n"
        body = steps.to_markdown({"block": block})
        self.assertNotIn("templates:", body)
        self.assertEqual(steps.build_block(block, body), block)


class SplitBodyTests(unittest.TestCase):
    def test_yaml_must_be_last(self):
        self.assertEqual(steps.split_body("Текст\n\n```python\nx\n```"), ("Текст\n\n```python\nx\n```", None))
        self.assertEqual(steps.split_body("A\n\n```yaml stepik\ntype: x\n```\n"), ("A", "type: x\n"))


if __name__ == "__main__":
    unittest.main()
