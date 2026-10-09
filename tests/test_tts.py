import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_persistent_no_audio_error_names_voice_and_text_length(self):
        class SilentCommunicate:
            def __init__(self, text, voice, rate):
                pass

            async def save(self, path):
                raise RuntimeError("No audio was received.")

        with tempfile.TemporaryDirectory() as temporary:
            with patch("core.tts.edge_tts.Communicate", SilentCommunicate), patch("core.tts.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "voice='test-voice'.*text=11 characters"):
                    tts.synth("Hello world", "test-voice", "+0%",
                              Path(temporary) / "voice.mp3", Path(temporary) / "voice.wav", retries=1)

    def test_empty_narration_is_rejected_before_tts_request(self):
        with patch("core.tts.edge_tts.Communicate") as communicate:
            with self.assertRaisesRegex(ValueError, "empty narration"):
                tts.synth("  \n  ", "test-voice", "+0%", "voice.mp3", "voice.wav")
        communicate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
