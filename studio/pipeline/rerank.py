from __future__ import annotations

import re
from typing import Any

RERANK_FORMULA_VERSION = "focus-prompt-v1"

_BASE_WEIGHT = 0.75
_PROMPT_WEIGHT = 0.25
_WORD_PATTERN = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> set[str]:
    return set(_WORD_PATTERN.findall(text.lower()))


def prompt_relevance(prompt_text: str, hot_moment: Any) -> float:
    """Local (no network) semantic-relevance proxy: token-overlap of stored data.

    Uses only already-stored transcript evidence, summary and reasons, so a
    prompt change never re-triggers transcription/detection/VLM stages
    (docs/pre-alpha-local/03-media-ai-pipeline.md §6).
    """
    prompt_tokens = _tokenize(prompt_text)
    if not prompt_tokens:
        return 0.0

    moment_text = " ".join(
        [
            hot_moment.transcript_evidence or "",
            hot_moment.summary or "",
            " ".join(hot_moment.reasons or []),
            " ".join(hot_moment.characters or []),
        ]
    )
    moment_tokens = _tokenize(moment_text)
    if not moment_tokens:
        return 0.0

    overlap = prompt_tokens & moment_tokens
    return round(len(overlap) / len(prompt_tokens), 4) if prompt_tokens else 0.0


def rank_moments(prompt_text: str, hot_moments: list[Any]) -> list[dict[str, Any]]:
    """Compute final_rank = 0.75*base_score + 0.25*(prompt_relevance*100) per §6.

    base_score is 0-100; prompt_relevance is stored 0-1 (matches the
    MomentRanking model's validated range) and scaled to 0-100 here so both
    terms combine on the same scale.
    """
    scored = []
    for hot_moment in hot_moments:
        relevance = prompt_relevance(prompt_text, hot_moment)
        final_score = _BASE_WEIGHT * hot_moment.base_score + _PROMPT_WEIGHT * (relevance * 100)
        scored.append(
            {
                "hot_moment": hot_moment,
                "prompt_relevance": relevance,
                "final_score": final_score,
            }
        )

    scored.sort(key=lambda item: item["final_score"], reverse=True)
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
    return scored
