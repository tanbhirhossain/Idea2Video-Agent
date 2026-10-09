"""End-to-end video generation pipeline."""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
from pathlib import Path

from . import render, tts
from .comfy import ComfyClient
from .script_gen import (
    make_script,
    revise_script_for_duration,
    script_was_edited,
)
from .subtitles import build_ass
from .timing import duration_tolerance, narration_target_seconds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_DURATION_REVISIONS = 3


def resolve_size(cfg, fmt, custom):
    if fmt == "custom":
        if not custom or len(custom) != 2:
            raise ValueError("Custom format needs a width and height (WxH).")
        w, h = custom
    else:
        if fmt not in cfg["formats"]:
            raise ValueError(f"Unknown output format: {fmt}")
        w, h = cfg["formats"][fmt]
    if int(w) < 2 or int(h) < 2:
        raise ValueError("Output width and height must be at least 2 pixels.")
    return int(w) // 2 * 2, int(h) // 2 * 2


def _save_script(path: Path, script: dict) -> None:
    path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")


def _clear_scene_assets(job: Path) -> None:
    """Clear generated voice, visual and render assets when a script is replaced."""
    patterns = ("a*.mp3", "a*.wav", "a*.meta.json", "m[0-9][0-9][0-9].*", "c[0-9][0-9][0-9].mp4",
                "subs.ass", "narration.wav", "audio.txt")
    for pattern in patterns:
        for path in job.glob(pattern):
            if path.is_file():
                path.unlink()


