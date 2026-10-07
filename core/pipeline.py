import json
import math
import random
import re
import time
from pathlib import Path

from . import render, tts
from .comfy import ComfyClient
from .script_gen import make_script
from .subtitles import build_ass


def resolve_size(cfg, fmt, custom):
    if fmt == "custom":
        w, h = custom
    else:
        w, h = cfg["formats"][fmt]
    return w // 2 * 2, h // 2 * 2


def build(cfg, job: Path, source: str, mode: str, fmt: str, custom=None,
          seconds=45, language="English", log=print) -> Path:
    job.mkdir(parents=True, exist_ok=True)
    W, H = resolve_size(cfg, fmt, custom)
    portrait = H > W
    v = cfg["video"]

    # 1. script (cached -> edit script.json by hand and re-run to tweak)
    sp = job / "script.json"
    if sp.exists():
        script = json.loads(sp.read_text(encoding="utf-8"))
        log("script: reusing script.json")
    else:
        log("script: generating with Ollama ...")
        script = make_script(cfg, source, mode, fmt, seconds, language)
        sp.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    scenes = script["scenes"]
    log(f"script: '{script['title']}' - {len(scenes)} scenes")

    comfy = ComfyClient(cfg["comfy"]["host"], cfg["comfy"]["timeout_s"])
    spec = cfg["comfy"][mode]
    comfy.validate(spec)
    durations, wavs, clips = [], [], []

    for i, sc in enumerate(scenes):
        tag = f"{i:03d}"
        # 2. voice
        mp3, wav = job / f"a{tag}.mp3", job / f"a{tag}.wav"
        if not wav.exists():
            tts.synth(sc["narration"], cfg["tts"]["voice"], cfg["tts"]["rate"], mp3, wav)
        dur = tts.probe_duration(wav)
        durations.append(dur)
        wavs.append(wav)

        # 3. visual
        found = list(job.glob(f"m{tag}.*"))
        if found:
            media = found[0]
        else:
            log(f"scene {i + 1}/{len(scenes)}: generating {mode} ...")
            if mode == "video":
                prompt = (f"{v['style_video']}.\n\n{sc['visual_prompt']}\n\n"
                          "No text, subtitles, logos or watermarks, no cartoon or overly-CG look, keep the live-action texture.")
            else:
                prompt = f"{sc['visual_prompt']}, {v['style']}"
            aspect = "9:16" if portrait else "16:9"
            values = {"prompt": prompt, "seed": random.randint(0, 2**31),
                      "aspect": comfy.resolve_aspect(spec, aspect) or aspect}
            if mode == "video":
                values["duration"] = float(min(spec.get("max_clip_s", 15), max(2, math.ceil(dur + v["transition_s"]))))
            # fallback ladder: full quality -> lower megapixels -> turbo on
            ladder = [values,
                      {**values, "megapixels": 1},
                      {**values, "megapixels": 1, "turbo": True}]
            media = None
            for attempt, vals in enumerate(ladder):
                try:
                    media = comfy.generate(spec, vals, job / f"m{tag}")
                    break
                except Exception as e:
                    if "out of memory" not in str(e).lower() and "memory" not in str(e).lower():
                        raise
                    if attempt == len(ladder) - 1:
                        raise RuntimeError(
                            f"GPU out of memory even at lowest quality. Close other apps, lower video.megapixels in "
                            f"config.yaml, or use --mode image. ({e})")
                    log(f"scene {i + 1}: GPU OOM, retrying at reduced quality ({attempt + 1}/{len(ladder) - 1}) ...")
                    time.sleep(10)
        kind = "image" if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else "video"

        # 4. per-scene clip (+T so transitions overlap without shifting sync)
        clip = job / f"c{tag}.mp4"
        render.scene_clip(media.resolve(), kind, dur + v["transition_s"], W, H, v["fps"], clip, i)
        clips.append(clip)

    # 5. subtitles + assemble
    build_ass(scenes, durations, W, H, cfg, job / "subs.ass")
    name = re.sub(r"[^\w\-]+", "_", script["title"])[:50] or "video"
    final = f"{name}_{W}x{H}.mp4"
    log("render: assembling final video ...")
    music = render.pick_music(Path("music")) if v.get("music", True) else None
    if music:
        log(f"render: background music: {music.name}")
    es = v.get("end_screen") or {}
    end_screen = es if (es.get("enabled") and es.get("channel")) else None
    if end_screen:
        end_screen = {**end_screen, "W": W, "H": H}
    render.assemble(job, clips, wavs, durations, "subs.ass", final, v["transition_s"], v["transitions"],
                    music_path=music, music_volume=v.get("music_volume", 0.15),
                    end_screen=end_screen)
    return job / final
