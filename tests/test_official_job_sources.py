import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import article_enricher
import scraper
from official_job_sources import (anapec_detail_html, ofppt_detail_evidence,
                                  ofppt_detail_html, parse_anapec_links,
                                  smartrecruiters_detail_html, smartrecruiters_listing_rows)


class OfficialJobSourcesTests(unittest.TestCase):
    def test_ofppt_parser_only_accepts_real_official_notices(self):
        page = """<a href="/offre/3802">Formateur en génie mécanique</a>
        <a href="/offres">All jobs</a>
        <a href="https://other.example/offre/9999">Impostor</a>
        <a href="/offre/3803">Chargé de gestion</a>"""
        found = scraper._parse_ofppt_job_links(page, "https://recrutement.ofppt.ma/offres")
        self.assertEqual(len(found), 2)
        self.assertTrue(found[0]["url"].endswith("/offre/3802"))
        self.assertNotIn("source_published_at", found[0])

    def test_ofppt_listing_reads_deadline_and_skips_expired_cards(self):
        page = """
        <article class="offer-box">
          <strong class="title"><a href="/offre/3808">Chef du Service Amérique et Asie</a></strong>
          <p class="article-actions">Référence: RH 118/26
            <i class="fa fa-circle"></i> Date d’expiration: 01.11.2026
            <a href="/offre/3808">Détails de l'offre</a></p>
        </article>
        <article class="offer-box">
          <strong class="title"><a href="/offre/3064">Formateur en Froid et Génie Thermique</a></strong>
          <p class="article-actions">Référence: RH 88/25
            <i class="fa fa-circle"></i> Date d’expiration: 27.06.2025
            <a href="/offre/3064">Détails de l'offre</a></p>
        </article>
        """
        found = scraper._parse_ofppt_job_links(
            page,
            "https://recrutement.ofppt.ma/offres?page=1",
            now=datetime(2026, 10, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["job_deadline"], "2026-11-01")
        self.assertEqual(found[0]["job_deadline_display"], "01.11.2026")
        self.assertEqual(found[0]["job_external_reference"], "RH 118/26")
        self.assertEqual(found[0]["job_notice_status"], "active")
        self.assertNotIn("3064", found[0]["url"])

    def test_ofppt_detail_extracts_table_pdf_and_specific_apply_action(self):
        document = """
        <div id="offer-container">
          <h1>Chef du Service Amérique et Asie</h1>
          <table id="offer-infos"><tbody>
            <tr><td><strong>Référence</strong></td><td>RH 118/26</td></tr>
            <tr><td><strong>Poste</strong></td><td>Chef du Service Amérique et Asie</td></tr>
            <tr><td><strong>Dernier Délai</strong></td><td>01.11.2026</td></tr>
          </tbody></table>
          <table><tr><td>Avis de concours</td><td>
            <a href="https://recrutement.ofppt.ma/files/offer/3808/avis.pdf?preview=Y">Télécharger</a>
          </td></tr></table>
          <a href="javascript:void(0)" onclick="return Offer.postuler(3808)">Postuler à cette offre</a>
        </div>
        """
        evidence = ofppt_detail_evidence(
            document, {}, "https://recrutement.ofppt.ma/offre/3808"
        )
        self.assertFalse(evidence["expired"])
        self.assertEqual(evidence["reference"], "RH 118/26")
        self.assertEqual(evidence["deadline"], "2026-11-01")
        self.assertEqual(evidence["fields"]["job_application_url"],
                         "https://recrutement.ofppt.ma/offre/3808/postuler")
        self.assertEqual(evidence["fields"]["job_application_link_kind"], "direct_apply")
        self.assertEqual(len(evidence["documents"]), 1)
        article = {
            "job_document_texts": [{"page_number": 1, "text": "Profil requis : Technicien Spécialisé."}]
        }
        rendered = ofppt_detail_html(document, article,
                                     "https://recrutement.ofppt.ma/offre/3808",
                                     evidence=evidence)
        self.assertIn("Technicien Spécialisé", rendered)
        self.assertIn("/offre/3808/postuler", evidence["fields"]["job_application_url"])
        self.assertIn("avis.pdf", rendered)

    def test_ofppt_detail_marks_expired_notice_and_skips_apply_action(self):
        document = """
        <div id="offer-container">
          <h1>Formateur en Froid et Génie Thermique <span>(Expirée)</span></h1>
          <table id="offer-infos"><tbody>
            <tr><td>Référence</td><td>RH 88/25</td></tr>
            <tr><td>Dernier Délai</td><td>27.06.2025</td></tr>
          </tbody></table>
        </div>
        """
        evidence = ofppt_detail_evidence(
            document, {}, "https://recrutement.ofppt.ma/offre/3064"
        )
        self.assertTrue(evidence["expired"])
        self.assertEqual(evidence["fields"]["job_notice_status"], "expired")
        self.assertNotIn("job_application_url", evidence["fields"])

    def test_ofppt_enrichment_uses_official_pdf_and_keeps_apply_action(self):
        document = """
        <div id="offer-container">
          <h1>Formateur en Froid et Génie Thermique</h1>
          <table id="offer-infos"><tbody>
            <tr><td>Référence</td><td>RH 88/26</td></tr>
            <tr><td>Poste</td><td>Formateur en Froid et Génie Thermique</td></tr>
            <tr><td>Dernier Délai</td><td>01.11.2026</td></tr>
          </tbody></table>
          <table><tr><td>Avis de concours</td><td>
            <a href="https://recrutement.ofppt.ma/files/offer/3900/avis.pdf">Télécharger</a>
          </td></tr></table>
        </div>
        """
        article = {
            "id": "ofppt-3900",
            "title": "Formateur en Froid et Génie Thermique",
            "url": "https://recrutement.ofppt.ma/offre/3900",
            "canonical_url": "https://recrutement.ofppt.ma/offre/3900",
            "source_name": "OFPPT — offres d'emploi",
            "official_source": True,
        }
        pdf_text = (
            "L'Office lance un appel à candidatures pour le poste de Formateur. "
            "Profil requis : être titulaire du diplôme de Technicien Spécialisé "
            "ou équivalent dans le domaine du froid et de la climatisation. "
            "Le formateur assure la planification, la conception et l'animation "
            "des formations au profit des stagiaires. Les principales tâches "
            "comprennent la dispensation des programmes, l'évaluation des "
            "compétences et l'encadrement des stages en entreprise. "
            "Les candidats doivent déposer leur dossier sur le site officiel "
            "avant la date limite indiquée dans l'avis. "
        )

        def fake_documents(target, **_kwargs):
            target["job_document_texts"] = [{"page_number": 1, "text": pdf_text}]
            return target["job_document_texts"]

        with patch.object(article_enricher, "extract_job_document_texts", side_effect=fake_documents), \
             patch.object(article_enricher, "resolve_company_logo", return_value={}):
            ok, error = article_enricher._apply_enrichment_from_html(
                article, document, article["url"]
            )

        self.assertTrue(ok, error)
        self.assertEqual(article["job_deadline"], "2026-11-01")
        self.assertEqual(article["job_external_reference"], "RH 88/26")
        self.assertEqual(
            article["job_application_url"],
            "https://recrutement.ofppt.ma/offre/3900/postuler",
        )
        self.assertEqual(article["job_application_link_kind"], "direct_apply")
        self.assertIn("Technicien Spécialisé", article["full_article_text"])
        self.assertEqual(article["content_fetch_status"], "success")

    def test_ofppt_expired_offer_fails_before_fetching_pdf(self):
        document = """
        <div id="offer-container">
          <h1>Formateur en Froid et Génie Thermique <span>(Expirée)</span></h1>
          <table id="offer-infos"><tbody>
            <tr><td>Référence</td><td>RH 88/25</td></tr>
            <tr><td>Dernier Délai</td><td>27.06.2025</td></tr>
          </tbody></table>
        </div>
        """
        article = {
            "id": "ofppt-3064",
            "title": "Formateur en Froid et Génie Thermique",
            "url": "https://recrutement.ofppt.ma/offre/3064",
            "source_name": "OFPPT — offres d'emploi",
        }
        with patch.object(article_enricher, "extract_job_document_texts") as documents:
            ok, error = article_enricher._apply_enrichment_from_html(
                article, document, article["url"]
            )
        self.assertFalse(ok)
        self.assertIn("expired", error)
        self.assertEqual(article["job_deadline"], "2025-06-27")
        self.assertEqual(article["job_notice_status"], "expired")
        documents.assert_not_called()

    def test_accor_morocco_parser_only_accepts_detail_jid(self):
        page = """<a href="/global/en/job/valet-de-chambre-in-casablanca-morocco-jid-83352">Valet de chambre</a>
        <a href="/global/en/jobs">All jobs</a>
        <a href="https://fake.example/global/en/job/false-jid-333">Wrong site</a>"""
        found = scraper._parse_accor_morocco_job_links(page, "https://careers.accor.com/global/en/morocco")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["title"], "Valet de chambre")

    def test_tanmia_parser_discovers_only_real_dated_vacancy_cards(self):
        document = '''
        <h3><a href="https://tanmia.ma/09-10-2026/89155/">Laboratory & Diagnostic Officer</a></h3>
        <h3><a href="/08-10-2026/89101/">Senior Driver G3 Rabat</a></h3>
        <h3><a href="https://evil.example/09-10-2026/89188/">False outside-site job</a></h3>
        <h3><a href="/evenements/">Events and other news</a></h3>
        <h3><a href="/45-99-2026/89156/">Impossible dated post</a></h3>
        '''
        rows = scraper._parse_tanmia_job_links(document, "https://tanmia.ma/offres-demploi/")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_published_at"], "2026-10-09")
        self.assertEqual(rows[0]["published_at_source"], "third_party_listing_date_only")
        self.assertEqual(rows[1]["source_published_at"], "2026-10-08")
        self.assertNotIn("job_application_url", rows[0])

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

    def test_anapec_requests_transport_returns_official_html(self):
        response = Mock(status_code=200, text="<html><body>Aucune offre disponible</body></html>")
        with patch.dict(scraper.os.environ, {"GITHUB_ACTIONS": ""}, clear=False), \
             patch.object(scraper.requests, "get", return_value=response) as get:
            text, error, status = scraper._fetch_anapec_text_sync(
                "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
            )
        self.assertIn("Aucune offre", text)
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(get.call_args.kwargs["timeout"][1], 12.0)
        self.assertTrue(get.call_args.kwargs["allow_redirects"])

    def test_anapec_async_fetch_uses_requests_transport_not_aiohttp(self):
        response = Mock(status_code=200, text="<html>Aucune offre disponible</html>")
        session = Mock()
        with patch.dict(scraper.os.environ, {"GITHUB_ACTIONS": ""}, clear=False), \
             patch.object(scraper.requests, "get", return_value=response):
            text, error, status = asyncio.run(
                scraper._fetch_text_async(
                    session,
                    "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
                )
            )
        self.assertIn("Aucune offre", text)
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        session.get.assert_not_called()

    def test_anapec_actions_prefers_curl_transport(self):
        with patch.dict(scraper.os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(scraper.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(
                 scraper,
                 "_fetch_anapec_with_curl_sync",
                 return_value=("<html>Aucune offre disponible</html>", "", 200),
             ) as curl_fetch, \
             patch.object(scraper.requests, "get") as requests_get:
            text, error, status = scraper._fetch_anapec_text_sync(
                "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
            )
        self.assertIn("Aucune offre", text)
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        curl_fetch.assert_called_once()
        requests_get.assert_not_called()

    def test_anapec_actions_curl_timeout_falls_back_to_requests(self):
        response = Mock(status_code=200, text="<html>Aucune offre disponible</html>")
        with patch.dict(scraper.os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(scraper.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(
                 scraper,
                 "_fetch_anapec_with_curl_sync",
                 return_value=("", "TimeoutError", None),
             ), \
             patch.object(scraper.requests, "get", return_value=response) as requests_get:
            text, error, status = scraper._fetch_anapec_text_sync(
                "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
            )
        self.assertIn("Aucune offre", text)
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        requests_get.assert_called_once()

    def test_anapec_actions_connect_timeout_does_not_repeat_with_requests(self):
        with patch.dict(scraper.os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), \
             patch.object(scraper.shutil, "which", return_value="/usr/bin/curl"), \
             patch.object(
                 scraper,
                 "_fetch_anapec_with_curl_sync",
                 return_value=("", "CurlConnectTimeout", None),
             ), \
             patch.object(scraper.requests, "get") as requests_get:
            text, error, status = scraper._fetch_anapec_text_sync(
                "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
            )
        self.assertEqual(text, "")
        self.assertEqual(error, "GitHubRunnerConnectTimeout")
        self.assertIsNone(status)
        requests_get.assert_not_called()

    def test_emploi_public_uses_critical_source_timeout_profile(self):
        timeout = scraper._source_fetch_timeout_seconds(
            "https://www.emploi-public.ma/ar/liste-des-concours"
        )
        self.assertGreaterEqual(timeout, 10.0)

    def test_emploi_public_timeout_cooldown_is_five_minutes(self):
        with patch.object(scraper, "record_source_cooldown") as cooldown:
            scraper._record_source_result(
                "https://www.emploi-public.ma/ar/liste-des-concours",
                "Emploi-Public",
                "TimeoutError",
                0,
            )
        self.assertEqual(cooldown.call_args.kwargs["minutes"], 5)

    def test_anapec_uses_longer_fetch_timeout_without_slowing_other_sources(self):
        anapec_timeout = scraper._source_fetch_timeout_seconds(
            "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
        )
        normal_timeout = scraper._source_fetch_timeout_seconds("https://example.com/jobs")
        self.assertGreaterEqual(anapec_timeout, 12.0)
        self.assertEqual(normal_timeout, max(1.0, float(scraper.ASYNC_FETCH_TIMEOUT_SECONDS or 1)))

    def test_anapec_timeout_cooldown_retries_sooner_than_generic_sources(self):
        # Keep this unit test independent from the repository's persisted
        # production source-health failure count.
        with patch.object(scraper, "source_health_record", return_value={"failure_count": 0}), \
             patch.object(scraper, "record_source_cooldown") as cooldown:
            scraper._record_source_result(
                "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
                "ANAPEC — offres nationales",
                "TimeoutError",
                0,
            )
        self.assertEqual(cooldown.call_args.kwargs["minutes"], 5)

    def test_legacy_anapec_timeout_cooldown_is_capped_at_five_minutes(self):
        source = {
            "name": "ANAPEC — offres nationales",
            "base_url": "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
        }
        old_failure = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        with patch.object(scraper, "is_source_cooled_down", return_value=(True, "2099-01-01T00:00:00Z")), \
             patch.object(scraper, "source_health_record", return_value={
                 "last_error": "head refresh: TimeoutError",
                 "last_failure_at": old_failure,
             }):
            healthy, skipped = scraper._filter_healthy_sources([source])
        self.assertEqual(healthy, [source])
        self.assertEqual(skipped, [])

    def test_anapec_repeated_timeouts_back_off_without_disabling_source(self):
        source = "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all"
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 0}), 5)
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 2}), 15)
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 6}), 30)

    def test_emploi_public_timeout_backoff_is_bounded(self):
        source = "https://www.emploi-public.ma/ar/liste"
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 0}), 5)
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 2}), 10)
        self.assertEqual(scraper._critical_timeout_cooldown_minutes(source, {"failure_count": 7}), 15)

    def test_anapec_legacy_root_resume_is_discarded(self):
        self.assertEqual(
            scraper._sanitize_discovery_resume(
                "anapec_jobs",
                {"kind": "html", "url": "https://www.anapec.org/sigec-app-rv/"},
            ),
            {},
        )
        valid = {
            "kind": "html",
            "url": "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
        }
        self.assertEqual(scraper._sanitize_discovery_resume("anapec_jobs", valid), valid)

    def test_anapec_async_timeout_is_failure_not_zero_jobs_success(self):
        with patch.object(scraper, "_fetch_text_async", return_value=("", "TimeoutError", None)):
            rows, error, status, details = asyncio.run(scraper._collect_article_links_for_source_async(
                None, "https://www.anapec.org/sigec-app-rv/", extractor_type="anapec_jobs"))
        self.assertEqual(rows, [])
        self.assertEqual(error, "TimeoutError")
        self.assertFalse(details["empty_ok"])

    def test_anapec_discovery_retries_official_search_routes(self):
        candidate = {
            "title": "Développeur informatique",
            "url": "https://www.anapec.org/sigec-app-rv/fr/entreprises/bloc_offre_home/1152572/resultat_recherche",
            "ats_provider": "anapec",
            "ats_reference": "1152572",
        }
        with patch.object(
            scraper,
            "_collect_paginated_html_links_async",
            side_effect=[
                ([], "ANAPEC listing contains no recognized official offer links", 200, {}),
                ([candidate], "", 200, {"pages_scanned": 1}),
            ],
        ) as collect:
            rows, error, status, details = asyncio.run(
                scraper._collect_article_links_for_source_async(
                    None,
                    "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
                    extractor_type="anapec_jobs",
                )
            )
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ats_reference"], "1152572")
        tried = details["tried_listing_urls"]
        self.assertEqual(tried[0], "https://www.anapec.org/sigec-app-rv/chercheurs/resultat_recherche/tout:all")
        self.assertEqual(tried[1], "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all")
        self.assertEqual(collect.call_count, 2)

    def test_anapec_transport_failure_fails_over_to_other_official_host(self):
        candidate = {
            "title": "Technicien systèmes",
            "url": "https://www.anapec.ma/sigec-app-rv/fr/entreprises/bloc_offre_home/1152999/resultat_recherche",
            "ats_provider": "anapec",
            "ats_reference": "1152999",
        }
        with patch.object(
            scraper,
            "_collect_paginated_html_links_async",
            side_effect=[
                ([], "curl:TimeoutError; requests:TimeoutError", None, {}),
                ([candidate], "", 200, {"pages_scanned": 1}),
            ],
        ) as collect:
            rows, error, status, details = asyncio.run(
                scraper._collect_article_links_for_source_async(
                    None,
                    "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
                    extractor_type="anapec_jobs",
                )
            )
        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ats_reference"], "1152999")
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(
            details["tried_listing_urls"],
            [
                "https://www.anapec.org/sigec-app-rv/chercheurs/resultat_recherche/tout:all",
                "https://www.anapec.ma/sigec-app-rv/chercheurs/resultat_recherche/tout:all",
            ],
        )
        self.assertEqual(details["transport_failures"], 1)
        self.assertEqual(details["failed_transport_hosts"], ["www.anapec.org"])

    def test_anapec_transport_failures_stop_after_both_official_hosts(self):
        with patch.object(
            scraper,
            "_collect_paginated_html_links_async",
            side_effect=[
                ([], "curl:TimeoutError; requests:TimeoutError", None, {}),
                ([], "curl:TimeoutError; requests:TimeoutError", None, {}),
            ],
        ) as collect:
            rows, error, status, details = asyncio.run(
                scraper._collect_article_links_for_source_async(
                    None,
                    "https://www.anapec.org/sigec-app-rv/fr/chercheurs/resultat_recherche/tout:all",
                    extractor_type="anapec_jobs",
                )
            )
        self.assertEqual(rows, [])
        self.assertIn("TimeoutError", error)
        self.assertIsNone(status)
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(
            details["tried_listing_urls"],
            [
                "https://www.anapec.org/sigec-app-rv/chercheurs/resultat_recherche/tout:all",
                "https://www.anapec.ma/sigec-app-rv/chercheurs/resultat_recherche/tout:all",
            ],
        )
        self.assertEqual(details["transport_failures"], 2)
        self.assertEqual(
            details["failed_transport_hosts"],
            ["www.anapec.ma", "www.anapec.org"],
        )

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
