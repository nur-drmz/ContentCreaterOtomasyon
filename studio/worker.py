from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import preflight, services
from .models import (
    AnalysisRun,
    FocusPromptRun,
    HotMoment,
    Job,
    JobCheckpoint,
    MomentRanking,
    MomentSelection,
    RenderArtifact,
    Scene,
    SourceAsset,
    TimelineVersion,
    TranscriptSegment,
    VoiceOver,
    VoiceSegment,
)
from .pipeline import PipelineStageError
from .pipeline import audio as pipeline_audio
from .pipeline import candidates as pipeline_candidates
from .pipeline import keyframes as pipeline_keyframes
from .pipeline import motion as pipeline_motion
from .pipeline import proxy as pipeline_proxy
from .pipeline import render as pipeline_render
from .pipeline import rerank as pipeline_rerank
from .pipeline import scenes as pipeline_scenes
from .pipeline import scoring as pipeline_scoring
from .pipeline import timeline as pipeline_timeline
from .pipeline import transcript as pipeline_transcript
from .pipeline import vlm as pipeline_vlm
from .voice.providers import VoiceProviderError, get_provider


StageHandler = Callable[[Any], dict[str, Any]]

PIPELINE_VERSION = "pre-alpha-1"

PREFLIGHT_STAGES = (
    "SOURCE_PATH",
    "SOURCE_LIMITS",
    "FFPROBE",
    "STORAGE",
    "CUDA",
)
PREFLIGHT_PROGRESS = {
    "SOURCE_PATH": 5,
    "SOURCE_LIMITS": 8,
    "FFPROBE": 12,
    "STORAGE": 18,
    "CUDA": 25,
}

ANALYSIS_STAGES = (
    "PROXY",
    "TRANSCRIPT",
    "SCENES",
    "AUDIO",
    "MOTION",
    "PRE_CANDIDATES",
    "VLM_ENRICHMENT",
    "SCORING",
)
ANALYSIS_PROGRESS = {
    "PROXY": 30,
    "TRANSCRIPT": 45,
    "SCENES": 55,
    "AUDIO": 65,
    "MOTION": 75,
    "PRE_CANDIDATES": 85,
    "VLM_ENRICHMENT": 95,
    "SCORING": 100,
}
VLM_KEYFRAMES_PER_CANDIDATE = 4


def claim_next_analysis_job() -> Job | None:
    """Claim one queued analysis job using a SQLite-safe conditional update."""
    with transaction.atomic():
        candidate_ids = Job.objects.filter(
            job_type=Job.Type.ANALYZE_SOURCE,
            status=Job.Status.QUEUED,
        ).order_by("queued_at").values_list("id", flat=True)
        now = timezone.now()
        for candidate_id in candidate_ids:
            changed = Job.objects.filter(
                id=candidate_id,
                status=Job.Status.QUEUED,
            ).update(
                status=Job.Status.RUNNING,
                current_stage="Starting source preflight",
                started_at=now,
                finished_at=None,
                failure_code="",
                failure_detail="",
                attempt_count=F("attempt_count") + 1,
                updated_at=now,
            )
            if changed == 1:
                return Job.objects.select_related("project__source").get(id=candidate_id)
    return None


def _claim_next_job(job_type: str, *, starting_stage: str) -> Job | None:
    """Generic SQLite-safe conditional claim, shared by non-analysis job types."""
    with transaction.atomic():
        candidate_ids = (
            Job.objects.filter(job_type=job_type, status=Job.Status.QUEUED)
            .order_by("queued_at")
            .values_list("id", flat=True)
        )
        now = timezone.now()
        for candidate_id in candidate_ids:
            changed = Job.objects.filter(id=candidate_id, status=Job.Status.QUEUED).update(
                status=Job.Status.RUNNING,
                current_stage=starting_stage,
                started_at=now,
                finished_at=None,
                failure_code="",
                failure_detail="",
                attempt_count=F("attempt_count") + 1,
                updated_at=now,
            )
            if changed == 1:
                return Job.objects.select_related("project").get(id=candidate_id)
    return None


def _stage_handlers() -> dict[str, StageHandler]:
    return {
        "SOURCE_PATH": lambda source: preflight.validate_source_path(source)[1],
        "SOURCE_LIMITS": preflight.inspect_source_size,
        "FFPROBE": preflight.inspect_source_media,
        "STORAGE": lambda _source: preflight.inspect_storage(),
        "CUDA": lambda _source: preflight.inspect_cuda(),
    }


