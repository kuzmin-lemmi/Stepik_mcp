import unittest
from unittest.mock import Mock, patch

from stepik_mcp import analytics, server
from stepik_mcp.client import StepikClient, StepikError


class LessonStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.lesson = {
            "id": 10, "title": "Test lesson", "viewed_by": 2,
            "passed_by": 2, "time_to_complete": None,
        }
        self.unit = {"id": 40, "section": 30, "lesson": 10}
        self.data = {
            "lessons": [self.lesson],
            "courses": [{"id": 20, "sections": [30]}],
            "sections": [{"id": 30, "units": [40]}],
            "units": [self.unit],
        }
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)

    def get(self, endpoint, **params):
        self.assertEqual(set(params), {"ids"})
        return {endpoint: [row for row in self.data[endpoint] if row["id"] in params["ids"]]}

    def test_seconds_are_converted_to_minutes(self):
        self.lesson["time_to_complete"] = 125
        text = analytics.render_lesson_stats(self.client, 10)
        self.assertIn("125", text)
        self.assertIn("2.08", text)
        self.assertIn("`lesson.time_to_complete`", text)
        self.assertNotIn("%", text)

    def test_unit_fields_do_not_override_lesson_aggregates(self):
        self.unit.update(viewed_by=999, passed_by=888, time_to_complete=777)
        text = analytics.render_lesson_stats(self.client, 10, course_id=20)
        for value in ("999", "888", "777"):
            self.assertNotIn(value, text)
        self.assertIn("`40`", text)

    def test_real_null_shape_is_unavailable_not_zero(self):
        data = analytics.lesson_statistics(self.client, 10, course_id=20)
        self.assertEqual(data["metrics"], {
            "viewed_by": 2, "passed_by": 2, "time_to_complete": None,
        })
        self.assertIn("null", data["unavailable"]["time_to_complete"])
        text = analytics.render_lesson_stats(self.client, 10)
        self.assertIn("null", text)
        self.assertNotIn("100%", text)
        self.assertNotIn("100.0%", text)

    def test_zero_metrics_are_available(self):
        self.lesson.update(viewed_by=0, passed_by=0, time_to_complete=0)
        data = analytics.lesson_statistics(self.client, 10)
        self.assertEqual(data["metrics"], {
            "viewed_by": 0, "passed_by": 0, "time_to_complete": 0,
        })
        self.assertEqual(data["unavailable"], {})
        text = analytics.render_lesson_stats(self.client, 10)
        self.assertIn("0.00", text)
        self.assertNotIn("%", text)

    def test_float_counts_and_subminute_duration_are_not_percentages(self):
        self.lesson.update(viewed_by=1.0, passed_by=0.0, time_to_complete=30.0)
        data = analytics.lesson_statistics(self.client, 10)
        self.assertIs(type(data["metrics"]["viewed_by"]), int)
        text = analytics.render_lesson_stats(self.client, 10)
        self.assertIn("0.50", text)
        self.assertNotIn("%", text)

    def test_missing_fields_and_aliases_are_not_guessed(self):
        for field in ("viewed_by", "passed_by", "time_to_complete"):
            del self.lesson[field]
        self.lesson.update(views=100, completed_by=90, average_time=15)
        data = analytics.lesson_statistics(self.client, 10)
        self.assertTrue(all(value is None for value in data["metrics"].values()))
        self.assertEqual(set(data["unavailable"]), set(data["metrics"]))

    def test_invalid_metrics_are_unavailable(self):
        for field in ("viewed_by", "passed_by", "time_to_complete"):
            for value in (True, False, "2", {}, [], -1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    self.lesson[field] = value
                    data = analytics.lesson_statistics(self.client, 10)
                    self.assertIsNone(data["metrics"][field])
                    self.assertIn(field, data["unavailable"])

    def test_fractional_counts_are_invalid_but_fractional_seconds_are_valid(self):
        self.lesson.update(viewed_by=2.5, passed_by=1.5, time_to_complete=0.5)
        data = analytics.lesson_statistics(self.client, 10)
        self.assertIsNone(data["metrics"]["viewed_by"])
        self.assertIsNone(data["metrics"]["passed_by"])
        self.assertEqual(data["metrics"]["time_to_complete"], 0.5)

    def test_without_course_only_fetches_lesson(self):
        data = analytics.lesson_statistics(self.client, 10)
        self.assertEqual(data["units"], [])
        self.client.get.assert_called_once_with("lessons", ids=[10])

    def test_course_context_checks_membership(self):
        data = analytics.lesson_statistics(self.client, 10, course_id=20)
        self.assertEqual([unit["id"] for unit in data["units"]], [40])
        self.assertEqual(self.client.get.call_count, 4)

    def test_multiple_placements_do_not_multiply_counts(self):
        self.data["sections"][0]["units"].append(41)
        self.data["units"].append({"id": 41, "section": 30, "lesson": 10})
        data = analytics.lesson_statistics(self.client, 10, course_id=20)
        self.assertEqual(len(data["units"]), 2)
        self.assertEqual(data["metrics"]["passed_by"], 2)

    def test_missing_lesson_is_an_error(self):
        with self.assertRaises(StepikError):
            analytics.lesson_statistics(self.client, 999)

    def test_unavailable_course_or_partial_structure_is_an_error(self):
        for endpoint in ("courses", "sections", "units"):
            with self.subTest(endpoint=endpoint):
                with patch.dict(self.data, {endpoint: []}):
                    with self.assertRaises(StepikError):
                        analytics.lesson_statistics(self.client, 10, course_id=20)

    def test_lesson_outside_course_is_an_error(self):
        self.unit["lesson"] = 999
        with self.assertRaises(StepikError):
            analytics.lesson_statistics(self.client, 10, course_id=20)

    def test_empty_course_is_not_a_confirmed_context(self):
        self.data["courses"][0]["sections"] = []
        with self.assertRaises(StepikError):
            analytics.lesson_statistics(self.client, 10, course_id=20)

    def test_output_does_not_scan_actions_or_other_lesson_fields(self):
        self.lesson.update(actions={"view_statistics": "PRIVATE_ACTION"},
                           private_view_data="PRIVATE_VALUE")
        text = analytics.render_lesson_stats(self.client, 10)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("actions", text)

    def test_mcp_wrapper_uses_the_same_renderer(self):
        with patch.object(server, "_guard", return_value=None):
            with patch.object(server, "CLIENT", self.client):
                self.assertEqual(
                    server.stepik_lesson_stats(10, 20),
                    analytics.render_lesson_stats(self.client, 10, course_id=20),
                )


if __name__ == "__main__":
    unittest.main()
