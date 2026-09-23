import unittest
from unittest.mock import Mock

from stepik_mcp import feedback
from stepik_mcp.client import IDS_CHUNK, StepikClient, StepikError


def comment(cid, step=10, replies=None, parent=None):
    return {
        "id": cid, "target": step, "replies": replies or [],
        "parent": parent, "user": 7, "text": f"<p>Body {cid}</p>",
    }


class CommentsTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "steps": [{"id": 10, "discussion_proxy": "default-10"},
                      {"id": 20, "discussion_proxy": "default-20"}],
            "discussion-proxies": [
                {"id": "default-10", "discussions": [100]},
                {"id": "default-20", "discussions": []},
            ],
            "comments": [comment(100)],
            "users": [{"id": 7, "full_name": "Test Author"}],
        }
        # Exercise real by_ids batching, but never send a network request.
        self.client = StepikClient("", "")
        self.client.get = Mock(side_effect=self.get)

    def get(self, endpoint, **params):
        if endpoint == "lessons":
            return {"lessons": [{"id": 1, "steps": [20, 10]}]}
        self.assertEqual(set(params), {"ids"})
        self.assertLessEqual(len(params["ids"]), IDS_CHUNK)
        return {endpoint: [r for r in self.data[endpoint] if r["id"] in params["ids"]]}

    def test_small_discussion_stops_without_extra_requests(self):
        comments, users = feedback.step_comments(self.client, 10)
        self.assertEqual([c["id"] for c in comments], [100])
        self.assertEqual(users[7]["full_name"], "Test Author")
        self.assertEqual(self.client.get.call_count, 4)

    def test_empty_discussion_does_not_request_comments_or_users(self):
        self.assertEqual(feedback.step_comments(self.client, 20), ([], {}))
        self.assertEqual(self.client.get.call_count, 2)

    def test_replies_are_loaded_once_even_with_cycles_and_duplicates(self):
        self.data["comments"] = [comment(100, replies=[101, 101]),
                                 comment(101, replies=[100, 102], parent=100),
                                 comment(102, parent=101)]
        comments, _ = feedback.step_comments(self.client, 10)
        self.assertEqual([c["id"] for c in comments], [100, 101, 102])

    def test_lesson_batches_steps_and_users_and_preserves_positions(self):
        self.data["discussion-proxies"][1]["discussions"] = [200]
        self.data["comments"].append(comment(200, step=20))
        text = feedback.render_lesson_comments(self.client, 1)
        self.assertLess(text.index("step 20"), text.index("step 10"))
        self.assertIn("Body 100", text)
        self.assertIn("Body 200", text)
        self.assertEqual(self.client.get.call_count, 5)

    def test_more_than_100_comments_are_loaded_in_id_batches(self):
        ids = list(range(100, 225))
        self.data["discussion-proxies"][0]["discussions"] = ids
        self.data["comments"] = [comment(cid) for cid in reversed(ids)]
        comments, _ = feedback.step_comments(self.client, 10)
        self.assertEqual([c["id"] for c in comments], ids)

    def test_solutions_are_not_loaded(self):
        self.data["steps"][0]["discussion_threads"] = ["default-10", "solutions-10"]
        self.data["discussion-proxies"].append({"id": "solutions-10", "discussions": [900]})
        self.data["comments"].append(comment(900))
        comments, _ = feedback.step_comments(self.client, 10)
        self.assertEqual([c["id"] for c in comments], [100])

    def test_unavailable_step_is_not_reported_as_empty(self):
        with self.assertRaises(StepikError):
            feedback.step_comments(self.client, 999)

    def test_missing_proxy_or_discussions_is_an_error(self):
        for proxy in (None, {"id": "default-10"}):
            with self.subTest(proxy=proxy):
                self.data["discussion-proxies"] = [proxy] if proxy else []
                with self.assertRaises(StepikError):
                    feedback.step_comments(self.client, 10)

    def test_missing_comment_or_reply_is_an_error(self):
        for rows in ([], [comment(100, replies=[101])]):
            with self.subTest(rows=rows):
                self.data["comments"] = rows
                with self.assertRaises(StepikError):
                    feedback.step_comments(self.client, 10)

    def test_wrong_target_is_rejected(self):
        self.data["comments"][0]["target"] = 999
        with self.assertRaises(StepikError):
            feedback.step_comments(self.client, 10)

    def test_missing_replies_is_not_treated_as_complete(self):
        del self.data["comments"][0]["replies"]
        with self.assertRaises(StepikError):
            feedback.step_comments(self.client, 10)

    def test_render_includes_parent_comment_id(self):
        self.data["comments"] = [comment(100, replies=[101]), comment(101, parent=100)]
        text = feedback.render_step_comments(self.client, 10)
        self.assertIn("comment 101", text)
        self.assertIn("comment 100", text)
        self.assertIn("Body 101", text)


if __name__ == "__main__":
    unittest.main()
