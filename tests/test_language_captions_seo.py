import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.captions import prepare_captions
from core.language import normalize_language, resolve_subtitle_language, resolve_voice, resolve_voice_rate
from core.pipeline import prepare_voiceover
from core.script_gen import make_script, translate_subtitles
from core.seo import fallback_metadata, normalize_metadata
from core.subtitles import build_ass


class LanguageAndCaptionTests(unittest.TestCase):
    def test_bangla_language_automatically_uses_bangla_edge_voice(self):
        config = {"tts": {"voice": "custom-en", "bangla_voice": "custom-bn"}}
        self.assertEqual(normalize_language("Bengali"), "Bangla")
        self.assertEqual(resolve_voice(config, "Bangla"), "custom-bn")
        self.assertEqual(resolve_voice(config, "English"), "custom-en")
        self.assertEqual(resolve_voice_rate({"tts": {"rate": "+0%", "bangla_rate": "-7%"}}, "Bangla"), "-7%")
        self.assertEqual(resolve_voice_rate({"tts": {"rate": "+0%"}}, "Bangla"), "-5%")
        self.assertEqual(resolve_voice_rate({"tts": {"rate": "+2%"}}, "English"), "+2%")
        self.assertEqual(resolve_subtitle_language("Bangla", "auto"), "Bangla")
        self.assertEqual(resolve_subtitle_language("Bangla", "English"), "English")

    def test_bangla_voiceover_uses_natural_voice_and_slow_rate_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = []

            def fake_synth(text, voice, rate, mp3_path, wav_path):
                calls.append((voice, rate))
                Path(mp3_path).write_bytes(b"mp3")
                Path(wav_path).write_bytes(b"wav")

            config = {"tts": {"bangla_voice": "bn-BD-NabanitaNeural", "bangla_rate": "-6%"}}
            with patch("core.pipeline.tts.synth", side_effect=fake_synth), \
                 patch("core.pipeline.tts.probe_duration", return_value=3.5):
                durations, _ = prepare_voiceover(Path(temporary), [{"narration": "বাংলায় একটি ছোট গল্প।"}],
                                                 config, log=lambda _: None, language="Bangla")
        self.assertEqual(calls, [("bn-BD-NabanitaNeural", "-6%")])
        self.assertEqual(durations, [3.5])

    def test_google_fallback_provider_is_recorded_for_cached_bangla_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = Path(temporary)
            logs = []

            def fake_synth(text, voice, rate, mp3_path, wav_path):
                Path(mp3_path).write_bytes(b"mp3")
                Path(wav_path).write_bytes(b"wav")
                return "google"

            config = {"tts": {"bangla_voice": "bn-BD-NabanitaNeural", "bangla_rate": "-5%"}}
            with patch("core.pipeline.tts.synth", side_effect=fake_synth), \
                 patch("core.pipeline.tts.probe_duration", return_value=3.5):
                prepare_voiceover(job, [{"narration": "বাংলায় একটি ছোট গল্প।"}], config,
                                  log=logs.append, language="Bangla")
            meta = json.loads((job / "a000.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["provider"], "google")
        self.assertTrue(any("Google TTS fallback (locale=bn)" in message for message in logs))

    def test_script_prompt_requests_bangla_script_and_keeps_visuals_english(self):
        cfg = {
            "formats": {"shorts": [1080, 1920]},
            "video": {"scene_seconds": 6, "narration_wpm": 190, "bangla_narration_wpm": 155},
            "ollama": {"host": "http://ollama", "model": "test", "num_predict": 8192},
        }
        generated = {"title": "বাংলার গল্প", "scenes": [
            {"narration": "বাংলা ভাষার গল্প শুরু হলো।", "visual_prompt": "A documentary scene in Dhaka."},
            {"narration": "গল্পটি একটি সিদ্ধান্তে শেষ হয়।", "visual_prompt": "A quiet street at sunset."},
        ]}
        with patch("core.script_gen._call_ollama", return_value=generated) as call:
            script = make_script(cfg, "A story", "image", "shorts", 15,
                                 "Bangla", narration_seconds=10)
        self.assertEqual(script["_generator"]["language"], "Bangla")
        self.assertEqual(script["_generator"]["words_per_minute"], 155)
        self.assertIn("written in বাংলা script", call.call_args.args[1])
        self.assertIn("natural Bengali punctuation and pauses", call.call_args.args[1])
        self.assertIn("spoken aloud", call.call_args.args[1])
        self.assertIn("visual_prompt\" must be in English", call.call_args.args[1])

    def test_subtitle_translation_preserves_scene_count_and_bangla_script(self):
        scenes = [{"narration": "A river runs through the town."}, {"narration": "People cross the old bridge."}]
        response = {"scenes": [{"text": "শহরের ভেতর দিয়ে একটি নদী বয়ে যায়।"},
                               {"text": "মানুষ পুরোনো সেতুটি পার হয়।"}]}
        with patch("core.script_gen._call_ollama", return_value=response) as call:
            captions = translate_subtitles({}, scenes, "English", "Bangla")
        self.assertEqual(len(captions), len(scenes))
        self.assertIn("শহরের", captions[0])
        self.assertIn("Bengali script", call.call_args.args[1])

    def test_captions_are_cached_by_script_and_language(self):
        script = {"title": "Story", "scenes": [
            {"narration": "An old bridge connects two villages.", "visual_prompt": "A stone bridge."},
            {"narration": "The river floods every summer.", "visual_prompt": "A flooded river."},
        ]}
        translated = ["একটি পুরোনো সেতু দুটি গ্রামকে যুক্ত করে।", "প্রতি গ্রীষ্মে নদীতে বন্যা হয়।"]
        with tempfile.TemporaryDirectory() as temporary:
            with patch("core.captions.translate_subtitles", return_value=translated) as translate:
                first, language = prepare_captions(Path(temporary), {}, script, "English", "Bangla", log=lambda _: None)
                second, _ = prepare_captions(Path(temporary), {}, script, "English", "Bangla", log=lambda _: None)
            self.assertEqual(first, second)
            self.assertEqual(language, "Bangla")
            translate.assert_called_once()
            saved = json.loads((Path(temporary) / "subtitles.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["target_language"], "Bangla")
            self.assertEqual(saved["version"], 2)

    def test_ass_captions_use_bangla_font_and_translated_text(self):
        config = {"subtitles": {"font": "Arial", "font_bangla": "Nirmala UI",
                                "words_per_cue": 4, "uppercase": False}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captions.ass"
            build_ass([{"narration": "Old English narration."}], [3.0], 1080, 1920,
                      config, output, captions=["বাংলা সাবটাইটেল এখানে।"], language="Bangla")
            text = output.read_text(encoding="utf-8")
        self.assertIn("Style: Default,Nirmala UI", text)
        self.assertIn("বাংলা সাবটাইটেল", text)
        self.assertNotIn("Old English narration", text)

    def test_bangla_subtitles_wrap_at_word_boundaries_and_use_readable_cues(self):
        config = {"subtitles": {"font_bangla": "Nirmala UI", "words_per_cue_bangla": 6,
                                "max_chars_per_line_bangla": 14, "uppercase": True}}
        caption = "মানুষ পুরোনো সেতুটি পার হয়ে নদীর ওপারে নতুন গ্রামের দিকে যায়।"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captions.ass"
            build_ass([{"narration": caption}], [6.0], 1080, 1920, config,
                      output, captions=[caption], language="Bangla")
            text = output.read_text(encoding="utf-8")
        self.assertIn(r"\N", text)
        self.assertGreaterEqual(text.count("Dialogue:"), 2)
        self.assertIn("গ্রামের", text)
        self.assertIn("Style: Default,Nirmala UI", text)


class SeoMetadataTests(unittest.TestCase):
    def test_platform_metadata_is_normalized_and_fallback_is_complete(self):
        script = {"title": "A river story", "scenes": [{"narration": "A river shaped the town."}]}
        data = normalize_metadata({
            "YouTube": {"title": "A river shaped this town", "description": "A concise story.", "tags": ["river", "history"]},
            "Facebook": {"title": "The river and the town", "description": "A social post.", "tags": "river, community"},
        }, script)
        self.assertIn("youtube", data["platforms"])
        self.assertEqual(len(data["platforms"]["facebook"]["tags"]), 2)
        fallback = fallback_metadata(script)
        self.assertEqual(set(fallback["platforms"]), {"youtube", "facebook"})
        self.assertTrue(fallback["platforms"]["youtube"]["tags"])


if __name__ == "__main__":
    unittest.main()
