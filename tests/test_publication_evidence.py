"""Missing posting evidence must retry without weakening publication gates."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup

import article_enricher
import article_queue
import job_core
import job_extractor
from tests.test_jobs_core import sample_job


class PublicationEvidenceTests(unittest.TestCase):
    def test_capgemini_labelled_month_first_date_is_authoritative(self):
        row = sample_job(job_published_at="", source_published_at="")
        soup = BeautifulSoup(
            '<h1>DevOps Cloud Engineer</h1><p>Ref. code: 549563</p>'
            '<p>Posted on: Sep 7, 2026</p><p>Location: Casablanca, MA</p>'
            '<footer>Copyright 2026. Applications close Oct 15, 2026.</footer>',
            "html.parser",
        )
        fields = job_extractor.extract_job_fields(
            soup, row, "https://careers.capgemini.com/job/Casablanca-DevOps/12345/"
        )
        self.assertEqual(fields["job_published_at"], "2026-09-07")
        self.assertFalse(job_core.score_job(
            {**row, **fields}, now=datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        )["passed"])
        self.assertEqual(job_extractor._publication_date_details_from_text(
            "Copyright 2026. Applications close Oct 15, 2026."
        ), ("", ""))

    def test_invalid_labelled_calendar_date_does_not_prove_freshness(self):
        self.assertEqual(job_extractor._publication_date_details_from_text(
            "Posted on: Feb 31, 2026"
        ), ("", ""))

    def test_missing_date_is_deferred_and_never_selected_for_ai(self):
        now = datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        row = sample_job(
            id="missing-date", status="ready", content_fetch_status="success",
            job_published_at="", source_published_at="",
        )
        with (
            patch.object(job_core, "classify_identity", return_value={
                "action": "new", "reason": "new job", "existing": {}
            }),
            patch.object(job_core, "load_job_state", return_value={}),
            patch.object(job_core, "_source_publish_history_from_memory", return_value={}),
        ):
            selected = job_core.select_best_job_from_queue({"articles": [row]}, now=now)
            retry_at = row["candidate_retry_after"]
            selected_again = job_core.select_best_job_from_queue({"articles": [row]}, now=now)
        self.assertIsNone(selected)
        self.assertIsNone(selected_again)
        self.assertEqual(row["status"], "ready")
        self.assertTrue(row["publication_evidence_refresh_pending"])
        self.assertEqual(row["candidate_retry_after"], retry_at)
        self.assertGreater(datetime.fromisoformat(retry_at), now)

    def test_old_terminal_skip_repairs_once_without_reopening_published_or_duplicates(self):
        waiting = {"id": "waiting", "status": "skipped",
                   "skip_reason": "publication time is not verified"}
        protected = [
            dict(waiting, id="published", blogger_post_id="existing-post"),
            dict(waiting, id="archived", archived=True),
            dict(waiting, id="duplicate", skip_reason="job duplicate confirmed: same campaign"),
        ]
        before = copy.deepcopy(protected)
        queue = {"articles": [waiting, *protected]}
        with (
            patch.object(article_queue, "load_article_queue", return_value=queue),
            patch.object(article_queue, "save_article_queue"),
        ):
            result = article_queue.repair_runtime_queue_state()
            waiting["status"] = "skipped"
            waiting["skip_reason"] = "publication time is not verified"
            again = article_queue.repair_runtime_queue_state()
        self.assertEqual(result["publication_evidence_requeued"], 1)
        self.assertTrue(waiting["publication_evidence_refresh_pending"])
        self.assertEqual(again["publication_evidence_requeued"], 0)
        self.assertEqual(protected, before)

    def test_cached_success_refreshes_missing_date_only_after_retry_clock(self):
        row = sample_job(
            id="refresh-date", status="ready", category_label="jobs-morocco",
            content_fetch_status="success", full_article_text="verified detail " * 300,
            job_published_at="", source_published_at="",
            publication_evidence_refresh_pending=True,
            candidate_retry_after="2099-01-01T00:00:00+00:00",
        )
        with (
            patch.object(article_enricher, "load_article_queue", return_value={"articles": [row]}),
            patch.object(article_enricher, "save_article_queue"),
            patch.object(article_enricher, "_can_run_async_fetch", return_value=False),
            patch.object(article_enricher, "enrich_article", return_value=(True, "")) as fetch,
        ):
            article_enricher.enrich_ready_articles()
            fetch.assert_not_called()
            row.pop("candidate_retry_after")
            result = article_enricher.enrich_ready_articles()
        fetch.assert_called_once_with(row)
        self.assertEqual(result["enriched"], 1)
        self.assertEqual(result["already_enriched"], 0)


if __name__ == "__main__":
    unittest.main()
