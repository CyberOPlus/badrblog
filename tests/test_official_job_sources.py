import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import article_enricher
import scraper
from official_job_sources import (anapec_detail_html, parse_anapec_links,
                                  smartrecruiters_detail_html, smartrecruiters_listing_rows)


class OfficialJobSourcesTests(unittest.TestCase):
    def test_anapec_offer_ids_dates_and_host_are_bound_to_each_card(self):
        document = '''<table><tr><td><a href="/sigec-app-rv/fr/entreprises/bloc_offre_home/1152572/resultat_recherche">Développeur informatique</a></td><td>Date de publication : 03/10/2026</td></tr>
        <tr><td><a href="/sigec-app-rv/fr/entreprises/bloc_offre_home/1152573/resultat_recherche">Technicien réseaux</a></td></tr></table>
        <a href="https://fake.example/sigec-app-rv/fr/entreprises/bloc_offre_home/999999/resultat_recherche">False job</a>
        <a href="/sigec-app-rv/fr/entreprises/bloc_offre_home/1152572/resultat_recherche">Postuler</a>'''
        rows = parse_anapec_links(document, "https://www.anapec.org/sigec-app-rv/")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_published_at"], "2026-10-03")
        self.assertNotIn("source_published_at", rows[1])

    def test_anapec_login_or_changed_page_is_not_an_empty_healthy_source(self):
        with self.assertRaises(ValueError):
            parse_anapec_links("<form>Connexion mot de passe</form>", "https://www.anapec.org/sigec-app-rv/")
        self.assertEqual(parse_anapec_links("Aucune offre disponible", "https://www.anapec.org/sigec-app-rv/"), [])

    def test_anapec_start_date_is_not_publication_date(self):
        url = "https://www.anapec.org/sigec-app-rv/fr/entreprises/bloc_offre_home/1152572/resultat_recherche"
        document = "<h2>Référence de l’offre : CA0310261152572</h2><p>Date de début : 03/10/2026</p><h2>Description de Poste</h2>"
        result = anapec_detail_html(document, {"title": "Développeur"}, url)
        node = json.loads(BeautifulSoup(result, "html.parser").find("script").string)
        self.assertNotIn("datePosted", node)
        document = document.replace("Date de début", "Date")
        node = json.loads(BeautifulSoup(anapec_detail_html(document, {"title":"Développeur"}, url), "html.parser").find("script").string)
        self.assertEqual(node["datePosted"], "2026-10-03")
        self.assertNotIn("hiringOrganization", node)

    def test_anapec_async_timeout_is_failure_not_zero_jobs_success(self):
        with patch.object(scraper, "_fetch_text_async", return_value=("", "TimeoutError", None)):
            rows, error, status, details = asyncio.run(scraper._collect_article_links_for_source_async(
                None, "https://www.anapec.org/sigec-app-rv/", extractor_type="anapec_jobs"))
        self.assertEqual(rows, [])
        self.assertEqual(error, "TimeoutError")
        self.assertFalse(details["empty_ok"])

    def test_smartrecruiters_follows_pagination_even_when_first_page_is_known(self):
        source = "https://api.smartrecruiters.com/v1/companies/ALTEN/postings?country=ma"
        job = lambda number: {"id":str(number),"name":"Software developer","releasedDate":"2026-10-03T08:00:00Z","location":{"country":"ma","city":"Rabat"}}
        first = {"content":[job(744000153237859)],"totalFound":2}
        second = {"content":[job(744000153237860)],"totalFound":2}
        known = {scraper._discovery_identity(smartrecruiters_listing_rows(first, source)[0])}
        with patch.object(scraper, "_fetch_text_async", side_effect=[(json.dumps(first),"",200),(json.dumps(second),"",200)]) as fetch:
            rows, error, _, _ = asyncio.run(scraper._collect_smartrecruiters_async(None,source,known,3,500,{}))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["ats_reference"],"744000153237860")
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(error, "")

    def test_smartrecruiters_filters_foreign_jobs_and_rejects_wrong_detail(self):
        source = "https://api.smartrecruiters.com/v1/companies/ALTEN/postings?country=ma"
        payload = {"content":[{"id":"744000153237859","name":"Developer","location":{"country":"fr"}}]}
        self.assertEqual(smartrecruiters_listing_rows(payload, source), [])
        with self.assertRaises(ValueError):
            smartrecruiters_detail_html(json.dumps({"id":"wrong"}),{},source.split("?")[0]+"/744000153237859")

    def test_official_ats_detail_passes_existing_enrichment_and_application_binding(self):
        identity = "744000153237859"
        url = "https://api.smartrecruiters.com/v1/companies/ALTEN/postings/"+identity
        description = "Administration des systèmes informatiques et des réseaux. "*20
        payload = {"id":identity,"name":"Développeur informatique","releasedDate":"2026-10-03T08:00:00Z",
                   "refNumber":"REF21054C","company":{"name":"ALTEN MAROC"},
                   "postingUrl":"https://jobs.smartrecruiters.com/ALTEN/"+identity+"-developpeur",
                   "location":{"country":"ma","city":"Rabat"},
                   "jobAd":{"sections":{"jobDescription":{"title":"Description","text":"<p>"+description+"</p>"}}}}
        article = {"title":payload["name"],"url":url,"source_url":url.rsplit("/",1)[0],
                   "source_name":"ALTEN Maroc","official_source":True,"source_country":"MA","ats_provider":"smartrecruiters"}
        with patch.object(article_enricher,"resolve_company_logo",return_value={}):
            ok,error=article_enricher._apply_enrichment_from_html(article,json.dumps(payload),url)
        self.assertTrue(ok,error)
        self.assertEqual(article["job_published_at"],payload["releasedDate"])
        self.assertEqual(article["job_company"],"ALTEN MAROC")
        self.assertEqual(article["job_external_reference"],"REF21054C")
        self.assertTrue(article["job_application_url"])


if __name__ == "__main__":
    unittest.main()
