"""Generate publish-ready platform metadata from the final video script."""
from __future__ import annotations

import re

from .language import normalize_language
from .script_gen import _call_ollama


def _as_tags(value, limit=15):
    if isinstance(value, str):
        values = re.split(r"[,\n]", value)
    elif isinstance(value, list):
        values = value
    else:
        values = []
    tags, seen = [], set()
    for item in values:
        tag = re.sub(r"\s+", " ", str(item or "")).strip().strip("#")
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag[:80])
        if len(tags) >= limit:
            break
    return tags


def normalize_metadata(data: dict, script: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("SEO response must be a JSON object.")
    platforms = data.get("platforms") if isinstance(data.get("platforms"), dict) else data
    result = {}
    for key, aliases in (("youtube", ("youtube", "YouTube")), ("facebook", ("facebook", "Facebook"))):
        item = next((platforms.get(alias) for alias in aliases if isinstance(platforms.get(alias), dict)), {})
        title = re.sub(r"\s+", " ", str(item.get("title") or script.get("title") or "Video")).strip()[:100]
        description = str(item.get("description") or "").strip()[:5000]
        tags = _as_tags(item.get("tags"))
        if not title or not description or not tags:
            raise ValueError(f"SEO response is missing title, description, or tags for {key}.")
        result[key] = {"title": title, "description": description, "tags": tags}
    return {"version": 1, "platforms": result}


def fallback_metadata(script: dict, language: str = "English") -> dict:
    title = re.sub(r"\s+", " ", str(script.get("title") or "Video")).strip()[:100] or "Video"
    scenes = script.get("scenes") or []
    summary = " ".join(str(scene.get("narration", "")).strip() for scene in scenes[:4])
    summary = re.sub(r"\s+", " ", summary).strip()[:900]
    if not summary:
        summary = title
    words = re.findall(r"[^\W_]+", f"{title} {summary}", flags=re.UNICODE)
    tags = _as_tags(words, limit=12) or [title]
    tags = list(dict.fromkeys(tags + ["video", "story"]))[:15]
    description = f"{summary}\n\nWatch the full video and share your thoughts."
    return {
        "version": 1,
        "platforms": {
            "youtube": {"title": title, "description": description, "tags": tags},
            "facebook": {"title": title, "description": description, "tags": tags[:10]},
        },
        "fallback": True,
        "language": normalize_language(language),
    }


def generate_metadata(cfg: dict, script: dict, language: str = "English") -> dict:
    language = normalize_language(language)
    language_note = (
        "Write natural Bangladeshi Bangla in Bengali script, not Latin transliteration."
        if language == "Bangla" else "Write in natural, polished English."
    )
    scenes = script.get("scenes") or []
    narration = "\n".join(str(scene.get("narration", "")).strip() for scene in scenes)
    prompt = f"""Create accurate, search-friendly publishing metadata for this finished video.
Return one set for YouTube and one for Facebook. {language_note}
Do not invent facts, dates, or claims. Make titles specific and compelling, not clickbait.
YouTube: title under 90 characters, a useful 2-4 sentence description, and 8-15 concise keyword tags.
Facebook: a natural social-first title, a brief engaging description, and 3-8 relevant hashtags or keywords.
Return JSON only in this shape:
{{"youtube":{{"title":"...","description":"...","tags":["..."]}},"facebook":{{"title":"...","description":"...","tags":["..."]}}}}

Video title: {script.get('title', 'Video')}
Narration:
{narration[:12000]}
"""
    return normalize_metadata(_call_ollama(cfg, prompt, temperature=0.3), script)
