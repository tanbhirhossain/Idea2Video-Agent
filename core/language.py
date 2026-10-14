"""Language choices and automatic Edge TTS voice selection."""
from __future__ import annotations


SUPPORTED_LANGUAGES = {
    "english": "English",
    "en": "English",
    "bangla": "Bangla",
    "bengali": "Bangla",
    "bn": "Bangla",
    "বাংলা": "Bangla",
}


def normalize_language(value: str, strict: bool = False) -> str:
    raw = str(value or "English").strip()
    normalized = SUPPORTED_LANGUAGES.get(raw.casefold())
    if normalized:
        return normalized
    if strict:
        raise ValueError("Language must be English or Bangla.")
    return raw or "English"


def resolve_voice(cfg: dict, language: str) -> str:
    """Choose the configured voice for the narration language, with safe defaults."""
    tts_cfg = cfg.get("tts", {})
    canonical = normalize_language(language)
    voices = tts_cfg.get("voices") or {}
    if canonical == "Bangla":
        return str(voices.get("bangla") or tts_cfg.get("bangla_voice") or "bn-BD-NabanitaNeural")
    if canonical == "English":
        return str(voices.get("english") or tts_cfg.get("voice") or "en-US-AndrewMultilingualNeural")
    return str(tts_cfg.get("voice") or "en-US-AndrewMultilingualNeural")


def resolve_voice_rate(cfg: dict, language: str) -> str:
    """Use a language-specific Edge TTS rate when configured."""
    tts_cfg = cfg.get("tts", {})
    if normalize_language(language) == "Bangla":
        return str(tts_cfg.get("bangla_rate") or "-5%")
    return str(tts_cfg.get("rate") or "+0%")


def resolve_subtitle_language(narration_language: str, subtitle_language: str = "auto") -> str:
    requested = str(subtitle_language or "auto").strip().casefold()
    narration = normalize_language(narration_language)
    if requested in ("", "auto", "same", "same as narration"):
        return narration
    return normalize_language(requested, strict=True)
