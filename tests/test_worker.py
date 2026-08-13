import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from studio import preflight, worker
from studio.models import Job, JobCheckpoint, Project, SourceAsset


def create_job(source_path: Path, *, job_type=Job.Type.ANALYZE_SOURCE) -> Job:
    project = Project.objects.create(
        title="Worker test",
        rights_accepted_at=timezone.now(),
    )
    SourceAsset.objects.create(
        project=project,
        relative_path=source_path.name,
        filename=source_path.name,
        size_bytes=source_path.stat().st_size,
        container="mp4",
    )
    return Job.objects.create(
        project=project,
        job_type=job_type,
        idempotency_key=f"worker:{project.id}",
    )


def successful_handlers(**overrides):
    handlers = {stage: Mock(return_value={"stage": stage}) for stage in worker.PREFLIGHT_STAGES}
    handlers.update(overrides)
    return handlers


@pytest.mark.django_db
def test_claims_only_one_oldest_analysis_job_and_increments_attempt(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    first = create_job(source)
    second = create_job(source)

    claimed = worker.claim_next_analysis_job()
    first.refresh_from_db()
    second.refresh_from_db()

    assert claimed.id == first.id
    assert first.status == Job.Status.RUNNING
    assert first.attempt_count == 1
    assert second.status == Job.Status.QUEUED


@pytest.mark.django_db
def test_second_claim_does_not_return_already_claimed_job(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)

    assert worker.claim_next_analysis_job().id == job.id
    assert worker.claim_next_analysis_job() is None


@pytest.mark.django_db
def test_claim_ignores_non_analysis_jobs(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    create_job(source, job_type=Job.Type.RENDER_ROUGH_CUT)
    assert worker.claim_next_analysis_job() is None


@pytest.mark.django_db(transaction=True)
def test_external_preflight_runs_outside_database_transaction(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        claimed = worker.claim_next_analysis_job()
        handlers = successful_handlers(
            SOURCE_PATH=Mock(
                side_effect=lambda _source: {
                    "outside_transaction": not connection.in_atomic_block,
                }
            )
        )
        worker.run_preflight(claimed, handlers=handlers)
    checkpoint = JobCheckpoint.objects.get(job=job, stage="SOURCE_PATH")
    assert checkpoint.result["outside_transaction"] is True


@pytest.mark.django_db
def test_successful_preflight_pauses_job_and_persists_checkpoints(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        result = worker.run_preflight(
            worker.claim_next_analysis_job(),
            handlers=successful_handlers(),
        )

    assert result.status == Job.Status.PAUSED
    assert result.progress == 25
    assert "transcript stage not implemented" in result.current_stage
    assert JobCheckpoint.objects.filter(
        job=job, status=JobCheckpoint.Status.SUCCEEDED
    ).count() == 5


@pytest.mark.django_db
def test_successful_checkpoints_are_reused(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        claimed = worker.claim_next_analysis_job()
        worker.run_preflight(claimed, handlers=successful_handlers())
        Job.objects.filter(id=job.id).update(status=Job.Status.RUNNING)
        job.refresh_from_db()
        never_run = {
            stage: Mock(side_effect=AssertionError("checkpoint was not reused"))
            for stage in worker.PREFLIGHT_STAGES
        }
        result = worker.run_preflight(job, handlers=never_run)
    assert result.status == Job.Status.PAUSED
    assert JobCheckpoint.objects.filter(job=job).count() == 5


@pytest.mark.django_db
def test_failure_stops_later_stages_and_records_job_error(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    later = Mock(return_value={})
    handlers = successful_handlers(
        SOURCE_LIMITS=Mock(
            side_effect=preflight.PreflightError("SOURCE_TOO_LARGE", "Too large.")
        ),
        FFPROBE=later,
    )
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        result = worker.run_preflight(worker.claim_next_analysis_job(), handlers=handlers)

    assert result.status == Job.Status.FAILED
    assert result.failure_code == "SOURCE_TOO_LARGE"
    assert result.failure_detail == "Too large."
    later.assert_not_called()
    assert not JobCheckpoint.objects.filter(job=job, stage="FFPROBE").exists()


@pytest.mark.django_db
def test_cancellation_between_stages_preserves_completed_checkpoint(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)

    def request_cancel(_source):
        Job.objects.filter(id=job.id).update(cancel_requested=True)
        return {"valid": True}

    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        result = worker.run_preflight(
            worker.claim_next_analysis_job(),
            handlers=successful_handlers(SOURCE_PATH=request_cancel),
        )
    assert result.status == Job.Status.CANCELLED
    assert JobCheckpoint.objects.filter(
        job=job,
        stage="SOURCE_PATH",
        status=JobCheckpoint.Status.SUCCEEDED,
    ).exists()
    assert not JobCheckpoint.objects.filter(job=job, stage="SOURCE_LIMITS").exists()


def test_source_path_validation_accepts_safe_relative_file(tmp_path):
    source_path = tmp_path / "sample.mp4"
    source_path.write_bytes(b"media")
    source = SimpleNamespace(relative_path="sample.mp4")
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        resolved, result = preflight.validate_source_path(source)
    assert resolved == source_path.resolve()
    assert result["size_bytes"] == 5


@pytest.mark.parametrize("relative_path", [r"..\outside.mp4", r"C:\outside.mp4"])
def test_source_path_validation_rejects_escape_and_absolute(tmp_path, relative_path):
    source = SimpleNamespace(relative_path=relative_path)
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        with pytest.raises(preflight.PreflightError) as caught:
            preflight.validate_source_path(source)
    assert caught.value.code == "SOURCE_OUTSIDE_LIBRARY"


def test_source_path_validation_rejects_unsupported_and_missing(tmp_path):
    (tmp_path / "sample.txt").write_text("x", encoding="utf-8")
    with override_settings(SOURCE_LIBRARY_ROOT=tmp_path):
        with pytest.raises(preflight.PreflightError) as unsupported:
            preflight.validate_source_path(SimpleNamespace(relative_path="sample.txt"))
        with pytest.raises(preflight.PreflightError) as missing:
            preflight.validate_source_path(SimpleNamespace(relative_path="missing.mp4"))
    assert unsupported.value.code == "SOURCE_TYPE_UNSUPPORTED"
    assert missing.value.code == "SOURCE_NOT_FOUND"


def test_ffprobe_resolution_uses_explicit_setting(tmp_path):
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"exe")
    with override_settings(FFPROBE_PATH=executable):
        assert preflight.discover_ffprobe() == executable.resolve()


def test_ffprobe_resolution_uses_winget_package_fallback(tmp_path):
    local_app_data = tmp_path / "LocalAppData"
    executable = (
        local_app_data
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.1.2-full_build-shared"
        / "bin"
        / "ffprobe.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    with (
        override_settings(FFPROBE_PATH=None),
        patch("studio.preflight.shutil.which", return_value=None),
        patch.dict("studio.preflight.os.environ", {"LOCALAPPDATA": str(local_app_data)}),
    ):
        assert preflight.discover_ffprobe() == executable.resolve()


def probe_payload(**overrides):
    payload = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "1328.599",
            "size": "214073347",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24000/1001",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
                "channels": 2,
            },
        ],
    }
    payload.update(overrides)
    return payload


def completed_probe(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def test_ffprobe_normalizes_successful_result(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"exe")
    version = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ffprobe version 8.1.2\n", stderr=""
    )
    with patch(
        "studio.preflight.subprocess.run",
        side_effect=[completed_probe(probe_payload()), version],
    ):
        result = preflight.inspect_with_ffprobe(source, executable)
    assert result["duration_seconds"] == pytest.approx(1328.599)
    assert result["frame_rate"] == pytest.approx(23.976024)
    assert result["video_codec"] == "h264"
    assert result["audio_channels"] == 2
    assert result["ffprobe_version"] == "ffprobe version 8.1.2"


@pytest.mark.parametrize(
    ("effect", "expected_code"),
    [
        (subprocess.TimeoutExpired("ffprobe", 30), "FFPROBE_TIMEOUT"),
        (
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="invalid media"
            ),
            "FFPROBE_FAILED",
        ),
        (
            subprocess.CompletedProcess(args=[], returncode=0, stdout="{", stderr=""),
            "FFPROBE_INVALID_JSON",
        ),
    ],
)
def test_ffprobe_process_failures_are_structured(tmp_path, effect, expected_code):
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"exe")
    patch_kwargs = (
        {"side_effect": effect}
        if isinstance(effect, BaseException)
        else {"return_value": effect}
    )
    with patch("studio.preflight.subprocess.run", **patch_kwargs):
        with pytest.raises(preflight.PreflightError) as caught:
            preflight.inspect_with_ffprobe(tmp_path / "sample.mp4", executable)
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (
            {"format": {"duration": "10"}, "streams": [{"codec_type": "audio"}]},
            "VIDEO_STREAM_MISSING",
        ),
        (
            {"format": {"duration": "0"}, "streams": [{"codec_type": "video"}]},
            "SOURCE_DURATION_INVALID",
        ),
        (
            {"format": {"duration": str(181 * 60)}, "streams": [{"codec_type": "video"}]},
            "SOURCE_TOO_LONG",
        ),
    ],
)
def test_ffprobe_rejects_invalid_media_metadata(tmp_path, payload, expected_code):
    executable = tmp_path / "ffprobe.exe"
    executable.write_bytes(b"exe")
    with patch("studio.preflight.subprocess.run", return_value=completed_probe(payload)):
        with pytest.raises(preflight.PreflightError) as caught:
            preflight.inspect_with_ffprobe(tmp_path / "sample.mp4", executable)
    assert caught.value.code == expected_code


