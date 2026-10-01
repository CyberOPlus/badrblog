import json
import re
import subprocess
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import article_draft_publisher
import article_enricher
import article_ai_processor
import article_processor
import article_queue
import article_scorer
import blogger_client
import content_filter
import facebook_publisher
import internal_link_cache
import utils.facebook_image_generator as facebook_image_generator
import runtime_state
import source_sanitizer
from article_draft_publisher import _ensure_post_url_for_mode
from bs4 import BeautifulSoup
from duplicate_utils import canonicalize_url, topic_signature
from facebook_publisher import _build_caption, _eligible_for_facebook
import main
import scraper
from production_logging import _clean_value
from quality_gate import QualityGateResult, duplicate_publish_reason, validate_before_publish


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

    def test_non_technical_entertainment_article_is_skipped(self):
        from content_filter import is_non_technical_entertainment_article

        article = {
            "title": "4 classic Oscar-winning movies you can stream on Netflix today",
            "rss_summary": "A list of films to watch this week.",
            "source_name": "How-To Geek",
        }
        blocked, reason = is_non_technical_entertainment_article(article)
        self.assertTrue(blocked)
        self.assertEqual(reason, "non_technical_entertainment_content")
        self.assertEqual(article_scorer.score_article(article), 0)

    def test_netflix_security_article_is_allowed(self):
        from content_filter import is_non_technical_entertainment_article

        article = {
            "title": "Netflix account protection update improves privacy and security",
            "rss_summary": "The app update adds account protection and privacy controls.",
        }
        blocked, reason = is_non_technical_entertainment_article(article)
        self.assertFalse(blocked, reason)

    def test_facebook_caption_uses_variable_cta_and_three_to_six_hashtags(self):
        article = {
            "id": "fb1",
            "title": "Google تضيف ميزة AI جديدة لحماية Android من Malware",
            "seo_description": "توضح Google أن الميزة الجديدة تستخدم AI لتحليل السلوك المشبوه على Android وتقليل مخاطر Malware قبل وصولها إلى المستخدم.",
            "final_html": "<p>توضح Google أن الميزة الجديدة تستخدم AI لتحليل السلوك المشبوه على Android وتقليل مخاطر Malware قبل وصولها إلى المستخدم.</p>",
            "suggested_category": "Cyber-Security",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.com/post",
        }
        blueprint = facebook_publisher._prepare_facebook_post(article, [article], article["blogger_post_url"])
        self.assertIn("أول تعليق", blueprint["caption"])
        self.assertNotIn("https://example.com/post", blueprint["caption"])
        hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", blueprint["caption"], flags=re.UNICODE)
        self.assertGreaterEqual(len(hashtags), 3)
        self.assertLessEqual(len(hashtags), 6)

    def test_facebook_preview_uses_fallback_when_validation_fails(self):
        article = {
            "id": "fb-preview",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.com/post",
            "title": "Netflix movies with too much entertainment English",
            "seo_description": "English entertainment text that would fail validation.",
            "suggested_category": "Apps-Programs",
        }
        queue = {"articles": [article], "notifications": {}}
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(facebook_publisher, "_prepare_facebook_post", side_effect=RuntimeError("bad caption")):
                article_queue.save_article_queue(queue)
                preview = facebook_publisher.preview_next_facebook_post()

        self.assertTrue(preview["available"])
        self.assertEqual(preview["preview_status"], "fallback_used")
        self.assertIn("أول تعليق", preview["post_text"])

    def test_blogger_image_alt_is_single_clean_attribute(self):
        html = (
            "<p>مقدمة المقال</p>"
            "<p><img class='full' alt='قديم' alt='مكرر' src='https://example.com/old.jpg'/></p>"
            "<p>باقي المقال</p>"
        )
        article = {
            "title": "Google تضيف حماية جديدة إلى Android",
            "content": html,
            "image": {"url": "https://example.com/main.jpg", "alt": ""},
        }
        final_html = blogger_client._finalize_article_content(article)
        self.assertEqual(final_html.count("<img"), 1)
        self.assertEqual(final_html.count("alt="), 1)
        self.assertIn("https://example.com/main.jpg", final_html)
        self.assertIn("Google تضيف حماية جديدة إلى Android", final_html)

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

    def test_article_older_than_seven_days_is_skipped(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Old story", "url": f"{base_url}/story", "published_at": recent_iso(169)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
        )

        self.assertFalse(result["first_valid"])
        self.assertIn("under 7 days", result["reason"])
        self.assertEqual(result["source_results"][0]["old_links_skipped"], 1)

    def test_stale_but_under_seven_days_article_is_accepted_without_strict_freshness(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            if "older" in base_url:
                return [{"title": "Expanded story", "url": f"{base_url}/story", "published_at": recent_iso(7)}], "", 200, {"method_used": "feed"}
            return [{"title": "Fresh story", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [
                    {"name": "Older", "base_url": "https://older.example", "enabled": True},
                    {"name": "Fresh", "base_url": "https://fresh.example", "enabled": True},
                ],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(calls, ["https://older.example"])
        self.assertEqual(result["articles"][0]["title"], "Expanded story")
        self.assertEqual(result["articles"][0]["freshness_window_hours"], 168)

    def test_missing_date_uses_new_url_fallback_when_strict_recent_mode_enabled(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Undated story", "url": f"{base_url}/story"}], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "ALLOW_UNKNOWN_DATE_IN_FAST_MODE", False), patch.object(scraper, "_resolve_article_published_at", return_value=("", "")), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(result["articles"][0]["freshness_source"], "fallback_no_date")
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
            "reason": "no new publishable article under 7 days",
            "failed_sources": [],
            "zero_link_sources": [],
        }
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", False), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only") as score:
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
                        "source_published_at": recent_iso(169),
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

    def test_unknown_date_articles_are_queued_with_fallback_in_fresh_discovery(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Undated story", "url": f"{base_url}/story"}], "", 200, {"method_used": "html"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "ALLOW_UNKNOWN_DATE_IN_FAST_MODE", False), patch.object(scraper, "_resolve_article_published_at", return_value=("", "")), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_fresh_article_links(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
                published_urls=set(),
            )

        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0]["freshness_source"], "fallback_no_date")
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
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", False), patch.object(main, "FIRST_VALID_ARTICLE_MODE", True), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value=enrich), patch.object(main, "_lock_specific_ready_article", return_value=None), patch.object(main, "run_plan_next_only") as planner:
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
            "reason": "no new publishable article under 7 days",
            "failed_sources": [],
            "zero_link_sources": [],
        }
        cleanup = {"expired_archived": 0, "missing_date_archived": 0}
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FAST_NEWS_MODE", True), patch.object(main, "CATEGORY_ROTATION_MODE", False), patch.object(main, "FRESH_QUEUE_MODE", True), patch.object(main, "FIRST_VALID_ARTICLE_MODE", False), patch.object(main, "RECENT_NEWS_ONLY", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value=fetch), patch.object(main, "archive_expired_queue_articles", return_value=cleanup), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value={"failed": 0, "weak": 0}), patch.object(main, "_select_oldest_fresh_ready_article", return_value=None):
            result = main.run_safe_cycle_only()

        self.assertFalse(result["completed"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no new publishable article under 7 days")

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
            "tech_news",
            blogger_url="https://example.com/post",
        )
        self.assertNotIn("https://example.com/post", caption)
        self.assertIn("أول تعليق", caption)

    def test_facebook_hashtags_count_stays_between_three_and_six(self):
        blueprint = facebook_publisher._build_post_blueprint(
            {
                "seo_title": "OpenAI launches new productivity workflow for enterprise teams",
                "seo_description": "A practical update focused on team productivity, automation, and faster daily workflows.",
                "suggested_category": "AI-Tools",
                "content_preview": "The tool helps teams automate repeated work and shorten delivery time.",
            },
            style="ai_tools",
        )
        hashtags = re.findall(r"#[\w\u0600-\u06FF_]+", blueprint["caption"], flags=re.UNICODE)
        self.assertGreaterEqual(len(hashtags), 3)
        self.assertLessEqual(len(hashtags), 6)

    def test_facebook_hook_is_not_equal_to_title(self):
        article = {
            "seo_title": "Microsoft launches new Windows security update",
            "seo_description": "The latest update fixes a high-risk issue and changes the protection flow for users.",
            "suggested_category": "Cyber-Security",
            "content_preview": "Users should apply the update quickly to reduce the exposure window.",
        }
        blueprint = facebook_publisher._build_post_blueprint(article, style="cybersecurity")
        self.assertNotEqual(
            facebook_publisher._normalize_memory_text(blueprint["hook"]),
            facebook_publisher._normalize_memory_text(article["seo_title"]),
        )

    def test_facebook_style_rotation_avoids_third_repeat(self):
        with TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "facebook_style_memory.json"
            memory_path.write_text(
                json.dumps(
                    {
                        "global_styles": ["tech_news", "tech_news"],
                        "recent": {"Tech-News": ["tech_news"]},
                        "recent_hooks": [],
                        "recent_structures": [],
                        "stats": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(facebook_publisher, "FACEBOOK_STYLE_MEMORY_PATH", memory_path):
                pattern = facebook_publisher._choose_caption_pattern(
                    {"suggested_category": "Tech-News"},
                    [],
                )

        self.assertNotEqual(pattern, "tech_news")

    def test_default_openrouter_fallback_list_is_available(self):
        import config

        self.assertEqual(
            config.FAST_OPENROUTER_MODELS,
            ["openrouter/free"],
        )
        self.assertEqual(config.OPENROUTER_MODELS, config.FAST_OPENROUTER_MODELS)

    def test_github_actions_facebook_image_dependencies_are_declared(self):
        requirements_text = Path("requirements.txt").read_text(encoding="utf-8")
        workflow_text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8-sig")

        self.assertRegex(requirements_text, r"(?im)^\s*Pillow\b")
        self.assertIn("pip install -r requirements.txt", workflow_text)
        self.assertTrue(Path("assets/facebook_template.png").exists())
        self.assertTrue(Path("assets/fallback_article.png").exists())

    def test_facebook_image_generator_uses_fallback_image(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in this local environment")

        with TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            template = temp_dir / "facebook_template.png"
            fallback = temp_dir / "fallback_article.png"
            output = temp_dir / "out.jpg"
            Image.new("RGB", (1080, 1080), (20, 20, 30)).save(template)
            Image.new("RGB", (600, 400), (80, 120, 180)).save(fallback)

            with patch.object(facebook_image_generator, "FACEBOOK_IMAGE_TEMPLATE_PATH", template), patch.object(facebook_image_generator, "FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH", fallback), patch.object(facebook_image_generator, "FACEBOOK_IMAGE_OUTPUT_DIR", temp_dir):
                result = facebook_image_generator.generate_facebook_image(
                    "اختبار صورة فيسبوك",
                    "https://invalid.example/missing.jpg",
                    output,
                )
                output_exists = output.exists()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["used_fallback"])
        self.assertTrue(output_exists)

    def test_facebook_image_generator_accepts_unexpected_template_size(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in this local environment")

        with TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            template = temp_dir / "facebook_template.png"
            fallback = temp_dir / "fallback_article.png"
            output = temp_dir / "out.jpg"
            Image.new("RGBA", (1122, 1402), (20, 20, 30, 90)).save(template)
            Image.new("RGB", (900, 600), (80, 120, 180)).save(fallback)

            with patch.object(facebook_image_generator, "FACEBOOK_IMAGE_TEMPLATE_PATH", template), patch.object(facebook_image_generator, "FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH", fallback), patch.object(facebook_image_generator, "FACEBOOK_IMAGE_OUTPUT_DIR", temp_dir):
                result = facebook_image_generator.generate_facebook_image(
                    "اختبار قالب مختلف الحجم",
                    "",
                    output,
                    hook_text="عنوان عربي قصير فوق صورة المقال",
                )
                output_exists = output.exists()

        self.assertTrue(result["ok"], result)
        self.assertTrue(output_exists)

    def test_facebook_image_generator_works_without_template_or_fallback_asset(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed in this local environment")

        with TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            output = temp_dir / "out.jpg"
            missing_template = temp_dir / "missing_template.png"
            missing_fallback = temp_dir / "missing_fallback.png"

            with patch.object(facebook_image_generator, "FACEBOOK_IMAGE_TEMPLATE_PATH", missing_template), patch.object(facebook_image_generator, "FACEBOOK_FALLBACK_ARTICLE_IMAGE_PATH", missing_fallback), patch.object(facebook_image_generator, "FACEBOOK_IMAGE_OUTPUT_DIR", temp_dir):
                result = facebook_image_generator.generate_facebook_image(
                    "Android security update",
                    "",
                    output,
                    hook_text="تنبيه تقني مهم لمستخدمي Android",
                )
                output_exists = output.exists()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["used_fallback"])
        self.assertTrue(output_exists)

    def test_freshness_safety_margin_does_not_block_under_seven_days(self):
        def fake_collect(base_url, **_kwargs):
            return [{"title": "Almost expired", "url": f"{base_url}/story", "published_at": recent_iso(5.95)}], "", 200, {"method_used": "feed"}

        with patch.object(scraper, "RECENT_NEWS_ONLY", True), patch.object(scraper, "RECENT_NEWS_MAX_AGE_HOURS", 2), patch.object(scraper, "MAX_AI_ARTICLE_AGE_HOURS", 1.75), patch.object(scraper, "MAX_SOURCES_PER_RUN", 0), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect):
            result = scraper.discover_first_valid_article_link(
                [{"name": "A", "base_url": "https://a.example", "enabled": True}],
                existing_articles=[],
            )

        self.assertTrue(result["first_valid"])
        self.assertEqual(result["source_results"][0]["too_close_links_skipped"], 0)

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
        with patch.object(article_ai_processor, "_resolve_providers", return_value=["gemini", "openrouter"]), patch.object(article_ai_processor, "_preferred_provider", return_value=""), patch.object(article_ai_processor, "AI_PROVIDER", "auto"), patch.object(article_ai_processor, "MAX_AI_ATTEMPTS", 3):
            self.assertEqual(article_ai_processor._attempt_provider_sequence(), ["gemini", "openrouter"])

    def test_ai_cooldown_memory_persists_without_secret(self):
        with TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "ai_provider_memory.json"
            candidate = {"provider": "openrouter", "model": "test/free", "api_key": "sk-test-secret-value"}
            with patch.object(article_ai_processor, "AI_PROVIDER_MEMORY_PATH", memory_path), patch.object(article_ai_processor, "_AI_MEMORY_CACHE", None), patch.object(article_ai_processor, "_AI_COOLDOWNS", {}):
                article_ai_processor._put_candidate_on_cooldown(candidate, RuntimeError("quota sk-test-secret-value"))
                self.assertGreater(article_ai_processor._cooldown_remaining(candidate), 0)
                data = json.loads(memory_path.read_text(encoding="utf-8"))

        self.assertIn("openrouter:test/free", next(iter(data["cooldowns"])))
        self.assertNotIn("sk-test-secret-value", json.dumps(data))

    def test_ai_speed_memory_tracks_fastest_success_model(self):
        with TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "ai_provider_memory.json"
            fast_candidate = {"provider": "openrouter", "model": "openai/gpt-oss-20b:free", "api_key": "sk-fast"}
            slow_candidate = {"provider": "gemini", "model": "gemini-2.5-flash", "api_key": "sk-gemini"}
            with patch.object(article_ai_processor, "AI_PROVIDER_MEMORY_PATH", memory_path), patch.object(article_ai_processor, "_AI_MEMORY_CACHE", None):
                article_ai_processor._record_candidate_success(slow_candidate, elapsed_seconds=8.0)
                article_ai_processor._record_candidate_success(fast_candidate, elapsed_seconds=3.0)
                data = json.loads(memory_path.read_text(encoding="utf-8"))

        self.assertEqual(data["fastest_success_model"], "openrouter:openai/gpt-oss-20b:free")
        self.assertEqual(data["avg_time"], 3.0)

    def test_openrouter_candidates_use_fast_models_only(self):
        with patch.object(article_ai_processor, "OPENROUTER_API_KEY", "sk-fast"), patch.object(
            article_ai_processor,
            "OPENROUTER_MODELS",
            [
                "openai/gpt-oss-120b:free",
                "inclusionai/ling-2.6-flash:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
                "openai/gpt-oss-20b:free",
            ],
        ), patch.object(article_ai_processor, "OPENROUTER_MODEL", "openai/gpt-oss-120b:free"):
            candidates = article_ai_processor._openrouter_candidates()

        self.assertEqual(
            [candidate["model"] for candidate in candidates],
            ["inclusionai/ling-2.6-flash:free", "openai/gpt-oss-20b:free"],
        )

    def test_gemini_timeout_uses_15_seconds(self):
        class FakeResponse:
            text = '{"title":"x","description":"y","slug":"z","html_content":"<p>ok</p>"}'

        class FakeModel:
            def __init__(self, _name):
                self.request_options = None

            def generate_content(self, _prompt, request_options=None):
                self.request_options = request_options
                return FakeResponse()

        fake_model = FakeModel("gemini-2.5-flash")
        fake_genai = type(
            "FakeGenAI",
            (),
            {
                "configure": staticmethod(lambda **_kwargs: None),
                "GenerativeModel": staticmethod(lambda _name: fake_model),
            },
        )

        with patch.object(article_ai_processor, "genai", fake_genai), patch.object(article_ai_processor, "GEMINI_API_KEY", "sk-gemini"):
            article_ai_processor._generate_with_gemini("prompt", timeout_seconds=15)

        self.assertEqual(fake_model.request_options, {"timeout": 15})

    def test_openrouter_timeout_uses_model_timeout(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "model": "openai/gpt-oss-20b:free",
                    "choices": [{"message": {"content": '{"title":"x","description":"y","slug":"z","html_content":"<p>ok</p>"}'}}],
                }

        with patch.object(article_ai_processor.requests, "post", return_value=FakeResponse()) as post, patch.object(
            article_ai_processor, "OPENROUTER_API_KEY", "sk-openrouter"
        ):
            article_ai_processor._generate_with_openrouter("prompt", model_name="openai/gpt-oss-20b:free", timeout_seconds=12)

        self.assertEqual(post.call_args.kwargs["timeout"], 12)

    def test_gemini_timeout_retries_twice_before_provider_fallback(self):
        context = article_ai_processor.AIExecutionContext(article_id="a1")
        candidate = {"provider": "gemini", "model": "gemini-2.5-flash", "api_key": "sk-gemini"}
        timeout_error = article_ai_processor.requests.exceptions.Timeout("request timed out")

        with patch.object(article_ai_processor, "_provider_candidates", return_value=[candidate]), patch.object(
            article_ai_processor, "_cooldown_remaining", return_value=0
        ), patch.object(
            article_ai_processor,
            "_generate_with_candidate",
            side_effect=[timeout_error, timeout_error, RuntimeError("Gemini API error 429: quota exceeded")],
        ) as generate_candidate, patch.object(
            article_ai_processor, "_put_candidate_on_cooldown"
        ), patch.object(article_ai_processor.time, "sleep"):
            with self.assertRaises(article_ai_processor.AIProviderFallbackNeeded):
                article_ai_processor._generate_with_provider_name("gemini", "prompt", context=context)

        self.assertEqual(generate_candidate.call_count, 3)

    def test_caption_style_memory_avoids_recent_pattern(self):
        with TemporaryDirectory() as temp_dir:
            memory_path = Path(temp_dir) / "facebook_style_memory.json"
            memory_path.write_text(
                json.dumps(
                    {
                        "global_styles": ["tech_news"],
                        "recent": {"Tech-News": ["tech_news", "ai_tools"]},
                        "recent_hooks": [],
                        "recent_structures": [],
                        "stats": {"Tech-News": {"apps_programs": {"used": 20}}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(facebook_publisher, "FACEBOOK_STYLE_MEMORY_PATH", memory_path):
                pattern = facebook_publisher._choose_caption_pattern(
                    {"suggested_category": "Tech-News"},
                    [{"facebook_posted_at": "2026-01-01T00:00:00", "facebook_caption_pattern": "tech_news"}],
                )

        self.assertNotIn(pattern, {"tech_news", "ai_tools"})

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
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini", "openrouter"]), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=RuntimeError("provider failed")):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["article"]["ai_status"], "failed")
        self.assertTrue(result["article"]["ai_rotation_exhausted"])

    def test_openrouter_empty_response_switches_provider_without_stopping_article(self):
        calls = []
        good = {
            "title": "Important Chrome security update released today",
            "description": "A clear summary of the Chrome security update and why users should install it quickly to reduce practical risk.",
            "slug": "chrome-security-update",
            "html_content": "<p>" + " ".join(["security"] * 130) + "</p>",
        }

        def fake_generate(provider, prompt, context=None):
            calls.append(provider)
            if provider == "openrouter":
                raise article_ai_processor.AIProviderEmptyResponse("OpenRouter returned no choices.")
            return json.dumps(good), "gemini:test"

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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["openrouter", "gemini"]), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=fake_generate), patch.object(article_ai_processor, "validate_ai_article_output", return_value=QualityGateResult(True, "", 130)), patch.object(article_ai_processor, "_phase3_quality_failure_reason", return_value=""), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(calls, ["openrouter", "gemini"])
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["article"]["ai_provider_used"], "gemini:test")
        self.assertEqual(result["article"]["ai_quality_status"], "passed")

    def test_ai_quality_retries_until_article_passes(self):
        intro = " ".join(["يوضح", "هذا", "التحديث", "الأمني", "سبب", "أهمية", "المتابعة", "السريعة"] * 13)
        details = " ".join(["تساعد", "هذه", "الخطوة", "المستخدمين", "على", "تقليل", "المخاطر", "وتثبيت", "الإصلاحات"] * 12)
        good = {
            "title": "تحديث أمني مهم لمتصفح Chrome",
            "description": "شرح مبسط لتحديث أمني مهم في Chrome ولماذا ينبغي للمستخدمين تثبيت التحديث بسرعة لحماية بياناتهم وتقليل مخاطر الاستغلال.",
            "slug": "chrome-security-update",
            "html_content": f"<p>{intro}</p><h2>ما الذي حدث؟</h2><p>{details}</p>",
        }
        bad = {
            "title": "تحديث Chrome",
            "description": "وصف قصير عن تحديث Chrome الأمني.",
            "slug": "chrome-update",
            "html_content": "<p>short</p>",
        }
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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            outputs = [json.dumps(bad), json.dumps(bad), json.dumps(good, ensure_ascii=False)]
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=[(outputs[0], "gemini:test"), (outputs[1], "gemini:test"), (outputs[2], "gemini:test")]), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["article"]["ai_quality_attempts"], 3)
        self.assertEqual(result["article"]["ai_quality_status"], "passed")

    def test_ai_incomplete_json_retries_until_complete_response(self):
        good = {
            "title": "تحديث أمني كامل لمتصفح Chrome",
            "description": "شرح عربي واضح لتحديث أمني جديد في Chrome وما الذي يجب على المستخدم معرفته قبل تثبيت الإصلاح بسرعة مناسبة.",
            "slug": "chrome-security-update",
            "html_content": (
                "<p>" + " ".join(["حماية"] * 90) + "</p>"
                + "<h2>ما الذي حدث؟</h2><p>" + " ".join(["التحديث"] * 70) + "</p>"
            ),
        }
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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(
                article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]
            ), patch.object(
                article_ai_processor,
                "_generate_with_provider_name",
                side_effect=[('{"title":"broken"', "gemini:test"), (json.dumps(good, ensure_ascii=False), "gemini:test")],
            ), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["article"]["ai_quality_attempts"], 2)
        self.assertEqual(result["article"]["ai_quality_status"], "passed")

    def test_rich_input_short_article_is_rejected_and_regenerated(self):
        rich_good_html = (
            "<p>" + " ".join(["يوضح"] * 180) + "</p>"
            + "<h2>ما الذي حدث؟</h2><p>" + " ".join(["التحديث"] * 190) + "</p>"
            + "<h2>ماذا يعني هذا لك؟</h2><p>" + " ".join(["المستخدم"] * 180) + "</p>"
            + "<h2>كيف تحمي نفسك</h2><p>" + " ".join(["الوقاية"] * 170) + "</p>"
        )
        bad = {
            "title": "تحديث أمني مهم لمتصفح Chrome",
            "description": "شرح عربي موجز لخبر أمني جديد في Chrome وما الذي يجب متابعته.",
            "slug": "chrome-security-update",
            "html_content": "<p>" + " ".join(["security"] * 140) + "</p>",
        }
        good = {
            "title": "تحديث أمني مهم لمتصفح Chrome",
            "description": "شرح عربي كامل لتحديث أمني في Chrome وما الذي يجب على المستخدم معرفته وتطبيقه بسرعة لتقليل المخاطر اليومية.",
            "slug": "chrome-security-update",
            "html_content": rich_good_html,
        }
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
                            "content_preview": "Google released an emergency Chrome security update.",
                            "full_article_text": " ".join(["تفاصيل"] * 240),
                            "suggested_category": "Cyber-Security",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(
                article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]
            ), patch.object(
                article_ai_processor,
                "_generate_with_provider_name",
                side_effect=[(json.dumps(bad), "gemini:test"), (json.dumps(good, ensure_ascii=False), "gemini:test")],
            ), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["article"]["ai_quality_attempts"], 2)
        self.assertGreaterEqual(result["article"]["final_word_count"], 700)

    def test_ai_retries_once_for_excess_english(self):
        bad_english = " ".join(
            [
                "movie streaming classic oscar watch today feature workflow productivity account protection privacy security software update tool movies shows films drama comedy"
            ]
            * 9
        )
        bad = {
            "title": "تحديث أمني مهم لمستخدمي Android",
            "description": "شرح عربي موجز يوضح أهمية التحديث الجديد للمستخدمين وكيف يساعد في تحسين الحماية اليومية.",
            "slug": "android-security-update",
            "html_content": (
                "<p class='pIndent'><span class='dropCap'>ه</span> "
                f"{bad_english}.</p>"
            ),
        }
        good_intro = " ".join(["يوضح", "هذا", "التحديث", "الأمني", "أهمية", "حماية", "الحسابات", "والبيانات"] * 18)
        good_details = " ".join(["يساعد", "المستخدمين", "على", "تقليل", "المخاطر", "ومراجعة", "الإعدادات"] * 9)
        good = {
            "title": "تحديث أمني مهم لمستخدمي Android",
            "description": "شرح عربي موجز يوضح أهمية التحديث الجديد للمستخدمين وكيف يساعد في تحسين الحماية اليومية.",
            "slug": "android-security-update",
            "html_content": f"<p class='pIndent'><span class='dropCap'>ه</span> {good_intro}</p><h2>لماذا يهمك هذا؟</h2><p class='pIndent'>{good_details}</p>",
        }
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "a1",
                        "url": "https://example.com/android-security",
                        "status": "selected",
                        "processing_status": "ready_for_ai",
                        "ai_input_package": {
                            "title": "Android security update",
                            "url": "https://example.com/android-security",
                            "source_published_at": recent_iso(1),
                            "content_preview": "Android security update protects accounts.",
                            "suggested_category": "Cyber-Security",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=[(json.dumps(bad), "gemini:test"), (json.dumps(good, ensure_ascii=False), "gemini:test")]), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertTrue(result["article"].get("ai_excess_english_retry_used"))
        self.assertEqual(result["article"]["ai_quality_attempts"], 2)

    def test_ai_quality_failed_after_three_retries_skips_article(self):
        bad = {
            "title": "تحديث Chrome",
            "description": "وصف قصير عن تحديث Chrome الأمني.",
            "slug": "chrome-update",
            "html_content": "<p>short</p>",
        }
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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]), patch.object(article_ai_processor, "_generate_with_provider_name", return_value=(json.dumps(bad), "gemini:test")), patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["article"]["ai_quality_status"], "failed_after_retries")
        self.assertEqual(result["article"]["ai_quality_attempts"], 3)

    def test_gemini_429_triggers_openrouter_fallback(self):
        intro = " ".join(["يوضح", "هذا", "التحديث", "الأمني", "أهمية", "المتابعة", "السريعة", "للمستخدمين"] * 13)
        details = " ".join(["يساعد", "التثبيت", "السريع", "على", "تقليل", "المخاطر", "وحماية", "البيانات"] * 13)
        good = {
            "title": "تحديث أمني مهم لمتصفح Chrome",
            "description": "شرح مبسط لتحديث أمني مهم في Chrome ولماذا ينبغي للمستخدمين تثبيت التحديث بسرعة لحماية بياناتهم وتقليل مخاطر الاستغلال.",
            "slug": "chrome-security-update",
            "html_content": f"<p>{intro}</p><h2>ما الذي حدث؟</h2><p>{details}</p>",
        }
        calls = []

        def fake_generate(provider, prompt, context=None):
            calls.append(provider)
            if provider == "gemini":
                raise RuntimeError("Gemini API error 429: quota exceeded retry_delay")
            return json.dumps(good, ensure_ascii=False), "openrouter:test-model"

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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            original_validate = article_ai_processor.validate_ai_article_output
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]), patch.object(article_ai_processor, "_openrouter_fallback_available", return_value=True), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=fake_generate), patch.object(article_ai_processor, "validate_ai_article_output", wraps=original_validate) as validate_gate, patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(calls, ["gemini", "openrouter"])
        self.assertEqual(result["article"]["ai_provider_used"], "openrouter:test-model")
        self.assertEqual(result["article"]["ai_status"], "completed")
        self.assertEqual(result["article"]["ai_quality_status"], "passed")
        self.assertEqual(validate_gate.call_count, 1)
        self.assertTrue(validate_gate.call_args.args[0]["html_content"])

    def test_gemini_429_rotation_exhaustion_does_not_send_quality_gate_warning(self):
        calls = []

        def fake_generate(provider, prompt, context=None):
            calls.append(provider)
            if provider == "gemini":
                raise article_ai_processor.AIProviderFallbackNeeded("Gemini 429 quota exceeded retry_delay")
            raise RuntimeError("OpenRouter API error 429: rate limit exceeded")

        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "a1",
                        "url": "https://example.com/news",
                        "title": "Provider quota article",
                        "source_name": "Example Source",
                        "status": "selected",
                        "processing_status": "ready_for_ai",
                        "ai_input_package": {
                            "title": "Chrome fixes active zero-day vulnerability",
                            "url": "https://example.com/news",
                            "source_published_at": recent_iso(1),
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]), patch.object(article_ai_processor, "_openrouter_fallback_available", return_value=True), patch.object(article_ai_processor, "_generate_with_provider_name", side_effect=fake_generate), patch.object(article_ai_processor, "validate_ai_article_output") as validate_gate, patch.object(article_ai_processor.time, "sleep"):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["article"]["ai_status"], "failed")
        self.assertTrue(result["article"]["ai_rotation_exhausted"])
        self.assertEqual(result["article"]["ai_quality_status"], "provider_rotation_exhausted")
        self.assertEqual(calls, ["gemini", "openrouter"])
        validate_gate.assert_not_called()

    def test_openrouter_stops_after_two_fast_failures_when_gemini_failed(self):
        context = article_ai_processor.AIExecutionContext(article_id="a1")
        context.gemini_failures = 1
        candidates = [
            {"provider": "openrouter", "model": "inclusionai/ling-2.6-flash:free", "api_key": "sk-openrouter"},
            {"provider": "openrouter", "model": "liquid/lfm-2.5-1.2b-instruct:free", "api_key": "sk-openrouter"},
            {"provider": "openrouter", "model": "openai/gpt-oss-20b:free", "api_key": "sk-openrouter"},
        ]

        with patch.object(article_ai_processor, "_openrouter_candidates", return_value=candidates), patch.object(
            article_ai_processor, "_generate_with_candidate", side_effect=RuntimeError("provider failed")
        ) as generate_candidate, patch.object(
            article_ai_processor, "_cooldown_remaining", return_value=0
        ), patch.object(article_ai_processor, "_put_candidate_on_cooldown"):
            with self.assertRaises(article_ai_processor.AIProviderRotationExhausted):
                article_ai_processor._generate_with_provider_name("openrouter", "prompt", context=context)

        self.assertEqual(generate_candidate.call_count, 2)

    def test_ai_time_budget_stops_additional_attempts(self):
        bad = {
            "title": "ØªØ­Ø¯ÙŠØ« Chrome",
            "description": "ÙˆØµÙ Ù‚ØµÙŠØ± Ø¹Ù† ØªØ­Ø¯ÙŠØ« Chrome Ø§Ù„Ø£Ù…Ù†ÙŠ.",
            "slug": "chrome-update",
            "html_content": "<p>short</p>",
        }
        check_calls = {"count": 0}

        def fake_budget_check(_context, stage=""):
            check_calls["count"] += 1
            if stage == "attempt_2_start":
                raise article_ai_processor.AITimeBudgetExceeded("ai_time_budget_exceeded")

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
                            "content_preview": "Google released an emergency Chrome security update.",
                        },
                    }
                ],
                "notifications": {},
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(
                article_ai_processor, "_attempt_provider_sequence", return_value=["gemini"]
            ), patch.object(
                article_ai_processor, "_generate_with_provider_name", return_value=(json.dumps(bad), "gemini:test")
            ) as generate_provider, patch.object(
                article_ai_processor, "_check_ai_time_budget", side_effect=fake_budget_check
            ):
                article_queue.save_article_queue(queue)
                result = article_ai_processor.process_one_selected_article_with_ai(target_article_id="a1")

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["article"]["ai_quality_status"], "time_budget_exceeded")
        self.assertTrue(result["article"]["ai_time_budget_exceeded"])
        self.assertEqual(generate_provider.call_count, 1)
        self.assertGreaterEqual(check_calls["count"], 2)

    def test_plus_ui_format_places_main_image_after_first_paragraph(self):
        html = "<p>هذه مقدمة عربية واضحة عن الخبر وتشرح الفكرة ببساطة.</p><h2>التفاصيل</h2><p>هذه فقرة ثانية توضح الأثر على القارئ.</p>"
        formatted = article_ai_processor.format_phase3_article_html(
            html,
            {"main_image": "https://cdn.example/image.jpg", "title": "خبر أمني"},
        )
        self.assertIn("class=\"pIndent\"", formatted)
        self.assertIn("class=\"dropCap\"", formatted)
        self.assertLess(formatted.find("</p>"), formatted.find("<img"))
        self.assertIn("class=\"full\"", formatted)

    def test_ads_affiliate_articles_are_skipped(self):
        blocked, reason = content_filter.is_promotional_article(
            {"title": "Best VPN discount coupon deal", "url": "https://example.com/deals/best-vpn"}
        )
        self.assertTrue(blocked)
        self.assertTrue(reason)

    def test_cisa_vulnerability_summary_is_allowed_with_trusted_bypass(self):
        article = {
            "title": "Vulnerability Summary for the Week of April 20, 2026 | CISA",
            "url": "https://www.cisa.gov/news-events/bulletins/sb26-117",
            "content_preview": (
                "The CISA Vulnerability Bulletin provides a summary of new vulnerabilities. "
                "Entries may include additional information provided by organizations and "
                "efforts sponsored by CISA."
            ),
        }
        blocked, reason = content_filter.is_promotional_article(article)
        self.assertFalse(blocked, reason)
        self.assertEqual(reason, "Trusted source bypass applied")
        self.assertEqual(article["content_filter_bypass_message"], "Trusted source bypass applied")

    def test_cisa_advisory_passes_fast_quality_gate(self):
        article = {
            "title": "CISA Cybersecurity Advisory for CVE-2026-1234",
            "url": "https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-117a",
            "source_published_at": recent_iso(1),
            "seo_title": "CISA Cybersecurity Advisory for CVE-2026-1234",
            "seo_description": "CISA released a cybersecurity advisory about an exploited vulnerability.",
            "final_html": "<p>" + " ".join(["security"] * 130) + "</p>",
        }
        result = validate_before_publish(article, check_duplicate=False, fast_news_mode=True)
        self.assertTrue(result.passed, result.reason)
        self.assertIn("Trusted source bypass applied", result.warnings)

    def test_cve_article_with_many_links_is_allowed(self):
        content = " ".join(
            [
                "CVE-2026-1234",
                "security advisory",
                "references",
                "https://nvd.nist.gov/vuln/detail/CVE-2026-1234",
                "https://vendor.example/advisory",
                "https://example.org/patch",
                "https://example.org/mitigation",
                "https://example.org/ioc",
            ]
        )
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Researchers publish CVE-2026-1234 exploit analysis with references",
                "url": "https://research.example/report/cve-2026-1234",
                "content_preview": content,
            }
        )
        self.assertFalse(blocked, reason)

    def test_bleepingcomputer_article_is_allowed(self):
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Ransomware gang exploits zero-day in enterprise VPNs",
                "url": "https://www.bleepingcomputer.com/news/security/ransomware-gang-exploits-zero-day/",
                "content_preview": "The report includes CVE details and indicators of compromise.",
            }
        )
        self.assertFalse(blocked, reason)

    def test_buy_antivirus_now_is_blocked(self):
        blocked, reason = content_filter.is_promotional_article(
            {"title": "Buy antivirus now", "url": "https://example.com/security/buy-antivirus-now"}
        )
        self.assertTrue(blocked)
        self.assertTrue(reason)

    def test_affiliate_blog_post_is_blocked(self):
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Partner antivirus deal for readers",
                "url": "https://example.com/reviews/antivirus",
                "content_preview": "This affiliate blog post includes sponsored partner offers and coupon discounts.",
            }
        )
        self.assertTrue(blocked)
        self.assertTrue(reason)

    def test_normal_business_deal_news_is_not_skipped(self):
        blocked, reason = content_filter.is_promotional_article(
            {
                "title": "Meta inks deal for solar power beamed from space",
                "url": "https://techcrunch.com/2026/04/27/meta-solar-power-agreement",
                "content_preview": "The agreement would add renewable energy capacity for future data centers.",
            }
        )
        self.assertFalse(blocked, reason)

    def test_apps_ai_title_summary_marketing_words_need_real_ad_signal(self):
        article = {
            "title": "Android AI software partner deal brings new app automation tools",
            "url": "https://example.com/apps/android-ai-software-partner-deal",
            "content_preview": "The companies announced a software partnership for Android app automation and AI developer tools.",
        }
        with patch.object(content_filter, "log_event") as log:
            blocked, reason = content_filter.is_promotional_article(article)

        self.assertFalse(blocked, reason)
        log.assert_called()
        self.assertEqual(log.call_args.args[0], "quality_gate_false_positive_avoided")

    def test_title_only_marketing_signal_with_clean_body_is_not_blocked(self):
        article = {
            "title": "EU Commission AI deal expands Android software ecosystem",
            "url": "https://example.com/news/eu-commission-ai-ecosystem",
            "full_article_text": (
                "European regulators approved a broader interoperability framework for Android software tools. "
                "The report focuses on policy, developer access, and ecosystem changes without any buying prompts."
            ),
        }
        blocked, reason = content_filter.is_promotional_article(article)
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

    def test_workflow_cron_and_facebook_safety_are_current(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "1,7,13,19,25,31,37,43,49,55 * * * *"', text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("  push:\n", text)
        self.assertIn("group: jobs-production-${{ github.ref }}", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("timeout-minutes: 15", text)
        self.assertIn("timeout-minutes: 10", text)
        self.assertIn('"CATEGORY_ROTATION_MODE": "false"', text)
        self.assertIn('"MAX_SOURCES_PER_RUN": "20"', text)
        self.assertIn('"MAX_POSTS_PER_RUN": "1"', text)
        self.assertIn('"MAX_ARTICLES_PER_RUN": "1"', text)
        self.assertIn('"SAFE_CYCLE_MAX_ARTICLES": "1"', text)
        self.assertIn('"MAX_LIVE_POSTS_PER_DAY": "240"', text)
        self.assertIn('"TARGET_LIVE_POSTS_PER_DAY": "240"', text)
        self.assertIn('"MIN_MINUTES_BETWEEN_LIVE_POSTS": "0"', text)
        self.assertIn('"META_GRAPH_API_VERSION": "v26.0"', text)
        self.assertIn('"MAX_FACEBOOK_POSTS_PER_DAY": "2"', text)
        self.assertIn('"FACEBOOK_HARD_MAX_POSTS_PER_DAY": "3"', text)
        self.assertIn('"FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES": "45"', text)
        self.assertIn('"JOBS_MIN_PUBLISH_INTERVAL_MINUTES": "5"', text)
        self.assertIn('"JOBS_PREFERRED_FRESH_HOURS": "12"', text)
        self.assertIn('"JOBS_MAX_JOB_AGE_HOURS": "24"', text)
        self.assertIn("continue-on-error: true", text)
        self.assertIn("for attempt in 1 2 3 4 5 6; do", text)
        self.assertIn("actions/checkout@v7", text)
        self.assertIn("actions/setup-python@v7", text)
        self.assertIn('JOBS_RUN_BASE_SHA=$(git rev-parse HEAD)', text)
        self.assertIn('RUN_BASE_SHA="${JOBS_RUN_BASE_SHA:-}"', text)
        self.assertIn("merge_jobs_queue_snapshot", text)
        self.assertIn("Jobs queue changed upstream; merging remote and runner snapshot.", text)
        self.assertIn("added_snapshot_only", text)
        self.assertNotIn("prefer_jobs_queue_snapshot", text)

    def test_live_post_allowed_after_one_minute(self):
        now = datetime(2026, 4, 27, 12, 10, 0)
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "MIN_MINUTES_BETWEEN_LIVE_POSTS", 1), patch.object(main, "MAX_LIVE_POSTS_PER_DAY", 288):
                article_queue.save_article_queue(
                    {
                        "articles": [
                            {
                                "id": "published",
                                "status": "published",
                                "published_at": (now - timedelta(minutes=1, seconds=5)).isoformat(),
                            }
                        ],
                        "notifications": {},
                    }
                )
                status = main.get_publish_schedule_status(mode="live", now=now)

        self.assertTrue(status["allowed_now"], status["reasons"])
        self.assertEqual(status["min_minutes_between_live_posts"], 1)

    def test_live_post_not_blocked_by_old_five_minute_setting(self):
        now = datetime(2026, 4, 27, 12, 10, 0)
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "MIN_MINUTES_BETWEEN_LIVE_POSTS", 1), patch.object(main, "MAX_LIVE_POSTS_PER_DAY", 288):
                article_queue.save_article_queue(
                    {
                        "articles": [
                            {
                                "id": "published",
                                "status": "published",
                                "published_at": (now - timedelta(minutes=2)).isoformat(),
                            }
                        ],
                        "notifications": {},
                    }
                )
                status = main.get_publish_schedule_status(mode="live", now=now)

        self.assertTrue(status["allowed_now"], "old 5-minute spacing should not block at 2 minutes")
        self.assertNotIn("minimum minutes between live posts has not elapsed", status["reasons"])

    def test_zero_live_spacing_ignores_future_timestamp_from_timezone_skew(self):
        now = datetime(2026, 4, 29, 0, 0, 26)
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "MIN_MINUTES_BETWEEN_LIVE_POSTS", 0), patch.object(main, "MAX_LIVE_POSTS_PER_DAY", 20):
                article_queue.save_article_queue(
                    {
                        "articles": [
                            {
                                "id": "published",
                                "status": "published",
                                "published_at": "2026-04-29T00:48:05",
                            }
                        ],
                        "notifications": {},
                    }
                )
                status = main.get_publish_schedule_status(mode="live", now=now)

        self.assertTrue(status["allowed_now"], status["reasons"])
        self.assertNotIn("minimum minutes between live posts has not elapsed", status["reasons"])

    def test_rate_limit_wait_is_skip_not_fatal_failure(self):
        next_allowed = datetime(2026, 4, 27, 12, 1, 0)
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "drafts_created_today": 0,
            "live_posts_created_today": 1,
            "max_drafts_per_day": 10,
            "max_live_posts_per_day": 288,
            "last_draft_time": None,
            "last_live_publish_time": datetime(2026, 4, 27, 12, 0, 30),
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": 0,
            "min_minutes_between_drafts": 30,
            "min_minutes_between_live_posts": 1,
            "allowed_now": False,
            "next_allowed_time": next_allowed,
            "reasons": ["minimum minutes between live posts has not elapsed"],
        }
        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule):
            result = main.run_safe_cycle_only()

        self.assertFalse(result["completed"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "Waiting for next publishing window")
        self.assertEqual(result["step_reached"], "publish-limit-check")

    def test_facebook_after_blogger_success_still_respects_limits(self):
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
            "min_minutes_between_live_posts": 1,
            "allowed_now": True,
            "next_allowed_time": None,
            "reasons": [],
        }
        selected = {"id": "a1", "url": "https://example.com/a1", "title": "Fresh story"}
        ready = dict(selected, processing_status="ready_for_ai")
        ai_done = dict(ready, ai_status="completed", final_html="<p>ready</p>")
        published = dict(
            ai_done,
            publish_status="published",
            blogger_post_url="https://blog.example/a1",
            suggested_category="Cyber-Security",
        )
        draft_result = {
            "checked": 1,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": True,
            "error": "",
        }

        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "JOBS_MODE", False))
            stack.enter_context(patch.object(main, "SAFE_MODE", False))
            stack.enter_context(patch.object(main, "PUBLISH_MODE", "live"))
            stack.enter_context(patch.object(main, "FACEBOOK_AUTO_POST", True))
            stack.enter_context(patch.object(main, "CATEGORY_ROTATION_MODE", True))
            stack.enter_context(patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True))
            stack.enter_context(patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1))
            stack.enter_context(
                patch.object(main, "get_publish_schedule_status", return_value=schedule)
            )
            stack.enter_context(
                patch.object(
                    main,
                    "run_fetch_only",
                    return_value={
                        "selected_category": "Cyber-Security",
                        "queued_candidate_ids": ["a1"],
                        "failed_sources": [],
                        "zero_link_sources": [],
                    },
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "archive_expired_queue_articles",
                    return_value={"expired_archived": 0, "missing_date_archived": 0},
                )
            )
            stack.enter_context(patch.object(main, "run_score_only", return_value={}))
            stack.enter_context(
                patch.object(
                    main,
                    "run_enrich_only",
                    return_value={"failed": 0, "weak": 0},
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_select_newest_fresh_ready_article",
                    return_value=selected,
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "prepare_selected_articles_for_ai",
                    return_value={"checked": 1, "ready_for_ai": 1, "failed": 0},
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "process_one_selected_article_with_ai",
                    return_value={"processed": 1, "success": 1, "failed": 0},
                )
            )
            stack.enter_context(
                patch.object(main, "publish_one_blogger_post", return_value=draft_result)
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_find_article_by_id",
                    side_effect=[ready, ai_done, published, published, published],
                )
            )
            post_fb = stack.enter_context(
                patch.object(
                    main,
                    "post_one_article_to_facebook",
                    return_value={"posted": True, "article": published},
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "preview_next_facebook_post",
                    return_value={"available": False, "error": "preview skipped"},
                )
            )
            stack.enter_context(patch.object(main, "mark_many_as_published"))
            stack.enter_context(patch.object(main, "add_topic_fingerprint"))
            stack.enter_context(patch.object(main, "archive_published_queue_article"))

            result = main.run_safe_cycle_only()

        self.assertTrue(result["completed"])
        post_fb.assert_called_once()
        self.assertTrue(post_fb.call_args.kwargs.get("respect_limits"))
        # Facebook is a real pending queue now. Blogger success triggers a queue
        # drain, not a forced post for the article that just published; deadline
        # and queue priority decide which pending job is promoted next.
        self.assertNotIn("target_article_id", post_fb.call_args.kwargs)

    def test_facebook_safety_interval_cannot_be_disabled_by_zero_env_value(self):
        now = datetime(2026, 4, 27, 12, 10, 0)
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), \
                 patch.object(facebook_publisher, "MIN_MINUTES_BETWEEN_FACEBOOK_POSTS", 0), \
                 patch.object(facebook_publisher, "FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES", 45), \
                 patch.object(facebook_publisher, "MAX_FACEBOOK_POSTS_PER_DAY", 2), \
                 patch.object(facebook_publisher, "FACEBOOK_HARD_MAX_POSTS_PER_DAY", 3), \
                 patch.object(facebook_publisher, "JOBS_MODE", False):
                article_queue.save_article_queue(
                    {
                        "articles": [
                            {
                                "id": "posted",
                                "facebook_status": "posted",
                                "facebook_posted_at": now.isoformat(),
                            }
                        ],
                        "notifications": {},
                    }
                )
                status = facebook_publisher.get_facebook_limits_status(now=now)

        self.assertFalse(status["allowed_now"])
        self.assertEqual(status["min_minutes_between_facebook_posts"], 45)
        self.assertIn("safety interval", " ".join(status["reasons"]))

    def test_blogger_failure_prevents_facebook_post(self):
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
            "min_minutes_between_live_posts": 1,
            "allowed_now": True,
            "next_allowed_time": None,
            "reasons": [],
        }
        selected = {"id": "a1", "url": "https://example.com/a1", "title": "Fresh story"}
        ready = dict(selected, processing_status="ready_for_ai")
        ai_done = dict(ready, ai_status="completed", final_html="<p>ready</p>")
        draft_result = {"checked": 1, "duplicate_count": 0, "updated_existing": False, "created_new": False, "error": "Blogger failed"}

        with patch.object(main, "SAFE_MODE", False), patch.object(main, "PUBLISH_MODE", "live"), patch.object(main, "FACEBOOK_AUTO_POST", True), patch.object(main, "CATEGORY_ROTATION_MODE", True), patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), patch.object(main, "get_publish_schedule_status", return_value=schedule), patch.object(main, "run_fetch_only", return_value={"selected_category": "Cyber-Security", "queued_candidate_ids": ["a1"], "failed_sources": [], "zero_link_sources": []}), patch.object(main, "archive_expired_queue_articles", return_value={"expired_archived": 0, "missing_date_archived": 0}), patch.object(main, "run_score_only", return_value={}), patch.object(main, "run_enrich_only", return_value={"failed": 0, "weak": 0}), patch.object(main, "_select_newest_fresh_ready_article", return_value=selected), patch.object(main, "prepare_selected_articles_for_ai", return_value={"checked": 1, "ready_for_ai": 1, "failed": 0}), patch.object(main, "process_one_selected_article_with_ai", return_value={"processed": 1, "success": 1, "failed": 0}), patch.object(main, "publish_one_blogger_post", return_value=draft_result), patch.object(main, "_find_article_by_id", side_effect=[ready, ai_done, ai_done]), patch.object(main, "post_one_article_to_facebook") as post_fb, patch.object(main, "preview_next_facebook_post", return_value={"available": False, "error": "preview skipped"}):
            result = main.run_safe_cycle_only()

        self.assertFalse(result["completed"])
        self.assertEqual(result["reason"], "Blogger failed")
        post_fb.assert_not_called()

    def test_workflow_state_cache_and_fallback_paths_are_safe(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        for path in (
            "article_queue.json",
            "data/article_backlog.json",
            "data/published_ids.json",
            "data/crawl_state.json",
            "data/topic_fingerprints.json",
            "data/source_health.json",
            "data/internal_link_cache.json",
            "logs/auto_cycle_runs.jsonl",
        ):
            self.assertIn(path, text)
        self.assertIn("Persist runtime state fallback", text)
        self.assertIn('git commit -m "Update bot runtime state [skip ci]"', text)
        self.assertIn("rm -f .env client_secret.json data/token.json", text)
        self.assertIn("git rm --cached --ignore-unmatch .env client_secret.json data/token.json", text)

    def test_workflow_safe_diagnostics_are_present(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        self.assertIn("Safe runtime diagnostics", text)
        self.assertIn("UTC time:", text)
        self.assertIn("Workflow event:", text)
        self.assertIn("Branch:", text)
        self.assertIn("Category selected:", text)
        self.assertIn("Recent hours:", text)
        self.assertIn("Max sources:", text)

    def test_workflow_self_trigger_loop_is_present_and_guarded(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "*/15 * * * *"', text)
        self.assertIn("self_trigger:", text)
        self.assertIn("actions: write", text)
        self.assertIn("Recent run guard", text)
        self.assertIn("Self trigger next run", text)
        self.assertIn("900 - elapsed", text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)
        self.assertIn("github.event.inputs.self_trigger == 'true'", text)
        self.assertIn("/actions/workflows/auto-cycle.yml/dispatches", text)

    def test_no_env_or_secret_files_are_committed(self):
        tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
        self.assertNotIn(".env", tracked)
        self.assertNotIn("client_secret.json", tracked)
        self.assertNotIn("data/token.json", tracked)

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

    def test_source_rotation_state_advances_within_category(self):
        sources = [
            {"name": "Cyber A", "base_url": "https://a.example"},
            {"name": "Cyber B", "base_url": "https://b.example"},
        ]
        with TemporaryDirectory() as temp_dir:
            crawl_path = Path(temp_dir) / "crawl_state.json"
            with patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path):
                first_order = runtime_state.order_sources_for_rotation("Cyber-Security", sources)
                runtime_state.advance_source_rotation(
                    "Cyber-Security",
                    first_order[0]["base_url"],
                    first_order[0]["name"],
                    [source["base_url"] for source in sources],
                )
                second_order = runtime_state.order_sources_for_rotation("Cyber-Security", sources)
                record = runtime_state.source_rotation_record("Cyber-Security")

        self.assertEqual(first_order[0]["base_url"], "https://a.example")
        self.assertEqual(second_order[0]["base_url"], "https://b.example")
        self.assertEqual(record["last_source_key"], "https://a.example")

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
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "ARTICLE_QUEUE_PATH", queue_path), patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(main, "load_sources", return_value=sources), patch.object(main, "load_published_ids", return_value=set()), patch.object(main, "CATEGORY_ROTATION_MODE", True), patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1):
                result = main.run_fetch_only()

        self.assertEqual(result["selected_category"], "Cyber-Security")
        self.assertEqual(result["sources_checked"], 2)
        self.assertEqual(len(calls), 2)
        self.assertIn("https://cyber-a.example", calls)
        self.assertIn("https://cyber-b.example", calls)

    def test_lightweight_category_fetch_exits_without_next_category_fallback(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            if "cyber" in base_url:
                return [], "", 200, {"method_used": "feed"}
            return [{"title": f"Fresh {base_url}", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        sources = [
            {"name": "Cyber A", "base_url": "https://cyber-a.example", "enabled": True, "category_hint": "Cyber-Security", "category_label": "Cyber-Security", "fetch_limit_per_run": 3},
            {"name": "AI A", "base_url": "https://ai-a.example", "enabled": True, "category_hint": "AI-Tools", "category_label": "AI-Tools", "fetch_limit_per_run": 3},
        ]
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            crawl_path = Path(temp_dir) / "crawl_state.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "ARTICLE_QUEUE_PATH", queue_path), patch.object(runtime_state, "CRAWL_STATE_PATH", crawl_path), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(main, "load_sources", return_value=sources), patch.object(main, "load_published_ids", return_value=set()), patch.object(main, "CATEGORY_ROTATION_MODE", True), patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1):
                result = main.run_fetch_only()

        self.assertEqual(result["selected_category"], "Cyber-Security")
        self.assertEqual(result["selected_source_name"], "Cyber A")
        self.assertEqual(calls, ["https://cyber-a.example"])
        self.assertEqual(result["articles_found"], 0)

    def test_hourly_batch_fetch_processes_all_configured_categories(self):
        calls = []

        def fake_collect(base_url, **_kwargs):
            calls.append(base_url)
            return [{"title": f"Fresh {base_url}", "url": f"{base_url}/story", "published_at": recent_iso(1)}], "", 200, {"method_used": "feed"}

        sources = [
            {"name": "Cyber A", "base_url": "https://cyber-a.example", "enabled": True, "category_hint": "Cyber-Security", "category_label": "Cyber-Security", "fetch_limit_per_run": 3},
            {"name": "AI A", "base_url": "https://ai-a.example", "enabled": True, "category_hint": "AI-Tools", "category_label": "AI-Tools", "fetch_limit_per_run": 3},
            {"name": "Tech A", "base_url": "https://tech-a.example", "enabled": True, "category_hint": "Tech-News", "category_label": "Tech-News", "fetch_limit_per_run": 3},
            {"name": "Apps A", "base_url": "https://apps-a.example", "enabled": True, "category_hint": "Apps-Programs", "category_label": "Apps-Programs", "fetch_limit_per_run": 3},
        ]
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), patch.object(main, "ARTICLE_QUEUE_PATH", queue_path), patch.object(scraper, "source_crawl_record", return_value={}), patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect), patch.object(main, "load_sources", return_value=sources), patch.object(main, "load_published_ids", return_value=set()), patch.object(main, "CATEGORY_ROTATION_MODE", True), patch.object(main, "PROCESS_FULL_CATEGORY_PER_RUN", True), patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 8):
                result = main.run_fetch_only()

        self.assertEqual(result["selected_category"], "ALL")
        self.assertEqual(result["sources_checked"], 4)
        self.assertEqual(set(calls), {"https://cyber-a.example", "https://ai-a.example", "https://tech-a.example", "https://apps-a.example"})

    def test_hourly_candidate_prefers_unused_source_within_category(self):
        with TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path):
                article_queue.save_article_queue(
                    {
                        "articles": [
                            {
                                "id": "newer-a",
                                "url": "https://a.example/newer",
                                "title": "Newer source A",
                                "source_name": "Source A",
                                "category_label": "Cyber-Security",
                                "source_published_at": recent_iso(0.5),
                                "status": "ready",
                                "content_fetch_status": "success",
                            },
                            {
                                "id": "older-b",
                                "url": "https://b.example/older",
                                "title": "Older source B",
                                "source_name": "Source B",
                                "category_label": "Cyber-Security",
                                "source_published_at": recent_iso(1),
                                "status": "ready",
                                "content_fetch_status": "success",
                            },
                        ],
                        "notifications": {},
                    }
                )
                selected = main._lock_hourly_candidate("Cyber-Security", used_sources={"Source A"})

        self.assertEqual(selected["id"], "older-b")

    def test_hourly_target_does_not_post_facebook_when_blogger_fails(self):
        selected = {"id": "a1", "url": "https://example.com/a1", "source_name": "Source A", "category_label": "Cyber-Security"}
        ready = dict(selected, processing_status="ready_for_ai")
        ai_done = dict(ready, ai_status="completed", final_html="<p>" + " ".join(["ready"] * 130) + "</p>")
        draft_result = {"checked": 1, "duplicate_count": 0, "updated_existing": False, "created_new": False, "error": "Blogger failed"}

        with patch.object(main, "FACEBOOK_AUTO_POST", True), patch.object(main, "prepare_selected_articles_for_ai", return_value={"checked": 1, "ready_for_ai": 1, "failed": 0}), patch.object(main, "process_one_selected_article_with_ai", return_value={"processed": 1, "success": 1, "failed": 0}), patch.object(main, "publish_one_blogger_post", return_value=draft_result), patch.object(main, "_find_article_by_id", side_effect=[ready, ai_done, ai_done]), patch.object(main, "post_one_article_to_facebook") as post_fb:
            result = main._process_hourly_target(selected, "live")

        self.assertFalse(result["completed"])
        self.assertEqual(result["reason"], "Blogger failed")
        post_fb.assert_not_called()

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



    def test_auto_cycle_run_log_contains_reliability_fields(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "auto_cycle_runs.jsonl"
            result = {
                "completed": False,
                "skipped": True,
                "reason": "no valid article",
                "fetch": {
                    "selected_category": "Cyber-Security",
                    "sources_checked": 12,
                    "articles_found": 3,
                },
                "execution_seconds": 4.2,
            }
            record = main._auto_cycle_record_from_result("run-1", "2026-04-27T00:00:00", result)
            with patch.object(main, "AUTO_CYCLE_RUN_LOG", log_path):
                main._append_auto_cycle_run_log(record)
            saved = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertTrue(saved["timestamp"])
        self.assertEqual(saved["category"], "Cyber-Security")
        self.assertEqual(saved["sources_checked"], 12)
        self.assertEqual(saved["candidates_found"], 3)
        self.assertEqual(saved["skip_reason"], "no valid article")
        self.assertIn("published_url", saved)
        self.assertEqual(saved["execution_seconds"], 4.2)

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

    def test_sanitize_source_links_removes_original_domain(self):
        html = (
            "<p>Text <a href='https://news.example/story'>source link</a></p>"
            "<div class='related'><b>قد يهمك أيضًا</b>"
            "<a href='https://news.example/related'>related</a></div>"
        )
        cleaned, removed = source_sanitizer.sanitize_source_links(html, "news.example")
        self.assertEqual(removed, 2)
        self.assertNotIn("https://news.example", cleaned)
        self.assertIn("source link", cleaned)
        self.assertNotIn("related</a>", cleaned)

    def test_sanitize_source_links_keeps_trusted_external_links(self):
        html = (
            "<p><a href='https://github.com/org/repo'>GitHub</a> "
            "<a href='https://www.cisa.gov/news-events/alerts'>CISA</a> "
            "<a href='https://www.microsoft.com/security'>Microsoft</a></p>"
        )
        cleaned, removed = source_sanitizer.sanitize_source_links(html, "news.example")
        self.assertEqual(removed, 0)
        self.assertIn("github.com", cleaned)
        self.assertIn("cisa.gov", cleaned)
        self.assertIn("microsoft.com", cleaned)

    def test_full_article_extraction_prefers_jsonld_article_body(self):
        body = " ".join(f"securityword{i}" for i in range(130))
        html = (
            "<html><head><script type='application/ld+json'>"
            + json.dumps({"@type": "NewsArticle", "articleBody": body})
            + "</script><meta name='description' content='Short metadata only'></head>"
            "<body><article><p>Short visible summary only.</p></article></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")

        text, method, _meta, tried = article_enricher._choose_enrichment_text(
            {"title": "JSON-LD article", "source_name": "Example"},
            soup,
            80,
        )

        self.assertEqual(method, "jsonld_article_body")
        self.assertGreaterEqual(article_enricher._word_count(text), 120)
        self.assertIn("jsonld_article_body", " ".join(tried))

    def test_article_tag_extraction_removes_noise_blocks(self):
        body = " ".join(f"analysisword{i}" for i in range(125))
        html = (
            "<html><body><article>"
            f"<p>{body}</p>"
            "<div class='newsletter'>Subscribe to our newsletter</div>"
            "<div class='related'><a href='https://news.example/related'>Read more</a></div>"
            "</article></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")

        text, method, _meta, _tried = article_enricher._choose_enrichment_text(
            {"title": "Clean article", "source_name": "Example"},
            soup,
            80,
        )

        self.assertEqual(method, "article_tag")
        self.assertNotIn("Subscribe", text)
        self.assertNotIn("Read more", text)
        self.assertGreaterEqual(article_enricher._word_count(text), 120)

    def test_paragraph_fallback_runs_after_primary_extractors_fail(self):
        body = " ".join(f"fallbackword{i}" for i in range(125))
        html = f"<html><body><div><p>{body}</p></div></body></html>"
        soup = BeautifulSoup(html, "html.parser")

        text, method, _meta, _tried = article_enricher._choose_enrichment_text(
            {"title": "Fallback article", "source_name": "Example"},
            soup,
            80,
        )

        self.assertEqual(method, "paragraph_fallback")
        self.assertGreaterEqual(article_enricher._word_count(text), 120)

    def test_extraction_removes_source_and_affiliate_links_but_keeps_official_refs(self):
        html = (
            "<article>"
            "<p>Useful text <a href='https://news.example/internal'>source link</a></p>"
            "<p>Deal text <a href='https://amzn.to/example'>affiliate</a></p>"
            "<p>Official <a href='https://www.cisa.gov/news-events/alerts?utm_source=x'>CISA alert</a></p>"
            "</article>"
        )
        soup = BeautifulSoup(html, "html.parser")

        source_removed, affiliate_removed = article_enricher._remove_unwanted_links(
            soup,
            "https://news.example/story",
            "https://news.example",
        )
        refs = article_enricher._extract_trusted_references(
            soup,
            "https://news.example/story",
            "https://news.example",
        )

        self.assertEqual(source_removed, 1)
        self.assertEqual(affiliate_removed, 1)
        self.assertEqual([ref["url"] for ref in refs], ["https://www.cisa.gov/news-events/alerts"])

    def test_extract_article_images_prefers_og_image(self):
        soup = BeautifulSoup(
            "<html><head><meta property='og:image' content='/images/story.jpg'></head>"
            "<body><article><img src='/article.jpg' width='800' height='400'></article></body></html>",
            "html.parser",
        )
        images = article_enricher._extract_article_images(soup, "https://news.example/post")
        self.assertEqual(images[0]["url"], "https://news.example/images/story.jpg")
        self.assertEqual(images[0]["source"], "og")

    def test_extract_article_images_reads_jsonld_image(self):
        soup = BeautifulSoup(
            '<script type="application/ld+json">{"@type":"NewsArticle","image":{"url":"/jsonld.jpg"}}</script>',
            "html.parser",
        )
        images = article_enricher._extract_article_images(soup, "https://news.example/post")
        self.assertEqual(images[0]["url"], "https://news.example/jsonld.jpg")
        self.assertEqual(images[0]["source"], "jsonld")

    def test_extract_article_images_skips_small_logo(self):
        soup = BeautifulSoup(
            "<article>"
            "<img src='/logo.png' width='64' height='64' alt='logo'>"
            "<img src='/big.jpg' width='900' height='500' alt='story'>"
            "</article>",
            "html.parser",
        )
        images = article_enricher._extract_article_images(soup, "https://news.example/post")
        self.assertEqual(images[0]["url"], "https://news.example/big.jpg")

    def test_runtime_state_commit_skips_when_no_changes(self):
        with patch.object(main, "RUNTIME_STATE_PATHS", (Path("article_queue.json"),)), patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["git"], 0),
                subprocess.CompletedProcess(["git"], 0),
                subprocess.CompletedProcess(["git"], 0),
                subprocess.CompletedProcess(["git"], 0),
            ]
            result = main.save_runtime_state_to_git()

        self.assertFalse(result["saved"])
        self.assertEqual(result["git_push_state"], "skipped")
        called_commands = [call.args[0] for call in run.call_args_list]
        self.assertNotIn(["git", "commit", "-m", "Update bot runtime state [skip ci]"], called_commands)

    def test_internal_cache_prunes_links_older_than_sixty_minutes(self):
        now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        data = {
            "links": [
                {"title": "Fresh", "url": "https://blog.example/fresh", "published_at": (now - timedelta(minutes=30)).isoformat()},
                {"title": "Old", "url": "https://blog.example/old", "published_at": (now - timedelta(minutes=61)).isoformat()},
            ]
        }
        pruned, stats = internal_link_cache.prune_internal_link_cache(data, now=now)
        self.assertEqual(len(pruned["links"]), 1)
        self.assertEqual(stats["expired_removed"], 1)
        self.assertEqual(pruned["links"][0]["title"], "Fresh")

    def test_internal_cache_keeps_maximum_fifty_links(self):
        now = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        data = {
            "links": [
                {
                    "title": f"Post {index}",
                    "url": f"https://blog.example/post-{index}",
                    "published_at": (now - timedelta(seconds=index)).isoformat(),
                }
                for index in range(55)
            ]
        }
        pruned, stats = internal_link_cache.prune_internal_link_cache(data, now=now)
        self.assertEqual(len(pruned["links"]), 50)
        self.assertEqual(stats["trimmed_removed"], 5)

    def test_internal_links_do_not_link_current_article_to_itself(self):
        article = {
            "seo_title": "Microsoft security update",
            "suggested_category": "Cyber-Security",
            "blogger_post_url": "https://blog.example/current",
            "final_html": "<p>Microsoft issued a security update.</p>",
        }
        links = [
            {"title": "Microsoft security update", "url": "https://blog.example/current", "category": "Cyber-Security", "keywords": ["Microsoft"]},
            {"title": "Microsoft patch guidance", "url": "https://blog.example/patch", "category": "Cyber-Security", "keywords": ["Microsoft"]},
        ]
        selected = internal_link_cache.select_internal_link_candidates(article, links)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["url"], "https://blog.example/patch")

    def test_internal_link_insertion_adds_one_to_three_links_only(self):
        article = {
            "seo_title": "Microsoft and Google security patch",
            "suggested_category": "Cyber-Security",
            "final_html": "<p>Microsoft and Google released security patches.</p>",
        }
        links = [
            {"title": f"Microsoft Google patch {index}", "url": f"https://blog.example/post-{index}", "category": "Cyber-Security", "keywords": ["Microsoft", "Google"]}
            for index in range(6)
        ]
        html, count = internal_link_cache.insert_internal_links(article["final_html"], article, {"links": links})
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(count, 3)
        self.assertEqual(html.count("<li><a href="), count)

    def test_link_enrichment_does_not_insert_original_source_links(self):
        article = {
            "seo_title": "CISA Microsoft alert",
            "suggested_category": "Cyber-Security",
            "final_html": "<p>CISA and Microsoft published guidance.</p>",
            "trusted_references": [
                {"title": "Original source", "url": "https://source.example/story"},
                {"title": "CISA alert", "url": "https://www.cisa.gov/news-events/alerts"},
            ],
        }
        html, count = internal_link_cache.insert_trusted_external_links(
            article["final_html"],
            article["trusted_references"],
            source_domain="source.example",
        )
        self.assertEqual(count, 1)
        self.assertNotIn("source.example", html)
        self.assertIn("cisa.gov", html)

    def test_internal_cache_saved_after_live_publish(self):
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "internal_link_cache.json"
            article = {
                "seo_title": "Microsoft security patch",
                "suggested_category": "Cyber-Security",
                "seo_slug": "microsoft-security-patch",
                "published_at": "2026-04-28T12:00:00Z",
                "keywords": ["Microsoft", "security"],
            }
            stats = internal_link_cache.record_published_article(
                article,
                "https://blog.example/microsoft-security-patch",
                path=cache_path,
                now=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
            )
            saved = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertTrue(stats["saved"])
        self.assertEqual(saved["links"][0]["url"], "https://blog.example/microsoft-security-patch")
        self.assertNotIn("final_html", saved["links"][0])


if __name__ == "__main__":
    unittest.main()
