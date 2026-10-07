"""ASS subtitle generation. Timing = proportional to character count inside each scene's audio."""


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(scenes, durations, W, H, cfg, out_path):
    sc = cfg["subtitles"]
    size = int(W * 0.08) if H > W else int(H * 0.05)
    margin_v = int(H * 0.18) if H > W else int(H * 0.08)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{sc['font']},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3, size // 14)},2,2,{int(W*0.06)},{int(W*0.06)},{margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines, offset, n = [], 0.0, sc["words_per_cue"]
    for scene, dur in zip(scenes, durations):
        words = scene["narration"].replace("{", "").replace("}", "").split()
        chunks = [" ".join(words[i:i + n]) for i in range(0, len(words), n)]
        total = sum(len(c) for c in chunks) or 1
        t = offset
        for c in chunks:
            d = dur * len(c) / total
            text = c.upper() if sc["uppercase"] else c
            lines.append(f"Dialogue: 0,{_ts(t)},{_ts(t + d)},Default,,0,0,0,,{{\\fad(60,60)}}{text}")
            t += d
        offset += dur
    out_path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
