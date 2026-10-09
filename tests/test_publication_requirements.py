"""Hard publishing requirements: a future application deadline and < Bac+3."""
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
        "job_application_url": "https://example.org/jobs/123",
        "url": "https://example.org/jobs/123",
        "job_title": "Technicien réseau",
        "title": "Technicien réseau",
    }
    value.update(overrides)
    return value


class PublicationRequirementsTests(unittest.TestCase):
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

    def test_bac3_or_higher_is_blocked(self):
        for value in ("Bac+3", "Bac + 5", "Licence professionnelle", "Bachelor",
                      "Master", "Diplôme ingénieur", "الإجازة", "الماستر"):
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
        result = score_job(posting(job_deadline="", job_diploma="Bac+3"), now=NOW)
        self.assertFalse(result["passed"])
        self.assertIn("application closing deadline is missing or invalid", result["reasons"])
        self.assertIn("required diploma is Bac+3 or higher", result["reasons"])

    def test_fresh_job_rechecked_before_blogger_write(self):
        good = posting(official_source=True)
        _assert_fresh_job_for_new_live_publish(good, now=NOW)
        with self.assertRaisesRegex(RuntimeError, "policy blocked"):
            _assert_fresh_job_for_new_live_publish(
                posting(official_source=True, job_diploma="Bac+3"), now=NOW
            )


if __name__ == "__main__":
    unittest.main()
