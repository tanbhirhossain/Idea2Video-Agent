import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tasks


class PersistentTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ideas = root / "ideas.json"
        self.ideas_db = root / "ideas.sqlite3"
        self.scheduler = root / "scheduler.json"
        self.patches = [
            patch.object(tasks, "IDEAS_FILE", self.ideas),
            patch.object(tasks, "IDEAS_DB", self.ideas_db),
            patch.object(tasks, "SCHED_FILE", self.scheduler),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_create_and_progress_are_persisted_atomically(self):
        created = tasks.add_ideas(
            ["First story", "Second story"], mode="image", fmt="shorts", seconds=45,
            language="Bangla", subtitle_language="English", origin="single",
        )
        self.assertEqual(len(created), 2)
        self.assertNotEqual(created[0]["id"], created[1]["id"])
        self.assertEqual(created[0]["job"], f"job_{created[0]['id']}")
        tasks.update_idea(created[0]["id"], status="processing", progress=38, stage="Scene visuals · 3/8")
        loaded = tasks.get_idea_by_job(created[0]["job"])
        self.assertEqual(loaded["progress"], 38)
        self.assertEqual(loaded["subtitle_language"], "English")
        self.assertTrue(self.ideas_db.exists())
        self.assertFalse(self.ideas.with_suffix(".json.tmp").exists())

    def test_existing_json_queue_is_migrated_once_to_sqlite(self):
        self.ideas.write_text(json.dumps([{
            "id": 27, "text": "Legacy story", "status": "pending", "mode": "video",
            "format": "wide", "seconds": 50, "language": "Bangla",
            "subtitle_language": "English", "created": "2025-01-01T10:00:00",
        }]), encoding="utf-8")
        self.assertEqual(tasks.list_ideas()[0]["text"], "Legacy story")
        self.ideas.write_text("[]", encoding="utf-8")
        self.assertEqual(tasks.list_ideas()[0]["id"], 27)
        self.assertGreater(tasks.add_ideas(["New story"])[0]["id"], 27)

    def test_restart_requeues_interrupted_work_for_resume(self):
        item = tasks.add_ideas(["Resume me"], mode="video", seconds=60)[0]
        tasks.update_idea(item["id"], status="processing", progress=51, stage="Scene 4/8")
        self.assertEqual(tasks.recover_interrupted_ideas(), 1)
        restored = tasks.get_idea(item["id"])
        self.assertEqual(restored["status"], "pending")
        self.assertEqual(restored["progress"], 0)
        self.assertIn("resuming", restored["stage"])
        self.assertIn("server restarted", restored["logs"][-1])

    def test_pipeline_logs_map_to_monotonic_progress(self):
        p1, stage1 = tasks._progress_for_log("voice 1/10: synthesizing narration ...", 0)
        p2, stage2 = tasks._progress_for_log("scene 2/10: generating image ...", p1)
        p3, stage3 = tasks._progress_for_log("scene 2/10: clip rendered", p2)
        p4, stage4 = tasks._progress_for_log("render: assembling final video ...", p3)
        self.assertEqual(stage1, "Narration · clip 1/10")
        self.assertEqual(stage2, "Scene visuals · 2/10")
        self.assertGreater(p3, p2)
        self.assertEqual(stage4, "Assembling the final video")
        self.assertGreater(p4, p3)

    def test_processing_idea_cannot_be_deleted_mid_generation(self):
        item = tasks.add_ideas(["Keep me"])[0]
        tasks.update_idea(item["id"], status="processing")
        self.assertFalse(tasks.delete_idea(item["id"]))
        self.assertIsNotNone(tasks.get_idea(item["id"]))


if __name__ == "__main__":
    unittest.main()
