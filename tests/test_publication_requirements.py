"""Hard publishing requirements: a future deadline and qualification up to Licence/Bac+3."""
import unittest
from datetime import datetime, timezone

from job_core import job_publication_policy, job_qualification_policy, score_job
from article_draft_publisher import _assert_fresh_job_for_new_live_publish


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def posting(**overrides):
    value = {
        "job_deadline": "2026-10-12",
        "job_diploma": "Diplôme Bac+2 (BTS)",
        "job_description": "Niveau d'études: Bac+2 demandé.",
        "job_published_at": "2026-10-09T11:00:00Z",
        "job_eligibility": "morocco",
        "job_application_url": "https://example.org/apply/12345",
        "url": "https://example.org/jobs/123",
        "job_title": "Technicien réseau",
        "title": "Technicien réseau",
    }
    value.update(overrides)
    return value


class PublicationRequirementsTests(unittest.TestCase):
    def test_internship_and_scholarship_have_longer_verified_freshness_windows(self):
        from job_core import job_publication_freshness
        internship = posting(opportunity_kind="internship", job_published_at="2026-10-06T12:00:00Z")
        grant = posting(opportunity_kind="scholarship", job_published_at="2026-09-20T12:00:00Z")
        same_job = posting(opportunity_kind="job", job_published_at="2026-10-06T12:00:00Z")
        self.assertTrue(job_publication_freshness(internship, now=NOW)["fresh"])
        self.assertTrue(job_publication_freshness(grant, now=NOW)["fresh"])
        self.assertFalse(job_publication_freshness(same_job, now=NOW)["fresh"])

    def test_rolling_training_needs_explicit_official_evidence(self):
        row = posting(opportunity_kind="training", job_deadline="", official_source=True)
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])
        row["job_application_rolling_verified"] = True
        row["job_application_rolling_evidence"] = "Official source says admissions are open continuously"
        self.assertTrue(job_publication_policy(row, now=NOW)["passed"])
        row["official_source"] = False
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])

    def test_verified_official_new_private_vacancy_can_omit_unspecified_deadline(self):
        row = posting(job_deadline="", official_source=True)
        row["job_action_links"] = [{"kind": "apply", "url": row["job_application_url"]}]
        self.assertTrue(job_publication_policy(row, now=NOW)["passed"])
        row["job_published_at"] = "2026-10-06T08:00:00Z"
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])
        row["job_published_at"] = "2026-10-09T11:00:00Z"
        row["job_action_links"] = []
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])
        row["job_action_links"] = [{"kind": "apply", "url": row["job_application_url"]}]
        row["job_notice_status"] = "closed"
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])

    def test_scholarship_without_deadline_cannot_be_invented_as_rolling(self):
        row = posting(opportunity_kind="scholarship", official_source=True,
                      job_deadline="", job_application_rolling_verified=True,
                      job_application_rolling_evidence="No closing date found")
        self.assertFalse(job_publication_policy(row, now=NOW)["passed"])

    def test_valid_bac2_with_future_deadline(self):
        result = job_publication_policy(posting(), now=NOW)
        self.assertTrue(result["passed"])
        self.assertEqual(result["education"], "below_bac3")

    def test_date_only_deadline_is_valid_until_end_of_day(self):
        result = job_publication_policy(posting(job_deadline="2026-10-09"), now=NOW)
        self.assertTrue(result["passed"])

    def test_missing_and_invalid_deadline_are_blocked(self):
        for value in ("", "not a real deadline", "2026-99-99"):
            with self.subTest(deadline=value):
                self.assertFalse(job_publication_policy(posting(job_deadline=value), now=NOW)["passed"])

    def test_expired_deadline_is_blocked(self):
        result = job_publication_policy(posting(job_deadline="2026-10-08"), now=NOW)
        self.assertFalse(result["passed"])
        self.assertTrue(result["permanent_reject"])

    def test_bac3_licence_bachelor_are_accepted(self):
        for value in ("Bac+3", "Licence professionnelle", "Bachelor", "الإجازة"):
            with self.subTest(level=value):
                result = job_publication_policy(posting(job_diploma=value), now=NOW)
                self.assertTrue(result["passed"])
                self.assertEqual(result["education"], "bac3")

    def test_above_bachelor_remains_rejected(self):
        for value in ("Bac + 5", "Master", "Diplôme ingénieur", "الماستر"):
            with self.subTest(level=value):
                result = job_publication_policy(posting(job_diploma=value), now=NOW)
                self.assertFalse(result["passed"])
                self.assertTrue(result["permanent_reject"])

    def test_unverified_diploma_does_not_publish(self):
        result = job_publication_policy(posting(job_diploma="", job_description="", full_article_text="Poste informatique"), now=NOW)
        self.assertFalse(result["passed"])
        self.assertEqual(result["education"], "unverified")

    def test_bac2_in_unrelated_duty_does_not_prove_eligibility(self):
        result = job_publication_policy(posting(job_diploma="", job_description="Former des personnes Bac+2 en sécurité réseau"), now=NOW)
        self.assertFalse(result["passed"])

    def test_verified_low_qualification_from_full_text(self):
        result = job_publication_policy(posting(job_diploma="", job_description="", full_article_text="Profil recherché: Technicien spécialisé (Bac+2)"), now=NOW)
        self.assertTrue(result["passed"])

    def test_mixed_diplomas_are_conservatively_blocked(self):
        result = job_publication_policy(posting(job_diploma="Bac+2 / Bac+5"), now=NOW)
        self.assertFalse(result["passed"])

    def test_scoring_never_overrides_hard_gates(self):
        result = score_job(posting(job_deadline="", job_diploma="Master"), now=NOW)
        self.assertFalse(result["passed"])
        self.assertIn("application closing deadline is missing or invalid", result["reasons"])
        self.assertIn("required diploma is above Bac+3", result["reasons"])

    def test_fresh_job_rechecked_before_blogger_write(self):
        good = posting(official_source=True)
        _assert_fresh_job_for_new_live_publish(good, now=NOW)
        with self.assertRaisesRegex(RuntimeError, "policy blocked"):
            _assert_fresh_job_for_new_live_publish(
                posting(official_source=True, job_diploma="Master"), now=NOW
            )


if __name__ == "__main__":
    unittest.main()
