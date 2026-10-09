"""Official PDF submission destinations and English-only Blogger permalinks."""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import article_ai_processor as ai
import article_draft_publisher as publisher
import article_processor as processor
import job_core
import job_document_renderer as docs
from job_extractor import _deadline_details_from_text


DETAIL = "https://www.emploi-public.ma/ar/تفاصيل/المباريات/2835f644-995b-4a06-a386-c41338bde62b"
PORTAL = "https://recrutement.enssup.gov.ma"


def public_job(pdf_text, **overrides):
    data = {
        "id": "enssup-test",
        "source_name": "Emploi-Public — services de l'État",
        "official_source": True,
        "job_notice_type": "competition",
        "job_notice_type_source": "source",
        "job_title": "مباراة توظيف تقنيين من الدرجة الثالثة",
        "title": "مباراة توظيف تقنيين من الدرجة الثالثة",
        "job_eligibility": "morocco",
        "job_published_at": "2026-10-09T08:00:00Z",
        "canonical_url": DETAIL,
        "url": DETAIL,
        "job_detail_url": DETAIL,
        "job_application_url": DETAIL,
        "job_application_link_kind": "official_job_page",
        "job_action_links": [],
        "job_document_links": [{"url": "https://www.emploi-public.ma/annonce.pdf", "kind": "document"}],
        "job_document_texts": [
            {"page_number": 2, "text": pdf_text}
        ],
    }
    data.update(overrides)
    return data


class OfficialPdfApplicationTests(unittest.TestCase):
    def test_year_first_slash_deadline_in_arabic_notice(self):
        self.assertEqual(
            _deadline_details_from_text("يجب أن تتم عملية الترشيح وجوبا عبر المنصة، وذلك قبل :2026/10/25."),
            ("2026-10-25", "2026/10/25"),
        )

    def test_enssup_pdf_portal_is_promoted_as_exact_application_url(self):
        row = public_job(
            "على الراغبين اجتياز المباراة الترشيح عبر المنصة الإلكترونية التالية:\n"
            "https://recrutement.enssup.gov.ma\n"
            "شهادة أو دبلوم: Bac+2\n"
            "يجب أن تتم عملية الترشيح وذلك قبل :2026/10/25."
        )
        self.assertEqual(docs.promote_job_document_application_channel(row), PORTAL)
        self.assertEqual(docs.promote_job_document_deadline(row), "2026-10-25")
        self.assertEqual(row["job_application_source"], "official_pdf")
        self.assertEqual(row["job_application_link_kind"], "official_application_channel")
        self.assertEqual(job_core.job_direct_application_policy(row), "")
        now = datetime(2026, 10, 9, 11, tzinfo=timezone.utc)
        result = job_core.score_job(row, now=now)
        self.assertTrue(result["passed"], result["reasons"])

    def test_unproven_official_portal_does_not_pass(self):
        row = public_job("الدبلوم Bac+2",
            job_application_url=PORTAL,
            job_application_link_kind="official_application_channel",
            job_application_source="official_pdf",
        )
        self.assertNotEqual(job_core.job_direct_application_policy(row), "")

    def test_pdf_application_email_gets_mailto_link(self):
        row = public_job(
            "يتعين إرسال ملفات الترشيح عبر البريد الإلكتروني jobs@agency.gov.ma\n"
            "آخر أجل للتقديم 25/10/2026\n"
            "المستوى الدراسي: Bac+2",
        )
        self.assertEqual(
            docs.promote_job_document_application_channel(row),
            "mailto:jobs@agency.gov.ma",
        )
        self.assertTrue(row["job_application_email_verified"])
        self.assertEqual(row["job_application_link_kind"], "direct_email")
        self.assertEqual(job_core.job_direct_application_policy(row), "")
        self.assertTrue(job_core.is_application_url_bound_to_job(row, row["job_application_url"]))

    def test_official_pdf_is_recovered_before_selection(self):
        row = public_job(
            "", status="ready", content_fetch_status="success",
            job_deadline="", job_document_texts=[],
        )
        queue = {"articles": [row]}

        def set_pdf_evidence(article):
            article["job_document_texts"] = [{
                "page_number": 1,
                "text": "شهادة أو دبلوم Bac+2\\n"
                        "الترشيح عبر المنصة https://recrutement.enssup.gov.ma\\n"
                        "وذلك قبل 2026/10/25",
            }]
            article["job_document_text_read_complete"] = True
            article["job_document_text_attempted_documents"] = 1
            docs.promote_job_document_application_channel(article)
            docs.promote_job_document_deadline(article)

        with patch.object(processor, "load_article_queue", return_value=queue), \\
             patch.object(processor, "save_article_queue") as save, \\
             patch.object(processor, "_prepare_identity_evidence", side_effect=set_pdf_evidence), \\
             patch.object(processor, "job_publication_freshness", return_value={"verified": True, "fresh": True}):
            result = processor.recover_public_competition_submission_evidence(max_articles=1)
        self.assertEqual(result, {"checked": 1, "recovered": 1})
        self.assertEqual(row["job_application_url"], PORTAL)
        self.assertEqual(row["job_deadline"], "2026-10-25")
        save.assert_called_once()

    def test_unverified_email_does_not_pass(self):
        row = public_job(
            "", job_application_url="mailto:hello@agency.gov.ma",
            job_application_link_kind="direct_email",
        )
        self.assertTrue(job_core.job_direct_application_policy(row))

    def test_final_article_links_to_pdf_portal_without_source_hop(self):
        row = public_job("شهادة أو دبلوم: Bac+2", job_application_url=PORTAL,
                         job_application_link_kind="official_application_channel",
                         job_application_source="official_pdf")
        row["job_document_texts"][0]["text"] += " " + PORTAL
        row["job_action_links"] = [{"url": PORTAL, "kind": "apply", "label": "الترشيح"}]
        text = (
            '<p>شروط المباراة والمناصب المتاحة.</p>'
            f'<p><a href="{DETAIL}">المصدر الأصلي</a></p>'
        )
        output = ai.format_phase3_article_html(text, row)
        soup = BeautifulSoup(output, "html.parser")
        links = [a["href"] for a in soup.find_all("a", href=True)]
        self.assertEqual(links.count(PORTAL), 1)
        self.assertNotIn(DETAIL, links)
        self.assertIn("الترشيح عبر المنصة الرسمية", soup.get_text(" ", strip=True))
        self.assertEqual(
            [a["href"] for a in BeautifulSoup(ai.format_phase3_article_html(output, row), "html.parser").find_all("a", href=True)].count(PORTAL),
            1,
        )

    def test_english_slug_only_new_blogger_permalink(self):
        row = {"id": "sample", "seo_slug": "ministry-technician-recruitment"}
        self.assertEqual(publisher._permalink_seed_title(row), "ministry technician recruitment")
        self.assertIsNone(publisher._reject_numeric_new_job_permalink(
            MagicMock(), {"url": "https://example.blogspot.com/2026/10/ministry-technician-recruitment.html"}, row
        ))
        with patch.object(publisher, "_execute_blogger_request", return_value={}) as operation:
            with self.assertRaisesRegex(RuntimeError, "non-English"):
                publisher._reject_numeric_new_job_permalink(
                    MagicMock(),
                    {"url": "https://example.blogspot.com/2026/10/وظيفة-تقني.html", "id": "post-1"}, row,
                )
            operation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