def _stage_config_hash(job: Job, stage: str) -> str:
    source = job.project.source
    fingerprint: dict[str, Any] = {
        "stage": stage,
        "source_id": str(source.id),
        "relative_path": source.relative_path,
        "max_size": settings.MAX_SOURCE_SIZE_BYTES,
        "max_duration": settings.MAX_SOURCE_DURATION_SECONDS,
    }
    try:
        path, _result = preflight.validate_source_path(source)
        stat = path.stat()
        fingerprint.update(source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
    except (OSError, preflight.PreflightError):
        fingerprint["source_state"] = "unavailable"

    if stage == "FFPROBE":
        fingerprint.update(
            ffprobe_path=str(getattr(settings, "FFPROBE_PATH", "") or ""),
            ffprobe_timeout=settings.FFPROBE_TIMEOUT_SECONDS,
        )
    elif stage == "STORAGE":
        fingerprint.update(
            generated_root=str(settings.GENERATED_MEDIA_ROOT),
            model_root=str(settings.MODEL_ROOT),
            generated_min=settings.MIN_GENERATED_FREE_BYTES,
            model_min=settings.MIN_MODEL_FREE_BYTES,
        )
    elif stage == "CUDA":
        fingerprint["minimum_free_vram"] = settings.MIN_FREE_VRAM_BYTES

    encoded = json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cancel_job(job: Job) -> Job:
    now = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.CANCELLED,
        current_stage="Cancelled between preflight stages",
        finished_at=now,
        updated_at=now,
    )
    job.refresh_from_db()
    return job


def run_preflight(
    job: Job,
    *,
    handlers: dict[str, StageHandler] | None = None,
) -> Job:
    """Run bounded preflight stages outside the short claim transaction."""
    if job.status != Job.Status.RUNNING or job.job_type != Job.Type.ANALYZE_SOURCE:
        raise ValueError("Only a running ANALYZE_SOURCE job can execute preflight.")

    handlers = handlers or _stage_handlers()
    source = job.project.source

    for stage in PREFLIGHT_STAGES:
        job.refresh_from_db(fields=["cancel_requested", "status"])
        if job.cancel_requested:
            return _cancel_job(job)
        if job.status != Job.Status.RUNNING:
            return job

        config_hash = _stage_config_hash(job, stage)
        existing = JobCheckpoint.objects.filter(
            job=job,
            stage=stage,
            stage_config_hash=config_hash,
            status=JobCheckpoint.Status.SUCCEEDED,
        ).first()
        if existing:
            Job.objects.filter(id=job.id).update(
                current_stage=f"{stage} reused",
                progress=PREFLIGHT_PROGRESS[stage],
                updated_at=timezone.now(),
            )
            continue

        now = timezone.now()
        checkpoint, _created = JobCheckpoint.objects.update_or_create(
            job=job,
            stage=stage,
            stage_config_hash=config_hash,
            defaults={
                "status": JobCheckpoint.Status.RUNNING,
                "result": {},
                "started_at": now,
                "completed_at": None,
                "failure_code": "",
                "failure_detail": "",
            },
        )
        Job.objects.filter(id=job.id).update(
            current_stage=f"Preflight: {stage}",
            updated_at=now,
        )
        try:
            result = handlers[stage](source)
        except preflight.PreflightError as exc:
            completed_at = timezone.now()
            checkpoint.status = JobCheckpoint.Status.FAILED
            checkpoint.failure_code = exc.code
            checkpoint.failure_detail = exc.detail
            checkpoint.completed_at = completed_at
            checkpoint.save(
                update_fields=[
                    "status",
                    "failure_code",
                    "failure_detail",
                    "completed_at",
                ]
            )
            Job.objects.filter(id=job.id).update(
                status=Job.Status.FAILED,
                current_stage=f"Preflight failed: {stage}",
                failure_code=exc.code,
                failure_detail=exc.detail,
                finished_at=completed_at,
                updated_at=completed_at,
            )
            job.refresh_from_db()
            return job

        completed_at = timezone.now()
        checkpoint.status = JobCheckpoint.Status.SUCCEEDED
        checkpoint.result = result
        checkpoint.completed_at = completed_at
        checkpoint.save(update_fields=["status", "result", "completed_at"])
        Job.objects.filter(id=job.id).update(
            current_stage=f"Preflight complete: {stage}",
            progress=PREFLIGHT_PROGRESS[stage],
            updated_at=completed_at,
        )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.PAUSED,
        current_stage="Preflight complete; transcript stage not implemented",
        progress=PREFLIGHT_PROGRESS["CUDA"],
        failure_code="",
        failure_detail="",
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