def test_source_size_limit_is_enforced():
    source = SimpleNamespace()
    too_large = 20 * 1024**3 + 1
    with patch(
        "studio.preflight.validate_source_path",
        return_value=(Path("sample.mp4"), {"size_bytes": too_large}),
    ):
        with pytest.raises(preflight.PreflightError) as caught:
            preflight.inspect_source_size(source)
    assert caught.value.code == "SOURCE_TOO_LARGE"


@pytest.mark.django_db
def test_source_media_rejects_renamed_unsupported_container(tmp_path):
    source_path = tmp_path / "renamed.mp4"
    source_path.write_bytes(b"media")
    project = Project.objects.create(
        title="Renamed source",
        rights_accepted_at=timezone.now(),
    )
    source = SourceAsset.objects.create(
        project=project,
        relative_path=source_path.name,
        filename=source_path.name,
        size_bytes=source_path.stat().st_size,
    )
    metadata = {
        "container": "avi",
        "duration_seconds": 10,
    }
    with (
        override_settings(SOURCE_LIBRARY_ROOT=tmp_path),
        patch("studio.preflight.inspect_with_ffprobe", return_value=metadata),
    ):
        with pytest.raises(preflight.PreflightError) as caught:
            preflight.inspect_source_media(source)
    assert caught.value.code == "SOURCE_TYPE_UNSUPPORTED"


