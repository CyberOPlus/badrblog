"""Regressions for unattended Jobs delivery; no external requests or publishing."""
import copy
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import article_ai_processor as ai
import article_draft_publisher as draft
import article_enricher
import article_queue
import article_processor
import facebook_publisher as facebook
import job_core
import job_document_renderer
import main
import quality_gate
import verified_fact_manifest as fact_manifest
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
        self.assertEqual(policy["daily_cap"], 240)

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

    def test_adaptive_ai_or_facebook_instability_holds_ramp_up(self):
        state = {
            "version": 1,
            "green_score": 6,
            "current_day": "2026-09-28",
            "last_evaluated_day": "",
            "days": {
                "2026-09-28": {
                    "blogger_success": 3,
                    "blogger_failure": 0,
                    "blogger_rate_limit": 0,
                    "ai_provider_failure": 1,
                    "facebook_failure": 0,
                    "source_warning": 0,
                    "deterministic_fallback": 1,
                }
            },
        }
        with patch.object(adaptive, "load_state", return_value=state), \
             patch.object(adaptive, "save_state"), \
             patch.object(adaptive, "JOBS_ADAPTIVE_PUBLISHING", True):
            adaptive.current_policy(datetime(2026, 9, 29, 8, tzinfo=timezone.utc))
        self.assertEqual(state["green_score"], 6)
        self.assertEqual(state["days"]["2026-09-28"]["health_state"], "yellow")

    def test_jobs_auto_mode_allows_zero_ai_keys_for_deterministic_fallback(self):
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "AI_PROVIDER", "auto"), \
             patch.object(ai, "GEMINI_API_KEY", ""), \
             patch.object(ai, "GROQ_API_KEY", ""), \
             patch.object(ai, "OPENROUTER_API_KEY", ""), \
             patch.object(ai, "CLOUDFLARE_API_TOKEN", ""), \
             patch.object(ai, "CLOUDFLARE_ACCOUNT_ID", ""), \
             patch.object(ai, "MISTRAL_API_KEY", ""), \
             patch.object(ai, "OPENAI_API_KEY", ""):
            self.assertEqual(ai._resolve_providers(), [])
            self.assertEqual(ai._attempt_provider_sequence(), [])

    def test_single_ai_candidate_on_cooldown_is_not_called(self):
        candidate = {"provider": "groq", "api_key": "secret", "model": "test"}
        with patch.object(ai, "_provider_candidates", return_value=[candidate]), \
             patch.object(ai, "_cooldown_remaining", return_value=120), \
             patch.object(ai, "_generate_with_candidate") as generate:
            with self.assertRaises(ai.AIProviderFallbackNeeded):
                ai._generate_with_provider_name("groq", "prompt")
        generate.assert_not_called()

    def test_structured_job_success_is_not_reenriched_when_compact(self):
        article = {
            "id": "structured",
            "status": "ready",
            "category_label": "jobs-morocco",
            "content_fetch_status": "success",
            "full_article_text": " ".join(["detail"] * 55),
            "ats_provider": "phenom",
            "job_application_url": "https://example.com/apply",
            "job_title": "Network Engineer",
            "job_company": "Example",
        }
        with patch.object(article_enricher, "JOBS_MODE", True), \
             patch.object(article_enricher, "load_article_queue", return_value={"articles": [article]}), \
             patch.object(article_enricher, "save_article_queue"), \
             patch.object(article_enricher, "enrich_article") as enrich:
            result = article_enricher.enrich_ready_articles(force=False)
        self.assertEqual(result["already_enriched"], 1)
        enrich.assert_not_called()

    def test_candidate_failure_backoff_grows_and_caps(self):
        first = article_enricher._candidate_failure_backoff_minutes(1)
        later = article_enricher._candidate_failure_backoff_minutes(4)
        capped = article_enricher._candidate_failure_backoff_minutes(99)
        self.assertGreaterEqual(first, 1)
        self.assertGreater(later, first)
        self.assertLessEqual(capped, 24 * 60)

    def test_low_score_facebook_backlog_is_settled_not_failed(self):
        article = {
            "id": "low",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/2026/09/job.html",
            "facebook_status": "failed",
            "facebook_error": "waiting for Morocco Facebook publishing slot",
            "facebook_failure_count": 3,
            "job_score": 65,
            "job_number_of_positions": 1,
            "job_notice_type": "vacancy",
        }
        queue = {"articles": [article]}
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_SCORE", 75), \
             patch.object(facebook, "classify_urgency", return_value={"level": "normal"}), \
             patch.object(facebook, "save_article_queue") as save:
            settled = facebook._settle_unselected_job_facebook(queue)
        self.assertEqual(settled, 1)
        self.assertEqual(article["facebook_status"], "not_selected")
        self.assertNotIn("facebook_error", article)
        self.assertNotIn("facebook_failure_count", article)
        save.assert_called_once()

    def test_archived_published_job_can_be_reopened_for_link_repair(self):
        with self.subTest("published archive repair"):
            original = article_queue.ARTICLE_QUEUE_PATH
            from tempfile import TemporaryDirectory
            from pathlib import Path
            with TemporaryDirectory() as temp_dir:
                article_queue.ARTICLE_QUEUE_PATH = Path(temp_dir) / "jobs_article_queue.json"
                try:
                    article_queue.save_article_queue({
                        "articles": [{
                            "id": "published-bad-link",
                            "status": "published",
                            "publish_status": "published",
                            "archived": True,
                            "archive_reason": "published_to_blogger",
                            "archived_at": "2026-09-30T10:00:00",
                            "url": "https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
                            "canonical_url": "https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
                            "job_detail_url": "https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
                            "job_application_url": "https://www.emploi-public.ma/ar/تفاصيل/المباريات/59305efc-899b-4884-906d-d39e894e6099",
                            "job_action_links": [],
                            "job_document_links": [],
                            "ai_status": "completed",
                            "ai_quality_status": "passed",
                            "processing_status": "ready_for_ai",
                            "ai_input_package": {"job_application_url": "stale"},
                            "final_html": "<p>stale wrong link</p>",
                            "blogger_article_html": "<p>stale wrong link</p>",
                            "final_word_count": 4,
                        }],
                        "notifications": {},
                    })
                    stats = article_queue.repair_job_link_bindings()
                    repaired = article_queue.load_article_queue()["articles"][0]
                    self.assertEqual(stats["reopened_published"], 1)
                    self.assertEqual(repaired["status"], "ready")
                    self.assertEqual(repaired["publish_status"], "repair_pending")
                    self.assertFalse(repaired["archived"])
                    self.assertEqual(repaired["job_application_url"], repaired["job_detail_url"])
                    self.assertNotIn("final_html", repaired)
                    self.assertNotIn("blogger_article_html", repaired)
                    self.assertNotIn("ai_status", repaired)
                    self.assertNotIn("ai_input_package", repaired)
                    self.assertNotIn("processing_status", repaired)
                finally:
                    article_queue.ARTICLE_QUEUE_PATH = original

    def test_pending_link_repair_regenerates_even_after_previous_publish_block(self):
        article = {
            "status": "selected",
            "publish_status": "failed",
            "job_link_repair_pending": True,
            "job_link_binding_repaired_at": "2026-09-30T14:00:00",
            "blogger_post_id": "post-1",
            "blogger_post_url": "https://example.blogspot.com/job.html",
            "url": "https://example.com/jobs/12345",
            "canonical_url": "https://example.com/jobs/12345",
            "job_detail_url": "https://example.com/jobs/12345",
            "job_application_url": "https://example.com/jobs/12345",
            "job_action_links": [],
            "job_document_links": [],
            "ai_status": "completed",
            "processing_status": "ready_for_ai",
            "ai_input_package": {"stale": True},
            "final_html": "<p>stale</p>",
        }
        original = article_queue.ARTICLE_QUEUE_PATH
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as temp_dir:
            article_queue.ARTICLE_QUEUE_PATH = Path(temp_dir) / "jobs_article_queue.json"
            try:
                article_queue.save_article_queue({"articles": [article], "notifications": {}})
                stats = article_queue.repair_job_link_bindings()
                repaired = article_queue.load_article_queue()["articles"][0]
                self.assertEqual(stats["repaired"], 1)
                self.assertEqual(repaired["status"], "ready")
                self.assertEqual(repaired["publish_status"], "repair_pending")
                self.assertEqual(repaired["blogger_post_id"], "post-1")
                self.assertNotIn("final_html", repaired)
                self.assertNotIn("ai_status", repaired)
                self.assertNotIn("processing_status", repaired)
                self.assertNotIn("ai_input_package", repaired)
            finally:
                article_queue.ARTICLE_QUEUE_PATH = original

    def test_queue_save_is_noop_when_payload_is_unchanged(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            path = Path(temp) / "queue.json"
            queue = {"articles": [{"id": "x"}], "notifications": {}}
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "_now_iso", return_value="first"):
                self.assertTrue(article_queue.save_article_queue(queue))
                first = path.read_text(encoding="utf-8")
                self.assertFalse(article_queue.save_article_queue(queue))
                second = path.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertIn('"updated_at": "first"', first)

    def test_stale_logo_wait_is_archived_automatically(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        queue = {
            "articles": [{
                "id": "logo-old",
                "status": "selected",
                "publish_status": "waiting_for_logo",
                "logo_first_wait_at": old,
                "discovered_at": old,
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                saved = article_queue.load_article_queue()
        self.assertEqual(stats["archived_stale_logo_wait"], 1)
        self.assertTrue(saved["articles"][0]["archived"])

    def test_job_archive_compaction_drops_large_payload_fields(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        archived_time = datetime.now(timezone.utc) - timedelta(days=8)
        old = archived_time.isoformat()
        queue = {
            "articles": [{
                "id": "terminal",
                "url": "https://example.com/jobs/1",
                "title": "Job",
                "archived": True,
                "archived_at": old,
                "archive_reason": "published_to_blogger",
                "publish_status": "published",
                "facebook_status": "posted",
                "facebook_post_id": "fb1",
                "blogger_post_url": "https://example.blogspot.com/job.html",
                "full_article_text": "x" * 10000,
                "final_html": "<p>" + ("x" * 10000) + "</p>",
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                archive_path = (
                    Path(temp)
                    / "data"
                    / "job_queue_archive"
                    / f"{archived_time:%Y-%m}.json"
                )
                archive = __import__("json").loads(
                    archive_path.read_text(encoding="utf-8")
                )
        self.assertEqual(stats["compacted_archived"], 1)
        record = next(iter(archive["records"].values()))
        self.assertNotIn("full_article_text", record)
        self.assertNotIn("final_html", record)
        self.assertEqual(record["facebook_post_id"], "fb1")

    def test_duplicate_ats_scan_keeps_original_publish_time(self):
        existing = {
            "source_published_at": "2026-09-01T08:00:00+00:00",
            "job_published_at": "2026-09-01T08:00:00+00:00",
        }
        discovered = {
            "source_published_at": "2026-09-29T19:00:00+00:00",
            "job_published_at": "2026-09-29T19:00:00+00:00",
        }
        changed = article_queue._merge_job_discovery_metadata(existing, discovered)
        self.assertFalse(changed)
        self.assertEqual(
            existing["source_published_at"],
            "2026-09-01T08:00:00+00:00",
        )

    def test_no_deadline_ready_job_expires_from_hot_queue_after_60_days(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        old = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
        queue = {
            "articles": [{
                "id": "old-ready",
                "url": "https://example.com/jobs/old-ready",
                "title": "Old Ready Job",
                "status": "ready",
                "content_fetch_status": "success",
                "job_deadline": "",
                "discovered_at": old,
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                saved = article_queue.load_article_queue()
        self.assertEqual(stats["archived_stale_no_deadline"], 1)
        self.assertTrue(saved["articles"][0]["archived"])

    def test_old_job_campaign_memory_is_pruned_with_indexes(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        with TemporaryDirectory() as temp:
            memory = Path(temp) / "job_memory"
            campaign_id = "oldcampaign123"
            identity = "identity123"
            semantic = "semantic123"
            record = {
                "campaign_id": campaign_id,
                "identity_key": identity,
                "semantic_key": semantic,
                "updated_at": "2023-01-01T00:00:00+00:00",
            }
            with patch.object(job_core, "MEMORY_DIR", memory):
                job_core._save_json(
                    job_core._memory_path("campaigns", campaign_id),
                    record,
                )
                job_core._save_json(
                    job_core._memory_path("identity", identity),
                    {"campaign_id": campaign_id},
                )
                job_core._save_json(
                    job_core._memory_path("semantic", semantic),
                    {"campaign_ids": [campaign_id]},
                )
                stats = job_core.maintain_job_memory(now=now, retention_days=730)
                campaign_exists = job_core._memory_path(
                    "campaigns", campaign_id
                ).exists()
                identity_exists = job_core._memory_path(
                    "identity", identity
                ).exists()
                semantic_exists = job_core._memory_path(
                    "semantic", semantic
                ).exists()
        self.assertEqual(stats["campaigns_pruned"], 1)
        self.assertFalse(campaign_exists)
        self.assertFalse(identity_exists)
        self.assertFalse(semantic_exists)

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

    def test_jobs_pre_ai_evidence_preflight_avoids_unrepairable_calls(self):
        structured_missing_application = {
            "url": "https://example.com/jobs/123",
            "job_notice_type": "vacancy",
            "job_notice_type_source": "structured",
            "job_application_url": "",
        }
        self.assertIn(
            "application resource",
            ai._jobs_pre_ai_evidence_error(structured_missing_application),
        )

        heuristic_missing_application = dict(
            structured_missing_application,
            job_notice_type_source="heuristic",
        )
        self.assertEqual(
            ai._jobs_pre_ai_evidence_error(heuristic_missing_application),
            "",
        )

        failed_pdf_evidence = {
            "url": "https://example.com/jobs/123",
            "job_notice_type": "candidate_list",
            "job_notice_type_source": "heuristic",
            "identity_evidence_stage_status": "incomplete",
            "job_document_text_download_failures": 1,
        }
        self.assertIn(
            "document evidence",
            ai._jobs_pre_ai_evidence_error(failed_pdf_evidence),
        )

        incomplete_evidence = {
            "url": "https://example.com/jobs/123",
            "job_notice_type": "candidate_list",
            "job_notice_type_source": "heuristic",
            "identity_evidence_stage_status": "incomplete",
            "job_document_text_download_failures": 0,
        }
        self.assertIn(
            "evidence stage is incomplete",
            ai._jobs_pre_ai_evidence_error(incomplete_evidence),
        )

    def test_invalid_application_evidence_never_calls_ai_provider(self):
        article = {
            "id": "bad-application-job",
            "url": "https://company.example/jobs/12345",
            "source_name": "Official Employer",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_input_package": {
                "title": "Network Engineer",
                "url": "https://company.example/jobs/12345",
                "source_url": "https://company.example/jobs/12345",
                "job_detail_url": "https://company.example/jobs/12345",
                "full_article_text": "verified source evidence",
                "job_notice_type": "vacancy",
                "job_notice_type_source": "official",
                "job_application_url": "https://company.example/careers",
                "job_application_link_kind": "official_job_page",
                "job_action_links": [],
            },
        }
        queue = {"articles": [article]}

        with (
            patch.object(ai, "JOBS_MODE", True),
            patch.object(ai, "load_article_queue", return_value=queue),
            patch.object(ai, "save_article_queue"),
            patch.object(ai, "_generate_with_provider_name") as generate,
            patch.object(ai, "_generate_ai_article") as generate_any,
        ):
            result = ai.process_one_selected_article_with_ai(
                target_article_id="bad-application-job"
            )

        generate.assert_not_called()
        generate_any.assert_not_called()
        self.assertEqual(result["failure_scope"], "article_input")
        self.assertIn("generic application portal", result["message"])

    def test_global_circuit_reopens_when_earliest_provider_recovers(self):
        memory = {
            "avg_time": 0.0,
            "cooldowns": {},
            "provider_circuits": {
                "gemini": {
                    "until": 1100,
                    "fingerprint": "gemini-fp",
                    "category": "quota",
                },
                "groq": {
                    "until": 1500,
                    "fingerprint": "groq-fp",
                    "category": "outage",
                },
            },
            "global_circuit": {},
            "failure_fingerprints": {},
            "fastest_success_model": "",
            "stats": {},
        }
        with (
            patch.object(ai, "_AI_MEMORY_CACHE", memory),
            patch.object(ai.time, "time", return_value=1000),
            patch.object(
                ai,
                "_record_failure_fingerprint",
                return_value=("global-fp", "outage", 5000),
            ),
        ):
            result = ai._open_global_circuit(
                RuntimeError("HTTP 503 unavailable"),
                providers=["gemini", "groq"],
            )

        self.assertEqual(result["until"], 1100)
        self.assertEqual(memory["global_circuit"]["until"], 1100)

    def test_open_global_circuit_does_not_extend_existing_open_circuit(self):
        memory = {
            "avg_time": 0.0,
            "cooldowns": {},
            "provider_circuits": {},
            "global_circuit": {
                "until": 2000,
                "fingerprint": "existing-fp",
                "category": "outage",
            },
            "failure_fingerprints": {},
            "fastest_success_model": "",
            "stats": {},
        }
        with patch.object(ai, "_AI_MEMORY_CACHE", memory), \
             patch.object(ai.time, "time", return_value=1000), \
             patch.object(ai, "_record_failure_fingerprint") as record:
            result = ai._open_global_circuit(
                RuntimeError("HTTP 503 unavailable"),
                providers=["gemini", "groq"],
            )
        self.assertEqual(result["until"], 2000)
        self.assertEqual(result["fingerprint"], "existing-fp")
        record.assert_not_called()

    def test_two_provider_outages_open_global_circuit_before_third_provider(self):
        article = {
            "id": "outage-job",
            "url": "https://example.com/jobs/outage",
            "source_name": "Official Source",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_input_package": {
                "title": "Network Engineer",
                "url": "https://example.com/jobs/outage",
                "full_article_text": "verified source evidence",
                "job_notice_type": "candidate_list",
                "job_notice_type_source": "heuristic",
            },
        }
        queue = {"articles": [article]}
        generate = [
            ai.AIProviderFallbackNeeded("gemini provider failed: HTTP 503 unavailable"),
            ai.AIProviderFallbackNeeded("groq provider failed: HTTP 503 unavailable"),
        ]
        circuit = {
            "until": 2000000000,
            "fingerprint": "global-outage-fp",
            "category": "outage",
        }
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "load_article_queue", return_value=queue), \
             patch.object(ai, "save_article_queue"), \
             patch.object(ai, "_attempt_provider_sequence", return_value=["gemini", "groq", "openrouter"]), \
             patch.object(ai, "_resolve_providers", return_value=["gemini", "groq", "openrouter"]), \
             patch.object(ai, "_build_prompt", return_value="prompt"), \
             patch.object(ai, "_source_stats", return_value=("text", 100, 20)), \
             patch.object(ai, "_skipped_slow_models_count", return_value=0), \
             patch.object(ai, "_generate_with_provider_name", side_effect=generate) as call_provider, \
             patch.object(ai, "_open_global_circuit", return_value=circuit) as open_global, \
             patch.object(
                 ai,
                 "ai_circuit_status",
                 return_value={
                     "global_open": True,
                     "global_fingerprint": "global-outage-fp",
                     "global_category": "outage",
                 },
             ), \
             patch.object(ai, "_global_circuit_until", return_value=2000000000):
            result = ai.process_one_selected_article_with_ai(
                target_article_id="outage-job"
            )

        self.assertEqual(call_provider.call_count, 2)
        open_global.assert_called_once()
        self.assertEqual(result["failure_scope"], "global_outage")
        self.assertEqual(result["failure_category"], "outage")

    def test_provider_preflight_failure_becomes_global_backoff_without_ai_call(self):
        article = {
            "id": "no-provider-job",
            "url": "https://example.com/jobs/no-provider",
            "source_name": "Official Source",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_input_package": {
                "title": "Network Engineer",
                "url": "https://example.com/jobs/no-provider",
                "full_article_text": "verified source evidence",
                "job_notice_type": "candidate_list",
                "job_notice_type_source": "heuristic",
            },
        }
        queue = {"articles": [article]}
        circuit = {
            "until": 2000000000,
            "fingerprint": "config-fp",
            "category": "config",
        }
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "load_article_queue", return_value=queue), \
             patch.object(ai, "save_article_queue"), \
             patch.object(ai, "_build_prompt", return_value="prompt"), \
             patch.object(ai, "_source_stats", return_value=("text", 100, 20)), \
             patch.object(ai, "_skipped_slow_models_count", return_value=0), \
             patch.object(
                 ai,
                 "_attempt_provider_sequence",
                 side_effect=RuntimeError("No AI provider key configured."),
             ), \
             patch.object(
                 ai,
                 "_resolve_providers",
                 side_effect=RuntimeError("No AI provider key configured."),
             ), \
             patch.object(ai, "_generate_with_provider_name") as call_provider, \
             patch.object(ai, "_open_global_circuit", return_value=circuit) as open_global, \
             patch.object(
                 ai,
                 "ai_circuit_status",
                 return_value={
                     "global_open": True,
                     "global_fingerprint": "config-fp",
                     "global_category": "config",
                 },
             ), \
             patch.object(ai, "_global_circuit_until", return_value=2000000000):
            result = ai.process_one_selected_article_with_ai(
                target_article_id="no-provider-job"
            )

        call_provider.assert_not_called()
        open_global.assert_called_once()
        self.assertEqual(result["failure_scope"], "global_outage")
        self.assertEqual(result["failure_category"], "config")

    def test_jobs_provider_failure_uses_one_call_then_rotates(self):
        candidates = [
            {"provider": "groq", "api_key": "k1", "model": "m1"},
            {"provider": "groq", "api_key": "k1", "model": "m2"},
        ]
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "AI_TIMEOUT_RETRIES", 0), \
             patch.object(ai, "_global_circuit_remaining", return_value=0), \
             patch.object(ai, "_provider_circuit_remaining", return_value=0), \
             patch.object(ai, "_provider_candidates", return_value=candidates), \
             patch.object(ai, "_cooldown_remaining", return_value=0), \
             patch.object(ai, "_put_candidate_on_cooldown") as cooldown, \
             patch.object(ai, "_generate_with_candidate", side_effect=RuntimeError("HTTP 503 unavailable")) as generate:
            with self.assertRaises(ai.AIProviderFallbackNeeded):
                ai._generate_with_provider_name("groq", "prompt")
        self.assertEqual(generate.call_count, 1)
        cooldown.assert_called_once()

    def test_article_input_failure_does_not_rotate_providers(self):
        candidates = [
            {"provider": "groq", "api_key": "k1", "model": "m1"},
        ]
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "_global_circuit_remaining", return_value=0), \
             patch.object(ai, "_provider_circuit_remaining", return_value=0), \
             patch.object(ai, "_provider_candidates", return_value=candidates), \
             patch.object(ai, "_cooldown_remaining", return_value=0), \
             patch.object(ai, "_put_candidate_on_cooldown") as cooldown, \
             patch.object(
                 ai,
                 "_generate_with_candidate",
                 side_effect=RuntimeError("HTTP 413 input too long for context length"),
             ) as generate:
            with self.assertRaises(ai.AIArticleInputError):
                ai._generate_with_provider_name("groq", "prompt")
        self.assertEqual(generate.call_count, 1)
        cooldown.assert_not_called()

    def test_failure_fingerprint_ignores_dynamic_numeric_ids(self):
        first, _category = ai._failure_fingerprint(
            RuntimeError("HTTP 429 request 123456 quota exceeded"),
            scope="provider",
            provider="groq",
        )
        second, _category = ai._failure_fingerprint(
            RuntimeError("HTTP 429 request 987654 quota exceeded"),
            scope="provider",
            provider="groq",
        )
        self.assertEqual(first, second)

    def test_jobs_quality_failure_repairs_same_provider_once(self):
        article = {
            "id": "quality-job",
            "url": "https://example.com/jobs/quality",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_input_package": {
                "title": "Network Engineer",
                "url": "https://example.com/jobs/quality",
                "full_article_text": "verified source text",
                "job_notice_type": "vacancy",
                "job_notice_type_source": "heuristic",
            },
        }
        queue = {"articles": [article]}
        response = {
            "title": "عنوان",
            "description": "وصف صالح للمقال",
            "slug": "network-engineer",
            "html_content": "<p>نص</p>",
            "notice_type": "vacancy",
        }
        generated = []

        def fake_generate(provider, prompt, context=None):
            generated.append(provider)
            return "raw", f"{provider}:model"

        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "JOBS_AI_QUALITY_REPAIRS", 1), \
             patch.object(ai, "load_article_queue", return_value=queue), \
             patch.object(ai, "save_article_queue"), \
             patch.object(ai, "_attempt_provider_sequence", return_value=["gemini", "groq"]), \
             patch.object(ai, "_skipped_slow_models_count", return_value=0), \
             patch.object(ai, "_source_stats", return_value=("text", 100, 20)), \
             patch.object(ai, "_build_prompt", return_value="prompt"), \
             patch.object(ai, "_build_expansion_retry_prompt", return_value="repair"), \
             patch.object(ai, "_generate_with_provider_name", side_effect=fake_generate), \
             patch.object(ai, "_parse_complete_ai_json", return_value=dict(response)), \
             patch.object(ai, "_shorten_metadata_once_if_needed", side_effect=lambda data: data), \
             patch.object(ai, "_normalize_ai_output", side_effect=lambda data: data), \
             patch.object(ai, "_finalize_html_content", side_effect=lambda data, package: data), \
             patch.object(ai, "_validate_ai_output", side_effect=ValueError("quality mismatch")), \
             patch.object(ai, "_record_failure_fingerprint", return_value=("quality-fp", "quality", 2000000000)):
            result = ai.process_one_selected_article_with_ai(target_article_id="quality-job")

        self.assertEqual(generated, ["gemini", "gemini"])
        self.assertEqual(result["failure_scope"], "quality")
        self.assertEqual(article["ai_quality_repairs_used"], 1)

    def test_provider_sequence_skips_open_provider_circuits(self):
        with patch.object(ai, "JOBS_MODE", True), \
             patch.object(ai, "_resolve_providers", return_value=["gemini", "groq"]), \
             patch.object(ai, "_global_circuit_remaining", return_value=0), \
             patch.object(
                 ai,
                 "_provider_circuit_remaining",
                 side_effect=lambda provider: 120 if provider == "gemini" else 0,
             ):
            self.assertEqual(ai._attempt_provider_sequence(), ["groq"])

    def test_ai_retry_backoff_preflight_makes_no_provider_call(self):
        article = {
            "id": "backoff-job",
            "url": "https://example.com/jobs/backoff",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_status": "failed",
            "ai_failure_scope": "quality",
            "ai_failure_fingerprint": "quality-fp",
            "ai_failure_category": "quality",
            "ai_retry_after": "2099-01-01T00:00:00+00:00",
            "ai_input_package": {
                "title": "Network Engineer",
                "url": "https://example.com/jobs/backoff",
                "full_article_text": "verified source evidence",
                "job_notice_type": "vacancy",
                "job_notice_type_source": "heuristic",
            },
        }
        queue = {"articles": [article]}
        with (
            patch.object(ai, "JOBS_MODE", True),
            patch.object(ai, "load_article_queue", return_value=queue),
            patch.object(ai, "save_article_queue"),
            patch.object(ai, "_attempt_provider_sequence") as sequence,
            patch.object(ai, "_generate_with_provider_name") as generate,
            patch.object(
                ai,
                "_failure_fingerprint_retry_until",
                return_value=0,
            ),
        ):
            result = ai.process_one_selected_article_with_ai(
                target_article_id="backoff-job"
            )

        sequence.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(result["failure_scope"], "retry_backoff")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(article["ai_quality_status"], "retry_backoff")

    def test_run_ai_cross_candidate_retry_is_capped_to_one(self):
        failed = {
            "id": "failed-job",
            "url": "https://example.com/jobs/failed",
            "ai_failure_scope": "quality",
            "ai_failure_fingerprint": "quality-fp",
            "ai_retry_after": "2099-01-01T00:00:00+00:00",
        }
        next_one = {"id": "next-one", "url": "https://example.com/jobs/next-one"}
        next_two = {"id": "next-two", "url": "https://example.com/jobs/next-two"}

        with (
            patch.object(main, "JOBS_MODE", True),
            patch.object(main, "JOBS_AI_CROSS_CANDIDATE_RETRIES", 1),
            patch.object(main, "_mark_candidate_failure_for_retry", return_value=failed),
            patch.object(
                main,
                "_select_retry_candidate",
                side_effect=[next_one, next_two],
            ) as select,
            patch.object(
                main,
                "_process_hourly_target",
                return_value={
                    "completed": False,
                    "article": next_one,
                    "reason": "AI quality failed",
                    "step_reached": "run-ai",
                },
            ) as process,
            patch.object(main, "ai_circuit_status", return_value={"global_open": False}),
        ):
            success, retries = main._retry_after_single_candidate_failure(
                failed,
                "run-ai",
                "quality mismatch",
                "live",
                {},
                {"failed-job"},
            )

        self.assertIsNone(success)
        self.assertEqual(len(retries), 1)
        self.assertEqual(select.call_count, 1)
        self.assertEqual(process.call_count, 1)

    def test_mark_candidate_failure_preserves_ai_retry_backoff(self):
        article = {
            "id": "backoff-job",
            "url": "https://example.com/jobs/backoff",
            "ai_failure_scope": "retry_backoff",
            "ai_failure_fingerprint": "quality-fp",
            "ai_retry_after": "2099-01-01T00:00:00+00:00",
        }
        with patch.object(main, "mark_article_recent_failure") as mark:
            result = main._mark_candidate_failure_for_retry(
                article,
                "run-ai",
                "AI retry backoff active",
            )

        self.assertIs(result, article)
        mark.assert_not_called()

    def test_retry_backoff_does_not_increment_failure_or_rotate_candidate(self):
        failed = {
            "id": "backoff-job",
            "url": "https://example.com/jobs/backoff",
            "ai_failure_scope": "retry_backoff",
            "ai_failure_fingerprint": "quality-fp",
            "ai_retry_after": "2099-01-01T00:00:00+00:00",
        }
        with (
            patch.object(main, "_mark_candidate_failure_for_retry") as mark,
            patch.object(main, "_select_retry_candidate") as select,
        ):
            success, retries = main._retry_after_single_candidate_failure(
                failed,
                "run-ai",
                "AI retry backoff active",
                "live",
                {},
                {"backoff-job"},
            )

        self.assertIsNone(success)
        self.assertEqual(retries, [])
        mark.assert_not_called()
        select.assert_not_called()

    def test_hourly_jobs_batch_stops_after_one_extra_ai_candidate(self):
        candidates = [
            {"id": "job-1", "url": "https://example.com/jobs/1", "source_name": "S1"},
            {"id": "job-2", "url": "https://example.com/jobs/2", "source_name": "S2"},
            {"id": "job-3", "url": "https://example.com/jobs/3", "source_name": "S3"},
        ]
        failed_results = [
            {
                "completed": False,
                "article": candidates[0],
                "reason": "quality mismatch",
                "step_reached": "run-ai",
                "failure_scope": "quality",
                "failure_fingerprint": "fp-1",
            },
            {
                "completed": False,
                "article": candidates[1],
                "reason": "quality mismatch",
                "step_reached": "run-ai",
                "failure_scope": "quality",
                "failure_fingerprint": "fp-2",
            },
        ]
        hourly = {
            "total": 0,
            "by_category": {},
            "by_category_source": {},
        }
        cleanup = {"expired_archived": 0, "missing_date_archived": 0}

        with (
            patch.object(main, "JOBS_MODE", True),
            patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1),
            patch.object(main, "HOURLY_POST_LIMIT", 10),
            patch.object(main, "CATEGORY_POSTS_PER_HOUR", 5),
            patch.object(main, "JOBS_AI_CROSS_CANDIDATE_RETRIES", 1),
            patch.object(main, "_effective_publish_mode", return_value="live"),
            patch.object(main, "load_sources", return_value=[]),
            patch.object(main, "_available_category_labels", return_value=["jobs"]),
            patch.object(main, "_published_hourly_counts", return_value=hourly),
            patch.object(main, "run_fetch_only", return_value={}),
            patch.object(main, "archive_expired_queue_articles", return_value=cleanup),
            patch.object(main, "run_score_only", return_value={}),
            patch.object(main, "run_enrich_only", return_value={}),
            patch.object(main, "resolve_identity_pending_articles", return_value={}),
            patch.object(main, "ai_circuit_status", return_value={"global_open": False}),
            patch.object(
                main,
                "_lock_hourly_candidate",
                side_effect=candidates,
            ) as lock,
            patch.object(
                main,
                "_process_hourly_target",
                side_effect=failed_results,
            ) as process,
        ):
            result = main.run_hourly_category_cycle()

        self.assertFalse(result["completed"])
        self.assertEqual(process.call_count, 2)
        self.assertEqual(lock.call_count, 2)
        self.assertEqual(result["failed_count"], 2)

    def test_global_ai_outage_stops_cross_candidate_retry(self):
        failed = {
            "id": "failed-job",
            "url": "https://example.com/jobs/failed",
            "ai_failure_scope": "global_outage",
            "ai_failure_fingerprint": "global-fp",
            "ai_retry_after": "2099-01-01T00:00:00+00:00",
        }
        with patch.object(main, "_mark_candidate_failure_for_retry", return_value=failed), \
             patch.object(main, "_select_retry_candidate") as select:
            success, retries = main._retry_after_single_candidate_failure(
                failed,
                "run-ai",
                "provider rotation exhausted",
                "live",
                {},
                {"failed-job"},
            )
        self.assertIsNone(success)
        self.assertEqual(retries, [])
        select.assert_not_called()

    def test_candidate_failure_fingerprint_increases_backoff(self):
        row = {
            "id": "candidate",
            "url": "https://example.com/jobs/candidate",
            "status": "ready",
        }
        queue = {"articles": [row]}
        with patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue"):
            article_queue.mark_article_recent_failure(
                article_id="candidate",
                stage="prepare-ai",
                reason="missing evidence 12345",
                cooldown_minutes=15,
            )
            first_fp = row["candidate_failure_fingerprint"]
            first_backoff = row["candidate_failure_backoff_minutes"]
            article_queue.mark_article_recent_failure(
                article_id="candidate",
                stage="prepare-ai",
                reason="missing evidence 67890",
                cooldown_minutes=15,
            )

        self.assertEqual(row["candidate_failure_fingerprint"], first_fp)
        self.assertGreater(row["candidate_failure_backoff_minutes"], first_backoff)
        self.assertEqual(row["candidate_failure_repeat_count"], 2)

    def test_provider_cooldowns_are_error_specific(self):
        self.assertGreater(
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 403 forbidden")),
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 503 unavailable")),
        )
        self.assertGreater(
            ai._cooldown_seconds_for_error(RuntimeError("HTTP 429 rate limit")),
            ai._cooldown_seconds_for_error(ai.AIProviderEmptyResponse("empty response")),
        )

    def test_jobs_slug_is_english_and_digit_free(self):
        slug = ai._normalize_job_english_slug(
            "Orange Business Cybersecurity Consultant Casablanca 2026"
        )
        self.assertEqual(
            slug,
            "orange-business-cybersecurity-consultant-casablanca",
        )
        self.assertNotRegex(slug, r"\d")

        fallback = ai._fallback_job_english_slug(
            {
                "job_company": "جامعة محمد الأول",
                "job_title": "أستاذ محاضر",
                "job_location": "وجدة",
            }
        )
        self.assertIn("university", fallback)
        self.assertIn("lecturer", fallback)
        self.assertIn("oujda", fallback)
        self.assertNotRegex(fallback, r"\d")

    def test_jobs_prompt_requires_professional_semantic_html_body(self):
        package = {
            "job_title": "مهندس شبكات",
            "job_company": "Example Company",
            "job_location": "الدار البيضاء",
            "job_application_url": "https://example.com/apply",
            "job_document_links": [{"url": "https://example.com/notice.pdf", "label": "الإعلان الرسمي"}],
            "job_notice_type": "vacancy",
            "desired_slug": "example-network-engineer",
        }
        with patch.object(ai, "JOBS_MODE", True):
            prompt = ai._build_prompt(package)
        self.assertIn("<h2>تفاصيل الوظيفة</h2>", prompt)
        self.assertIn("<h2>المهام والمسؤوليات</h2>", prompt)
        self.assertIn("<h2>الشروط والمؤهلات</h2>", prompt)
        self.assertIn("<h2>الملفات والوثائق الرسمية</h2>", prompt)
        self.assertIn("<h2>التقديم والروابط الرسمية</h2>", prompt)
        self.assertIn("NEVER add <h1>", prompt)
        self.assertIn("No copied boilerplate solely to increase word count", prompt)

    def test_deterministic_jobs_body_uses_professional_plain_html_sections(self):
        package = {
            "job_title": "مهندس شبكات",
            "job_company": "Example Company",
            "job_location": "الدار البيضاء",
            "job_contract_type": "CDI",
            "job_application_url": "https://example.com/apply",
            "job_application_link_kind": "direct_apply",
            "job_document_links": [{"url": "https://example.com/notice.pdf", "label": "الإعلان الرسمي"}],
            "job_notice_type": "vacancy",
            "desired_slug": "example-network-engineer",
        }
        with patch.object(ai, "JOBS_MODE", True):
            data = ai._deterministic_job_article(package)
            data = ai._finalize_html_content(data, package)
        html = data["html_content"]
        self.assertIn("<h2>تفاصيل الوظيفة</h2>", html)
        self.assertIn("<h2>التقديم والروابط الرسمية</h2>", html)
        self.assertIn(package["job_application_url"], html)
        self.assertIn(package["job_document_links"][0]["url"], html)
        self.assertNotRegex(html, r"(?i)<(?:script|style|iframe|form|h1)\\b")
        self.assertNotRegex(html, r"(?i)\\sstyle=")

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

    def test_verified_fact_manifest_marks_only_explicit_supported_facts_high(self):
        article = {
            "url": "https://example.gov.ma/jobs/42",
            "job_detail_url": "https://example.gov.ma/jobs/42",
            "official_source": True,
            "job_official_source": True,
            "content_fetch_status": "success",
            "full_article_text": (
                "آخر أجل للترشيح هو 15/10/2026. عدد المناصب 3. "
                "التقديم عبر الرابط الرسمي."
            ),
            "job_deadline": "2026-10-15",
            "job_deadline_display": "15 أكتوبر 2026",
            "job_number_of_positions": 3,
            "job_application_url": "https://example.gov.ma/jobs/42/apply",
            "job_application_link_kind": "direct_apply",
            "source_tables": [
                {"rows": [
                    ["التخصص", "الأمن السيبراني"],
                    ["الاختبار", "اختبار كتابي"],
                    ["ملاحظة إدارية", "الرقم 7788 للاستعمال الداخلي"],
                ]}
            ],
            "source_tables_count": 1,
            "source_tables_truncated": False,
            "job_document_links": [],
        }
        manifest = fact_manifest.build_verified_fact_manifest(article)

        self.assertEqual(manifest["facts"]["deadline"][0]["confidence"], "high")
        self.assertEqual(manifest["facts"]["positions"][0]["confidence"], "high")
        self.assertEqual(manifest["facts"]["application"][0]["confidence"], "high")
        self.assertEqual(manifest["facts"]["specialties"][0]["confidence"], "high")
        self.assertEqual(manifest["facts"]["tests"][0]["confidence"], "high")
        self.assertNotIn("7788", str(manifest))

    def test_manifest_extracts_values_under_explicit_table_headers(self):
        article = {
            "url": "https://example.gov.ma/jobs/99",
            "job_detail_url": "https://example.gov.ma/jobs/99",
            "official_source": True,
            "job_official_source": True,
            "source_tables": [{
                "rows": [
                    ["التخصص", "عدد المناصب", "الاختبار"],
                    ["الأمن السيبراني", "2", "اختبار كتابي"],
                    ["الشبكات", "1", "اختبار شفوي"],
                ]
            }],
            "source_tables_count": 1,
            "source_tables_truncated": False,
            "job_document_links": [],
        }
        manifest = fact_manifest.build_verified_fact_manifest(article)

        specialties = [
            fact["value"] for fact in manifest["facts"].get("specialties", [])
        ]
        tests = [
            fact["value"] for fact in manifest["facts"].get("tests", [])
        ]
        self.assertEqual(specialties, ["الأمن السيبراني", "الشبكات"])
        self.assertEqual(tests, ["اختبار كتابي", "اختبار شفوي"])
        self.assertNotIn("عدد المناصب", specialties)
        self.assertTrue(all(
            fact["confidence"] == "high"
            for fact in manifest["facts"]["specialties"]
        ))

    def test_manifest_extracts_explicit_pdf_label_values(self):
        article = {
            "url": "https://example.gov.ma/jobs/77",
            "job_detail_url": "https://example.gov.ma/jobs/77",
            "official_source": True,
            "job_official_source": True,
            "source_tables": [],
            "source_tables_count": 0,
            "source_tables_truncated": False,
            "job_document_texts": [{
                "page_number": 1,
                "text": (
                    "التخصص: الذكاء الاصطناعي\n"
                    "الاختبار: اختبار كتابي\n"
                    "ملاحظة داخلية بدون تسمية موثقة 4455"
                ),
            }],
            "job_document_links": [],
        }
        manifest = fact_manifest.build_verified_fact_manifest(article)

        specialties = [
            fact["value"] for fact in manifest["facts"].get("specialties", [])
        ]
        tests = [
            fact["value"] for fact in manifest["facts"].get("tests", [])
        ]
        self.assertIn("الذكاء الاصطناعي", specialties)
        self.assertIn("اختبار كتابي", tests)
        self.assertTrue(all(
            fact["confidence"] == "high"
            for fact in manifest["facts"].get("specialties", [])
        ))
        self.assertNotIn("4455", str(manifest))

    def test_manifest_requiredness_follows_notice_confidence(self):
        base = {
            "url": "https://example.com/jobs/42",
            "job_detail_url": "https://example.com/jobs/42",
            "official_source": True,
            "job_official_source": True,
            "full_article_text": (
                "آخر أجل للترشيح 15/10/2026. "
                "التقديم عبر https://example.com/jobs/42/apply"
            ),
            "job_deadline": "2026-10-15",
            "job_application_url": "https://example.com/jobs/42/apply",
            "job_application_link_kind": "direct_apply",
            "job_notice_type": "vacancy",
            "source_tables": [],
            "job_document_links": [],
        }

        heuristic = dict(base, job_notice_type_source="heuristic")
        heuristic_manifest = fact_manifest.build_verified_fact_manifest(heuristic)
        self.assertFalse(
            heuristic_manifest["facts"]["deadline"][0]["required_in_output"]
        )
        self.assertFalse(
            heuristic_manifest["facts"]["application"][0]["required_in_output"]
        )

        official = dict(base, job_notice_type_source="official")
        official_manifest = fact_manifest.build_verified_fact_manifest(official)
        self.assertTrue(
            official_manifest["facts"]["deadline"][0]["required_in_output"]
        )
        self.assertTrue(
            official_manifest["facts"]["application"][0]["required_in_output"]
        )

    def test_manifest_specific_detail_url_is_high_required_fact(self):
        article = {
            "url": "https://example.com/jobs/42",
            "job_detail_url": "https://example.com/jobs/42",
            "official_source": True,
            "job_official_source": True,
            "job_notice_type": "update",
            "job_notice_type_source": "official",
            "source_tables": [],
            "job_document_links": [],
        }
        manifest = fact_manifest.build_verified_fact_manifest(article)
        detail = manifest["facts"]["detail"][0]
        self.assertEqual(detail["confidence"], "high")
        self.assertTrue(detail["required_in_output"])

    def test_manifest_high_fact_missing_blocks_but_medium_fact_only_warns(self):
        high_manifest = {
            "facts": {
                "positions": [{
                    "value": 3,
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }]
            },
            "warnings": [],
        }
        blocking, warnings = fact_manifest.validate_output_against_manifest(
            high_manifest,
            "شركة Example تعلن عن توظيف مهندسين",
            "<p>تفاصيل موثقة عن عملية التوظيف.</p>",
        )
        self.assertTrue(blocking)
        self.assertFalse(warnings)

        medium_manifest = {
            "facts": {
                "salary": [{
                    "value": "12000 MAD",
                    "source": "extracted_field",
                    "confidence": "medium",
                    "blocking": False,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }]
            },
            "warnings": [],
        }
        blocking, warnings = fact_manifest.validate_output_against_manifest(
            medium_manifest,
            "شركة Example تعلن عن توظيف مهندس نظم",
            "<p>تفاصيل المنصب وطريقة التقديم الرسمية.</p>",
        )
        self.assertEqual(blocking, [])
        self.assertTrue(any("salary" in warning for warning in warnings))

    def test_manifest_contradicting_high_position_count_blocks(self):
        manifest = {
            "facts": {
                "positions": [{
                    "value": 3,
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }]
            },
            "warnings": [],
        }
        blocking, _warnings = fact_manifest.validate_output_against_manifest(
            manifest,
            "شركة Example تعلن عن توظيف 5 مهندسين",
            "<p>تفتح الشركة 5 مناصب ضمن هذا الإعلان.</p>",
        )
        self.assertTrue(any("contradicts" in reason for reason in blocking))

    def test_manifest_accepts_arabic_deadline_format_and_grouped_salary(self):
        manifest = {
            "facts": {
                "deadline": [{
                    "value": "2026-10-15",
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": ["15 أكتوبر 2026"],
                    "meta": {},
                }],
                "salary": [{
                    "value": "10 000 MAD",
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }],
            },
            "warnings": [],
        }
        blocking, _warnings = fact_manifest.validate_output_against_manifest(
            manifest,
            "شركة Example تعلن عن توظيف مهندس",
            "<p>آخر أجل للترشيح هو 15 أكتوبر 2026، والراتب 10 000 MAD.</p>",
        )
        self.assertEqual(blocking, [])

    def test_ai_input_package_contains_verified_fact_manifest(self):
        article = {
            "title": "Network Engineer",
            "fetched_title": "Network Engineer",
            "url": "https://example.com/jobs/42",
            "source_name": "Example Careers",
            "official_source": True,
            "content_fetch_status": "success",
            "full_article_text": "عدد المناصب 2 وطريقة التقديم عبر الرابط الرسمي.",
            "job_number_of_positions": 2,
            "job_detail_url": "https://example.com/jobs/42",
            "job_application_url": "https://example.com/jobs/42/apply",
            "job_application_link_kind": "direct_apply",
            "job_notice_type": "vacancy",
            "job_notice_type_source": "official",
            "source_tables": [],
            "job_document_links": [],
        }
        with patch.object(article_processor, "JOBS_MODE", True):
            package = article_processor._build_ai_input_package(article)
        self.assertIn("verified_fact_manifest", package)
        self.assertEqual(
            package["verified_fact_manifest"]["facts"]["positions"][0]["confidence"],
            "high",
        )
        self.assertIs(
            article["verified_fact_manifest"],
            package["verified_fact_manifest"],
        )

    def test_jobs_gate_downgrades_fact_repetition_heuristics_to_warning(self):
        manifest = {
            "version": 1,
            "facts": {
                "positions": [{
                    "value": 3,
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }],
                "notice_type": [{
                    "value": "update",
                    "source": "verified",
                    "confidence": "high",
                    "blocking": False,
                    "required_in_output": False,
                    "aliases": [],
                    "meta": {},
                }],
            },
            "warnings": [],
        }
        package = {
            "url": "https://example.com/jobs/update-55",
            "source_url": "https://example.com/jobs/update-55",
            "job_notice_type": "update",
            "verified_fact_manifest": manifest,
        }
        article = {
            "url": package["url"],
            "source_url": package["source_url"],
            "seo_title": "تحديث رسمي حول إجراءات مباراة توظيف تقنيين بإحدى المؤسسات",
            "seo_description": (
                "تحديث رسمي يوضح المرحلة الحالية من المباراة والمعطيات المؤكدة "
                "التي تهم المترشحين وفق الإعلان المنشور من الجهة المنظمة."
            ),
            "final_html": (
                "<p>تشمل المعطيات الحالية 3 مناصب ضمن هذه المرحلة.</p>"
                "<h2>المعطيات المؤكدة</h2>"
                "<table><tbody><tr><th>عدد المناصب</th><td>3 مناصب</td></tr></tbody></table>"
            ),
            "job_notice_type": "update",
            "ai_input_package": package,
        }
        with patch.object(quality_gate, "JOBS_MODE", True):
            result = quality_gate.validate_before_publish(
                article,
                check_duplicate=False,
            )

        self.assertTrue(result.passed, result.reason)
        self.assertTrue(any("repeat" in warning.lower() for warning in result.warnings))

    def test_jobs_gate_keeps_h1_as_structural_blocker(self):
        package = {
            "url": "https://example.com/jobs/update-56",
            "source_url": "https://example.com/jobs/update-56",
            "job_notice_type": "update",
            "verified_fact_manifest": {
                "version": 1,
                "facts": {},
                "warnings": [],
            },
        }
        article = {
            "url": package["url"],
            "source_url": package["source_url"],
            "seo_title": "تحديث رسمي حول إجراءات مباراة توظيف تقنيين بإحدى المؤسسات",
            "seo_description": (
                "تحديث رسمي يوضح المرحلة الحالية من المباراة والمعطيات المؤكدة "
                "التي تهم المترشحين وفق الإعلان المنشور من الجهة المنظمة."
            ),
            "final_html": (
                "<h1>عنوان داخل جسم المقال</h1>"
                "<p>تفاصيل موثقة حول المرحلة الحالية من الإجراءات.</p>"
            ),
            "job_notice_type": "update",
            "ai_input_package": package,
        }
        with patch.object(quality_gate, "JOBS_MODE", True):
            result = quality_gate.validate_before_publish(
                article,
                check_duplicate=False,
            )

        self.assertFalse(result.passed)
        self.assertIn("h1", result.reason.lower())

    def test_jobs_gate_keeps_medium_manifest_and_arbitrary_rows_as_warnings(self):
        manifest = {
            "version": 1,
            "facts": {
                "salary": [{
                    "value": "12000 MAD",
                    "source": "extracted_field",
                    "confidence": "medium",
                    "blocking": False,
                    "required_in_output": True,
                    "aliases": [],
                    "meta": {},
                }],
                "notice_type": [{
                    "value": "update",
                    "source": "heuristic",
                    "confidence": "heuristic",
                    "blocking": False,
                    "required_in_output": False,
                    "aliases": [],
                    "meta": {},
                }],
            },
            "warnings": [],
        }
        package = {
            "url": "https://example.com/jobs/update-42",
            "source_url": "https://example.com/jobs/update-42",
            "job_notice_type": "update",
            "job_notice_type_source": "heuristic",
            "verified_fact_manifest": manifest,
            "source_tables": [
                {"rows": [["ملاحظة إدارية", "الرقم 7788 للاستعمال الداخلي"]]}
            ],
        }
        article = {
            "url": package["url"],
            "source_url": package["source_url"],
            "seo_title": "تحديث حول إجراءات مباراة توظيف تقنيين بإحدى المؤسسات المغربية",
            "seo_description": (
                "تحديث موثق يوضح مستجدات إجراءات المباراة والخطوات الحالية "
                "للمترشحين بالاعتماد على المعلومات المنشورة في الإعلان."
            ),
            "final_html": (
                "<p>نشرت الجهة المنظمة توضيحات جديدة حول المرحلة الحالية من "
                "الإجراءات، مع الإبقاء على التفاصيل المؤكدة فقط.</p>"
            ),
            "job_notice_type": "update",
            "ai_input_package": package,
        }
        with patch.object(quality_gate, "JOBS_MODE", True):
            result = quality_gate.validate_before_publish(
                article,
                check_duplicate=False,
            )

        self.assertTrue(result.passed, result.reason)
        self.assertTrue(any("salary" in warning for warning in result.warnings))
        self.assertFalse(any("7788" in warning for warning in result.warnings))

    def test_ai_success_preserves_manifest_warnings(self):
        article = {
            "ai_input_package": {},
            "ai_quality_warnings": ["medium salary fact omitted"],
        }
        data = {
            "title": "شركة Example تعلن عن توظيف مهندس نظم في المغرب",
            "description": (
                "تفاصيل موثقة حول فرصة توظيف مهندس نظم ومتطلبات المنصب "
                "والمعلومات الرسمية المتاحة للمرشحين."
            ),
            "slug": "example-systems-engineer",
            "html_content": "<p>تفاصيل موثقة حول المنصب.</p>",
            "notice_type": "vacancy",
        }
        with patch.object(ai, "JOBS_MODE", True):
            ai._apply_success(article, data, "test-provider")

        self.assertEqual(
            article["ai_quality_warnings"],
            ["medium salary fact omitted"],
        )

    def test_jobs_quality_gate_rejects_marketing_filler(self):
        article = {
            "url": "https://example.com/jobs/42",
            "job_application_url": "https://example.com/apply/42",
            "seo_title": "شركة Example تعلن عن توظيف مهندس نظم في الدار البيضاء",
            "seo_description": "فرصة توظيف موثقة لدى شركة Example لمهندس نظم في الدار البيضاء، مع تفاصيل المنصب وطريقة التقديم المباشر عبر الرابط الرسمي.",
            "final_html": (
                "<p>الشركة الرائدة تقدم "
                + " ".join(["معلومة"] * 125)
                + "</p><h2>التقديم</h2>"
                + "<p><a href='https://example.com/apply/42'>التقديم</a></p>"
            ),
        }
        with patch.object(quality_gate, "JOBS_MODE", True):
            result = quality_gate.validate_before_publish(article, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("promotional", result.reason)

    def test_jobs_finalizer_appends_every_official_file_and_detail_page(self):
        package = {
            "job_application_url": "https://example.com/apply/42",
            "job_application_link_kind": "direct_apply",
            "job_detail_url": "https://example.com/jobs/42",
            "job_document_links": [
                {"url": "https://example.com/docs/avis.pdf", "label": "الإعلان الرسمي"},
                {"url": "https://example.com/docs/decision.pdf", "label": "قرار المباراة"},
            ],
        }
        with patch.object(ai, "JOBS_MODE", True):
            html = ai._append_job_action_links_if_missing(
                "<p>" + " ".join(["تفصيل"] * 100) + "</p>",
                package,
            )
        self.assertIn(package["job_application_url"], html)
        self.assertIn(package["job_detail_url"], html)
        for row in package["job_document_links"]:
            self.assertIn(row["url"], html)
        self.assertIn("الملفات والوثائق الرسمية", html)

    def test_jobs_action_links_are_standardized_and_not_duplicated(self):
        package = {
            "job_application_url": "https://example.com/jobs/42/apply",
            "job_application_link_kind": "direct_apply",
            "job_detail_url": "https://example.com/jobs/42",
            "job_document_links": [
                {"url": "https://example.com/docs/notice.pdf", "label": "الإعلان الرسمي"},
            ],
        }
        source = (
            "<p>مقدمة الوظيفة.</p>"
            "<p><a href='https://example.com/jobs/42/apply'>رابط قديم</a></p>"
            "<p><a href='https://example.com/docs/notice.pdf'>ملف</a></p>"
        )
        with patch.object(ai, "JOBS_MODE", True):
            html = ai._append_job_action_links_if_missing(source, package)
        self.assertEqual(html.count(package["job_application_url"]), 1)
        self.assertEqual(html.count(package["job_document_links"][0]["url"]), 1)
        self.assertIn("jobApplyButton", html)
        self.assertIn("jobDocumentButton", html)
        self.assertIn("التقديم الآن عبر الرابط الرسمي", html)
        self.assertIn("فتح أو تحميل الوثيقة الرسمية", html)

    def test_jobs_quality_gate_accepts_verified_public_application_channel(self):
        portal = "https://recrutement.enssup.gov.ma/"
        detail = (
            "https://www.emploi-public.ma/ar/تفاصيل/المباريات/"
            "85a046f8-2af5-4f26-8b3f-a811967e2a4e"
        )
        package = {
            "url": detail,
            "source_url": detail,
            "official_source": True,
            "job_official_source": True,
            "job_notice_type": "competition",
            "job_application_url": portal,
            "job_application_link_kind": "official_application_channel",
            "job_detail_url": detail,
            "job_action_links": [
                {"url": portal, "label": "إيداع الترشيح", "kind": "apply"}
            ],
            "job_document_links": [],
        }
        article = {
            "url": detail,
            "source_url": detail,
            "seo_title": "جامعة مغربية تعلن عن مباراة توظيف تقنيين من الدرجة الثالثة",
            "seo_description": (
                "تفاصيل مباراة توظيف تقنيين من الدرجة الثالثة مع شروط الترشيح "
                "والمواعيد والروابط الرسمية المعتمدة لإيداع الطلبات."
            ),
            "final_html": (
                "<p>تتوفر المعطيات الرسمية الخاصة بهذه المباراة وشروط المشاركة "
                "والمراحل المطلوبة للترشيح وفق الإعلان المنشور من الجهة المنظمة.</p>"
                f"<p><a href='{portal}'>منصة الترشيح الرسمية</a></p>"
                f"<p><a href='{detail}'>صفحة الإعلان الرسمية</a></p>"
            ),
            "job_notice_type": "competition",
            "job_application_url": portal,
            "job_application_link_kind": "official_application_channel",
            "job_detail_url": detail,
            "ai_input_package": package,
        }
        with patch.object(quality_gate, "JOBS_MODE", True):
            result = quality_gate.validate_before_publish(article, check_duplicate=False)
        self.assertTrue(result.passed, result.reason)

    def test_public_application_channel_is_never_labeled_direct_apply(self):
        portal = "https://recrutement.enssup.gov.ma/"
        package = {
            "job_application_url": portal,
            "job_application_link_kind": "official_application_channel",
            "job_detail_url": (
                "https://www.emploi-public.ma/ar/تفاصيل/المباريات/"
                "85a046f8-2af5-4f26-8b3f-a811967e2a4e"
            ),
            "job_document_links": [],
        }
        source = (
            "<p>معلومات المباراة.</p>"
            f"<p><a href='{portal}'>التقديم المباشر</a></p>"
        )
        with patch.object(ai, "JOBS_MODE", True):
            html = ai._append_job_action_links_if_missing(source, package)
        self.assertIn("منصة الترشيح الرسمية", html)
        self.assertIn("jobApplicationChannelButton", html)
        self.assertNotIn(">التقديم المباشر</a>", html)

    def test_jobs_document_pages_are_appended_in_sequence(self):
        package = {
            "job_document_page_images": [
                {
                    "document_url": "https://example.com/docs/notice.pdf",
                    "document_label": "إعلان وشروط المباراة",
                    "page_number": 1,
                    "url": "https://raw.example/page-01.jpg",
                    "alt": "إعلان وشروط المباراة — الصفحة 1",
                },
                {
                    "document_url": "https://example.com/docs/notice.pdf",
                    "document_label": "إعلان وشروط المباراة",
                    "page_number": 2,
                    "url": "https://raw.example/page-02.jpg",
                    "alt": "إعلان وشروط المباراة — الصفحة 2",
                },
            ]
        }
        with patch.object(ai, "JOBS_MODE", True):
            html = ai._append_job_document_page_images("<p>مقدمة</p>", package)
        self.assertIn("صفحات الوثيقة الرسمية", html)
        self.assertLess(html.index("page-01.jpg"), html.index("page-02.jpg"))
        self.assertEqual(html.count("jobDocPageImage"), 2)

    def test_identity_pdf_priority_follows_notice_type(self):
        documents = [
            {"url": "https://example.com/results.pdf", "label": "لائحة النتائج"},
            {"url": "https://example.com/notice.pdf", "label": "الإعلان الرسمي للمباراة"},
            {"url": "https://example.com/conditions.pdf", "label": "شروط المباراة"},
            {"url": "https://example.com/list.pdf", "label": "Liste des admis"},
        ]

        competition = {
            "job_notice_type": "competition",
            "job_document_links": documents,
        }
        competition_selected = job_document_renderer._eligible_documents(
            competition,
            max_documents=2,
        )
        self.assertEqual(
            [item["url"] for item in competition_selected],
            [
                "https://example.com/notice.pdf",
                "https://example.com/conditions.pdf",
            ],
        )

        results = {
            "job_notice_type": "results",
            "job_document_links": documents,
        }
        result_selected = job_document_renderer._eligible_documents(
            results,
            max_documents=2,
        )
        self.assertEqual(
            [item["url"] for item in result_selected],
            [
                "https://example.com/results.pdf",
                "https://example.com/list.pdf",
            ],
        )

    def test_official_pdf_renderer_creates_readable_page_images(self):
        import fitz
        import tempfile
        from pathlib import Path

        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "Official job conditions")
        payload = pdf.tobytes()
        pdf.close()

        class Response:
            headers = {"Content-Type": "application/pdf"}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                yield payload

        article = {
            "id": "pdf-job",
            "seo_slug": "example-engineer-casablanca",
            "job_notice_type": "vacancy",
            "job_document_links": [
                {
                    "url": "https://example.com/docs/conditions.pdf",
                    "label": "إعلان وشروط المباراة",
                    "context": "الشروط الرسمية",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(job_document_renderer.requests, "get", return_value=Response()):
            pages = job_document_renderer.render_job_document_pages(
                article,
                output_root=Path(temp),
                raw_base="https://raw.example/main",
            )
            self.assertEqual(len(pages), 1)
            self.assertTrue(Path(pages[0]["path"]).exists())
            self.assertEqual(pages[0]["page_number"], 1)
            self.assertTrue(pages[0]["url"].endswith(".jpg"))

    def test_jobs_quality_gate_rejects_scripts_and_missing_official_files(self):
        package = {
            "url": "https://example.com/jobs/42",
            "official_source": True,
            "job_official_source": True,
            "job_notice_type": "vacancy",
            "job_application_url": "https://example.com/apply/42",
            "job_detail_url": "https://example.com/jobs/42",
            "job_document_links": [
                {"url": "https://example.com/docs/avis.pdf", "label": "الإعلان الرسمي"},
            ],
        }
        base = {
            "url": package["url"],
            "job_application_url": package["job_application_url"],
            "job_detail_url": package["job_detail_url"],
            "job_document_links": package["job_document_links"],
            "seo_title": "شركة Example تعلن عن توظيف مهندس نظم في الدار البيضاء",
            "seo_description": "فرصة توظيف موثقة لدى شركة Example لمهندس نظم في الدار البيضاء، مع تفاصيل المنصب وروابط التقديم والوثائق الرسمية.",
            "ai_input_package": package,
        }
        html = (
            "<p>" + " ".join(["معلومة"] * 125) + "</p>"
            "<h2>التقديم</h2>"
            f"<p><a href='{package['job_application_url']}'>التقديم</a></p>"
        )
        with patch.object(quality_gate, "JOBS_MODE", True):
            missing = quality_gate.validate_before_publish(
                dict(base, final_html=html),
                check_duplicate=False,
            )
            self.assertFalse(missing.passed)
            self.assertIn("document", missing.reason)

            with_doc = (
                html
                + f"<p><a href='{package['job_document_links'][0]['url']}'>PDF</a></p>"
            )
            scripted = quality_gate.validate_before_publish(
                dict(base, final_html=with_doc + "<script>alert(1)</script>"),
                check_duplicate=False,
            )
            self.assertFalse(scripted.passed)
            self.assertIn("script", scripted.reason)

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
            "last_publish_at": (now - timedelta(minutes=3)).isoformat(),
        }
        with patch.object(job_core, "JOBS_ADAPTIVE_PUBLISHING", True), \
             patch.object(job_core, "JOBS_MIN_PUBLISH_INTERVAL_MINUTES", 5), \
             patch.object(job_core, "load_job_state", return_value=state), \
             patch.object(job_core, "publishable_backlog_count", return_value=20), \
             patch.object(job_core, "current_policy", return_value={
                 "enabled": True,
                 "stage": 5,
                 "green_score": 20,
                 "daily_cap": 240,
             }):
            status = job_core.job_publish_window_status(now=now)
        self.assertFalse(status["allowed_now"])
        self.assertEqual(status["min_interval_minutes"], 5)
        self.assertIn("spacing", " ".join(status["reasons"]))


    def test_adaptive_blogger_interval_tracks_publishable_backlog(self):
        with patch.object(job_core, "JOBS_MIN_PUBLISH_INTERVAL_MINUTES", 5):
            self.assertEqual(job_core.adaptive_publish_interval_minutes(2), 10)
            self.assertEqual(job_core.adaptive_publish_interval_minutes(8), 7)
            self.assertEqual(job_core.adaptive_publish_interval_minutes(20), 5)


    def test_job_score_accepts_naive_scheduler_datetime(self):
        article = {
            "official_source": True,
            "job_published_at": "2026-09-30T08:00:00+00:00",
            "source_priority": "S",
            "job_number_of_positions": 10,
            "job_deadline": "2026-10-10",
            "job_location": "Casablanca",
            "job_diploma": "Bac+2",
            "job_application_url": "https://example.com/jobs/12345",
            "canonical_url": "https://example.com/jobs/12345",
            "job_eligibility": "morocco",
            "job_title": "Technicien informatique",
        }
        result = job_core.score_job(article, now=datetime(2026, 9, 30, 12, 0, 0))
        self.assertGreaterEqual(result["score"], 65)

    def test_job_permalink_seed_is_alphabetic_even_with_numeric_reference(self):
        article = {
            "desired_slug": "orange-business-consultant-cyber-securite-abcdwxyz",
            "ats_reference": "ICM-584854",
        }
        seed = draft._permalink_seed_title(article)
        self.assertNotRegex(seed, r"\\d")
        self.assertEqual(
            draft._job_permalink_stem(
                "https://example.blogspot.com/2026/09/orange-business-consultant-cyber-securite-abcdwxyz.html"
            ),
            "orange-business-consultant-cyber-securite-abcdwxyz",
        )
        self.assertFalse(
            any(ch.isdigit() for ch in draft._job_permalink_stem(
                "https://example.blogspot.com/2026/09/orange-business-consultant-cyber-securite-abcdwxyz.html"
            ))
        )

    def test_permalink_retry_suffix_stays_alphabetic(self):
        article = {
            "desired_slug": "orange-business-consultant-cyber-securite-abcdwxyz",
            "permalink_attempt": 2,
        }
        seed = draft._permalink_seed_title(article)
        self.assertTrue(seed.endswith(" b"), seed)
        self.assertNotRegex(seed, r"\\d")

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
        package = {
            "url": "https://employer.example/jobs/42",
            "job_notice_type": "vacancy",
            "job_application_url": "https://employer.example/jobs/42/apply",
        }
        data = {
            "title": "شركة أورنج تعلن عن توظيف خبير في الأمن السيبراني",
            "description": "فرصة توظيف لدى شركة أورنج في مجال الأمن السيبراني، تعرف على المعلومات الواردة في الإعلان الرسمي وطريقة تقديم طلب الترشيح.",
            "slug": "orange-cybersecurity",
            "html_content": (
                "<p>" + " ".join("معلومة" + str(i) for i in range(125)) + "</p>"
                "<p><a href='https://employer.example/jobs/42/apply'>التقديم الرسمي</a></p>"
            ),
        }
        article = {"ai_input_package": package}
        with patch.object(ai, "JOBS_MODE", True), patch.object(quality_gate, "JOBS_MODE", True), \
             patch.object(ai, "_phase3_quality_failure_reason", return_value=""):
            ai._validate_ai_output(data, package)
            ai._apply_success(article, data, "gemini:test")
        self.assertEqual(article["ai_status"], "completed")
        self.assertGreaterEqual(article["final_word_count"], 125)

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

    def test_identity_pending_resolver_uses_document_evidence_and_reopens_ready(self):
        row = {
            "id": "pending-job",
            "url": "https://example.com/jobs/pending",
            "status": "identity_pending",
            "content_fetch_status": "success",
            "job_document_links": [{"url": "https://example.com/notice.pdf", "label": "الإعلان"}],
        }
        queue = {"articles": [row]}

        def add_pdf_evidence(article):
            article["job_document_texts"] = [
                {"text": "Référence du concours : REF-2026-9001", "page_number": 1}
            ]
            return article["job_document_texts"]

        with (
            patch.object(article_processor, "load_article_queue", return_value=queue),
            patch.object(article_processor, "save_article_queue") as save,
            patch.object(
                article_processor,
                "extract_job_document_texts",
                side_effect=add_pdf_evidence,
            ) as extract,
            patch.object(
                article_processor,
                "classify_identity",
                return_value={
                    "action": "new_campaign",
                    "reason": "different external reference",
                    "existing": {},
                },
            ),
        ):
            stats = article_processor.resolve_identity_pending_articles()

        self.assertEqual(stats["resolved_ready"], 1)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["job_identity_action"], "new_campaign")
        extract.assert_called_once_with(row)
        save.assert_called_once()

    def test_identity_pending_resolver_does_not_redownload_unchanged_documents(self):
        row = {
            "id": "pending-job",
            "url": "https://example.com/jobs/pending",
            "status": "identity_pending",
            "content_fetch_status": "success",
            "job_document_links": [
                {"url": "https://example.com/notice.pdf", "label": "الإعلان"}
            ],
            "identity_pending_evidence_checked_at": "2026-09-30T10:00:00",
            "identity_evidence_stage_checked_at": "2026-09-30T10:00:00",
            "identity_evidence_document_fingerprint": "https://example.com/notice.pdf",
            "job_document_text_download_failures": 0,
            "source_tables": [],
            "source_tables_count": 0,
            "job_detail_url": "https://example.com/jobs/pending",
            "job_document_texts": [],
        }
        queue = {"articles": [row]}
        with (
            patch.object(article_processor, "load_article_queue", return_value=queue),
            patch.object(article_processor, "save_article_queue"),
            patch.object(article_processor, "extract_job_document_texts") as extract,
            patch.object(
                article_processor,
                "classify_identity",
                return_value={
                    "action": "hold",
                    "reason": "ambiguous same role without strong identifier",
                    "existing": {},
                },
            ),
        ):
            stats = article_processor.resolve_identity_pending_articles()

        self.assertEqual(stats["still_pending"], 1)
        extract.assert_not_called()

    def test_identity_pending_resolver_keeps_ambiguous_job_pending(self):
        row = {
            "id": "pending-job",
            "url": "https://example.com/jobs/pending",
            "status": "identity_pending",
            "content_fetch_status": "success",
            "job_document_links": [],
            "skip_reason": "old hold reason",
        }
        queue = {"articles": [row]}
        with (
            patch.object(article_processor, "load_article_queue", return_value=queue),
            patch.object(article_processor, "save_article_queue"),
            patch.object(
                article_processor,
                "classify_identity",
                return_value={
                    "action": "hold",
                    "reason": "ambiguous same role without strong identifier",
                    "existing": {},
                },
            ),
        ):
            stats = article_processor.resolve_identity_pending_articles()

        self.assertEqual(stats["still_pending"], 1)
        self.assertEqual(row["status"], "identity_pending")
        self.assertNotIn("skip_reason", row)
        self.assertFalse(row["job_identity_final"])

    def test_identity_pending_resolver_skips_only_confirmed_duplicate(self):
        row = {
            "id": "pending-job",
            "url": "https://example.com/jobs/pending",
            "status": "identity_pending",
            "content_fetch_status": "success",
            "job_document_links": [],
        }
        queue = {"articles": [row]}
        with (
            patch.object(article_processor, "load_article_queue", return_value=queue),
            patch.object(article_processor, "save_article_queue"),
            patch.object(
                article_processor,
                "classify_identity",
                return_value={
                    "action": "duplicate",
                    "reason": "same external reference",
                    "existing": {},
                },
            ),
        ):
            stats = article_processor.resolve_identity_pending_articles()

        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(row["status"], "skipped")
        self.assertTrue(row["job_identity_final"])
        self.assertIn("duplicate confirmed", row["skip_reason"])

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
