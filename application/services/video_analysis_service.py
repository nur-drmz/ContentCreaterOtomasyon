"""VideoAnalysisService: video-content analysis and `hot_moments` ownership.

WHAT THIS OWNS
--------------
The Phase 1 video-analysis use cases:

  - running the deterministic analysis engine over an attached source, either
    over an operator-selected window or over the WHOLE source
    (`analysis_scope="full_source"`), and
  - producing the `hot_moments` artifact (`results/hot_moments.json`) -- the
    single write authority for that artifact at the application layer.

WHAT IT DELIBERATELY DOES NOT OWN
---------------------------------
- **The analysis algorithms**: clip/audio extraction, faster-whisper ASR,
  PySceneDetect scene detection, librosa-style audio-energy scoring,
  candidate construction, ranking, overlap merging, full-source scanning and
  preview generation stay canonical in `local_pipeline.video_analysis`. This
  service orchestrates; it never re-implements a score or a timestamp.
- **Moment selection**: `record_moment_selection`, `moment_rank_owner`,
  `selected_moment` and the `moment_selection` artifact stay on the façade
  for a future MomentService milestone. This service only PRODUCES the ranked
  candidates that selection later reads.
- **Eligibility policy**: `require_phase("video_analysis")` is consumed
  through `ports.WorkflowGateQuery`, never through a façade callback. Mode
  policy (direct vs narrated) stays in `local_pipeline.source_strategy`.
- **Source assessment**: the Phase 0 `source_assessment` artifact is owned by
  `AnalysisService`. This service only READS it (for `source.duration_sec`);
  it never creates or mutates it.
- **Providers**: the analysis is fully local -- FFmpeg, faster-whisper,
  PySceneDetect, audio-energy math. No AI provider is invoked, so this
  service holds no secrets resolver and emits no provider audit events.
- **Job execution**: idempotency, leases, retries and cancellation requests
  remain with the job store. `job_id` is an observability label here, and
  `OperationCancelled` simply propagates.
- **Approvals**: a finished analysis records `status="review_required"`
  candidates only. Nothing in this service can grant a human approval.

BOUNDARY NOTES
--------------
- Source access goes through `ports.SourceQuery.get_source_path()` -- the
  attached, bytes-verified local path. No S3 URL or arbitrary path reaches
  the engine.
- Persistence goes through `ports.ArtifactRepository`; the repository emits
  the `artifact.written` audit event, which is why this service carries no
  audit sink of its own.
- Progress goes through `ports.ProgressFactory`. The full-source scope
  declares its own honest stage set (`FULL_SOURCE_ANALYSIS_STAGES`) so a
  film-length scan is never rendered as a single clip extraction.
"""
from __future__ import annotations

from application.artifact_catalog import RESULTS_DIRNAME, RUNTIME_DIRNAME
from application.errors import PipelineServiceError
from local_pipeline import video_analysis

#: Full-source scanning genuinely has different stages than window analysis.
#: Declaring the real ones keeps the panel honest: a window run must not
#: display a "region" stage it will never enter, and a full scan must not be
#: described as a single clip extraction.
FULL_SOURCE_ANALYSIS_STAGES = (
    {"key": "full_scan", "label": "Tum kaynak taraniyor"},
    {"key": "region_analysis", "label": "Aday bolgeler inceleniyor"},
    {"key": "scoring", "label": "Film geneli siralama"},
    {"key": "previews", "label": "Onizleme klipleri"},
)


