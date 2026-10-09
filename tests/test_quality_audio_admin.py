import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import webui
from core import render
from core.script_gen import REVISION_TEMPLATE, SCRIPT_TEMPLATE, script_content_hash


class AudioAndQualityTests(unittest.TestCase):
    def test_music_graph_normalizes_fades_ducks_and_mixes_music_under_voice(self):
        graph = render._music_audio_graph(2, 3, 0.65, 63.0, "apad=pad_dur=4.000,loudnorm=I=-16")
        self.assertIn("loudnorm=I=-18", graph)
        self.assertIn("sidechaincompress=threshold=0.12:ratio=3", graph)
        self.assertIn("volume=0.650", graph)
        self.assertIn("afade=t=in", graph)
        self.assertIn("afade=t=out:st=63.000", graph)
        self.assertIn("[nar]asplit=2[nar_sc][nar_mix]", graph)
        self.assertIn("[mus][nar_sc]sidechaincompress", graph)
        self.assertIn("[nar_mix][ducked]amix=inputs=2", graph)
        self.assertIn("alimiter=limit=0.95", graph)

    def test_assemble_routes_the_looped_track_through_the_voice_ducker(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            (job / "voice.wav").write_bytes(b"wav")
            (job / "scene.mp4").write_bytes(b"mp4")
            music = job / "music.mp3"
            music.write_bytes(b"mp3")
            with patch("core.render.run") as mocked_run:
                render.assemble(
                    job, [job / "scene.mp4"], [job / "voice.wav"], [5.0], "captions.ass",
                    "final.mp4", 0.5, ["fade"], music_path=music, music_volume=0.65,
                )
            self.assertEqual(mocked_run.call_count, 2)
            cmd = mocked_run.call_args_list[1].args[0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("-stream_loop", cmd)
            self.assertIn("sidechaincompress", graph)
            self.assertIn("[aout]", graph)
            self.assertIn("-crf", cmd)
            self.assertIn("192k", cmd)

    def test_music_picker_skips_files_without_audio_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "silent.mp3").write_bytes(b"bad")
            (folder / "usable.mp3").write_bytes(b"audio")
            with patch("core.render._contains_audio", side_effect=lambda p: p.name == "usable.mp3"), \
                 patch("core.render.random.choice", side_effect=lambda choices: choices[0]):
                selected = render.pick_music(folder)
            self.assertEqual(selected.name, "usable.mp3")

    def test_scene_encode_uses_high_quality_scaling_and_codec_settings(self):
        with patch("core.render.run") as mocked_run:
            render.scene_clip("input.png", "image", 4, 1080, 1920, 30, "scene.mp4", 0)
        cmd = mocked_run.call_args.args[0]
        self.assertIn("flags=lanczos", cmd[cmd.index("-vf") + 1])
        self.assertIn("unsharp=", cmd[cmd.index("-vf") + 1])
        self.assertEqual(cmd[cmd.index("-crf") + 1], "16")
        self.assertEqual(cmd[cmd.index("-preset") + 1], "slow")

    def test_script_prompts_require_short_plain_language_for_general_audience(self):
        self.assertIn("first 15 percent", SCRIPT_TEMPLATE)
        self.assertIn("next 70 percent", SCRIPT_TEMPLATE)
        self.assertIn("final 15 percent", SCRIPT_TEMPLATE)
        self.assertIn("Assume the viewer has no background knowledge", SCRIPT_TEMPLATE)
        self.assertIn("short but meaningful", REVISION_TEMPLATE)


class AdminConfigApiTests(unittest.TestCase):
    def setUp(self):
        self.client = webui.app.test_client()
        self.config = {
            "ollama": {"host": "http://localhost:11434", "model": "old:latest", "think": False},
            "comfy": {
                "host": "http://localhost:8188", "timeout_s": 1800,
                "image": {"fixed": {"megapixels": 2}},
                "video": {"fixed": {"megapixels": 2}},
            },
            "tts": {},
            "video": {},
        }

    def test_admin_api_saves_ollama_comfyui_and_resolution_settings(self):
        with patch("webui.cfg", return_value=self.config), patch("webui.save_cfg") as save:
            response = self.client.post("/api/config", json={
                "ollama": {"host": "ollama.internal:11434/", "model": "qwen3.5:latest", "think": False, "num_predict": 10000},
                "comfy": {
                    "host": "https://comfy.example/api/", "timeout_s": 7201,
                    "image": {"megapixels": 3.5}, "video": {"megapixels": 0.5},
                },
            })
        self.assertEqual(response.status_code, 200)
        saved = save.call_args.args[0]
        self.assertEqual(saved["ollama"]["host"], "http://ollama.internal:11434")
        self.assertEqual(saved["ollama"]["model"], "qwen3.5:latest")
        self.assertEqual(saved["ollama"]["num_predict"], 10000)
        self.assertEqual(saved["comfy"]["host"], "https://comfy.example/api")
        self.assertEqual(saved["comfy"]["timeout_s"], 7200)
        self.assertEqual(saved["comfy"]["image"]["fixed"]["megapixels"], 3.5)
        self.assertEqual(saved["comfy"]["video"]["fixed"]["megapixels"], 1.0)

    def test_admin_api_rejects_invalid_service_url_without_saving(self):
        with patch("webui.cfg", return_value=self.config), patch("webui.save_cfg") as save:
            response = self.client.post("/api/config", json={"ollama": {"host": "not a url/"}})
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_admin_connection_test_checks_selected_ollama_model_and_comfyui(self):
        ollama_response = MagicMock()
        ollama_response.json.return_value = {"models": [{"name": "qwen3.5:latest"}]}
        comfy_response = MagicMock()
        with patch("webui.cfg", return_value={
            "ollama": {"host": "http://ollama.test", "model": "qwen3.5:latest"},
            "comfy": {"host": "http://comfy.test"},
        }), patch("webui.requests.get", side_effect=[ollama_response, comfy_response]) as get:
            response = self.client.post("/api/admin/test-connections", json={})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["ollama"]["ok"])
        self.assertTrue(result["ollama"]["model_installed"])
        self.assertTrue(result["comfy"]["ok"])
        self.assertEqual(get.call_count, 2)

    def test_rerender_corrects_duration_from_saved_script_before_export(self):
        job_name = "job_rerender_duration_test"
        config = {
            "formats": {"shorts": [1080, 1920]},
            "tts": {"voice": "voice", "rate": "+0%"},
            "video": {
                "transition_s": 0.5,
                "transitions": ["fade"],
                "fps": 30,
                "music": False,
                "end_screen": {"enabled": True, "channel": "Channel", "seconds": 4},
                "duration_tolerance_s": 2,
                "duration_tolerance_ratio": 0.04,
            },
        }
        initial_script = {
            "title": "Saved story",
            "scenes": [
                {"narration": "Old opening.", "visual_prompt": "A city at dawn."},
                {"narration": "Old ending.", "visual_prompt": "A city at dusk."},
            ],
            "_generator": {
                "version": 2, "requested_seconds": 65, "format": "shorts", "mode": "image",
                "language": "English", "duration_revisions": 2, "source_hash": "original-source-hash",
                "output_size": [1080, 1920],
            },
        }
        initial_script["_generator"]["content_hash"] = script_content_hash(initial_script)
        revised_script = {
            "title": "Saved story",
            "scenes": [
                {"narration": "Corrected opening narration.", "visual_prompt": "A city at dawn."},
                {"narration": "Corrected ending narration.", "visual_prompt": "A city at dusk."},
            ],
            "_generator": {
                "version": 2, "requested_seconds": 65, "format": "shorts", "mode": "image",
                "language": "English", "duration_revisions": 3, "source_hash": "", "output_size": [1080, 1920],
            },
        }
        revised_script["_generator"]["content_hash"] = script_content_hash(revised_script)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / "output" / job_name
            job.mkdir(parents=True)
            (job / "script.json").write_text(json.dumps(initial_script), encoding="utf-8")
            (job / "m000.png").write_bytes(b"image")
            (job / "m001.png").write_bytes(b"image")
            overlong = ([35.0, 35.0], [job / "a000.wav", job / "a001.wav"])
            fitted = ([30.25, 30.25], [job / "a000.wav", job / "a001.wav"])
            with patch("webui.ROOT", root), patch("webui.cfg", return_value=config), \
                 patch("webui.prepare_voiceover", side_effect=[overlong, fitted]) as prepare, \
                 patch("webui.revise_script_for_duration", return_value=revised_script) as revise, \
                 patch("webui.render.scene_clip"), patch("webui.render.assemble"), \
                 patch("webui.render.duration_of", return_value=65.0), \
                 patch("core.subtitles.build_ass"), \
                 patch("webui.threading.Thread.start", lambda thread: thread.run()):
                response = self.client.post(f"/api/job/{job_name}/rerender", json={})
            saved = json.loads((job / "script.json").read_text(encoding="utf-8"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(prepare.call_count, 2)
        revise.assert_called_once()
        self.assertEqual(saved["_generator"]["source_hash"], "original-source-hash")
        self.assertEqual(saved["_generator"]["duration_revisions"], 3)
        self.assertEqual(webui.JOBS[job_name]["status"], "done")
        self.assertTrue(any("narration fitted to 60.5s" in line for line in webui.JOBS[job_name]["log"]))
        webui.JOBS.pop(job_name, None)

    def test_admin_panel_is_available_in_the_studio(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"AI services", response.data)
        self.assertIn(b"adminOllamaModel", response.data)
        self.assertIn(b"adminNumPredict", response.data)
        self.assertIn(b"id=\"sVol\"", response.data)
        self.assertIn(b"max=\"100\" step=\"1\"", response.data)
        self.assertIn(b"50% balanced", response.data)


if __name__ == "__main__":
    unittest.main()
