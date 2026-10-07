"""Structured story scripts and scene prompts via local Ollama."""
from __future__ import annotations

import hashlib
import json
import math

import requests

from .timing import narration_target_seconds

SCRIPT_VERSION = 2
DEFAULT_NARRATION_WPM = 190

SYSTEM = """You are a precise, engaging documentary storyteller and short-form video scriptwriter.
Write narration that sounds natural when spoken aloud. Start directly with the story, keep the facts tied to the supplied idea, and finish the thought completely. Return only valid JSON; no markdown or commentary."""

SCRIPT_TEMPLATE = """Create a complete spoken-narration script based on this user idea or source material:
\"\"\"
{source}
\"\"\"

VIDEO TARGET
- Requested finished-video runtime: {requested_seconds} seconds, including transition padding and any configured outro.
- Narration runtime target: about {narration_seconds:.1f} seconds.
- Approximate speaking pace: {wpm} words per minute.
- Total narration budget: about {total_words} words (stay within 5 percent).
- Language: {language}.
- Visual format: {fmt_name}. Visual mode: {mode_desc}.
- Return exactly {scene_count} scenes.

REQUIRED STORY ARC — allocate narration by spoken words, not by scene count:
1. OPENING — first 15 percent (about {opening_words} words): begin inside the idea with its most compelling moment, tension, question, or consequence. Give only the essential setup. Do not add greetings, channel introductions, "in this video", broad background, or throat-clearing.
2. CORE — next 70 percent (about {core_words} words): spend most of the script explaining and developing the actual idea. Use a clear progression of relevant events, causes, evidence, stakes, or steps. Keep context concise and connected; do not pad with generic facts or wander into a different topic.
3. RESOLUTION — final 15 percent (about {ending_words} words): close the specific question or story introduced at the start. Deliver a clear, earned outcome or takeaway, and where natural echo the opening. End on a complete, memorable sentence. Do not abruptly stop, introduce a surprise unrelated twist, add a new topic, or tack on a generic subscribe call-to-action.

ACCURACY AND STYLE
- Treat the user's idea as the subject, not merely a prompt for a generic introduction.
- Do not invent named people, quotes, dates, causes, or events. If the source is only an idea, use careful wording for uncertain claims.
- Use concise, speakable sentences and concrete details. No filler phrases or repeated conclusions.
- The final narration must feel like a finished story, not a teaser or an unfinished episode.
- Every scene must advance the same story arc. Balance narration across scenes so the full script lands near the word budget.
- "visual_prompt" must be in English, concrete, self-contained, and suitable for the requested mode. {mode_rule}
- Keep visual continuity. No text, subtitles, logos, or watermarks in the visual prompts.

Return this JSON shape only:
{{
  "title": "Short, specific title",
  "scenes": [
    {{"narration": "Spoken narration for this scene.", "visual_prompt": "English visual prompt."}}
  ]
}}"""

REVISION_TEMPLATE = """Rewrite the draft below so its measured spoken length matches the video runtime, while preserving its subject and improving the story arc.

RUNTIME CHECK
- Finished-video target: {requested_seconds} seconds.
- Narration target: about {narration_seconds:.1f} seconds.
- The current text measures about {actual_seconds:.1f} seconds when spoken by the configured voice.
- Target about {total_words} spoken words (within 5 percent), at roughly {wpm} words per minute.
- Return exactly {scene_count} scenes in {language}.

STORY REQUIREMENTS
- First 15 percent: enter the idea immediately, with a compelling hook and only essential setup. No generic preface or greetings.
- Middle 70 percent: develop the actual idea with relevant, connected detail; add useful explanation rather than padding.
- Final 15 percent: resolve the opening question/conflict and end with a complete, earned final sentence. No abrupt cutoff, unrelated twist, new topic, or generic CTA.
- Preserve factual restraint. Do not invent names, quotes, dates, or events.
- Keep the narration natural to speak. Update each visual prompt to match its narration.
- Keep the supplied topic and language; do not mention this rewrite request.
- Visual mode: {mode_desc}. {mode_rule}

CURRENT DRAFT:
{draft_json}

Return only this JSON shape:
{{"title":"Short, specific title","scenes":[{{"narration":"...","visual_prompt":"English visual prompt."}}]}}"""

MODES = {
    "image": (
        "still photographic images",
        "Describe one composed still frame, not a sequence; avoid motion verbs."
    ),
    "video": (
        "short moving video clips",
        "Describe one continuous live-action shot: subject, setting, lighting, camera movement, and subject action. Never mention sound, music, dialogue, subtitles, or on-screen text."
    ),
}


