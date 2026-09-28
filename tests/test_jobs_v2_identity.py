import unittest

from jobs_bot_v2.blog_safety import validate_blogger_result_url
from jobs_bot_v2.fact_resolver import reconcile_batch
from jobs_bot_v2.job_identity import compare_to_existing, snapshot
from jobs_bot_v2.models import JobCandidate
from jobs_bot_v2.slug_policy import desired_slug


def job(**overrides):
    base = dict(
        source_name="Official",
        source_url="https://example.com/jobs/123",
        canonical_url="https://example.com/jobs/123",
        title="Technicien informatique",
        company="Example SA",
        location="Casablanca",
        country="MA",
        application_url="https://example.com/apply/123",
        number_of_positions=100,
        deadline="2026-10-10",
        published_at="2026-09-28T08:00:00+00:00",
        eligibility="morocco",
        official_source=True,
        source_priority="S",
        raw={"job_id": "123"},
    )
    base.update(overrides)
    return JobCandidate(**base)


class IdentityTests(unittest.TestCase):
    def test_position_change_is_same_posting_update(self):
        old = job(number_of_positions=100)
        record = snapshot(old)
        new = job(number_of_positions=20)
        decision = compare_to_existing(new, record)
        self.assertEqual(decision.action, "update")
        self.assertTrue(decision.material_update)

    def test_different_reference_is_new_campaign(self):
        old = job(raw={"job_id": "123"}, application_url="", canonical_url="")
        record = snapshot(old)
        new = job(raw={"job_id": "999"}, application_url="", canonical_url="")
        decision = compare_to_existing(new, record)
        self.assertEqual(decision.action, "new_campaign")

    def test_ambiguous_same_semantic_needs_review(self):
        old = job(raw={}, application_url="", canonical_url="", source_url="")
        record = snapshot(old)
        new = job(raw={}, application_url="", canonical_url="", source_url="", number_of_positions=20)
        decision = compare_to_existing(new, record)
        self.assertEqual(decision.action, "needs_review")

    def test_slugs_are_stable_and_collision_resistant(self):
        a = job(raw={"job_id": "123", "_campaign_id": "abc123def456"})
        b = job(raw={"job_id": "999", "_campaign_id": "fff999eee888"}, application_url="https://example.com/apply/999")
        self.assertEqual(desired_slug(a), desired_slug(a))
        self.assertNotEqual(desired_slug(a), desired_slug(b))

    def test_mutable_facts_do_not_change_slug(self):
        a = job(
            raw={"job_id": "123", "_campaign_id": "abc123def456"},
            number_of_positions=100,
            deadline="2026-10-10",
            salary="",
            location="Casablanca",
        )
        b = job(
            raw={"job_id": "123", "_campaign_id": "abc123def456"},
            number_of_positions=20,
            deadline="2026-11-20",
            salary="9000 MAD",
            location="Rabat",
        )
        self.assertEqual(desired_slug(a), desired_slug(b))
        self.assertNotIn("100", desired_slug(a))
        self.assertNotIn("2026", desired_slug(a))

    def test_same_role_next_year_becomes_new_campaign(self):
        old = job(
            published_at="2026-01-10T08:00:00+00:00",
            deadline="2026-02-01",
            raw={"job_id": "123"},
        )
        record = snapshot(old)
        new = job(
            published_at="2027-01-15T08:00:00+00:00",
            deadline="2027-02-01",
            raw={"job_id": "123"},
        )
        decision = compare_to_existing(new, record)
        self.assertEqual(decision.action, "new_campaign")


class SourceMergeTests(unittest.TestCase):
    def test_official_source_beats_aggregator_conflict(self):
        official = job(number_of_positions=100, official_source=True, source_priority="S")
        aggregator = job(
            source_name="Aggregator",
            source_url="https://agg.example/item",
            canonical_url="https://agg.example/item",
            official_source=False,
            source_priority="B",
            number_of_positions=20,
            raw={},
        )
        merged, held = reconcile_batch([aggregator, official])
        self.assertEqual(len(held), 0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].number_of_positions, 100)
        self.assertTrue(merged[0].official_source)

    def test_equal_official_conflict_is_held(self):
        a = job(source_name="Official A", number_of_positions=100, raw={})
        b = job(
            source_name="Official B",
            source_url="https://other.example/job",
            canonical_url="https://other.example/job",
            number_of_positions=20,
            raw={},
        )
        # Shared apply URL and equal official priority make this clearly one
        # campaign with conflicting authoritative facts.
        merged, held = reconcile_batch([a, b])
        self.assertEqual(len(merged), 0)
        self.assertEqual(len(held), 1)
        self.assertIn("number_of_positions", held[0]["conflicting_fields"])


class BlogSafetyTests(unittest.TestCase):
    def test_test_blog_host_allowed(self):
        url = "https://cyberopluss.blogspot.com/2026/09/test.html"
        self.assertEqual(validate_blogger_result_url(url), url)

    def test_real_domain_blocked_during_test(self):
        with self.assertRaises(RuntimeError):
            validate_blogger_result_url("https://www.cyberoplus.com/2026/09/test.html")


if __name__ == "__main__":
    unittest.main()
