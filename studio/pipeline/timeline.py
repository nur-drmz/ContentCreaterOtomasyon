from __future__ import annotations

from typing import Any

_SNAP_TOLERANCE_SECONDS = 0.75
_OVER_BUDGET_TOLERANCE = 1.2
_MIN_CLIP_SECONDS = 1.5


def _snap_to_transcript_boundary(
    timestamp: float, boundaries: list[float], *, tolerance: float = _SNAP_TOLERANCE_SECONDS
) -> float:
    """Snap to the nearest transcript segment boundary within tolerance, else unchanged."""
    nearest = min(boundaries, key=lambda b: abs(b - timestamp), default=None) if boundaries else None
    if nearest is not None and abs(nearest - timestamp) <= tolerance:
        return nearest
    return timestamp


def _rebalance_to_target(clips: list[dict[str, Any]], target_duration_seconds: float) -> None:
    """Shrink unlocked clips proportionally so the total lands near the target duration."""
    total = sum(clip["end_ts"] - clip["start_ts"] for clip in clips)
    if total <= target_duration_seconds * _OVER_BUDGET_TOLERANCE or total <= 0:
        return

    locked_total = sum(
        clip["end_ts"] - clip["start_ts"] for clip in clips if clip["locked"]
    )
    unlocked = [clip for clip in clips if not clip["locked"]]
    unlocked_total = sum(clip["end_ts"] - clip["start_ts"] for clip in unlocked)
    budget = max(target_duration_seconds - locked_total, 0.0)
    if unlocked_total <= 0 or budget <= 0:
        return

    scale = budget / unlocked_total
    for clip in unlocked:
        duration = clip["end_ts"] - clip["start_ts"]
        new_duration = max(duration * scale, _MIN_CLIP_SECONDS)
        clip["end_ts"] = clip["start_ts"] + new_duration


def propose_timeline(
    *,
    selections: list[Any],
    target_duration_seconds: float,
    transcript_boundaries_by_moment: dict[Any, list[float]],
) -> dict[str, Any]:
    """Build a clip list from selected moments, per docs/pre-alpha-local/03 §7.

    ``selections`` are ``MomentSelection`` rows (decision=SELECTED, ordered).
    Locked clips keep their exact trim points; unlocked clips may be snapped
    to transcript boundaries and rebalanced to fit the target duration.
    """
    clips: list[dict[str, Any]] = []
    for order_index, selection in enumerate(selections):
        moment = selection.hot_moment
        start_ts = selection.trim_in if selection.trim_in is not None else moment.core_start_ts
        end_ts = selection.trim_out if selection.trim_out is not None else moment.core_end_ts

        if not selection.locked:
            boundaries = transcript_boundaries_by_moment.get(moment.id, [])
            start_ts = _snap_to_transcript_boundary(start_ts, boundaries)
            end_ts = _snap_to_transcript_boundary(end_ts, boundaries)
            if end_ts <= start_ts:
                start_ts, end_ts = moment.core_start_ts, moment.core_end_ts

        clips.append(
            {
                "hot_moment_id": str(moment.id),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "order_index": order_index,
                "locked": selection.locked,
            }
        )

    _rebalance_to_target(clips, target_duration_seconds)
    total_duration = sum(clip["end_ts"] - clip["start_ts"] for clip in clips)
    return {
        "clips": clips[:6] if len(clips) > 6 else clips,
        "target_duration_seconds": target_duration_seconds,
        "total_duration_seconds": total_duration,
    }
