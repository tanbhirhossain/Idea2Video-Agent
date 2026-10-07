"""FFmpeg rendering: Ken-Burns/loop per scene, xfade transitions, burned subtitles, loudness-normalised audio.

Sync math: each clip is (audio_dur + T) long, xfade overlap is T, so scene i starts exactly at
sum(audio_dur[:i]) and stays aligned with the concatenated narration.
"""
import random
import subprocess
from pathlib import Path


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"ffmpeg failed:\n{p.stderr[-2000:]}")


def _even(x): return int(x) // 2 * 2


def duration_of(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def scene_clip(src, kind, dur, W, H, fps, out, idx):
    if kind == "image":
        frames = int(dur * fps)
        bw, bh = _even(W * 1.5), _even(H * 1.5)
        z = f"1+0.15*on/{frames}" if idx % 2 == 0 else f"1.15-0.15*on/{frames}"
        vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={fps},"
              f"eq=contrast=1.05:saturation=1.1,format=yuv420p")
        pre = ["-loop", "1"]
    else:
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},"
              f"eq=contrast=1.05:saturation=1.1,format=yuv420p")
        pre = ["-stream_loop", "-1"]
    run(["ffmpeg", "-y", *pre, "-i", src, "-vf", vf, "-t", f"{dur:.3f}", "-r", fps,
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-an", out])


def pick_music(music_dir: Path):
    """Return a random music file from music/ (mp3/wav/ogg/m4a/flac), or None."""
    if not music_dir.is_dir():
        return None
    files = [p for p in music_dir.iterdir()
             if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a", ".flac")]
    return random.choice(files) if files else None


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
    a = f"if(lt(t,{s1:.3f}),(t-{s0:.3f})/{fade},1)"
    return (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill:enable='gte(t,{s0:.3f})',"
        f"drawtext=text='SUBSCRIBE':fontsize={int(fs_big * 0.6)}:fontcolor=white:font='Arial':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42-({fs_big}*1.6):"
        f"alpha='{a}':enable='gte(t,{s0:.3f})',"
        f"drawtext=text='{ch}':fontsize={fs_big}:fontcolor=white:font='Arial Bold':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42:"
        f"alpha='{a}':enable='gte(t,{s0:.3f})',"
        f"drawtext=text='{tg}':fontsize={fs_small}:fontcolor=0xd8d8e8:font='Arial':"
        f"borderw=0:x=(w-text_w)/2:y=h*0.42+{fs_big}*1.4:"
        f"alpha='{a}':enable='gte(t,{s0:.3f})'"
    )


def assemble(job: Path, clips, audio_wavs, durations, ass_name, final_name, T, transitions,
             music_path=None, music_volume=0.15, end_screen=None):
    """end_screen: None or dict {channel, tagline, seconds} — appends a subscribe card on a held last frame."""
    (job / "audio.txt").write_text("".join(f"file '{Path(a).name}'\n" for a in audio_wavs))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "audio.txt", "-c", "copy", "narration.wav"], cwd=job)

    inputs = []
    for c in clips:
        inputs += ["-i", Path(c).name]
    n = len(clips)
    if n == 1:
        fc, last = "", "0:v"
    else:
        parts, last, off = [], "0:v", 0.0
        for i in range(1, n):
            off += durations[i - 1]
            tr = transitions[(i - 1) % len(transitions)]
            out = f"v{i}"
            parts.append(f"[{last}][{i}:v]xfade=transition={tr}:duration={T}:offset={off:.3f}[{out}]")
            last = out
        fc = ";".join(parts) + ";"
    total = sum(durations) + T
    end_s = 0.0
    tail_vf = f"subtitles={ass_name}"
    if end_screen and end_screen.get("channel") and end_screen.get("seconds", 0) > 0:
        end_s = float(end_screen["seconds"])
        total += end_s
        tail_vf = (f"subtitles={ass_name},tpad=stop_mode=clone:stop_duration={end_s:.3f},"
                   + end_screen_filter(end_screen["channel"], end_screen.get("tagline", "for more videos like this"), end_s,
                                       int(end_screen.get("W", 1080)), int(end_screen.get("H", 1920)), sum(durations) + T))
    fc += f"[{last}]{tail_vf}[vout]"

    inputs += ["-i", "narration.wav"]
    aidx = n  # narration.wav input index
    amap = f"{aidx}:a"
    apad = T + end_s
    afilter = "apad=pad_dur=%.3f,loudnorm=I=-16:TP=-1.5:LRA=11" % apad
    if music_path:
        # music ducked under narration, faded out at the end, looped if shorter than the video
        inputs += ["-stream_loop", "-1", "-i", str(Path(music_path).resolve())]
        midx = n + 1
        fc += (f";[{aidx}:a]{afilter}[nar];"
               f"[{midx}:a]volume={music_volume},afade=t=out:st={max(0.0, total - 2):.3f}:d=2[mus];"
               f"[nar][mus]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        amap = "[aout]"
        afilter = None

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", fc,
           "-map", "[vout]", "-map", amap]
    if afilter:
        cmd += ["-af", afilter]
    cmd += ["-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final_name]
    run(cmd, cwd=job)
