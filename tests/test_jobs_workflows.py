"""Jobs entrypoint, deployment, workflow persistence and credential guardrails."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import main
import blogger_client
import source_validator
from production_logging import _clean_value


class JobsWorkflowTests(unittest.TestCase):
    def test_workflow_cron_and_facebook_safety_are_current(self):
        text = Path(".github/workflows/auto-cycle.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "1,7,13,19,25,31,37,43,49,55 * * * *"', text)
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("  push:\n", text)
        self.assertIn("group: jobs-production-refs/heads/main", text)
        self.assertIn("auto-cycle:\n    if: github.ref == 'refs/heads/main'", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("timeout-minutes: 15", text)
        self.assertIn("timeout-minutes: 10", text)
        self.assertIn('"MAX_SOURCES_PER_RUN": "20"', text)
        self.assertIn('"MAX_POSTS_PER_RUN": "1"', text)
        self.assertIn('"MAX_ARTICLES_PER_RUN": "1"', text)
        self.assertIn('"SAFE_CYCLE_MAX_ARTICLES": "1"', text)
        self.assertIn('"MAX_LIVE_POSTS_PER_DAY": "240"', text)
        self.assertIn('"TARGET_LIVE_POSTS_PER_DAY": "240"', text)
        self.assertIn('"MIN_MINUTES_BETWEEN_LIVE_POSTS": "0"', text)
        self.assertIn('"META_GRAPH_API_VERSION": "v26.0"', text)
        self.assertIn('"JOBS_MIN_PUBLISH_INTERVAL_MINUTES": "5"', text)
        self.assertIn("continue-on-error: true", text)
        self.assertIn("for attempt in 1 2 3 4 5 6; do", text)
        self.assertIn("actions/checkout@v7", text)
        self.assertIn("actions/setup-python@v7", text)
        self.assertIn('JOBS_RUN_BASE_SHA=$(git rev-parse HEAD)', text)
        self.assertIn('RUN_BASE_SHA="${JOBS_RUN_BASE_SHA:-}"', text)
        self.assertIn("merge_jobs_queue_snapshot", text)
        self.assertIn("Jobs queue changed upstream; merging remote and runner snapshot.", text)
        self.assertIn("added_snapshot_only", text)
        self.assertIn("Continue production cycle directly", text)
        self.assertIn("run.id !== context.runId", text)
        self.assertIn("Next production cycle dispatched directly.", text)
        self.assertIn('"JOBS_FACEBOOK_FOLLOW_ARTICLE": "true"', text)
        self.assertIn('"JOBS_MAX_PUBLISH_AGE_HOURS": "12"', text)
        watchdog = Path(".github/workflows/jobs-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Jobs Auto Cycle", "Jobs Core Tests"]', watchdog)
        self.assertIn("types: [completed]", watchdog)
        self.assertIn("github.rest.git.getBlob", watchdog)
        self.assertIn("conclusion !== \"success\"", watchdog)
        self.assertIn('context.eventName === "workflow_run"', watchdog)
        self.assertNotIn("prefer_jobs_queue_snapshot", text)

        watchdog = Path(".github/workflows/jobs-watchdog.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "4,10,16,22,28,34,40,46,52,58 * * * *"', watchdog)
        self.assertIn("if (ageMinutes < 4)", watchdog)
        self.assertIn("} else if (ageMinutes <= 8) {", watchdog)
        self.assertIn("github.rest.actions.createWorkflowDispatch", watchdog)
        self.assertIn("run.status === \"queued\" || run.status === \"in_progress\"", watchdog)


    def test_no_env_or_secret_files_are_committed(self):
        tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
        self.assertNotIn(".env", tracked)
        self.assertNotIn("client_secret.json", tracked)
        self.assertNotIn("data/token.json", tracked)


    def test_auto_cycle_run_log_contains_reliability_fields(self):
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "auto_cycle_runs.jsonl"
            result = {
                "completed": False,
                "skipped": True,
                "reason": "no valid article",
                "fetch": {
                    "selected_category": "jobs",
                    "sources_checked": 12,
                    "articles_found": 3,
                },
                "execution_seconds": 4.2,
            }
            record = main._auto_cycle_record_from_result("run-1", "2026-04-27T00:00:00", result)
            with patch.object(main, "AUTO_CYCLE_RUN_LOG", log_path):
                main._append_auto_cycle_run_log(record)
            saved = json.loads(log_path.read_text(encoding="utf-8").strip())

        self.assertTrue(saved["timestamp"])
        self.assertEqual(saved["category"], "jobs")
        self.assertEqual(saved["sources_checked"], 12)
        self.assertEqual(saved["candidates_found"], 3)
        self.assertEqual(saved["skip_reason"], "no valid article")
        self.assertIn("published_url", saved)
        self.assertEqual(saved["execution_seconds"], 4.2)


    def test_secret_redaction(self):
        cleaned = _clean_value(
            {
                "api_key": "abc123",
                "refresh_token": "refresh-secret",
                "headers": {"Authorization": "Bearer token-secret"},
                "url": "https://example.com/?access_token=token-secret",
            }
        )
        self.assertNotIn("abc123", cleaned)
        self.assertNotIn("refresh-secret", cleaned)
        self.assertNotIn("token-secret", cleaned)
        self.assertIn("[redacted]", cleaned)


    def test_example_environment_and_facebook_styles_are_jobs_only(self):
        env_text = Path("env.example").read_text(encoding="utf-8")
        self.assertIn("JOBS_MODE=true", env_text)
        self.assertIn("JOBS_MAX_PUBLISH_AGE_HOURS=12", env_text)
        self.assertIn("JOBS_DISCOVERY_SEEN_MEMORY=5000", env_text)
        self.assertIn("JOBS_FACEBOOK_MIN_INTERVAL_MINUTES=5", env_text)
        for retired in (
            "FAST_NEWS_MODE",
            "CATEGORY_ROTATION_MODE",
            "PROCESS_FULL_CATEGORY_PER_RUN",
            "RECENT_NEWS_ONLY",
            "SOURCE_URL=",
            "SOURCES=",
            "FACEBOOK_LINK_MODE",
            "JOBS_MODE=false",
        ):
            self.assertNotIn(retired, env_text)

        facebook_text = Path("facebook_publisher.py").read_text(encoding="utf-8")
        for retired_style in ("tech_news", "apps_programs", "ai_tools"):
            self.assertNotIn(retired_style, facebook_text)
        self.assertIn('JOBS_CAPTION_STYLE = "jobs"', facebook_text)

    def test_legacy_environment_cannot_enable_news_or_disable_jobs_spacing(self):
        env = dict(os.environ, JOBS_MODE="false", JOBS_FACEBOOK_FOLLOW_ARTICLE="false",
                   JOBS_FACEBOOK_MIN_INTERVAL_MINUTES="0", JOBS_MAX_PUBLISH_AGE_HOURS="999",
                   FAST_NEWS_MODE="true", CATEGORY_ROTATION_MODE="true")
        code = "import config; print(config.JOBS_MODE, config.ARTICLE_QUEUE_PATH.name, config.JOBS_FACEBOOK_FOLLOW_ARTICLE, config.JOBS_FACEBOOK_MIN_INTERVAL_MINUTES, config.JOBS_MAX_PUBLISH_AGE_HOURS)"
        result = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        self.assertEqual(result.strip(), "True jobs_article_queue.json True 5 12")

    def test_default_entrypoint_runs_jobs_cycle_once(self):
        with patch.object(sys, "argv", ["main.py"]), \
             patch.object(main, "run_auto_cycle_logged", return_value={"completed": True}) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            main.main()
        run.assert_called_once_with()

    def test_retired_news_command_is_rejected_before_running(self):
        with patch.object(sys, "argv", ["main.py", "reset-state"]), \
             patch.object(main, "run_auto_cycle_logged") as run, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_jobs_loop_recovers_from_a_cycle_exception(self):
        with patch.object(sys, "argv", ["main.py", "--loop"]), \
             patch.object(main, "run_auto_cycle_logged", side_effect=[RuntimeError("transient"), KeyboardInterrupt()]) as run, \
             patch.object(main.time, "sleep") as sleep, \
             contextlib.redirect_stdout(io.StringIO()):
            main.main()
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()

    def test_jobs_state_and_secrets_are_persisted_separately(self):
        workflow = Path(".github/workflows/auto-cycle.yml").read_text()
        for required in ("jobs_article_queue.json", "data/job_memory", "data/crawl_state.json",
                         "data/job_state.json", "data/job_visual_state.json", "merge_jobs_queue_snapshot",
                         "Persist Jobs runtime state", "rm -f .env client_secret.json data/token.json"):
            self.assertIn(required, workflow)
        for retired in ("data/article_backlog.json", "data/published_ids.json", "data/topic_fingerprints.json"):
            self.assertNotIn(retired, workflow)

    def test_runner_with_missing_token_never_opens_oauth_or_fakes_publish(self):
        with TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
             patch.object(blogger_client, "TOKEN_FILE", Path(temp) / "token.json"), \
             patch.object(blogger_client, "BLOGGER_CLIENT_ID", "configured-client"), \
             patch.object(blogger_client, "BLOGGER_CLIENT_SECRET", "configured-secret"), \
             patch.object(blogger_client, "InstalledAppFlow") as oauth, \
             patch.object(blogger_client, "build") as build, \
             contextlib.redirect_stdout(io.StringIO()):
            creds = blogger_client.get_credentials()
            self.assertIsNone(creds)
            self.assertIsNone(blogger_client.create_blogger_service(creds))
        oauth.from_client_config.assert_not_called()
        oauth.from_client_secrets_file.assert_not_called()
        build.assert_not_called()

    def test_source_validator_accepts_jobs_and_rejects_news(self):
        source = {"name": "Official", "base_url": "https://example.com/careers",
                  "enabled": True, "fetch_limit_per_run": 20, "category_hint": "jobs-morocco"}
        with patch.object(source_validator, "load_sources", return_value=[source]):
            self.assertEqual(source_validator.check_sources_config()["invalid_count"], 0)
            source["category_hint"] = "Tech-News"
            self.assertEqual(source_validator.check_sources_config()["invalid_count"], 1)

    def test_successful_article_targets_its_own_facebook_post_even_in_force_mode(self):
        article = {"id": "new-job", "processing_status": "ready_for_ai", "ai_status": "completed",
                   "final_html": "<p>Verified job</p>", "publish_status": "published",
                   "blogger_post_url": "https://blog.example/new-job.html"}
        for social_error in (False, True):
            with self.subTest(social_error=social_error), contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, {"JOBS_ONE_SHOT_FORCE_RUN": "true"}))
                stack.enter_context(patch.object(main, "FACEBOOK_AUTO_POST", True))
                stack.enter_context(patch.object(main, "_find_article_by_id", return_value=article))
                stack.enter_context(patch.object(main, "prepare_selected_articles_for_ai", return_value={}))
                stack.enter_context(patch.object(main, "process_one_selected_article_with_ai", return_value={}))
                stack.enter_context(patch.object(main, "publish_one_blogger_post", return_value={"created_new": True}))
                record = stack.enter_context(patch.object(main, "_record_successful_publish"))
                post = stack.enter_context(patch.object(main, "post_one_article_to_facebook",
                    return_value={"posted": True}, side_effect=RuntimeError("temporary outage") if social_error else None))
                result = main._process_job_target(article, "live")
            self.assertTrue(result["completed"])
            post.assert_called_once_with(target_article_id="new-job", respect_limits=True)
            record.assert_called_once_with(article)

    def test_blogger_failure_does_not_create_a_facebook_post(self):
        article = {"id": "unpublished-job", "processing_status": "ready_for_ai",
                   "ai_status": "completed", "final_html": "<p>Verified job</p>"}
        with patch.object(main, "_find_article_by_id", return_value=article), \
             patch.object(main, "prepare_selected_articles_for_ai", return_value={}), \
             patch.object(main, "process_one_selected_article_with_ai", return_value={}), \
             patch.object(main, "publish_one_blogger_post", return_value={"error": "publish failed"}), \
             patch.object(main, "_mark_candidate_failure_for_retry"), \
             patch.object(main, "post_one_article_to_facebook") as post:
            result = main._process_job_target(article, "live")
        self.assertFalse(result["completed"])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
