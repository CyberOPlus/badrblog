"""Regression tests for Jobs source fairness and evidence guardrails."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import article_enricher
import company_logo_resolver
import job_document_renderer
import job_extractor
import quality_gate
import requests
from bs4 import BeautifulSoup


class JobsSourceFairnessTests(unittest.TestCase):
    def test_ofppt_document_links_are_bound_to_the_exact_offer_and_host(self):
        html = '''<a href="/files/offer/3804/avis_concours/s3_notice.pdf?preview=Y">Télécharger</a>
        <a href="/files/offer/3805/avis_concours/s3_other.pdf?preview=Y">Wrong offer</a>
        <a href="https://evil.example/files/offer/3804/avis_concours/s3_fake.pdf">Wrong host</a>
        <a href="/files/offer/3804/other/notice.pdf">Wrong path</a>'''
        links = job_extractor.ofppt_official_document_links(BeautifulSoup(html, "html.parser"), "https://recrutement.ofppt.ma/offre/3804")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], "https://recrutement.ofppt.ma/files/offer/3804/avis_concours/s3_notice.pdf?preview=Y")

    def test_ofppt_notice_pdf_is_source_backed_competition_evidence(self):
        html = '''<h1>Directeur des systèmes d'information</h1><p>Dernier Délai : 26.10.2026</p>
        <a href="/files/offer/3804/avis_concours/s3_notice.pdf?preview=Y">Télécharger</a>
        <a href="/offre/3804">Postuler à cette offre</a>'''
        article = {"title":"Directeur des systèmes d'information", "official_source":True, "source_country":"MA", "source_name":"OFPPT — offres d'emploi"}
        fields = job_extractor.extract_job_fields(BeautifulSoup(html, "html.parser"), article, "https://recrutement.ofppt.ma/offre/3804", full_text="Description du poste et conditions générales de candidature.")
        self.assertEqual(fields["job_notice_type"], "competition")
        self.assertEqual(fields["job_notice_type_source"], "source")
        self.assertEqual(len(fields["job_document_links"]), 1)
        self.assertIn("/files/offer/3804/avis_concours/", fields["job_document_links"][0]["url"])

    def test_enrichment_batch_gives_other_hosts_a_slot(self):
        rows = [(i, {"url":f"https://recrutement.ofppt.ma/offre/{3800+i}", "source_name":"OFPPT — offres d'emploi", "status":"ready", "official_source":True, "source_priority":"S", "title":f"Poste OFPPT {i}"}) for i in range(6)]
        rows += [(10+i, {"url":f"https://jobs.example.org/jobs/{i+1}", "source_name":"Other official source", "status":"ready", "official_source":True, "source_priority":"S", "title":f"Poste other {i}"}) for i in range(2)]
        selected, deferred = article_enricher._select_diverse_enrichment_targets(rows, 4, now=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        selected_hosts = [article_enricher._enrichment_source_key(row[1]) for row in selected]
        self.assertEqual(len(selected), 4)
        self.assertEqual(deferred, 4)
        self.assertLessEqual(selected_hosts.count("recrutement.ofppt.ma"), 2)
        self.assertIn("jobs.example.org", selected_hosts)

    def test_quality_gate_rejects_visible_raw_url_but_not_verified_anchor(self):
        self.assertIn("raw URL", quality_gate._job_raw_url_text_reason("<p>المؤسسة: chttps://ens.umS.ac.maللمؤسسة</p>"))
        self.assertEqual(quality_gate._job_raw_url_text_reason('<p><a href="https://example.org/apply/123">التقديم الرسمي</a></p>'), "")

    def test_404_logo_placeholder_is_never_a_candidate_or_verified_logo(self):
        candidates = {}
        company_logo_resolver._candidate(candidates, "https://www.emploi-public.ma/backoffice/files/images/administrations/logo-404.png", 100, "official_page")
        self.assertEqual(candidates, {})
        with patch.object(company_logo_resolver, "_load_registry", return_value={"records": {}}):
            result = company_logo_resolver.verified_company_logo({"job_company":"Example Employer", "company_logo_url":"https://www.emploi-public.ma/backoffice/files/images/administrations/logo-404.png", "company_logo_verified":True, "company_logo_confidence":100})
        self.assertFalse(result["company_logo_verified"])
        self.assertEqual(result["company_logo_url"], "")

    def test_fast_fail_pdf_download_uses_bounded_timeouts(self):
        with patch.object(job_document_renderer.requests, "get", side_effect=requests.exceptions.Timeout("timeout")) as get:
            with self.assertRaises(requests.exceptions.Timeout):
                job_document_renderer._download_pdf("https://recrutement.ofppt.ma/files/offer/3804/avis_concours/s3.pdf", timeout=12, attempts=1, fast_fail=True)
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 12))


if __name__ == "__main__":
    unittest.main()
