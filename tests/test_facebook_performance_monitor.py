"""Non-mutating Facebook engagement monitor regression checks."""
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import facebook_performance_monitor as metrics

NOW = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)
POST_ID = "103026497833721_1091452636583134"


def article(age, **kwargs):
    row = {
        "facebook_post_id": POST_ID,
        "facebook_posted_at": (NOW - timedelta(hours=age)).isoformat(),
        "facebook_comment_id": "103_555",
        "job_notice_type": "competition",
    }
    row.update(kwargs)
    return row


def payload():
    return {
        "id": POST_ID,
        "reactions": {"summary": {"total_count": 10}},
        "comments": {"summary": {"total_count": 4}},
        "shares": {"count": 2},
    }


class FacebookMetricsTests(unittest.TestCase):
    def test_no_network_when_disabled(self):
        request = Mock()
        with patch.dict("os.environ", {"JOBS_FACEBOOK_METRICS_ENABLED": "false"}):
            self.assertEqual(metrics.collect(now=NOW, session=request)["status"], "disabled")
        request.get.assert_not_called()

    def test_verified_comments_subtract_first_comment_once(self):
        self.assertEqual(metrics._extract_counts(payload(), True), {
            "reactions": 10, "comments_total": 4,
            "comments_excluding_first_comment": 3, "shares": 2,
            "known_interactions": 15,
        })
        self.assertEqual(metrics._extract_counts(payload(), False)["known_interactions"], 16)

    def test_missing_metric_is_not_falsely_zero(self):
        result = metrics._extract_counts({"id": POST_ID, "shares": {"count": 1}}, False)
        self.assertIsNone(result["reactions"])
        self.assertIsNone(result["comments_total"])
        self.assertEqual(result["known_interactions"], 1)

    def test_age_windows_do_not_label_older_count_as_24h(self):
        state = {"posts": {}}
        for age, stage in [(2, "early"), (30, "day"), (180, "week")]:
            candidates = list(metrics._candidate_samples({"articles": [article(age)]}, state, NOW))
            self.assertEqual([x[3] for x in candidates], [stage])
        self.assertEqual(list(metrics._candidate_samples({"articles": [article(75)]}, state, NOW)), [])

    def test_uses_capped_read_only_calls_and_remembers_snapshot(self):
        response = Mock(status_code=200)
        response.json.return_value = payload()
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as td, \
             patch.object(metrics, "QUEUE_PATH", Path(td) / "queue.json"), \
             patch.object(metrics, "STATE_PATH", Path(td) / "stats.json"), \
             patch.object(metrics, "FACEBOOK_PAGE_ACCESS_TOKEN", "fake-token"), \
             patch.dict("os.environ", {"JOBS_FACEBOOK_METRICS_ENABLED": "true"}):
            metrics.QUEUE_PATH.write_text(
                __import__("json").dumps({"articles": [article(26)]}),
                encoding="utf-8",
            )
            result = metrics.collect(now=NOW, session=session)
            summary = metrics.performance_snapshot()
            again = metrics.collect(now=NOW + timedelta(minutes=2), session=session)
        self.assertEqual(result["saved_samples"], 1)
        self.assertEqual(result["queried"], 1)
        self.assertEqual(summary["comparable_24h_samples"], 1)
        self.assertFalse(summary["can_recommend_slots"])
        self.assertEqual(again["status"], "rate_guard")
        session.get.assert_called_once()
        args, kwargs = session.get.call_args
        self.assertIn(POST_ID, args[0])
        self.assertEqual(kwargs["params"]["access_token"], "fake-token")
        self.assertNotIn("impressions", kwargs["params"]["fields"])

    def test_permission_denied_does_not_retry_every_cycle(self):
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"message": "Forbidden"}}
        session = Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as td, \
             patch.object(metrics, "QUEUE_PATH", Path(td) / "queue.json"), \
             patch.object(metrics, "STATE_PATH", Path(td) / "stats.json"), \
             patch.object(metrics, "FACEBOOK_PAGE_ACCESS_TOKEN", "fake-token"), \
             patch.dict("os.environ", {"JOBS_FACEBOOK_METRICS_ENABLED": "true"}):
            metrics.QUEUE_PATH.write_text(
                __import__("json").dumps({"articles": [article(26)]}), encoding="utf-8"
            )
            first = metrics.collect(now=NOW, session=session)
            second = metrics.collect(now=NOW + timedelta(hours=1), session=session)
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["error_code"], "http_403")
        self.assertEqual(second["status"], "read_permission_cooldown")
        session.get.assert_called_once()

    def test_discard_unknown_post_id_and_non_post_record(self):
        candidates = list(metrics._candidate_samples({
            "articles": [article(26, facebook_post_id="https://evil.com"), {
                "facebook_status": "facebook_pending",
                "facebook_posted_at": NOW.isoformat(),
            }]
        }, {"posts": {}}, NOW))
        self.assertEqual(candidates, [])

    def test_no_fake_best_time_without_multiple_24h_samples(self):
        entries = {
            "p1": {
                "morocco_weekday": 3,
                "morocco_hour": 9,
                "snapshots": {"day": {
                    "age_hours": 24.3,
                    "counts": {"known_interactions": 99}
                }}
            },
        }
        summary = metrics.performance_snapshot({"posts": entries})
        self.assertEqual(summary["comparable_24h_samples"], 1)
        self.assertFalse(summary["can_recommend_slots"])


if __name__ == "__main__":
    unittest.main()
