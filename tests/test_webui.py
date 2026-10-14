import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webui


class WebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = webui.app.test_client()

    def test_studio_and_compiled_tailwind_asset_are_served_locally(self):
        page = self.client.get("/")
        css = self.client.get("/static/app.css")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/static/app.css", page.data)
        self.assertEqual(css.status_code, 200)
        self.assertIn(b"tailwindcss v4", css.data)
        page.close()
        css.close()

    def test_generate_rejects_duration_that_cannot_fit_the_outro(self):
        response = self.client.post("/api/generate", json={"idea": "A short story", "seconds": 5})
        self.assertEqual(response.status_code, 400)
        self.assertIn("transition/outro", response.get_json()["error"])

    def test_single_create_is_saved_as_a_background_queue_job(self):
        created = [{"id": 123456, "job": "job_123456"}]
        with patch("webui.tasks.add_ideas", return_value=created) as add, patch.object(webui.SCHED, "start") as start:
            response = self.client.post("/api/generate", json={
                "idea": "How a small idea changed a city",
                "mode": "image", "format": "shorts", "seconds": 65,
                "language": "English", "subtitle_language": "auto",
            })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job"], "job_123456")
        self.assertEqual(response.get_json()["status"], "queued")
        add.assert_called_once()
        self.assertEqual(add.call_args.kwargs["origin"], "single")
        self.assertEqual(add.call_args.kwargs["subtitle_language"], "auto")
        start.assert_called_once()

    def test_single_create_accepts_bangla_and_english_subtitles(self):
        created = [{"id": 9876, "job": "job_9876"}]
        with patch("webui.tasks.add_ideas", return_value=created) as add, patch.object(webui.SCHED, "start"):
            response = self.client.post("/api/generate", json={
                "idea": "A Bangla story", "seconds": 65, "language": "Bangla",
                "subtitle_language": "English",
            })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(add.call_args.kwargs["language"], "Bangla")
        self.assertEqual(add.call_args.kwargs["subtitle_language"], "English")

    def test_invalid_language_is_rejected(self):
        response = self.client.post("/api/generate", json={
            "idea": "A story", "seconds": 65, "language": "Klingon",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("English or Bangla", response.get_json()["error"])

    def test_status_endpoint_resolves_persisted_job_after_page_reload(self):
        with patch("webui.tasks.get_idea_by_job", return_value={
            "job": "job_123", "status": "processing", "progress": 47,
            "stage": "Scene visuals · 4/8", "logs": ["scene 4/8: generating image ..."],
            "video": None, "error": None,
        }):
            response = self.client.get("/api/status/job_123")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["progress"], 47)
        self.assertIn("Scene visuals", data["stage"])

    def test_video_route_supports_http_byte_ranges(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "output" / "job_video_test"
            project.mkdir(parents=True)
            content = bytes(range(256)) * 8
            (project / "finished.mp4").write_bytes(content)
            with patch("webui.ROOT", root):
                response = self.client.get(
                    "/api/video/job_video_test/finished.mp4",
                    headers={"Range": "bytes=20-49"},
                )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, content[20:50])
            self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")
            response.close()

    def test_video_workflow_diagnostics_endpoint_returns_comfy_report(self):
        report = {"ok": False, "version": "0.34.0", "errors": ["missing H3 VAE"], "warnings": [], "missing_models": []}
        with patch.object(webui.ComfyClient, "diagnostics", return_value=report):
            response = self.client.post("/api/admin/test-workflow", json={"mode": "video"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), report)

    def test_studio_exposes_progress_bangla_caption_and_seo_controls(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"genProgressBar", response.data)
        self.assertIn(b"iSubtitleLanguage", response.data)
        self.assertIn(b"Bangla", response.data)
        self.assertIn(b"testVideoWorkflowBtn", response.data)
        self.assertIn(b"SEO metadata", response.data)
        self.assertIn(b"copy-seo", response.data)

    def test_dashboard_refresh_preserves_video_element_when_project_list_is_unchanged(self):
        response = self.client.get("/")
        self.assertIn(b"dashJobs.dataset.signature !== signature", response.data)
        self.assertIn(b"dashboardVideoRefreshDeferred", response.data)


if __name__ == "__main__":
    unittest.main()
