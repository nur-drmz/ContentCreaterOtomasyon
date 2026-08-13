from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings

from . import PipelineStageError

MODEL_NAME = "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"

_REQUIRED_FIELDS = {
    "summary": str,
    "characters": list,
    "narrativeImportance": (int, float),
    "emotionalIntensity": (int, float),
    "theoryPotential": (int, float),
    "hookSuitability": (int, float),
    "independentComprehensibility": (int, float),
    "spoilerLevel": str,
    "reasons": list,
}
_SPOILER_LEVELS = {"low", "medium", "high"}


def validate_vlm_payload(payload: Any) -> dict[str, Any]:
    """Validate the exact JSON schema from docs/pre-alpha-local/03-media-ai-pipeline.md §4.

    Unexpected fields are ignored. Missing/malformed required fields raise
    ValueError so the caller can retry once with a repair prompt.
    """
    if not isinstance(payload, dict):
        raise ValueError("VLM payload must be a JSON object.")

    result: dict[str, Any] = {}
    for field, expected_type in _REQUIRED_FIELDS.items():
        if field not in payload:
            raise ValueError(f"VLM payload is missing required field: {field}")
        value = payload[field]
        if not isinstance(value, expected_type):
            raise ValueError(f"VLM payload field {field!r} has the wrong type.")
        result[field] = value

    for numeric_field in (
        "narrativeImportance",
        "emotionalIntensity",
        "theoryPotential",
        "hookSuitability",
        "independentComprehensibility",
    ):
        value = float(result[numeric_field])
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"VLM payload field {numeric_field!r} must be between 0 and 1.")
        result[numeric_field] = value

    if str(result["spoilerLevel"]).lower() not in _SPOILER_LEVELS:
        raise ValueError("VLM payload field 'spoilerLevel' must be low, medium or high.")
    result["spoilerLevel"] = str(result["spoilerLevel"]).capitalize()

    return result


def _filter_characters(characters: list[Any], allowlist: list[str]) -> list[str]:
    allowed = {name.lower() for name in allowlist}
    filtered: list[str] = []
    for character in characters:
        name = str(character)
        filtered.append(name if name.lower() in allowed else "unknown")
    return filtered


class QwenVLModel:
    """Thin wrapper around the local Qwen2.5-VL model.

    Loading is deferred to first use so importing this module never touches
    the GPU or downloads weights; the worker only loads it once the deterministic
    stages have unloaded Whisper (sequential GPU load, per
    docs/pre-alpha-local/02-local-architecture.md §4).
    """

    def __init__(self) -> None:
        self._model = None
        self._processor = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoProcessor, Qwen2VLForConditionalGeneration
        except ImportError as exc:
            raise PipelineStageError(
                "VLM_DEPENDENCIES_MISSING", f"VLM dependencies are not installed: {exc}"
            ) from exc

        if not torch.cuda.is_available():
            raise PipelineStageError("CUDA_UNAVAILABLE", "CUDA is unavailable for VLM enrichment.")

        model_root = Path(settings.MODEL_ROOT) / "vlm"
        # local_files_only=True: per docs/pre-alpha-local/06-security-privacy.md §2,
        # "no automatic model download occurs during an analysis job." The model
        # must already be present from an explicit environment/model update step;
        # otherwise this raises and the caller falls back to deterministic-only
        # scoring rather than silently reaching out to the network.
        try:
            self._model = Qwen2VLForConditionalGeneration.from_pretrained(
                MODEL_NAME, cache_dir=str(model_root), device_map="cuda", local_files_only=True
            )
            self._processor = AutoProcessor.from_pretrained(
                MODEL_NAME, cache_dir=str(model_root), local_files_only=True
            )
        except OSError as exc:
            raise PipelineStageError(
                "VLM_MODEL_NOT_CACHED",
                f"The VLM model is not locally cached; run the explicit model "
                f"download step first. {exc}",
            ) from exc

    def generate(self, *, keyframes: list[Any], prompt: str) -> str:
        """Run one generation pass and return the raw text response."""
        self._ensure_loaded()
        messages = [
            {
                "role": "user",
                "content": [{"type": "image", "image": frame} for frame in keyframes]
                + [{"type": "text", "text": prompt}],
            }
        ]
        text_prompt = self._processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = self._processor(text=[text_prompt], images=keyframes, return_tensors="pt").to("cuda")
        generated_ids = self._model.generate(**inputs, max_new_tokens=512)
        output_ids = generated_ids[:, inputs["input_ids"].shape[1] :]
        return self._processor.batch_decode(output_ids, skip_special_tokens=True)[0]

    def unload(self) -> None:
        if self._model is None:
            return
        import torch

        del self._model
        del self._processor
        self._model = None
        self._processor = None
        torch.cuda.empty_cache()


def _build_prompt(
    *, transcript_excerpt: str, project_title: str, episode_label: str, known_characters: list[str]
) -> str:
    return (
        "You are analyzing a short video segment for a highlights tool. "
        f"Show: {project_title} {episode_label}.\n"
        f"Known characters (use exactly these names or 'unknown'): {', '.join(known_characters) or 'none provided'}.\n"
        f"Transcript excerpt: {transcript_excerpt or '(no dialogue)'}\n\n"
        "Respond with ONLY a JSON object with exactly these fields: "
        "summary (string), characters (array of strings from the known list or 'unknown'), "
        "narrativeImportance, emotionalIntensity, theoryPotential, hookSuitability, "
        "independentComprehensibility (each a number from 0.0 to 1.0), "
        "spoilerLevel ('low'|'medium'|'high'), reasons (array of short strings)."
    )


def enrich_candidate(
    model: QwenVLModel,
    *,
    keyframes: list[Any],
    transcript_excerpt: str,
    project_title: str,
    episode_label: str,
    known_characters: list[str],
) -> dict[str, Any] | None:
    """Enrich one pre-candidate. Returns None (never invented values) on persistent failure."""
    prompt = _build_prompt(
        transcript_excerpt=transcript_excerpt,
        project_title=project_title,
        episode_label=episode_label,
        known_characters=known_characters,
    )

    for attempt in range(2):
        try:
            raw = model.generate(keyframes=keyframes, prompt=prompt)
            payload = json.loads(raw)
            validated = validate_vlm_payload(payload)
        except (PipelineStageError,):
            raise
        except (ValueError, json.JSONDecodeError):
            if attempt == 0:
                prompt = (
                    prompt
                    + "\n\nYour previous response was invalid JSON or missing a required field. "
                    "Respond again with ONLY the corrected JSON object."
                )
                continue
            return None

        validated["characters"] = _filter_characters(validated["characters"], known_characters)
        return validated

    return None
