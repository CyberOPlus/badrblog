"""Safety and read-only feedback regressions for CyberOPlus Facebook Jobs."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import facebook_performance as metrics
import facebook_publisher as facebook


NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


def record(index=1, posted=None):
    published = posted or (NOW - timedelta(hours=3))
    return {
        "facebook_post_id": f"123_{index}",
        "facebook_posted_at": published.isoformat(),
        "source_name": "Emploi-Public — services de l'État",
    }


def result(post_id, comments=4, reactions=10, shares=1):
    return {
        "id": post_id,
        "created_time": NOW.isoformat(),
        "reactions": {"summary": {"total_count": reactions}},
        "comments": {"summary": {"total_count": comments}},
        "shares": {"count": shares},
    }


class FacebookFeedbackTests(unittest.TestCase):
    def test_urgency_is_based_on_current_deadline_not_stale_publish_flag(self):
        row = {
            "job_notice_type": "competition",
            "official_source": True,
            "job_deadline": "2026-10-10T08:00:00Z",
            "job_diploma": "Bac+2",
            "job_publish_immediately": False,
        }
        self.assertTrue(facebook._facebook_urgent_now(row, now=NOW))
        row["job_deadline"] = "2026-10-08T08:00:00Z"
        row["job_publish_immediately"] = True
        self.assertFalse(facebook._facebook_urgent_now(row, now=NOW))

    def test_do_not_infer_reach_from_graph_interactions(self):
        row = {"post_id": "123_1", "published_at": NOW.isoformat()}
        obs = metrics._observation(row, result("123_1"), NOW)
        self.assertEqual(obs["interactions_observed"], 15)
        self.assertEqual(obs["interactions_available"], 3)
        self.assertNotIn("reach", obs)
        self.assertEqual(obs["local_weekday"], 4)

    def test_collect_read_only_and_persist_one_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fb.json"
            fetched = Mock(side_effect=lambda key, params: result(key))
            outcome = metrics.collect_facebook_performance(
                now=NOW, queue_articles=[record()], campaign_records=[],
                fetcher=fetched, state_path=path, token="fake-test-token",
            )
            self.assertEqual(outcome["checked"], 1)
            self.assertEqual(outcome["succeeded"], 1)
            self.assertEqual(fetched.call_count, 1)
            self.assertEqual(fetched.call_args.args[1]["fields"], metrics.API_FIELDS)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["posts"]["123_1"]["interactions_observed"], 15)
            self.assertNotIn("fake-test-token", path.read_text(encoding="utf-8"))
            again = metrics.collect_facebook_performance(
                now=NOW + timedelta(hours=1), queue_articles=[record()],
                campaign_records=[], fetcher=fetched, state_path=path, token="fake-test-token",
            )
            self.assertEqual(again["status"], "cooldown")
            self.assertEqual(fetched.call_count, 1)

    def test_hard_maximum_four_requests_per_collection_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = Mock(side_effect=lambda key, params: result(key))
            outcome = metrics.collect_facebook_performance(
                now=NOW, queue_articles=[record(index=i) for i in range(1, 12)],
                campaign_records=[], fetcher=fetcher,
                state_path=Path(tmp) / "metrics.json", token="fake-test-token",
            )
            self.assertEqual(outcome["checked"], 4)
            self.assertEqual(fetcher.call_count, 4)

    def test_no_token_no_api_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = Mock()
            state_path = Path(tmp) / "metrics.json"
            outcome = metrics.collect_facebook_performance(
                now=NOW, queue_articles=[record()], campaign_records=[],
                fetcher=fetcher, state_path=state_path, token="",
            )
            self.assertEqual(outcome["status"], "no_facebook_token")
            fetcher.assert_not_called()
            self.assertFalse(state_path.exists())

    def test_graph_errors_do_not_trigger_post_retries_or_leak_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "metrics.json"
            def fail(key, params):
                raise RuntimeError("hidden access token fake-test-token")
            outcome = metrics.collect_facebook_performance(
                now=NOW, queue_articles=[record()], campaign_records=[],
                fetcher=fail, state_path=state_path, token="fake-test-token",
            )
            self.assertEqual(outcome["status"], "metrics_unavailable")
            self.assertEqual(outcome["unavailable"], 1)
            saved = state_path.read_text(encoding="utf-8")
            self.assertNotIn("fake-test-token", saved)
            self.assertIn("RuntimeError", saved)

    def test_dedupe_memory_queue_and_skip_stale_posts(self):
        candidates = metrics._latest_posts(
            NOW, queue_articles=[record(1), record(2, NOW-timedelta(days=35))],
            campaign_records=[record(1), record(3, NOW-timedelta(minutes=10))],
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["post_id"], "123_1")

    def test_report_only_after_three_complete_observations_in_window(self):
        posts = {
            str(i): {"local_hour": 9, "interactions_observed": i * 2}
            for i in range(1, 4)
        }
        posts["4"] = {"local_hour": 19, "interactions_observed": 100}
        outcome = metrics.summarize_performance(posts)
        self.assertEqual(outcome, {"morning": {"samples": 3, "average_interactions": 4.0}})


if __name__ == "__main__":
    unittest.main()
