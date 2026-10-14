import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core import tts


class TtsTests(unittest.TestCase):
    def test_transient_no_audio_response_is_retried_and_converted(self):
        class FlakyCommunicate:
            calls = 0

            def __init__(self, text, voice, rate):
                self.text = text
                self.voice = voice
                self.rate = rate

            async def save(self, path):
                type(self).calls += 1
                if type(self).calls < 3:
                    raise RuntimeError("No audio was received.")
                Path(path).write_bytes(b"ID3" + b"x" * 200)

        def fake_ffmpeg(command, **kwargs):
            Path(command[-1]).write_bytes(b"RIFF" + b"x" * 200)

        with tempfile.TemporaryDirectory() as temporary:
            mp3 = Path(temporary) / "voice.mp3"
            wav = Path(temporary) / "voice.wav"
            with patch("core.tts.edge_tts.Communicate", FlakyCommunicate), \
                 patch("core.tts.subprocess.run", side_effect=fake_ffmpeg), \
                 patch("core.tts.time.sleep"):
                tts.synth("A short narration.", "en-US-AndrewMultilingualNeural", "+0%", mp3, wav)
            self.assertEqual(FlakyCommunicate.calls, 3)
            self.assertGreater(mp3.stat().st_size, 128)
            self.assertGreater(wav.stat().st_size, 128)
            self.assertFalse((Path(temporary) / "voice.partial.mp3").exists())

    def test_edge_403_falls_back_to_google_and_applies_requested_rate(self):
        class WSServerHandshakeError(RuntimeError):
            status = 403

        class ForbiddenCommunicate:
            calls = 0

            def __init__(self, text, voice, rate):
                pass

            async def save(self, path):
                type(self).calls += 1
                raise WSServerHandshakeError("403, Invalid response status")

        def fake_google(text, voice, path):
            self.assertEqual(text, "A short narration.")
            self.assertEqual(voice, "en-US-AndrewNeural")
            Path(path).write_bytes(b"ID3" + b"x" * 200)
            return "en"

        commands = []

        def fake_ffmpeg(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"RIFF" + b"x" * 200)

        with tempfile.TemporaryDirectory() as temporary:
            mp3 = Path(temporary) / "voice.mp3"
            wav = Path(temporary) / "voice.wav"
            with patch("core.tts.edge_tts.Communicate", ForbiddenCommunicate), \
                 patch("core.tts._synthesize_google", side_effect=fake_google) as google, \
                 patch("core.tts.subprocess.run", side_effect=fake_ffmpeg):
                provider = tts.synth("A short narration.", "en-US-AndrewNeural", "-5%", mp3, wav)
            self.assertEqual(provider, "google")
            self.assertEqual(ForbiddenCommunicate.calls, 1)
            google.assert_called_once()
            self.assertIn("atempo=0.9500", commands[0])
            self.assertGreater(mp3.stat().st_size, 128)
            self.assertGreater(wav.stat().st_size, 128)

    def test_missing_google_tts_dependency_shows_exact_install_command(self):
        with patch.dict("sys.modules", {"gtts": None}):
            with self.assertRaisesRegex(RuntimeError, r"python -m pip install -r requirements\.txt"):
                tts._synthesize_google("Hello there", "en-US-AndrewNeural", Path("voice.mp3"))

    def test_google_fallback_maps_bangla_voice_to_bengali(self):
        from types import SimpleNamespace

        fake_gtts = MagicMock()
        modules = {
            "gtts": SimpleNamespace(gTTS=fake_gtts),
            "gtts.lang": SimpleNamespace(tts_langs=lambda: {"en": "English", "bn": "Bengali"}),
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.partial.mp3"
            with patch.dict("sys.modules", modules):
                language = tts._synthesize_google("বাংলা কথা", "bn-BD-NabanitaNeural", output)
        self.assertEqual(language, "bn")
        fake_gtts.assert_called_once_with(text="বাংলা কথা", lang="bn", tld="com")
        fake_gtts.return_value.save.assert_called_once_with(str(output))

    def test_persistent_no_audio_error_names_voice_and_text_length(self):
        class SilentCommunicate:
            def __init__(self, text, voice, rate):
                pass

            async def save(self, path):
                raise RuntimeError("No audio was received.")

        with tempfile.TemporaryDirectory() as temporary:
            with patch("core.tts.edge_tts.Communicate", SilentCommunicate), \
                 patch("core.tts._synthesize_google", side_effect=RuntimeError("Google is unavailable")), \
                 patch("core.tts.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "voice='test-voice'.*text=11 characters") as caught:
                    tts.synth("Hello world", "test-voice", "+0%",
                              Path(temporary) / "voice.mp3", Path(temporary) / "voice.wav", retries=1)
        self.assertIn("Microsoft Edge TTS failed", str(caught.exception))
        self.assertIn("Google TTS fallback failed", str(caught.exception))
        self.assertIn("No audio was received", str(caught.exception))

    def test_empty_narration_is_rejected_before_tts_request(self):
        with patch("core.tts.edge_tts.Communicate") as communicate:
            with self.assertRaisesRegex(ValueError, "empty narration"):
                tts.synth("  \n  ", "test-voice", "+0%", "voice.mp3", "voice.wav")
        communicate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
