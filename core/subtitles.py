"""ASS subtitle generation with readable, language-aware cue wrapping."""
from __future__ import annotations

import re

from .language import normalize_language


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _wrap_words(words, max_chars: int) -> str:
    """Wrap at word boundaries for ASS; never split a Bengali conjunct/word."""
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return r"\N".join(lines)


def _cue_chunks(caption: str, max_words: int, max_chars: int, max_lines: int = 2):
    words = str(caption or "").replace("{", "").replace("}", "").replace("\\", " ").split()
    chunks, current = [], []
    sentence_end = re.compile(r"(?:[.!?…।॥]+[\"'’”)]*)$")
    for word in words:
        candidate = current + [word]
        wrapped = _wrap_words(candidate, max_chars)
        too_wide = len(wrapped.split(r"\N")) > max_lines
        if current and (len(current) >= max_words or too_wide):
            chunks.append(current)
            current = [word]
        else:
            current = candidate
        # Prefer complete thought boundaries without producing one-word flashes.
        if len(current) >= 3 and sentence_end.search(word):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build_ass(scenes, durations, W, H, cfg, out_path, captions=None, language="English"):
    sc = cfg.get("subtitles", {})
    language = normalize_language(language)
    bangla = language == "Bangla"
    font = (sc.get("font_bangla") or "Nirmala UI") if bangla else (sc.get("font_english") or sc.get("font") or "Arial")
    font = re.sub(r"[\r\n,]", "", str(font)).strip() or ("Nirmala UI" if bangla else "Arial")
    size = int(W * 0.08) if H > W else int(H * 0.05)
    margin_v = int(H * 0.18) if H > W else int(H * 0.08)
    words_per_cue = int(sc.get("words_per_cue_bangla", 6) if bangla else sc.get("words_per_cue", 4))
    max_chars = int(sc.get("max_chars_per_line_bangla", 23) if bangla else sc.get("max_chars_per_line", 34))
    words_per_cue = max(2, min(12, words_per_cue))
    max_chars = max(12, min(72, max_chars))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3, size // 14)},2,2,{int(W*0.06)},{int(W*0.06)},{margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines, offset = [], 0.0
    for index, (scene, dur) in enumerate(zip(scenes, durations)):
        caption = captions[index] if captions and index < len(captions) else scene.get("narration", "")
        chunks = _cue_chunks(caption, words_per_cue, max_chars)
        weights = [max(1, sum(len(word) for word in chunk)) for chunk in chunks]
        total_weight = sum(weights) or 1
        t = offset
        for chunk, weight in zip(chunks, weights):
            cue_duration = float(dur) * weight / total_weight
            text = _wrap_words(chunk, max_chars)
            if sc.get("uppercase") and not bangla:
                text = text.upper()
            lines.append(f"Dialogue: 0,{_ts(t)},{_ts(t + cue_duration)},Default,,0,0,0,,{{\\fad(100,100)}}{text}")
            t += cue_duration
        offset += float(dur)
    out_path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
