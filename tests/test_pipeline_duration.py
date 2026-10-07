import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.pipeline import build
from core.script_gen import script_content_hash


def generated_script(title, texts, revisions=0):
    script = {
        "title": title,
        "scenes": [
            {"narration": text, "visual_prompt": f"A cinematic scene for {text}"}
            for text in texts
        ],
    }
    script["_generator"] = {
        "version": 2,
        "requested_seconds": 65.0,
        "narration_target_seconds": 60.5,
        "words_per_minute": 190,
        "word_budget": 192,
        "duration_revisions": revisions,
        "language": "English",
        "mode": "image",
        "format": "shorts",
        "source_hash": hashlib.sha256(b"test idea").hexdigest(),
        "content_hash": script_content_hash(script),
    }
    return script


class PipelineDurationTests(unittest.TestCase):
    def test_measured_tts_duration_triggers_script_rewrite_before_render(self):
        initial = generated_script("Measured story", ["short opening", "short ending"])
        revised = generated_script("Measured story", ["expanded opening", "complete resolution"], revisions=1)
        seconds_by_text = {
            "short opening": 20.0,
            "short ending": 20.0,
            "expanded opening": 30.0,
            "complete resolution": 30.5,
        }
        config = {
            "formats": {"shorts": [1080, 1920]},
            "comfy": {"host": "http://127.0.0.1:8188", "timeout_s": 10, "image": {"map": {}}},
            "tts": {"voice": "test-voice", "rate": "+0%"},
            "video": {
                "scene_seconds": 6,
                "narration_wpm": 190,
                "duration_tolerance_s": 2,
                "duration_tolerance_ratio": 0.04,
                "transition_s": 0.5,
                "transitions": ["fade"],
                "fps": 30,
                "style": "cinematic",
                "style_video": "cinematic live action",
                "music": False,
                "music_volume": 0.15,
                "end_screen": {"enabled": True, "channel": "A channel", "seconds": 4},
            },
            "subtitles": {"font": "Arial", "words_per_cue": 4, "uppercase": False},
        }
        logs = []

        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            fake_comfy = MagicMock()
            fake_comfy.resolve_aspect.return_value = "9:16"

            def fake_generate(spec, values, out_stem):
                visual = Path(out_stem).with_suffix(".png")
                visual.write_bytes(b"image")
                return visual

            fake_comfy.generate.side_effect = fake_generate

            def fake_synth(text, voice, rate, mp3, wav):
                Path(mp3).write_bytes(b"mp3")
                Path(wav).write_text(text, encoding="utf-8")

            def fake_assemble(job_path, clips, wavs, durations, ass, final_name, *args, **kwargs):
                (Path(job_path) / final_name).write_bytes(b"mp4")

            with patch("core.pipeline.make_script", return_value=initial), \
                 patch("core.pipeline.revise_script_for_duration", return_value=revised) as revise, \
                 patch("core.pipeline.ComfyClient", return_value=fake_comfy), \
                 patch("core.pipeline.tts.synth", side_effect=fake_synth), \
                 patch("core.pipeline.tts.probe_duration", side_effect=lambda path: seconds_by_text[Path(path).read_text(encoding="utf-8")]), \
                 patch("core.pipeline.render.scene_clip") as scene_clip, \
                 patch("core.pipeline.render.assemble", side_effect=fake_assemble), \
                 patch("core.pipeline.render.duration_of", return_value=65.0):
                final = build(config, job, "test idea", "image", "shorts", seconds=65,
                              language="English", log=logs.append)

        revise.assert_called_once()
        self.assertEqual(scene_clip.call_count, 2)
        self.assertEqual(final.name, "Measured_story_1080x1920.mp4")
        self.assertTrue(any("measured narration is 40.0s" in line for line in logs))
        self.assertTrue(any("narration fitted to 60.5s" in line for line in logs))
        self.assertTrue(any("final duration 65.0s" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