def test_generated_and_model_storage_low_are_distinct(tmp_path):
    gib = 1024**3
    low = shutil_usage(total=100 * gib, used=90 * gib, free=10 * gib)
    enough = shutil_usage(total=100 * gib, used=50 * gib, free=50 * gib)
    settings_override = override_settings(
        GENERATED_MEDIA_ROOT=tmp_path,
        MODEL_ROOT=tmp_path,
        MIN_GENERATED_FREE_BYTES=20 * gib,
        MIN_MODEL_FREE_BYTES=10 * gib,
    )
    with settings_override, patch("studio.preflight.shutil.disk_usage", return_value=low):
        with pytest.raises(preflight.PreflightError) as generated:
            preflight.inspect_storage()
    with settings_override, patch(
        "studio.preflight.shutil.disk_usage", side_effect=[enough, low]
    ):
        # Exactly 10 GB passes; make the configured model requirement higher.
        with override_settings(MIN_MODEL_FREE_BYTES=11 * gib):
            with pytest.raises(preflight.PreflightError) as model:
                preflight.inspect_storage()
    assert generated.value.code == "GENERATED_STORAGE_LOW"
    assert model.value.code == "MODEL_STORAGE_LOW"


def shutil_usage(*, total, used, free):
    return SimpleNamespace(total=total, used=used, free=free)


def test_cuda_unavailable_and_low_vram_are_structured():
    import torch

    with patch.object(torch.cuda, "is_available", return_value=False):
        with pytest.raises(preflight.PreflightError) as unavailable:
            preflight.inspect_cuda()
    device = SimpleNamespace(name="Test GPU", total_memory=10 * 1024**3)
    with (
        patch.object(torch.cuda, "is_available", return_value=True),
        patch.object(torch.cuda, "get_device_properties", return_value=device),
        patch.object(torch.cuda, "mem_get_info", return_value=(7 * 1024**3, 10 * 1024**3)),
    ):
        with pytest.raises(preflight.PreflightError) as low:
            preflight.inspect_cuda()
    assert unavailable.value.code == "CUDA_UNAVAILABLE"
    assert low.value.code == "GPU_VRAM_LOW"


@pytest.mark.django_db
def test_run_worker_once_exits_cleanly_when_no_job():
    output = io.StringIO()
    call_command("run_worker", "--once", stdout=output)
    assert json.loads(output.getvalue()) == {"claimed": False, "status": "idle"}


@pytest.mark.django_db
def test_project_detail_renders_honest_successful_preflight_summary(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"media")
    job = create_job(source)
    job.status = Job.Status.PAUSED
    job.current_stage = "Preflight complete; transcript stage not implemented"
    job.progress = 25
    job.save()
    now = timezone.now()
    for stage, result in [
        (
            "FFPROBE",
            {
                "duration_seconds": 90,
                "width": 1920,
                "height": 1080,
                "video_codec": "h264",
                "audio_codec": "aac",
                "size_bytes": 1024,
            },
        ),
        (
            "CUDA",
            {
                "gpu_name": "RTX Test",
                "free_vram_bytes": 9 * 1024**3,
            },
        ),
    ]:
        JobCheckpoint.objects.create(
            job=job,
            stage=stage,
            status=JobCheckpoint.Status.SUCCEEDED,
            result=result,
            stage_config_hash=stage.lower(),
            started_at=now,
            completed_at=now,
        )

    response = Client().get(reverse("studio:project-detail", args=[job.project_id]))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Paused mid-pipeline" in content
    assert "RTX Test" in content
    assert "1920×1080" in content
    assert "Run the local worker again to resume" in content