class VideoAnalysisService:
    """Phase 1 video-analysis use cases for ONE project namespace."""

    def __init__(self, *, artifacts, namespace, ownership, source, binaries,
                 progress, workflow):
        #: `ports.ArtifactRepository` -- reads `source_assessment`, owns the
        #: `hot_moments` write.
        self._artifacts = artifacts
        #: `ports.ProjectNamespaceStore` -- resolves `runtime/` and
        #: `results/moment_previews/` inside this project only.
        self._namespace = namespace
        self._ownership = ownership
        #: `ports.SourceQuery` -- the narrow read-only source capability.
        self._source = source
        #: `application.media_binaries.MediaBinaries` -- FFmpeg resolution.
        self._binaries = binaries
        #: `ports.ProgressFactory` -- the job runtime binds cancellation here.
        self._progress = progress
        #: `ports.WorkflowGateQuery` -- phase eligibility, never the facade.
        self._workflow = workflow

    # ------------------------------------------------------------ use case
    def run_analysis(self, *, clip_start_sec=0, clip_duration_sec=240,
                     focus_region=None, clip_duration_min=None,
                     custom_start_min=None, analysis_scope="window",
                     whisper_model_size="small", scene_detector="content",
                     top_n=5, job_id=None, actor="console") -> dict:
        """Run video analysis and persist the `hot_moments` artifact.

        Exactly the historical facade semantics: gate first, then progress,
        then assessment-duration lookup, then the scope-specific engine call.
        Every failure fails the tracker and propagates -- a failed analysis
        can never leave a half-written manifest or a 'completed' record.
        """
        self._workflow.require_phase("video_analysis")
        tracker = self._progress.create(
            "video_analysis", job_id=job_id,
            stages=(FULL_SOURCE_ANALYSIS_STAGES
                    if analysis_scope == "full_source" else None))
        tracker.start("Video analizi basladi")
        try:
            assessment = self._artifacts.load_artifact(
                "source_assessment", required=True)
            source_duration = (((assessment.get("data") or {})
                                .get("source") or {}).get("duration_sec"))
            if analysis_scope == "full_source":
                return self._run_full_source(
                    tracker, source_duration_sec=source_duration,
                    whisper_model_size=whisper_model_size,
                    scene_detector=scene_detector,
                    top_n=top_n, job_id=job_id, actor=actor)
            if analysis_scope != "window":
                raise PipelineServiceError(
                    f"bilinmeyen analiz kapsami: {analysis_scope!r} "
                    "(beklenen: window, full_source)")
            clip_start_sec, clip_duration_sec = video_analysis.resolve_analysis_range(
                source_duration_sec=source_duration,
                focus_region=focus_region,
                clip_duration_min=clip_duration_min,
                custom_start_min=custom_start_min,
                clip_start_sec=clip_start_sec,
                clip_duration_sec=clip_duration_sec,
            )
            return self._run_window(
                tracker, clip_start_sec=clip_start_sec,
                clip_duration_sec=clip_duration_sec,
                focus_region=focus_region,
                clip_duration_min=clip_duration_min,
                custom_start_min=custom_start_min,
                whisper_model_size=whisper_model_size,
                scene_detector=scene_detector,
                top_n=top_n, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    # ------------------------------------------------------------- helpers
    def _run_full_source(self, tracker, *, source_duration_sec,
                         whisper_model_size, scene_detector, top_n,
                         job_id, actor) -> dict:
        """Scan the WHOLE source instead of one window.

        A batch of N Shorts cut from one 240-second window would be N
        variations of the same scene. Producing N genuinely different Shorts
        from a feature film requires candidates from the whole film, which is
        what this scope is for.
        """
        if not source_duration_sec:
            raise PipelineServiceError(
                "Tam kaynak taramasi icin video suresi bilinmiyor; once kaynak "
                "degerlendirmesini calistir.")
        source = self._source.get_source_path()
        ffmpeg_bin, _ = self._binaries.resolve()
        runtime_dir = self._namespace.resolve(
            self._ownership, RUNTIME_DIRNAME, create_parent=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = self._namespace.resolve(
            self._ownership, RESULTS_DIRNAME, "moment_previews",
            create_parent=True)
        data = video_analysis.run_full_source_analysis(
            ffmpeg_bin, source, runtime_dir,
            source_duration_sec=float(source_duration_sec),
            whisper_model_size=whisper_model_size,
            scene_detector=scene_detector,
            top_n=top_n, preview_dir=preview_dir, progress=tracker,
        )
        self._artifacts.write_artifact("hot_moments", data, job_id=job_id,
                                       actor=actor)
        tracker.finish(
            message=f"{len(data.get('hot_moments') or [])} aday moment "
                    f"({(data.get('full_source_scan') or {}).get('regions_scanned', 0)} bolge)")
        return data

    def _run_window(self, tracker, *, clip_start_sec, clip_duration_sec,
                    focus_region, clip_duration_min, custom_start_min,
                    whisper_model_size, scene_detector, top_n, job_id,
                    actor) -> dict:
        source = self._source.get_source_path()
        ffmpeg_bin, _ = self._binaries.resolve()
        runtime_dir = self._namespace.resolve(
            self._ownership, RUNTIME_DIRNAME, create_parent=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        preview_dir = self._namespace.resolve(
            self._ownership, RESULTS_DIRNAME, "moment_previews",
            create_parent=True)
        data = video_analysis.run_video_analysis(
            ffmpeg_bin, source, runtime_dir, clip_start_sec=clip_start_sec,
            clip_duration_sec=clip_duration_sec,
            whisper_model_size=whisper_model_size,
            scene_detector=scene_detector, top_n=top_n, preview_dir=preview_dir,
            progress=tracker, adaptive_candidate_windows=True,
        )
        data["analysis_scope"] = "window"
        if focus_region:
            data.setdefault("candidate_request", {}).update({
                "focus_region": focus_region,
                "clip_duration_min": float(clip_duration_min),
                "custom_start_min": (float(custom_start_min)
                                     if focus_region == "custom" else None),
                "resolved_start_sec": clip_start_sec,
                "resolved_duration_sec": clip_duration_sec,
            })
        self._artifacts.write_artifact("hot_moments", data, job_id=job_id,
                                       actor=actor)
        tracker.finish(
            message=f"{len(data.get('hot_moments') or [])} aday moment")
        return data
