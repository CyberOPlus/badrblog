import unittest
from datetime import datetime, timezone

from bs4 import BeautifulSoup

import article_enricher
import job_core
import job_extractor
import quality_gate
import scraper


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

    def test_etalent_parser_accepts_only_real_offer_detail_links(self):
        html = """
        <html><body>
          <a href="/offres">Offres d'emploi</a>
          <a href="/candidat/inscription">Créer mon espace candidat</a>
          <div><h3>CADRE CHARGE D'OPERATIONS</h3><a href="/offre/73">Voir l'offre</a></div>
        </body></html>
        """
        links = scraper._parse_etalent_links(
            html,
            "https://adm.etalent.ma/offres",
            per_source_limit=5,
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], "https://adm.etalent.ma/offre/73")
        self.assertEqual(links[0]["ats_provider"], "etalent")

    def test_phenom_ddo_parser_keeps_only_morocco_jobs(self):
        ddo = {
            "eagerLoadRefineSearch": {
                "jobs": [
                    {
                        "jobId": "ICM-588622",
                        "title": "Mobile Core Network Engineer",
                        "country": "MOROCCO",
                        "location": "CASABLANCA,MOROCCO",
                        "jobSeqNo": "MA-1",
                        "applyUrl": "https://careers-orange.icims.com/jobs/28099/job/login",
                    },
                    {
                        "jobId": "ICM-123",
                        "title": "Other Country Role",
                        "country": "FRANCE",
                        "location": "PARIS,FRANCE",
                        "jobSeqNo": "FR-1",
                        "applyUrl": "https://example.com/fr",
                    },
                ]
            }
        }
        html = (
            "<html><body><script>"
            "var phApp = {}; phApp.ddo = "
            + json.dumps(ddo)
            + ";</script></body></html>"
        )
        rows = scraper._phenom_jobs_from_html(html, country="MOROCCO")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["jobId"], "ICM-588622")

    def test_phenom_structured_payload_enriches_without_scraping_marketing_page(self):
        description = " ".join([
            "Nous recherchons un ingénieur réseau expérimenté pour rejoindre notre équipe au Maroc.",
            "Le poste couvre la conception, le déploiement, le suivi et l'amélioration des solutions techniques.",
            "Le candidat travaille avec les équipes métiers, sécurité et exploitation afin de garantir la qualité du service.",
            "Une expérience confirmée en télécommunications, analyse, documentation et résolution de problèmes est attendue.",
            "La mission comprend également la coordination technique, le partage des connaissances et le suivi des projets clients.",
        ])
        article = {
            "id": "orange-1",
            "title": "Mobile Core Network Engineer",
            "url": "https://careers-orange.icims.com/jobs/28099/mobile-core-network-engineer/job/login",
            "source_name": "Orange Maroc",
            "source_url": "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
            "ats_provider": "phenom",
            "ats_reference": "ICM-588622",
            "ats_description": description,
            "job_application_url": "https://careers-orange.icims.com/jobs/28099/mobile-core-network-engineer/job/login",
            "job_application_link_kind": "direct_apply",
            "job_location": "CASABLANCA,MOROCCO",
            "job_country": "MA",
            "job_contract_type": "CDI",
            "job_company": "Orange Business",
            "phenom_payload": {
                "company": "Orange Business",
                "category": "Réseau",
                "workModel": "Hybride",
                "hiringType": "Temps complet",
            },
        }
        ok, error = article_enricher._apply_phenom_enrichment(article)
        self.assertTrue(ok, error)
        self.assertEqual(article["content_fetch_status"], "success")
        self.assertEqual(article["job_application_link_kind"], "direct_apply")
        self.assertEqual(article["job_country"], "MA")
        self.assertIn("ingénieur réseau", article["full_article_text"].lower())

    def test_csod_structured_payload_enriches_description_and_deadline(self):
        description = "<p>" + " ".join([
            "Nous recherchons un responsable expérimenté pour piloter une fonction stratégique au sein du groupe.",
            "La mission comprend la définition des priorités, la coordination des équipes et le suivi des objectifs opérationnels.",
            "Le candidat devra analyser les besoins, proposer des améliorations et accompagner les parties prenantes dans leur mise en œuvre.",
            "Une expérience solide en management, communication, organisation et conduite de projets complexes est demandée.",
            "Le poste offre un environnement structuré avec une forte collaboration entre équipes et une responsabilité directe sur les résultats.",
        ]) + "</p>"
        article = {
            "id": "iam-69",
            "title": "Head of People Experience & Culture",
            "url": "https://iam.csod.com/ux/ats/careersite/4/home/requisition/69?c=iam",
            "source_name": "Maroc Telecom",
            "source_url": "https://iam.csod.com/ux/ats/careersite/4/home?c=iam",
            "ats_provider": "csod",
            "ats_reference": "69",
        }
        detail = {
            "data": {
                "displayTitle": "Head of People Experience & Culture",
                "externalDescription": description,
                "ref": "req69",
                "requisitionStatusId": 2,
                "allowApply": True,
                "primaryLocation": {
                    "title": "Tour Maroc Telecom avenue Annakhil, Hay Riad Rabat",
                    "country": "MA",
                },
                "companyApplyUrl": "https://iam.csod.com/ux/ats/careersite/4/home/requisition/69?c=iam",
                "openDate": "2026-09-25T16:12:02",
            }
        }
        posting = {
            "data": {
                "postings": [{
                    "isDefault": True,
                    "startDate": "2026-09-25T00:00:00",
                    "endDate": "2026-11-25T23:59:59",
                }]
            }
        }
        ok, error = article_enricher._apply_csod_payloads(article, detail, posting, {})
        self.assertTrue(ok, error)
        self.assertEqual(article["content_fetch_status"], "success")
        self.assertEqual(article["job_deadline"], "2026-11-25")
        self.assertEqual(article["job_country"], "MA")
        self.assertIn("Rabat", article["job_location"])
        self.assertIn("responsable expérimenté", article["full_article_text"].lower())

    def test_csod_article_config_parses_requisition(self):
        cfg = article_enricher._csod_article_config({
            "url": "https://iam.csod.com/ux/ats/careersite/4/home/requisition/69?c=iam"
        })
        self.assertEqual(cfg["site_id"], 4)
        self.assertEqual(cfg["req_id"], "69")
        self.assertIn("/v2/requisitions/69/jobDetails", cfg["detail_url"])

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
