import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .models import AnalysisRun, AuditEvent, HotMoment, MomentSelection, Project, SourceAsset

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov"}


@dataclass(frozen=True)
class LibraryVideo:
    relative_path: str
    filename: str
    size_bytes: int
    size_label: str
    extension: str
    modified_at: float


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, remainder = divmod(total_seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


def format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def source_library_root() -> Path:
    return Path(settings.SOURCE_LIBRARY_ROOT).resolve()


def resolve_library_video(relative_path: str) -> Path:
    root = source_library_root()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("The selected file is outside the approved source library.")
    if candidate.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError("The selected file type is not supported.")
    if not candidate.is_file():
        raise ValueError("The selected source video no longer exists.")
    return candidate


def discover_library_videos() -> list[LibraryVideo]:
    root = source_library_root()
    if not root.exists():
        return []

    videos = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        stat = path.stat()
        videos.append(
            LibraryVideo(
                relative_path=str(path.relative_to(root)),
                filename=path.name,
                size_bytes=stat.st_size,
                size_label=format_bytes(stat.st_size),
                extension=path.suffix.removeprefix(".").upper(),
                modified_at=stat.st_mtime,
            )
        )
    return sorted(videos, key=lambda video: video.modified_at, reverse=True)


def create_analysis_run(
    *,
    project: Project,
    source_asset: SourceAsset,
    pipeline_version: str,
    config_hash: str = "",
    status: str | None = None,
    current_stage: str = "",
    started_at=None,
    completed_at=None,
) -> AnalysisRun:
    """The single trusted creation path for ``AnalysisRun`` rows.

    This validates (via ``full_clean()``, which invokes ``AnalysisRun.clean()``)
    that ``source_asset`` actually belongs to ``project`` before saving, so
    every caller — the demo seeder today, the future sequential analysis
    worker later — gets the same project/source integrity guarantee without
    repeating the check. Prefer this over ``AnalysisRun.objects.create()``
    for any code that creates analysis runs.
    """
    analysis_run = AnalysisRun(
        project=project,
        source_asset=source_asset,
        pipeline_version=pipeline_version,
        config_hash=config_hash,
        status=status or AnalysisRun.Status.QUEUED,
        current_stage=current_stage,
        started_at=started_at,
        completed_at=completed_at,
    )
    analysis_run.full_clean()
    analysis_run.save()
    return analysis_run


def hot_moment_duration_seconds(hot_moment: HotMoment) -> int:
    return round(hot_moment.end_ts - hot_moment.start_ts)


def build_moment_selection_summary(project: Project) -> dict:
    """Authoritative, database-derived Moment Explorer summary values.

    Both the full-page project Moment Explorer and the AJAX decision
    endpoint call this, so the browser is never trusted to calculate
    selected/rejected counts or selected duration itself — every number
    returned to the client is recomputed from persisted state.
    """
    selections = MomentSelection.objects.filter(project=project).select_related("hot_moment")
    selected_moments = [
        selection.hot_moment
        for selection in selections
        if selection.decision == MomentSelection.Decision.SELECTED
    ]
    rejected_count = sum(
        1 for selection in selections if selection.decision == MomentSelection.Decision.REJECTED
    )
    selected_duration = sum(hot_moment_duration_seconds(moment) for moment in selected_moments)
    target_duration = project.target_duration_seconds

    duration_percent = 0
    if target_duration:
        duration_percent = max(0, min(100, round(selected_duration / target_duration * 100)))

    return {
        "selected_count": len(selected_moments),
        "rejected_count": rejected_count,
        "selected_duration": selected_duration,
        "target_duration": target_duration,
        "duration_percent": duration_percent,
    }


def split_into_sentences(text: str) -> list[str]:
    """Split narration text into sentences: newlines first, then terminal punctuation."""
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for piece in _SENTENCE_SPLIT_RE.split(line):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


def record_audit_event(project: Project, action: str, **details: object) -> AuditEvent:
    """Log a local audit record for an external (network) call.

    Per docs/pre-alpha-local/06-security-privacy.md §6, logs should omit
    full text bodies when a shorter reference suffices — callers should pass
    lengths/hashes/costs in ``details``, not raw narration text.
    """
    return AuditEvent.objects.create(project=project, action=action, details=details)

