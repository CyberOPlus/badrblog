import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import article_draft_publisher
from article_draft_publisher import _ensure_post_url_for_mode
from duplicate_utils import canonicalize_url
from facebook_publisher import _build_caption, _eligible_for_facebook
import main
import notifier
import scraper
from production_logging import _clean_value
from quality_gate import duplicate_publish_reason, validate_before_publish


def long_arabic_html(word="اختبار", cyber=False):
    intro = " ".join([word] * 110)
    body = " ".join([word] * 260)
    reader = " ".join([word] * 190)
    protection = " ".join([word] * 170)
    conclusion = " ".join([word] * 130)
    protection_section = (
        f"<h2>كيف تحمي نفسك</h2><p>{protection}</p>"
        if cyber
        else ""
    )
    return (
        f"<p>{intro}</p>"
        f"<h2>الشرح الرئيسي</h2><p>{body}</p>"
        f"<h2>ماذا يعني هذا لك؟</h2><p>{reader}</p>"
        f"{protection_section}"
        f"<h2>الخلاصة</h2><p>{conclusion}</p>"
    )


def recent_iso(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class ProductionHardeningTests(unittest.TestCase):
    def test_fast_news_mode_accepts_120_plus_words(self):
        article = {
            "url": "https://example.com/recent",
            "source_published_at": recent_iso(1),
            "seo_title": "عنوان اختبار طويل بما يكفي للنشر",
            "seo_description": "وصف اختبار طويل بما يكفي حتى يمر شرط الوصف الخاص بالنشر.",
            "final_html": "<p>" + " ".join(["خبر"] * 160) + "</p>",
        }
        result = validate_before_publish(article, check_duplicate=False, fast_news_mode=True)
        self.assertTrue(result.passed, result.reason)

    def test_long_article_mode_rejects_under_800_words(self):
        article = {
            "seo_title": "عنوان اختبار طويل بما يكفي للنشر",
            "seo_description": "وصف اختبار طويل بما يكفي حتى يمر شرط الوصف الخاص بالنشر.",
            "final_html": long_arabic_html(),
        }
        result = validate_before_publish(article, check_duplicate=False, fast_news_mode=False)
        self.assertFalse(result.passed)
        self.assertIn("too short", result.reason)

    def test_short_article_rejected(self):
        article = {
            "url": "https://example.com/short",
            "source_published_at": recent_iso(1),
            "seo_title": "عنوان اختبار طويل بما يكفي للنشر",
            "seo_description": "وصف اختبار طويل بما يكفي حتى يمر شرط الوصف الخاص بالنشر.",
            "final_html": "<p>قصير جدا</p>",
        }
        result = validate_before_publish(article, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("too short", result.reason)

    def test_missing_blogger_url_rejected_for_live_publish(self):
        with patch.object(article_draft_publisher, "SAFE_MODE", False), patch.object(article_draft_publisher, "PUBLISH_MODE", "live"):
            with self.assertRaises(RuntimeError):
                _ensure_post_url_for_mode({"id": "123", "url": ""}, "live")

    def test_duplicate_canonical_url_detection(self):
        article = {
            "id": "new",
            "url": "https://Example.com/news/story/?utm_source=x&fbclid=1",
            "seo_title": "عنوان اختبار طويل بما يكفي للنشر",
            "seo_description": "وصف اختبار طويل بما يكفي حتى يمر شرط الوصف الخاص بالنشر.",
            "final_html": long_arabic_html(),
        }
        existing = [
            {
                "id": "old",
                "url": "https://example.com/news/story",
                "publish_status": "published",
            }
        ]
        self.assertEqual(
            canonicalize_url(article["url"]),
            "https://example.com/news/story",
        )
        self.assertIn("canonical URL", duplicate_publish_reason(article, existing))

    def test_facebook_requires_successful_blogger_publish(self):
        self.assertFalse(
            _eligible_for_facebook(
                {
                    "status": "published",
                    "publish_status": "failed",
                    "blogger_post_url": "https://example.com/post",
                }
            )
        )

    def test_safe_mode_effective_action_is_draft(self):
        with patch.object(main, "SAFE_MODE", True), patch.object(main, "PUBLISH_MODE", "live"):
            self.assertEqual(main._effective_action(), "DRAFT")

    def test_live_fast_recent_effective_action(self):
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True):
            self.assertEqual(main._effective_action(), "LIVE_FAST_RECENT_NEWS")

    def test_first_valid_article_mode_stops_after_first_valid_source(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            return [("Fresh story", f"{base_url}/story")], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", False), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [
                    {"name": "A", "base_url": "https://a.example", "enabled": True},
                    {"name": "B", "base_url": "https://b.example", "enabled": True},
                ],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(calls, ["https://a.example"])

    def test_403_source_is_skipped_fast_to_next_source(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            if "blocked" in base_url:
                return [], "http 403", 403, {"method_used": "failed"}
            return [("Fresh story", f"{base_url}/story")], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", False), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [
                    {"name": "Blocked", "base_url": "https://blocked.example", "enabled": True},
                    {"name": "Good", "base_url": "https://good.example", "enabled": True},
                ],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(calls, ["https://blocked.example", "https://good.example"])
        self.assertFalse(
            _eligible_for_facebook(
                {
                    "status": "published",
                    "publish_status": "published",
                    "blogger_post_url": "",
                }
            )
        )

    def test_recent_article_is_accepted(self):
        published_at = recent_iso(1)
        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2):
            is_recent, age = scraper._is_recent_published_at(published_at)
        self.assertTrue(is_recent)
        self.assertLess(age, 2)

    def test_article_older_than_two_hours_is_skipped(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Old story", "url": f"{base_url}/story", "published_at": recent_iso(3)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
            )

        self.assertFalse(result["first_valid"])
        self.assertIn("last 2 hours", result["reason"])
        self.assertEqual(result["source_results"][0]["old_links_skipped"], 1)

    def test_missing_date_is_skipped_when_strict_recent_mode_enabled(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Undated story", "url": f"{base_url}/story"}], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "ALLOW_UNKNOWN_DATE_IN_FAST_MODE", False), patch.object(scraper, "_resolve_article_published_at", return_value=("", "")), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
            )

        self.assertFalse(result["first_valid"])
        self.assertEqual(result["source_results"][0]["missing_date_skipped"], 1)

    def test_first_valid_recent_article_stops_source_scanning(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            return [{"title": "Recent story", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [
                    {"name": "The Hacker News", "base_url": "https://first.example", "enabled": True},
                    {"name": "BleepingComputer", "base_url": "https://second.example", "enabled": True},
                ],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(calls, ["https://first.example"])

    def test_live_fast_mode_does_not_create_drafts(self):
        with patch.object(article_draft_publisher, "SAFE_MODE", False), patch.object(article_draft_publisher, "PUBLISH_MODE", "live"):
            self.assertEqual(article_draft_publisher._effective_publish_mode("live"), "live")

    def test_startup_config_logs_live_fast_recent_action(self):
        output = StringIO()
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), redirect_stdout(output):
            main.print_startup_config()
        text = output.getvalue()
        self.assertIn("RECENT_NEWS_ONLY", text)
        self.assertIn("LIVE_FAST_RECENT_NEWS", text)

    def test_telegram_reports_skipped_when_no_recent_article(self):
        with patch.object(notifier, "send_telegram_message", return_value={"sent": False, "skipped": True, "reason": "disabled"}) as send:
            notifier.notify_auto_cycle_summary(
                {"completed": False, "reason": "no article in last 2 hours", "source_warnings_count": 1},
                run_id="unit-test-no-recent",
            )
        message = send.call_args.args[0]
        self.assertIn("no article in last 2 hours", message)

    def test_live_fast_recent_stops_before_old_queue_fallback(self):
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "drafts_created_today": 0,
            "live_posts_created_today": 0,
            "max_drafts_per_day": 10,
            "max_live_posts_per_day": 5,
            "last_draft_time": None,
            "last_live_publish_time": None,
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": None,
            "min_minutes_between_drafts": 30,
            "min_minutes_between_live_posts": 60,
            "allowed_now": True,
            "next_allowed_time": None,
            "reasons": [],
        }
        fetch = {
            "first_valid_url": "",
            "reason": "no article in last 2 hours",
            "failed_sources": [],
            "zero_link_sources": [],
        }
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only") as score, patch.object(main, "notify_auto_cycle_blocked"):
            result = main.run_safe_cycle_only()
        self.assertFalse(result["completed"])
        self.assertEqual(result["step_reached"], "fetch")
        score.assert_not_called()

    def test_live_fast_recent_stops_when_fresh_article_is_not_ready(self):
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "drafts_created_today": 0,
            "live_posts_created_today": 0,
            "max_drafts_per_day": 10,
            "max_live_posts_per_day": 144,
            "last_draft_time": None,
            "last_live_publish_time": None,
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": None,
            "min_minutes_between_drafts": 30,
            "min_minutes_between_live_posts": 10,
            "allowed_now": True,
            "next_allowed_time": None,
            "reasons": [],
        }
        fetch = {
            "first_valid_url": "https://example.com/fresh",
            "reason": "",
            "failed_sources": [],
            "zero_link_sources": [],
        }
        enrich = {"failed": 0, "weak": 0}
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value=enrich), patch.object(main, "_lock_specific_ready_article", return_value=None), patch.object(main, "run_plan_next_only") as planner, patch.object(main, "notify_auto_cycle_blocked"):
            result = main.run_safe_cycle_only()
        self.assertFalse(result["completed"])
        self.assertEqual(result["step_reached"], "plan-next")
        self.assertIn("not ready after enrichment", result["reason"])
        planner.assert_not_called()

    def test_facebook_default_caption_does_not_duplicate_comment_link(self):
        caption = _build_caption(
            {
                "seo_title": "اختبار منشور فيسبوك",
                "seo_description": "ملخص عربي مهني قصير لاختبار منشور فيسبوك.",
                "suggested_category": "أخبار التقنية",
            },
            "insight_knowledge",
            blogger_url="https://example.com/post",
        )
        self.assertNotIn("https://example.com/post", caption)

    def test_secret_redaction(self):
        cleaned = _clean_value(
            {
                "api_key": "abc123",
                "refresh_token": "refresh-secret",
                "headers": {"Authorization": "Bearer token-secret"},
                "url": "https://example.com/?access_token=token-secret",
            }
        )
        self.assertNotIn("abc123", cleaned)
        self.assertNotIn("refresh-secret", cleaned)
        self.assertNotIn("token-secret", cleaned)
        self.assertIn("[redacted]", cleaned)


if __name__ == "__main__":
    unittest.main()
