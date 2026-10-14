"""FFmpeg rendering for scene clips, narration, music, transitions, and export."""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"ffmpeg failed:\n{p.stderr[-3000:]}")


def _even(x):
    return int(x) // 2 * 2


def duration_of(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def scene_clip(src, kind, dur, W, H, fps, out, idx, crf=16, preset="slow"):
    """Turn a still or generated video into a crisp, transition-ready scene clip."""
    frames = max(1, int(round(float(dur) * float(fps))))
    if kind == "image":
        bw, bh = _even(W * 1.5), _even(H * 1.5)
        z = f"1+0.15*on/{frames}" if idx % 2 == 0 else f"1.15-0.15*on/{frames}"
        vf = (
            f"scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={bw}:{bh},"
            f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={W}x{H}:fps={fps},"
            "eq=contrast=1.03:saturation=1.06,"
            "unsharp=5:5:0.25:3:3:0,format=yuv420p"
        )
        pre = ["-loop", "1"]
    else:
        vf = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H},fps={fps},"
            "eq=contrast=1.02:saturation=1.04,"
            "unsharp=5:5:0.20:3:3:0,format=yuv420p"
        )
        pre = ["-stream_loop", "-1"]
    run([
        "ffmpeg", "-y", *pre, "-i", src, "-vf", vf, "-t", f"{float(dur):.3f}",
        "-r", str(fps), "-c:v", "libx264", "-preset", str(preset), "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p", "-an", out,
    ])


def _contains_audio(path: Path) -> bool | None:
    """Return True/False for an audio stream; None if ffprobe is not installed."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    return result.returncode == 0 and bool(result.stdout.strip())


def pick_music(music_dir: Path):
    """Choose a playable local audio track, ignoring files without an audio stream."""
    if not music_dir.is_dir():
        return None
    files = [p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]
    valid = []
    for path in files:
        has_audio = _contains_audio(path)
        if has_audio is not False:
            valid.append(path)
    return random.choice(valid) if valid else None


def _esc_drawtext(s: str) -> str:
    """Escape a string for ffmpeg drawtext text=... inside a filter_complex."""
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%").replace(",", "\\,")


def end_screen_filter(channel: str, tagline: str, end_s: float, W: int, H: int, start_at: float) -> str:
    """Animated subscribe end screen over the held last frame. start_at = when the screen begins."""
    ch = _esc_drawtext(channel)
    tg = _esc_drawtext(tagline)
    fs_big = int(H * 0.055)
    fs_small = int(H * 0.028)
    fade = 0.6
    s0 = max(0.0, start_at)
    s1 = s0 + fade
    alpha = f"if(lt(t,{s1:.3f}),(t-{s0:.3f})/{fade},1)"
    return (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill:enable='gte(t,{s0:.3f})',"
        f"drawtext=text='SUBSCRIBE':fontsize={int(fs_big * 0.6)}:fontcolor=white:font='Arial':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42-({fs_big}*1.6):"
        f"alpha='{alpha}':enable='gte(t,{s0:.3f})',"
        f"drawtext=text='{ch}':fontsize={fs_big}:fontcolor=white:font='Arial Bold':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42:"
        f"alpha='{alpha}':enable='gte(t,{s0:.3f})',"
        f"drawtext=text='{tg}':fontsize={fs_small}:fontcolor=0xd8d8e8:font='Arial':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42+{fs_big}*1.4:"
        f"alpha='{alpha}':enable='gte(t,{s0:.3f})'"
    )


def _music_audio_graph(aidx: int, midx: int, music_volume: float, fade_out_at: float,
                       narration_filter: str) -> str:
    """Normalize the music bed, fade it, duck it under speech, then mix it."""
    volume = min(1.0, max(0.0, float(music_volume)))
    return (
        f"[{aidx}:a]{narration_filter}[nar];[nar]asplit=2[nar_sc][nar_mix];"
        f"[{midx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"loudnorm=I=-18:TP=-1.5:LRA=11,volume={volume:.3f},"
        f"afade=t=in:st=0:d=0.8,afade=t=out:st={max(0.0, fade_out_at):.3f}:d=2[mus];"
        f"[mus][nar_sc]sidechaincompress=threshold=0.12:ratio=3:attack=30:release=300[ducked];"
        f"[nar_mix][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];"
        f"[mix]alimiter=limit=0.95[aout]"
    )


def assemble(job: Path, clips, audio_wavs, durations, ass_name, final_name, T, transitions,
             music_path=None, music_volume=0.5, end_screen=None,
             video_crf=16, video_preset="slow"):
    """Assemble clips with narration and optional normalized, ducked background music."""
    job = Path(job)
    (job / "audio.txt").write_text("".join(f"file '{Path(a).name}'\n" for a in audio_wavs), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "audio.txt", "-c", "copy", "narration.wav"], cwd=job)

    inputs = []
    for clip in clips:
        inputs += ["-i", Path(clip).name]
    n = len(clips)
    if n == 0:
        raise ValueError("Cannot assemble a video without scene clips.")
    if n == 1:
        fc, last = "", "0:v"
    else:
        parts, last, offset = [], "0:v", 0.0
        for index in range(1, n):
            offset += durations[index - 1]
            transition = transitions[(index - 1) % len(transitions)]
            output = f"v{index}"
            parts.append(f"[{last}][{index}:v]xfade=transition={transition}:duration={T}:offset={offset:.3f}[{output}]")
            last = output
        fc = ";".join(parts) + ";"
    total = sum(durations) + T
    end_s = 0.0
    tail_vf = f"subtitles={ass_name}"
    if end_screen and str(end_screen.get("channel", "")).strip() and end_screen.get("seconds", 0) > 0:
        end_s = float(end_screen["seconds"])
        total += end_s
        tail_vf = (
            f"subtitles={ass_name},tpad=stop_mode=clone:stop_duration={end_s:.3f},"
            + end_screen_filter(
                end_screen["channel"], end_screen.get("tagline", "for more videos like this"),
                end_s, int(end_screen.get("W", 1080)), int(end_screen.get("H", 1920)), sum(durations) + T,
            )
        )
    fc += f"[{last}]{tail_vf}[vout]"

    inputs += ["-i", "narration.wav"]
    aidx = n
    amap = f"{aidx}:a"
    apad = T + end_s
    narration_filter = f"apad=pad_dur={apad:.3f},loudnorm=I=-16:TP=-1.5:LRA=11"
    music_volume = min(1.0, max(0.0, float(music_volume)))
    if music_path and music_volume > 0:
        inputs += ["-stream_loop", "-1", "-i", str(Path(music_path).resolve())]
        midx = n + 1
        fc += ";" + _music_audio_graph(aidx, midx, music_volume, total - 2, narration_filter)
        amap = "[aout]"
        narration_filter = None

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
           "-map", "[vout]", "-map", amap]
    if narration_filter:
        cmd += ["-af", narration_filter]
    cmd += ["-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", str(video_preset), "-crf", str(int(video_crf)),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final_name]
    run(cmd, cwd=job)
