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
from .captions import prepare_captions
from .comfy import ComfyClient
from .language import normalize_language, resolve_subtitle_language, resolve_voice, resolve_voice_rate
from .script_gen import make_script, revise_script_for_duration, script_was_edited
from .seo import fallback_metadata, generate_metadata
from .subtitles import build_ass
from .timing import duration_tolerance, narration_target_seconds, narration_wpm

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


def prepare_voiceover(job: Path, scenes: list[dict], cfg: dict, log=print,
                      language: str = "English") -> tuple[list[float], list[Path]]:
    """Synthesize all narration with the language-appropriate voice and measure actual TTS time."""
    durations, wavs = [], []
    voice, rate = resolve_voice(cfg, language), resolve_voice_rate(cfg, language)
    for i, scene in enumerate(scenes):
        tag = f"{i:03d}"
        text = str(scene.get("narration", "")).strip()
        if not text:
            raise ValueError(f"Scene {i + 1} has no narration.")
        mp3, wav = job / f"a{tag}.mp3", job / f"a{tag}.wav"
        meta_path = job / f"a{tag}.meta.json"
        signature = _audio_signature(text, voice, rate)
        cached_signature, cached_provider = None, None
        if meta_path.exists():
            try:
                cached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(cached_meta, dict):
                    cached_signature = cached_meta.get("signature")
                    cached_provider = cached_meta.get("provider")
            except (OSError, json.JSONDecodeError):
                cached_signature = None
        if not wav.exists() or cached_signature != signature:
            log(f"voice {i + 1}/{len(scenes)}: synthesizing narration ...")
            try:
                provider = tts.synth(text, voice, rate, mp3, wav)
            except Exception as exc:
                raise RuntimeError(
                    f"Text-to-speech failed for scene {i + 1}/{len(scenes)} "
                    f"(voice={voice!r}, {len(text)} characters): {exc}"
                ) from exc
            meta = {"signature": signature}
            if provider:
                meta["provider"] = provider
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            if provider == "google":
                locale = voice.split("-", 1)[0].casefold()
                log(f"voice {i + 1}/{len(scenes)}: Edge TTS was unavailable; used Google TTS fallback (locale={locale}).")
        elif cached_provider == "google":
            log(f"voice {i + 1}/{len(scenes)}: reusing cached Google TTS fallback audio.")
        duration = tts.probe_duration(wav)
        durations.append(duration)
        wavs.append(wav)
    return durations, wavs


def build(cfg, job: Path, source: str, mode: str, fmt: str, custom=None,
          seconds=45, language="English", subtitle_language="auto", log=print) -> Path:
    job = Path(job)
    language = normalize_language(language)
    subtitle_language = str(subtitle_language or "auto").strip()
    requested_caption_language = resolve_subtitle_language(language, subtitle_language)
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
        current_wpm = narration_wpm(video_cfg, language)
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
    comfy = ComfyClient(cfg["comfy"]["host"], cfg["comfy"].get("timeout_s", 7200), log=log)
    spec = cfg["comfy"][mode]
    comfy_report = comfy.validate(spec)
    if isinstance(comfy_report, dict):
        for warning in comfy_report.get("warnings", []):
            log(f"ComfyUI warning: {warning}")
        errors = [str(error) for error in comfy_report.get("errors", [])]
        if errors or comfy_report.get("ok") is False:
            detail = "; ".join(errors) or "workflow validation failed without a detailed error"
            raise RuntimeError(
                f"ComfyUI {mode} workflow preflight failed: {detail}. "
                "Open Admin panel → Diagnose AI video and install/fix the listed nodes or models."
            )

    # 2. Synthesize all narration first. This catches the original duration bug:
    # scene-count estimates are no longer treated as a proxy for actual TTS length.
    durations, wavs = prepare_voiceover(job, scenes, cfg, log, language=language)
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
        durations, wavs = prepare_voiceover(job, scenes, cfg, log, language=language)
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

    metadata = script.setdefault("_generator", {})
    metadata["subtitle_language"] = subtitle_language
    metadata["subtitle_language_resolved"] = requested_caption_language
    _save_script(script_path, script)
    try:
        captions, caption_language = prepare_captions(
            job, cfg, script, language, subtitle_language, log=log,
        )
    except Exception as exc:
        log(f"warning: subtitle translation to {requested_caption_language} failed; using {language} captions ({exc})")
        captions = [str(scene.get("narration", "")).strip() for scene in scenes]
        caption_language = language

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
            visual_prompt = str(scene.get("visual_prompt", "")).strip()
            story_beat = str(scene.get("narration", "")).strip()
            if mode == "video":
                prompt = (
                    f"{video_cfg['style_video']}.\n\n"
                    f"Story beat to depict (semantic context only; never render these words): {story_beat}\n\n"
                    f"Shot: {visual_prompt}\n\n"
                    "Create one coherent continuous shot that directly illustrates this exact beat, with specific subjects, "
                    "actions, place, period details, and physically plausible motion. No text, subtitles, logos, watermarks, "
                    "unrelated stock imagery, or cartoon/overly-CG styling."
                )
            else:
                prompt = (
                    f"{video_cfg['style']}.\n\n"
                    f"Story beat (visual meaning only; never render words): {story_beat}\n\n"
                    f"Specific visual description: {visual_prompt}\n\n"
                    "One photorealistic documentary frame directly illustrating this exact beat. Use relevant setting and period "
                    "details, one clear focal subject and action, believable anatomy, natural lighting, and a clean composition. "
                    "No text, letters, captions, logos, watermarks, unrelated generic imagery, or collage."
                )
            aspect = "9:16" if portrait else "16:9"
            values = {
                "prompt": prompt,
                "seed": random.randint(0, 2**31),
                "aspect": comfy.resolve_aspect(spec, aspect) or aspect,
            }
            if mode == "video":
                values["duration"] = float(
                    min(spec.get("max_clip_s", 15),
                        max(spec.get("min_clip_s", 2), math.ceil(duration + video_cfg["transition_s"])))
                )
            # Fallback ladder: reduce resolution without silently changing model quality/mode.
            ladder = [values] + [
                {**values, "megapixels": float(megapixels)}
                for megapixels in spec.get("fallback_megapixels", [1.0, 0.6])
            ]
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
                    time.sleep(5)

        log(f"scene {i + 1}/{len(scenes)}: visual ready")
        kind = "image" if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else "video"
        clip = job / f"c{tag}.mp4"
        render.scene_clip(
            media.resolve(), kind, duration + video_cfg["transition_s"],
            W, H, video_cfg["fps"], clip, i,
            crf=video_cfg.get("scene_crf", 16), preset=video_cfg.get("render_preset", "slow"),
        )
        clips.append(clip)
        log(f"scene {i + 1}/{len(scenes)}: clip rendered")

    # 4. Captions and final assembly. The configured runtime includes the outro.
    build_ass(scenes, durations, W, H, cfg, job / "subs.ass", captions=captions, language=caption_language)
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

    log("seo: generating YouTube and Facebook metadata ...")
    try:
        seo = generate_metadata(cfg, script, language)
    except Exception as exc:
        seo = fallback_metadata(script, language)
        seo["warning"] = str(exc)[:500]
        log(f"warning: AI SEO generation failed; saved basic metadata instead ({exc})")
    seo_path = job / "seo.json"
    seo_tmp = job / "seo.json.tmp"
    seo_tmp.write_text(json.dumps(seo, ensure_ascii=False, indent=2), encoding="utf-8")
    seo_tmp.replace(seo_path)
    log("seo: YouTube and Facebook metadata saved")
    return final
