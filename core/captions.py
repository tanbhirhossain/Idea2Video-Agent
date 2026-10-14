"""Caption-language resolution and cached cross-language subtitle translation."""
from __future__ import annotations

import json
from pathlib import Path

from .language import normalize_language, resolve_subtitle_language
from .script_gen import script_content_hash, translate_subtitles

CAPTION_CACHE_VERSION = 2


def prepare_captions(job: Path, cfg: dict, script: dict, narration_language: str,
                     subtitle_language: str = "auto", log=print) -> tuple[list[str], str]:
    """Return one caption per scene, translating only when the selected language differs."""
    scenes = script.get("scenes") or []
    source_language = normalize_language(narration_language)
    target_language = resolve_subtitle_language(source_language, subtitle_language)
    if source_language == target_language:
        return [str(scene.get("narration", "")).strip() for scene in scenes], target_language

    cache_path = Path(job) / "subtitles.json"
    content_hash = script_content_hash(script)
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (cached.get("version") == CAPTION_CACHE_VERSION
                    and cached.get("script_hash") == content_hash
                    and cached.get("source_language") == source_language
                    and cached.get("target_language") == target_language
                    and isinstance(cached.get("captions"), list)
                    and len(cached["captions"]) == len(scenes)
                    and all(str(value).strip() for value in cached["captions"])):
                if log:
                    log(f"captions: reusing {target_language} translation")
                return [str(value) for value in cached["captions"]], target_language
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    if log:
        log(f"captions: translating {source_language} narration to {target_language} ...")
    captions = translate_subtitles(cfg, scenes, source_language, target_language)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "version": CAPTION_CACHE_VERSION,
        "script_hash": content_hash,
        "source_language": source_language,
        "target_language": target_language,
        "captions": captions,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(cache_path)
    return captions, target_language
