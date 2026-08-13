from __future__ import annotations

from typing import Any

# Mirrors the Hot Moment weighting in docs/pre-alpha-local/03-media-ai-pipeline.md
# section 5. studio.demo_data imports this list so the illustrative preview
# communicates the *shape* of the real scoring output.
SCORE_BREAKDOWN_LABELS = [
    ("narrative", "Narrative importance", 22),
    ("emotional", "Emotional intensity", 18),
    ("audio", "Audio & music rise", 15),
    ("motion", "Motion / action", 15),
    ("character", "Character importance", 10),
    ("theory", "Theory potential", 10),
    ("hook", "Hook suitability", 5),
    ("comprehensibility", "Independent comprehensibility", 5),
]

FORMULA_VERSION = "hot-moment-v1"

# Maps our score component keys to the VLM's required JSON schema fields
# (docs/pre-alpha-local/03-media-ai-pipeline.md section 4).
_VLM_FIELD_BY_KEY = {
    "narrative": "narrativeImportance",
    "emotional": "emotionalIntensity",
    "theory": "theoryPotential",
    "hook": "hookSuitability",
    "comprehensibility": "independentComprehensibility",
}

_CORE_TRIM_FRACTION = 0.15
_OVERLAP_SUPPRESSION_THRESHOLD = 0.5


def _component_value(
    key: str, candidate: dict[str, Any], vlm_result: dict[str, Any] | None
) -> float:
    """Return a 0-100 component value. Never invents a value for unavailable VLM data."""
    if key == "audio":
        return round(candidate.get("audio_signal", 0.0) * 100)
    if key == "motion":
        return round(candidate.get("motion_signal", 0.0) * 100)
    if key == "character":
        if vlm_result and vlm_result.get("characters"):
            return 100.0
        return 0.0

    vlm_field = _VLM_FIELD_BY_KEY[key]
    if vlm_result and vlm_result.get(vlm_field) is not None:
        return round(float(vlm_result[vlm_field]) * 100)
    return 0.0


def build_score_breakdown(
    candidate: dict[str, Any], vlm_result: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "weight": weight,
            "value": _component_value(key, candidate, vlm_result),
        }
        for key, label, weight in SCORE_BREAKDOWN_LABELS
    ]


def compute_base_score(breakdown: list[dict[str, Any]]) -> int:
    weighted = sum(item["value"] * item["weight"] / 100 for item in breakdown)
    return max(0, min(100, round(weighted)))


def _overlap_fraction(a: dict[str, Any], b: dict[str, Any]) -> float:
    overlap_start = max(a["start_ts"], b["start_ts"])
    overlap_end = min(a["end_ts"], b["end_ts"])
    overlap = max(0.0, overlap_end - overlap_start)
    shorter = min(a["end_ts"] - a["start_ts"], b["end_ts"] - b["start_ts"])
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _suppress_overlaps(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(scored, key=lambda item: item["base_score"], reverse=True)
    kept: list[dict[str, Any]] = []
    for candidate in ordered:
        if any(_overlap_fraction(candidate, other) > _OVERLAP_SUPPRESSION_THRESHOLD for other in kept):
            continue
        kept.append(candidate)
    return kept


def score_candidates(
    candidates: list[dict[str, Any]],
    vlm_results: dict[int, dict[str, Any] | None],
    *,
    target_count: int,
) -> list[dict[str, Any]]:
    """Score, merge/suppress overlaps, and cap to the adaptive target count."""
    scored: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        vlm_result = vlm_results.get(index)
        breakdown = build_score_breakdown(candidate, vlm_result)
        base_score = compute_base_score(breakdown)
        duration = candidate["end_ts"] - candidate["start_ts"]
        trim = duration * _CORE_TRIM_FRACTION
        characters = (vlm_result or {}).get("characters") or []
        spoiler_level = (vlm_result or {}).get("spoilerLevel") or "Medium"
        spoiler_level = {"low": "Low", "medium": "Medium", "high": "High"}.get(
            str(spoiler_level).lower(), "Medium"
        )
        reasons = list((vlm_result or {}).get("reasons") or [])
        if vlm_result is None:
            reasons.append("VLM enrichment unavailable; score reflects deterministic signals only.")
        summary = (vlm_result or {}).get("summary") or candidate.get("transcript_excerpt", "")[:280]

        scored.append(
            {
                "start_ts": candidate["start_ts"],
                "end_ts": candidate["end_ts"],
                "core_start_ts": candidate["start_ts"] + trim,
                "core_end_ts": candidate["end_ts"] - trim,
                "base_score": base_score,
                "score_breakdown": breakdown,
                "summary": summary,
                "characters": characters,
                "spoiler_level": spoiler_level,
                "reasons": reasons,
                "transcript_evidence": candidate.get("transcript_excerpt", ""),
            }
        )

    suppressed = _suppress_overlaps(scored)
    suppressed.sort(key=lambda item: item["base_score"], reverse=True)
    return suppressed[:target_count]
