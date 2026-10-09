import unittest
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

    def test_65_second_request_is_accepted_without_waiting_for_generation(self):
        with patch("webui.threading.Thread.start", lambda thread: None):
            response = self.client.post("/api/generate", json={
                "idea": "How a small idea changed a city",
                "mode": "image",
                "format": "shorts",
                "seconds": 65,
                "language": "English",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["job"].startswith("job_"))


if __name__ == "__main__":
    unittest.main()
