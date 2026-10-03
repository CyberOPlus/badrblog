import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup

import article_enricher
import article_queue
import job_core
import job_extractor


DETAIL_URL = "https://jobs.unicef.org/cw/en-us/job/596019/technical-support"


def detail_html(title="Technical Support International Consultant (Home based)"):
    # PageUp's real structure: generic OG/H1, then job-specific H2 and dates.
    return f"""
    <html><head><meta property="og:title" content="Vacancies"></head><body>
      <h1>Current vacancies</h1>
      <div id="job"><div id="job-content">
        <h2>{title}</h2>
        <p><b>Job no:</b><span class="job-externalJobNo">596019</span><br>
        <b>Contract type:</b><span class="work-type">Consultant</span><br>
        <b>Duty Station:</b> Lusaka<br>
        <b>Location:</b><span class="location">Zambia</span><br></p>
        <div id="job-details">
          <p>The consultant will support the information system, document its
          operation, investigate technical incidents and train staff who use it.
          Applicants need relevant technical experience and must submit a CV,
          a technical proposal and the requested supporting documents through
          the official application page before the stated deadline.</p>
          <p>Applications from qualified candidates are welcome regardless of nationality.</p>
        </div>
        <p><b>Advertised:</b><span class="open-date">
          <time datetime="2026-10-02T07:00:00Z">02 Oct 2026</time></span><br>
        <b>Deadline:</b><span class="close-date">
          <time datetime="2026-10-07T21:55:00Z">07 Oct 2026</time></span></p>
        <a class="apply-link" href="https://secure.dc7.pageuppeople.com/apply/671/gateway/default.aspx?c=apply&amp;lJobID=596019">Apply now</a>
      </div></div>
      <aside><h2>Unrelated vacancy</h2><span class="open-date">
        <time datetime="2099-01-01T00:00:00Z">Unrelated date</time></span>
        <a href="https://jobs.unicef.org/cw/en-us/job/999999/other">Other job</a>
      </aside>
    </body></html>
    """


def article_row(**overrides):
    row = {
        "id": "unicef-596019", "url": DETAIL_URL, "canonical_url": DETAIL_URL,
        "title": "Technical Support International Consultant (Home based)",
        "fetched_title": "Vacancies", "source_name": "UNICEF Vacancies",
        "source_country": "GLOBAL", "source_eligibility": "unknown",
        "official_source": True, "source_priority": "A+",
    }
    row.update(overrides)
    return row


class UnicefExtractionTests(unittest.TestCase):
    def test_real_detail_fields_pass_gates_only_inside_the_freshness_window(self):
        soup = BeautifulSoup(detail_html(), "html.parser")
        row = article_row()
        fields = job_extractor.extract_job_fields(soup, row, DETAIL_URL)
        row.update(fields)
        self.assertEqual(fields["job_title"], row["title"])
        self.assertEqual(fields["job_published_at"], "2026-10-02T07:00:00Z")
        self.assertEqual(fields["job_deadline"], "2026-10-07T21:55:00Z")
        self.assertEqual(fields["job_country"], "Zambia")
        self.assertEqual(fields["job_location"], "Lusaka")
        self.assertEqual(fields["job_company"], "UNICEF")
        self.assertEqual(fields["job_external_reference"], "596019")
        self.assertEqual(fields["job_eligibility"], "abroad_open")
        self.assertTrue(fields["job_remote"])
        self.assertIn("lJobID=596019", fields["job_application_url"])
        fresh = job_core.score_job(row, now=datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
        self.assertTrue(fresh["passed"], fresh["reasons"])
        stale = job_core.score_job(row, now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc))
        self.assertFalse(stale["passed"])
        self.assertIn("job is older than 24 hours", stale["reasons"])

    def test_national_only_or_unspecified_roles_still_fail_eligibility(self):
        for title in (
            "International Consultant (Open only for Nepalese Nationals)",
            "National Consultant - Nationals only",
            "Technical Consultant (Remote)",
        ):
            with self.subTest(title=title):
                fields = job_extractor.extract_job_fields(
                    BeautifulSoup(detail_html(title), "html.parser"), article_row(), DETAIL_URL
                )
                self.assertEqual(fields["job_eligibility"], "unknown")
                self.assertIn("eligibility must be verified", job_core.score_job(
                    {**article_row(), **fields},
                    now=datetime(2026, 10, 2, 10, tzinfo=timezone.utc),
                )["reasons"])

    def test_listing_other_host_or_mismatched_reference_is_not_a_detail(self):
        soup = BeautifulSoup(detail_html(), "html.parser")
        for url in (
            "https://jobs.unicef.org/cw/en-us/",
            DETAIL_URL.replace("596019", "999999"),
            DETAIL_URL.replace("jobs.unicef.org", "example.com"),
        ):
            self.assertIsNone(job_extractor.unicef_job_content(soup, url))

    def test_enrichment_excludes_unrelated_jobs_and_generic_title(self):
        row = article_row()
        with patch.object(article_enricher, "resolve_company_logo", return_value={}), \
             patch.object(article_enricher, "log_event"):
            result = article_enricher._apply_enrichment_from_html(row, detail_html(), DETAIL_URL)
        self.assertTrue(result[0], result)
        self.assertEqual(row["fetched_title"], row["title"])
        self.assertNotIn("Unrelated vacancy", row["full_article_text"])
        self.assertNotIn("999999", str(row["job_action_links"]))
        self.assertEqual(row["unicef_detail_extraction_version"], 1)

    def test_missing_advertised_timestamp_is_not_invented(self):
        html = detail_html().replace('datetime="2026-10-02T07:00:00Z"', '')
        row = article_row()
        fields = job_extractor.extract_job_fields(BeautifulSoup(html, "html.parser"), row, DETAIL_URL)
        self.assertFalse(fields.get("job_published_at"))

    def test_legacy_rejected_details_retry_once_without_touching_published_or_other_sources(self):
        broken = article_row(status="skipped", content_fetch_status="success",
                             skip_reason="publication time is not verified; generic careers/listing page is not a job posting")
        published = {**copy.deepcopy(broken), "blogger_post_id": "keep-this-post"}
        archived = {**copy.deepcopy(broken), "archived": True}
        unrelated = {**copy.deepcopy(broken), "url": "https://example.com/jobs/123"}
        queue = {"articles": [broken, published, archived, unrelated]}
        before = copy.deepcopy(queue["articles"][1:])
        with patch.object(article_queue, "load_article_queue", return_value=queue), \
             patch.object(article_queue, "save_article_queue"), \
             patch.object(article_queue, "log_event"):
            result = article_queue.repair_runtime_queue_state()
            self.assertEqual(result["unicef_details_requeued"], 1)
            self.assertEqual(broken["status"], "ready")
            self.assertNotIn("content_fetch_status", broken)
            self.assertEqual(queue["articles"][1:], before)
            broken["status"] = "skipped"
            self.assertEqual(article_queue.repair_runtime_queue_state()["unicef_details_requeued"], 0)


if __name__ == "__main__":
    unittest.main()
