import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import article_draft_publisher
import article_ai_processor
import article_processor
import article_queue
import content_filter
import runtime_state
from article_draft_publisher import _ensure_post_url_for_mode
from duplicate_utils import canonicalize_url, topic_signature
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

    def test_short_fast_article_is_accepted(self):
        article = {
            "url": "https://example.com/short",
            "source_published_at": recent_iso(1),
            "seo_title": "عنوان اختبار طويل بما يكفي للنشر",
            "seo_description": "وصف اختبار طويل بما يكفي حتى يمر شرط الوصف الخاص بالنشر.",
            "final_html": "<p>قصير جدا</p>",
        }
        article["final_html"] = "<p>" + " ".join(["خبر"] * 85) + "</p>"
        result = validate_before_publish(article, check_duplicate=False)
        self.assertTrue(result.passed, result.reason)

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

    def test_live_fresh_queue_effective_action(self):
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", False), patch.object(main, "RECENT_NEWS_ONLY", True):
            self.assertEqual(main._effective_action(), "LIVE_FRESH_QUEUE")

    def test_first_valid_article_mode_stops_after_first_valid_source(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            return [("Fresh story", f"{base_url}/story")], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", False), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
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

        with patch.object(scraper, "RECENT_NEWS_ONLY", False), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
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

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
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

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
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

    def test_startup_config_logs_live_fresh_queue_action(self):
        output = StringIO()
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", False), patch.object(main, "RECENT_NEWS_ONLY", True), redirect_stdout(output):
            main.print_startup_config()
        text = output.getvalue()
        self.assertIn("FRESH_QUEUE_MODE", text)
        self.assertIn("RECENT_NEWS_ONLY", text)
        self.assertIn("LIVE_FRESH_QUEUE", text)

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
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", False), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only") as score, patch.object(main, "notify_auto_cycle_blocked"):
            result = main.run_safe_cycle_only()
        self.assertFalse(result["completed"])
        self.assertEqual(result["step_reached"], "fetch")
        score.assert_not_called()

    def test_multiple_fresh_articles_found_in_one_scan_are_queued(self):
        def fake_collect(base_url, **_kwargs):
            return [
                {"title": f"{base_url} first", "url": f"{base_url}/first", "published_at": recent_iso(1.5)},
                {"title": f"{base_url} second", "url": f"{base_url}/second", "published_at": recent_iso(1)},
            ], "", 200, {"method_used": "feed"}

        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "ALLOW_UNKNOWN_DATE_IN_FAST_MODE", False), patch.object(scraper, "source_crawl_record", return_value={}):
                result = scraper.discover_fresh_article_links(
                    [
                        {"name": "A", "base_url": "https://a.example", "enabled": True},
                        {"name": "B", "base_url": "https://b.example", "enabled": True},
                    ],
                    existing_articles=[],
                    published_urls=set(),
                )
                queue_stats = article_queue.add_articles_to_queue(result["articles"])
                reloaded = article_queue.load_article_queue()

        self.assertEqual(len(result["articles"]), 4)
        self.assertEqual(queue_stats["added"], 4)
        self.assertEqual(len(reloaded["articles"]), 4)

    def test_oldest_fresh_queue_article_selected_first_and_next_run_gets_next(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "older",
                        "url": "https://example.com/older",
                        "status": "ready",
                        "content_fetch_status": "success",
                        "source_published_at": recent_iso(1.8),
                    },
                    {
                        "id": "newer",
                        "url": "https://example.com/newer",
                        "status": "ready",
                        "content_fetch_status": "success",
                        "source_published_at": recent_iso(0.8),
                    },
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path):
                article_queue.save_article_queue(queue)
                with patch.object(main, "suggest_category", return_value="Tech"):
                    first = main._select_oldest_fresh_ready_article()
                article_queue.archive_published_queue_article(article_id=first["id"], article_url=first["url"])
                with patch.object(main, "suggest_category", return_value="Tech"):
                    second = main._select_oldest_fresh_ready_article()

        self.assertEqual(first["id"], "older")
        self.assertEqual(second["id"], "newer")

    def test_expired_queued_articles_are_removed(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "expired",
                        "url": "https://example.com/expired",
                        "status": "ready",
                        "content_fetch_status": "success",
                        "source_published_at": recent_iso(7),
                    },
                    {
                        "id": "fresh",
                        "url": "https://example.com/fresh",
                        "status": "ready",
                        "content_fetch_status": "success",
                        "source_published_at": recent_iso(1),
                    },
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path):
                article_queue.save_article_queue(queue)
                stats = article_queue.archive_expired_queue_articles()
                reloaded = article_queue.load_article_queue()

        self.assertEqual(stats["expired_archived"], 1)
        expired = next(article for article in reloaded["articles"] if article["id"] == "expired")
        fresh = next(article for article in reloaded["articles"] if article["id"] == "fresh")
        self.assertTrue(expired["archived"])
        self.assertFalse(fresh.get("archived", False))

    def test_unknown_date_articles_are_skipped_in_fresh_queue_discovery(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Undated story", "url": f"{base_url}/story"}], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "ALLOW_UNKNOWN_DATE_IN_FAST_MODE", False), patch.object(scraper, "_resolve_article_published_at", return_value=("", "")), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_fresh_article_links(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
                published_urls=set(),
            )

        self.assertFalse(result["articles"])
        self.assertEqual(result["source_results"][0]["missing_date_skipped"], 1)

    def test_duplicate_urls_are_not_queued_twice(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path):
                queue_stats = article_queue.add_articles_to_queue(
                    [
                        {"title": "Story A", "url": "https://example.com/post?utm_source=x", "source_name": "A", "source_url": "https://example.com", "category_hint": "Tech"},
                        {"title": "Story A Again", "url": "https://example.com/post", "source_name": "A", "source_url": "https://example.com", "category_hint": "Tech"},
                    ]
                )
                reloaded = article_queue.load_article_queue()

        self.assertEqual(queue_stats["added"], 1)
        self.assertEqual(queue_stats["duplicate_url"], 1)
        self.assertEqual(len(reloaded["articles"]), 1)

    def test_published_topic_fingerprint_prevents_requeue(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Same Topic", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "source_crawl_record", return_value={}):
            result = scraper.discover_fresh_article_links(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
                published_urls=set(),
                published_topic_hashes={main.title_hash("Same Topic")},
            )

        self.assertFalse(result["articles"])

    def test_existing_crawl_state_uses_last_crawl_with_overlap_not_first_run_fallback(self):
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        last_crawled = now - timedelta(hours=6)
        expected = last_crawled - timedelta(minutes=10)

        with patch.object(scraper, "source_crawl_record", return_value={"last_crawled_at": last_crawled.isoformat().replace("+00:00", "Z")}), patch.object(scraper, "CRAWL_OVERLAP_MINUTES", 10), patch.object(scraper, "FALLBACK_FIRST_RUN_LOOKBACK_HOURS", 2):
            window = scraper._source_crawl_window_start("https://a.example", now=now)

        self.assertEqual(window, expected)

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
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", False), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value=enrich), patch.object(main, "_lock_specific_ready_article", return_value=None), patch.object(main, "run_plan_next_only") as planner, patch.object(main, "notify_auto_cycle_blocked"):
            result = main.run_safe_cycle_only()
        self.assertFalse(result["completed"])
        self.assertEqual(result["step_reached"], "plan-next")
        self.assertIn("not ready after enrichment", result["reason"])
        planner.assert_not_called()

    def test_fresh_queue_no_article_exits_successfully(self):
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "drafts_created_today": 0,
            "live_posts_created_today": 0,
            "max_drafts_per_day": 10,
            "max_live_posts_per_day": 288,
            "last_draft_time": None,
            "last_live_publish_time": None,
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": None,
            "min_minutes_between_drafts": 30,
            "min_minutes_between_live_posts": 5,
            "allowed_now": True,
            "next_allowed_time": None,
            "reasons": [],
        }
        fetch = {
            "first_valid_url": "",
            "reason": "no fresh article in the last 2 hours",
            "failed_sources": [],
            "zero_link_sources": [],
        }
        cleanup = {"expired_archived": 0, "missing_date_archived": 0}
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", False), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "archive_expired_queue_articles", return_value=cleanup), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value={"failed": 0, "weak": 0}), patch.object(main, "_select_oldest_fresh_ready_article", return_value=None), patch.object(main, "notify_auto_cycle_blocked"):
            result = main.run_safe_cycle_only()

        self.assertFalse(result["completed"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no fresh article in the last 2 hours")

    def test_lock_specific_ready_article_persists_selection(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "article-1",
                        "url": "https://example.com/fresh",
                        "status": "ready",
                        "content_fetch_status": "success",
                        "source_published_at": recent_iso(1),
                        "suggested_category": "",
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path):
                article_queue.save_article_queue(queue)
                with patch.object(main, "suggest_category", return_value="Cyber-Security"):
                    locked = main._lock_specific_ready_article("https://example.com/fresh")

                self.assertIsNotNone(locked)
                self.assertEqual(locked["status"], "selected")
                self.assertEqual(locked["selection_reason"], "first valid article fast mode")
                self.assertEqual(locked["suggested_category"], "Cyber-Security")
                self.assertTrue(locked.get("selected_at"))

                reloaded = article_queue.load_article_queue()
                persisted = reloaded["articles"][0]
                self.assertEqual(persisted["status"], "selected")
                self.assertEqual(persisted["selection_reason"], "first valid article fast mode")
                self.assertEqual(persisted["suggested_category"], "Cyber-Security")
                self.assertTrue(persisted.get("selected_at"))

    def test_auto_cycle_record_stays_success_when_blogger_publishes_and_facebook_fails(self):
        result = {
            "completed": True,
            "draft_action": "created",
            "article": {
                "id": "article-1",
                "title": "Fresh article",
                "publish_status": "published",
                "blogger_post_url": "https://example.com/post",
                "facebook_status": "failed",
            },
            "facebook": {
                "posted": False,
                "error": "Facebook token expired",
            },
            "step_reached": "publish",
        }

        record = main._auto_cycle_record_from_result("run-1", "2026-04-27T13:00:00", result)

        self.assertTrue(record["success"])
        self.assertEqual(record["blogger_status"], "published")
        self.assertEqual(record["facebook_status"], "failed")
        self.assertEqual(record["warning"], "Facebook token expired")
        self.assertEqual(record["stopped_reason"], "")

    def test_reset_state_clears_runtime_files(self):
        with TemporaryDirectory() as temp_dir:
            article_queue_path = Path(temp_dir) / "article_queue.json"
            backlog_path = Path(temp_dir) / "article_backlog.json"
            published_path = Path(temp_dir) / "published_ids.json"
            crawl_path = Path(temp_dir) / "crawl_state.json"
            topic_path = Path(temp_dir) / "topic_fingerprints.json"
            log_path = Path(temp_dir) / "auto_cycle_runs.jsonl"

            for path in (article_queue_path, backlog_path, published_path, crawl_path, topic_path, log_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            with patch.object(main, "ARTICLE_QUEUE_PATH", article_queue_path), patch.object(main, "ARTICLE_BACKLOG_PATH", backlog_path), patch.object(main, "PUBLISHED_DB_PATH", published_path), patch.object(main, "CRAWL_STATE_PATH", crawl_path), patch.object(main, "TOPIC_FINGERPRINTS_PATH", topic_path), patch.object(main, "AUTO_CYCLE_RUN_LOG", log_path), patch.object(article_queue, "ARTICLE_QUEUE_PATH", article_queue_path), patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path), patch.object(runtime_state, "TOPIC_FINGERPRINTS_PATH", topic_path):
                result = main.reset_runtime_state()

            self.assertTrue(result["ok"])
            self.assertTrue(article_queue_path.exists())
            self.assertFalse(backlog_path.exists())
            self.assertFalse(published_path.exists())
            self.assertTrue(crawl_path.exists())
            self.assertTrue(topic_path.exists())

    def test_facebook_default_caption_does_not_duplicate_comment_link(self):
        caption = _build_caption(
            {
                "seo_title": "اختبار منشور فيسبوك",
                "seo_description": "ملخص عربي مهني قصير لاختبار منشور فيسبوك.",
                "suggested_category": "Tech-News",
            },
            "insight_knowledge",
            blogger_url="https://example.com/post",
        )
        self.assertNotIn("https://example.com/post", caption)

    def test_freshness_safety_margin_skips_article_before_ai(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Almost expired", "url": f"{base_url}/story", "published_at": recent_iso(1.9)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_AI_ARTICLE_AGE_HOURS", 1.75), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(notifier, "send_telegram_message", return_value={"sent": False, "skipped": True}):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
            )

        self.assertFalse(result["first_valid"])
        self.assertEqual(result["source_results"][0]["too_close_links_skipped"], 1)

    def test_prepare_ai_rejects_empty_or_short_content(self):
        article = {"title": "Valid title", "url": "https://example.com/post", "content_preview": "short", "suggested_category": "Tech", "content_fetch_status": "success", "source_published_at": recent_iso(1)}
        missing = article_processor._validate_selected_article(article)
        self.assertIn("main content below 80 characters", missing)

    def test_weak_extracted_article_is_accepted_for_ai(self):
        article = {
            "title": "Valid title",
            "url": "https://example.com/post",
            "content_preview": "x" * 90,
            "suggested_category": "Tech",
            "content_fetch_status": "success",
            "source_published_at": recent_iso(1),
        }
        self.assertEqual(article_processor._validate_selected_article(article), [])

    def test_ai_provider_falls_back_after_primary_failure(self):
        with patch.object(article_ai_processor, "_resolve_providers", return_value=["gemini", "openrouter"]), patch.object(article_ai_processor, "_generate_with_gemini", side_effect=RuntimeError("provider down")), patch.object(article_ai_processor, "_generate_with_openrouter", return_value=('{"title":"x","description":"y","slug":"z","html_content":"<p>ok</p>"}', "openrouter:test")):
            raw, provider = article_ai_processor._generate_ai_article("prompt")
        self.assertIn("html_content", raw)
        self.assertEqual(provider, "openrouter:test")

    def test_auto_ai_sequence_is_gemini_openrouter_gemini(self):
        with patch.object(article_ai_processor, "_resolve_providers", return_value=["gemini", "openrouter"]), patch.object(article_ai_processor, "AI_PROVIDER", "auto"), patch.object(article_ai_processor, "MAX_AI_ATTEMPTS", 3):
            self.assertEqual(article_ai_processor._attempt_provider_sequence(), ["gemini", "openrouter", "gemini"])

    def test_basic_template_fallback_has_publishable_words(self):
        package = {
            "title": "Chrome fixes active zero-day vulnerability",
            "url": "https://example.com/chrome-zero-day",
            "source_published_at": recent_iso(1),
            "content_preview": "Google released an emergency Chrome update for an actively exploited security flaw. Users should install the latest browser update when available.",
            "source_name": "Example Source",
        }
        data = article_ai_processor._basic_fallback_article(package, error="both providers failed")
        self.assertGreaterEqual(article_ai_processor.html_word_count(data["html_content"]), 80)
        self.assertIn("<h2>", data["html_content"])

    def test_process_uses_template_fallback_when_both_ai_providers_fail(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "a1",
                        "url": "https://example.com/news",
                        "status": "selected",
                        "processing_status": "ready_for_ai",
                        "ai_input_package": {
                            "title": "Chrome fixes active zero-day vulnerability",
                            "url": "https://example.com/news",
                            "source_published_at": recent_iso(1),
                            "content_preview": "Google released an emergency Chrome update for an actively exploited security flaw. Users should install the latest browser update when available.",
                            "source_name": "Example Source",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini", "openrouter", "gemini"]), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=RuntimeError("provider failed")):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["article"]["ai_provider_used"], "basic-template-fallback")
        self.assertGreaterEqual(result["article"]["final_word_count"], 80)

    def test_ads_affiliate_articles_are_skipped(self):
        blocked, reason = content_filter.is_promotional_article(
            {"title": "Best VPN discount coupon deal", "url": "https://example.com/deals/best-vpn"}
        )
        self.assertTrue(blocked)
        self.assertTrue(reason)

    def test_cisa_bulletin_summary_is_not_false_positive_promo(self):
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Vulnerability Summary for the Week of April 20, 2026 | CISA",
                "url": "https://www.cisa.gov/news-events/bulletins/sb26-117",
                "content_preview": (
                    "The CISA Vulnerability Bulletin provides a summary of new vulnerabilities. "
                    "Entries may include additional information provided by organizations and "
                    "efforts sponsored by CISA."
                ),
            }
        )
        self.assertFalse(blocked, reason)

    def test_normal_business_deal_news_is_not_skipped(self):
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Meta inks deal for solar power beamed from space",
                "url": "https://techcrunch.com/2026/04/27/meta-solar-power-agreement",
                "content_preview": "The agreement would add renewable energy capacity for future data centers.",
            }
        )
        self.assertFalse(blocked, reason)

    def test_duplicate_topic_signature_blocks_repeated_story(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Google fixes Chrome zero-day CVE-2026-1234", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "source_crawl_record", return_value={}):
            result = scraper.discover_fresh_article_links(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
                published_urls=set(),
                published_topic_hashes={topic_signature("Chrome zero day CVE-2026-1234 patched by Google")},
            )

        self.assertFalse(result["articles"])

    def test_workflow_cron_is_every_five_minutes(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("timeout-minutes: 10", text)
        self.assertIn('"CATEGORY_ROTATION_MODE": "true"', text)
        self.assertIn('"MAX_SOURCES_PER_RUN": "999"', text)

    def test_articles_up_to_six_hours_are_accepted(self):
        with patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 6):
            is_recent, age = scraper._is_recent_published_at(recent_iso(5.5))
        self.assertTrue(is_recent)
        self.assertLess(age, 6)

    def test_category_rotation_state_advances(self):
        with TemporaryDirectory() as temp_dir:
            crawl_path = Path(temp_dir) / "crawl_state.json"
            with patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path):
                first = runtime_state.select_category_for_rotation(["Cyber-Security", "AI-Tools"])
                runtime_state.advance_category_rotation(first["category"], ["Cyber-Security", "AI-Tools"])
                second = runtime_state.select_category_for_rotation(["Cyber-Security", "AI-Tools"])

        self.assertEqual(first["category"], "Cyber-Security")
        self.assertEqual(second["category"], "AI-Tools")

    def test_category_rotation_fetch_processes_all_sources_in_category(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            return [{"title": f"Fresh {base_url}", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        sources = [
            {"name": "Cyber A", "base_url": "https://cyber-a.example", "enabled": True, "category_hint": "Cyber-Security", "category_label": "Cyber-Security", "fetch_limit_per_run": 3},
            {"name": "Cyber B", "base_url": "https://cyber-b.example", "enabled": True, "category_hint": "Cyber-Security", "category_label": "Cyber-Security", "fetch_limit_per_run": 3},
            {"name": "AI A", "base_url": "https://ai-a.example", "enabled": True, "category_hint": "AI-Tools", "category_label": "AI-Tools", "fetch_limit_per_run": 3},
        ]
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            crawl_path = Path(temp_dir) / "crawl_state.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "ARTICLE_QUEUE_PATH", queue_path), patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(main, "load_sources", return_value=sources), patch.object(main, "load_published_ids", return_value=set()), patch.object(main, "CATEGORY_ROTATION_MODE", True), patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True):
                result = main.run_fetch_only()

        self.assertEqual(result["selected_category"], "Cyber-Security")
        self.assertEqual(result["sources_checked"], 2)
        self.assertEqual(len(calls), 2)
        self.assertIn("https://cyber-a.example", calls)
        self.assertIn("https://cyber-b.example", calls)

    def test_blogger_labels_are_english_slugs(self):
        from article_selector import suggest_category

        self.assertEqual(
            suggest_category({"title": "Chrome zero-day vulnerability patched"}),
            "Cyber-Security",
        )

    def test_source_cooldown_after_three_failures(self):
        with TemporaryDirectory() as temp_dir:
            health_path = Path(temp_dir) / "source_health.json"
            with patch.object(runtime_state, "SOURCE_HEALTH_PATH", health_path), patch.object(runtime_state, "SOURCE_HEALTH_ENABLED", True), patch.object(runtime_state, "SOURCE_FAILURE_THRESHOLD", 3), patch.object(runtime_state, "SOURCE_FAILURE_COOLDOWN_MINUTES", 30):
                runtime_state.record_source_failure("https://bad.example", "Bad", "timeout")
                runtime_state.record_source_failure("https://bad.example", "Bad", "timeout")
                record = runtime_state.record_source_failure("https://bad.example", "Bad", "timeout")
                cooled, until = runtime_state.is_source_cooled_down("https://bad.example")

        self.assertEqual(record["failure_count"], 3)
        self.assertTrue(cooled)
        self.assertTrue(until)

    def test_telegram_success_summary_uses_short_arabic_format(self):
        result = {
            "completed": True,
            "article": {
                "id": "a1",
                "title": "Fresh story",
                "publish_status": "published",
                "blogger_post_url": "https://example.com/post",
                "facebook_status": "posted",
                "final_word_count": 180,
            },
            "draft_action": "created",
            "execution_seconds": 12.3,
        }
        with patch.object(notifier, "send_telegram_message", return_value={"sent": False, "skipped": True, "reason": "disabled"}) as send:
            notifier.notify_auto_cycle_summary(result, run_id="unit-success-format")
        message = send.call_args.args[0]
        self.assertIn("تم نشر مقال جديد", message)
        self.assertIn("Blogger URL:", message)

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