def _audio_signature(text: str, voice: str, rate: str) -> str:
    raw = "\0".join((text, voice, rate)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def prepare_voiceover(job: Path, scenes: list[dict], cfg: dict, log=print) -> tuple[list[float], list[Path]]:
    """Synthesize all narration before image/video generation and measure actual TTS time."""
    durations, wavs = [], []
    voice, rate = cfg["tts"]["voice"], cfg["tts"]["rate"]
    for i, scene in enumerate(scenes):
        tag = f"{i:03d}"
        text = str(scene.get("narration", "")).strip()
        if not text:
            raise ValueError(f"Scene {i + 1} has no narration.")
        mp3, wav = job / f"a{tag}.mp3", job / f"a{tag}.wav"
        meta_path = job / f"a{tag}.meta.json"
        signature = _audio_signature(text, voice, rate)
        cached_signature = None
        if meta_path.exists():
            try:
                cached_signature = json.loads(meta_path.read_text(encoding="utf-8")).get("signature")
            except (OSError, json.JSONDecodeError):
                cached_signature = None
        if not wav.exists() or cached_signature != signature:
            log(f"voice {i + 1}/{len(scenes)}: synthesizing narration ...")
            try:
                tts.synth(text, voice, rate, mp3, wav)
            except Exception as exc:
                raise RuntimeError(
                    f"Text-to-speech failed for scene {i + 1}/{len(scenes)} "
                    f"(voice={voice!r}, {len(text)} characters): {exc}"
                ) from exc
            meta_path.write_text(json.dumps({"signature": signature}, indent=2), encoding="utf-8")
        duration = tts.probe_duration(wav)
        durations.append(duration)
        wavs.append(wav)
    return durations, wavs


def build(cfg, job: Path, source: str, mode: str, fmt: str, custom=None,
          seconds=45, language="English", log=print) -> Path:
    job = Path(job)
    job.mkdir(parents=True, exist_ok=True)
    requested_seconds = float(seconds)
    W, H = resolve_size(cfg, fmt, custom)
    portrait = H > W
    video_cfg = cfg["video"]
    target_narration = narration_target_seconds(video_cfg, requested_seconds)

    # 1. Generate or resume the script. A saved target mismatch invalidates cached
    # assets; an edited script is left intact rather than silently overwritten.
    script_path = job / "script.json"
    user_edited = False
    if script_path.exists():
        script = json.loads(script_path.read_text(encoding="utf-8"))
        user_edited = script_was_edited(script)
        metadata = script.get("_generator") or {}
        old_target = metadata.get("requested_seconds")
        try:
            target_changed = old_target is not None and abs(float(old_target) - requested_seconds) > 0.01
        except (TypeError, ValueError):
            target_changed = True
        current_wpm = int(video_cfg.get("narration_wpm", 190))
        current_source_hash = hashlib.sha256(str(source).encode("utf-8")).hexdigest()
        changed_inputs = []
        if target_changed:
            changed_inputs.append(f"duration {old_target}s → {requested_seconds:g}s")
        for key, current in (("language", language), ("mode", mode), ("format", fmt),
                             ("words_per_minute", current_wpm), ("source_hash", current_source_hash)):
            if metadata.get(key) is not None and metadata.get(key) != current:
                changed_inputs.append(key.replace("_", " "))
        if changed_inputs and not user_edited:
            log(f"script: saved inputs changed ({', '.join(changed_inputs)}); rebuilding ...")
            _clear_scene_assets(job)
            script = make_script(cfg, source, mode, fmt, requested_seconds, language, target_narration)
            user_edited = False
            _save_script(script_path, script)
        else:
            suffix = " (edited by user; keeping your text)" if user_edited else ""
            log("script: reusing script.json" + suffix)
    else:
        log("script: generating with Ollama (15/70/15 story arc) ...")
        script = make_script(cfg, source, mode, fmt, requested_seconds, language, target_narration)
        _save_script(script_path, script)

    scenes = script.get("scenes") or []
    if isinstance(script.get("_generator"), dict):
        script["_generator"]["output_size"] = [W, H]
        _save_script(script_path, script)
    if len(scenes) < 2:
        raise RuntimeError("The saved script needs at least two scenes. Edit or regenerate script.json.")
    log(f"script: '{script.get('title', 'Untitled')}' - {len(scenes)} scenes")

    # Fail early if ComfyUI/workflow wiring is wrong; the expensive voiceover and
    # visual steps should not run when the renderer cannot accept the job.
    comfy = ComfyClient(cfg["comfy"]["host"], cfg["comfy"]["timeout_s"])
    spec = cfg["comfy"][mode]
    comfy.validate(spec)

    # 2. Synthesize all narration first. This catches the original duration bug:
    # scene-count estimates are no longer treated as a proxy for actual TTS length.
    durations, wavs = prepare_voiceover(job, scenes, cfg, log)
    actual_narration = sum(durations)
    metadata = script.get("_generator") or {}
    can_revise = bool(metadata.get("version")) and not user_edited
    revisions = int(metadata.get("duration_revisions", 0) or 0)
    tolerance = duration_tolerance(video_cfg, target_narration)

    while abs(actual_narration - target_narration) > tolerance and can_revise and revisions < MAX_DURATION_REVISIONS:
        log(
            f"script: measured narration is {actual_narration:.1f}s; target is "
            f"{target_narration:.1f}s. Refining the script for duration ({revisions + 1}/{MAX_DURATION_REVISIONS}) ..."
        )
        revisions += 1
        script = revise_script_for_duration(
            cfg, source, mode, fmt, requested_seconds, target_narration,
            actual_narration, language, script, revisions, log=log,
        )
        if isinstance(script.get("_generator"), dict):
            script["_generator"]["output_size"] = [W, H]
        # Duration revisions update narration only and retain visual prompts, so
        # already-generated scene media can be reused without another GPU pass.
        _save_script(script_path, script)
        scenes = script["scenes"]
        durations, wavs = prepare_voiceover(job, scenes, cfg, log)
        actual_narration = sum(durations)

    estimated_final = actual_narration + (requested_seconds - target_narration)
    if abs(actual_narration - target_narration) > tolerance:
        reason = "edited/cached script" if user_edited or not can_revise else "duration retry limit reached"
        log(
            f"warning: narration is still {actual_narration:.1f}s (target {target_narration:.1f}s); "
            f"expected final runtime about {estimated_final:.1f}s vs {requested_seconds:.1f}s ({reason})."
        )
    else:
        log(f"script: narration fitted to {actual_narration:.1f}s (target {target_narration:.1f}s)")

    # 3. Generate visuals only after the script/audio duration has been measured and
    # corrected. This avoids spending GPU time on scenes for a script that is too short.
    clips = []
    for i, (scene, duration) in enumerate(zip(scenes, durations)):
        tag = f"{i:03d}"
        found = list(job.glob(f"m{tag}.*"))
        if found:
            media = found[0]
        else:
            log(f"scene {i + 1}/{len(scenes)}: generating {mode} ...")
            if mode == "video":
                prompt = (
                    f"{video_cfg['style_video']}.\n\n{scene['visual_prompt']}\n\n"
                    "No text, subtitles, logos or watermarks, no cartoon or overly-CG look, keep the live-action texture."
                )
            else:
                prompt = f"{scene['visual_prompt']}, {video_cfg['style']}"
            aspect = "9:16" if portrait else "16:9"
            values = {
                "prompt": prompt,
                "seed": random.randint(0, 2**31),
                "aspect": comfy.resolve_aspect(spec, aspect) or aspect,
            }
            if mode == "video":
                values["duration"] = float(
                    min(spec.get("max_clip_s", 15), max(2, math.ceil(duration + video_cfg["transition_s"])))
                )
            # Fallback ladder: full quality -> lower megapixels -> turbo on.
            ladder = [values, {**values, "megapixels": 1}, {**values, "megapixels": 1, "turbo": True}]
            media = None
            for attempt, vals in enumerate(ladder):
                try:
                    media = comfy.generate(spec, vals, job / f"m{tag}")
                    break
                except Exception as exc:
                    if "out of memory" not in str(exc).lower() and "memory" not in str(exc).lower():
                        raise
                    if attempt == len(ladder) - 1:
                        raise RuntimeError(
                            f"GPU out of memory even at lowest quality. Close other apps, lower video.megapixels in "
                            f"config.yaml, or use --mode image. ({exc})") from exc
                    log(f"scene {i + 1}: GPU OOM, retrying at reduced quality ({attempt + 1}/{len(ladder) - 1}) ...")
                    time.sleep(10)

        kind = "image" if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else "video"
        clip = job / f"c{tag}.mp4"
        render.scene_clip(
            media.resolve(), kind, duration + video_cfg["transition_s"],
            W, H, video_cfg["fps"], clip, i,
            crf=video_cfg.get("scene_crf", 16), preset=video_cfg.get("render_preset", "slow"),
        )
        clips.append(clip)

    # 4. Captions and final assembly. The configured runtime includes the outro.
    build_ass(scenes, durations, W, H, cfg, job / "subs.ass")
    title = re.sub(r"[^\w\-]+", "_", script.get("title", "video"))[:50] or "video"
    final_name = f"{title}_{W}x{H}.mp4"
    log("render: assembling final video ...")
    music = render.pick_music(PROJECT_ROOT / "music") if video_cfg.get("music", True) else None
    if music:
        log(f"render: background music: {music.name} (level {100 * video_cfg.get('music_volume', 0.5):.0f}%, gentle voice ducking on)")
    elif video_cfg.get("music", True):
        log("render: music is enabled, but no playable audio tracks were found in the project music folder")
    end_screen_cfg = video_cfg.get("end_screen") or {}
    end_screen = end_screen_cfg if (end_screen_cfg.get("enabled") and str(end_screen_cfg.get("channel", "")).strip()) else None
    if end_screen:
        end_screen = {**end_screen, "W": W, "H": H}
    render.assemble(
        job, clips, wavs, durations, "subs.ass", final_name,
        video_cfg["transition_s"], video_cfg["transitions"],
        music_path=music, music_volume=video_cfg.get("music_volume", 0.5),
        end_screen=end_screen,
        video_crf=video_cfg.get("final_crf", 16),
        video_preset=video_cfg.get("render_preset", "slow"),
    )
    final = job / final_name
    actual_final = render.duration_of(final)
    delta = actual_final - requested_seconds
    log(f"render: final duration {actual_final:.1f}s (target {requested_seconds:.1f}s; {delta:+.1f}s)")
    if abs(delta) > max(2.0, requested_seconds * 0.04):
        log("warning: final runtime is outside the expected tolerance; check the script length and TTS voice rate.")
    return final
