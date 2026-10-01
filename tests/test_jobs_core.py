import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import article_enricher
import article_queue
import company_logo_resolver
import job_core
import job_extractor
import jobposting
import internal_link_cache
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
    def test_emploi_public_labelled_dates_are_read_from_full_page_not_body_only(self):
        html = """
        <html><body>
          <main>
            <h1>مباراة لتوظيف مهندس دولة من الدرجة الأولى</h1>
            <section>
              <h3>آخر أجل لإيداع الترشيحات</h3><p>27 شتنبر 2026</p>
              <h3>تاريخ إجراء المباراة</h3><p>10 أكتوبر 2026</p>
              <h3>تاريخ النشر</h3><p>7 شتنبر 2026</p>
            </section>
          </main>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        article = sample_job(
            title="مباراة لتوظيف مهندس دولة من الدرجة الأولى",
            job_deadline="",
            job_published_at="",
            source_published_at="",
            ats_provider="emploi_public",
        )
        fields = job_extractor.extract_job_fields(
            soup,
            article,
            "https://www.emploi-public.ma/ar/details/job-id",
            full_text="وصف مختصر للمباراة بدون تواريخ.",
        )
        self.assertEqual(fields["job_deadline"], "2026-09-27")
        self.assertEqual(fields["job_exam_date"], "2026-10-10")
        self.assertEqual(fields["job_published_at"], "2026-09-07")
        self.assertEqual(fields["job_published_at_display"], "7 شتنبر 2026")

    def test_job_specific_application_url_rejects_generic_search_and_careers(self):
        self.assertFalse(job_core.is_job_specific_url("https://company.example/jobs"))
        self.assertFalse(job_core.is_job_specific_url("https://company.example/jobs?search=security"))
        self.assertFalse(job_core.is_job_specific_url("https://company.example/careers?page=2"))
        self.assertTrue(job_core.is_job_specific_url("https://company.example/jobs/8448475-manager-security"))
        self.assertTrue(job_core.is_job_specific_url("https://company.example/apply?job_id=8448475"))
        self.assertFalse(
            job_core.is_job_specific_url(
                "https://secure.dc7.pageuppeople.com/apply/671/cw/applicationForm/default.asp"
            )
        )
        self.assertFalse(
            job_core.is_job_specific_url(
                "https://recrutement.enssup.gov.ma/annonce/list?statutExpiration=ACTIVE"
            )
        )
        self.assertTrue(
            job_core.is_job_specific_url(
                "https://secure.dc7.pageuppeople.com/apply/671/cw/applicationForm/default.asp?lJobID=595952"
            )
        )


    def test_application_url_cannot_cross_wire_same_host_vacancy(self):
        current = sample_job(
            url="https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
            canonical_url="https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
            job_application_url="https://www.emploi-public.ma/ar/تفاصيل/المباريات/85a046f8-2af5-4f26-8b3f-a811967e2a4e",
            raw={"job_id": "85a046f8-2af5-4f26-8b3f-a811967e2a4e"},
        )
        other = "https://www.emploi-public.ma/ar/تفاصيل/المباريات/59305efc-899b-4884-906d-d39e894e6099"
        self.assertTrue(job_core.is_application_url_bound_to_job(current, current["canonical_url"]))
        self.assertFalse(job_core.is_application_url_bound_to_job(current, other))

    def test_public_competition_accepts_verified_central_application_channel(self):
        portal = "https://recrutement.enssup.gov.ma/"
        detail = (
            "https://www.emploi-public.ma/ar/تفاصيل/المباريات/"
            "85a046f8-2af5-4f26-8b3f-a811967e2a4e"
        )
        competition = sample_job(
            url=detail,
            canonical_url=detail,
            job_detail_url=detail,
            job_notice_type="competition",
            official_source=True,
            job_application_url=portal,
            job_action_links=[
                {"url": portal, "label": "إيداع الترشيح", "kind": "apply"}
            ],
        )
        self.assertFalse(job_core.is_job_specific_url(portal))
        self.assertTrue(
            job_core.is_verified_official_application_channel(competition, portal)
        )
        self.assertTrue(job_core.is_application_url_bound_to_job(competition, portal))

    def test_generic_application_portal_stays_rejected_without_public_competition_evidence(self):
        portal = "https://recrutement.enssup.gov.ma/"
        detail = "https://company.example/jobs/12345"

        private_job = sample_job(
            url=detail,
            canonical_url=detail,
            job_detail_url=detail,
            job_notice_type="vacancy",
            official_source=True,
            job_application_url=portal,
            job_action_links=[
                {"url": portal, "label": "Apply", "kind": "apply"}
            ],
        )
        self.assertFalse(job_core.is_application_url_bound_to_job(private_job, portal))

        public_detail = (
            "https://www.emploi-public.ma/ar/تفاصيل/المباريات/"
            "85a046f8-2af5-4f26-8b3f-a811967e2a4e"
        )
        unlinked_competition = sample_job(
            url=public_detail,
            canonical_url=public_detail,
            job_detail_url=public_detail,
            job_notice_type="competition",
            official_source=True,
            job_application_url=portal,
            job_action_links=[],
        )
        self.assertFalse(
            job_core.is_application_url_bound_to_job(unlinked_competition, portal)
        )


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

    def test_slug_prefers_stable_official_reference(self):
        row = sample_job(
            job_title="Consultant cyber-sécurité",
            job_company="Orange Business",
            ats_reference="ICM-584854",
        )
        slug = job_core.desired_slug(row, campaign_id="opaque-campaign")
        self.assertIn("orange-business-consultant-cyber-securite", slug)
        self.assertNotRegex(slug, r"\\d")
        self.assertRegex(slug, r"^[a-z-]+$")

    def test_jobposting_schema_uses_verified_job_entities(self):
        row = sample_job(
            job_contract_type="Temps plein",
            job_salary="8 000 - 12 000 MAD par mois",
            job_application_link_kind="direct_apply",
            company_logo_verified=True,
            company_logo_url="https://example.com/logo.png",
            ats_reference="ICM-584854",
            final_html="<p>Description détaillée du poste.</p>",
        )
        schema = jobposting.build_jobposting(
            row,
            "https://jobs.example.com/2026/09/example-role.html",
        )
        self.assertEqual(schema["@type"], "JobPosting")
        self.assertEqual(schema["title"], "Technicien informatique")
        self.assertEqual(schema["employmentType"], "FULL_TIME")
        self.assertEqual(schema["hiringOrganization"]["name"], "Example SA")
        self.assertTrue(schema["directApply"])
        self.assertEqual(schema["baseSalary"]["currency"], "MAD")
        self.assertEqual(schema["baseSalary"]["value"]["minValue"], 8000)
        self.assertEqual(schema["baseSalary"]["value"]["maxValue"], 12000)
        self.assertEqual(schema["baseSalary"]["value"]["unitText"], "MONTH")
        self.assertFalse(jobposting.jobposting_validation_errors(row))

    def test_jobposting_does_not_guess_unparseable_salary(self):
        row = sample_job(
            job_salary="راتب تنافسي",
            final_html="<p>تفاصيل الوظيفة الرسمية.</p>",
        )
        schema = jobposting.build_jobposting(row, "https://example.com/job")
        self.assertNotIn("baseSalary", schema)

    def test_jobs_internal_link_cache_is_long_lived_and_entity_weighted(self):
        now = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "internal-links.json"
            old = now.replace(year=2026, month=8, day=1)
            data = {
                "links": [
                    {
                        "title": "Orange Business توظف مهندس أمن سيبراني",
                        "url": "https://example.com/jobs/orange-security",
                        "category": "jobs",
                        "published_at": old.isoformat(),
                        "keywords": ["orange", "cyber"],
                        "job_company": "Orange Business",
                        "job_title": "مهندس أمن سيبراني",
                        "job_location": "Casablanca",
                        "job_contract_type": "CDI",
                        "notice_type": "vacancy",
                    }
                ]
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            loaded, stats = internal_link_cache.load_internal_link_cache(
                path=path,
                now=now,
                save=False,
            )
            self.assertEqual(stats["expired_removed"], 0)
            current = sample_job(
                seo_title="Orange Business توظف مستشار أمن سيبراني بالدار البيضاء",
                job_company="Orange Business",
                job_title="مستشار أمن سيبراني",
                job_location="Casablanca",
                job_contract_type="CDI",
                final_html="<p>أمن سيبراني</p>",
                suggested_category="jobs",
            )
            selected = internal_link_cache.select_internal_link_candidates(
                current,
                loaded["links"],
                limit=3,
            )
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["job_company"], "Orange Business")

    def test_jobs_hub_link_is_always_inserted_contextually(self):
        html = "<p>تعلن الشركة عن التوظيف في منصب تقني بالدار البيضاء.</p>"
        linked, count = internal_link_cache.insert_jobs_hub_link(html)
        self.assertEqual(count, 1)
        self.assertEqual(
            linked.count("https://www.cyberoplus.com/search/label/jobs"),
            1,
        )
        self.assertIn(">التوظيف</a>", linked)

        linked_again, second_count = internal_link_cache.insert_jobs_hub_link(linked)
        self.assertEqual(second_count, 0)
        self.assertEqual(
            linked_again.count("https://www.cyberoplus.com/search/label/jobs"),
            1,
        )

    def test_arabic_job_slug_is_readable_and_contains_no_digits(self):
        row = sample_job(
            job_company="وزارة الصحة",
            job_title="تقني من الدرجة الثالثة",
            ats_reference="REF-2026-584854",
        )
        slug = job_core.desired_slug(row, campaign_id="campaign-2026")
        self.assertRegex(slug, r"^[a-z-]+$")
        self.assertNotRegex(slug, r"\\d")
        self.assertTrue(slug.startswith("wzara"), slug)

    def test_jobs_hub_link_is_not_added_to_non_jobs_articles(self):
        html = "<p>هذا خبر تقني عن العمل على تحديث جديد.</p>"
        linked, count = internal_link_cache.insert_internal_links(
            html,
            {"seo_title": "تحديث تقني", "suggested_category": "Tech-News"},
            {"links": []},
        )
        self.assertEqual(count, 0)
        self.assertNotIn(internal_link_cache.JOBS_HUB_URL, linked)

    def test_extractor_keeps_arabic_public_job_files_and_exam_date(self):
        html = """
        <html><body>
          <p>تاريخ إجراء المباراة : 25 أكتوبر 2026</p>
          <p>آخر أجل لإيداع ملفات الترشيح : 5 أكتوبر 2026 - 16:30</p>
          <a href="/files/avis.pdf">تحميل الإعلان</a>
          <a href="/files/decision.pdf">قرار المباراة</a>
          <a href="/candidature">إيداع الترشيح</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "Emploi-Public — services de l'État",
                "official_source": True,
                "source_country": "MA",
                "source_eligibility": "morocco",
                "title": "مباراة لتوظيف تقني من الدرجة الثالثة",
            },
            "https://www.emploi-public.ma/ar/تفاصيل/المباريات/test",
            full_text=soup.get_text(" ", strip=True),
        )
        self.assertEqual(fields["job_exam_date"], "2026-10-25")
        self.assertEqual(fields["job_deadline"], "2026-10-05")
        self.assertEqual(fields["job_notice_type"], "competition")
        self.assertEqual(fields["job_application_link_kind"], "official_application_channel")
        self.assertEqual(fields["job_application_url"], "https://www.emploi-public.ma/candidature")
        self.assertTrue(fields["job_application_is_official_channel"])
        self.assertEqual(len(fields["job_document_links"]), 2)

    def test_extractor_accepts_text_only_official_application_channel(self):
        html = """
        <html><body>
          <p>Nom du poste : Recrutement administrateur 2ème grade</p>
          <p>Type de dépôt : dépôt en ligne sur le site de l'administration</p>
          <p>Site de dépôt : https://recrutement.enssup.gov.ma/</p>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "Emploi-Public — établissements publics",
                "official_source": True,
                "source_country": "MA",
                "source_eligibility": "morocco",
                "ats_provider": "emploi_public",
                "title": "Recrutement administrateur 2ème grade",
            },
            (
                "https://www.emploi-public.ma/fr/concours/details/"
                "e33a1e44-b745-40d8-b32e-7db723c10c27"
            ),
            full_text=soup.get_text(" ", strip=True),
        )
        self.assertEqual(fields["job_notice_type"], "competition")
        self.assertEqual(
            fields["job_application_url"],
            "https://recrutement.enssup.gov.ma/",
        )
        self.assertEqual(
            fields["job_application_link_kind"],
            "official_application_channel",
        )
        self.assertTrue(fields["job_application_is_official_channel"])
        self.assertTrue(any(
            row.get("kind") == "apply"
            and row.get("url") == "https://recrutement.enssup.gov.ma/"
            for row in fields["job_action_links"]
        ))

    def test_workday_discovery_identity_uses_specific_url_not_bullet_field(self):
        first = {
            "url": "https://tenant.wd5.myworkdayjobs.com/site/job/one",
            "ats_provider": "workday",
            "ats_reference": "Casablanca",
        }
        second = {
            "url": "https://tenant.wd5.myworkdayjobs.com/site/job/two",
            "ats_provider": "workday",
            "ats_reference": "Casablanca",
        }
        self.assertNotEqual(
            scraper._discovery_identity(first),
            scraper._discovery_identity(second),
        )
        self.assertTrue(
            scraper._discovery_identity(first).startswith("url:")
        )

    def test_generic_tuple_discovery_identity_is_supported(self):
        identity = scraper._discovery_identity(
            ("Network Engineer", "https://jobs.example.com/jobs/123")
        )
        self.assertEqual(
            identity,
            "url:https://jobs.example.com/jobs/123",
        )

    def test_jobs_zero_new_links_does_not_cool_down_healthy_source(self):
        with (
            patch.object(scraper, "JOBS_MODE", True),
            patch.object(scraper, "record_source_success") as success,
            patch.object(scraper, "record_source_failure") as failure,
            patch.object(scraper, "record_source_cooldown") as cooldown,
        ):
            scraper._record_source_result(
                "https://jobs.example/list",
                "Quiet official source",
                "",
                0,
            )

        success.assert_called_once()
        failure.assert_not_called()
        cooldown.assert_not_called()

    def test_discovery_seen_streak_stops_after_known_run(self):
        known = {
            f"url:https://example.com/jobs/known-{index}"
            for index in range(1, 9)
        }
        links = [
            {
                "title": "Fresh role",
                "url": "https://example.com/jobs/fresh",
                "ats_provider": "workday",
                "ats_reference": "fresh-1",
            },
            *[
                {
                    "title": f"Known role {index}",
                    "url": f"https://example.com/jobs/known-{index}",
                    "ats_provider": "workday",
                    "ats_reference": f"known-{index}",
                }
                for index in range(1, 10)
            ],
            {
                "title": "Buried role",
                "url": "https://example.com/jobs/buried",
                "ats_provider": "workday",
                "ats_reference": "buried-1",
            },
        ]
        selected, meta = scraper._filter_new_discovery_links(
            links,
            known,
            seen_streak_stop=8,
            max_items=100,
        )
        self.assertEqual(
            [row["ats_reference"] for row in selected],
            ["fresh-1"],
        )
        self.assertEqual(meta["stop_reason"], "seen_streak")
        self.assertEqual(meta["seen_streak"], 8)

    def test_inwi_parser_keeps_only_real_job_details(self):
        html = """
        <html><body>
          <div class="job-card">
            <a href="/jobs/8479198-responsable-experience-client">
              Responsable Expérience Client
            </a>
          </div>
          <a href="/jobs/show_more?page=2">Afficher 20 de plus</a>
          <a href="/jobs">Offres d'emploi</a>
          <a href="/locations/casablanca">Casablanca</a>
        </body></html>
        """
        links = scraper._parse_inwi_job_links(
            html,
            "https://jobs.inwi.ma/jobs",
            per_source_limit=20,
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0]["url"],
            "https://jobs.inwi.ma/jobs/8479198-responsable-experience-client",
        )
        self.assertEqual(links[0]["ats_provider"], "teamtailor")
        self.assertEqual(links[0]["ats_reference"], "8479198")

    def test_inwi_load_more_is_pagination_not_a_job(self):
        html = """
        <html><body>
          <a href="/jobs/8479198-responsable-experience-client">Role</a>
          <a href="/jobs/show_more?page=2">Afficher 20 de plus</a>
        </body></html>
        """
        self.assertEqual(
            scraper._pagination_next_url(html, "https://jobs.inwi.ma/jobs"),
            "https://jobs.inwi.ma/jobs/show_more?page=2",
        )
        links = scraper._parse_inwi_job_links(
            html,
            "https://jobs.inwi.ma/jobs",
            per_source_limit=20,
        )
        self.assertNotIn(
            "https://jobs.inwi.ma/jobs/show_more?page=2",
            [row["url"] for row in links],
        )

    def test_credit_du_maroc_parser_rejects_navigation_and_captures_listing_facts(self):
        html = """
        <html><body>
          <div class="offer">
            <h3>
              <a href="/offre-de-emploi/emploi-conseiller-clientele-particuliers-polyvalent-h-f_3331.aspx">
                Conseiller Clientèle Particuliers Polyvalent H/F
              </a>
            </h3>
            <span>Réf. : 2026-3331</span>
            <span>29/09/2026</span>
          </div>
          <a href="/my-account/log-in.aspx">Connexion</a>
          <a href="/mon-compte/retrouver-mon-mot-de-passe.aspx">Mot de passe perdu</a>
          <a href="/offre-de-emploi/tous-les-flux-rss.aspx">Flux RSS</a>
          <a href="/mention-legale.aspx">Mentions légales</a>
          <a href="/offre-de-emploi/liste-toutes-offres.aspx">Nos offres d'emploi</a>
          <a href="/offre-de-emploi/ma-selection-offres.aspx">Ma sélection d'offres</a>
        </body></html>
        """
        links = scraper._parse_credit_du_maroc_job_links(
            html,
            "https://carriere.creditdumaroc.ma/offre-de-emploi/liste-offres.aspx?showSearchUrl=1",
            per_source_limit=20,
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["ats_reference"], "2026-3331")
        self.assertEqual(links[0]["source_published_at"], "2026-09-29T00:00:00Z")
        self.assertEqual(links[0]["published_at_source"], "official_listing")
        self.assertTrue(
            links[0]["url"].endswith(
                "/offre-de-emploi/emploi-conseiller-clientele-particuliers-polyvalent-h-f_3331.aspx"
            )
        )

    def test_html_pagination_follows_only_real_next_link(self):
        first = """
        <html><body>
          <a class="job" href="/jobs/1">One</a>
          <a rel="next" href="/jobs?page=2">Next</a>
        </body></html>
        """
        second = """
        <html><body>
          <a class="job" href="/jobs/2">Two</a>
          <a href="https://evil.example/jobs?page=3">Next page</a>
        </body></html>
        """
        responses = {
            "https://example.com/jobs": (first, "", 200),
            "https://example.com/jobs?page=2": (second, "", 200),
        }

        async def fake_fetch(_session, url):
            return responses[url]

        def parser(html_text, current_url, per_source_limit=None):
            soup = BeautifulSoup(html_text, "html.parser")
            return [
                {
                    "title": anchor.get_text(" ", strip=True),
                    "url": __import__(
                        "urllib.parse",
                        fromlist=["urljoin"],
                    ).urljoin(current_url, anchor["href"]),
                }
                for anchor in soup.select("a.job[href]")
            ]

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, status, meta = asyncio.run(
                scraper._collect_paginated_html_links_async(
                    object(),
                    "https://example.com/jobs",
                    parser,
                    known_ids=set(),
                    max_pages=5,
                    seen_streak_stop=8,
                    max_items=50,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["resume_url"], "")
        self.assertEqual(
            [row["url"] for row in links],
            [
                "https://example.com/jobs/1",
                "https://example.com/jobs/2",
            ],
        )

    def test_html_pagination_follows_official_load_more_link(self):
        first = """
        <html><body>
          <a class="job" href="/jobs/1">One</a>
          <a href="/ar/loadCardsConcours?page=2">أظهر المزيد</a>
        </body></html>
        """
        second = """
        <html><body>
          <a class="job" href="/jobs/2">Two</a>
          <a href="/ar/loadCardsConcours?page=3" aria-label="عرض المزيد"></a>
        </body></html>
        """
        third = """
        <html><body>
          <a class="job" href="/jobs/3">Three</a>
          <a href="https://evil.example/loadCardsConcours?page=4">أظهر المزيد</a>
        </body></html>
        """
        responses = {
            "https://example.com/jobs": (first, "", 200),
            "https://example.com/ar/loadCardsConcours?page=2": (second, "", 200),
            "https://example.com/ar/loadCardsConcours?page=3": (third, "", 200),
        }

        async def fake_fetch(_session, url):
            return responses[url]

        def parser(html_text, current_url, per_source_limit=None):
            soup = BeautifulSoup(html_text, "html.parser")
            return [
                {
                    "title": anchor.get_text(" ", strip=True),
                    "url": __import__(
                        "urllib.parse",
                        fromlist=["urljoin"],
                    ).urljoin(current_url, anchor["href"]),
                }
                for anchor in soup.select("a.job[href]")
            ]

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, status, meta = asyncio.run(
                scraper._collect_paginated_html_links_async(
                    object(),
                    "https://example.com/jobs",
                    parser,
                    known_ids=set(),
                    page_size=8,
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=50,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["pages_scanned"], 3)
        self.assertEqual(
            [row["url"] for row in links],
            [
                "https://example.com/jobs/1",
                "https://example.com/jobs/2",
                "https://example.com/jobs/3",
            ],
        )

    def test_emploi_public_load_more_collects_beyond_fetch_hint(self):
        def page(start, count, next_page=None):
            cards = []
            for index in range(start, start + count):
                ref = f"00000000-0000-0000-0000-{index:012d}"
                cards.append(
                    f"<div><h3>مباراة توظيف تقني رقم {index}</h3>"
                    f"<a href='/ar/تفاصيل/المباريات/{ref}'>التفاصيل</a></div>"
                )
            more = (
                f"<a href='/ar/loadCardsConcours?page={next_page}'>أظهر المزيد</a>"
                if next_page
                else ""
            )
            return "<html><body>" + "".join(cards) + more + "</body></html>"

        responses = {
            "https://www.emploi-public.ma/ar/list": (page(1, 10, 2), "", 200),
            "https://www.emploi-public.ma/ar/loadCardsConcours?page=2": (
                page(11, 10, 3), "", 200
            ),
            "https://www.emploi-public.ma/ar/loadCardsConcours?page=3": (
                page(21, 5), "", 200
            ),
        }

        async def fake_fetch(_session, url):
            return responses[url]

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, status, meta = asyncio.run(
                scraper._collect_paginated_html_links_async(
                    object(),
                    "https://www.emploi-public.ma/ar/list",
                    scraper._parse_emploi_public_links,
                    known_ids=set(),
                    page_size=8,
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=100,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(links), 25)
        self.assertEqual(meta["pages_scanned"], 3)
        self.assertEqual(meta["stop_reason"], "end")
        self.assertTrue(
            links[-1]["ats_reference"].endswith("000000000025")
        )

    def test_workday_pagination_collects_beyond_first_eight(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status = 200
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, errors="ignore"):
                return json.dumps(self._payload)

        class FakeSession:
            def __init__(self):
                self.offsets = []

            def post(self, _url, json=None, headers=None, timeout=None):
                offset = int((json or {}).get("offset") or 0)
                limit = int((json or {}).get("limit") or 20)
                self.offsets.append(offset)
                rows = []
                for index in range(offset, min(offset + limit, 45)):
                    rows.append({
                        "title": f"Role {index + 1}",
                        "externalPath": f"/job/{index + 1}",
                        "bulletFields": [f"REQ-{index + 1}"],
                        "postedOn": "Posted Today",
                    })
                return FakeResponse({
                    "jobPostings": rows,
                    "total": 45,
                })

        session = FakeSession()
        links, error, status, meta = asyncio.run(
            scraper._collect_workday_links_async(
                session,
                "https://tenant.wd5.myworkdayjobs.com/site",
                per_source_limit=8,
                known_ids=set(),
                max_pages=10,
                seen_streak_stop=8,
                max_items=100,
            )
        )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(links), 45)
        self.assertEqual(session.offsets, [0, 20, 40])
        self.assertEqual(links[-1]["ats_reference"], "REQ-45")
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["resume_offset"], 0)

    def test_workday_resume_cursor_reaches_jobs_beyond_safety_cap(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status = 200
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, errors="ignore"):
                return json.dumps(self._payload)

        class FakeSession:
            def __init__(self):
                self.offsets = []

            def post(self, _url, json=None, headers=None, timeout=None):
                offset = int((json or {}).get("offset") or 0)
                limit = int((json or {}).get("limit") or 20)
                self.offsets.append(offset)
                rows = [
                    {
                        "title": f"Role {index + 1}",
                        "externalPath": f"/job/{index + 1}",
                        "bulletFields": [f"REQ-{index + 1}"],
                    }
                    for index in range(offset, min(offset + limit, 70))
                ]
                return FakeResponse({"jobPostings": rows, "total": 70})

        first_session = FakeSession()
        first_links, first_error, _status, first_meta = asyncio.run(
            scraper._collect_workday_links_async(
                first_session,
                "https://tenant.wd5.myworkdayjobs.com/site",
                per_source_limit=8,
                known_ids=set(),
                max_pages=10,
                seen_streak_stop=8,
                max_items=40,
            )
        )
        self.assertEqual(first_error, "")
        self.assertEqual(len(first_links), 40)
        self.assertEqual(first_meta["stop_reason"], "max_items")
        self.assertEqual(first_meta["resume_offset"], 40)

        known = {
            scraper._discovery_identity(row)
            for row in first_links
        }
        second_session = FakeSession()
        second_links, second_error, _status, second_meta = asyncio.run(
            scraper._collect_workday_links_async(
                second_session,
                "https://tenant.wd5.myworkdayjobs.com/site",
                per_source_limit=8,
                known_ids=known,
                max_pages=10,
                seen_streak_stop=8,
                max_items=40,
                start_offset=first_meta["resume_offset"],
            )
        )
        self.assertEqual(second_error, "")
        self.assertEqual(second_session.offsets, [40, 60])
        self.assertEqual(len(second_links), 30)
        self.assertEqual(second_links[0]["ats_reference"], "REQ-41")
        self.assertEqual(second_links[-1]["ats_reference"], "REQ-70")
        self.assertEqual(second_meta["stop_reason"], "end")
        self.assertEqual(second_meta["resume_offset"], 0)

    def test_workday_resume_continues_through_known_overlap(self):
        class FakeResponse:
            def __init__(self, payload):
                self.status = 200
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, errors="ignore"):
                return json.dumps(self._payload)

        class FakeSession:
            def __init__(self):
                self.offsets = []

            def post(self, _url, json=None, headers=None, timeout=None):
                offset = int((json or {}).get("offset") or 0)
                self.offsets.append(offset)
                if offset == 40:
                    rows = [
                        {
                            "title": f"Known shifted role {index}",
                            "externalPath": f"/job/{index}",
                            "bulletFields": ["Casablanca"],
                        }
                        for index in range(1, 21)
                    ]
                    return FakeResponse({"jobPostings": rows, "total": 65})
                rows = [
                    {
                        "title": f"Fresh deep role {index}",
                        "externalPath": f"/job/{index}",
                        "bulletFields": ["Casablanca"],
                    }
                    for index in range(61, 66)
                ]
                return FakeResponse({"jobPostings": rows, "total": 65})

        known = {
            f"url:https://tenant.wd5.myworkdayjobs.com/site/job/{index}"
            for index in range(1, 21)
        }
        session = FakeSession()
        links, error, status, meta = asyncio.run(
            scraper._collect_workday_links_async(
                session,
                "https://tenant.wd5.myworkdayjobs.com/site",
                per_source_limit=8,
                known_ids=known,
                max_pages=5,
                seen_streak_stop=8,
                max_items=100,
                start_offset=40,
            )
        )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(session.offsets, [40, 60])
        self.assertEqual(
            [row["url"] for row in links],
            [
                f"https://tenant.wd5.myworkdayjobs.com/site/job/{index}"
                for index in range(61, 66)
            ],
        )
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["resume_offset"], 0)

    def test_workday_seen_streak_stops_before_later_pages(self):
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, errors="ignore"):
                return json.dumps({
                    "jobPostings": [
                        {
                            "title": f"Known {index}",
                            "externalPath": f"/job/{index}",
                            "bulletFields": [f"REQ-{index}"],
                        }
                        for index in range(1, 21)
                    ],
                    "total": 100,
                })

        class FakeSession:
            def __init__(self):
                self.calls = 0

            def post(self, _url, json=None, headers=None, timeout=None):
                self.calls += 1
                return FakeResponse()

        known = {
            f"url:https://tenant.wd5.myworkdayjobs.com/site/job/{index}"
            for index in range(1, 9)
        }
        session = FakeSession()
        links, error, status, meta = asyncio.run(
            scraper._collect_workday_links_async(
                session,
                "https://tenant.wd5.myworkdayjobs.com/site",
                per_source_limit=8,
                known_ids=known,
                max_pages=10,
                seen_streak_stop=8,
                max_items=100,
            )
        )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(links, [])
        self.assertEqual(session.calls, 1)
        self.assertEqual(meta["stop_reason"], "seen_streak")
        self.assertEqual(meta["resume_offset"], 0)

    def test_jobs_top_level_discovery_does_not_truncate_to_fetch_limit(self):
        source = {
            "name": "Official Bulk Source",
            "base_url": "https://jobs.example.com/openings",
            "enabled": True,
            "fetch_limit_per_run": 8,
            "extractor_type": "auto",
            "official_source": True,
            "source_country": "MA",
            "source_eligibility": "morocco",
        }
        links = [
            {
                "title": f"Role {index}",
                "url": f"https://jobs.example.com/openings/{index}",
                "ats_provider": "example",
                "ats_reference": f"REQ-{index}",
            }
            for index in range(1, 51)
        ]

        with (
            patch.object(scraper, "JOBS_MODE", True),
            patch.object(scraper, "_can_run_async_discovery", return_value=False),
            patch.object(scraper, "_filter_healthy_sources", side_effect=lambda rows: (rows, [])),
            patch.object(scraper, "_order_sources_for_fast_run", side_effect=lambda rows: rows),
            patch.object(
                scraper,
                "_collect_article_links_for_source",
                return_value=(
                    links,
                    "",
                    200,
                    {
                        "normal_links_found": 50,
                        "feed_links_found": 0,
                        "method_used": "html-pagination",
                        "tried_feed_urls": [],
                        "discovery_meta": {
                            "stop_reason": "end",
                            "pages_scanned": 5,
                        },
                        "discovery_resume": {},
                    },
                ),
            ) as collect,
            patch.object(scraper, "source_crawl_record", return_value={"job_seen_ids": []}),
            patch.object(scraper, "update_source_crawl") as update_crawl,
            patch.object(scraper, "_record_source_result"),
        ):
            result = scraper.discover_latest_article_links([source])

        self.assertEqual(len(result["articles"]), 50)
        self.assertEqual(result["source_results"][0]["fetch_limit_per_run"], 8)
        self.assertEqual(result["source_results"][0]["links_found"], 50)
        self.assertEqual(
            result["source_results"][0]["discovery_mode"],
            "paginated_seen_ids",
        )
        self.assertEqual(
            collect.call_args.kwargs["max_items"],
            scraper.JOBS_DISCOVERY_MAX_ITEMS_PER_SOURCE,
        )
        update_kwargs = update_crawl.call_args.kwargs
        self.assertEqual(len(update_kwargs["job_seen_ids"]), 50)
        self.assertEqual(update_kwargs["discovery_last_new_count"], 50)

    def test_jobs_discovery_reuses_persisted_resume_cursor(self):
        source = {
            "name": "Official Cursor Source",
            "base_url": "https://jobs.example.com/openings",
            "enabled": True,
            "fetch_limit_per_run": 8,
            "extractor_type": "auto",
            "official_source": True,
            "source_country": "MA",
            "source_eligibility": "morocco",
        }
        resume = {
            "kind": "html",
            "url": "https://jobs.example.com/openings?page=4",
        }
        crawl_record = {
            "job_seen_ids": ["example:req-1"],
            "job_discovery_resume": resume,
        }

        with (
            patch.object(scraper, "JOBS_MODE", True),
            patch.object(scraper, "_can_run_async_discovery", return_value=False),
            patch.object(scraper, "_filter_healthy_sources", side_effect=lambda rows: (rows, [])),
            patch.object(scraper, "_order_sources_for_fast_run", side_effect=lambda rows: rows),
            patch.object(scraper, "source_crawl_record", return_value=crawl_record),
            patch.object(
                scraper,
                "_collect_article_links_for_source",
                return_value=(
                    [],
                    "",
                    200,
                    {
                        "normal_links_found": 0,
                        "feed_links_found": 0,
                        "method_used": "html-pagination",
                        "tried_feed_urls": [],
                        "discovery_meta": {
                            "stop_reason": "end",
                            "pages_scanned": 1,
                        },
                        "discovery_resume": {},
                        "empty_ok": True,
                    },
                ),
            ) as collect,
            patch.object(scraper, "update_source_crawl"),
            patch.object(scraper, "_record_source_result"),
        ):
            scraper.discover_latest_article_links([source])

        self.assertEqual(collect.call_args.kwargs["resume_state"], resume)
        self.assertEqual(
            collect.call_args.kwargs["known_ids"],
            {"example:req-1"},
        )

    def test_emploi_public_parser_captures_official_listing_deadline(self):
        html = """
        <html><body>
          <div class="competition-card">
            <a href="/ar/تفاصيل/المباريات/a25bed63-cd17-4f71-abb9-60827ddb43c0">
              مباراة لتوظيف مهندس دولة من الدرجة الأولى - سلم 11
            </a>
            <span>الإعلان</span>
            <span>2 مناصب</span>
            <span>آخر أجل لإيداع ملفات الترشيح : 27 شتنبر 2026</span>
          </div>
        </body></html>
        """
        links = scraper._parse_emploi_public_links(
            html,
            "https://www.emploi-public.ma/ar/قائمة-المباريات?stat=service_etat",
            per_source_limit=20,
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["job_deadline"], "2026-09-27")

    def test_emploi_public_parser_keeps_only_competition_details(self):
        html = """
        <html><body>
          <a href="/ar/المترشح/تسجيل">إنشاء حساب</a>
          <a href="/fr/concours-liste">Français</a>
          <a href="/ar/قائمة-المباريات">مباريات التوظيف</a>
          <a href="/ar/تفاصيل/المباريات/3ca5d2b5-8e32-4de2-b0f7-4596d229daa2">
            متصرف من الدرجة الثانية - تخصص المالية والمحاسبة
          </a>
          <a href="/fr/concours/details/abc12345">
            Administrateur 2ème grade
          </a>
        </body></html>
        """
        links = scraper._parse_emploi_public_links(
            html,
            "https://www.emploi-public.ma/ar/قائمة-المباريات",
            per_source_limit=10,
        )
        self.assertEqual(len(links), 2)
        self.assertTrue(all(
            "/details/" in row["url"]
            or "تفاصيل" in __import__("urllib.parse", fromlist=["unquote"]).unquote(row["url"])
            for row in links
        ))
        self.assertTrue(all(row["ats_provider"] == "emploi_public" for row in links))
        self.assertFalse(any("تسجيل" in row["url"] for row in links))

    def test_capgemini_parser_accepts_only_official_job_details(self):
        html = """
        <html><body>
          <a href="/search/?q=&locationsearch=Morocco">Search jobs</a>
          <div><a href="/job/Casablanca-Software-Engineer/1441884333/">Software Engineer</a></div>
          <a href="/profile/">View profile</a>
        </body></html>
        """
        links = scraper._parse_capgemini_job_links(
            html,
            "https://careers.capgemini.com/search/?q=&locationsearch=Morocco",
            per_source_limit=5,
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0]["url"],
            "https://careers.capgemini.com/job/Casablanca-Software-Engineer/1441884333/",
        )
        self.assertEqual(links[0]["ats_provider"], "capgemini_successfactors")
        self.assertEqual(links[0]["ats_reference"], "1441884333")

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

    def test_phenom_pagination_collects_beyond_first_page(self):
        def page_html(start, count):
            rows = [
                {
                    "jobId": f"JOB-{index}",
                    "jobSeqNo": f"SEQ-{index}",
                    "title": f"Orange Role {index}",
                    "applyUrl": f"https://orange.jobs/apply?jobSeqNo=SEQ-{index}",
                    "country": "MOROCCO",
                    "location": "Casablanca, Morocco",
                }
                for index in range(start, start + count)
            ]
            payload = {"search": {"results": rows}}
            return (
                "<html><body><script>"
                + "phApp.ddo = "
                + json.dumps(payload)
                + ";</script></body></html>"
            )

        responses = {
            "https://orange.jobs/fr/fr/mea-morocco-job-search-results": (
                page_html(1, 10), "", 200
            ),
            "https://orange.jobs/fr/fr/mea-morocco-job-search-results?from=10": (
                page_html(11, 10), "", 200
            ),
            "https://orange.jobs/fr/fr/mea-morocco-job-search-results?from=20": (
                page_html(21, 5), "", 200
            ),
        }

        async def fake_fetch(_session, url):
            return responses[url]

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, status, meta = asyncio.run(
                scraper._collect_phenom_links_async(
                    object(),
                    "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
                    per_source_limit=8,
                    known_ids=set(),
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=100,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(len(links), 25)
        self.assertEqual(links[0]["ats_reference"], "JOB-1")
        self.assertEqual(links[-1]["ats_reference"], "JOB-25")
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["resume_offset"], 0)
        self.assertEqual(meta["pages_scanned"], 3)
        self.assertEqual(meta["page_size"], 10)

    def test_phenom_resume_cursor_reaches_jobs_beyond_safety_cap(self):
        def page_html(start, count):
            rows = [
                {
                    "jobId": f"JOB-{index}",
                    "jobSeqNo": f"SEQ-{index}",
                    "title": f"Orange Role {index}",
                    "applyUrl": f"https://orange.jobs/apply?jobSeqNo=SEQ-{index}",
                    "country": "MOROCCO",
                    "location": "Rabat, Morocco",
                }
                for index in range(start, start + count)
            ]
            return (
                "<script>phApp.ddo = "
                + json.dumps({"search": {"results": rows}})
                + ";</script>"
            )

        all_pages = {
            0: page_html(1, 10),
            10: page_html(11, 10),
            20: page_html(21, 10),
            30: page_html(31, 10),
            40: page_html(41, 10),
            50: page_html(51, 5),
        }

        async def fake_fetch(_session, url):
            query = __import__(
                "urllib.parse",
                fromlist=["urlparse", "parse_qs"],
            )
            parsed = query.urlparse(url)
            offset = int((query.parse_qs(parsed.query).get("from") or [0])[0])
            return all_pages[offset], "", 200

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            first, error, _status, first_meta = asyncio.run(
                scraper._collect_phenom_links_async(
                    object(),
                    "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
                    per_source_limit=8,
                    known_ids=set(),
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=30,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(len(first), 30)
        self.assertEqual(first_meta["stop_reason"], "max_items")
        self.assertEqual(first_meta["resume_offset"], 30)

        known = {scraper._discovery_identity(row) for row in first}
        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            second, error, _status, second_meta = asyncio.run(
                scraper._collect_phenom_links_async(
                    object(),
                    "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
                    per_source_limit=8,
                    known_ids=known,
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=30,
                    start_offset=first_meta["resume_offset"],
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(len(second), 25)
        self.assertEqual(second[0]["ats_reference"], "JOB-31")
        self.assertEqual(second[-1]["ats_reference"], "JOB-55")
        self.assertEqual(second_meta["stop_reason"], "end")
        self.assertEqual(second_meta["resume_offset"], 0)

    def test_phenom_resume_continues_through_known_overlap(self):
        def page_html(rows):
            return (
                "<script>phApp.ddo = "
                + json.dumps({"search": {"results": rows}})
                + ";</script>"
            )

        known_rows = [
            {
                "jobId": f"KNOWN-{index}",
                "jobSeqNo": f"KNOWN-SEQ-{index}",
                "title": f"Known Orange Role {index}",
                "applyUrl": f"https://orange.jobs/apply?jobSeqNo=KNOWN-SEQ-{index}",
                "country": "MOROCCO",
            }
            for index in range(1, 11)
        ]
        fresh_rows = [
            {
                "jobId": f"FRESH-{index}",
                "jobSeqNo": f"FRESH-SEQ-{index}",
                "title": f"Fresh Orange Role {index}",
                "applyUrl": f"https://orange.jobs/apply?jobSeqNo=FRESH-SEQ-{index}",
                "country": "MOROCCO",
            }
            for index in range(1, 6)
        ]

        async def fake_fetch(_session, url):
            parsed = __import__(
                "urllib.parse",
                fromlist=["urlparse", "parse_qs"],
            )
            query = parsed.parse_qs(parsed.urlparse(url).query)
            offset = int((query.get("from") or [0])[0])
            if offset == 30:
                return page_html(known_rows), "", 200
            if offset == 40:
                return page_html(fresh_rows), "", 200
            raise AssertionError(f"unexpected offset {offset}")

        known = {
            f"phenom:known-{index}"
            for index in range(1, 11)
        }
        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, status, meta = asyncio.run(
                scraper._collect_phenom_links_async(
                    object(),
                    "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
                    per_source_limit=8,
                    known_ids=known,
                    max_pages=5,
                    seen_streak_stop=8,
                    max_items=100,
                    start_offset=30,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["ats_reference"] for row in links],
            [f"FRESH-{index}" for index in range(1, 6)],
        )
        self.assertEqual(meta["stop_reason"], "end")
        self.assertEqual(meta["resume_offset"], 0)
        self.assertEqual(meta["pages_scanned"], 2)

    def test_phenom_page_repeat_stops_if_offset_is_ignored(self):
        rows = [
            {
                "jobId": f"JOB-{index}",
                "jobSeqNo": f"SEQ-{index}",
                "title": f"Orange Role {index}",
                "applyUrl": f"https://orange.jobs/apply?jobSeqNo=SEQ-{index}",
                "country": "MOROCCO",
            }
            for index in range(1, 11)
        ]
        html = "<script>phApp.ddo = " + json.dumps({"rows": rows}) + ";</script>"

        async def fake_fetch(_session, _url):
            return html, "", 200

        with patch.object(scraper, "_fetch_text_async", side_effect=fake_fetch):
            links, error, _status, meta = asyncio.run(
                scraper._collect_phenom_links_async(
                    object(),
                    "https://orange.jobs/fr/fr/mea-morocco-job-search-results",
                    per_source_limit=8,
                    known_ids=set(),
                    max_pages=10,
                    seen_streak_stop=8,
                    max_items=100,
                )
            )

        self.assertEqual(error, "")
        self.assertEqual(len(links), 10)
        self.assertEqual(meta["stop_reason"], "page_repeat")
        self.assertEqual(meta["resume_offset"], 0)
        self.assertEqual(meta["pages_scanned"], 2)

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

    def test_jobs_queue_refreshes_structured_ats_metadata_on_duplicate(self):
        existing = {
            "id": "old-orange",
            "url": "https://careers-orange.icims.com/jobs/28099/job/login",
            "status": "ready",
            "content_fetch_status": "failed",
            "content_fetch_error": "http 405",
            "candidate_retry_after": "2099-01-01T00:00:00Z",
            "identity_evidence_stage_status": "complete",
            "identity_evidence_signature": "stale-signature",
            "identity_evidence_strength": 6,
            "identity_evidence_comparison_strength": 4,
        }
        discovered = {
            "ats_provider": "phenom",
            "ats_reference": "ICM-588622",
            "ats_description": "Structured Orange job description.",
            "job_application_url": "https://careers-orange.icims.com/jobs/28099/job/login",
            "job_application_link_kind": "direct_apply",
            "job_location": "CASABLANCA,MOROCCO",
            "job_country": "MA",
            "job_contract_type": "CDI",
            "job_company": "Orange Business",
            "phenom_payload": {"country": "MOROCCO"},
        }
        changed = article_queue._merge_job_discovery_metadata(existing, discovered)
        self.assertTrue(changed)
        self.assertEqual(existing["ats_provider"], "phenom")
        self.assertEqual(existing["ats_reference"], "ICM-588622")
        self.assertEqual(existing["job_application_link_kind"], "direct_apply")
        self.assertEqual(existing["job_country"], "MA")
        self.assertNotIn("candidate_retry_after", existing)
        self.assertNotIn("content_fetch_status", existing)
        self.assertNotIn("content_fetch_error", existing)
        self.assertNotIn("identity_evidence_stage_status", existing)
        self.assertNotIn("identity_evidence_signature", existing)
        self.assertEqual(
            existing["identity_evidence_invalidation_reason"],
            "structured discovery identity facts changed",
        )

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

    def test_low_ranking_score_does_not_block_verified_job(self):
        now = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
        sparse = sample_job(
            job_title="Chargé administratif",
            official_source=False,
            job_location="",
            job_number_of_positions=1,
            job_published_at="2026-09-30T08:00:00+00:00",
            source_priority="",
            job_diploma="",
            job_salary="",
            job_entry_level=False,
            job_deadline="2026-10-08",
        )
        result = job_core.score_job(sparse, now=now)
        self.assertLess(result["score"], job_core.MIN_SELECTION_SCORE)
        self.assertTrue(result["hard_gate_passed"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "publish")
        self.assertEqual(result["threshold_applies_to"], "ranking_only")

    def test_job_between_twelve_and_twenty_four_hours_remains_publishable(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        result = job_core.score_job(
            sample_job(
                job_published_at="2026-09-29T22:59:00+00:00",
                source_published_at="2026-09-29T22:59:00+00:00",
            ),
            now=now,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "publish")
        self.assertGreater(result["publication_age_hours"], 12)
        self.assertLess(result["publication_age_hours"], 24)
        self.assertEqual(result["freshness"]["preferred_rank"], 2)
        self.assertEqual(result["max_publish_age_hours"], 24)

    def test_job_older_than_twenty_four_hours_is_rejected(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        result = job_core.score_job(
            sample_job(
                job_published_at="2026-09-29T10:00:00+00:00",
                source_published_at="2026-09-29T10:00:00+00:00",
            ),
            now=now,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "reject")
        self.assertEqual(result["freshness"]["bucket"], "too_old")
        self.assertTrue(any("older than 24h" in reason for reason in result["reasons"]))

    def test_job_without_verified_publication_time_cannot_publish(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        result = job_core.score_job(
            sample_job(
                job_published_at="",
                source_published_at="",
                discovered_at="2026-09-30T11:00:00+00:00",
            ),
            now=now,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "queue")
        self.assertIsNone(result["publication_age_hours"])
        self.assertEqual(result["freshness"]["bucket"], "unknown")
        self.assertIn("publication time is not verified", result["reasons"])

    def test_job_at_exact_twelve_hour_boundary_is_preferred(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        result = job_core.score_job(
            sample_job(
                job_published_at="2026-09-30T00:00:00+00:00",
                source_published_at="2026-09-30T00:00:00+00:00",
            ),
            now=now,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["preferred_fresh_hours"], 12)
        self.assertEqual(result["max_publish_age_hours"], 24)
        self.assertEqual(result["publication_age_hours"], 12.0)
        self.assertEqual(result["freshness"]["preferred_rank"], 3)

    def test_workday_relative_publication_age_respects_twelve_and_twenty_four_hours(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        recent = job_core.score_job(
            sample_job(
                job_published_at="",
                source_published_at="",
                source_published_label="Posted 4 Hours Ago",
            ),
            now=now,
        )
        self.assertTrue(recent["passed"])
        self.assertEqual(recent["freshness"]["preferred_rank"], 3)
        self.assertEqual(recent["publication_age_hours"], 4.0)

        boundary = job_core.score_job(
            sample_job(
                job_published_at="",
                source_published_at="",
                source_published_label="Posted 20 Hours Ago",
            ),
            now=now,
        )
        self.assertTrue(boundary["passed"])
        self.assertEqual(boundary["freshness"]["preferred_rank"], 2)

        stale = job_core.score_job(
            sample_job(
                job_published_at="",
                source_published_at="",
                source_published_label="Posted 2 Days Ago",
            ),
            now=now,
        )
        self.assertFalse(stale["passed"])
        self.assertEqual(stale["status"], "reject")

    def test_focus_priority_prefers_technical_student_and_arabic_roles(self):
        cyber = sample_job(job_title="Cybersecurity SOC Analyst")
        developer = sample_job(job_title="Développeur Backend Python")
        internship = sample_job(job_title="Stage PFE Data Engineering", job_entry_level=True)
        arabic_cyber = sample_job(job_title="محلل الأمن السيبراني")
        arabic_dev = sample_job(job_title="مطور برمجيات")
        arabic_stage = sample_job(job_title="تدريب في تقنية المعلومات")
        general = sample_job(job_title="Chargé de clientèle")

        for row in (cyber, developer, internship, arabic_cyber, arabic_dev, arabic_stage):
            self.assertGreater(
                job_core.job_focus_priority(row),
                job_core.job_focus_priority(general),
            )

    def test_jobs_queue_prefers_newer_job_before_older_technical_role(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        general = sample_job(
            id="general-newer",
            job_title="Chargé de clientèle",
            status="ready",
            content_fetch_status="success",
            job_published_at="2026-09-30T11:30:00+00:00",
            source_published_at="2026-09-30T11:30:00+00:00",
            job_urgency={"level": "normal"},
        )
        technical = sample_job(
            id="technical-older",
            job_title="Analyste Cybersécurité SOC",
            status="ready",
            content_fetch_status="success",
            job_published_at="2026-09-30T08:00:00+00:00",
            source_published_at="2026-09-30T08:00:00+00:00",
            job_urgency={"level": "normal"},
        )

        def fake_prepare(article, now=None):
            return (
                {"score": 70, "status": "publish", "passed": True, "reasons": []},
                {"action": "new", "reason": "new verified job", "existing": {}},
            )

        with (
            patch.object(job_core, "prepare_job_candidate", side_effect=fake_prepare),
            patch.object(job_core, "can_publish_new_job", return_value=True),
        ):
            selected = job_core.select_best_job_from_queue(
                {"articles": [general, technical]},
                now=now,
            )

        self.assertEqual(selected["id"], "general-newer")

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

    def test_job_quality_gate_allows_complete_length_and_rejects_duplicate_links(self):
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
            "<p>" + " ".join(f"تفصيل{i}" for i in range(330)) + "</p>"
            "<h2>طريقة التقديم</h2>"
            f"<p><a href='{apply_url}'>التقديم المباشر</a></p>"
        )
        longer_complete = dict(base_article, final_html=long_html)
        result = quality_gate.validate_before_publish(longer_complete, check_duplicate=False)
        self.assertTrue(result.passed, result.reason)

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


    def test_identity_hold_becomes_pending_not_skipped(self):
        row = sample_job(
            status="ready",
            content_fetch_status="success",
        )
        quality = {
            "score": 90,
            "status": "publish",
            "passed": True,
            "reasons": [],
        }
        decision = {
            "action": "hold",
            "reason": "ambiguous same role without strong identifier",
            "existing": {"campaign_id": "existing"},
        }
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        with patch.object(job_core, "prepare_job_candidate", return_value=(quality, decision)):
            selected = job_core.select_best_job_from_queue({"articles": [row]}, now=now)
        self.assertIsNone(selected)
        self.assertEqual(row["status"], "identity_pending")
        self.assertEqual(row["job_identity_action"], "hold")
        self.assertNotIn("skip_reason", row)
        self.assertEqual(row["identity_pending_evidence_status"], "awaiting_more_evidence")

    def test_jobs_queue_prefers_newer_verified_job_over_higher_score_old_job(self):
        now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        older = sample_job(
            id="older-high-score",
            status="ready",
            content_fetch_status="success",
            job_published_at="2026-09-23T08:00:00+00:00",
            source_published_at="2026-09-23T08:00:00+00:00",
            job_number_of_positions=1,
            job_urgency={"level": "normal"},
        )
        newer = sample_job(
            id="newer-lower-score",
            status="ready",
            content_fetch_status="success",
            job_published_at="2026-09-30T10:00:00+00:00",
            source_published_at="2026-09-30T10:00:00+00:00",
            job_number_of_positions=1,
            job_urgency={"level": "normal"},
        )

        def fake_prepare(article, now=None):
            score = 95 if article["id"] == "older-high-score" else 55
            return (
                {"score": score, "status": "publish", "passed": True, "reasons": []},
                {"action": "new", "reason": "new verified job", "existing": {}},
            )

        with (
            patch.object(job_core, "prepare_job_candidate", side_effect=fake_prepare),
            patch.object(job_core, "can_publish_new_job", return_value=True),
        ):
            selected = job_core.select_best_job_from_queue(
                {"articles": [older, newer]},
                now=now,
            )

        self.assertEqual(selected["id"], "newer-lower-score")


    def test_jobs_discovery_refreshes_head_before_resuming_deep_backlog(self):
        source = {
            "name": "Large official source",
            "base_url": "https://jobs.example/list",
            "enabled": True,
            "fetch_limit_per_run": 8,
            "extractor_type": "workday_api",
            "official_source": True,
        }
        crawl_record = {
            "job_seen_ids": ["source:known"],
            "job_discovery_resume": {"kind": "workday", "offset": 250},
        }
        calls = []

        def fake_collect(_base_url, **kwargs):
            calls.append(dict(kwargs))
            if not kwargs.get("resume_state"):
                return (
                    [{
                        "title": "Newest role",
                        "url": "https://jobs.example/job/newest",
                        "ats_provider": "source",
                        "ats_reference": "NEWEST",
                    }],
                    "",
                    200,
                    {
                        "discovery_resume": {},
                        "discovery_meta": {"stop_reason": "seen_streak", "pages_scanned": 1},
                    },
                )
            return (
                [{
                    "title": "Deep backlog role",
                    "url": "https://jobs.example/job/deep",
                    "ats_provider": "source",
                    "ats_reference": "DEEP",
                }],
                "",
                200,
                {
                    "discovery_resume": {"kind": "workday", "offset": 500},
                    "discovery_meta": {"stop_reason": "max_items", "pages_scanned": 12},
                },
            )

        with (
            patch.object(scraper, "JOBS_MODE", True),
            patch.object(scraper, "_can_run_async_discovery", return_value=False),
            patch.object(scraper, "source_crawl_record", return_value=crawl_record),
            patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect),
            patch.object(scraper, "_record_source_result"),
            patch.object(scraper, "update_source_crawl"),
        ):
            result = scraper.discover_latest_article_links([source])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["resume_state"], {})
        self.assertEqual(calls[1]["resume_state"], {"kind": "workday", "offset": 250})
        self.assertEqual(
            {row["url"] for row in result["articles"]},
            {"https://jobs.example/job/newest", "https://jobs.example/job/deep"},
        )
        self.assertEqual(
            result["source_results"][0]["resume_after"],
            {"kind": "workday", "offset": 500},
        )


    def test_jobs_discovery_rebases_resume_when_fresh_head_burst_is_not_exhausted(self):
        source = {
            "name": "Burst source",
            "base_url": "https://burst.example/jobs",
            "enabled": True,
            "fetch_limit_per_run": 8,
            "extractor_type": "workday_api",
        }
        crawl_record = {
            "job_seen_ids": ["source:known"],
            "job_discovery_resume": {"kind": "workday", "offset": 1000},
        }
        calls = []

        def fake_collect(_base_url, **kwargs):
            calls.append(dict(kwargs))
            return (
                [{
                    "title": "Fresh burst role",
                    "url": "https://burst.example/jobs/fresh",
                    "ats_provider": "source",
                    "ats_reference": "FRESH-BURST",
                }],
                "",
                200,
                {
                    "discovery_resume": {"kind": "workday", "offset": 250},
                    "discovery_meta": {"stop_reason": "max_items", "pages_scanned": 12},
                },
            )

        with (
            patch.object(scraper, "JOBS_MODE", True),
            patch.object(scraper, "_can_run_async_discovery", return_value=False),
            patch.object(scraper, "source_crawl_record", return_value=crawl_record),
            patch.object(scraper, "_collect_article_links_for_source", side_effect=fake_collect),
            patch.object(scraper, "_record_source_result"),
            patch.object(scraper, "update_source_crawl"),
        ):
            result = scraper.discover_latest_article_links([source])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resume_state"], {})
        self.assertTrue(result["source_results"][0]["head_refresh_rebased_resume"])
        self.assertEqual(
            result["source_results"][0]["resume_after"],
            {"kind": "workday", "offset": 250},
        )


    def test_confirmed_identity_duplicate_is_the_identity_terminal_skip(self):
        row = sample_job(
            status="ready",
            content_fetch_status="success",
        )
        quality = {
            "score": 90,
            "status": "publish",
            "passed": True,
            "reasons": [],
        }
        decision = {
            "action": "duplicate",
            "reason": "same external reference",
            "existing": {"campaign_id": "existing"},
        }
        with patch.object(job_core, "prepare_job_candidate", return_value=(quality, decision)):
            selected = job_core.select_best_job_from_queue({"articles": [row]})
        self.assertIsNone(selected)
        self.assertEqual(row["status"], "skipped")
        self.assertTrue(row["job_identity_final"])
        self.assertIn("duplicate confirmed", row["skip_reason"])

    def test_internal_queue_id_is_not_an_external_job_reference(self):
        row = sample_job(
            id="internal-queue-hash",
            job_external_reference="",
            raw={},
        )
        self.assertEqual(job_core.external_reference(row), "")

        source_row = sample_job(
            id="internal-queue-hash",
            job_external_reference="",
            raw={"id": "SOURCE-7788"},
        )
        self.assertEqual(job_core.external_reference(source_row), "SOURCE-7788")

    def test_ats_reference_is_strong_identity_evidence(self):
        row = sample_job(
            job_external_reference="",
            ats_reference="ICM-588622",
            raw={},
        )
        self.assertEqual(job_core.external_reference(row), "ICM-588622")

    def test_pdf_reference_can_become_identity_evidence(self):
        row = sample_job(
            job_external_reference="",
            raw={},
            job_document_texts=[
                {
                    "text": "Référence du concours : ENS-TECH-2026-17",
                    "page_number": 1,
                }
            ],
        )
        self.assertEqual(job_core.external_reference(row), "ENS-TECH-2026-17")

    def test_central_application_channel_is_not_identity_strong(self):
        row = sample_job(
            job_application_url="https://recrutement.enssup.gov.ma/",
            job_application_link_kind="official_application_channel",
        )
        self.assertEqual(
            job_core._identity_strong_application_url(
                row,
                row["job_application_url"],
            ),
            "",
        )

    def _complete_identity_evidence(self, article):
        article["content_fetch_status"] = "success"
        article["job_detail_url"] = article.get("job_detail_url") or article.get("url")
        article.setdefault("source_tables", [])
        article.setdefault("source_tables_count", len(article.get("source_tables") or []))
        article.setdefault("job_document_links", [])
        article["identity_evidence_document_fingerprint"] = "|".join(sorted(
            job_core.canonicalize_job_url(item.get("url"))
            for item in article.get("job_document_links") or []
            if isinstance(item, dict) and job_core.canonicalize_job_url(item.get("url"))
        ))
        article["job_document_text_download_failures"] = 0
        job_core.finalize_identity_evidence_stage(article)
        return article

    def test_same_external_reference_waits_for_evidence_before_duplicate(self):
        article = sample_job(
            job_external_reference="REF-2026-100",
            content_fetch_status="success",
        )
        record = {
            "campaign_id": "campaign-a",
            "identity_key": job_core.identity_key(article),
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "REF-2026-100",
            "deadline": article["job_deadline"],
            "number_of_positions": article["job_number_of_positions"],
            "published_at": article["job_published_at"],
            "application_url": article["job_application_url"],
            "location": article["job_location"],
            "notice_type": "vacancy",
            "notice_status": "",
            "document_urls": "",
        }
        with patch.object(job_core, "get_by_identity", return_value=record), \
             patch.object(job_core, "get_semantic_candidates", return_value=[]):
            pending = job_core.classify_identity(article)
        self.assertEqual(pending["action"], "hold")
        self.assertIn("awaiting evidence stage", pending["reason"])

        self._complete_identity_evidence(article)
        record["identity_evidence_signature"] = article["identity_evidence_signature"]
        record["identity_evidence_strength"] = article["identity_evidence_strength"]
        with patch.object(job_core, "get_by_identity", return_value=record), \
             patch.object(job_core, "get_semantic_candidates", return_value=[]):
            final = job_core.classify_identity(article)
        self.assertEqual(final["action"], "duplicate")

    def test_pdf_failure_keeps_matching_reference_pending(self):
        article = sample_job(
            job_external_reference="REF-2026-FAIL",
            content_fetch_status="success",
            job_detail_url="https://example.com/jobs/12345",
            source_tables=[],
            source_tables_count=0,
            job_document_links=[
                {"url": "https://example.com/notice.pdf", "label": "الإعلان"}
            ],
            job_document_text_download_failures=1,
        )
        job_core.finalize_identity_evidence_stage(article)
        self.assertFalse(job_core.identity_evidence_stage_complete(article))

        record = {
            "campaign_id": "campaign-a",
            "identity_key": job_core.identity_key(article),
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "REF-2026-FAIL",
            "deadline": article["job_deadline"],
            "number_of_positions": article["job_number_of_positions"],
            "published_at": article["job_published_at"],
            "application_url": article["job_application_url"],
        }
        with patch.object(job_core, "get_by_identity", return_value=record), \
             patch.object(job_core, "get_semantic_candidates", return_value=[]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "hold")
        self.assertIn("awaiting evidence stage", result["reason"])

    def test_same_reference_with_different_verified_evidence_is_update(self):
        article = sample_job(
            job_external_reference="REF-2026-101",
            source_tables=[
                {"rows": [["التخصص", "الأمن السيبراني"], ["عدد المناصب", "3"]]}
            ],
            source_tables_count=1,
            job_number_of_positions=3,
        )
        self._complete_identity_evidence(article)
        record = {
            "campaign_id": "campaign-a",
            "identity_key": job_core.identity_key(article),
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "REF-2026-101",
            "deadline": article["job_deadline"],
            "number_of_positions": 3,
            "published_at": article["job_published_at"],
            "application_url": article["job_application_url"],
            "location": article["job_location"],
            "notice_type": "vacancy",
            "notice_status": "",
            "document_urls": "",
            "identity_evidence_signature": "different-signature",
            "identity_evidence_strength": max(4, int(article["identity_evidence_strength"])),
        }
        with patch.object(job_core, "get_by_identity", return_value=record), \
             patch.object(job_core, "get_semantic_candidates", return_value=[]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "update")
        self.assertIn("material change", result["reason"])

    def test_matching_verified_evidence_can_confirm_semantic_duplicate(self):
        article = sample_job(
            job_external_reference="",
            ats_reference="",
            raw={},
            job_application_url="",
            source_tables=[
                {"rows": [["التخصص", "الشبكات والأنظمة"], ["عدد المناصب", "5"]]}
            ],
            source_tables_count=1,
            job_number_of_positions=5,
        )
        self._complete_identity_evidence(article)
        record = {
            "campaign_id": "campaign-a",
            "identity_key": "old",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "",
            "deadline": article["job_deadline"],
            "number_of_positions": 5,
            "published_at": article["job_published_at"],
            "application_url": "",
            "identity_evidence_signature": article["identity_evidence_signature"],
            "identity_evidence_strength": article["identity_evidence_strength"],
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(job_core, "get_semantic_candidates", return_value=[record]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "duplicate")
        self.assertIn("verified evidence", result["reason"])

    def test_different_verified_evidence_keeps_same_title_as_new_campaign(self):
        article = sample_job(
            job_external_reference="",
            ats_reference="",
            raw={},
            job_application_url="",
            source_tables=[
                {"rows": [["التخصص", "الذكاء الاصطناعي"], ["عدد المناصب", "2"]]}
            ],
            source_tables_count=1,
            job_number_of_positions=2,
        )
        self._complete_identity_evidence(article)
        record = {
            "campaign_id": "campaign-a",
            "identity_key": "old",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "",
            "deadline": article["job_deadline"],
            "number_of_positions": 2,
            "published_at": article["job_published_at"],
            "application_url": "",
            "identity_evidence_signature": "other-specialty-signature",
            "identity_evidence_strength": max(4, int(article["identity_evidence_strength"])),
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(job_core, "get_semantic_candidates", return_value=[record]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "new_campaign")
        self.assertIn("evidence differs", result["reason"])

    def test_legacy_campaign_without_strong_evidence_cannot_confirm_duplicate(self):
        article = sample_job(
            job_external_reference="",
            ats_reference="",
            raw={},
            job_application_url="",
            source_tables=[],
            source_tables_count=0,
        )
        self._complete_identity_evidence(article)
        record = {
            "campaign_id": "legacy",
            "identity_key": "old",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "",
            "deadline": article["job_deadline"],
            "number_of_positions": article["job_number_of_positions"],
            "published_at": article["job_published_at"],
            "application_url": "",
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(job_core, "get_semantic_candidates", return_value=[record]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "new_campaign")
        self.assertIn("duplicate not confirmed", result["reason"])

    def test_identity_checks_all_semantic_campaigns_before_final_decision(self):
        article = sample_job(
            job_external_reference="",
            ats_reference="",
            raw={},
            job_application_url="",
            source_tables=[
                {"rows": [["التخصص", "أمن الشبكات"], ["عدد المناصب", "4"]]}
            ],
            source_tables_count=1,
            job_number_of_positions=4,
        )
        self._complete_identity_evidence(article)

        different = {
            "campaign_id": "campaign-old-different",
            "identity_key": "old-a",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "",
            "deadline": article["job_deadline"],
            "number_of_positions": 4,
            "published_at": article["job_published_at"],
            "application_url": "",
            "identity_evidence_signature": "different-campaign-signature",
            "identity_evidence_strength": 4,
            "identity_evidence_comparison_strength": 4,
        }
        matching = {
            "campaign_id": "campaign-existing-match",
            "identity_key": "old-b",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "",
            "deadline": article["job_deadline"],
            "number_of_positions": 4,
            "published_at": article["job_published_at"],
            "application_url": "",
            "identity_evidence_signature": article["identity_evidence_signature"],
            "identity_evidence_strength": article["identity_evidence_strength"],
            "identity_evidence_comparison_strength": article["identity_evidence_comparison_strength"],
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(
                 job_core,
                 "get_semantic_candidates",
                 return_value=[different, matching],
             ):
            result = job_core.classify_identity(article)

        self.assertEqual(result["action"], "duplicate")
        self.assertEqual(result["existing"]["campaign_id"], "campaign-existing-match")

    def test_cross_source_same_campaign_is_duplicate_with_strong_evidence(self):
        article = sample_job(
            source_name="Second Official Source",
            job_external_reference="REF-NEW",
            job_deadline="2026-10-10",
            job_number_of_positions=100,
            source_tables=[
                {"rows": [["التخصص", "تقني في الشبكات"], ["عدد المناصب", "100"]]}
            ],
            source_tables_count=1,
        )
        self._complete_identity_evidence(article)
        record = {
            "campaign_id": "campaign-a",
            "identity_key": "old",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "REF-OLD",
            "deadline": "2026-10-10",
            "number_of_positions": 100,
            "published_at": "2026-09-28T08:00:00+00:00",
            "application_url": "https://other.example/jobs/old",
            "identity_evidence_signature": article["identity_evidence_signature"],
            "identity_evidence_strength": article["identity_evidence_strength"],
            "identity_evidence_comparison_strength": article["identity_evidence_comparison_strength"],
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(job_core, "get_semantic_candidates", return_value=[record]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "duplicate")
        self.assertIn("same campaign", result["reason"])

    def test_cross_source_same_role_with_different_deadline_stays_new_campaign(self):
        article = sample_job(
            source_name="Second Official Source",
            job_external_reference="REF-NEW",
            job_deadline="2026-11-20",
            job_number_of_positions=3,
        )
        record = {
            "campaign_id": "campaign-a",
            "identity_key": "old",
            "semantic_key": job_core.semantic_key(article),
            "external_reference": "REF-OLD",
            "deadline": "2026-10-10",
            "number_of_positions": 100,
            "published_at": "2026-07-01T08:00:00+00:00",
            "application_url": "https://other.example/jobs/old",
        }
        with patch.object(job_core, "get_by_identity", return_value={}), \
             patch.object(job_core, "get_semantic_candidates", return_value=[record]):
            result = job_core.classify_identity(article)
        self.assertEqual(result["action"], "new_campaign")



    def test_job_extractor_reads_labelled_public_employer(self):
        html = """
        <html><body>
          <h3>Administration qui recrute</h3>
          <p>Ministère de l’intérieur - Province de Settat</p>
          <h3>Délai de dépôt des candidatures</h3>
          <p>30 Mai 2026 - 16:30</p>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        fields = job_extractor.extract_job_fields(
            soup,
            {
                "source_name": "Emploi-Public — services de l'État",
                "source_country": "MA",
                "official_source": True,
            },
            "https://www.emploi-public.ma/fr/concours/details/test",
            full_text=soup.get_text(" ", strip=True),
        )
        self.assertEqual(
            fields["job_company"],
            "Ministère de l’intérieur - Province de Settat",
        )

    def test_logo_resolver_prefers_emploi_public_administration_logo(self):
        html = """
        <html><body>
          <h3>Administration organisatrice : Ministère de l’intérieur</h3>
          <img
            src="/backoffice/files/images/administrations/interieur.png"
            alt="Ministère de l’intérieur"
          >
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        article = {
            "job_company": "Ministère de l’intérieur",
            "source_name": "Emploi-Public — services de l'État",
            "official_source": True,
        }
        probe = {
            "ok": True,
            "kind": "raster",
            "width": 600,
            "height": 300,
            "checksum": "abc123",
            "final_url": "https://www.emploi-public.ma/backoffice/files/images/administrations/interieur.png",
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(
                 company_logo_resolver,
                 "REGISTRY_PATH",
                 Path(temp_dir) / "company_logo_registry.json",
             ), \
             patch.object(company_logo_resolver, "_probe_image", return_value=probe):
            resolved = company_logo_resolver.resolve_company_logo(
                soup,
                article,
                "https://www.emploi-public.ma/fr/concours/details/test",
            )

        self.assertTrue(resolved["company_logo_verified"])
        self.assertGreaterEqual(resolved["company_logo_confidence"], 99)
        self.assertEqual(
            resolved["company_logo_source"],
            "emploi_public_administration",
        )
        self.assertIn("/images/administrations/", resolved["company_logo_url"])

    def test_logo_resolver_rejects_platform_logo_for_unrelated_employer(self):
        html = """
        <html><body>
          <header>
            <img src="/assets/logo.png" class="logo" alt="Emploi Public">
          </header>
          <h3>Administration organisatrice : Archives du Maroc</h3>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        article = {
            "job_company": "Archives du Maroc",
            "source_name": "Emploi-Public — établissements publics",
            "official_source": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(
                 company_logo_resolver,
                 "REGISTRY_PATH",
                 Path(temp_dir) / "company_logo_registry.json",
             ), \
             patch.object(company_logo_resolver, "_probe_image") as probe:
            resolved = company_logo_resolver.resolve_company_logo(
                soup,
                article,
                "https://www.emploi-public.ma/fr/concours/details/test",
            )

        self.assertFalse(resolved["company_logo_verified"])
        self.assertEqual(resolved["company_logo_url"], "")
        probe.assert_not_called()

    def test_logo_resolver_accepts_matching_jobposting_logo(self):
        html = """
        <html><head>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Network Engineer",
            "hiringOrganization": {
              "@type": "Organization",
              "name": "Example Telecom",
              "logo": "https://cdn.example.com/example-telecom-logo.png"
            }
          }
          </script>
        </head><body></body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        article = {
            "job_company": "Example Telecom",
            "source_name": "Example Telecom Careers",
            "official_source": True,
        }
        probe_result = {
            "ok": True,
            "kind": "raster",
            "width": 800,
            "height": 260,
            "checksum": "def456",
            "final_url": "https://cdn.example.com/example-telecom-logo.png",
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.object(
                 company_logo_resolver,
                 "REGISTRY_PATH",
                 Path(temp_dir) / "company_logo_registry.json",
             ), \
             patch.object(
                 company_logo_resolver,
                 "_probe_image",
                 return_value=probe_result,
             ):
            resolved = company_logo_resolver.resolve_company_logo(
                soup,
                article,
                "https://careers.example.com/jobs/123",
            )

        self.assertTrue(resolved["company_logo_verified"])
        self.assertEqual(resolved["company_logo_source"], "jobposting_jsonld")
        self.assertGreaterEqual(resolved["company_logo_confidence"], 99)


if __name__ == "__main__":
    unittest.main()
