import unittest
from unittest.mock import patch

from stepik_mcp import roundtrip, steps
from test_steps import CHOICE, CODE, TABLE


class RoundTripReportTests(unittest.TestCase):
    def test_report_counts_types(self):
        text = {"id": 2, "lesson": 10, "block": {"name": "text", "text": '<p><a href="x">ссылка</a></p>'}}
        report = roundtrip.check_sources([CHOICE, CODE, TABLE, text])
        self.assertEqual((report["step_count"], report["passed"], report["failed"]), (4, 4, 0))
        self.assertEqual(report["by_type"]["choice"], "1/1")
        self.assertEqual(report["without_schema"], ["table"])
        self.assertEqual(report["steps_losing_formatting_on_edit"], 1)

    def test_mismatch_names_step_and_field(self):
        def broken(block, body):
            return {**block, "text": "другое"}
        with patch.object(steps, "build_block", broken):
            report = roundtrip.check_sources([CHOICE])
        self.assertEqual(report["failures"], [{"step_id": 1, "lesson_id": 10, "type": "choice", "fields": ["text"]}])

    def test_unclosed_code_block_is_reported(self):
        with patch.object(steps, "to_markdown", lambda source: "```\nкод без конца"):
            report = roundtrip.check_sources([{"id": 3, "block": {"name": "text", "text": ""}}])
        self.assertIn("не закрыт блок кода", report["failures"][0]["fields"][0])

    def test_course_walk_uses_only_reads(self):
        class Client:
            def by_ids(self, endpoint, ids):
                data = {"courses": [{"id": 5, "sections": [1]}], "sections": [{"id": 1, "units": [2]}],
                        "units": [{"id": 2, "lesson": 10}], "lessons": [{"id": 10, "steps": [1]}],
                        "step-sources": [CHOICE]}
                return data[endpoint]
        report = roundtrip.check_course(Client(), 5)
        self.assertEqual((report["status"], report["lesson_count"], report["passed"]), ("ok", 1, 1))


if __name__ == "__main__":
    unittest.main()
