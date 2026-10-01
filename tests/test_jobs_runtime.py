"""Regressions for unattended Jobs delivery; no external requests or publishing."""
import json
import copy
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
from googleapiclient.errors import HttpError

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
import runtime_state
import verified_fact_manifest as fact_manifest
import jobs_adaptive_controller as adaptive


class JobsRuntimeTests(unittest.TestCase):
    def test_jobs_article_hides_publication_date_and_internal_reference(self):
        html = """
        <p>مقدمة مفيدة عن الوظيفة.</p>
        <table><tbody>
          <tr><th>المرجع</th><td>C43918/26</td></tr>
          <tr><th>تاريخ النشر</th><td>30 شتنبر 2026</td></tr>
          <tr><th>عدد المناصب</th><td>1 منصب</td></tr>
        </tbody></table>
        <p>رمز المباراة: C43918/26</p>
        <p>تم نشر الإعلان بتاريخ 30 شتنبر 2026</p>
        <p>رقم المباراة: 2026/42</p>
        """
        cleaned = ai.format_phase3_article_html(html, {})
        self.assertNotIn("C43918/26", cleaned)
        self.assertNotIn("تاريخ النشر", cleaned)
        self.assertNotIn("تم نشر الإعلان", cleaned)
        self.assertNotIn("رقم المباراة", cleaned)
        self.assertNotIn("2026/42", cleaned)
        self.assertIn("عدد المناصب", cleaned)
        self.assertIn("1 منصب", cleaned)

    def test_jobs_ai_package_keeps_freshness_and_reference_internal(self):
        article = {
            "title": "وظيفة اختبار",
            "url": "https://example.com/jobs/one",
            "source_name": "Official",
            "full_article_text": "نص رسمي كاف للوظيفة",
            "job_title": "إطار إداري",
            "job_company": "شركة مثال",
            "job_published_at": "2026-09-30",
            "job_published_at_display": "30 شتنبر 2026",
            "source_published_at": "2026-09-30",
            "published_at_source": "official_detail_page",
            "article_age_hours": 5,
            "job_external_reference": "REF-123",
            "ats_reference": "REF-123",
        }
        package = article_processor._build_ai_input_package(article)
        for key in (
            "job_published_at", "job_published_at_display",
            "source_published_at", "published_at_source", "article_age_hours",
            "job_external_reference", "ats_reference",
        ):
            self.assertNotIn(key, package)

    def test_publication_value_does_not_erase_same_day_deadline(self):
        article = {
            "final_html": (
                "<p>آخر أجل للترشيح هو 2026-10-10.</p>"
                "<p>تاريخ النشر: 2026-10-10</p>"
            ),
            "job_published_at": "2026-10-10",
            "job_published_at_display": "10 أكتوبر 2026",
            "source_published_at": "2026-10-10",
            "ai_input_package": {},
        }
        draft._sanitize_article_final_html(article, prepare_visuals=False)
        self.assertIn("آخر أجل للترشيح هو 2026-10-10", article["final_html"])
        self.assertNotIn("تاريخ النشر", article["final_html"])

    def test_jobs_quality_gate_blocks_unlabeled_raw_reference_value(self):
        reference = "C43918/26"
        article = {
            "url": "https://official.example/jobs/42",
            "seo_title": "تحديث رسمي حول مباراة توظيف التقنيين بالمغرب",
            "seo_description": (
                "تفاصيل رسمية موجزة حول مباراة توظيف التقنيين وشروطها الأساسية "
                f"وفق الملف {reference} مع رابط الوثيقة الرسمية."
            ),
            "final_html": "<p>تفاصيل موثقة ومباشرة حول المباراة وشروط الترشيح الأساسية.</p>",
            "job_notice_type": "update",
            "job_external_reference": reference,
        }
        result = quality_gate.validate_before_publish(article, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("internal job reference value", result.reason)

    def test_pdf_outages_keep_retrying_with_bounded_backoff(self):
        article = {"job_document_render_retry_count": 20, "ai_input_package": {}}
        draft._mark_document_render_retry(article, article["ai_input_package"], "offline")
        self.assertEqual(article["job_document_render_status"], "document_render_retry")
        delay = datetime.fromisoformat(article["job_document_render_retry_after"]) - datetime.now(timezone.utc)
        self.assertGreater(delay, timedelta(hours=5, minutes=59))
        self.assertLessEqual(delay, timedelta(hours=6))
        article["job_document_page_images"] = [{"url": "https://assets.example/page.jpg"}]
        article["visual_sync_retry_count"] = 20
        draft._mark_visual_sync_retry(article, "Blogger outage")
        self.assertTrue(article["visual_sync_retry_pending"])
        self.assertEqual(article["visual_sync_status"], "visual_sync_retry")

    def test_pdf_html_stays_in_one_ordered_section_across_batches(self):
        from bs4 import BeautifulSoup

        rows = [
            {"document_url": "https://official.example/notice.pdf", "page_number": number,
             "url": f"https://assets.example/page-{number}.jpg"}
            for number in range(1, 4)
        ]
        with patch.object(ai, "JOBS_MODE", True):
            html = ai._append_job_document_page_images("<p>مقدمة</p>", {"job_document_page_images": rows[:1]})
            html = ai.format_phase3_article_html(html, {"job_document_page_images": rows})
            html = ai.format_phase3_article_html(html, {"job_document_page_images": rows})
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(len(soup.select("section.jobOfficialDocuments")), 1)
        self.assertEqual([image["src"] for image in soup.select("img.jobDocPageImage")], [row["url"] for row in rows])
        self.assertEqual([h.get_text() for h in soup.find_all("h2")].count("صفحات الوثيقة الرسمية"), 1)

    def test_scanned_pdf_ocr_covers_every_processed_page_by_default(self):
        pdf = job_document_renderer.fitz.open()
        for _ in range(10):
            pdf.new_page(width=100, height=100)
        payload = pdf.tobytes()
        pdf.close()

        article = {
            "id": "ocr-all-pages",
            "source_country": "Morocco",
            "job_document_links": [
                {"url": "https://official.example/scanned.pdf", "label": "شروط المباراة"}
            ],
        }

        def fake_ocr(page, **_kwargs):
            return f"شروط ومعلومات موثقة من الصفحة {page.number + 1}", ""

        with patch.object(job_document_renderer, "_download_pdf", return_value=payload), \
             patch.object(job_document_renderer, "_ocr_pdf_page_text", side_effect=fake_ocr) as ocr:
            rows = job_document_renderer.extract_job_document_texts(
                article,
                max_documents=1,
                max_total_pages=10,
            )

        self.assertEqual(len(rows), 10)
        self.assertEqual(article["job_document_ocr_pages"], 10)
        self.assertEqual(article["job_document_ocr_attempts"], 10)
        self.assertEqual(article["job_document_unread_pages"], 0)
        self.assertTrue(article["job_document_text_read_complete"])
        self.assertEqual(ocr.call_count, 10)
        self.assertIn("الصفحة 10", rows[-1]["text"])

    def test_jobs_quality_gate_requires_pdf_pages_even_without_cover(self):
        article = {
            "url": "https://official.example/jobs/42",
            "seo_title": "تحديث رسمي حول مباراة توظيف التقنيين بالمغرب",
            "seo_description": (
                "تفاصيل رسمية موجزة حول مباراة توظيف التقنيين وشروطها الأساسية "
                "مع توجيه المترشحين إلى الوثيقة الرسمية."
            ),
            "final_html": "<p>تفاصيل موثقة ومباشرة حول المباراة وشروط الترشيح الأساسية.</p>",
            "job_notice_type": "update",
            "job_document_page_images": [
                {"url": "https://assets.example/doc-page-1.jpg"}
            ],
            "ai_input_package": {
                "job_notice_type": "update",
                "verified_fact_manifest": {"facts": []},
                "job_document_page_images": [
                    {"url": "https://assets.example/doc-page-1.jpg"}
                ],
            },
        }
        with patch.object(
            quality_gate,
            "validate_output_against_manifest",
            return_value=([], []),
        ):
            result = quality_gate.validate_before_publish(article, check_duplicate=False)

        self.assertFalse(result.passed)
        self.assertIn("rendered official PDF pages", result.reason)

    def test_jobs_quality_gate_keeps_pdf_pages_required_when_cover_exists(self):
        cover = "https://assets.example/cover.jpg"
        pdf_page = "https://assets.example/doc-page-1.jpg"
        article = {
            "url": "https://official.example/jobs/42",
            "seo_title": "تحديث رسمي حول مباراة توظيف التقنيين بالمغرب",
            "seo_description": (
                "تفاصيل رسمية موجزة حول مباراة توظيف التقنيين وشروطها الأساسية "
                "مع توجيه المترشحين إلى الوثيقة الرسمية."
            ),
            "final_html": f"<p>تفاصيل موثقة حول المباراة.</p><img src='{cover}'/>",
            "job_notice_type": "update",
            "job_article_cover_url": cover,
            "job_document_page_images": [{"url": pdf_page}],
            "ai_input_package": {
                "job_notice_type": "update",
                "verified_fact_manifest": {"facts": []},
                "job_article_cover_url": cover,
                "job_document_page_images": [{"url": pdf_page}],
            },
        }
        with patch.object(
            quality_gate,
            "validate_output_against_manifest",
            return_value=([], []),
        ):
            result = quality_gate.validate_before_publish(article, check_duplicate=False)

        self.assertFalse(result.passed)
        self.assertEqual(
            result.reason,
            "rendered official PDF pages are missing, reordered, duplicated, or unverified",
        )
        self.assertTrue(draft._jobs_quality_error_is_ai_repairable(result.reason))

    def test_jobs_quality_gate_blocks_internal_reference_in_reader_metadata(self):
        article = {
            "url": "https://official.example/jobs/42",
            "seo_title": "تحديث مباراة توظيف التقنيين - المرجع C43918/26",
            "seo_description": (
                "تفاصيل رسمية موجزة حول مباراة توظيف التقنيين وشروطها الأساسية "
                "مع توجيه المترشحين إلى الوثيقة الرسمية."
            ),
            "final_html": "<p>تفاصيل موثقة ومباشرة حول المباراة وشروط الترشيح الأساسية.</p>",
            "job_notice_type": "update",
        }
        result = quality_gate.validate_before_publish(article, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("internal job publication/reference metadata", result.reason)

    def test_queue_maintenance_accepts_mixed_timezone_timestamps(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"id": "aware", "status": "failed", "updated_at": (now - timedelta(days=10)).isoformat()},
            {"id": "legacy", "status": "skipped", "updated_at": (now - timedelta(days=10)).replace(tzinfo=None).isoformat()},
            {"id": "recent", "status": "failed", "updated_at": now.astimezone(ZoneInfo("Africa/Casablanca")).isoformat()},
        ]
        with patch.object(article_queue, "load_article_queue", return_value={"articles": rows}), \
             patch.object(article_queue, "save_article_queue") as save, \
             patch.object(article_queue, "_compact_job_queue_archive", return_value=0):
            stats = article_queue.maintain_article_queue(days=7)
        self.assertEqual(stats["archived_old_failed"], 1)
        self.assertEqual(stats["archived_old_skipped"], 1)
        self.assertFalse(rows[2].get("archived"))
        save.assert_called_once()

    def test_pdf_batches_resume_every_page_and_every_document(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        pdf = job_document_renderer.fitz.open()
        for _ in range(3):
            pdf.new_page(width=100, height=100)
        payload = pdf.tobytes()
        pdf.close()
        article = {
            "id": "batched-pdf", "seo_slug": "batched-pdf",
            "job_document_links": [{"url": f"https://official.example/doc-{n}.pdf"} for n in range(7)],
        }
        with TemporaryDirectory() as temporary, \
             patch.object(job_document_renderer, "_download_pdf", return_value=payload) as download:
            for batch in range(14):
                pages = job_document_renderer.render_job_document_pages(
                    article, output_root=Path(temporary), max_documents=1, max_total_pages=2,
                )
                self.assertLessEqual(article["job_document_render_new_pages"], 2)
                self.assertLessEqual(article["job_document_render_attempted_documents"], 1)
                if not article["job_document_pages_truncated"]:
                    break
            self.assertEqual(len(pages), 21)
            self.assertEqual(len({(row["document_url"], row["page_number"]) for row in pages}), 21)
            self.assertTrue(all(Path(row["path"]).is_file() for row in pages))
            self.assertFalse(article["job_document_pages_truncated"])
            download.reset_mock()
            self.assertEqual(job_document_renderer.render_job_document_pages(article, output_root=Path(temporary)), pages)
            download.assert_not_called()

    def test_partial_pdf_download_failure_keeps_already_rendered_pages(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        pdf = job_document_renderer.fitz.open()
        pdf.new_page(width=100, height=100)
        pdf.new_page(width=100, height=100)
        payload = pdf.tobytes()
        pdf.close()
        article = {"job_document_links": [{"url": "https://official.example/conditions.pdf"}]}
        with TemporaryDirectory() as temporary:
            with patch.object(job_document_renderer, "_download_pdf", return_value=payload):
                before = job_document_renderer.render_job_document_pages(article, output_root=Path(temporary), max_total_pages=1)
            with patch.object(job_document_renderer, "_download_pdf", side_effect=OSError("offline")):
                after = job_document_renderer.render_job_document_pages(article, output_root=Path(temporary))
        self.assertEqual(after, before)
        self.assertEqual(article["job_document_render_failures"], 1)

    def test_pdf_batch_progress_does_not_exhaust_failure_retry_limit(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        pdf = job_document_renderer.fitz.open()
        for _ in range(49):
            pdf.new_page(width=50, height=50)
        payload = pdf.tobytes()
        pdf.close()
        article = {
            "job_document_links": [{"url": "https://official.example/conditions.pdf"}],
            "job_document_render_retry_count": draft.MAX_JOB_DOCUMENT_RENDER_RETRIES,
        }
        renderer = job_document_renderer.render_job_document_pages
        with TemporaryDirectory() as temporary, \
             patch.object(draft, "JOBS_MODE", True), \
             patch.object(job_document_renderer, "_download_pdf", return_value=payload), \
             patch.object(draft, "_persist_generated_job_assets"), \
             patch.object(draft, "render_job_document_pages", side_effect=lambda row, **kw: renderer(row, output_root=Path(temporary), **kw)):
            first = draft._prepare_job_document_page_images(article)
            self.assertEqual(len(first), 48)
            self.assertEqual(article["job_document_render_status"], "document_render_retry")
            self.assertEqual(article["job_document_render_retry_reason"], "batch_remaining")
            second = draft._prepare_job_document_page_images(article, force_retry=True)
            self.assertEqual(len(second), 49)
            self.assertEqual(article["job_document_render_status"], "rendered")
            self.assertFalse(article["job_document_pages_truncated"])

    def test_jobs_queue_storage_health_distinguishes_corrupt_from_valid_empty(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path):
                path.write_text("", encoding="utf-8")
                corrupt = article_queue.article_queue_storage_status()
                self.assertFalse(corrupt["valid"])
                self.assertEqual(corrupt["article_count"], 0)

                path.write_text(
                    json.dumps({"updated_at": "", "articles": [], "notifications": {}}),
                    encoding="utf-8",
                )
                valid_empty = article_queue.article_queue_storage_status()
                self.assertTrue(valid_empty["valid"])
                self.assertEqual(valid_empty["article_count"], 0)

    def test_jobs_queue_recovery_marker_is_distinct_from_intentional_empty(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            payload = {
                "updated_at": "",
                "articles": [],
                "notifications": {
                    "queue_recovery_required": True,
                    "queue_recovery_reason": "zero_byte_runtime_state_repair",
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path):
                status = article_queue.article_queue_storage_status()

        self.assertTrue(status["valid"])
        self.assertTrue(status["recovery_required"])
        self.assertEqual(status["reason"], "zero_byte_runtime_state_repair")

    def test_jobs_enrichment_batch_limits_work_without_dropping_backlog(self):
        rows = []
        for index in range(8):
            rows.append({
                "id": f"job-{index}",
                "url": f"https://example.com/jobs/{index}",
                "status": "ready",
                "category_label": "jobs-morocco",
                "job_score": 60 + index,
                "source_priority": "A+" if index % 2 else "B",
            })
        queue = {"articles": rows}

        with (
            patch.object(article_enricher, "JOBS_MODE", True),
            patch.object(article_enricher, "JOBS_ENRICH_MAX_TARGETS_PER_CYCLE", 3),
            patch.object(article_enricher, "load_article_queue", return_value=queue),
            patch.object(article_enricher, "save_article_queue") as save,
            patch.object(article_enricher, "_can_run_async_fetch", return_value=False),
            patch.object(article_enricher, "enrich_article", return_value=(True, "")) as enrich,
        ):
            stats = article_enricher.enrich_ready_articles(force=False)

        self.assertEqual(stats["enriched"], 3)
        self.assertEqual(stats["deferred_targets"], 5)
        self.assertEqual(stats["batch_limit"], 3)
        self.assertEqual(enrich.call_count, 3)
        enriched_ids = [call.args[0]["id"] for call in enrich.call_args_list]
        self.assertEqual(enriched_ids, ["job-7", "job-6", "job-5"])
        self.assertEqual(len(queue["articles"]), 8)
        save.assert_called_once()

    def test_jobs_discovery_state_reset_forgets_seen_ids_but_not_other_source_fields(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            path = Path(temp) / "crawl_state.json"
            state = {
                "sources": {
                    "https://example.com/jobs": {
                        "source_name": "Example",
                        "last_crawled_at": "2026-09-30T22:00:00Z",
                        "job_seen_ids": ["source:1", "source:2"],
                        "job_seen_ids_count": 2,
                        "job_discovery_resume": {"kind": "html", "url": "https://example.com/jobs?page=2"},
                        "discovery_pages_scanned": 1,
                        "discovery_stop_reason": "max_pages",
                    }
                },
                "category_rotation": {},
                "source_rotation": {},
                "updated_at": "",
            }
            path.write_text(json.dumps(state), encoding="utf-8")

            with patch.object(runtime_state, "CRAWL_STATE_PATH", path):
                result = runtime_state.reset_job_discovery_state(
                    "recovery_reset_after_queue_storage_loss"
                )
                saved = runtime_state.load_crawl_state()

        record = saved["sources"]["https://example.com/jobs"]
        self.assertEqual(result["changed_sources"], 1)
        self.assertEqual(result["forgotten_ids"], 2)
        self.assertEqual(record["job_seen_ids"], [])
        self.assertEqual(record["job_seen_ids_count"], 0)
        self.assertEqual(record["job_discovery_resume"], {})
        self.assertEqual(record["discovery_pages_scanned"], 0)
        self.assertEqual(
            record["discovery_stop_reason"],
            "recovery_reset_after_queue_storage_loss",
        )
        self.assertEqual(record["source_name"], "Example")
        self.assertEqual(record["last_crawled_at"], "2026-09-30T22:00:00Z")

    def test_jobs_fetch_self_heals_discovery_after_corrupt_queue_storage(self):
        sources = [{
            "name": "Source A",
            "base_url": "https://example.com/a",
            "enabled": True,
        }]
        discovery = {
            "checked_sources": 1,
            "articles": [],
            "source_results": [],
            "reason": "",
        }
        queue_stats = {
            "added": 0,
            "duplicates": 0,
            "duplicate_url": 0,
            "duplicate_title": 0,
            "total_queued": 0,
            "added_by_category": {},
        }
        with (
            patch.object(main, "JOBS_MODE", True),
            patch.object(main, "load_sources", return_value=sources),
            patch.object(
                main,
                "article_queue_storage_status",
                return_value={
                    "exists": True,
                    "valid": False,
                    "article_count": 0,
                    "reason": "JSONDecodeError",
                },
            ),
            patch.object(
                main,
                "reset_job_discovery_state",
                return_value={
                    "changed_sources": 1,
                    "forgotten_ids": 8,
                    "reason": "recovery_reset_after_queue_storage_loss",
                },
            ) as reset,
            patch.object(main, "discover_latest_article_links", return_value=discovery),
            patch.object(main, "add_articles_to_queue", return_value=queue_stats),
            redirect_stdout(StringIO()),
        ):
            main.run_fetch_only()

        reset.assert_called_once_with(
            reason="recovery_reset_after_queue_storage_loss"
        )

    def test_jobs_fetch_uses_exhaustive_discovery_not_news_shortcuts(self):
        sources = [
            {
                "name": "Source A",
                "base_url": "https://example.com/a",
                "enabled": True,
            },
            {
                "name": "Source B",
                "base_url": "https://example.com/b",
                "enabled": True,
            },
        ]
        discovery = {
            "checked_sources": 2,
            "articles": [],
            "source_results": [],
            "reason": "",
        }
        queue_stats = {
            "added": 0,
            "duplicates": 0,
            "duplicate_url": 0,
            "duplicate_title": 0,
            "total_queued": 0,
            "added_by_category": {},
        }

        with (
            patch.object(main, "JOBS_MODE", True),
            patch.object(main, "load_sources", return_value=sources),
            patch.object(
                main,
                "article_queue_storage_status",
                return_value={"exists": True, "valid": True, "article_count": 0, "reason": ""},
            ),
            patch.object(
                main,
                "discover_latest_article_links",
                return_value=discovery,
            ) as latest,
            patch.object(main, "add_articles_to_queue", return_value=queue_stats),
            redirect_stdout(StringIO()),
        ):
            result = main.run_fetch_only()

        latest.assert_called_once_with(sources)
        self.assertEqual(result["sources_checked"], 2)

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

    def test_jobs_enrichment_batch_defers_backlog_without_dropping_jobs(self):
        articles = [
            {
                "id": f"job-{index}",
                "url": f"https://example.com/jobs/{index}",
                "status": "ready",
                "category_label": "jobs-morocco",
                "content_fetch_status": "",
                "score": score,
                "source_priority": priority,
            }
            for index, score, priority in (
                (1, 10, "A+"),
                (2, 9, "A"),
                (3, 4, "S"),
                (4, 3, "A"),
                (5, 1, "B"),
            )
        ]
        queue = {"articles": articles}

        def enrich(article):
            article["content_fetch_status"] = "success"
            article["full_article_text"] = " ".join(["verified"] * 60)
            return True, ""

        with (
            patch.object(article_enricher, "JOBS_MODE", True),
            patch.object(article_enricher, "JOBS_ENRICH_MAX_TARGETS_PER_CYCLE", 2),
            patch.object(article_enricher, "load_article_queue", return_value=queue),
            patch.object(article_enricher, "save_article_queue") as save,
            patch.object(article_enricher, "_can_run_async_fetch", return_value=False),
            patch.object(article_enricher, "enrich_article", side_effect=enrich) as enrich_call,
        ):
            result = article_enricher.enrich_ready_articles(force=False)

        enriched_ids = [call.args[0]["id"] for call in enrich_call.call_args_list]
        self.assertEqual(enriched_ids, ["job-1", "job-2"])
        self.assertEqual(result["deferred_targets"], 3)
        self.assertEqual(result["batch_limit"], 2)
        self.assertEqual(result["enriched"], 2)
        self.assertTrue(all(article["status"] == "ready" for article in articles[2:]))
        self.assertTrue(all(not article.get("archived") for article in articles[2:]))
        save.assert_called_once()

    def test_jobs_enrichment_priority_prefers_newer_job_before_higher_score_old_job(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        older_high_score = {
            "status": "ready",
            "score": 100,
            "source_priority": "S+",
            "source_published_at": "2026-09-23T08:00:00+00:00",
        }
        newer_lower_score = {
            "status": "ready",
            "score": 10,
            "source_priority": "B",
            "source_published_at": "2026-09-30T10:00:00+00:00",
        }
        self.assertLess(
            article_enricher._jobs_enrichment_priority(newer_lower_score, 1, now=now),
            article_enricher._jobs_enrichment_priority(older_high_score, 0, now=now),
        )

    def test_jobs_enrichment_priority_uses_explicit_emploi_public_listing_deadline_hint(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        expired_emploi = {
            "status": "ready",
            "score": 100,
            "source_priority": "S+",
            "ats_provider": "emploi_public",
            "title": (
                "مباراة لتوظيف مهندس دولة آخر أجل لإيداع ملفات الترشيح : "
                "27 شتنبر 2026"
            ),
            "discovered_at": "2026-09-30T11:00:00+00:00",
        }
        open_job = {
            "status": "ready",
            "score": 10,
            "source_priority": "A",
            "ats_provider": "workday",
            "job_deadline": "2026-10-15",
            "source_published_at": "2026-09-30T10:00:00+00:00",
        }
        self.assertLess(
            article_enricher._jobs_enrichment_priority(open_job, 1, now=now),
            article_enricher._jobs_enrichment_priority(expired_emploi, 0, now=now),
        )
        self.assertNotIn("job_deadline", expired_emploi)

    def test_jobs_enrichment_priority_prefers_technical_role_before_general_job(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        technical = {
            "status": "ready",
            "job_title": "Développeur Full Stack",
            "source_published_at": "2026-09-30T08:00:00+00:00",
            "score": 10,
            "source_priority": "B",
        }
        general = {
            "status": "ready",
            "job_title": "Chargé de clientèle",
            "source_published_at": "2026-09-30T11:00:00+00:00",
            "score": 100,
            "source_priority": "S+",
        }
        self.assertLess(
            article_enricher._jobs_enrichment_priority(technical, 1, now=now),
            article_enricher._jobs_enrichment_priority(general, 0, now=now),
        )

    def test_jobs_enrichment_skips_known_stale_job_before_fetch(self):
        article = {
            "id": "stale-known",
            "url": "https://example.com/jobs/stale-known",
            "status": "ready",
            "category_label": "jobs-morocco",
            "source_published_at": "2026-09-28T08:00:00+00:00",
            "job_title": "Développeur Backend",
        }
        queue = {"articles": [article]}
        with (
            patch.object(article_enricher, "JOBS_MODE", True),
            patch.object(article_enricher, "JOBS_MAX_PUBLISH_AGE_HOURS", 12),
            patch.object(article_enricher, "load_article_queue", return_value=queue),
            patch.object(article_enricher, "save_article_queue") as save,
            patch.object(article_enricher, "enrich_article") as enrich,
        ):
            result = article_enricher.enrich_ready_articles(force=False)

        self.assertEqual(article["status"], "skipped")
        self.assertIn("outside fresh window", article["skip_reason"])
        enrich.assert_not_called()
        save.assert_called_once()
        self.assertEqual(result["enriched"], 0)

    def test_jobs_enrichment_priority_advances_near_deadline_before_score(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        urgent = {
            "status": "ready",
            "score": 1,
            "source_priority": "A",
            "job_deadline": "2026-10-01T08:00:00+00:00",
        }
        later = {
            "status": "ready",
            "score": 10,
            "source_priority": "S+",
            "job_deadline": "2026-10-20T08:00:00+00:00",
        }
        self.assertLess(
            article_enricher._jobs_enrichment_priority(urgent, 1, now=now),
            article_enricher._jobs_enrichment_priority(later, 0, now=now),
        )

    def test_candidate_failure_backoff_grows_and_caps(self):
        first = article_enricher._candidate_failure_backoff_minutes(1)
        later = article_enricher._candidate_failure_backoff_minutes(4)
        capped = article_enricher._candidate_failure_backoff_minutes(99)
        self.assertGreaterEqual(first, 1)
        self.assertGreater(later, first)
        self.assertLessEqual(capped, 24 * 60)

    def test_published_job_is_not_archived_while_facebook_is_pending(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            queue_path = Path(temp) / "jobs_article_queue.json"
            article = {
                "id": "published-pending-facebook",
                "url": "https://example.com/jobs/42",
                "status": "published",
                "publish_status": "published",
                "blogger_post_url": "https://example.blogspot.com/2026/09/job.html",
                "facebook_status": "facebook_pending",
            }
            with (
                patch.object(article_queue, "JOBS_MODE", True),
                patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path),
            ):
                article_queue.save_article_queue({"articles": [article]})
                archived = article_queue.archive_published_queue_article(
                    article_id=article["id"],
                    article_url=article["url"],
                )
                saved = article_queue.load_article_queue()["articles"][0]

        self.assertFalse(archived)
        self.assertFalse(saved.get("archived", False))
        self.assertEqual(saved["archive_deferred_reason"], "facebook_retry_pending")

    def test_facebook_queue_recovers_blogger_publish_from_campaign_memory(self):
        now = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
        queue = {"articles": []}
        campaign = {
            "campaign_id": "campaign-orange",
            "company": "Orange Business",
            "title": "Analyste Cybersécurité Junior",
            "location": "Casablanca",
            "contract_type": "CDI",
            "application_url": "https://careersfr-orange.icims.com/jobs/28406/analyste-cybersecurite-junior/job/login",
            "application_link_kind": "direct_apply",
            "application_is_specific": True,
            "notice_type": "vacancy",
            "company_logo_url": "https://cdn.example.com/orange.png",
            "company_logo_verified": True,
            "company_logo_confidence": 98,
            "company_logo_source": "verified_registry",
            "company_official_domain": "orange.jobs",
            "company_logo_checksum": "orange-checksum",
            "source_url": "https://careers.example.com/jobs/28406",
            "source_name": "Orange Maroc",
            "source_priority": "A+",
            "blogger_post_id": "8146187171530968237",
            "blogger_url": "https://example.blogspot.com/2026/09/orange.html",
            "updated_at": "2026-09-30T22:29:42+00:00",
            "status": "active",
        }
        cache = {
            "links": [{
                "title": "توظيف محلل أمن سيبراني مبتدئ لدى Orange Business بالدار البيضاء",
                "url": campaign["blogger_url"],
                "category": "jobs-morocco",
                "published_at": "2026-09-30T22:29:42Z",
            }]
        }

        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "list_active_job_campaign_records", return_value=[campaign]),
            patch.object(facebook, "load_internal_link_cache", return_value=(cache, {})),
            patch.object(facebook, "classify_urgency", return_value={"level": "normal"}),
            patch.object(facebook, "_persist_jobs_social_state") as persist_social,
            patch.object(facebook, "save_article_queue") as save,
        ):
            stats = facebook._sync_jobs_facebook_queue(queue, now=now)

        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(len(queue["articles"]), 1)
        recovered = queue["articles"][0]
        self.assertEqual(recovered["facebook_status"], "facebook_pending")
        self.assertEqual(recovered["blogger_post_id"], campaign["blogger_post_id"])
        self.assertEqual(recovered["blogger_post_url"], campaign["blogger_url"])
        self.assertEqual(recovered["seo_title"], cache["links"][0]["title"])
        self.assertEqual(recovered["job_application_link_kind"], "direct_apply")
        self.assertTrue(recovered["job_application_is_specific"])
        self.assertTrue(recovered["company_logo_verified"])
        self.assertEqual(recovered["company_logo_url"], campaign["company_logo_url"])
        self.assertEqual(recovered["company_logo_confidence"], 98)
        self.assertEqual(recovered["company_official_domain"], "orange.jobs")
        self.assertTrue(recovered["facebook_queue_recovered"])
        persist_social.assert_called_once_with(recovered)
        save.assert_called_once()

    def test_legacy_campaign_recovery_only_infers_unmistakable_direct_apply(self):
        self.assertEqual(
            facebook._recovered_application_link_kind({
                "application_url": (
                    "https://careersfr-orange.icims.com/jobs/28406/"
                    "analyste-cybersecurite-junior/job/login"
                ),
            }),
            "direct_apply",
        )
        self.assertEqual(
            facebook._recovered_application_link_kind({
                "application_url": "https://example.com/jobs/28406",
            }),
            "official_job_page",
        )

    def test_record_job_publish_preserves_application_semantics(self):
        now = datetime(2026, 10, 1, 0, 20, tzinfo=timezone.utc)
        article = {
            "id": "orange-cyber",
            "job_identity_action": "new",
            "job_campaign_id": "campaign-orange-cyber",
            "job_company": "Orange Business",
            "job_title": "Analyste Cybersécurité Junior",
            "job_location": "Casablanca",
            "job_published_at": "2026-09-30T23:00:00+00:00",
            "job_application_url": (
                "https://careersfr-orange.icims.com/jobs/28406/"
                "analyste-cybersecurite-junior/job/login"
            ),
            "job_application_link_kind": "direct_apply",
            "job_application_is_specific": True,
            "job_notice_type": "vacancy",
            "company_logo_url": "https://cdn.example.com/orange.png",
            "company_logo_verified": True,
            "company_logo_confidence": 98,
            "company_logo_source": "verified_registry",
            "company_official_domain": "orange.jobs",
            "company_logo_checksum": "orange-checksum",
            "url": "https://careersfr-orange.icims.com/jobs/28406/job/login",
            "blogger_post_id": "post-1",
            "blogger_post_url": "https://example.blogspot.com/2026/10/orange.html",
        }
        with (
            patch.object(job_core, "get_by_identity", return_value={}),
            patch.object(job_core, "_save_json"),
            patch.object(job_core, "_load_json", return_value={}),
            patch.object(job_core, "load_job_state", return_value={}),
            patch.object(job_core, "save_job_state"),
        ):
            record = job_core.record_job_publish(article, now=now)

        self.assertEqual(record["application_link_kind"], "direct_apply")
        self.assertTrue(record["application_is_specific"])
        self.assertEqual(
            record["application_url"],
            "https://careersfr-orange.icims.com/jobs/28406/analyste-cybersecurite-junior/job/login",
        )
        self.assertTrue(record["company_logo_verified"])
        self.assertEqual(record["company_logo_url"], "https://cdn.example.com/orange.png")
        self.assertEqual(record["company_logo_confidence"], 98)
        self.assertEqual(record["company_logo_source"], "verified_registry")
        self.assertEqual(record["company_official_domain"], "orange.jobs")
        self.assertEqual(record["company_logo_checksum"], "orange-checksum")

    def test_legacy_not_selected_job_is_requeued_for_facebook(self):
        article = {
            "id": "low",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/2026/09/job.html",
            "facebook_status": "not_selected",
            "facebook_selection_reason": "old score policy",
            "job_score": 65,
            "job_number_of_positions": 1,
            "job_notice_type": "vacancy",
        }
        queue = {"articles": [article]}
        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "classify_urgency", return_value={"level": "normal"}),
            patch.object(facebook, "save_article_queue") as save,
        ):
            stats = facebook._sync_jobs_facebook_queue(
                queue,
                now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(article["facebook_status"], "facebook_pending")
        self.assertEqual(article["facebook_queue_reason"], "legacy_requeued")
        self.assertNotIn("facebook_selection_reason", article)
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
                            "blogger_post_id": "post-repair-1",
                            "blogger_post_url": "https://example.blogspot.com/job.html",
                            "job_campaign_id": "campaign-repair-1",
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
                    self.assertEqual(repaired["blogger_post_id"], "post-repair-1")
                    self.assertEqual(repaired["job_campaign_id"], "campaign-repair-1")
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

    def test_legacy_logo_wait_is_released_not_archived(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        queue = {
            "articles": [{
                "id": "logo-old",
                "status": "selected",
                "publish_status": "waiting_for_logo",
                "logo_first_wait_at": old,
                "candidate_failure_stage": "company-logo",
                "candidate_retry_after": old,
                "discovered_at": old,
                "ai_status": "completed",
                "final_html": "<p>مقال جاهز للنشر.</p>",
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                saved = article_queue.load_article_queue()

        article = saved["articles"][0]
        self.assertEqual(stats["released_logo_waits"], 1)
        self.assertFalse(article.get("archived", False))
        self.assertEqual(article["publish_status"], "visual_optional_ready")
        self.assertEqual(article["status"], "selected")
        self.assertTrue(article["visual_content_reuse_required"])
        self.assertEqual(
            article["job_article_cover_status"],
            "optional_missing_verified_logo",
        )
        self.assertNotIn("candidate_retry_after", article)

    def test_saved_blogger_lookup_failure_preserves_authoritative_post_id(self):
        article = {
            "id": "repair-job",
            "status": "selected",
            "publish_status": "repair_pending",
            "blogger_post_id": "post-404",
            "blogger_post_url": "https://example.blogspot.com/job.html",
        }
        service = MagicMock()
        service.posts.return_value.get.return_value = MagicMock()
        response = MagicMock()
        response.status = 404
        error = HttpError(response, b'{"error":{"message":"not found"}}')

        with patch.object(draft, "_execute_blogger_request", side_effect=error):
            post = draft._get_saved_post_by_id(service, article, mode="live")

        self.assertEqual(post["id"], "post-404")
        self.assertEqual(post["status"], "LIVE")
        self.assertTrue(post["_lookup_failed"])

    def test_saved_blogger_id_blocks_insert_fallback(self):
        with self.assertRaises(RuntimeError):
            draft._assert_insert_allowed({
                "blogger_post_id": "post-existing",
                "job_campaign_id": "campaign-existing",
            })

    def test_live_repair_updates_same_blogger_post_and_never_inserts(self):
        article = {
            "id": "repair-live",
            "url": "https://example.com/jobs/repair-live",
            "status": "selected",
            "publish_status": "repair_pending",
            "processing_status": "ready_for_ai",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "ai_provider_used": "gemini",
            "final_html": "<p>محتوى الوظيفة المصحح.</p>",
            "seo_title": "إعلان وظيفي مصحح",
            "blogger_post_id": "post-123",
            "blogger_post_url": "https://example.blogspot.com/2026/09/job.html",
            "job_campaign_id": "campaign-123",
            "job_identity_action": "update",
        }
        queue = {"articles": [article]}
        service = MagicMock()
        posts = MagicMock()
        service.posts.return_value = posts
        posts.update.return_value = MagicMock()

        updated = {
            "id": "post-123",
            "status": "LIVE",
            "url": "https://example.blogspot.com/2026/09/job.html",
        }

        with (
            patch.object(draft, "load_article_queue", return_value=queue),
            patch.object(draft, "save_article_queue"),
            patch.object(draft, "_sanitize_article_final_html"),
            patch.object(draft, "_publish_quality_error", return_value=""),
            patch.object(draft, "get_credentials", return_value=object()),
            patch.object(draft, "create_blogger_service", return_value=service),
            patch.object(draft, "is_local_publisher", return_value=False),
            patch.object(draft, "_ensure_jobs_target_blog"),
            patch.object(draft, "JOBS_TEST_MODE", False),
            patch.object(
                draft,
                "_get_saved_post_by_id",
                return_value={
                    "id": "post-123",
                    "status": "LIVE",
                    "url": article["blogger_post_url"],
                },
            ),
            patch.object(draft, "_execute_blogger_request", return_value=updated),
            patch.object(draft, "_ensure_returned_post_url", side_effect=lambda _s, post: post),
            patch.object(draft, "_publish_if_live", side_effect=lambda _s, post, _m: post),
            patch.object(draft, "_apply_jobposting_schema", side_effect=lambda _s, post, _a, _m: post),
            patch.object(draft, "record_published_article", return_value={"saved": True}),
            patch.object(draft, "notify_job_url", return_value={"status": "disabled"}),
            patch.object(draft, "_reject_numeric_new_job_permalink") as reject_numeric,
        ):
            result = draft.publish_one_blogger_post(
                target_article_id="repair-live",
                mode="live",
            )

        self.assertTrue(result["updated_existing"])
        self.assertFalse(result["created_new"])
        self.assertEqual(article["blogger_post_id"], "post-123")
        self.assertEqual(article["job_campaign_id"], "campaign-123")
        self.assertEqual(posts.update.call_args.kwargs["postId"], "post-123")
        posts.insert.assert_not_called()
        reject_numeric.assert_not_called()

    def test_saved_live_post_refuses_draft_title_fallback(self):
        article = {
            "id": "repair-live-draft-mode",
            "url": "https://example.com/jobs/repair-live-draft-mode",
            "status": "selected",
            "publish_status": "repair_pending",
            "processing_status": "ready_for_ai",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "ai_provider_used": "gemini",
            "final_html": "<p>محتوى مصحح.</p>",
            "seo_title": "إعلان مصحح",
            "blogger_post_id": "post-live-77",
            "blogger_post_url": "https://example.blogspot.com/job-77.html",
            "job_campaign_id": "campaign-77",
        }
        queue = {"articles": [article]}
        service = MagicMock()
        posts = MagicMock()
        service.posts.return_value = posts

        with (
            patch.object(draft, "load_article_queue", return_value=queue),
            patch.object(draft, "save_article_queue"),
            patch.object(draft, "_sanitize_article_final_html"),
            patch.object(draft, "_publish_quality_error", return_value=""),
            patch.object(draft, "get_credentials", return_value=object()),
            patch.object(draft, "create_blogger_service", return_value=service),
            patch.object(draft, "is_local_publisher", return_value=False),
            patch.object(draft, "_ensure_jobs_target_blog"),
            patch.object(
                draft,
                "_get_saved_post_by_id",
                return_value={
                    "id": "post-live-77",
                    "status": "LIVE",
                    "url": article["blogger_post_url"],
                },
            ),
            patch.object(draft, "_find_matching_blogger_posts") as find_matches,
        ):
            result = draft.publish_one_blogger_post(
                target_article_id="repair-live-draft-mode",
                mode="draft",
            )

        self.assertFalse(result["created_new"])
        self.assertFalse(result["updated_existing"])
        self.assertIn("authoritative", result["error"])
        find_matches.assert_not_called()
        posts.insert.assert_not_called()
        posts.update.assert_not_called()

    def test_published_job_waits_for_visual_retry_before_archive(self):
        article = {
            "id": "published-visual-pending",
            "url": "https://example.com/jobs/visual-pending",
            "status": "published",
            "publish_status": "published",
            "job_document_render_status": "document_render_retry",
            "job_document_render_retry_after": "2099-01-01T00:00:00+00:00",
        }
        queue = {"articles": [article]}
        with patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue") as save, \
             patch.object(article_queue, "JOBS_MODE", True):
            archived = article_queue.archive_published_queue_article(
                article_id=article["id"],
                article_url=article["url"],
            )

        self.assertFalse(archived)
        self.assertFalse(article.get("archived", False))
        self.assertEqual(article["archive_deferred_reason"], "visual_retry_pending")
        save.assert_called_once()

    def test_released_logo_wait_does_not_call_ai_again(self):
        article = {
            "id": "legacy-logo-ready",
            "url": "https://example.com/jobs/legacy-logo",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "ai_provider_used": "gemini",
            "final_html": "<p>مقال جاهز ومراجع.</p>",
            "visual_content_reuse_required": True,
            "ai_input_package": {
                "url": "https://example.com/jobs/legacy-logo",
            },
        }
        queue = {"articles": [article]}
        with (
            patch.object(ai, "JOBS_MODE", True),
            patch.object(ai, "load_article_queue", return_value=queue),
            patch.object(ai, "save_article_queue"),
            patch.object(ai, "_attempt_provider_sequence") as providers,
            patch.object(ai, "_generate_with_provider_name") as generate,
        ):
            result = ai.process_one_selected_article_with_ai(
                target_article_id=article["id"],
            )

        self.assertEqual(result["processed"], 0)
        self.assertEqual(article["ai_status"], "completed")
        providers.assert_not_called()
        generate.assert_not_called()

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

    def test_no_deadline_job_uses_official_publish_date_before_discovery_date(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        old_published = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        discovered_now = datetime.now(timezone.utc).isoformat()
        queue = {
            "articles": [{
                "id": "old-official-listing",
                "url": "https://example.com/jobs/old-official-listing",
                "title": "Old Official Listing",
                "status": "ready",
                "content_fetch_status": "",
                "job_deadline": "",
                "job_published_at": old_published,
                "source_published_at": old_published,
                "discovered_at": discovered_now,
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
        self.assertEqual(
            saved["articles"][0]["archive_reason"],
            "no_deadline_unpublished_older_than_60_days",
        )

    def test_jobs_deadline_cleanup_archives_expired_before_enrichment(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        queue = {
            "articles": [{
                "id": "expired-deadline",
                "url": "https://example.com/jobs/expired-deadline",
                "title": "Expired Deadline Job",
                "status": "ready",
                "content_fetch_status": "success",
                "job_deadline": "2026-09-29",
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.archive_expired_queue_articles(
                    now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
                )
                saved = article_queue.load_article_queue()

        self.assertEqual(stats["expired_archived"], 1)
        self.assertTrue(saved["articles"][0]["archived"])
        self.assertEqual(
            saved["articles"][0]["archive_reason"],
            "job_deadline_passed",
        )

    def test_old_jobs_listing_with_future_deadline_is_archived_by_publication_age(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        queue = {
            "articles": [{
                "id": "old-listing-open-deadline",
                "url": "https://example.com/jobs/open",
                "title": "Still Open Job",
                "status": "ready",
                "content_fetch_status": "success",
                "source_published_at": "2026-09-01T08:00:00+00:00",
                "job_deadline": "2026-10-08",
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.archive_expired_queue_articles(
                    now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
                )
                saved = article_queue.load_article_queue()

        self.assertEqual(stats["stale_jobs_archived"], 1)
        self.assertTrue(saved["articles"][0]["archived"])
        self.assertEqual(saved["articles"][0]["archive_reason"], "job_publication_window_passed")

    def test_stale_cached_results_are_archived_but_live_social_and_pdf_work_survives(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        old = "2024-10-02T08:00:00+00:00"
        rows = [
            {"id": "old-result", "status": "ready", "content_fetch_status": "success",
             "job_notice_type": "final_results", "job_published_at": old},
            {"id": "old-no-deadline", "status": "ready", "job_published_at": old},
            {"id": "boundary", "status": "ready", "job_published_at": "2026-10-01T00:00:00Z"},
            {"id": "unknown", "status": "ready"},
            {"id": "live-pending", "status": "published", "job_published_at": old,
             "facebook_status": "facebook_pending", "job_document_render_status": "document_render_retry"},
            {"id": "live-repair", "status": "selected", "publish_status": "published",
             "blogger_post_id": "saved-id", "job_published_at": old},
        ]
        queue = {"articles": rows}
        with patch.object(article_queue, "JOBS_MODE", True), \
             patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue"):
            stats = article_queue.archive_expired_queue_articles(now=now)
        self.assertEqual(stats["stale_jobs_archived"], 2)
        self.assertEqual([r["id"] for r in rows if r.get("archived")],
                         ["old-result", "old-no-deadline"])
        self.assertEqual(rows[4]["facebook_status"], "facebook_pending")
        self.assertEqual(rows[4]["job_document_render_status"], "document_render_retry")

    def test_old_discovery_rows_never_enter_hot_queue_but_unknown_dates_can_enrich(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"title": "Old Job", "url": "https://example.com/job/old",
             "job_published_at": (now - timedelta(hours=13)).isoformat()},
            {"title": "Fresh Job", "url": "https://example.com/job/fresh",
             "source_published_at": (now - timedelta(hours=1)).isoformat()},
            {"title": "Undated Job", "url": "https://example.com/job/undated"},
        ]
        queue = {"articles": []}
        with patch.object(article_queue, "JOBS_MODE", True), \
             patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue"):
            stats = article_queue.add_articles_to_queue(rows)
            repeated = article_queue.add_articles_to_queue(rows)
        self.assertEqual(stats["stale_jobs_rejected"], 1)
        self.assertEqual(stats["added"], 2)
        self.assertEqual(repeated["added"], 0)
        self.assertEqual(repeated["duplicates"], 2)
        self.assertEqual([r["title"] for r in queue["articles"]], ["Fresh Job", "Undated Job"])

    def test_cached_stale_enrichment_cannot_keep_ready_status(self):
        article = {
            "id": "cached-old", "status": "ready", "category_label": "jobs-morocco",
            "content_fetch_status": "success", "full_article_text": "verified " * 500,
            "job_published_at": "2024-10-02T08:00:00Z",
        }
        with patch.object(article_enricher, "JOBS_MODE", True), \
             patch.object(article_enricher, "load_article_queue", return_value={"articles": [article]}), \
             patch.object(article_enricher, "save_article_queue"), \
             patch.object(article_enricher, "enrich_article") as fetch:
            stats = article_enricher.enrich_ready_articles()
        self.assertEqual(article["status"], "skipped")
        self.assertEqual(stats["already_enriched"], 0)
        fetch.assert_not_called()

    def test_new_live_write_rechecks_publication_age_after_preparation(self):
        for date in ("", "2024-10-02T08:00:00Z", "2099-01-01T00:00:00Z"):
            with self.subTest(publication=date):
                article = {
                    "id": "stale-before-write", "status": "selected",
                    "processing_status": "ready_for_ai", "ai_status": "completed",
                    "ai_quality_status": "passed", "ai_provider_used": "gemini",
                    "final_html": "<p>مقال موثق.</p>", "job_published_at": date,
                }
                service = MagicMock()
                with patch.object(draft, "JOBS_MODE", True), \
                     patch.object(draft, "SAFE_MODE", False), \
                     patch.object(draft, "load_article_queue", return_value={"articles": [article]}), \
                     patch.object(draft, "save_article_queue"), \
                     patch.object(draft, "_sanitize_article_final_html"), \
                     patch.object(draft, "_publish_quality_error", return_value=""), \
                     patch.object(draft, "get_credentials", return_value=object()), \
                     patch.object(draft, "create_blogger_service", return_value=service), \
                     patch.object(draft, "is_local_publisher", return_value=False), \
                     patch.object(draft, "_ensure_jobs_target_blog"), \
                     patch.object(draft, "_find_matching_blogger_posts", return_value=[]):
                    result = draft.publish_one_blogger_post(article["id"], mode="live")
                self.assertFalse(result["created_new"])
                self.assertIn("refusing new live publication", result["error"])
                self.assertEqual(article["ai_status"], "completed")
                service.posts.return_value.insert.assert_not_called()

    def test_write_boundary_allows_exactly_twelve_hours_but_checks_deadline(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        article = {"job_published_at": "2026-10-01T00:00:00Z"}
        with patch.object(draft, "JOBS_MODE", True):
            draft._assert_fresh_job_for_new_live_publish(article, now=now)
            article["job_deadline"] = "2026-09-30"
            with self.assertRaisesRegex(RuntimeError, "deadline passed"):
                draft._assert_fresh_job_for_new_live_publish(article, now=now)

    def test_pdf_links_recovered_from_package_are_rendered_and_inserted(self):
        import tempfile
        from pathlib import Path

        document = job_document_renderer.fitz.open()
        for label in ("Official conditions", "Application requirements"):
            page = document.new_page()
            page.insert_text((50, 50), label)
        payload = document.tobytes()
        document.close()
        link = {"url": "https://example.com/notice.pdf", "label": "شروط الترشح"}
        article = {"id": "package-pdf", "seo_slug": "package-pdf",
                   "ai_input_package": {"job_document_links": [link]}}
        render = job_document_renderer.render_job_document_pages
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(draft, "JOBS_MODE", True), \
             patch.object(ai, "JOBS_MODE", True), \
             patch.object(job_document_renderer, "_download_pdf", return_value=payload), \
             patch.object(draft, "_persist_generated_job_assets", return_value=True) as persist, \
             patch.object(draft, "render_job_document_pages", side_effect=lambda target, **kw:
                          render(target, output_root=Path(tmp), **kw)):
            pages = draft._prepare_job_document_page_images(article)
            html = ai._append_job_document_page_images("<p>مقدمة</p>", article["ai_input_package"])
            self.assertTrue(all(Path(row["path"]).is_file() for row in pages))
        self.assertEqual([p["page_number"] for p in pages], [1, 2])
        self.assertEqual(article["job_document_render_status"], "rendered")
        self.assertEqual(article["job_document_links"], [link])
        self.assertEqual(html.count("class='jobDocPageImage'"), 2)
        self.assertIn("الصفحة 2", html)
        persist.assert_called_once()

    def test_post_deadline_results_notice_stays_publishable(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        queue = {
            "articles": [{
                "id": "results-after-deadline",
                "url": "https://example.com/jobs/results-after-deadline",
                "title": "Final Results",
                "status": "ready",
                "content_fetch_status": "success",
                "job_notice_type": "final_results",
                "job_deadline": "2026-09-20",
                "source_published_at": "2026-09-30T08:00:00+00:00",
            }]
        }
        with TemporaryDirectory() as temp:
            path = Path(temp) / "jobs_article_queue.json"
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.archive_expired_queue_articles(
                    now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
                )
                saved = article_queue.load_article_queue()

        self.assertEqual(stats["expired_archived"], 0)
        self.assertFalse(saved["articles"][0].get("archived", False))

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

    def test_blogger_publish_sets_jobs_facebook_pending(self):
        article = {
            "id": "job-published",
            "status": "selected",
            "publish_status": "",
            "facebook_status": "",
        }
        post = {
            "id": "blogger-1",
            "url": "https://example.blogspot.com/2026/09/job-published.html",
        }
        with (
            patch.object(draft, "JOBS_MODE", True),
            patch.object(draft, "_effective_publish_mode", return_value="live"),
            patch.object(draft, "record_published_article", return_value={"saved": True}),
            patch.object(draft, "notify_job_url", return_value={"status": "disabled"}),
        ):
            draft._apply_success(article, post, "live")

        self.assertEqual(article["publish_status"], "published")
        self.assertEqual(article["facebook_status"], "facebook_pending")
        self.assertEqual(article["facebook_queue_reason"], "published_to_blogger")
        self.assertTrue(article["facebook_queued_at"])

    def test_scheduled_facebook_recovers_queue_even_when_configuration_is_missing(self):
        queue = {"articles": []}
        campaign = {
            "campaign_id": "campaign-recover-missing-config",
            "company": "Example Company",
            "title": "Network Engineer",
            "location": "Casablanca",
            "notice_type": "vacancy",
            "source_url": "https://example.com/jobs/42",
            "blogger_post_id": "blogger-42",
            "blogger_url": "https://example.blogspot.com/2026/09/job-42.html",
            "updated_at": "2026-09-30T22:00:00+00:00",
            "status": "active",
        }
        cache = {
            "links": [{
                "title": "شركة Example توظف مهندس شبكات بالدار البيضاء",
                "url": campaign["blogger_url"],
                "category": "jobs-morocco",
                "published_at": "2026-09-30T22:00:00Z",
            }]
        }
        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "load_article_queue", return_value=queue),
            patch.object(facebook, "save_article_queue"),
            patch.object(facebook, "list_active_job_campaign_records", return_value=[campaign]),
            patch.object(facebook, "load_internal_link_cache", return_value=(cache, {})),
            patch.object(facebook, "classify_urgency", return_value={"level": "normal"}),
            patch.object(facebook, "_is_configured", return_value=False),
        ):
            stats = facebook.drain_scheduled_facebook()

        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue(stats["configuration_missing"])
        self.assertEqual(queue["articles"][0]["facebook_status"], "facebook_pending")

    def test_facebook_missing_configuration_keeps_pending_not_failed(self):
        article = {
            "id": "pending-config",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/2026/09/pending-config.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
        }
        queue = {"articles": [article]}
        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "FACEBOOK_AUTO_POST", True),
            patch.object(facebook, "FACEBOOK_PAGE_ID", ""),
            patch.object(facebook, "FACEBOOK_PAGE_ACCESS_TOKEN", "token"),
            patch.object(facebook, "load_article_queue", return_value=queue),
            patch.object(facebook, "save_article_queue"),
            patch.object(facebook, "classify_urgency", return_value={"level": "normal"}),
        ):
            result = facebook.post_one_article_to_facebook()

        self.assertTrue(result["deferred"])
        self.assertEqual(article["facebook_status"], "facebook_pending")
        self.assertNotIn("facebook_failure_count", article)

    def test_facebook_score_ranks_jobs_but_never_filters_them(self):
        base = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
            "job_number_of_positions": 1,
        }
        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "classify_urgency", return_value={"level": "normal"}),
        ):
            low = dict(base, id="low", job_score=68)
            high = dict(base, id="high", job_score=82)
            self.assertTrue(facebook._eligible_for_facebook(low))
            self.assertTrue(facebook._eligible_for_facebook(high))
            pending, _comments = facebook._facebook_backfill_candidates([low, high])
        self.assertEqual([row["id"] for row in pending], ["high", "low"])

    def test_facebook_nearer_deadline_outranks_higher_score(self):
        base = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
            "job_number_of_positions": 1,
        }
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        near = dict(base, id="near", job_score=60, job_deadline="2026-10-01")
        far = dict(base, id="far", job_score=95, job_deadline="2026-10-20")
        self.assertGreater(
            facebook._facebook_job_priority(near, now=now),
            facebook._facebook_job_priority(far, now=now),
        )

    def test_facebook_queue_aging_prevents_low_score_starvation(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        base = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
            "job_number_of_positions": 1,
        }
        old_low = dict(
            base,
            id="old-low",
            job_score=55,
            facebook_queued_at=(now - timedelta(days=14)).isoformat(),
        )
        fresh_high_far_deadline = dict(
            base,
            id="fresh-high",
            job_score=95,
            job_deadline="2026-11-15",
            facebook_queued_at=now.isoformat(),
        )

        self.assertGreater(
            facebook._facebook_job_priority(old_low, now=now),
            facebook._facebook_job_priority(fresh_high_far_deadline, now=now),
        )

    def test_closing_soon_still_outranks_aged_no_deadline_job(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        base = {
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
            "job_number_of_positions": 1,
        }
        aged = dict(
            base,
            id="aged",
            job_score=95,
            facebook_queued_at=(now - timedelta(days=14)).isoformat(),
        )
        closing = dict(
            base,
            id="closing",
            job_score=50,
            job_deadline="2026-10-05",
            facebook_queued_at=now.isoformat(),
        )

        self.assertGreater(
            facebook._facebook_job_priority(closing, now=now),
            facebook._facebook_job_priority(aged, now=now),
        )

    def test_expired_job_drops_only_from_social_queue(self):
        article = {
            "id": "expired-social",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/2026/09/job.html",
            "facebook_status": "facebook_pending",
            "job_notice_type": "vacancy",
            "job_deadline": "2026-09-29",
        }
        queue = {"articles": [article]}
        with (
            patch.object(facebook, "JOBS_MODE", True),
            patch.object(facebook, "save_article_queue") as save,
        ):
            stats = facebook._sync_jobs_facebook_queue(
                queue,
                now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(article["facebook_status"], "facebook_expired")
        self.assertEqual(article["status"], "published")
        self.assertEqual(article["publish_status"], "published")
        self.assertTrue(article["blogger_post_url"])
        save.assert_called_once()

    def test_jobs_publish_bookkeeping_uses_job_memory_not_generic_db(self):
        article = {"publish_status": "published", "id": "x", "url": "https://example.com/job"}
        with patch.object(main, "JOBS_MODE", True), \
             patch.object(main, "record_job_publish") as record, \
             patch.object(main, "archive_published_queue_article") as archive:
            main._record_successful_publish(article)
        record.assert_called_once_with(article)
        archive.assert_called_once()

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

    def test_model_capacity_failure_rotates_provider_instead_of_backing_off_article(self):
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
                 side_effect=RuntimeError(
                     "groq API error 413: Request too large for model openai/gpt-oss-20b"
                 ),
             ) as generate:
            with self.assertRaises(ai.AIProviderFallbackNeeded):
                ai._generate_with_provider_name("groq", "prompt")
        self.assertEqual(generate.call_count, 1)
        cooldown.assert_called_once_with(candidates[0], generate.side_effect)

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

    def test_verified_position_count_is_injected_before_quality_validation(self):
        data = {
            "title": "شركة مثال تعلن عن توظيف إطار إداري",
            "description": "وصف مهني صالح يوضح تفاصيل إعلان التوظيف وشروط الترشيح والمعلومات الرسمية المتاحة.",
            "slug": "administrative-manager-job",
            "html_content": "<p>تعلن الشركة عن فتح باب الترشيح.</p><h2>التفاصيل</h2><p>معلومات إضافية.</p>",
            "notice_type": "vacancy",
        }
        package = {
            "verified_fact_manifest": {
                "facts": {
                    "positions": [
                        {
                            "value": 1,
                            "confidence": "high",
                            "required_in_output": True,
                        }
                    ]
                }
            }
        }
        fixed = ai._ensure_verified_position_count(dict(data), package)
        self.assertIn("1 منصب", fixed["html_content"])
        self.assertEqual(fixed["html_content"].count("1 منصب"), 1)

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

        self.assertEqual(generated, ["gemini", "gemini", "groq", "groq"])
        self.assertEqual(result["failure_scope"], "quality")
        self.assertEqual(article["ai_quality_repairs_used"], 2)

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
                "_process_job_target",
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

    def test_numeric_permalink_deferral_does_not_rotate_to_another_candidate(self):
        failed = {
            "id": "numeric-permalink-job",
            "url": "https://example.com/jobs/numeric",
            "permalink_attempt": 2,
            "blogger_numeric_permalink_rejected": (
                "https://example.blogspot.com/2026/09/job-title_1234567890.html"
            ),
        }
        reason = (
            "Blogger generated a numeric Jobs permalink; it was deleted and will retry "
            "with a new alphabetic permalink seed."
        )
        with (
            patch.object(main, "_mark_candidate_failure_for_retry") as mark,
            patch.object(main, "_select_retry_candidate") as select,
            patch.object(main, "_process_job_target") as process,
        ):
            success, retries = main._retry_after_single_candidate_failure(
                failed,
                "publish",
                reason,
                "live",
                {},
                {"numeric-permalink-job"},
            )

        self.assertIsNone(success)
        self.assertEqual(retries, [])
        mark.assert_not_called()
        select.assert_not_called()
        process.assert_not_called()


    def test_job_target_defers_clean_numeric_permalink_retry_without_ai_fanout(self):
        selected = {
            "id": "numeric-permalink-job",
            "url": "https://example.com/jobs/numeric",
            "source_name": "Official source",
            "category_label": "jobs-morocco",
            "permalink_attempt": 2,
        }
        ready = dict(selected, processing_status="ready_for_ai")
        ai_done = dict(
            ready,
            ai_status="completed",
            final_html="<p>" + " ".join(["ready"] * 130) + "</p>",
        )
        publish_failed = dict(
            ai_done,
            publish_status="failed",
            blogger_numeric_permalink_rejected=(
                "https://example.blogspot.com/2026/09/job-title_1234567890.html"
            ),
        )
        reason = (
            "Blogger generated a numeric Jobs permalink; it was deleted and will retry "
            "with a new alphabetic permalink seed."
        )
        draft_result = {
            "checked": 1,
            "duplicate_count": 0,
            "updated_existing": False,
            "created_new": False,
            "error": reason,
        }

        with (
            patch.object(
                main,
                "prepare_selected_articles_for_ai",
                return_value={"checked": 1, "ready_for_ai": 1, "failed": 0},
            ),
            patch.object(
                main,
                "process_one_selected_article_with_ai",
                return_value={"processed": 0, "success": 0, "failed": 0},
            ),
            patch.object(main, "publish_one_blogger_post", return_value=draft_result),
            patch.object(
                main,
                "_find_article_by_id",
                side_effect=[ready, ai_done, publish_failed],
            ),
            patch.object(main, "_mark_candidate_failure_for_retry") as mark,
            patch.object(main, "post_one_article_to_facebook") as post_fb,
        ):
            result = main._process_job_target(selected, "live")

        self.assertFalse(result["completed"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["waiting_for_publish_retry"])
        self.assertEqual(result["reason"], reason)
        mark.assert_not_called()
        post_fb.assert_not_called()


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
        self.assertIn("ADAPTIVE ARTICLE STRUCTURE", prompt)
        self.assertIn("There is NO mandatory universal sequence", prompt)
        self.assertIn("Do NOT force all generic fields into one summary table", prompt)
        self.assertIn("EVERY useful URL in job_document_links must remain", prompt)
        self.assertIn("NEVER add <h1>", prompt)
        self.assertIn("No copied boilerplate solely to increase word count", prompt)

    def test_jobs_article_ai_has_no_deterministic_content_fallback(self):
        # Article content must come from the AI/evidence pipeline; removing the
        # old deterministic fallback prevents silent low-quality publishing.
        self.assertFalse(hasattr(ai, "_deterministic_job_article"))

    def test_jobs_ai_output_requires_complete_structured_fields(self):
        package = {
            "url": "https://careers.example.com/jobs/42",
            "job_notice_type": "vacancy",
            "job_application_url": "https://careers.example.com/jobs/42/apply",
        }
        incomplete = {
            "description": "وصف موثق للوظيفة.",
            "slug": "example-cybersecurity-consultant",
            "html_content": "<p>محتوى موثق.</p>",
            "notice_type": "vacancy",
        }
        with patch.object(ai, "JOBS_MODE", True):
            with self.assertRaises(ai.AIIncompleteResponseError):
                ai._validate_ai_output(incomplete, package)

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

    def test_manifest_deadline_check_ignores_exam_date_in_same_sentence(self):
        manifest = {
            "facts": {
                "deadline": [{
                    "value": "2026-09-29",
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": ["29 شتنبر 2026"],
                    "meta": {},
                }]
            },
            "warnings": [],
        }
        blocking, _warnings = fact_manifest.validate_output_against_manifest(
            manifest,
            "إعلان مباراة توظيف",
            (
                "<p>آخر أجل للترشيح هو 29 شتنبر 2026، "
                "وتاريخ إجراء المباراة هو 15 أكتوبر 2026.</p>"
            ),
        )
        self.assertEqual(blocking, [])

    def test_manifest_deadline_check_blocks_wrong_date_attached_to_deadline_cue(self):
        manifest = {
            "facts": {
                "deadline": [{
                    "value": "2026-09-29",
                    "source": "official_detail_page",
                    "confidence": "high",
                    "blocking": True,
                    "required_in_output": True,
                    "aliases": ["29 شتنبر 2026"],
                    "meta": {},
                }]
            },
            "warnings": [],
        }
        blocking, _warnings = fact_manifest.validate_output_against_manifest(
            manifest,
            "إعلان مباراة توظيف",
            (
                "<p>تاريخ إجراء المباراة هو 29 شتنبر 2026. "
                "آخر أجل للترشيح هو 30 شتنبر 2026.</p>"
            ),
        )
        self.assertTrue(
            any("contradicts high-confidence manifest deadline" in reason for reason in blocking),
            blocking,
        )

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

    def test_jobs_finalizer_does_not_relocate_missing_verified_links(self):
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
        # Missing verified links are not silently injected into a different
        # editorial location; the quality gate must send the article back to AI.
        self.assertNotIn(package["job_application_url"], html)
        self.assertNotIn(package["job_detail_url"], html)
        for row in package["job_document_links"]:
            self.assertNotIn(row["url"], html)

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
        self.assertIn("فتح رابط التقديم الرسمي", html)
        self.assertIn("فتح أو تحميل الوثيقة الرسمية", html)
        self.assertNotIn(
            f'href="{package["job_detail_url"]}"',
            html,
        )

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
        import pymupdf as fitz
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

    def test_missing_logo_and_pdf_render_failure_do_not_block_publish_eligibility(self):
        article = {
            "id": "content-ready-visual-missing",
            "url": "https://example.com/jobs/content-ready",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "ai_provider_used": "gemini",
            "final_html": "<p>مقال وظيفة موثق ومكتمل.</p>",
            "job_article_cover_status": "optional_missing_verified_logo",
            "logo_resolution_status": "unavailable_optional",
            "article_logo_used": False,
            "job_document_render_status": "document_render_retry",
            "job_document_render_retry_after": "2099-01-01T00:00:00+00:00",
            "visual_readiness_status": "content_ready_visual_retry",
        }

        self.assertTrue(draft._eligible_for_publish(article))
        self.assertEqual(article["ai_status"], "completed")
        self.assertEqual(article["ai_quality_status"], "passed")

    def test_pdf_render_failure_sets_visual_retry_without_touching_ai(self):
        article = {
            "id": "pdf-render-retry",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "final_html": "<p>مقال صحيح.</p>",
            "job_document_links": [
                {"url": "https://example.com/notice.pdf", "label": "الإعلان"}
            ],
            "ai_input_package": {
                "job_document_links": [
                    {"url": "https://example.com/notice.pdf", "label": "الإعلان"}
                ]
            },
        }

        def failed_render(target, **_kwargs):
            target["job_document_render_attempted_documents"] = 1
            target["job_document_render_failures"] = 1
            target["job_document_render_failed_urls"] = [
                "https://example.com/notice.pdf"
            ]
            return []

        with (
            patch.object(draft, "JOBS_MODE", True),
            patch.object(draft, "render_job_document_pages", side_effect=failed_render),
        ):
            pages = draft._prepare_job_document_page_images(article)

        self.assertEqual(pages, [])
        self.assertEqual(
            article["job_document_render_status"],
            "document_render_retry",
        )
        self.assertTrue(article["job_document_render_retry_after"])
        retry_at = datetime.fromisoformat(article["job_document_render_retry_after"])
        now = datetime.now(timezone.utc)
        self.assertGreater(retry_at, now)
        self.assertLessEqual(retry_at - now, timedelta(minutes=16))
        self.assertEqual(article["ai_status"], "completed")
        self.assertEqual(article["final_html"], "<p>مقال صحيح.</p>")

    def test_document_render_retry_updates_same_blogger_post_without_ai(self):
        article = {
            "id": "published-pdf-retry",
            "status": "published",
            "publish_status": "published",
            "blogger_post_id": "post-123",
            "blogger_post_url": "https://example.blogspot.com/job.html",
            "job_document_render_status": "document_render_retry",
            "job_document_render_retry_after": "2000-01-01T00:00:00+00:00",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "final_html": "<p>مقال صحيح.</p>",
            "seo_title": "وظيفة مهندس نظم",
            "seo_description": "تفاصيل موثقة حول وظيفة مهندس نظم وروابطها الرسمية.",
            "ai_input_package": {},
        }
        queue = {"articles": [article]}
        pages = [{
            "document_url": "https://example.com/notice.pdf",
            "document_label": "الإعلان",
            "page_number": 1,
            "url": "https://raw.example/page-1.jpg",
            "path": "assets/generated/page-1.jpg",
            "alt": "الإعلان — الصفحة 1",
        }]

        post_get = MagicMock()
        post_get.execute.return_value = {
            "id": "post-123",
            "url": article["blogger_post_url"],
            "status": "LIVE",
            "title": article["seo_title"],
        }
        post_update = MagicMock()
        post_update.execute.return_value = {
            "id": "post-123",
            "url": article["blogger_post_url"],
            "status": "LIVE",
            "title": article["seo_title"],
        }
        posts = MagicMock()
        posts.get.return_value = post_get
        posts.update.return_value = post_update
        service = MagicMock()
        service.posts.return_value = posts

        def successful_render(target, force_retry=False):
            self.assertTrue(force_retry)
            target["job_document_page_images"] = list(pages)
            target["job_document_render_status"] = "rendered"
            return list(pages)

        with (
            patch.object(draft, "JOBS_MODE", True),
            patch.object(draft, "load_article_queue", return_value=queue),
            patch.object(draft, "save_article_queue") as save,
            patch.object(
                draft,
                "_prepare_job_document_page_images",
                side_effect=successful_render,
            ),
            patch.object(draft, "_sanitize_article_final_html"),
            patch.object(draft, "get_credentials", return_value=object()),
            patch.object(draft, "create_blogger_service", return_value=service),
            patch.object(draft, "is_local_publisher", return_value=False),
            patch.object(draft, "_ensure_jobs_target_blog"),
            patch.object(draft, "_apply_jobposting_schema"),
            patch.object(draft, "archive_published_queue_article", return_value=True),
        ):
            stats = draft.retry_pending_job_document_renders(max_articles=1)

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["document_rendered"], 1)
        self.assertEqual(stats["synced"], 1)
        posts.get.assert_called_once_with(blogId=draft.BLOG_ID, postId="post-123")
        self.assertEqual(posts.update.call_args.kwargs["postId"], "post-123")
        self.assertEqual(article["job_document_render_status"], "rendered")
        self.assertEqual(article["ai_status"], "completed")
        save.assert_called_once()

    def test_exhausted_visual_sync_retry_archives_published_job(self):
        article = {
            "id": "visual-sync-exhausted",
            "url": "https://example.com/jobs/visual-sync",
            "status": "published",
            "publish_status": "published",
            "blogger_post_id": "post-999",
            "blogger_post_url": "https://example.blogspot.com/visual-sync.html",
            "visual_sync_retry_pending": True,
            "visual_sync_retry_after": "2000-01-01T00:00:00+00:00",
            "visual_sync_retry_count": (
                draft.MAX_JOB_DOCUMENT_RENDER_RETRIES - 1
            ),
            "final_html": "<p>مقال منشور صحيح.</p>",
            "seo_title": "وظيفة مهندس نظم",
            "seo_description": "تفاصيل موثقة حول وظيفة مهندس نظم.",
            "ai_input_package": {},
        }
        queue = {"articles": [article]}
        service = MagicMock()
        service.posts.return_value.update.return_value = MagicMock()

        with (
            patch.object(draft, "JOBS_MODE", True),
            patch.object(draft, "load_article_queue", return_value=queue),
            patch.object(draft, "save_article_queue") as save,
            patch.object(draft, "_sanitize_article_final_html"),
            patch.object(draft, "get_credentials", return_value=object()),
            patch.object(draft, "create_blogger_service", return_value=service),
            patch.object(draft, "is_local_publisher", return_value=False),
            patch.object(draft, "_ensure_jobs_target_blog"),
            patch.object(
                draft,
                "_get_saved_post_by_id",
                return_value={
                    "id": "post-999",
                    "url": article["blogger_post_url"],
                    "status": "LIVE",
                },
            ),
            patch.object(draft, "_build_post_body", return_value={}),
            patch.object(
                draft,
                "_execute_blogger_request",
                side_effect=RuntimeError("temporary blogger sync outage"),
            ),
            patch.object(
                draft,
                "archive_published_queue_article",
                return_value=True,
            ) as archive,
        ):
            stats = draft.retry_pending_job_visuals(max_articles=1)

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["still_pending"], 0)
        self.assertEqual(stats["archived_after_retry"], 1)
        self.assertFalse(article["visual_sync_retry_pending"])
        self.assertEqual(article["visual_sync_status"], "unavailable_optional")
        archive.assert_called_once_with(
            article_id="visual-sync-exhausted",
            article_url="https://example.com/jobs/visual-sync",
        )
        save.assert_called_once()

    def test_visual_only_quality_failure_is_downgraded_without_ai_retry(self):
        article = {
            "id": "visual-quality-only",
            "url": "https://example.com/jobs/visual-quality",
            "status": "selected",
            "processing_status": "ready_for_ai",
            "ai_status": "completed",
            "ai_quality_status": "passed",
            "ai_provider_used": "gemini",
            "seo_title": "وظيفة مهندس نظم لدى Example Company",
            "seo_description": (
                "تفاصيل موثقة حول وظيفة مهندس نظم لدى Example Company "
                "ومتطلبات المنصب وطريقة التقديم الرسمية."
            ),
            "final_html": "<p>مقال صحيح ومكتمل عن الوظيفة.</p>",
            "main_image": "https://raw.example/cover.jpg",
            "job_article_cover_url": "https://raw.example/cover.jpg",
            "company_logo_verified": True,
            "job_document_links": [],
            "ai_input_package": {
                "url": "https://example.com/jobs/visual-quality",
                "main_image": "https://raw.example/cover.jpg",
                "job_article_cover_url": "https://raw.example/cover.jpg",
                "article_images": [{"url": "https://raw.example/cover.jpg"}],
            },
        }
        articles = [article]
        failed = quality_gate.QualityGateResult(
            False,
            "job article must start with the generated cover image",
            120,
            (),
        )
        passed = quality_gate.QualityGateResult(True, "", 120, ())

        with (
            patch.object(draft, "JOBS_MODE", True),
            patch.object(
                draft,
                "validate_before_publish",
                side_effect=[failed, passed],
            ),
            patch.object(draft, "validate_phase3_article_quality", return_value=""),
            patch.object(draft, "format_phase3_article_html", return_value=article["final_html"]),
        ):
            reason = draft._publish_quality_error(article, articles)

        self.assertEqual(reason, "")
        self.assertEqual(article["ai_status"], "completed")
        self.assertEqual(article["ai_quality_status"], "passed")
        self.assertTrue(article["logo_visual_retry_pending"])
        self.assertEqual(article.get("main_image"), "")
        self.assertEqual(article["ai_input_package"].get("main_image"), "")
        self.assertTrue(any(
            "optional visual removed before publish" in warning
            for warning in article.get("pre_publish_warnings", [])
        ))

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
            f"<p><a href='{package['job_detail_url']}'>صفحة الإعلان الرسمية</a></p>"
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

    def test_ai_quality_failure_is_deferred_without_failing_cycle(self):
        schedule = {
            "configured_publish_mode": "live",
            "publish_mode": "live",
            "allowed_now": True,
            "reasons": [],
        }
        article = {
            "id": "quality-retry-job",
            "url": "https://example.com/jobs/quality-retry-job",
            "status": "ready",
            "processing_status": "ready_for_ai",
            "ai_status": "failed",
            "ai_failure_scope": "quality",
            "ai_retry_after": "2099-01-01T00:00:00Z",
            "job_company": "Verified Employer",
            "job_title": "Cybersecurity Analyst",
            "job_score": 90,
        }
        queue = {"articles": [article]}

        with ExitStack() as stack, redirect_stdout(StringIO()):
            stack.enter_context(patch.object(main, "_effective_publish_mode", return_value="live"))
            stack.enter_context(patch.object(main, "_effective_action", return_value="LIVE"))
            stack.enter_context(patch.object(main, "_jobs_one_shot_force_run", return_value=False))
            stack.enter_context(patch.object(main, "SAFE_MODE", False))
            stack.enter_context(patch.object(main, "SAFE_CYCLE_MAX_ARTICLES", 1))
            stack.enter_context(patch.object(main, "SAFE_CYCLE_DRAFT_ONLY", False))
            stack.enter_context(patch.object(main, "repair_job_link_bindings", return_value={}))
            stack.enter_context(patch.object(main, "get_publish_schedule_status", return_value=schedule))
            stack.enter_context(patch.object(main, "print_safe_cycle_status"))
            stack.enter_context(patch.object(main, "run_fetch_only", return_value={
                "failed_sources": [], "zero_link_sources": [], "selected_category": ""
            }))
            stack.enter_context(patch.object(main, "archive_expired_queue_articles", return_value={
                "expired_archived": 0, "missing_date_archived": 0
            }))
            stack.enter_context(patch.object(main, "retry_pending_job_document_renders", return_value={}))
            stack.enter_context(patch.object(main, "run_score_only", return_value={}))
            stack.enter_context(patch.object(main, "run_enrich_only", return_value={
                "failed": 0, "weak": 0
            }))
            stack.enter_context(patch.object(main, "_cooldown_sources_after_candidate_failures"))
            stack.enter_context(patch.object(main, "resolve_identity_pending_articles", return_value={}))
            stack.enter_context(patch.object(main, "ai_circuit_status", return_value={
                "global_open": False
            }))
            stack.enter_context(patch.object(main, "load_article_queue", return_value=queue))
            stack.enter_context(patch.object(main, "save_article_queue"))
            stack.enter_context(patch.object(main, "select_best_job_from_queue", return_value=article))
            stack.enter_context(patch.object(main, "prepare_selected_articles_for_ai", return_value={
                "checked": 1, "ready_for_ai": 1, "failed": 0
            }))
            stack.enter_context(patch.object(main, "_find_article_by_id", return_value=article))
            stack.enter_context(patch.object(main, "process_one_selected_article_with_ai", return_value={
                "processed": 1,
                "success": 0,
                "failed": 1,
                "message": "Jobs article contains an unverified external URL",
            }))
            stack.enter_context(patch.object(
                main,
                "_retry_after_single_candidate_failure",
                return_value=(None, []),
            ))
            stack.enter_context(patch.object(main, "_print_safe_cycle_final_report"))
            result = main.run_safe_cycle_only()

        self.assertFalse(result["completed"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["waiting_for_ai_retry"])
        self.assertEqual(result["step_reached"], "run-ai")
        self.assertEqual(result["target_article_id"], "quality-retry-job")

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
             patch.object(main, "retry_pending_job_document_renders", return_value={}) as visual_retry, \
             patch.object(main, "run_score_only", return_value={"ready": 2}) as score, \
             patch.object(main, "run_enrich_only", return_value={"enriched": 2}) as enrich, \
             patch.object(main, "_print_safe_cycle_final_report"), \
             redirect_stdout(StringIO()):
            result = main.run_safe_cycle_only()
        self.assertTrue(result["skipped"])
        self.assertIsNotNone(result["ingest"])
        fetch.assert_called_once()
        cleanup.assert_called_once()
        visual_retry.assert_called_once_with(max_articles=1)
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


    def test_blogger_interval_honors_configured_minimum_without_hidden_delay(self):
        with patch.object(job_core, "JOBS_MIN_PUBLISH_INTERVAL_MINUTES", 5):
            self.assertEqual(job_core.adaptive_publish_interval_minutes(2), 5)
            self.assertEqual(job_core.adaptive_publish_interval_minutes(8), 5)
            self.assertEqual(job_core.adaptive_publish_interval_minutes(20), 5)

    def test_ai_quality_backoff_retries_before_long_input_backoff(self):
        self.assertEqual(
            ai._fingerprint_backoff_seconds("quality", "quality", 1),
            5 * 60,
        )
        self.assertEqual(
            ai._fingerprint_backoff_seconds("quality", "quality", 2),
            10 * 60,
        )
        self.assertEqual(
            ai._fingerprint_backoff_seconds("article_input", "input", 1),
            30 * 60,
        )


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
                    {
                        "id": "legacy-not-selected",
                        "archived": True,
                        "archived_at": old,
                        "publish_status": "published",
                        "facebook_status": "not_selected",
                    },
                    {
                        "id": "social-expired-terminal",
                        "archived": True,
                        "archived_at": old,
                        "publish_status": "published",
                        "facebook_status": "facebook_expired",
                        "facebook_expired_reason": "job expired before Facebook queue turn",
                    },
                ]
            }
            with patch.object(article_queue, "ARTICLE_QUEUE_PATH", queue_path), \
                 patch.object(article_queue, "JOBS_MODE", True):
                article_queue.save_article_queue(queue)
                stats = article_queue.maintain_article_queue(days=7)
                reloaded = article_queue.load_article_queue()

            self.assertEqual(stats["compacted_archived"], 2)
            self.assertEqual(
                [row["id"] for row in reloaded["articles"]],
                ["facebook-pending", "legacy-not-selected"],
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
            "notice_type": "vacancy",
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

    def test_incomplete_jobs_ai_output_is_rejected_even_when_short(self):
        package = {"url": "https://example.com/jobs/short", "job_notice_type": "vacancy"}
        data = {
            "html_content": "<p>قصير جدا</p>",
            "notice_type": "vacancy",
        }
        with patch.object(ai, "JOBS_MODE", True):
            with self.assertRaises(ai.AIIncompleteResponseError):
                ai._validate_ai_output(data, package)

    def test_jobs_promotion_has_no_overnight_or_calendar_slot_restriction(self):
        tz = ZoneInfo("Africa/Casablanca")
        start = datetime(2027, 1, 1, tzinfo=tz)
        for offset in range(365):
            day = start + timedelta(days=offset)
            for hour in (0, 2, 12, 23):
                now = day.replace(hour=hour)
                result = job_core.facebook_slot_status(now=now.astimezone(timezone.utc))
                self.assertTrue(result["allowed_now"], (now, result))
                self.assertEqual(result["mode"], "immediate")

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

    def test_identity_pending_resolver_retries_legacy_pdf_fingerprint_without_completed_read(self):
        row = {
            "id": "pending-scanned-job",
            "url": "https://example.com/jobs/scanned",
            "status": "identity_pending",
            "content_fetch_status": "success",
            "job_document_links": [
                {"url": "https://example.com/scanned.pdf", "label": "الإعلان"}
            ],
            "identity_evidence_document_fingerprint": "https://example.com/scanned.pdf",
            "job_document_text_download_failures": 0,
            "job_document_texts": [],
            "source_tables": [],
            "source_tables_count": 0,
            "job_detail_url": "https://example.com/jobs/scanned",
        }
        queue = {"articles": [row]}

        def complete_pdf_evidence(article):
            article["job_document_texts"] = [{
                "document_url": "https://example.com/scanned.pdf",
                "document_label": "الإعلان",
                "page_number": 1,
                "page_count": 1,
                "text": "شروط الترشيح الرسمية المستخرجة عبر OCR",
            }]
            article["job_document_text_pages"] = 1
            article["job_document_text_chars"] = 39
            article["job_document_text_download_failures"] = 0
            article["job_document_text_read_complete"] = True
            return article["job_document_texts"]

        with (
            patch.object(article_processor, "load_article_queue", return_value=queue),
            patch.object(article_processor, "save_article_queue"),
            patch.object(
                article_processor,
                "extract_job_document_texts",
                side_effect=complete_pdf_evidence,
            ) as extract,
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
        extract.assert_called_once_with(row)
        self.assertTrue(row["job_document_text_read_complete"])
        self.assertEqual(
            row["identity_evidence_document_fingerprint"],
            "https://example.com/scanned.pdf",
        )

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
            "job_document_text_read_complete": True,
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

    def test_jobs_auto_cycle_schedule_is_continuous_six_minute_cadence(self):
        self.assertEqual(
            main._workflow_schedule(),
            "1,7,13,19,25,31,37,43,49,55 * * * *",
        )

    def test_next_auto_cycle_tick_matches_workflow_minutes(self):
        now = datetime(2026, 9, 30, 21, 51, 8)
        self.assertEqual(
            main._next_auto_cycle_tick(now),
            datetime(2026, 9, 30, 21, 55),
        )
        exact_tick = datetime(2026, 9, 30, 21, 55)
        self.assertEqual(
            main._next_auto_cycle_tick(exact_tick),
            datetime(2026, 9, 30, 22, 1),
        )

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
             patch.object(main, "record_jobs_cycle_result", return_value={}), \
             patch.object(main, "_append_auto_cycle_run_log"), \
             patch.object(main, "log_event"), redirect_stdout(StringIO()):
            result = main.run_auto_cycle_logged()
        self.assertEqual(calls, ["blogger", "facebook"])
        self.assertEqual(result["scheduled_facebook"]["created"], 1)

    def test_social_drain_exception_does_not_fail_blogger_cycle(self):
        with patch.object(main, "JOBS_MODE", True), patch.object(main, "FACEBOOK_AUTO_POST", True), \
             patch.object(main, "_effective_publish_mode", return_value="live"), \
             patch.object(main, "drain_scheduled_facebook", side_effect=RuntimeError("social failed")), \
             patch.object(main, "run_safe_cycle_only", return_value={"completed": True}), \
             patch.object(main, "save_runtime_state_to_git", return_value={}), \
             patch.object(main, "record_jobs_cycle_result", return_value={}), \
             patch.object(main, "_append_auto_cycle_run_log"), \
             patch.object(main, "log_event"), redirect_stdout(StringIO()):
            result = main.run_auto_cycle_logged()
        self.assertTrue(result["completed"])
        self.assertEqual(result["scheduled_facebook"]["failed"], 1)

    def test_facebook_follows_jobs_at_night_with_five_minute_spacing(self):
        now = datetime(2026, 10, 1, 0, 0, tzinfo=ZoneInfo("Africa/Casablanca"))
        self.assertTrue(job_core.facebook_slot_status(now=now)["allowed_now"])
        rows = [{"facebook_status": "posted", "facebook_posted_at": now.isoformat()}]
        with patch.object(facebook, "load_article_queue", return_value={"articles": rows}), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5):
            self.assertFalse(facebook.get_facebook_limits_status(now=now + timedelta(minutes=4))["allowed_now"])
            self.assertTrue(facebook.get_facebook_limits_status(now=now + timedelta(minutes=5))["allowed_now"])

    def test_jobs_follow_article_policy_keeps_spacing_and_allows_all_articles(self):
        now = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        rows = [{"facebook_status": "posted", "facebook_posted_at": (now - timedelta(minutes=10+i)).isoformat()} for i in range(4)]
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "JOBS_FACEBOOK_FOLLOW_ARTICLE", True), \
             patch.object(job_core, "JOBS_FACEBOOK_FOLLOW_ARTICLE", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 240), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            status = facebook.get_facebook_limits_status(now=now)
            self.assertTrue(status["allowed_now"])
            self.assertEqual(status["hard_max_facebook_posts_per_day"], 240)
            rows.append({"facebook_status": "delivery_uncertain", "facebook_delivery_uncertain_at": now.isoformat()})
            self.assertFalse(facebook.get_facebook_limits_status(now=now)["allowed_now"])
            self.assertTrue(facebook.get_facebook_limits_status(now=now + timedelta(minutes=5))["allowed_now"])

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
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 3), \
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
            "facebook_posted_at": (now - timedelta(minutes=4)).isoformat(),
        }]
        with patch.object(facebook, "JOBS_MODE", True), \
             patch.object(facebook, "JOBS_FACEBOOK_MAX_POSTS_PER_DAY", 3), \
             patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5), \
             patch.object(facebook, "load_article_queue", return_value={"articles": rows}):
            result = facebook.get_facebook_limits_status(now=now, urgent=True)
        self.assertFalse(result["allowed_now"])
        self.assertEqual(result["min_minutes_between_facebook_posts"], 5)
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

    def test_facebook_local_image_failure_uses_jobs_social_retry_interval(self):
        article = {}
        with patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5), \
             patch.object(facebook.time, "time", return_value=1000):
            facebook._apply_failure(
                article,
                RuntimeError(
                    "Jobs Facebook image generation failed; refusing text-only publish."
                ),
            )

        self.assertEqual(article["facebook_status"], "failed")
        self.assertEqual(article["facebook_failure_count"], 1)
        self.assertEqual(article["facebook_retry_delay_seconds"], 5 * 60)
        self.assertEqual(article["facebook_retry_after_epoch"], 1000 + 5 * 60)
        self.assertFalse(facebook._facebook_retry_ready(article, now_epoch=1299))
        self.assertTrue(facebook._facebook_retry_ready(article, now_epoch=1300))

    def test_legacy_long_image_backoff_is_shortened_after_renderer_fix(self):
        article = {
            "facebook_status": "failed",
            "facebook_error": (
                "Jobs Facebook image generation failed; refusing text-only publish."
            ),
            "facebook_retry_after_epoch": 1000 + 3600,
            "facebook_retry_delay_seconds": 3600,
        }
        with patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5):
            self.assertFalse(
                facebook._facebook_retry_ready(article, now_epoch=1000 + 5 * 60 - 1)
            )
            self.assertTrue(
                facebook._facebook_retry_ready(article, now_epoch=1000 + 5 * 60)
            )

    def test_retry_ready_failed_job_becomes_immediately_eligible_pending(self):
        article = {
            "id": "renderer-retry",
            "status": "published",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/renderer-retry.html",
            "facebook_status": "failed",
            "facebook_error": (
                "Jobs Facebook image generation failed; refusing text-only publish."
            ),
            "facebook_retry_after_epoch": 1000 + 3600,
            "facebook_retry_delay_seconds": 3600,
            "facebook_failure_count": 2,
            "job_notice_type": "vacancy",
        }
        queue = {"articles": [article]}
        with patch.object(facebook, "JOBS_FACEBOOK_MIN_INTERVAL_MINUTES", 5), \
             patch.object(facebook.time, "time", return_value=1000 + 5 * 60), \
             patch.object(
                 facebook,
                 "_recover_jobs_facebook_queue_from_memory",
                 return_value={"recovered": 0, "skipped_terminal": 0},
             ), \
             patch.object(facebook, "_persist_jobs_social_state"), \
             patch.object(facebook, "save_article_queue"):
            stats = facebook._sync_jobs_facebook_queue(queue)
            pending, _comments = facebook._facebook_backfill_candidates(
                queue["articles"]
            )

        self.assertEqual(stats["queued"], 1)
        self.assertEqual(article["facebook_status"], "facebook_pending")
        self.assertNotIn("facebook_retry_after_epoch", article)
        self.assertNotIn("facebook_retry_delay_seconds", article)
        self.assertNotIn("facebook_failure_count", article)
        self.assertNotIn("facebook_error", article)
        self.assertEqual([row["id"] for row in pending], ["renderer-retry"])

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
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job.html",
            "facebook_post_source": "social_ai",
            "facebook_post_text": (
                "فرصة توظيف لمهندس شبكات في الرباط.\n\n"
                "راجع تفاصيل الشروط والمهام في المقال.\n\n"
                "رابط المقال في أول تعليق 👇\n"
                "#وظائف #المغرب #شبكات"
            ),
        }
        blueprint = facebook._jobs_facebook_blueprint(
            article,
            article["blogger_post_url"],
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



    def test_jobs_cached_social_ai_copy_is_reused_without_regeneration(self):
        article = {
            "id": "job-caption-cache",
            "job_campaign_id": "campaign-1",
            "seo_title": "شركة تجريبية توظف مهندس نظم في الدار البيضاء",
            "job_title": "مهندس نظم",
            "job_company": "شركة تجريبية",
            "job_location": "الدار البيضاء",
            "job_notice_type": "vacancy",
            "suggested_category": "jobs-morocco",
            "publish_status": "published",
            "blogger_post_url": "https://example.blogspot.com/p/job-caption-cache.html",
            "facebook_post_source": "social_ai",
            "facebook_post_text": (
                "فرصة جديدة لمهندس نظم في الدار البيضاء.\n\n"
                "التفاصيل الكاملة والشروط في المقال.\n\n"
                "رابط المقال في أول تعليق 👇\n"
                "#وظائف #المغرب #تقنية"
            ),
        }
        with patch.object(facebook, "generate_jobs_facebook_post") as generate:
            first = facebook._jobs_facebook_blueprint(
                article,
                article["blogger_post_url"],
            )
            second = facebook._jobs_facebook_blueprint(
                article,
                article["blogger_post_url"],
            )
        generate.assert_not_called()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["caption"], second["caption"])



if __name__ == "__main__":
    unittest.main()