def word_count(text: str) -> int:
    return len(str(text or "").split())


def story_plan(narration_seconds: float, scene_seconds: float = 6.0,
               words_per_minute: int = DEFAULT_NARRATION_WPM) -> dict:
    """Plan a duration-aware scene count and explicit 15/70/15 word budget."""
    narration_seconds = max(0.0, float(narration_seconds))
    scene_seconds = max(1.0, float(scene_seconds or 6.0))
    wpm = max(60, int(words_per_minute or DEFAULT_NARRATION_WPM))
    total_words = max(20, round(narration_seconds * wpm / 60.0))
    opening_words = max(1, round(total_words * 0.15))
    core_words = max(1, round(total_words * 0.70))
    ending_words = max(1, total_words - opening_words - core_words)
    # ceil keeps each generated visual near the configured scene length instead of
    # silently dropping a scene when the requested duration is not divisible by it.
    scene_count = max(3, math.ceil(narration_seconds / scene_seconds))
    return {
        "narration_seconds": round(narration_seconds, 2),
        "words_per_minute": wpm,
        "total_words": total_words,
        "opening_words": opening_words,
        "core_words": core_words,
        "ending_words": ending_words,
        "scene_count": scene_count,
        "words_per_scene": math.ceil(total_words / scene_count),
    }


def script_content_hash(script: dict) -> str:
    """Hash editable script content, excluding private generation metadata."""
    payload = {
        "title": str(script.get("title", "")),
        "scenes": [
            {"narration": str(scene.get("narration", "")),
             "visual_prompt": str(scene.get("visual_prompt", ""))}
            for scene in script.get("scenes", []) if isinstance(scene, dict)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def script_was_edited(script: dict) -> bool:
    metadata = script.get("_generator") or {}
    expected = metadata.get("content_hash")
    return bool(expected and expected != script_content_hash(script))


def _plan_from_config(cfg: dict, requested_seconds: float, narration_seconds: float | None):
    video = cfg.get("video", {})
    if narration_seconds is None:
        narration_seconds = narration_target_seconds(video, requested_seconds)
    wpm = int(video.get("narration_wpm", DEFAULT_NARRATION_WPM))
    return story_plan(narration_seconds, video.get("scene_seconds", 6), wpm)


def _clean_scenes(data: dict) -> list[dict]:
    result = []
    for scene in data.get("scenes", []) if isinstance(data, dict) else []:
        if not isinstance(scene, dict):
            continue
        narration = str(scene.get("narration", "")).strip()
        visual_prompt = str(scene.get("visual_prompt", "")).strip()
        if narration and visual_prompt:
            result.append({"narration": narration, "visual_prompt": visual_prompt})
    return result


def _stamp_metadata(script: dict, plan: dict, requested_seconds: float,
                    language: str, mode: str, fmt_name: str, source: str,
                    duration_revisions: int = 0) -> dict:
    script = {"title": str(script.get("title") or "Untitled").strip() or "Untitled",
              "scenes": _clean_scenes(script)}
    script["_generator"] = {
        "version": SCRIPT_VERSION,
        "requested_seconds": float(requested_seconds),
        "narration_target_seconds": plan["narration_seconds"],
        "words_per_minute": plan["words_per_minute"],
        "word_budget": plan["total_words"],
        "arc_percent": {"opening": 15, "core": 70, "resolution": 15},
        "language": language,
        "mode": mode,
        "format": fmt_name,
        "source_hash": hashlib.sha256(str(source).encode("utf-8")).hexdigest(),
        "duration_revisions": int(duration_revisions),
        "content_hash": "",
    }
    script["_generator"]["content_hash"] = script_content_hash(script)
    return script


def _decode_json_content(content):
    """Decode strict JSON, tolerating a model-added Markdown fence or short preamble."""
    if isinstance(content, (dict, list)):
        return content
    text = str(content or "").strip()
    if not text:
        raise ValueError("empty message.content")
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as original:
        # Some Ollama/model combinations add a sentence before an otherwise valid
        # JSON object. Extract one balanced object without corrupting quoted braces.
        start = next((i for i, char in enumerate(text) if char in "{["), -1)
        if start >= 0:
            opening = text[start]
            closing = "}" if opening == "{" else "]"
            depth, in_string, escaped = 0, False, False
            for index in range(start, len(text)):
                char = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:index + 1])
        raise original


