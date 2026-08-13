from pathlib import Path
from unittest.mock import Mock

import pytest
from django.test import override_settings
from django.utils import timezone

from studio import worker
from studio.models import (
    AnalysisRun,
    FocusPromptRun,
    HotMoment,
    Job,
    JobCheckpoint,
    MomentRanking,
    MomentSelection,
    Project,
    RenderArtifact,
    Scene,
    SourceAsset,
    TimelineVersion,
    TranscriptSegment,
)
from studio.pipeline import PipelineStageError
from studio.pipeline import candidates as pipeline_candidates
from studio.pipeline import ffmpeg_utils
from studio.pipeline import render as pipeline_render
from studio.pipeline import rerank as pipeline_rerank
from studio.pipeline import scoring as pipeline_scoring
from studio.pipeline import timeline as pipeline_timeline
from studio.pipeline import transcript as pipeline_transcript
from studio.pipeline import vlm as pipeline_vlm


def create_job(source_path: Path, *, job_type=Job.Type.ANALYZE_SOURCE) -> Job:
    project = Project.objects.create(title="Pipeline test", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project,
        relative_path=source_path.name,
        filename=source_path.name,
        size_bytes=source_path.stat().st_size,
        duration_seconds=90.0,
        container="mp4",
    )
    return Job.objects.create(
        project=project, job_type=job_type, idempotency_key=f"pipeline:{project.id}"
    )


def successful_analysis_handlers(**overrides):
    handlers = {
        stage: Mock(return_value={"stage": stage}) for stage in worker.ANALYSIS_STAGES
    }
    handlers.update(overrides)
    return handlers


def run_job_to_paused(tmp_path, source):
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        job = worker.run_preflight(job, handlers=worker._stage_handlers())
    return job


