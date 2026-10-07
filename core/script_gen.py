# """Script + scene prompts via local Ollama."""
# import json
# import requests

# SYSTEM = """You are an award-winning short-form video scriptwriter and director.
# Return ONLY valid JSON, no commentary."""

# TEMPLATE = """Source material:
# \"\"\"
# {source}
# \"\"\"

# Create a {seconds}-second video script in {language}.
# Format: {fmt_name}. Visual mood: {mode_desc}.

# Rules:
# - Strong hook in the first scene, clear payoff in the last.
# - Exactly {n} scenes. Each narration ~{words} words (spoken in about {scene_s} seconds).
# - "visual_prompt" is always ENGLISH, concrete and self-contained (subject, setting, lighting, composition).
#   {mode_rule}
# - Keep a consistent visual style across scenes. No text/logos/watermarks in visuals.

# JSON schema:
# {{"title": str, "scenes": [{{"narration": str, "visual_prompt": str}}]}}"""

# MODES = {
#     "image": ("still photographic images",
#               "Describe a single still frame (no motion verbs)."),
#     "video": ("short moving video clips",
#               "Describe ONE continuous live-action shot: subject, setting, lighting, explicit camera motion (e.g. slow dolly-in, tracking shot) and subject action. Never mention sound, music, dialogue, subtitles or on-screen text."),
# }


# def make_script(cfg, source, mode, fmt_name, seconds, language="English"):
#     scene_s = cfg["video"]["scene_seconds"]
#     n = max(3, round(seconds / scene_s))
#     mode_desc, mode_rule = MODES[mode]
#     prompt = TEMPLATE.format(source=source, seconds=seconds, language=language, fmt_name=fmt_name,
#                              mode_desc=mode_desc, mode_rule=mode_rule, n=n,
#                              words=int(scene_s * 2.5), scene_s=scene_s)
#     o = cfg["ollama"]
#     r = requests.post(f"{o['host']}/api/chat", timeout=900, json={
#         "model": o["model"], "stream": False, "format": "json", "think": False,
#         "options": {"temperature": 0.7},
#         "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
#     })
#     r.raise_for_status()
#     data = json.loads(r.json()["message"]["content"])
#     scenes = [s for s in data.get("scenes", []) if s.get("narration") and s.get("visual_prompt")]
#     if len(scenes) < 2:
#         raise RuntimeError(f"Model returned an unusable script: {data}")
#     return {"title": data.get("title", "Untitled"), "scenes": scenes}


"""Script + scene prompts via local Ollama."""
import json
import requests

SYSTEM = """You are an award-winning short-form video scriptwriter and director.
Return ONLY valid JSON with no additional commentary, markdown wrapper, or extra keys."""

TEMPLATE = """Source material:
\"\"\"
{source}
\"\"\"

Create a complete video script based on the source material.
- Total Target Duration: {seconds} seconds
- Language: {language}
- Visual Format: {fmt_name}
- Visual Style: {mode_desc}
- Total Number of Scenes: Exactly {n}

NARRATIVE ARC REQUIREMENTS (Must complete the full story):
- Scene 1: High-impact hook introducing the core setup or problem.
- Middle Scenes (Scene 2 to {n_minus_1}): Progressive development and rising story action.
- Scene {n} (Final Scene): Clear resolution, climax, or payoff that completes the story arc.

SCENE FORMAT RULES:
- Generate EXACTLY {n} scenes.
- Each scene narration must contain approximately {words} words (designed to be spoken in ~{scene_s} seconds).
- "visual_prompt" must be in ENGLISH, highly concrete, and self-contained (describing subject, setting, lighting, composition).
- {mode_rule}
- Keep visual style consistent across all scenes. No text, logos, or watermarks in visuals.

REQUIRED JSON OUTPUT FORMAT:
{{
  "title": "Script Title",
  "scenes": [
    {{
      "narration": "Narration text here",
      "visual_prompt": "Visual prompt text here"
    }}
  ]
}}"""

MODES = {
    "image": (
        "still photographic images",
        "Describe a single still frame (no motion verbs)."
    ),
    "video": (
        "short moving video clips",
        "Describe ONE continuous live-action shot: subject, setting, lighting, explicit camera motion (e.g., slow dolly-in, tracking shot), and subject action. Never mention sound, music, dialogue, subtitles, or on-screen text."
    ),
}


def make_script(cfg, source, mode, fmt_name, seconds, language="English"):
    scene_s = cfg["video"]["scene_seconds"]
    n = max(3, round(seconds / scene_s))
    words_per_scene = max(3, int(scene_s * 2.3))  # Safe estimation for natural speech pace
    mode_desc, mode_rule = MODES[mode]

    prompt = TEMPLATE.format(
        source=source,
        seconds=seconds,
        language=language,
        fmt_name=fmt_name,
        mode_desc=mode_desc,
        mode_rule=mode_rule,
        n=n,
        n_minus_1=max(2, n - 1),
        words=words_per_scene,
        scene_s=scene_s,
    )

    o = cfg["ollama"]
    payload = {
        "model": o["model"],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.7},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }

    # Optional think key handling for reasoning models (e.g., DeepSeek-R1)
    if "think" in o:
        payload["think"] = o["think"]

    r = requests.post(f"{o['host']}/api/chat", timeout=900, json=payload)
    r.raise_for_status()

    data = json.loads(r.json()["message"]["content"])
    scenes = [
        s for s in data.get("scenes", [])
        if s.get("narration") and s.get("visual_prompt")
    ]

    if len(scenes) < 2:
        raise RuntimeError(f"Model returned an unusable script layout: {data}")

    return {"title": data.get("title", "Untitled"), "scenes": scenes}