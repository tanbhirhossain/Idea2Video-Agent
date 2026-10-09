"""Edge TTS synthesis with transient-service retries and validated audio files."""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path

import edge_tts


def _clean_text(text: str) -> str:
    text = str(text or "")
    text = "".join(char for char in text if char.isprintable() or char in "\n\r\t")
    return re.sub(r"\s+", " ", text).strip()


def synth(text: str, voice: str, rate: str, mp3_path, wav_path, retries: int = 2):
    """Synthesize speech, retrying transient Edge TTS failures up to ``retries`` times."""
    text = _clean_text(text)
    if not text:
        raise ValueError("Cannot synthesize empty narration text.")
    if not str(voice or "").strip():
        raise ValueError("Text-to-speech voice is not configured.")

    mp3_path, wav_path = Path(mp3_path), Path(wav_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    partial_mp3 = mp3_path.with_name(f"{mp3_path.stem}.partial{mp3_path.suffix or '.mp3'}")
    retries = max(0, int(retries))
    last_error = None

    for attempt in range(retries + 1):
        partial_mp3.unlink(missing_ok=True)
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            asyncio.run(communicate.save(str(partial_mp3)))
            if not partial_mp3.is_file() or partial_mp3.stat().st_size < 128:
                raise RuntimeError("Edge TTS returned an empty or incomplete audio file.")
            partial_mp3.replace(mp3_path)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            partial_mp3.unlink(missing_ok=True)
            if attempt >= retries:
                raise RuntimeError(
                    f"Edge TTS produced no usable audio after {retries + 1} attempts "
                    f"(voice={voice!r}, rate={rate!r}, text={len(text)} characters). "
                    "Check the voice name and internet connection, then retry."
                ) from exc
            time.sleep(min(2 ** attempt, 4))

    if last_error is not None:
        raise RuntimeError(f"Edge TTS synthesis failed: {last_error}") from last_error

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path), "-ar", "44100", "-ac", "2", str(wav_path)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"FFmpeg could not convert the synthesized MP3 to WAV: {detail}") from exc
    if not wav_path.is_file() or wav_path.stat().st_size < 128:
        raise RuntimeError("FFmpeg created an empty narration WAV file.")


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())
