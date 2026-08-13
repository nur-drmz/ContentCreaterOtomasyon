from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineStageError(Exception):
    """Raised by any pipeline stage handler on a recoverable, reported failure."""

    code: str
    detail: str

    def __str__(self) -> str:
        return self.detail