def _call_ollama(cfg: dict, prompt: str, temperature: float = 0.55) -> dict:
    ollama = cfg["ollama"]
    payload = {
        "model": ollama["model"],
        "stream": False,
        "format": "json",
        # Reasoning models such as Qwen can otherwise put the structured answer
        # in the thinking channel, leaving message.content empty for the parser.
        "think": ollama.get("think", False),
        "options": {"temperature": temperature},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    url = f"{ollama['host'].rstrip('/')}/api/chat"

    def request_and_decode(request_payload):
        response = requests.post(url, timeout=900, json=request_payload)
        response.raise_for_status()
        result = response.json()
        message = result.get("message") or {}
        content = message.get("content")
        if not str(content or "").strip():
            reason = result.get("done_reason") or result.get("status") or "unknown"
            if message.get("thinking"):
                error = RuntimeError(
                    f"empty message.content; Ollama returned text in the thinking channel (finish reason: {reason})"
                )
            else:
                error = RuntimeError(f"empty message.content (finish reason: {reason})")
            return None, error, content
        try:
            return _decode_json_content(content), None, content
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return None, exc, content

    parsed, error, content = request_and_decode(payload)
    if error is not None and payload["think"] is False:
        # A few Ollama/model versions mishandle JSON mode when thinking is disabled.
        # Retry once with thinking explicitly enabled; use only message.content, never
        # the private reasoning field, and still require valid JSON before proceeding.
        parsed, retry_error, retry_content = request_and_decode({**payload, "think": True})
        if retry_error is None:
            return parsed
        error = RuntimeError(f"{error}; retry with think=true also failed: {retry_error}")
        content = retry_content
    if error is not None:
        excerpt = str(content or "").strip().replace("\n", " ")[:240]
        raise RuntimeError(
            f"Ollama could not return a valid script JSON object: {error}. "
            f"Response starts: {excerpt!r}. Check that Ollama is current and the configured model is available."
        ) from error
    return parsed


def _validate_script(data: dict) -> dict:
    if not isinstance(data, dict):
        raise RuntimeError("Model returned a script that is not a JSON object.")
    scenes = _clean_scenes(data)
    if len(scenes) < 2:
        raise RuntimeError(f"Model returned an unusable script layout: {data}")
    return {"title": str(data.get("title") or "Untitled"), "scenes": scenes}


def make_script(cfg: dict, source: str, mode: str, fmt_name: str, seconds: float,
                language: str = "English", narration_seconds: float | None = None) -> dict:
    """Generate a story-first script with a requested final-video duration."""
    if mode not in MODES:
        raise ValueError(f"Unsupported generation mode: {mode}")
    plan = _plan_from_config(cfg, seconds, narration_seconds)
    mode_desc, mode_rule = MODES[mode]
    prompt = SCRIPT_TEMPLATE.format(
        source=source,
        requested_seconds=float(seconds),
        narration_seconds=plan["narration_seconds"],
        wpm=plan["words_per_minute"],
        total_words=plan["total_words"],
        opening_words=plan["opening_words"],
        core_words=plan["core_words"],
        ending_words=plan["ending_words"],
        language=language,
        fmt_name=fmt_name,
        mode_desc=mode_desc,
        mode_rule=mode_rule,
        scene_count=plan["scene_count"],
    )
    script = _validate_script(_call_ollama(cfg, prompt))
    return _stamp_metadata(script, plan, seconds, language, mode, fmt_name, source)


def revise_script_for_duration(cfg: dict, source: str, mode: str, fmt_name: str,
                               requested_seconds: float, narration_seconds: float,
                               actual_seconds: float, language: str,
                               current_script: dict, revision_number: int = 1) -> dict:
    """Ask the model to repair duration drift without losing the story arc."""
    plan = _plan_from_config(cfg, requested_seconds, narration_seconds)
    mode_desc, mode_rule = MODES[mode]
    current = {"title": current_script.get("title", "Untitled"),
               "scenes": current_script.get("scenes", [])}
    prompt = REVISION_TEMPLATE.format(
        requested_seconds=float(requested_seconds),
        narration_seconds=plan["narration_seconds"],
        actual_seconds=float(actual_seconds),
        total_words=plan["total_words"],
        wpm=plan["words_per_minute"],
        scene_count=plan["scene_count"],
        language=language,
        mode_desc=mode_desc,
        mode_rule=mode_rule,
        draft_json=json.dumps(current, ensure_ascii=False, indent=2),
    )
    script = _validate_script(_call_ollama(cfg, prompt, temperature=0.35))
    return _stamp_metadata(script, plan, requested_seconds, language, mode, fmt_name, source, revision_number)
