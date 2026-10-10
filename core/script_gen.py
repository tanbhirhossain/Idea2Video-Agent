"""Structured story scripts and scene prompts via local Ollama."""
from __future__ import annotations

import hashlib
import json
import math
<<<<<<< HEAD
import re
=======
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff

import requests

from .timing import narration_target_seconds

SCRIPT_VERSION = 2
DEFAULT_NARRATION_WPM = 190
<<<<<<< HEAD
DEFAULT_NUM_PREDICT = 8192
MAX_NUM_PREDICT = 16384

SYSTEM = """You are a precise, engaging documentary storyteller and short-form video scriptwriter.
Write concise narration in familiar, plain language that a general audience can understand without prior knowledge. Start directly with the story, keep facts tied to the supplied idea, and finish the thought completely. Return only valid JSON; no markdown or commentary."""
=======

SYSTEM = """You are a precise, engaging documentary storyteller and short-form video scriptwriter.
Write narration that sounds natural when spoken aloud. Start directly with the story, keep the facts tied to the supplied idea, and finish the thought completely. Return only valid JSON; no markdown or commentary."""
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff

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
<<<<<<< HEAD
1. OPENING — first 15 percent (about {opening_words} words): begin inside the idea with its most compelling moment, tension, question, or consequence. Give only the essential setup. Do not add greetings, channel introductions, \"in this video\", broad background, or throat-clearing.
=======
1. OPENING — first 15 percent (about {opening_words} words): begin inside the idea with its most compelling moment, tension, question, or consequence. Give only the essential setup. Do not add greetings, channel introductions, "in this video", broad background, or throat-clearing.
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
2. CORE — next 70 percent (about {core_words} words): spend most of the script explaining and developing the actual idea. Use a clear progression of relevant events, causes, evidence, stakes, or steps. Keep context concise and connected; do not pad with generic facts or wander into a different topic.
3. RESOLUTION — final 15 percent (about {ending_words} words): close the specific question or story introduced at the start. Deliver a clear, earned outcome or takeaway, and where natural echo the opening. End on a complete, memorable sentence. Do not abruptly stop, introduce a surprise unrelated twist, add a new topic, or tack on a generic subscribe call-to-action.

ACCURACY AND STYLE
- Treat the user's idea as the subject, not merely a prompt for a generic introduction.
- Do not invent named people, quotes, dates, causes, or events. If the source is only an idea, use careful wording for uncertain claims.
<<<<<<< HEAD
- Make the script short but meaningful: every sentence should add a useful fact, action, cause, or consequence.
- Make sentences short, clear, and useful (usually under 18 words); use active voice and familiar words.
- Assume the viewer has no background knowledge. Explain an essential unfamiliar name, place, or term briefly the first time it appears.
- Choose only the few facts and actions needed to understand what happened and why it mattered; do not try to tell every detail.
- Keep one main point per sentence. Avoid jargon, long lists of names or dates, filler, and repeated conclusions.
- The final narration must feel like a finished story, not a teaser or an unfinished episode.
- Every scene must advance the same story arc. Balance narration across scenes so the full script lands near the word budget.
- \"visual_prompt\" must be in English, concrete, self-contained, and suitable for the requested mode. {mode_rule}
=======
- Use concise, speakable sentences and concrete details. No filler phrases or repeated conclusions.
- The final narration must feel like a finished story, not a teaser or an unfinished episode.
- Every scene must advance the same story arc. Balance narration across scenes so the full script lands near the word budget.
- "visual_prompt" must be in English, concrete, self-contained, and suitable for the requested mode. {mode_rule}
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
- Keep visual continuity. No text, subtitles, logos, or watermarks in the visual prompts.