@pytest.mark.django_db
def test_run_analysis_creates_analysis_run_and_succeeds(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        result = worker.run_analysis(job, handlers=successful_analysis_handlers())

    assert result.status == Job.Status.SUCCEEDED
    assert "scored and ready for review" in result.current_stage
    assert result.progress == 100
    assert JobCheckpoint.objects.filter(
        job=job, status=JobCheckpoint.Status.SUCCEEDED
    ).count() == len(worker.ANALYSIS_STAGES)
    assert AnalysisRun.objects.filter(project=job.project).count() == 1


@pytest.mark.django_db
def test_run_analysis_checkpoints_are_reused(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        worker.run_analysis(job, handlers=successful_analysis_handlers())
        Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
        job.refresh_from_db()
        never_run = {
            stage: Mock(side_effect=AssertionError("checkpoint was not reused"))
            for stage in worker.ANALYSIS_STAGES
        }
        result = worker.run_analysis(job, handlers=never_run)

    assert result.status == Job.Status.SUCCEEDED
    assert JobCheckpoint.objects.filter(job=job).count() == len(worker.ANALYSIS_STAGES)


@pytest.mark.django_db
def test_run_analysis_failure_stops_later_stages(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        handlers = successful_analysis_handlers(
            SCENES=Mock(side_effect=PipelineStageError("SCENE_DETECTION_FAILED", "boom")),
            AUDIO=Mock(side_effect=AssertionError("later stage must not run")),
        )
        result = worker.run_analysis(job, handlers=handlers)

    assert result.status == Job.Status.FAILED
    assert result.failure_code == "SCENE_DETECTION_FAILED"
    assert JobCheckpoint.objects.filter(
        job=job, stage="AUDIO"
    ).count() == 0


@pytest.mark.django_db
def test_run_analysis_cancellation_between_stages_preserves_checkpoints(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()

        def cancel_and_return(_source):
            Job.objects.filter(id=job.id).update(cancel_requested=True)
            return {"ok": True}

        handlers = successful_analysis_handlers(PROXY=Mock(side_effect=cancel_and_return))
        result = worker.run_analysis(job, handlers=handlers)

    assert result.status == Job.Status.CANCELLED
    assert JobCheckpoint.objects.filter(job=job, status=JobCheckpoint.Status.SUCCEEDED).count() == 1


@pytest.mark.django_db
def test_run_analysis_persists_transcript_and_scene_rows(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        analysis_run = worker._get_or_create_analysis_run(job, job.project.source)

        def fake_transcript(_source):
            TranscriptSegment.objects.create(
                analysis_run=analysis_run,
                start_ts=0.0,
                end_ts=2.0,
                text="hello world",
                source=TranscriptSegment.Source.WHISPER,
            )
            return {"source": "whisper", "segment_count": 1}

        def fake_scenes(_source):
            Scene.objects.create(analysis_run=analysis_run, start_ts=0.0, end_ts=5.0)
            return {"scene_count": 1}

        handlers = successful_analysis_handlers(
            TRANSCRIPT=Mock(side_effect=fake_transcript),
            SCENES=Mock(side_effect=fake_scenes),
        )
        worker.run_analysis(job, handlers=handlers)

    assert TranscriptSegment.objects.filter(analysis_run=analysis_run).count() == 1
    assert Scene.objects.filter(analysis_run=analysis_run).count() == 1


@pytest.mark.django_db
def test_run_analysis_real_vlm_and_scoring_stages_create_hot_moments(tmp_path):
    from studio import preflight

    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        job = worker.claim_next_analysis_job()
        analysis_run = worker._get_or_create_analysis_run(job, job.project.source)
        path, _result = preflight.validate_source_path(job.project.source)

        def fake_transcript(_source):
            TranscriptSegment.objects.create(
                analysis_run=analysis_run,
                start_ts=0.0,
                end_ts=4.0,
                text="hello brave world",
                source=TranscriptSegment.Source.WHISPER,
            )
            return {"source": "whisper", "segment_count": 1}

        def fake_scenes(_source):
            Scene.objects.create(analysis_run=analysis_run, start_ts=0.0, end_ts=10.0)
            Scene.objects.create(analysis_run=analysis_run, start_ts=10.0, end_ts=20.0)
            return {"scene_count": 2}

        def fake_audio(_source):
            return {"per_scene": {"0": {"onset_strength_mean": 1.0}, "1": {"onset_strength_mean": 0.3}}}

        def fake_motion(_source):
            return {"per_scene": {"0": {"motion_mean": 0.8}, "1": {"motion_mean": 0.2}}}

        real_handlers = worker._analysis_stage_handlers(job, analysis_run, job.project.source, path)
        handlers = {
            **real_handlers,
            "PROXY": Mock(return_value={"proxy_relative_path": str(tmp_path / "proxy.mp4")}),
            "TRANSCRIPT": Mock(side_effect=fake_transcript),
            "SCENES": Mock(side_effect=fake_scenes),
            "AUDIO": Mock(side_effect=fake_audio),
            "MOTION": Mock(side_effect=fake_motion),
        }
        result = worker.run_analysis(job, handlers=handlers)

    assert result.status == Job.Status.SUCCEEDED
    assert HotMoment.objects.filter(analysis_run=analysis_run).exists()
    for moment in HotMoment.objects.filter(analysis_run=analysis_run):
        assert 0 <= moment.base_score <= 100
        assert moment.core_start_ts >= moment.start_ts
        assert moment.core_end_ts <= moment.end_ts
    analysis_run.refresh_from_db()
    assert analysis_run.status == AnalysisRun.Status.SUCCEEDED


@pytest.mark.django_db
def test_run_rerank_ranks_by_prompt_relevance_without_touching_earlier_stages():
    from studio.models import HotMoment as HotMomentModel

    project = Project.objects.create(
        title="Rerank test", rights_accepted_at=timezone.now(), focus_prompt="dragon theory"
    )
    SourceAsset.objects.create(
        project=project,
        relative_path="sample.mp4",
        filename="sample.mp4",
        size_bytes=1024,
        container="mp4",
    )
    analysis_run = AnalysisRun.objects.create(
        project=project,
        source_asset=project.source,
        pipeline_version="test",
        config_hash="abc",
        status=AnalysisRun.Status.SUCCEEDED,
    )
    relevant = HotMomentModel.objects.create(
        analysis_run=analysis_run,
        start_ts=0.0,
        end_ts=10.0,
        core_start_ts=1.0,
        core_end_ts=9.0,
        base_score=70,
        spoiler_level=HotMomentModel.SpoilerLevel.LOW,
        summary="A dragon theory is revealed",
        transcript_evidence="dragon theory",
    )
    irrelevant = HotMomentModel.objects.create(
        analysis_run=analysis_run,
        start_ts=20.0,
        end_ts=30.0,
        core_start_ts=21.0,
        core_end_ts=29.0,
        base_score=75,
        spoiler_level=HotMomentModel.SpoilerLevel.LOW,
        summary="Unrelated small talk",
        transcript_evidence="unrelated small talk",
    )
    job = Job.objects.create(
        project=project,
        job_type=Job.Type.RERANK_MOMENTS,
        status=Job.Status.RUNNING,
        idempotency_key=f"rerank:{project.id}",
    )

    result = worker.run_rerank(job)

    assert result.status == Job.Status.SUCCEEDED
    focus_prompt_run = FocusPromptRun.objects.get(project=project)
    assert focus_prompt_run.prompt_text == "dragon theory"
    rankings = list(MomentRanking.objects.filter(focus_prompt_run=focus_prompt_run).order_by("rank"))
    assert rankings[0].hot_moment_id == relevant.id
    assert rankings[0].prompt_relevance > rankings[1].prompt_relevance
    assert rankings[1].hot_moment_id == irrelevant.id


@pytest.mark.django_db
def test_run_rerank_fails_cleanly_with_no_hot_moments():
    project = Project.objects.create(title="Empty project", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="sample.mp4", filename="sample.mp4", size_bytes=1024, container="mp4"
    )
    job = Job.objects.create(
        project=project,
        job_type=Job.Type.RERANK_MOMENTS,
        status=Job.Status.RUNNING,
        idempotency_key=f"rerank:{project.id}",
    )
    result = worker.run_rerank(job)
    assert result.status == Job.Status.FAILED
    assert result.failure_code == "NO_HOT_MOMENTS"


def test_score_candidates_never_invents_vlm_only_components():
    candidates = [
        {"start_ts": 0.0, "end_ts": 10.0, "audio_signal": 0.5, "motion_signal": 0.5, "transcript_excerpt": "x"}
    ]
    scored = pipeline_scoring.score_candidates(candidates, {0: None}, target_count=20)
    assert len(scored) == 1
    breakdown = {item["key"]: item["value"] for item in scored[0]["score_breakdown"]}
    assert breakdown["narrative"] == 0
    assert breakdown["character"] == 0
    assert "VLM enrichment unavailable" in scored[0]["reasons"][0]


def test_score_candidates_uses_vlm_values_when_available():
    candidates = [
        {"start_ts": 0.0, "end_ts": 10.0, "audio_signal": 0.0, "motion_signal": 0.0, "transcript_excerpt": "x"}
    ]
    vlm_result = {
        "summary": "A big reveal",
        "characters": ["Daemon"],
        "narrativeImportance": 0.9,
        "emotionalIntensity": 0.8,
        "theoryPotential": 0.7,
        "hookSuitability": 0.6,
        "independentComprehensibility": 0.5,
        "spoilerLevel": "High",
        "reasons": ["Big reveal"],
    }
    scored = pipeline_scoring.score_candidates(candidates, {0: vlm_result}, target_count=20)
    breakdown = {item["key"]: item["value"] for item in scored[0]["score_breakdown"]}
    assert breakdown["narrative"] == 90
    assert breakdown["character"] == 100
    assert scored[0]["spoiler_level"] == "High"
    assert scored[0]["characters"] == ["Daemon"]


def test_score_candidates_suppresses_heavy_overlap():
    candidates = [
        {"start_ts": 0.0, "end_ts": 10.0, "audio_signal": 1.0, "motion_signal": 1.0, "transcript_excerpt": ""},
        {"start_ts": 1.0, "end_ts": 9.0, "audio_signal": 0.1, "motion_signal": 0.1, "transcript_excerpt": ""},
    ]
    scored = pipeline_scoring.score_candidates(candidates, {}, target_count=20)
    assert len(scored) == 1
    assert scored[0]["start_ts"] == 0.0


def test_validate_vlm_payload_accepts_the_documented_schema():
    payload = {
        "summary": "A summary",
        "characters": ["Daemon", "unknown"],
        "narrativeImportance": 0.5,
        "emotionalIntensity": 0.5,
        "theoryPotential": 0.5,
        "hookSuitability": 0.5,
        "independentComprehensibility": 0.5,
        "spoilerLevel": "low",
        "reasons": ["A reason"],
    }
    validated = pipeline_vlm.validate_vlm_payload(payload)
    assert validated["spoilerLevel"] == "Low"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"summary": "x"},
        {
            "summary": "x",
            "characters": [],
            "narrativeImportance": 2.0,
            "emotionalIntensity": 0.5,
            "theoryPotential": 0.5,
            "hookSuitability": 0.5,
            "independentComprehensibility": 0.5,
            "spoilerLevel": "low",
            "reasons": [],
        },
        {
            "summary": "x",
            "characters": [],
            "narrativeImportance": 0.5,
            "emotionalIntensity": 0.5,
            "theoryPotential": 0.5,
            "hookSuitability": 0.5,
            "independentComprehensibility": 0.5,
            "spoilerLevel": "extreme",
            "reasons": [],
        },
    ],
)
def test_validate_vlm_payload_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        pipeline_vlm.validate_vlm_payload(payload)


def test_prompt_relevance_scores_matching_tokens_higher():
    from types import SimpleNamespace

    matching = SimpleNamespace(
        transcript_evidence="a dragon theory emerges", summary="", reasons=[], characters=[]
    )
    unrelated = SimpleNamespace(transcript_evidence="quiet small talk", summary="", reasons=[], characters=[])
    assert pipeline_rerank.prompt_relevance("dragon theory", matching) > pipeline_rerank.prompt_relevance(
        "dragon theory", unrelated
    )


def test_generate_pre_candidates_merges_and_scores_windows():
    scenes = [
        {"start_ts": 0.0, "end_ts": 6.0},
        {"start_ts": 6.0, "end_ts": 14.0},
        {"start_ts": 14.0, "end_ts": 20.0},
    ]
    transcript_segments = [
        {"start_ts": 1.0, "end_ts": 5.0, "text": "one two three four five"},
        {"start_ts": 15.0, "end_ts": 19.0, "text": "six"},
    ]
    audio_features = {"per_scene": {"0": {"onset_strength_mean": 1.0}, "1": {"onset_strength_mean": 0.2}}}
    motion_features = {"per_scene": {"0": {"motion_mean": 0.5}, "1": {"motion_mean": 2.0}}}

    candidates = pipeline_candidates.generate_pre_candidates(
        transcript_segments=transcript_segments,
        scenes=scenes,
        audio_features=audio_features,
        motion_features=motion_features,
        source_duration_seconds=20.0,
    )

    assert candidates
    assert all("heuristic_score" in c for c in candidates)
    assert candidates == sorted(candidates, key=lambda c: c["heuristic_score"], reverse=True)


def test_adaptive_target_count_matches_pipeline_spec():
    assert pipeline_candidates.adaptive_target_count(30 * 60) == 20
    assert pipeline_candidates.adaptive_target_count(90 * 60) == 30
    assert pipeline_candidates.adaptive_target_count(150 * 60) == 40


def test_parse_srt_extracts_segments():
    text = (
        "1\n00:00:01,000 --> 00:00:03,500\nHello there.\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\nSecond line.\n"
    )
    segments = pipeline_transcript.parse_srt(text)
    assert segments == [
        {"start_ts": 1.0, "end_ts": 3.5, "text": "Hello there."},
        {"start_ts": 4.0, "end_ts": 5.0, "text": "Second line."},
    ]


def test_parse_srt_ignores_malformed_blocks():
    assert pipeline_transcript.parse_srt("not a subtitle file") == []


def test_discover_ffmpeg_uses_configured_path(tmp_path, settings):
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"binary")
    settings.FFMPEG_PATH = str(fake_ffmpeg)
    assert ffmpeg_utils.discover_ffmpeg() == fake_ffmpeg.resolve()


def test_discover_ffmpeg_raises_when_not_found(settings, monkeypatch):
    settings.FFMPEG_PATH = None
    monkeypatch.setattr(ffmpeg_utils.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        ffmpeg_utils.preflight,
        "discover_ffprobe",
        Mock(side_effect=ffmpeg_utils.preflight.PreflightError("FFPROBE_NOT_FOUND", "missing")),
    )
    with pytest.raises(PipelineStageError) as exc_info:
        ffmpeg_utils.discover_ffmpeg()
    assert exc_info.value.code == "FFMPEG_NOT_FOUND"


def _create_selection(analysis_run, project, **overrides):
    defaults = {
        "start_ts": 0.0,
        "end_ts": 10.0,
        "core_start_ts": 1.0,
        "core_end_ts": 9.0,
        "base_score": 80,
        "spoiler_level": HotMoment.SpoilerLevel.LOW,
    }
    moment_kwargs = {
        key: overrides.pop(key)
        for key in list(overrides)
        if key in {"start_ts", "end_ts", "core_start_ts", "core_end_ts"}
    }
    defaults.update(moment_kwargs)
    moment = HotMoment.objects.create(analysis_run=analysis_run, **defaults)
    selection_defaults = {"decision": MomentSelection.Decision.SELECTED}
    selection_defaults.update(overrides)
    return MomentSelection.objects.create(project=project, hot_moment=moment, **selection_defaults)


@pytest.mark.django_db
def test_propose_timeline_uses_trim_when_set_else_core_range():
    project = Project.objects.create(title="Timeline test", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="s.mp4", filename="s.mp4", size_bytes=1, container="mp4"
    )
    analysis_run = AnalysisRun.objects.create(
        project=project, source_asset=project.source, pipeline_version="t", config_hash="h"
    )
    selection = _create_selection(analysis_run, project, order_index=0)

    proposal = pipeline_timeline.propose_timeline(
        selections=[selection],
        target_duration_seconds=45,
        transcript_boundaries_by_moment={},
    )
    clip = proposal["clips"][0]
    assert clip["start_ts"] == 1.0
    assert clip["end_ts"] == 9.0


@pytest.mark.django_db
def test_propose_timeline_snaps_unlocked_clip_to_transcript_boundary():
    project = Project.objects.create(title="Timeline test", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="s.mp4", filename="s.mp4", size_bytes=1, container="mp4"
    )
    analysis_run = AnalysisRun.objects.create(
        project=project, source_asset=project.source, pipeline_version="t", config_hash="h"
    )
    selection = _create_selection(
        analysis_run, project, order_index=0, core_start_ts=1.2, core_end_ts=8.9
    )

    proposal = pipeline_timeline.propose_timeline(
        selections=[selection],
        target_duration_seconds=45,
        transcript_boundaries_by_moment={selection.hot_moment_id: [1.0, 9.0]},
    )
    clip = proposal["clips"][0]
    assert clip["start_ts"] == 1.0
    assert clip["end_ts"] == 9.0


@pytest.mark.django_db
def test_propose_timeline_leaves_locked_clip_exact():
    project = Project.objects.create(title="Timeline test", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="s.mp4", filename="s.mp4", size_bytes=1, container="mp4"
    )
    analysis_run = AnalysisRun.objects.create(
        project=project, source_asset=project.source, pipeline_version="t", config_hash="h"
    )
    selection = _create_selection(
        analysis_run,
        project,
        order_index=0,
        core_start_ts=1.2,
        core_end_ts=8.9,
        locked=True,
        trim_in=2.0,
        trim_out=8.0,
    )

    proposal = pipeline_timeline.propose_timeline(
        selections=[selection],
        target_duration_seconds=45,
        transcript_boundaries_by_moment={selection.hot_moment_id: [1.0, 9.0]},
    )
    clip = proposal["clips"][0]
    assert clip["start_ts"] == 2.0
    assert clip["end_ts"] == 8.0
    assert clip["locked"] is True


@pytest.mark.django_db
def test_propose_timeline_caps_at_six_clips():
    project = Project.objects.create(title="Timeline test", rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="s.mp4", filename="s.mp4", size_bytes=1, container="mp4"
    )
    analysis_run = AnalysisRun.objects.create(
        project=project, source_asset=project.source, pipeline_version="t", config_hash="h"
    )
    selections = [
        _create_selection(
            analysis_run,
            project,
            order_index=i,
            start_ts=i * 20.0,
            end_ts=i * 20.0 + 15.0,
            core_start_ts=i * 20.0 + 1.0,
            core_end_ts=i * 20.0 + 14.0,
        )
        for i in range(8)
    ]

    proposal = pipeline_timeline.propose_timeline(
        selections=selections, target_duration_seconds=45, transcript_boundaries_by_moment={}
    )
    assert len(proposal["clips"]) == 6


def test_render_rough_cut_raises_without_clips(tmp_path):
    with pytest.raises(PipelineStageError) as exc_info:
        pipeline_render.render_rough_cut(
            source_path=tmp_path / "s.mp4", clips=[], output_path=tmp_path / "out.mp4"
        )
    assert exc_info.value.code == "RENDER_NO_CLIPS"


def test_render_rough_cut_calls_ffmpeg_per_clip_then_concat(tmp_path, monkeypatch):
    source_path = tmp_path / "s.mp4"
    source_path.write_bytes(b"media")
    output_path = tmp_path / "out" / "rough_cut.mp4"
    calls = []

    def fake_run_ffmpeg(args, **kwargs):
        calls.append(args)
        if "-filter_complex" in args:
            out = Path(args[-1])
            out.write_bytes(b"clip")
        else:
            Path(args[-1]).write_bytes(b"final")
        return None

    monkeypatch.setattr(pipeline_render, "run_ffmpeg", fake_run_ffmpeg)

    clips = [
        {"start_ts": 0.0, "end_ts": 5.0},
        {"start_ts": 10.0, "end_ts": 13.0},
    ]
    result = pipeline_render.render_rough_cut(
        source_path=source_path, clips=clips, output_path=output_path
    )

    assert result["width"] == pipeline_render.RENDER_WIDTH
    assert result["height"] == pipeline_render.RENDER_HEIGHT
    assert result["duration_seconds"] == 8.0
    assert len(calls) == 3
    assert "-c:v" in calls[-1] and "copy" in calls[-1]


def test_render_rough_cut_burns_captions_when_provided(tmp_path, monkeypatch):
    source_path = tmp_path / "s.mp4"
    source_path.write_bytes(b"media")
    output_path = tmp_path / "out.mp4"
    calls = []

    def fake_run_ffmpeg(args, **kwargs):
        calls.append(args)
        if "-filter_complex" in args:
            Path(args[-1]).write_bytes(b"clip")
        else:
            Path(args[-1]).write_bytes(b"final")
        return None

    monkeypatch.setattr(pipeline_render, "run_ffmpeg", fake_run_ffmpeg)

    clips = [{"start_ts": 0.0, "end_ts": 5.0}]
    captions = [{"start_ts": 0.0, "end_ts": 5.0, "text": "Hello"}]
    pipeline_render.render_rough_cut(
        source_path=source_path, clips=clips, output_path=output_path, captions=captions
    )
    final_args = calls[-1]
    assert any("subtitles=" in arg for arg in final_args)


@pytest.mark.django_db
def test_run_propose_timeline_creates_version_from_selected_moments(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source, job_type=Job.Type.PROPOSE_TIMELINE)
    analysis_run = AnalysisRun.objects.create(
        project=job.project, source_asset=job.project.source, pipeline_version="t", config_hash="h"
    )
    _create_selection(analysis_run, job.project, order_index=0)

    Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
    job.refresh_from_db()
    result = worker.run_propose_timeline(job)

    assert result.status == Job.Status.SUCCEEDED
    version = TimelineVersion.objects.get(project=job.project)
    assert version.version_number == 1
    assert len(version.clips) == 1


@pytest.mark.django_db
def test_run_propose_timeline_fails_without_selected_moments(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source, job_type=Job.Type.PROPOSE_TIMELINE)
    Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
    job.refresh_from_db()

    result = worker.run_propose_timeline(job)
    assert result.status == Job.Status.FAILED
    assert result.failure_code == "NO_SELECTED_MOMENTS"


@pytest.mark.django_db
def test_run_propose_timeline_increments_version_and_links_parent(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source, job_type=Job.Type.PROPOSE_TIMELINE)
    analysis_run = AnalysisRun.objects.create(
        project=job.project, source_asset=job.project.source, pipeline_version="t", config_hash="h"
    )
    _create_selection(analysis_run, job.project, order_index=0)
    Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
    job.refresh_from_db()
    worker.run_propose_timeline(job)

    second_job = Job.objects.create(
        project=job.project,
        job_type=Job.Type.PROPOSE_TIMELINE,
        status=Job.Status.RUNNING,
        idempotency_key=f"timeline2:{job.project.id}",
    )
    worker.run_propose_timeline(second_job)

    versions = list(TimelineVersion.objects.filter(project=job.project).order_by("version_number"))
    assert [v.version_number for v in versions] == [1, 2]
    assert versions[1].parent_version_id == versions[0].id


@pytest.mark.django_db
def test_run_render_creates_render_artifact(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source, job_type=Job.Type.RENDER_ROUGH_CUT)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        timeline_version = TimelineVersion.objects.create(
            project=job.project,
            version_number=1,
            target_duration_seconds=45,
            clips=[{"hot_moment_id": "x", "start_ts": 0.0, "end_ts": 5.0}],
        )
        Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
        job.refresh_from_db()

        fake_render = Mock(
            return_value={
                "relative_path": str(tmp_path / "rough_cut.mp4"),
                "width": 720,
                "height": 1280,
                "codec": "h264/aac",
                "duration_seconds": 5.0,
            }
        )
        result = worker.run_render(job, render_fn=fake_render)

    assert result.status == Job.Status.SUCCEEDED
    artifact = RenderArtifact.objects.get(timeline_version=timeline_version)
    assert artifact.width == 720
    assert artifact.height == 1280
    fake_render.assert_called_once()


@pytest.mark.django_db
def test_run_render_fails_without_timeline(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source, job_type=Job.Type.RENDER_ROUGH_CUT)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
        job.refresh_from_db()
        result = worker.run_render(job, render_fn=Mock())

    assert result.status == Job.Status.FAILED
    assert result.failure_code == "NO_TIMELINE"
