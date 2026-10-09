"""Production-loop regressions for approved-source queue and cached PDF recovery."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import article_queue
import article_processor


DETAIL = "https://www.emploi-public.ma/ar/تفاصيل/المباريات/2835f644-995b-4a06-a386-c41338bde62b"


def public_article(**extras):
    article = {
        "id": "public-test",
        "source_name": "Emploi-Public — services de l'État",
        "url": DETAIL, "canonical_url": DETAIL, "job_detail_url": DETAIL,
        "status": "ready", "content_fetch_status": "success",
        "official_source": True, "job_notice_type": "competition",
        "job_published_at": "2026-10-09T12:00:00Z",
        "job_deadline": "", "job_diploma": "",
        "job_application_url": DETAIL, "job_application_link_kind": "official_job_page",
        "job_eligibility": "morocco",
        "job_action_links": [],
        "job_document_links": [{"kind": "document", "url": "https://example.gov.ma/announcement.pdf"}],
        "job_document_text_read_complete": True,
        "job_document_text_attempted_documents": 1,
        "job_document_texts": [{"page_number": 1, "text":
            "تقديم طلب الترشيح حصريا عبر المنصة التالية: https://recrutement.enssup.gov.ma\n"
            "المستوى الدراسي: Bac+2\n"
            "يجب أن تتم عملية الترشيح وذلك قبل 2026/10/25"}],
        "candidate_failure_stage": "quality-evidence",
        "candidate_retry_after": "2099-01-01T00:00:00+00:00",
        "candidate_failure_reason": "missing application evidence",
        "job_quality_wait_count": 4,
        "ai_retry_after": "2099-01-01T00:00:00+00:00",
    }
    article.update(extras)
    return article


class LiveLoopReliabilityTests(unittest.TestCase):
    def test_archive_only_unpublished_retired_source_jobs(self):
        old_ready = {"id": "u1", "source_name": "UNICEF Vacancies", "status": "ready"}
        old_published = {
            "id": "u2", "source_name": "UNICEF Vacancies",
            "status": "published", "blogger_post_id": "post2",
            "facebook_status": "facebook_pending",
        }
        old_draft = {
            "id": "u3", "source_name": "UNICEF Vacancies",
            "status": "draft_created", "blogger_draft_id": "draft3",
        }
        approved = {"id": "ok", "source_name": "Emploi-Public — services de l'État", "status": "ready"}
        queue = {"articles": [old_ready, old_published, old_draft, approved]}
        with patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue") as saved:
            first = article_queue.archive_retired_source_jobs()
            second = article_queue.archive_retired_source_jobs()
        self.assertEqual(first["retired_archived"], 1)
        self.assertEqual(second["retired_archived"], 0)
        self.assertEqual(old_ready["archive_reason"], "source_removed_from_approved_registry")
        self.assertFalse(old_published.get("archived", False))
        self.assertFalse(old_draft.get("archived", False))
        self.assertFalse(approved.get("archived", False))
        saved.assert_called_once()

    def test_cached_pdf_restores_real_apply_and_releases_only_quality_cooldown(self):
        row = public_article()
        queue = {"articles": [row]}
        with patch.object(article_processor, "load_article_queue", return_value=queue), \
             patch.object(article_processor, "save_article_queue") as saved, \
             patch.object(article_processor, "_prepare_identity_evidence") as extract, \
             patch.object(article_processor, "job_publication_freshness",
                          return_value={"verified": True, "fresh": True}):
            result = article_processor.recover_public_competition_submission_evidence(max_articles=1)
            second = article_processor.recover_public_competition_submission_evidence(max_articles=1)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["cached_rechecks"], 1)
        self.assertEqual(result["quality_cooldowns_released"], 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(row["job_deadline"], "2026-10-25")
        self.assertEqual(row["job_application_url"], "https://recrutement.enssup.gov.ma")
        self.assertNotIn("candidate_retry_after", row)
        self.assertEqual(row["ai_retry_after"], "2099-01-01T00:00:00+00:00")
        saved.assert_called_once()
        extract.assert_not_called()

    def test_cached_pdf_cannot_clear_ai_failure_or_unverified_job(self):
        row = public_article(job_document_texts=[{"page_number": 1, "text": "المستوى الدراسي: Bac+2"}],
                             candidate_failure_stage="run-ai")
        queue = {"articles": [row]}
        with patch.object(article_processor, "load_article_queue", return_value=queue), \
             patch.object(article_processor, "save_article_queue"), \
             patch.object(article_processor, "job_publication_freshness",
                          return_value={"verified": True, "fresh": True}):
            result = article_processor.recover_public_competition_submission_evidence(max_articles=1)
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(result["quality_cooldowns_released"], 0)
        self.assertIn("candidate_retry_after", row)


if __name__ == "__main__":
    unittest.main()
