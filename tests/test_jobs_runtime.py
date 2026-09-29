"""Regressions for unattended Jobs delivery; no external requests or publishing."""
import copy
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import article_ai_processor as ai
import article_queue
import facebook_publisher as facebook
import job_core
import main
import quality_gate
import jobs_adaptive_controller as adaptive


class JobsRuntimeTests(unittest.TestCase):
    def test_adaptive_policy_starts_conservative_and_ramps_after_healthy_days(self):
        state = {
            "version": 1,
            "green_score": 0,
            "current_day": "2026-09-28",
            "last_evaluated_day": "",
            "days": {"2026-09-28": {"blogger_success": 3, "blogger_failure": 0, "blogger_rate_limit": 0}},
        }
        with patch.object(adaptive, "load_state", return_value=state), \
             patch.object(adaptive, "save_state"), \
             patch.object(adaptive, "JOBS_ADAPTIVE_PUBLISHING", True):
            policy = adaptive.current_policy(
                datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
            )
        self.assertEqual(state["green_score"], 1)
        self.assertGreaterEqual(policy["daily_cap"], 3)
        self.assertLessEqual(policy["daily_cap"], 12)

    def test_adaptive_rate_limit_reduces_health_score(self):
        state = {
            "version": 1,
            "green_score": 8,
            "current_day": "2026-09-28",
            "last_evaluated_day": "",
            "days": {"2026-09-28": {"blogger_success": 2, "blogger_failure": 1, "blogger_rate_limit": 1}},
        }
        with patch.object(adaptive, "load_state", return_value=state), \
             patch.object(adaptive, "save_state"):
            adaptive.current_policy(datetime(2026, 9, 29, 8, tzinfo=timezone.utc))
        self.assertEqual(state["green_score"], 4)

    def test_facebook_selects_only_strong_jobs_when_score_exists(self):
        base = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "",
            "job_notice_type": "vacancy",
            "job_number_of_positions": 1,
        }
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_SCORE", 75), \
             patch.object(facebook, "classify_urgency", return_value={"level": "normal"}):
            low = dict(base, job_score=68)
            high = dict(base, job_score=82)
            self.assertFalse(facebook._eligible_for_facebook(low))
            self.assertTrue(facebook._eligible_for_facebook(high))

    def test_jobs_publish_bookkeeping_uses_job_memory_not_generic_db(self):
        article = {"publish_status": "published", "id": "x", "url": "https://example.com/job"}
        with patch.object(main, "JOBS_MODE", True), \
             patch.object(main, "record_job_publish") as record, \
             patch.object(main, "archive_published_queue_article") as archive, \
             patch.object(main, "mark_many_as_published") as generic:
            main._record_successful_publish(article)
        record.assert_called_once_with(article)
        archive.assert_called_once()
        generic.assert_not_called()

    def test_auto_ai_provider_chain_uses_all_configured_backups(self):
        with patch.object(ai, "AI_PROVIDER", "auto"), \
             patch.object(ai, "GEMINI_API_KEY", "gemini-key"), \
             patch.object(ai, "GROQ_API_KEY", "groq-key"), \
             patch.object(ai, "OPENROUTER_API_KEY", "openrouter-key"), \
             patch.object(ai, "CLOUDFLARE_API_TOKEN", "cf-token"), \
             patch.object(ai, "CLOUDFLARE_ACCOUNT_ID", "cf-account"), \
             patch.object(ai, "MISTRAL_API_KEY", "mistral-key"), \
             patch.object(ai, "OPENAI_API_KEY", ""):
            self.assertEqual(
                ai._resolve_providers(),
                ["gemini", "groq", "openrouter", "cloudflare", "mistral"],
            )

    def test_provider_cooldowns_are_error_specific(self):
        self.assertGreater(
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 403 forbidden")),
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 503 unavailable")),
        )
        self.assertGreater(
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 429 rate limit")),
            ai._cooldown_seconds_for_error(ai.AIProviderEmptyResponse("empty response")),
        )

    def test_deterministic_jobs_fallback_passes_jobs_quality_gate(self):
        package = {
            "title": "Cybersecurity Consultant",
            "url": "https://careers.example.com/jobs/42",
            "source_url": "https://careers.example.com",
            "source_name": "Example Careers",
            "job_title": "مستشار الأمن السيبراني",
            "job_company": "Example Company",
            "job_location": "الدار البيضاء",
            "job_contract_type": "CDI",
            "job_deadline": "2026-10-15",
            "job_deadline_display": "15 أكتوبر 2026",
            "job_application_url": "https://careers.example.com/jobs/42/apply",
            "job_action_links": [
                {"url": "https://careers.example.com/jobs/42/apply", "label": "التقديم الرسمي"}
            ],
            "job_document_links": [],
            "job_notice_type": "vacancy",
            "desired_slug": "example-cybersecurity-42",
        }
        with patch.object(ai, "JOBS_MODE", True), patch.object(quality_gate, "JOBS_MODE", True):
            data = ai._deterministic_job_article(package)
            data = ai._finalize_html_content(data, package)
            ai._validate_ai_output(data, package)
        self.assertIn("توظيف", data["title"])
        self.assertIn(package["job_application_url"], data["html_content"])
        self.assertGreaterEqual(ai.html_word_count(data["html_content"]), 100)

    def test_publishing_window_block_still_runs_jobs_ingestion(self):
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "allowed_now": False,
            "reasons": ["adaptive Blogger spacing has not elapsed"],
            "next_allowed_time": datetime.now(),
            "drafts_created_today": 0,
            "live_posts_created_today": 1,
            "max_drafts_per_day": 3,
            "max_live_posts_per_day": 3,
            "target_live_posts_per_day": 3,
            "last_draft_time": None,
            "last_live_publish_time": None,
            "minutes_since_last_draft": None,
            "minutes_since_last_live_publish": 20,
            "min_minutes_between_drafts": 0,
            "min_minutes_between_live_posts": 80,
        }
        with patch.object(main, "JOBS_MODE", True), \
             patch.object(main, "SAFE_MODE", False), \
             patch.object(main, "PUBLISH_MODE", "live"), \
             patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1), \
             patch.object(main, "get_publish_schedule_status", return_value=schedule), \
             patch.object(main, "run_fetch_only", return_value={"articles_found": 2}) as fetch, \
             patch.object(main, "archive_expired_queue_articles", return_value={}) as cleanup, \
             patch.object(main, "run_score_only", return_value={"ready": 2}) as score, \
             patch.object(main, "run_enrich_only", return_value={"enriched": 2}) as enrich, \
             patch.object(main, "_print_safe_cycle_final_report"), \
             redirect_stdout(StringIO()):
            result = main.run_safe_cycle_only()
        self.assertTrue(result["skipped"])
        self.assertIsNotNone(result["ingest"])
        fetch.assert_called_once()
        cleanup.assert_called_once()
        score.assert_called_once()
        enrich.assert_called_once_with(force=False)

    def test_adaptive_window_prevents_blogger_burst(self):
        tz = ZoneInfo("Africa/Casablanca")
        now = datetime(2026, 9, 29, 10, 0, tzinfo=tz)
        state = {
            "daily_publish_count": {"2026-09-29": 2},
            "daily_urgent_override_count": {},
            "last_publish_at": (now - timedelta(minutes=30)).isoformat(),
        }
        with patch.object(job_core, "JOBS_ADAPTIVE_PUBLISHING", True), \
             patch.object(job_core, "load_job_state", return_value=state), \
             patch.object(job_core, "current_policy", return_value={
                 "enabled": True,
                 "stage": 5,
                 "green_score": 20,
                 "daily_cap": 12,
             }):
            status = job_core.job_publish_window_status(now=now)
        self.assertFalse(status["allowed_now"])
        self.assertGreaterEqual(status["min_interval_minutes"], 60)
        self.assertIn("spacing", " ".join(status["reasons"]))

    def test_queue_compaction_preserves_unresolved_facebook_delivery(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        with TemporaryDirectory() as temp:
            queue_path = Path(temp) / "jobs_article_queue.json"
            queue = {
                "articles": [
                    {
                        "id": "posted-terminal",
                        "archived": True,
                        "archived_at": old,
                        "publish_status": "published",
                        "facebook_status": "posted",
                    },
                    {
                        "id": "facebook-pending",
                        "archived": True,
                        "archived_at": old,
                        "publish_status": "published",
                        "facebook_status": "failed",
                    },
                ]
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                reloaded = article_queue.load_article_queue()

            self.assertEqual(stats["compacted_archived"], 1)
            self.assertEqual(
                [row["id"] for row in reloaded["articles"]],
                ["facebook-pending"],
            )
            archive_files = list((Path(temp) / "data" / "job_queue_archive").glob("*.json"))
            self.assertEqual(len(archive_files), 1)

    def test_compact_job_passes_both_word_gates(self):
        package = {"url": "https://employer.example/jobs/42", "job_notice_type": "vacancy"}
        data = {
            "title": "شركة أورنج تعلن عن توظيف خبير في الأمن السيبراني",
            "description": "فرصة توظيف لدى شركة أورنج في مجال الأمن السيبراني، تعرف على المعلومات الواردة في الإعلان الرسمي وطريقة تقديم طلب الترشيح.",
            "slug": "orange-cybersecurity",
            "html_content": "<p>" + " ".join("معلومة" + str(i) for i in range(118)) + "</p>",
        }
        article = {"ai_input_package": package}
        with patch.object(ai, "JOBS_MODE", True), patch.object(quality_gate, "JOBS_MODE", True), \
             patch.object(ai, "_phase3_quality_failure_reason", return_value=""):
            ai._validate_ai_output(data, package)
            ai._apply_success(article, data, "gemini:test")
        self.assertEqual(article["ai_status"], "completed")
        self.assertEqual(article["final_word_count"], 118)

    def test_jobs_do_not_truncate_long_institution_result_titles(self):
        title = "الوكالة الوطنية للمحافظة العقارية والمسح العقاري والخرائطية: لوائح المدعوين للاختبار الكتابي"
        with patch.object(ai, "JOBS_MODE", True):
            result = ai._shorten_metadata_once_if_needed({"title": title, "description": "وصف"})
        self.assertEqual(result["title"], title)
        self.assertEqual(quality_gate._job_title_style_reason(title, "candidate_list"), "")

    def test_subminimum_jobs_still_rejected(self):
        with patch.object(ai, "JOBS_MODE", True):
            with self.assertRaises(ValueError):
                ai._apply_success({"ai_input_package": {}}, {"html_content": "<p>قصير جدا</p>"}, "test")

    def test_all_year_slots_cover_regular_and_delayed_checks(self):
        tz = ZoneInfo("Africa/Casablanca")
        start = datetime(2027, 1, 1, tzinfo=tz)
        for offset in range(365):
            day = start + timedelta(days=offset)
            for slot in job_core.FACEBOOK_SLOTS[day.weekday()]:
                target = day.replace(hour=slot.hour, minute=slot.minute)
                for delay in (7, 22, 37, 49):
                    now = target + timedelta(minutes=delay)
                    result = job_core.facebook_slot_status(now=now.astimezone(timezone.utc))
                    self.assertTrue(result["allowed_now"], (now, result))

    def test_delayed_post_consumes_slot_without_duplicate(self):
        target = datetime(2026, 9, 29, 12, 30, tzinfo=ZoneInfo("Africa/Casablanca"))
        posted = target + timedelta(minutes=47)
        result = job_core.facebook_slot_status(
            posted_times=[posted.isoformat()], now=target + timedelta(minutes=49)
        )
        self.assertFalse(result["allowed_now"])

    def test_no_publishing_before_slot_or_overnight(self):
        tz = ZoneInfo("Africa/Casablanca")
        for hour, minute in ((2, 0), (12, 29), (23, 30)):
            result = job_core.facebook_slot_status(now=datetime(2026, 9, 29, hour, minute, tzinfo=tz))
            self.assertFalse(result["allowed_now"])

    def test_date_only_deadline_includes_whole_local_day(self):
        deadline = job_core.job_deadline_time({"job_deadline": "2026-09-29"})
        noon = datetime(2026, 9, 29, 12, tzinfo=ZoneInfo("Africa/Casablanca"))
        self.assertGreater(deadline, noon)
        self.assertEqual(deadline.astimezone(noon.tzinfo).hour, 23)
        explicit = "2026-09-29T16:00:00+00:00"
        self.assertEqual(job_core.job_deadline_time({"job_deadline": explicit}).isoformat(), explicit)

    def test_jobs_selector_respects_failed_candidate_cooldown(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        row = {"status": "ready", "content_fetch_status": "success",
               "candidate_retry_after": (now + timedelta(minutes=45)).isoformat()}
        with patch.object(job_core, "prepare_job_candidate") as prepare:
            self.assertIsNone(job_core.select_best_job_from_queue({"articles": [row]}, now))
            prepare.assert_not_called()

    def test_retry_uses_job_validation_and_excludes_attempted_ids(self):
        first, second = {"id": "failed"}, {"id": "verified", "status": "ready"}
        queue = {"articles": [first, second]}
        with patch.object(main, "JOBS_MODE", True), \
             patch.object(main, "load_article_queue", return_value=queue), \
             patch.object(main, "save_article_queue"), \
             patch.object(main, "select_best_job_from_queue", return_value=second) as select, \
             patch.object(main, "run_plan_next_only") as generic:
            result = main._select_retry_candidate({}, {"failed"})
        self.assertIs(result, second)
        self.assertEqual(select.call_args.args[0]["articles"], [second])
        generic.assert_not_called()

    def test_pending_facebook_runs_even_when_article_ai_fails(self):
        calls = []
        def social():
            calls.append("facebook")
            return {"created": 1}
        def cycle():
            calls.append("blogger")
            return {"completed": False, "step_reached": "run-ai", "reason": "AI failed"}
        with patch.object(main, "JOBS_MODE", True), patch.object(main, "FACEBOOK_AUTO_POST", True), \
             patch.object(main, "_effective_publish_mode", return_value="live"), \
             patch.object(main, "drain_scheduled_facebook", side_effect=social), \
             patch.object(main, "run_safe_cycle_only", side_effect=cycle), \
             patch.object(main, "save_runtime_state_to_git", return_value={}), \
             patch.object(main, "_append_auto_cycle_run_log"), \
             patch.object(main, "log_event"), redirect_stdout(StringIO()):
            result = main.run_auto_cycle_logged()
        self.assertEqual(calls, ["facebook", "blogger"])
        self.assertEqual(result["scheduled_facebook"]["created"], 1)

    def test_backlog_never_bypasses_schedule(self):
        pending = [{"id": "one"}, {"id": "two"}]
        with patch.object(facebook, "_is_configured", return_value=True), \
             patch.object(facebook, "load_article_queue", return_value={}), \
             patch.object(facebook, "_facebook_backfill_candidates", return_value=(pending, [])), \
             patch.object(facebook, "get_facebook_limits_status", return_value={"allowed_now": False}), \
             patch.object(facebook, "post_one_article_to_facebook") as post:
            result = facebook.drain_scheduled_facebook()
        post.assert_not_called()
        self.assertEqual(result["created"], 0)

    def test_backlog_posts_only_one_with_limits_enabled(self):
        pending = [{"id": "one"}, {"id": "two"}]
        with patch.object(facebook, "_is_configured", return_value=True), \
             patch.object(facebook, "load_article_queue", return_value={}), \
             patch.object(facebook, "_facebook_backfill_candidates", return_value=(pending, [])), \
             patch.object(facebook, "get_facebook_limits_status", return_value={"allowed_now": True}), \
             patch.object(facebook, "post_one_article_to_facebook", return_value={"posted": True}) as post:
            result = facebook.drain_scheduled_facebook()
        post.assert_called_once_with("one", respect_limits=True)
        self.assertEqual(result["created"], 1)

    def test_daily_social_count_uses_morocco_date_not_next_slot(self):
        # After today's final slot, tomorrow's next slot must not reset today's count.
        tz = ZoneInfo("Africa/Casablanca")
        now = datetime(2026, 9, 29, 23, 55, tzinfo=tz)
        rows = [{"facebook_status": "posted", "facebook_posted_at": now.isoformat()}]
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            result = facebook.get_facebook_limits_status(now=now)
        self.assertEqual(result["facebook_posts_today"], 1)

    def test_urgent_facebook_still_obeys_hard_daily_cap(self):
        tz = ZoneInfo("Africa/Casablanca")
        now = datetime(2026, 9, 29, 18, 0, tzinfo=tz)
        rows = [
            {
                "facebook_status": "posted",
                "facebook_posted_at": (now - timedelta(hours=offset + 1)).isoformat(),
            }
            for offset in range(3)
        ]
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "MAX_FACEBOOK_POSTS_PER_DAY", 2), \
             patch.object(facebook, "FACEBOOK_HARD_MAX_POSTS_PER_DAY", 3), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            result = facebook.get_facebook_limits_status(now=now, urgent=True)
        self.assertFalse(result["allowed_now"])
        self.assertEqual(result["facebook_posts_today"], 3)
        self.assertIn("hard daily safety limit", " ".join(result["reasons"]))

    def test_facebook_safety_interval_blocks_urgent_burst(self):
        tz = ZoneInfo("Africa/Casablanca")
        now = datetime(2026, 9, 29, 18, 0, tzinfo=tz)
        rows = [{
            "facebook_status": "posted",
            "facebook_posted_at": (now - timedelta(minutes=20)).isoformat(),
        }]
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "MAX_FACEBOOK_POSTS_PER_DAY", 2), \
             patch.object(facebook, "FACEBOOK_HARD_MAX_POSTS_PER_DAY", 3), \
             patch.object(facebook, "FACEBOOK_SAFETY_MIN_INTERVAL_MINUTES", 45), \
             patch.object(facebook, "MIN_MINUTES_BETWEEN_FACEBOOK_POSTS", 0), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            result = facebook.get_facebook_limits_status(now=now, urgent=True)
        self.assertFalse(result["allowed_now"])
        self.assertEqual(result["min_minutes_between_facebook_posts"], 45)
        self.assertIn("safety interval", " ".join(result["reasons"]))

    def test_manual_backfill_cannot_bypass_schedule(self):
        pending = [{"id": "one"}]
        with patch.object(facebook, "load_article_queue", return_value={"articles": []}), \
             patch.object(facebook, "_facebook_backfill_candidates", return_value=(pending, [])), \
             patch.object(facebook, "get_facebook_limits_status", return_value={"allowed_now": False}), \
             patch.object(facebook, "post_one_article_to_facebook") as post:
            result = facebook.backfill_facebook_posts()
        post.assert_not_called()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_uncertain_delivery_is_not_auto_retried(self):
        article = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/test.html",
            "facebook_status": "delivery_uncertain",
            "facebook_post_id": "",
        }
        self.assertFalse(facebook._eligible_for_facebook(article))


    def test_facebook_auth_failure_uses_long_retry_backoff(self):
        article = {}
        with patch.object(facebook.time, "time", return_value=1000):
            facebook._apply_failure(
                article,
                RuntimeError('Facebook Graph API error 400: {"error":{"type":"OAuthException","code":190}}'),
            )
        self.assertEqual(article["facebook_status"], "failed")
        self.assertEqual(article["facebook_failure_count"], 1)
        self.assertEqual(article["facebook_retry_after_epoch"], 1000 + 6 * 3600)
        self.assertFalse(facebook._facebook_retry_ready(article, now_epoch=1001))
        self.assertTrue(
            facebook._facebook_retry_ready(
                article,
                now_epoch=1000 + 6 * 3600,
            )
        )

    def test_facebook_failed_backfill_respects_retry_cooldown(self):
        article = {
            "id": "cooldown",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "failed",
            "facebook_retry_after_epoch": 9999999999,
        }
        pending, comments = facebook._facebook_backfill_candidates([article])
        self.assertEqual(pending, [])
        self.assertEqual(comments, [])


    def test_jobs_never_publish_text_only_when_card_render_fails(self):
        article = {
            "id": "job-image-required",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "job_notice_type": "vacancy",
            "job_company": "Example Company",
            "seo_title": "Example Company توظف مهندس شبكات",
            "facebook_template_key": "new",
        }
        blueprint = {
            "caption": "فرصة عمل جديدة\n\n💼 الوظيفة: مهندس شبكات\n\n#وظائف #فرص_عمل #المغرب",
            "hook": "فرصة عمل جديدة",
        }
        with patch.object(
            facebook,
            "generate_facebook_image",
            return_value={"ok": False, "path": "", "error": "render failed"},
        ), patch.object(facebook, "_post_to_graph") as text_post:
            with self.assertRaisesRegex(RuntimeError, "refusing text-only publish"):
                facebook._publish_facebook_post(article, blueprint)
        text_post.assert_not_called()

    def test_jobs_caption_fingerprint_is_remembered(self):
        article = {
            "seo_title": "شركة تجريبية توظف مهندس شبكات في الرباط",
            "job_title": "مهندس شبكات",
            "job_company": "شركة تجريبية",
            "job_location": "الرباط",
            "job_notice_type": "vacancy",
            "suggested_category": "jobs-morocco",
        }
        blueprint = facebook._jobs_facebook_blueprint(
            article,
            "https://example.blogspot.com/p/job.html",
        )
        with __import__("tempfile").TemporaryDirectory() as temp:
            memory_path = __import__("pathlib").Path(temp) / "facebook-style.json"
            with patch.object(facebook, "FACEBOOK_STYLE_MEMORY_PATH", memory_path):
                facebook._remember_caption_pattern(
                    article,
                    "jobs",
                    posted=False,
                    structure_id=blueprint["structure"],
                    hook=blueprint["hook"],
                    cta=blueprint["cta"],
                    hashtags=blueprint["hashtags"],
                    fingerprint=blueprint["fingerprint"],
                )
                failed_memory = facebook._load_style_memory()
                self.assertNotIn(
                    blueprint["fingerprint"],
                    failed_memory["recent_fingerprints"],
                )

                facebook._remember_caption_pattern(
                    article,
                    "jobs",
                    posted=True,
                    structure_id=blueprint["structure"],
                    hook=blueprint["hook"],
                    cta=blueprint["cta"],
                    hashtags=blueprint["hashtags"],
                    fingerprint=blueprint["fingerprint"],
                )
                memory = facebook._load_style_memory()
        self.assertIn(blueprint["fingerprint"], memory["recent_fingerprints"])


    def test_jobs_caption_uses_next_truthful_variant_when_recent_caption_matches(self):
        article = {
            "id": "job-caption-rotation",
            "job_campaign_id": "campaign-1",
            "seo_title": "شركة تجريبية توظف مهندس نظم في الدار البيضاء",
            "job_title": "مهندس نظم",
            "job_company": "شركة تجريبية",
            "job_location": "الدار البيضاء",
            "job_notice_type": "vacancy",
            "suggested_category": "jobs-morocco",
        }
        url = "https://example.blogspot.com/p/job-caption-rotation.html"
        first = facebook._jobs_facebook_blueprint(article, url, variant_offset=0)
        with __import__("tempfile").TemporaryDirectory() as temp:
            memory_path = __import__("pathlib").Path(temp) / "facebook-style.json"
            with patch.object(facebook, "FACEBOOK_STYLE_MEMORY_PATH", memory_path):
                facebook._remember_caption_pattern(
                    article,
                    "jobs",
                    posted=True,
                    structure_id=first["structure"],
                    hook=first["hook"],
                    cta=first["cta"],
                    hashtags=first["hashtags"],
                    fingerprint=first["fingerprint"],
                )
                second = facebook._prepare_facebook_post(article, [article], url)

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["variant_index"], second["variant_index"])



if __name__ == "__main__":
    unittest.main()
