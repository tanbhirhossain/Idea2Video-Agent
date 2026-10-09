"""Duration budgeting shared by script generation and final rendering."""
from __future__ import annotations

import math


def end_screen_seconds(video_cfg: dict) -> float:
    """Seconds reserved for the outro card, if it will actually be rendered."""
    end_screen = video_cfg.get("end_screen") or {}
    if not end_screen.get("enabled") or not str(end_screen.get("channel", "")).strip():
        return 0.0
    return max(0.0, float(end_screen.get("seconds", 0) or 0))


def render_overhead_seconds(video_cfg: dict) -> float:
    """Visual time added outside narration: the final transition pad and outro."""
    transition = max(0.0, float(video_cfg.get("transition_s", 0) or 0))
    return transition + end_screen_seconds(video_cfg)


def narration_target_seconds(video_cfg: dict, requested_seconds: float) -> float:
    """Return how much narration fits inside a requested *final video* duration."""
    requested = float(requested_seconds)
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("Video duration must be a positive number of seconds.")
    if requested > 900:
        raise ValueError("Video duration is limited to 900 seconds (15 minutes).")

    target = requested - render_overhead_seconds(video_cfg)
    minimum = max(4.0, float(video_cfg.get("min_narration_seconds", 4) or 4))
    if target < minimum:
        overhead = render_overhead_seconds(video_cfg)
        raise ValueError(
            f"A {requested:g}s video leaves only {target:.1f}s for narration after the "
            f"{overhead:.1f}s transition/outro. Choose at least {minimum + overhead:.0f}s."
        )
    return target


def duration_tolerance(video_cfg: dict, narration_target: float) -> float:
    """Accept small natural TTS variation while correcting material duration drift."""
    absolute = max(0.0, float(video_cfg.get("duration_tolerance_s", 2.0) or 0.0))
    ratio = max(0.0, float(video_cfg.get("duration_tolerance_ratio", 0.04) or 0.0))
    return max(absolute, float(narration_target) * ratio)


def final_duration_from_narration(video_cfg: dict, narration_seconds: float) -> float:
    """Expected rendered duration, including transition padding and any outro."""
    return float(narration_seconds) + render_overhead_seconds(video_cfg)
