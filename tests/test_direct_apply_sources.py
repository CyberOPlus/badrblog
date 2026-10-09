"""Regressions: only requested sources and direct vacancy-specific application actions."""
import json
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

import job_core
import article_draft_publisher as publisher
import facebook_publisher


def article(**kwargs):
    row = {
        "source_name": "Emploi-Public — services de l'État",
        "url": "https://example.org/jobs/12345",
        "canonical_url": "https://example.org/jobs/12345",
        "job_detail_url": "https://example.org/jobs/12345",
        "job_application_url": "https://example.org/apply/12345",
        "job_application_link_kind": "direct_apply",
        "job_deadline": "2099-01-01",
        "job_diploma": "Bac+2",
        "job_eligibility": "morocco",
        "job_title": "Technicien helpdesk",
        "job_published_at": "2026-10-09T10:30:00Z",
        "official_source": True,
    }
    row.update(kwargs)
    return row


class DirectApplySourcesTests(unittest.TestCase):
    def test_source_registry_contains_only_user_requested_job_sources(self):
        registry = json.loads((Path(__file__).resolve().parents[1] / "sources.json").read_text(encoding="utf-8"))
        names = {s["name"] for cat in registry["categories"] for s in cat["sources"]}
        self.assertEqual(len(names), 21)
        self.assertIn("ANAPEC — offres nationales", names)
        self.assertIn("Emploi-Public — services de l'État", names)
        self.assertIn("ReKrute Maroc", names)
        self.assertNotIn("Orange Business Morocco", names)
        self.assertNotIn("UNICEF Vacancies", names)
        self.assertNotIn("GOV.UK — register of licensed sponsors (verification only)", names)

    def test_specific_apply_form_passes(self):
        self.assertEqual(job_core.job_direct_application_policy(article()), "")

    def test_generic_listings_and_registration_are_blocked(self):
        for url in (
            "", "https://example.org/jobs", "https://example.org/register",
            "https://example.org/login?job_id=12345",
            "https://example.org/job/12345",
        ):
            with self.subTest(url=url):
                self.assertTrue(job_core.job_direct_application_policy(article(
                    job_application_url=url,
                    job_application_link_kind="",
                )))

    def test_candidate_account_platform_blocked_even_on_apply_path(self):
        reason = job_core.job_direct_application_policy(article(
            job_application_url="https://www.moncallcenter.ma/apply/12345"
        ))
        self.assertIn("registration", reason)

    def test_removed_source_in_stored_queue_never_passes(self):
        reason = job_core.job_direct_application_policy(article(source_name="Orange Business Morocco"))
        self.assertIn("retired source", reason)

    def test_specific_application_must_be_actual_apply_action(self):
        reason = job_core.job_direct_application_policy(article(
            job_application_url="https://example.org/jobs/12345",
            job_application_link_kind="official_job_page",
        ))
        self.assertTrue(reason)

    def test_explicit_apply_link_in_job_detail_passes_if_distinct(self):
        row = article(job_application_url="https://example.org/forms/job-12345",
                      job_application_link_kind="",
                      job_action_links=[{"kind": "apply", "url": "https://example.org/forms/job-12345"}])
        self.assertEqual(job_core.job_direct_application_policy(row), "")

    def test_registration_signal_overrides_apply_url(self):
        self.assertTrue(job_core.job_direct_application_policy(article(
            job_application_requires_registration=True
        )))

    def test_scoring_never_approves_generic_job_page(self):
        now = datetime(2026, 10, 9, 11, tzinfo=timezone.utc)
        result = job_core.score_job(article(
            job_application_url="https://example.org/jobs/12345",
            job_application_link_kind="official_job_page",
        ), now=now)
        self.assertFalse(result["passed"])
        self.assertTrue(result["direct_application_policy_reason"])

    def test_blogger_quality_gate_prevents_non_direct_application(self):
        row = article(job_application_url="https://example.org/login?job_id=12345")
        self.assertIn("direct job application", publisher._publish_quality_error(row, []))

    def test_facebook_cannot_queue_non_direct_application(self):
        row = article(
            status="published", publish_status="published",
            blogger_post_url="https://example.blogspot.com/job/12345.html",
            facebook_status="facebook_pending",
            job_application_url="https://example.org/jobs/12345",
            job_application_link_kind="official_job_page",
        )
        self.assertFalse(facebook_publisher._eligible_for_facebook(row))


if __name__ == "__main__":
    unittest.main()