def _get_or_create_analysis_run(job: Job, source: SourceAsset) -> AnalysisRun:
    config_hash = hashlib.sha256(
        f"{source.relative_path}:{source.size_bytes}:{source.duration_seconds}".encode()
    ).hexdigest()
    existing = (
        AnalysisRun.objects.filter(
            project=job.project,
            source_asset=source,
            pipeline_version=PIPELINE_VERSION,
            config_hash=config_hash,
        )
        .exclude(status=AnalysisRun.Status.FAILED)
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing
    return services.create_analysis_run(
        project=job.project,
        source_asset=source,
        pipeline_version=PIPELINE_VERSION,
        config_hash=config_hash,
        status=AnalysisRun.Status.RUNNING,
        current_stage="Starting analysis pipeline",
        started_at=timezone.now(),
    )


def _analysis_stage_handlers(
    job: Job, analysis_run: AnalysisRun, source: SourceAsset, source_path
) -> dict[str, StageHandler]:
    def handle_proxy(_source: SourceAsset) -> dict[str, Any]:
        return pipeline_proxy.create_proxy(source_path, str(job.project_id))

    def handle_transcript(_source: SourceAsset) -> dict[str, Any]:
        probe_metadata = source.probe_metadata or {}
        result = pipeline_transcript.extract_transcript(source_path, probe_metadata)
        segments = [s for s in result["segments"] if s["end_ts"] > s["start_ts"]]
        TranscriptSegment.objects.filter(analysis_run=analysis_run).delete()
        TranscriptSegment.objects.bulk_create(
            [
                TranscriptSegment(
                    analysis_run=analysis_run,
                    start_ts=segment["start_ts"],
                    end_ts=segment["end_ts"],
                    text=segment.get("text", ""),
                    source=result["source"],
                    confidence=segment.get("confidence"),
                    language=result.get("language", ""),
                )
                for segment in segments
            ]
        )
        return {"source": result["source"], "segment_count": len(segments)}

    def handle_scenes(_source: SourceAsset) -> dict[str, Any]:
        proxy = pipeline_proxy.proxy_path_for(job.project_id)
        scenes = pipeline_scenes.detect_scenes(proxy)
        Scene.objects.filter(analysis_run=analysis_run).delete()
        Scene.objects.bulk_create(
            [
                Scene(
                    analysis_run=analysis_run,
                    start_ts=scene["start_ts"],
                    end_ts=scene["end_ts"],
                )
                for scene in scenes
            ]
        )
        return {"scene_count": len(scenes)}

    def _stored_scenes() -> list[dict[str, Any]]:
        return list(
            Scene.objects.filter(analysis_run=analysis_run)
            .order_by("start_ts")
            .values("start_ts", "end_ts")
        )

    def handle_audio(_source: SourceAsset) -> dict[str, Any]:
        return pipeline_audio.analyze_audio(source_path, _stored_scenes())

    def handle_motion(_source: SourceAsset) -> dict[str, Any]:
        proxy = pipeline_proxy.proxy_path_for(job.project_id)
        return pipeline_motion.analyze_motion(proxy, _stored_scenes())

    def handle_pre_candidates(_source: SourceAsset) -> dict[str, Any]:
        transcript_segments = list(
            TranscriptSegment.objects.filter(analysis_run=analysis_run)
            .order_by("start_ts")
            .values("start_ts", "end_ts", "text")
        )
        audio_checkpoint = (
            JobCheckpoint.objects.filter(
                job=job, stage="AUDIO", status=JobCheckpoint.Status.SUCCEEDED
            )
            .order_by("-completed_at")
            .first()
        )
        motion_checkpoint = (
            JobCheckpoint.objects.filter(
                job=job, stage="MOTION", status=JobCheckpoint.Status.SUCCEEDED
            )
            .order_by("-completed_at")
            .first()
        )
        candidates = pipeline_candidates.generate_pre_candidates(
            transcript_segments=transcript_segments,
            scenes=_stored_scenes(),
            audio_features=audio_checkpoint.result if audio_checkpoint else {},
            motion_features=motion_checkpoint.result if motion_checkpoint else {},
            source_duration_seconds=source.duration_seconds or 0.0,
        )
        return {"candidates": candidates, "candidate_count": len(candidates)}

    def _pre_candidates() -> list[dict[str, Any]]:
        return list(_stage_result(job, "PRE_CANDIDATES").get("candidates", []))

    def handle_vlm_enrichment(_source: SourceAsset) -> dict[str, Any]:
        candidates = _pre_candidates()
        model = pipeline_vlm.QwenVLModel()
        try:
            model._ensure_loaded()
            vlm_available = True
        except PipelineStageError:
            vlm_available = False

        vlm_results: dict[str, dict[str, Any] | None] = {}
        if vlm_available:
            proxy = pipeline_proxy.proxy_path_for(job.project_id)
            try:
                for index, candidate in enumerate(candidates):
                    keyframes = pipeline_keyframes.extract_keyframes(
                        proxy,
                        candidate["start_ts"],
                        candidate["end_ts"],
                        VLM_KEYFRAMES_PER_CANDIDATE,
                    )
                    vlm_results[str(index)] = pipeline_vlm.enrich_candidate(
                        model,
                        keyframes=keyframes,
                        transcript_excerpt=candidate.get("transcript_excerpt", ""),
                        project_title=job.project.title,
                        episode_label=job.project.episode or "",
                        known_characters=job.project.known_characters or [],
                    )
            finally:
                model.unload()
        else:
            vlm_results = {str(index): None for index in range(len(candidates))}

        return {"vlm_available": vlm_available, "vlm_results": vlm_results}

    def handle_scoring(_source: SourceAsset) -> dict[str, Any]:
        candidates = _pre_candidates()
        vlm_result_map = _stage_result(job, "VLM_ENRICHMENT").get("vlm_results", {})
        vlm_results = {int(key): value for key, value in vlm_result_map.items()}
        target_count = pipeline_candidates.adaptive_target_count(source.duration_seconds or 0.0)
        scored = pipeline_scoring.score_candidates(candidates, vlm_results, target_count=target_count)

        HotMoment.objects.filter(analysis_run=analysis_run).delete()
        HotMoment.objects.bulk_create(
            [
                HotMoment(
                    analysis_run=analysis_run,
                    start_ts=item["start_ts"],
                    end_ts=item["end_ts"],
                    core_start_ts=item["core_start_ts"],
                    core_end_ts=item["core_end_ts"],
                    base_score=item["base_score"],
                    score_breakdown=item["score_breakdown"],
                    summary=item["summary"],
                    characters=item["characters"],
                    spoiler_level=item["spoiler_level"],
                    reasons=item["reasons"],
                    transcript_evidence=item["transcript_evidence"],
                    is_demo=False,
                )
                for item in scored
            ]
        )
        completed_at = timezone.now()
        AnalysisRun.objects.filter(id=analysis_run.id).update(
            status=AnalysisRun.Status.SUCCEEDED,
            current_stage="Scoring complete",
            completed_at=completed_at,
        )
        return {"hot_moment_count": len(scored), "target_count": target_count}

    return {
        "PROXY": handle_proxy,
        "TRANSCRIPT": handle_transcript,
        "SCENES": handle_scenes,
        "AUDIO": handle_audio,
        "MOTION": handle_motion,
        "PRE_CANDIDATES": handle_pre_candidates,
        "VLM_ENRICHMENT": handle_vlm_enrichment,
        "SCORING": handle_scoring,
    }


def _stage_result(job: Job, stage: str) -> dict[str, Any]:
    checkpoint = (
        JobCheckpoint.objects.filter(job=job, stage=stage, status=JobCheckpoint.Status.SUCCEEDED)
        .order_by("-completed_at")
        .first()
    )
    return checkpoint.result if checkpoint else {}


def _analysis_stage_config_hash(job: Job, stage: str, source: SourceAsset) -> str:
    fingerprint: dict[str, Any] = {
        "stage": stage,
        "pipeline_version": PIPELINE_VERSION,
        "source_id": str(source.id),
        "relative_path": source.relative_path,
    }
    try:
        path, _result = preflight.validate_source_path(source)
        stat = path.stat()
        fingerprint.update(source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
    except (OSError, preflight.PreflightError):
        fingerprint["source_state"] = "unavailable"

    if stage == "PROXY":
        fingerprint["proxy_max_height"] = getattr(settings, "PROXY_MAX_HEIGHT", 480)
    elif stage == "TRANSCRIPT":
        fingerprint["whisper_model"] = getattr(settings, "WHISPER_MODEL_NAME", "distil-large-v3.5")
    elif stage == "VLM_ENRICHMENT":
        fingerprint["vlm_model"] = pipeline_vlm.MODEL_NAME
        fingerprint["known_characters"] = sorted(job.project.known_characters or [])
    elif stage == "SCORING":
        fingerprint["formula_version"] = pipeline_scoring.FORMULA_VERSION

    encoded = json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_analysis(
    job: Job,
    *,
    handlers: dict[str, StageHandler] | None = None,
) -> Job:
    """Run the deterministic analysis stages (proxy through pre-candidates)."""
    if job.job_type != Job.Type.ANALYZE_SOURCE:
        raise ValueError("Only an ANALYZE_SOURCE job can execute the analysis stages.")
    if job.status not in (Job.Status.RUNNING, Job.Status.PAUSED):
        raise ValueError("Only a running or paused ANALYZE_SOURCE job can resume analysis.")

    now = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.RUNNING,
        current_stage="Resuming analysis pipeline",
        updated_at=now,
    )
    job.refresh_from_db()

    source = job.project.source
    analysis_run = _get_or_create_analysis_run(job, source)
    path, _result = preflight.validate_source_path(source)
    handlers = handlers or _analysis_stage_handlers(job, analysis_run, source, path)

    for stage in ANALYSIS_STAGES:
        job.refresh_from_db(fields=["cancel_requested", "status"])
        if job.cancel_requested:
            return _cancel_job(job)
        if job.status != Job.Status.RUNNING:
            return job

        config_hash = _analysis_stage_config_hash(job, stage, source)
        existing = JobCheckpoint.objects.filter(
            job=job,
            stage=stage,
            stage_config_hash=config_hash,
            status=JobCheckpoint.Status.SUCCEEDED,
        ).first()
        if existing:
            Job.objects.filter(id=job.id).update(
                current_stage=f"{stage} reused",
                progress=ANALYSIS_PROGRESS[stage],
                updated_at=timezone.now(),
            )
            continue

        stage_started = timezone.now()
        checkpoint, _created = JobCheckpoint.objects.update_or_create(
            job=job,
            stage=stage,
            stage_config_hash=config_hash,
            defaults={
                "status": JobCheckpoint.Status.RUNNING,
                "result": {},
                "started_at": stage_started,
                "completed_at": None,
                "failure_code": "",
                "failure_detail": "",
            },
        )
        Job.objects.filter(id=job.id).update(
            current_stage=f"Analysis: {stage}",
            updated_at=stage_started,
        )
        try:
            result = handlers[stage](source)
        except (preflight.PreflightError, PipelineStageError) as exc:
            completed_at = timezone.now()
            checkpoint.status = JobCheckpoint.Status.FAILED
            checkpoint.failure_code = exc.code
            checkpoint.failure_detail = exc.detail
            checkpoint.completed_at = completed_at
            checkpoint.save(
                update_fields=["status", "failure_code", "failure_detail", "completed_at"]
            )
            Job.objects.filter(id=job.id).update(
                status=Job.Status.FAILED,
                current_stage=f"Analysis failed: {stage}",
                failure_code=exc.code,
                failure_detail=exc.detail,
                finished_at=completed_at,
                updated_at=completed_at,
            )
            job.refresh_from_db()
            return job

        completed_at = timezone.now()
        checkpoint.status = JobCheckpoint.Status.SUCCEEDED
        checkpoint.result = result
        checkpoint.completed_at = completed_at
        checkpoint.save(update_fields=["status", "result", "completed_at"])
        Job.objects.filter(id=job.id).update(
            current_stage=f"Analysis complete: {stage}",
            progress=ANALYSIS_PROGRESS[stage],
            updated_at=completed_at,
        )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.SUCCEEDED,
        current_stage="Analysis complete: moments scored and ready for review",
        progress=100,
        failure_code="",
        failure_detail="",
        finished_at=completed_at,
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


def claim_next_rerank_job() -> Job | None:
    """Claim one queued focus-prompt re-rank job using a SQLite-safe conditional update."""
    with transaction.atomic():
        candidate_ids = (
            Job.objects.filter(job_type=Job.Type.RERANK_MOMENTS, status=Job.Status.QUEUED)
            .order_by("queued_at")
            .values_list("id", flat=True)
        )
        now = timezone.now()
        for candidate_id in candidate_ids:
            changed = Job.objects.filter(id=candidate_id, status=Job.Status.QUEUED).update(
                status=Job.Status.RUNNING,
                current_stage="Starting re-rank",
                started_at=now,
                finished_at=None,
                failure_code="",
                failure_detail="",
                attempt_count=F("attempt_count") + 1,
                updated_at=now,
            )
            if changed == 1:
                return Job.objects.select_related("project").get(id=candidate_id)
    return None


def run_rerank(job: Job) -> Job:
    """Re-rank an existing analysis run's Hot Moments against the project's focus prompt.

    Per docs/pre-alpha-local/03-media-ai-pipeline.md §6, this only reads
    already-stored HotMoment data - it never re-triggers stages 1-9.
    """
    if job.job_type != Job.Type.RERANK_MOMENTS:
        raise ValueError("Only a RERANK_MOMENTS job can execute a re-rank.")
    if job.status != Job.Status.RUNNING:
        raise ValueError("Only a running RERANK_MOMENTS job can execute a re-rank.")

    project = job.project
    prompt_text = project.focus_prompt or ""
    hot_moments = list(HotMoment.objects.filter(analysis_run__project=project))
    analysis_run = hot_moments[0].analysis_run if hot_moments else None

    if not hot_moments or analysis_run is None:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Re-rank failed: no analyzed moments",
            failure_code="NO_HOT_MOMENTS",
            failure_detail="This project has no analyzed moments to re-rank yet.",
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    focus_prompt_run = FocusPromptRun.objects.create(
        project=project,
        analysis_run=analysis_run,
        prompt_text=prompt_text,
        formula_version=pipeline_rerank.RERANK_FORMULA_VERSION,
    )
    ranked = pipeline_rerank.rank_moments(prompt_text, hot_moments)
    MomentRanking.objects.bulk_create(
        [
            MomentRanking(
                focus_prompt_run=focus_prompt_run,
                hot_moment=item["hot_moment"],
                prompt_relevance=item["prompt_relevance"],
                final_score=item["final_score"],
                rank=item["rank"],
            )
            for item in ranked
        ]
    )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.SUCCEEDED,
        current_stage="Re-rank complete",
        progress=100,
        finished_at=completed_at,
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


def claim_next_timeline_job() -> Job | None:
    return _claim_next_job(Job.Type.PROPOSE_TIMELINE, starting_stage="Starting timeline proposal")


def claim_next_render_job() -> Job | None:
    return _claim_next_job(Job.Type.RENDER_ROUGH_CUT, starting_stage="Starting render")


def _transcript_boundaries_by_moment(hot_moments: list[HotMoment]) -> dict[Any, list[float]]:
    boundaries: dict[Any, list[float]] = {}
    for moment in hot_moments:
        segments = TranscriptSegment.objects.filter(
            analysis_run=moment.analysis_run,
            start_ts__lte=moment.end_ts,
            end_ts__gte=moment.start_ts,
        ).values_list("start_ts", "end_ts")
        values: list[float] = []
        for start_ts, end_ts in segments:
            values.append(start_ts)
            values.append(end_ts)
        boundaries[moment.id] = values
    return boundaries


def run_propose_timeline(job: Job) -> Job:
    """Propose a 3-6 clip timeline from selected moments, per docs/pre-alpha-local/03 §7."""
    if job.job_type != Job.Type.PROPOSE_TIMELINE:
        raise ValueError("Only a PROPOSE_TIMELINE job can propose a timeline.")
    if job.status != Job.Status.RUNNING:
        raise ValueError("Only a running PROPOSE_TIMELINE job can propose a timeline.")

    project = job.project
    selections = list(
        MomentSelection.objects.filter(
            project=project, decision=MomentSelection.Decision.SELECTED
        )
        .select_related("hot_moment__analysis_run")
        .order_by("order_index", "created_at")
    )
    if not selections:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Timeline proposal failed: no selected moments",
            failure_code="NO_SELECTED_MOMENTS",
            failure_detail="Select at least one moment before proposing a timeline.",
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    hot_moments = [selection.hot_moment for selection in selections]
    boundaries = _transcript_boundaries_by_moment(hot_moments)
    proposal = pipeline_timeline.propose_timeline(
        selections=selections,
        target_duration_seconds=project.target_duration_seconds,
        transcript_boundaries_by_moment=boundaries,
    )

    latest_version = (
        TimelineVersion.objects.filter(project=project).order_by("-version_number").first()
    )
    next_version_number = (latest_version.version_number + 1) if latest_version else 1
    timeline_version = TimelineVersion.objects.create(
        project=project,
        version_number=next_version_number,
        parent_version=latest_version,
        target_duration_seconds=project.target_duration_seconds,
        captions_enabled=False,
        clips=proposal["clips"],
    )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.SUCCEEDED,
        current_stage=f"Timeline v{timeline_version.version_number} proposed",
        progress=100,
        finished_at=completed_at,
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


RenderFn = Callable[..., dict[str, Any]]


def run_render(job: Job, *, render_fn: RenderFn | None = None) -> Job:
    """Render the project's latest timeline version into a rough cut."""
    if job.job_type != Job.Type.RENDER_ROUGH_CUT:
        raise ValueError("Only a RENDER_ROUGH_CUT job can render a rough cut.")
    if job.status != Job.Status.RUNNING:
        raise ValueError("Only a running RENDER_ROUGH_CUT job can render a rough cut.")

    render_fn = render_fn or pipeline_render.render_rough_cut
    project = job.project
    timeline_version = (
        TimelineVersion.objects.filter(project=project).order_by("-version_number").first()
    )
    if timeline_version is None or not timeline_version.clips:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Render failed: no timeline",
            failure_code="NO_TIMELINE",
            failure_detail="Propose a timeline before rendering a rough cut.",
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    source_path, _result = preflight.validate_source_path(project.source)

    captions = None
    if timeline_version.captions_enabled:
        hot_moment_ids = [clip["hot_moment_id"] for clip in timeline_version.clips]
        moments_by_id = {str(m.id): m for m in HotMoment.objects.filter(id__in=hot_moment_ids)}
        captions = []
        cursor = 0.0
        for clip in timeline_version.clips:
            moment = moments_by_id.get(clip["hot_moment_id"])
            duration = clip["end_ts"] - clip["start_ts"]
            text = (moment.summary if moment else "").strip()
            if text:
                captions.append({"start_ts": cursor, "end_ts": cursor + duration, "text": text})
            cursor += duration

    output_path = pipeline_render.output_path_for(project.id, timeline_version.version_number)
    try:
        result = render_fn(
            source_path=source_path,
            clips=timeline_version.clips,
            output_path=output_path,
            captions=captions,
        )
    except (preflight.PreflightError, PipelineStageError) as exc:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage=f"Render failed: {exc.code}",
            failure_code=exc.code,
            failure_detail=exc.detail,
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    config_fingerprint = json.dumps(
        {"clips": timeline_version.clips, "version": pipeline_render.RENDER_CONFIG_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    RenderArtifact.objects.create(
        timeline_version=timeline_version,
        relative_path=result["relative_path"],
        width=result["width"],
        height=result["height"],
        codec=result["codec"],
        duration_seconds=result["duration_seconds"],
        render_config_hash=hashlib.sha256(config_fingerprint).hexdigest(),
    )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.SUCCEEDED,
        current_stage="Render complete",
        progress=100,
        finished_at=completed_at,
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


def claim_next_voiceover_job() -> Job | None:
    return _claim_next_job(Job.Type.GENERATE_VOICEOVER, starting_stage="Starting voice-over generation")


def _voice_segment_content_hash(sentence_text: str, voice_settings: dict[str, Any]) -> str:
    fingerprint = json.dumps(
        {"text": sentence_text, "voice_settings": voice_settings},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(fingerprint).hexdigest()


def _voice_segment_path(project_id: Any, segment_id: Any) -> Path:
    return Path(settings.GENERATED_MEDIA_ROOT) / "cache" / str(project_id) / "voice" / f"{segment_id}.mp3"


ProviderFactory = Callable[[str], Any]


def run_voiceover(job: Job, *, provider_factory: ProviderFactory | None = None) -> Job:
    """Generate voice-over audio for a project's narration script.

    Only sentences whose text or voice settings changed since the last
    successful generation are re-synthesized (docs/alpha/02-user-stories-
    acceptance.md US-10 AC2's segment-level regeneration, applied here even
    though the rest of that spec's Script Studio is out of scope). This is
    the first job type that calls an external service; the cost-limit check
    and audit trail below implement the gate required by
    docs/pre-alpha-local/06-security-privacy.md §3.
    """
    if job.job_type != Job.Type.GENERATE_VOICEOVER:
        raise ValueError("Only a GENERATE_VOICEOVER job can generate a voice-over.")
    if job.status != Job.Status.RUNNING:
        raise ValueError("Only a running GENERATE_VOICEOVER job can generate a voice-over.")

    provider_factory = provider_factory or get_provider
    project = job.project
    script = getattr(project, "narration_script", None)
    sentences = list(script.sentences.all()) if script else []
    if not script or not sentences:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Voice-over failed: no narration script",
            failure_code="NO_NARRATION_SCRIPT",
            failure_detail="Write a narration script before generating voice-over audio.",
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    voice_over = VoiceOver.objects.filter(script=script).order_by("-created_at").first()
    if voice_over is None:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Voice-over failed: no provider selected",
            failure_code="NO_VOICE_OVER_CONFIG",
            failure_detail="Select a provider before generating voice-over audio.",
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    try:
        provider = provider_factory(voice_over.provider_key)
    except VoiceProviderError as exc:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage=f"Voice-over failed: {exc.code}",
            failure_code=exc.code,
            failure_detail=exc.detail,
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    pending: list[tuple[Any, str]] = []
    for sentence in sentences:
        content_hash = _voice_segment_content_hash(sentence.text, voice_over.voice_settings)
        segment, _created = VoiceSegment.objects.get_or_create(
            voice_over=voice_over, sentence=sentence
        )
        if segment.content_hash == content_hash and segment.status == VoiceSegment.Status.READY:
            continue
        pending.append((segment, content_hash))

    if not pending:
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.SUCCEEDED,
            current_stage="Voice-over already up to date; no segments changed",
            progress=100,
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    estimated_cost = sum(
        (provider.estimate_cost(segment.sentence.text, voice_over.voice_settings) for segment, _h in pending),
        Decimal("0"),
    )
    cost_limit = Decimal(str(settings.VOICE_COST_LIMIT_USD))
    if estimated_cost > cost_limit:
        services.record_audit_event(
            project,
            "voice.cost_limit_exceeded",
            provider=provider.key,
            estimated_cost=str(estimated_cost),
            cost_limit=str(cost_limit),
            segment_count=len(pending),
        )
        completed_at = timezone.now()
        Job.objects.filter(id=job.id).update(
            status=Job.Status.FAILED,
            current_stage="Voice-over failed: estimated cost exceeds the configured limit",
            failure_code="VOICE_COST_LIMIT_EXCEEDED",
            failure_detail=(
                f"Estimated cost ${estimated_cost} exceeds the ${cost_limit} limit. "
                "Raise VOICE_COST_LIMIT_USD or shorten the script."
            ),
            finished_at=completed_at,
            updated_at=completed_at,
        )
        job.refresh_from_db()
        return job

    services.record_audit_event(
        project,
        "voice.generate.started",
        provider=provider.key,
        segment_count=len(pending),
        estimated_cost=str(estimated_cost),
    )

    for segment, content_hash in pending:
        job.refresh_from_db(fields=["cancel_requested", "status"])
        if job.cancel_requested:
            return _cancel_job(job)
        if job.status != Job.Status.RUNNING:
            return job

        segment.status = VoiceSegment.Status.GENERATING
        segment.save(update_fields=["status"])
        try:
            result = provider.synthesize(segment.sentence.text, voice_over.voice_settings)
        except VoiceProviderError as exc:
            segment.status = VoiceSegment.Status.FAILED
            segment.save(update_fields=["status"])
            services.record_audit_event(
                project,
                "voice.synthesize.failed",
                provider=provider.key,
                sentence_id=str(segment.sentence_id),
                failure_code=exc.code,
            )
            completed_at = timezone.now()
            Job.objects.filter(id=job.id).update(
                status=Job.Status.FAILED,
                current_stage=f"Voice-over failed: {exc.code}",
                failure_code=exc.code,
                failure_detail=exc.detail,
                finished_at=completed_at,
                updated_at=completed_at,
            )
            job.refresh_from_db()
            return job

        output_path = _voice_segment_path(project.id, segment.id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(result.audio_bytes)

        completed_at = timezone.now()
        segment.audio_relative_path = str(output_path)
        segment.status = VoiceSegment.Status.READY
        segment.content_hash = content_hash
        segment.duration_seconds = result.duration_seconds
        segment.cost_estimate = result.cost
        segment.regenerated_at = completed_at
        segment.save(
            update_fields=[
                "audio_relative_path",
                "status",
                "content_hash",
                "duration_seconds",
                "cost_estimate",
                "regenerated_at",
            ]
        )
        services.record_audit_event(
            project,
            "voice.synthesize",
            provider=provider.key,
            sentence_id=str(segment.sentence_id),
            text_length=len(segment.sentence.text),
            cost=str(result.cost),
        )

    completed_at = timezone.now()
    Job.objects.filter(id=job.id).update(
        status=Job.Status.SUCCEEDED,
        current_stage="Voice-over complete",
        progress=100,
        finished_at=completed_at,
        updated_at=completed_at,
    )
    job.refresh_from_db()
    return job


def run_once() -> Job | None:
    job = claim_next_analysis_job()
    if job is not None:
        job = run_preflight(job)
        if job.status == Job.Status.PAUSED and job.job_type == Job.Type.ANALYZE_SOURCE:
            job = run_analysis(job)
        return job

    job = claim_next_rerank_job()
    if job is not None:
        return run_rerank(job)

    job = claim_next_timeline_job()
    if job is not None:
        return run_propose_timeline(job)

    job = claim_next_render_job()
    if job is not None:
        return run_render(job)

    job = claim_next_voiceover_job()
    if job is not None:
        return run_voiceover(job)
    return job