Return this JSON shape only:
{{
<<<<<<< HEAD
  \"title\": \"Short, specific title\",
  \"scenes\": [
    {{\"narration\": \"Spoken narration for this scene.\", \"visual_prompt\": \"English visual prompt.\"}}
=======
  "title": "Short, specific title",
  "scenes": [
    {{"narration": "Spoken narration for this scene.", "visual_prompt": "English visual prompt."}}
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
  ]
}}"""

REVISION_TEMPLATE = """Rewrite the draft below so its measured spoken length matches the video runtime, while preserving its subject and improving the story arc.

RUNTIME CHECK
- Finished-video target: {requested_seconds} seconds.
- Narration target: about {narration_seconds:.1f} seconds.
- The current text measures about {actual_seconds:.1f} seconds when spoken by the configured voice.
<<<<<<< HEAD
- Target about {total_words} words from the runtime plan, at roughly {wpm} words per minute.
- The current narration has {current_words} words. Target {target_words} words total, within {word_tolerance} words; change the current draft by about {word_delta} words (positive means add, negative means remove).
- {duration_direction}
- Return exactly {scene_count} scenes in {language}, keeping the same scene order.
=======
- Target about {total_words} spoken words (within 5 percent), at roughly {wpm} words per minute.
- Return exactly {scene_count} scenes in {language}.
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff

STORY REQUIREMENTS
- First 15 percent: enter the idea immediately, with a compelling hook and only essential setup. No generic preface or greetings.
- Middle 70 percent: develop the actual idea with relevant, connected detail; add useful explanation rather than padding.
- Final 15 percent: resolve the opening question/conflict and end with a complete, earned final sentence. No abrupt cutoff, unrelated twist, new topic, or generic CTA.
- Preserve factual restraint. Do not invent names, quotes, dates, or events.
<<<<<<< HEAD
- Make it short but meaningful: use simple, familiar words, short active sentences, and one idea per sentence.
- Assume the viewer is new to the subject. Briefly explain essential terms; remove background details that do not help explain what happened or why it matters.
- Keep the narration natural to speak and preserve the key events and story arc.
- Keep the supplied topic and language; do not mention this rewrite request.
- Keep exactly the same number and order of scenes as the draft. Each scene needs narration; spread the full word budget across scenes.
- Do not return visual prompts. The application will preserve the existing visual prompt for each matching scene.

CURRENT DRAFT (narration only):
{draft_json}

Return only compact JSON in this shape, with no markdown:
{{\"title\":\"Short, specific title\",\"scenes\":[{{\"narration\":\"...\"}}]}}"""

MODES = {
    "image": (
        "high-resolution documentary stills",
        "Describe one composed, photorealistic frame with one clear focal subject, believable anatomy, accurate setting details, and uncluttered composition; no sequence or motion verbs."
    ),
    "video": (
        "high-quality moving live-action clips",
        "Describe one continuous photorealistic shot with a clear subject, believable action, natural anatomy, stable camera movement, and physically plausible lighting. Never mention sound, music, dialogue, subtitles, or on-screen text."
=======
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
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
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
<<<<<<< HEAD
    try:
        num_predict = int(ollama.get("num_predict", DEFAULT_NUM_PREDICT))
    except (TypeError, ValueError):
        num_predict = DEFAULT_NUM_PREDICT
    num_predict = max(512, min(MAX_NUM_PREDICT, num_predict))
=======
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
    payload = {
        "model": ollama["model"],
        "stream": False,
        "format": "json",
        # Reasoning models such as Qwen can otherwise put the structured answer
        # in the thinking channel, leaving message.content empty for the parser.
        "think": ollama.get("think", False),
<<<<<<< HEAD
        "options": {"temperature": temperature, "num_predict": num_predict},
=======
        "options": {"temperature": temperature},
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
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
<<<<<<< HEAD
        finish_reason = str(result.get("done_reason") or result.get("status") or "unknown")
        if not str(content or "").strip():
            if message.get("thinking"):
                error = RuntimeError(
                    f"empty message.content; Ollama returned text in the thinking channel (finish reason: {finish_reason})"
                )
            else:
                error = RuntimeError(f"empty message.content (finish reason: {finish_reason})")
            return None, error, content, finish_reason
        try:
            return _decode_json_content(content), None, content, finish_reason
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return None, exc, content, finish_reason

    parsed, error, content, finish_reason = request_and_decode(payload)
    if error is not None and finish_reason.lower() == "length":
        # Ollama's default/model output cap is often too small for many-scene JSON.
        # Retry with a larger completion budget without enabling chain-of-thought,
        # which may consume the extra tokens and leave message.content empty again.
        expanded = min(MAX_NUM_PREDICT, max(num_predict + 1024, num_predict * 2))
        if expanded > num_predict:
            retry_payload = {
                **payload,
                "options": {**payload["options"], "num_predict": expanded},
            }
            parsed, error, content, finish_reason = request_and_decode(retry_payload)
            num_predict = expanded
            if error is None:
                return parsed

    if error is not None and payload["think"] is False and finish_reason.lower() != "length":
        # A few Ollama/model versions mishandle JSON mode when thinking is disabled.
        # Retry once with thinking explicitly enabled; use only message.content, never
        # the private reasoning field, and still require valid JSON before proceeding.
        retry_payload = {
            **payload,
            "think": True,
            "options": {**payload["options"], "num_predict": num_predict},
        }
        parsed, retry_error, retry_content, finish_reason = request_and_decode(retry_payload)
        if retry_error is None:
            return parsed
        error = RuntimeError(f"{error}; retry with think=true also failed: {retry_error}")
        content = retry_content
    if error is not None:
        excerpt = str(content or "").strip().replace("\n", " ")[:240]
        length_note = (
            f" Ollama stopped at the output limit ({num_predict} tokens); increase \"ollama.num_predict\" in config.yaml."
            if finish_reason.lower() == "length" else ""
        )
        raise RuntimeError(
            f"Ollama could not return a valid script JSON object: {error}.{length_note} "
            f"Response starts: {excerpt!r}. Check that Ollama is current and the configured model is available."
        ) from error
    return parsed


=======
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


>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
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


<<<<<<< HEAD
def _split_narration(text: str) -> tuple[str, str] | None:
    """Split one narration at a natural boundary when a model omits a scene."""
    words = str(text or "").split()
    if len(words) < 2:
        return None
    midpoint = len(words) / 2
    sentence_breaks = [i for i in range(1, len(words)) if re.search(r"[.!?][\"')\]]?$", words[i - 1])]
    if sentence_breaks:
        split_at = min(sentence_breaks, key=lambda i: abs(i - midpoint))
    else:
        punctuation_breaks = [i for i in range(1, len(words)) if re.search(r"[,;:—-][\"')\]]?$", words[i - 1])]
        split_at = min(punctuation_breaks, key=lambda i: abs(i - midpoint)) if punctuation_breaks else round(midpoint)
    split_at = max(1, min(len(words) - 1, split_at))
    first, second = " ".join(words[:split_at]).strip(), " ".join(words[split_at:]).strip()
    if first and first[-1] not in ".!?":
        first += "."
    if second:
        second = second[0].upper() + second[1:]
    return first, second


def _normalize_scene_count(narrations: list[str], target_count: int, log=None) -> list[str]:
    """Keep one narration per saved visual by splitting/merging model scene text."""
    original_count = len(narrations)
    narrations = list(narrations)
    while len(narrations) > target_count:
        # Merge the shortest adjacent pair so the model's full narration is retained.
        index = min(
            range(len(narrations) - 1),
            key=lambda i: (word_count(narrations[i]) + word_count(narrations[i + 1]), -i),
        )
        narrations[index:index + 2] = [f"{narrations[index].rstrip()} {narrations[index + 1].lstrip()}".strip()]
    while len(narrations) < target_count:
        # Split from the end when possible so earlier text keeps its matching cached visual.
        index = next((i for i in range(len(narrations) - 1, -1, -1)
                      if _split_narration(narrations[i]) is not None), None)
        if index is None:
            raise RuntimeError(
                f"Could not safely split model narration into {target_count} scenes "
                f"(only {len(narrations)} non-empty scenes returned)."
            )
        narrations[index:index + 1] = list(_split_narration(narrations[index]))
    if original_count != target_count and log:
        action = "split" if original_count < target_count else "merged"
        log(
            f"script: Ollama returned {original_count} scenes instead of {target_count}; "
            f"{action} narration segments to preserve the saved visual sequence."
        )
    return narrations


def _merge_revised_narration(data: dict, current_script: dict, log=None) -> dict:
    """Merge narration-only output with saved prompts, repairing scene-count drift."""
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
        raise RuntimeError("Model returned an invalid duration revision: expected a scenes array.")
    old_scenes = current_script.get("scenes", [])
    raw_scenes = data["scenes"]
    narrations = []
    for index, new_scene in enumerate(raw_scenes, start=1):
        if isinstance(new_scene, str):
            narration = new_scene.strip()
        elif isinstance(new_scene, dict):
            narration = str(new_scene.get("narration", "")).strip()
        else:
            narration = ""
        if not narration:
            raise RuntimeError(f"Model returned empty narration for revised scene {index}.")
        narrations.append(narration)
    if not narrations:
        raise RuntimeError("Model returned an empty duration revision.")
    narrations = _normalize_scene_count(narrations, len(old_scenes), log=log)
    merged = []
    for index, (narration, old_scene) in enumerate(zip(narrations, old_scenes), start=1):
        visual_prompt = str(old_scene.get("visual_prompt", "")).strip()
        if not visual_prompt:
            raise RuntimeError(f"The existing script is missing a visual prompt for scene {index}.")
        merged.append({"narration": narration, "visual_prompt": visual_prompt})
    title = str(data.get("title") or current_script.get("title") or "Untitled").strip()
    return {"title": title or "Untitled", "scenes": merged}


def revise_script_for_duration(cfg: dict, source: str, mode: str, fmt_name: str,
                               requested_seconds: float, narration_seconds: float,
                               actual_seconds: float, language: str,
                               current_script: dict, revision_number: int = 1,
                               log=print) -> dict:
    """Repair measured duration drift with compact, word-budgeted narration revisions."""
    plan = _plan_from_config(cfg, requested_seconds, narration_seconds)
    old_scenes = current_script.get("scenes", [])
    if len(old_scenes) < 2:
        raise ValueError("Cannot revise a script with fewer than two scenes.")

    def narration_words(script):
        return word_count(" ".join(str(scene.get("narration", "")) for scene in script.get("scenes", [])))

    original_words = narration_words(current_script)
    if original_words < 1 or float(actual_seconds) <= 0:
        raise ValueError("Cannot calculate a duration correction from empty narration.")
    # Use measured TTS pace to set a more reliable word target than the configured
    # estimate alone; this compensates for the selected voice's real speaking rate.
    target_words = max(1, round(original_words * float(narration_seconds) / float(actual_seconds)))
    word_tolerance = max(4, round(target_words * 0.03))
    working_script = current_script
    working_seconds = float(actual_seconds)
    revised = current_script

    for attempt in range(2):
        current_words = narration_words(working_script)
        word_delta = target_words - current_words
        if working_seconds < narration_seconds:
            direction = (
                f"The narration is too short by about {float(narration_seconds) - working_seconds:.1f} seconds. "
                "Add useful context, causes, transitions, and consequences; never add repetition or filler."
            )
        else:
            direction = (
                f"The narration is too long by about {working_seconds - float(narration_seconds):.1f} seconds. "
                "Remove repetition and secondary detail while preserving essential facts and the story arc."
            )
        compact_draft = {
            "title": working_script.get("title", "Untitled"),
            "scenes": [{"narration": str(scene.get("narration", ""))}
                       for scene in working_script.get("scenes", [])],
        }
        prompt = REVISION_TEMPLATE.format(
            requested_seconds=float(requested_seconds),
            narration_seconds=plan["narration_seconds"],
            actual_seconds=working_seconds,
            total_words=plan["total_words"],
            current_words=current_words,
            target_words=target_words,
            word_tolerance=word_tolerance,
            word_delta=f"{word_delta:+d}",
            wpm=plan["words_per_minute"],
            duration_direction=direction,
            scene_count=len(old_scenes),
            language=language,
            draft_json=json.dumps(compact_draft, ensure_ascii=False, separators=(",", ":")),
        )
        revised_data = _call_ollama(cfg, prompt, temperature=0.35)
        revised = _merge_revised_narration(revised_data, working_script, log=log)
        revised_words = narration_words(revised)
        if abs(revised_words - target_words) <= word_tolerance or attempt == 1:
            break
        if log:
            log(
                f"script: revision draft has {revised_words} words; target is about {target_words}. "
                "Requesting a tighter word-count correction before TTS ..."
            )
        working_seconds = working_seconds * revised_words / max(1, current_words)
        working_script = revised

    return _stamp_metadata(revised, plan, requested_seconds, language, mode, fmt_name, source, revision_number)
=======
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
>>>>>>> 09673f3e04e823228dae1a54fa4c6fd47dcdebff
