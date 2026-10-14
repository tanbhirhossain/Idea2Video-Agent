"""Resilient speech synthesis: Microsoft Edge TTS with Google TTS fallback."""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path

import edge_tts


_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s'\"<>]+")
_RATE_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")


def _clean_text(text: str) -> str:
    text = str(text or "")
    text = "".join(char for char in text if char.isprintable() or char in "\n\r\t")
    return re.sub(r"\s+", " ", text).strip()


def _error_detail(exc: Exception) -> str:
    """Keep useful provider errors without exposing signed request URLs or query text."""
    message = _URL_RE.sub("[speech service URL]", str(exc).strip())
    if len(message) > 320:
        message = message[:317] + "..."
    return f"{type(exc).__name__}: {message or 'no details'}"


def _is_edge_access_denied(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    return status == 403 or (type(exc).__name__ == "WSServerHandshakeError" and re.search(r"\b403\b", str(exc)))


def _synthesize_google(text: str, voice: str, output_path: Path) -> str:
    """Use Google Translate TTS as a last resort, mapping Edge locales to gTTS codes."""
    try:
        from gtts import gTTS
        from gtts.lang import tts_langs
    except ImportError as exc:
        raise RuntimeError(
            "Google TTS fallback is not installed. Run `python -m pip install -r requirements.txt`, "
            "then restart Idea2Video Studio."
        ) from exc

    language = str(voice or "").split("-", 1)[0].casefold()
    supported = tts_langs()
    if language not in supported:
        raise ValueError(f"Google TTS has no fallback for the voice locale {language!r}.")
    gTTS(text=text, lang=language, tld="com").save(str(output_path))
    return language


def _fallback_tempo(rate: str):
    """Approximate Edge's percentage rate in FFmpeg when the Google voice is used."""
    match = _RATE_RE.fullmatch(str(rate or "").strip())
    if not match:
        return None
    tempo = max(0.5, min(2.0, 1.0 + float(match.group(1)) / 100.0))
    return None if abs(tempo - 1.0) < 0.001 else tempo


def synth(text: str, voice: str, rate: str, mp3_path, wav_path, retries: int = 2):
    """Synthesize with Edge TTS, then fall back to Google TTS if Edge returns no audio.

    The return value is the provider used (``"edge"`` or ``"google"``), so the
    pipeline can record and report a fallback without changing its audio-file API.
    """
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
    edge_error = None
    edge_attempts = 0
    provider = "edge"

    for attempt in range(retries + 1):
        edge_attempts = attempt + 1
        partial_mp3.unlink(missing_ok=True)
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            asyncio.run(communicate.save(str(partial_mp3)))
            if not partial_mp3.is_file() or partial_mp3.stat().st_size < 128:
                raise RuntimeError("Edge TTS returned an empty or incomplete audio file.")
            partial_mp3.replace(mp3_path)
            edge_error = None
            break
        except Exception as exc:
            edge_error = exc
            partial_mp3.unlink(missing_ok=True)
            # A 403 is a provider handshake rejection, not a bad narration; repeating
            # the same connection three times cannot repair it, so switch providers.
            if attempt >= retries or _is_edge_access_denied(exc):
                break
            time.sleep(min(2 ** attempt, 4))

    if edge_error is not None:
        provider = "google"
        try:
            language = _synthesize_google(text, voice, partial_mp3)
            if not partial_mp3.is_file() or partial_mp3.stat().st_size < 128:
                raise RuntimeError("Google TTS returned an empty or incomplete audio file.")
            partial_mp3.replace(mp3_path)
        except Exception as fallback_error:
            partial_mp3.unlink(missing_ok=True)
            raise RuntimeError(
                f"Text-to-speech failed for voice={voice!r}, rate={rate!r}, text={len(text)} characters. "
                f"Microsoft Edge TTS failed after {edge_attempts} attempt(s): {_error_detail(edge_error)}. "
                f"Google TTS fallback failed: {_error_detail(fallback_error)}. "
                "Check outbound internet access to both speech services."
            ) from fallback_error

    command = ["ffmpeg", "-y", "-i", str(mp3_path)]
    tempo = _fallback_tempo(rate) if provider == "google" else None
    if tempo is not None:
        command.extend(["-filter:a", f"atempo={tempo:.4f}"])
    command.extend(["-ar", "44100", "-ac", "2", str(wav_path)])
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"FFmpeg could not convert the synthesized MP3 to WAV: {detail}") from exc
    if not wav_path.is_file() or wav_path.stat().st_size < 128:
        raise RuntimeError("FFmpeg created an empty narration WAV file.")
    return provider


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())
