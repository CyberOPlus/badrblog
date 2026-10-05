"""Discovery blind spots and observable, bounded Facebook/AI delivery health."""

import asyncio
import contextlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import article_ai_processor as ai
import delivery_monitor as monitor
import main
import scraper


class DeliveryReliabilityTests(unittest.TestCase):
    def test_downloaded_listing_is_fully_scanned_after_known_streak(self):
        url = "https://example.com/jobs"
        links = [{"title": "Known vacancy", "url": f"{url}/{i}"} for i in range(9)]
        fresh = {"title": "New developer", "url": f"{url}/new"}
        links.append(fresh)
        known = {scraper._discovery_identity(row) for row in links[:-1]}
        with patch.object(scraper, "_fetch_text_async", return_value=("<html></html>", "", 200)):
            rows, error, _, _ = asyncio.run(scraper._collect_paginated_html_links_async(
                None, url, lambda *args, **kwargs: links,
                known_ids=known, seen_streak_stop=8, max_pages=1, max_items=100,
            ))
        self.assertEqual(rows, [fresh])
        self.assertEqual(error, "")
        self.assertNotIn(scraper._discovery_identity(fresh), known)

    def test_alten_card_date_is_bound_to_its_own_vacancy(self):
        html = '''<main>
        <article><a href="/jobs/744000153145339-developer/">Junior Developer</a><span>02/10/2026</span></article>
        <article><a href="/jobs/744000153145340-security/">Security Engineer</a><span>03/10/2026</span></article>
        <article><a href="/jobs/744000153145341-support/">Support Engineer</a></article>
        <a href="/carrieres/">Nos carrières</a>
        <a href="https://example.com/jobs/744000153145342-other/">Foreign vacancy</a>
        </main>'''
        rows = scraper._parse_alten_job_links(html, "https://www.alten.ma/rejoignez-nous/")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["source_published_at"], "2026-10-02")
        self.assertEqual(rows[1]["source_published_at"], "2026-10-03")
        self.assertNotIn("source_published_at", rows[2])

    def status(self, rows, services=None):
        with patch.object(monitor, "load_article_queue", return_value={"articles": rows}), \
             patch.object(monitor, "load_job_state", return_value={}):
            return monitor.delivery_status(now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc),
                                           services=services or {})

    def test_idle_worker_does_not_claim_hourly_delivery_is_healthy(self):
        result = self.status([])
        self.assertEqual(result["state"], "no_fresh_candidate")
        self.assertFalse(result["hourly_target_met"])

    def test_anapec_timeout_cooldown_is_reported_as_source_degraded(self):
        payload = {
            "sources": {
                "https://www.anapec.org/jobs": {
                    "source_name": "ANAPEC — offres nationales",
                    "failure_count": 10,
                    "last_error": "TimeoutError",
                    "cooldown_until": "2026-10-03T10:30:00Z",
                }
            }
        }
        with TemporaryDirectory() as temp, \
             patch.object(monitor, "SOURCE_HEALTH_PATH", Path(temp)/"source_health.json"):
            monitor.SOURCE_HEALTH_PATH.write_text(json.dumps(payload), encoding="utf-8")
            result = monitor.probe_sources(now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["active_issues"][0]["category"], "network_timeout")
        self.assertIn("ANAPEC", result["action"])

    def test_source_degradation_is_visible_without_becoming_blocking_state(self):
        result = self.status(
            [],
            services={
                "facebook": {"status": "ok"},
                "ai": {"status": "ok"},
                "sources": {
                    "status": "degraded",
                    "action": "ANAPEC is unreachable from GitHub-hosted runners.",
                },
            },
        )
        self.assertEqual(result["state"], "source_degraded")
        self.assertNotIn(result["state"], monitor.BLOCKING_STATES)

    def test_waiting_facebook_article_is_a_stall_not_an_empty_source_pool(self):
        result = self.status([{"status": "published", "facebook_status": "failed",
                               "facebook_queued_at": "2026-10-03T08:30:00Z"}])
        self.assertEqual(result["state"], "facebook_stalled")
        self.assertEqual(result["oldest_facebook_pending_minutes"], 90)

    def test_missing_traffic_link_comment_is_reported(self):
        result = self.status([{"facebook_post_id": "page_post", "facebook_posted_at": "2026-10-03T09:55:00Z",
                               "facebook_status": "posted_comment_failed"}])
        self.assertEqual(result["state"], "facebook_comment_pending")
        self.assertEqual(result["pending_first_comments"], 1)
        self.assertTrue(result["hourly_target_met"])

    def test_acknowledged_post_without_required_comment_is_not_complete(self):
        result = self.status([{"facebook_post_id": "page_post", "facebook_status": "posted",
                               "facebook_link_mode": "comment"}])
        self.assertEqual(result["state"], "facebook_comment_pending")
        self.assertIn(result["state"], monitor.BLOCKING_STATES)

    def test_fresh_ready_candidate_waiting_over_hour_is_reported(self):
        result = self.status([{"status": "ready", "official_source": True,
                               "job_published_at": "2026-10-03",
                               "discovered_at": "2026-10-03T08:30:00Z",
                               "content_fetch_status": "success",
                               "job_quality_status": "publish",
                               "job_quality_reasons": [],
                               "job_hard_gate_passed": True}])
        self.assertEqual(result["state"], "candidate_stalled")
        self.assertEqual(result["oldest_ready_candidate_minutes"], 90)
        self.assertEqual(result["verified_ready_candidate_count"], 1)
        self.assertEqual(result["unverified_ready_candidate_count"], 0)

    def test_unverified_ready_candidate_is_not_a_delivery_stall(self):
        result = self.status([{"status": "ready", "official_source": True,
                               "job_published_at": "2026-10-03",
                               "discovered_at": "2026-10-03T08:30:00Z",
                               "content_fetch_status": "success",
                               "job_quality_status": "queue",
                               "job_quality_reasons": ["eligibility must be verified"],
                               "job_hard_gate_passed": False}])
        self.assertEqual(result["state"], "no_verified_candidate")
        self.assertEqual(result["oldest_ready_candidate_minutes"], 0)
        self.assertEqual(result["verified_ready_candidate_count"], 0)
        self.assertEqual(result["unverified_ready_candidate_count"], 1)

    def test_expired_facebook_token_is_actionable_without_a_test_post(self):
        response = Mock(status_code=400)
        response.json.return_value = {"error": {"code": 190, "message": "private token response"}}
        with patch.object(monitor, "FACEBOOK_AUTO_POST", True), \
             patch.object(monitor, "FACEBOOK_PAGE_ID", "page"), \
             patch.object(monitor, "FACEBOOK_PAGE_ACCESS_TOKEN", "private-secret"), \
             patch.object(monitor.requests, "get", return_value=response) as get, \
             patch.object(monitor.requests, "post") as post:
            result = monitor.probe_facebook()
        self.assertEqual(result["status"], "auth")
        self.assertIn("Renew", result["action"])
        get.assert_called_once()
        post.assert_not_called()
        self.assertNotIn("private", json.dumps(result))

    def test_ai_probe_rotates_and_saves_only_failure_categories(self):
        candidates = [{"provider": "gemini", "api_key": "private-key"}, {"provider": "groq"}]
        with patch.object(ai, "_provider_candidates", return_value=candidates), \
             patch.object(ai, "_generate_with_candidate", side_effect=[RuntimeError("429 quota private-key"), ('{"status":"ok"}', "groq")]):
            result = monitor.probe_ai()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["providers"]["gemini"]["status"], "quota")
        self.assertEqual(result["providers"]["groq"]["status"], "ok")
        self.assertNotIn("private-key", json.dumps(result))

    def test_active_ai_circuit_overrides_stale_ok_probe(self):
        with patch.object(monitor, "load_article_queue", return_value={"articles": []}), \
             patch.object(monitor, "load_job_state", return_value={}):
            result = monitor.delivery_status(
                now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc),
                services={
                    "facebook": {"status": "ok"},
                    "ai": {"status": "ok"},
                },
                ai_circuit={
                    "global_open": True,
                    "global_retry_after": "2026-10-03T10:30:00+00:00",
                    "global_category": "cooldown",
                },
            )

        self.assertEqual(result["state"], "waiting_provider")
        self.assertEqual(result["services"]["ai"]["status"], "cooldown")
        self.assertEqual(
            result["services"]["ai"]["retry_after"],
            "2026-10-03T10:30:00+00:00",
        )

    def test_closed_ai_circuit_clears_stale_cooldown_health(self):
        with patch.object(monitor, "load_article_queue", return_value={"articles": []}), \
             patch.object(monitor, "load_job_state", return_value={}):
            result = monitor.delivery_status(
                now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc),
                services={
                    "facebook": {"status": "ok"},
                    "ai": {
                        "status": "cooldown",
                        "retry_after": "2026-10-03T10:30:00+00:00",
                        "category": "cooldown",
                        "providers": {
                            "groq": {"status": "ok"},
                            "mistral": {"status": "quota"},
                        },
                    },
                },
                ai_circuit={"global_open": False},
            )

        self.assertEqual(result["services"]["ai"]["status"], "ok")
        self.assertNotIn("retry_after", result["services"]["ai"])
        self.assertNotIn("category", result["services"]["ai"])
        self.assertNotEqual(result["state"], "waiting_provider")

    def test_service_checks_are_cached_between_cycles(self):
        recent = datetime.now(timezone.utc).isoformat()
        state = {"services_checked_at": recent, "services": {"facebook": {"status": "ok"}, "ai": {"status": "ok"}}}
        with TemporaryDirectory() as temp, \
             patch.object(monitor, "HEALTH_PATH", Path(temp)/"health.json"), \
             patch.object(monitor, "_read_health", return_value=state), \
             patch.object(monitor, "load_article_queue", return_value={"articles": []}), \
             patch.object(monitor, "load_job_state", return_value={}), \
             patch.object(monitor, "probe_facebook") as facebook, \
             patch.object(monitor, "probe_ai") as probe:
            monitor.run_monitor()
        facebook.assert_not_called()
        probe.assert_not_called()

    def test_source_health_is_refreshed_even_when_external_service_checks_are_cached(self):
        recent = datetime.now(timezone.utc).isoformat()
        state = {"services_checked_at": recent, "services": {"facebook": {"status": "ok"}, "ai": {"status": "ok"}}}
        with TemporaryDirectory() as temp, \
             patch.object(monitor, "HEALTH_PATH", Path(temp)/"health.json"), \
             patch.object(monitor, "_read_health", return_value=state), \
             patch.object(monitor, "load_article_queue", return_value={"articles": []}), \
             patch.object(monitor, "load_job_state", return_value={}), \
             patch.object(monitor, "probe_sources", return_value={"status": "ok", "active_issues": [], "action": "ok"}) as source_probe:
            result = monitor.run_monitor()
        source_probe.assert_called_once()
        self.assertEqual(result["services"]["sources"]["status"], "ok")

    def test_failure_scope_and_independent_social_failure_survive_checkpoint(self):
        result = {"completed": True, "scheduled_facebook": {"failed": 1},
                  "failure_scope": "external_auth_problem", "failure_fingerprint": "fingerprint",
                  "retry_after": "2026-10-03T11:00:00Z"}
        record = main._auto_cycle_record_from_result("run", "2026-10-03T10:00:00", result)
        with TemporaryDirectory() as temp, \
             patch.object(main, "LOGS_DIR", Path(temp)), \
             patch.object(main, "AUTO_CYCLE_RUN_LOG", Path(temp)/"runs.jsonl"):
            main._append_auto_cycle_run_log(record)
            saved = json.loads((Path(temp)/"runs.jsonl").read_text())
        self.assertEqual(saved["failure_scope"], "external_auth_problem")
        self.assertEqual(saved["failure_fingerprint"], "fingerprint")
        self.assertEqual(saved["retry_after"], result["retry_after"])
        self.assertIn("Facebook", saved["warning"])
        self.assertEqual(saved["facebook_queue_failures"], 1)

    def test_blocker_reporting_is_after_checkpoint_and_before_successor(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text()
        self.assertLess(text.index("name: Persist Jobs runtime state"), text.index("name: Report delivery blockers"))
        self.assertLess(text.index("name: Report delivery blockers"), text.index("name: Continue Jobs auto-cycle"))
        self.assertIn('"data/delivery_health.json"', text)
        self.assertIn("if: always() && github.ref == 'refs/heads/main'", text[text.index("name: Continue Jobs auto-cycle"):])


if __name__ == "__main__":
    unittest.main()
