import unittest
from datetime import datetime, timezone

from bs4 import BeautifulSoup

import job_core
import job_extractor
import quality_gate


def sample_job(**overrides):
    row = {
        "source_name": "Official Employer",
        "url": "https://example.com/jobs/12345",
        "canonical_url": "https://example.com/jobs/12345",
        "job_title": "Technicien informatique",
        "job_company": "Example SA",
        "job_location": "Casablanca",
        "job_country": "MA",
        "job_application_url": "https://example.com/apply/12345",
        "job_number_of_positions": 100,
        "job_deadline": "2026-10-10",
        "job_published_at": "2026-09-28T08:00:00+00:00",
        "job_eligibility": "morocco",
        "official_source": True,
        "source_priority": "S",
        "raw": {"job_id": "12345"},
    }
    row.update(overrides)
    return row


class JobsCoreTests(unittest.TestCase):
    def test_job_headline_style_matches_human_moroccan_patterns(self):
        self.assertEqual(
            quality_gate._job_title_style_reason(
                "inwi توظف مديرًا تقنيًا لمنصة ServiceNow بالدار البيضاء",
                "vacancy",
            ),
            "",
        )
        self.assertEqual(
            quality_gate._job_title_style_reason(
                "الوكالة الوطنية للمحافظة العقارية (ANCFCC): لوائح المدعوين لاجتياز الاختبار الكتابي",
                "candidate_list",
            ),
            "",
        )
        self.assertEqual(
            quality_gate._job_title_style_reason(
                "النتائج النهائية لمباريات توظيف المكتب الوطني للكهرباء ONEE",
                "final_results",
            ),
            "",
        )
        self.assertTrue(
            quality_gate._job_title_style_reason(
                "inwi: مدير تقني ServiceNow بالدار البيضاء",
                "vacancy",
            )
        )

    def test_slug_ignores_mutable_facts(self):
        a = sample_job(job_number_of_positions=100, job_deadline="2026-10-10", job_location="Casablanca")
        b = sample_job(job_number_of_positions=20, job_deadline="2026-11-20", job_location="Rabat")
        campaign_id = "stablecampaign"
        self.assertEqual(
            job_core.desired_slug(a, campaign_id=campaign_id),
            job_core.desired_slug(b, campaign_id=campaign_id),
        )
        slug = job_core.desired_slug(a, campaign_id=campaign_id)
        self.assertNotIn("100", slug)
        self.assertNotIn("2026", slug)

    def test_tracking_parameters_do_not_change_job_url(self):
        a = job_core.canonicalize_job_url("https://Example.com/jobs/123?utm_source=x&gclid=1")
        b = job_core.canonicalize_job_url("https://example.com/jobs/123")
        self.assertEqual(a, b)

    def test_generic_careers_page_is_not_specific_job(self):
        self.assertFalse(job_core.is_job_specific_url("https://example.com/careers"))
        self.assertTrue(job_core.is_job_specific_url("https://example.com/jobs/12345"))

    def test_labels_for_morocco(self):
        labels = job_core.job_labels(sample_job())
        self.assertIn("jobs", labels)
        self.assertIn("jobs-morocco", labels)

    def test_quality_requires_verified_eligibility_and_application(self):
        now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
        good = job_core.score_job(sample_job(), now=now)
        self.assertTrue(good["passed"])

        bad = job_core.score_job(sample_job(job_eligibility="unknown"), now=now)
        self.assertFalse(bad["passed"])

    def test_large_official_near_deadline_is_urgent(self):
        now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
        urgency = job_core.classify_urgency(
            sample_job(job_deadline="2026-09-29", job_number_of_positions=100),
            now=now,
        )
        self.assertTrue(urgency["publish_immediately"])
        self.assertTrue(urgency["allow_daily_override"])

    def test_daily_cap_respects_month_and_weekday(self):
        # Monday in September: weekday allows 2; September max allows 3 => 2.
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(job_core.daily_publish_cap(now), 2)

    def test_extractor_prefers_direct_apply_and_keeps_official_pdf(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@context":"https://schema.org",
          "@type":"JobPosting",
          "title":"Technicien Réseaux",
          "hiringOrganization":{"name":"Example SA","logo":"https://cdn.example.com/logo.png"},
          "jobLocation":{"address":{"addressLocality":"Casablanca","addressCountry":"MA"}},
          "url":"https://example.com/jobs/12345"
        }
        </script></head><body>
          <a href="/jobs/12345/apply">Postuler maintenant</a>
          <a href="/docs/conditions.pdf">Télécharger les conditions</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "Example Jobs",
                "official_source": True,
                "source_country": "MA",
                "source_eligibility": "morocco",
            },
            "https://example.com/jobs/12345",
            full_text="Offre officielle à Casablanca.",
        )
        self.assertEqual(fields["job_application_url"], "https://example.com/jobs/12345/apply")
        self.assertEqual(fields["job_application_link_kind"], "direct_apply")
        self.assertEqual(len(fields["job_document_links"]), 1)
        self.assertEqual(fields["job_document_links"][0]["url"], "https://example.com/docs/conditions.pdf")
        self.assertEqual(fields["company_logo_url"], "https://cdn.example.com/logo.png")

    def test_extractor_detects_lazy_loaded_direct_application_form(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@context":"https://schema.org",
          "@type":"JobPosting",
          "title":"Technical Lead ServiceNow",
          "hiringOrganization":{"name":"inwi"},
          "jobLocation":{"address":{"addressLocality":"Casablanca","addressCountry":"MA"}},
          "url":"https://jobs.inwi.ma/jobs/8463145-technical-lead-servicenow"
        }
        </script></head><body>
          <turbo-frame
            id="application_form"
            src="https://jobs.inwi.ma/jobs/8463145-technical-lead-servicenow/applications/new">
            Téléchargement du formulaire de candidature
          </turbo-frame>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "inwi",
                "official_source": True,
                "source_country": "MA",
                "source_eligibility": "morocco",
            },
            "https://jobs.inwi.ma/jobs/8463145-technical-lead-servicenow",
            full_text="Offre officielle à Casablanca.",
        )
        self.assertEqual(
            fields["job_application_url"],
            "https://jobs.inwi.ma/jobs/8463145-technical-lead-servicenow/applications/new",
        )
        self.assertEqual(fields["job_application_link_kind"], "direct_apply")
        self.assertTrue(any(
            row.get("url", "").endswith("/applications/new")
            for row in fields.get("job_action_links", [])
        ))

    def test_foreign_job_does_not_infer_moroccan_city_from_body_substring(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@context":"https://schema.org",
          "@type":"JobPosting",
          "title":"Driver",
          "hiringOrganization":{"name":"UNICEF"},
          "jobLocation":{"address":{"addressCountry":"BD"}},
          "url":"https://jobs.example.org/595938"
        }
        </script></head><body></body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "UNICEF Vacancies",
                "official_source": True,
                "source_country": "",
                "source_eligibility": "",
            },
            "https://jobs.example.org/595938",
            full_text="Professional services and safeguards for a position in Bangladesh.",
        )
        self.assertEqual(fields.get("job_country"), "BD")
        self.assertFalse(fields.get("job_location"))
        self.assertNotEqual(fields.get("job_location"), "Fes")

    def test_job_quality_gate_enforces_compact_length_and_unique_links(self):
        apply_url = "https://example.com/apply/12345"
        base_article = {
            "url": "https://example.com/jobs/12345",
            "job_application_url": apply_url,
            "seo_title": "فرصة توظيف تقني معلومات لدى شركة في الدار البيضاء",
            "seo_description": "إعلان توظيف رسمي يوضح أهم تفاصيل المنصب وطريقة التقديم المباشر للراغبين في إرسال ترشيحهم عبر الرابط الرسمي.",
        }

        compact_html = (
            "<p>" + " ".join(f"معلومة{i}" for i in range(130)) + "</p>"
            "<h2>طريقة التقديم</h2>"
            f"<p><a href='{apply_url}'>التقديم المباشر</a></p>"
        )
        compact = dict(base_article, final_html=compact_html)
        result = quality_gate.validate_before_publish(compact, check_duplicate=False)
        self.assertTrue(result.passed, result.reason)

        long_html = (
            "<p>" + " ".join(f"تفصيل{i}" for i in range(270)) + "</p>"
            "<h2>طريقة التقديم</h2>"
            f"<p><a href='{apply_url}'>التقديم المباشر</a></p>"
        )
        too_long = dict(base_article, final_html=long_html)
        result = quality_gate.validate_before_publish(too_long, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("too long", result.reason)

        duplicate_links_html = (
            "<p>" + " ".join(f"بيان{i}" for i in range(130)) + "</p>"
            "<h2>طريقة التقديم</h2>"
            f"<p><a href='{apply_url}'>التقديم</a></p>"
            f"<p><a href='{apply_url}'>التقديم مرة ثانية</a></p>"
        )
        duplicate_links = dict(base_article, final_html=duplicate_links_html)
        result = quality_gate.validate_before_publish(duplicate_links, check_duplicate=False)
        self.assertFalse(result.passed)
        self.assertIn("duplicate job link", result.reason)

    def test_campaign_rollover_next_year(self):
        old = {
            "published_at": "2026-01-10T08:00:00+00:00",
            "deadline": "2026-02-01",
        }
        new = sample_job(job_published_at="2027-01-15T08:00:00+00:00")
        self.assertTrue(job_core._campaign_rollover(new, old))


if __name__ == "__main__":
    unittest.main()
