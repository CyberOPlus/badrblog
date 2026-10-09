"""Weekly Morocco Facebook pilot and safe publisher rollout."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import job_core
import facebook_publisher as facebook
import jobs_adaptive_controller as adaptive
import main


TZ = ZoneInfo("Africa/Casablanca")


class WeeklyPilotTests(unittest.TestCase):
    def test_default_is_not_weekly_for_older_manual_environments(self):
        sample = datetime(2026, 10, 8, 2, 0, tzinfo=TZ)
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", False):
            self.assertTrue(job_core.facebook_slot_status(now=sample)["allowed_now"])
            self.assertEqual(job_core.facebook_slot_status(now=sample)["mode"], "immediate")

    def test_weekly_windows_include_mornings_and_evenings(self):
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True):
            for when in (
                datetime(2026, 10, 5, 9, 15, tzinfo=TZ),     # Monday
                datetime(2026, 10, 6, 13, 15, tzinfo=TZ),    # Tuesday
                datetime(2026, 10, 7, 18, 45, tzinfo=TZ),    # Wednesday
                datetime(2026, 10, 8, 9, 15, tzinfo=TZ),     # Thursday
                datetime(2026, 10, 9, 16, 15, tzinfo=TZ),    # Friday
                datetime(2026, 10, 10, 10, 15, tzinfo=TZ),   # Saturday
                datetime(2026, 10, 11, 18, 15, tzinfo=TZ),   # Sunday
            ):
                with self.subTest(when=when):
                    status = job_core.facebook_slot_status(now=when)
                    self.assertTrue(status["allowed_now"], status)
                    self.assertEqual(status["mode"], "weekly")

    def test_overnight_pending_jobs_wait_for_next_weekly_window(self):
        now = datetime(2026, 10, 9, 22, 0, tzinfo=TZ)
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True):
            status = job_core.facebook_slot_status(now=now)
        self.assertFalse(status["allowed_now"])
        self.assertEqual(datetime.fromisoformat(status["next_slot"]).astimezone(TZ),
                         datetime(2026, 10, 10, 10, 0, tzinfo=TZ))

    def test_deadline_urgent_bypasses_engagement_window_not_daily_limits(self):
        now = datetime(2026, 10, 8, 16, 0, tzinfo=TZ)
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True):
            self.assertFalse(job_core.facebook_slot_status(now=now)["allowed_now"])
            self.assertTrue(job_core.facebook_slot_status(now=now, urgent=True)["allowed_now"])
        rows = [{"facebook_status": "posted", "facebook_posted_at": (now - timedelta(hours=i+1)).isoformat()}
                for i in range(4)]
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}), \
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 4):
            self.assertFalse(facebook.get_facebook_limits_status(now=now, urgent=True)["allowed_now"])

    def test_weekend_facebook_cap_is_two(self):
        now = datetime(2026, 10, 10, 18, 15, tzinfo=TZ)
        rows = [
            {"facebook_status": "posted", "facebook_posted_at": (now - timedelta(hours=i+1)).isoformat()}
            for i in range(2)
        ]
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 4), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            status = facebook.get_facebook_limits_status(now=now)
        self.assertEqual(status["max_facebook_posts_per_day"], 2)
        self.assertFalse(status["allowed_now"])

    def test_facebook_spacing_never_allows_burst_inside_slot(self):
        now = datetime(2026, 10, 8, 9, 20, tzinfo=TZ)
        rows = [{"facebook_status": "posted", "facebook_posted_at": (now - timedelta(minutes=15)).isoformat()}]
        with patch.object(job_core, "JOBS_WEEKLY_FACEBOOK_SCHEDULE_ENABLED", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 4), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 70), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            status = facebook.get_facebook_limits_status(now=now)
        self.assertFalse(status["allowed_now"])
        self.assertIn("safety interval", " ".join(status["reasons"]))

    def test_adaptive_weekday_and_weekend_blog_ceiling(self):
        with patch.object(job_core, "JOBS_ADAPTIVE_PUBLISHING", True), \
             patch.object(job_core, "current_policy", return_value={"daily_cap": 12}):
            self.assertEqual(job_core.daily_publish_cap(now=datetime(2026, 10, 8, 15, tzinfo=TZ)), 12)
            self.assertEqual(job_core.daily_publish_cap(now=datetime(2026, 10, 10, 15, tzinfo=TZ)), 8)

    def test_health_adaptation_starts_at_eight_not_hundreds(self):
        state = {"version": 1, "current_day": "2026-10-09", "days": {}, "green_score": 0}
        with patch.object(adaptive, "load_state", return_value=state), \
             patch.object(adaptive, "save_state"), \
             patch.object(adaptive, "JOBS_ADAPTIVE_PUBLISHING", True), \
             patch.object(adaptive, "JOBS_ADAPTIVE_MIN_DAILY_CAP", 8), \
             patch.object(adaptive, "JOBS_ADAPTIVE_MAX_DAILY_CAP", 12):
            result = adaptive.current_policy(now=datetime(2026, 10, 9, 14, tzinfo=TZ))
            state["green_score"] = 12
            healthy = adaptive.current_policy(now=datetime(2026, 10, 9, 14, tzinfo=TZ))
        self.assertEqual(result["daily_cap"], 8)
        self.assertEqual(healthy["daily_cap"], 12)

    def test_one_day_rollout_grace_prevents_freezing_current_published_day(self):
        today = datetime(2026, 10, 9, 16, 0, tzinfo=TZ)
        tomorrow = datetime(2026, 10, 10, 16, 0, tzinfo=TZ)
        state = {"daily_publish_count": {"2026-10-09": 9, "2026-10-10": 0},
                 "last_publish_at": "2026-10-09T10:54:10+00:00"}
        with patch.dict(os.environ, {
            "JOBS_PILOT_ROLLOUT_GRACE_DAY": "2026-10-09",
            "JOBS_PILOT_ROLLOUT_GRACE_CAP": "12",
        }), patch.object(job_core, "JOBS_ADAPTIVE_PUBLISHING", True), \
             patch.object(job_core, "current_policy", return_value={"daily_cap": 8}), \
             patch.object(job_core, "load_job_state", return_value=state):
            today_window = job_core.job_publish_window_status(now=today, publishable_backlog=1)
            tomorrow_cap = job_core.daily_publish_cap(now=tomorrow)
        self.assertTrue(today_window["allowed_now"], today_window)
        self.assertEqual(today_window["daily_cap"], 12)
        self.assertEqual(today_window["published_today"], 9)
        self.assertEqual(tomorrow_cap, 8)

    def test_non_english_slug_retry_is_cleanly_deferred(self):
        row = {"permalink_attempt": 1}
        self.assertTrue(main._is_retryable_publish_deferral(
            "Blogger generated a non-English Jobs permalink; it was deleted and will retry on the next cycle.",
            row,
        ))


if __name__ == "__main__":
    unittest.main()
