"""Checkpoints must survive serialization and filesystem failures."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import article_ai_processor
import facebook_publisher
import job_core
import runtime_state
import state_io


class StateIOTests(unittest.TestCase):
    def test_atomic_checkpoint_round_trip_and_cleanup(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "data" / "state.json"
            state_io.atomic_write_json(path, {"seen": ["وظيفة/1"]})
            self.assertEqual(json.loads(path.read_text()), {"seen": ["وظيفة/1"]})
            state_io.atomic_write_json(path, {"seen": ["وظيفة/1", "وظيفة/2"]})
            self.assertEqual(len(json.loads(path.read_text())["seen"]), 2)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_serialization_preserves_last_checkpoint(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            state_io.atomic_write_json(path, {"seen": ["job-1"]})
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                state_io.atomic_write_json(path, {"bad": object()})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_disk_and_replace_failures_preserve_last_checkpoint(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                state_io.atomic_write_json(path, {"seen": ["job-1"]})
                before = path.read_bytes()
                with patch.object(state_io.os, operation, side_effect=OSError("disk unavailable")):
                    with self.assertRaises(OSError):
                        state_io.atomic_write_json(path, {"seen": ["job-2"]})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_crawl_cursor_survives_failed_save(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "crawl.json"
            with patch.object(runtime_state, "CRAWL_STATE_PATH", path):
                runtime_state.update_source_crawl("official", job_seen_ids=["job-1"], job_discovery_resume={"page": 3})
                with patch.object(state_io.os, "replace", side_effect=OSError("interrupted")):
                    with self.assertRaises(OSError):
                        runtime_state.update_source_crawl("official", job_seen_ids=["job-2"])
                saved = runtime_state.source_crawl_record("official")
                self.assertEqual(saved["job_seen_ids"], ["job-1"])
                self.assertEqual(saved["job_discovery_resume"], {"page": 3})

    def test_identity_memory_survives_failed_save(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            job_core._save_json(path, {"campaign_id": "published-job"})
            with patch.object(state_io.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    job_core._save_json(path, {"campaign_id": "new-job"})
            self.assertEqual(job_core._load_json(path), {"campaign_id": "published-job"})

    def test_provider_and_facebook_memory_keep_prior_state_on_soft_failure(self):
        for module, setting, writer in (
            (article_ai_processor, "AI_PROVIDER_MEMORY_PATH", article_ai_processor._save_ai_memory),
            (facebook_publisher, "FACEBOOK_STYLE_MEMORY_PATH", facebook_publisher._save_style_memory),
        ):
            with self.subTest(module=module.__name__), TemporaryDirectory() as temporary:
                path = Path(temporary) / "memory.json"
                with patch.object(module, setting, path):
                    writer({"cooldowns": {"provider": "later"}})
                    with patch.object(state_io.os, "replace", side_effect=OSError("interrupted")):
                        writer({"cooldowns": {}})
                self.assertEqual(json.loads(path.read_text()), {"cooldowns": {"provider": "later"}})
