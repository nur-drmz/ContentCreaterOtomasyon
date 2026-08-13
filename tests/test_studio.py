from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from studio.models import Job, Project, SourceAsset
from studio.services import discover_library_videos, resolve_library_video


@pytest.fixture
def source_library(tmp_path: Path) -> Path:
    (tmp_path / "sample.mp4").write_bytes(b"not-real-media")
    (tmp_path / "ignore.txt").write_text("ignore", encoding="utf-8")
    return tmp_path


@pytest.mark.django_db
@override_settings(SOURCE_LIBRARY_ROOT=Path("unused"))
def test_health(client):
    response = client.get(reverse("studio:health"))
    assert response.status_code == 200
    assert response.content == b"ok"


@pytest.mark.django_db
def test_dashboard_returns_200_and_reports_counts(client, source_library):
    with override_settings(SOURCE_LIBRARY_ROOT=source_library):
        response = client.get(reverse("studio:dashboard"))
    assert response.status_code == 200
    assert b"sample.mp4" in response.content
    assert b"Media stays on this PC" in response.content


def test_moment_explorer_design_preview(client):
    response = client.get(reverse("studio:moment-explorer"))
    assert response.status_code == 200
    assert b"Moment Explorer" in response.content
    assert b"DESIGN PREVIEW" in response.content
    assert b"A choice made before the words arrive" in response.content


def test_local_static_assets_are_served_in_debug(client):
    css_response = client.get("/static/css/app.css")
    javascript_response = client.get("/static/js/moment-explorer.js")
    assert css_response.status_code == 200
    assert javascript_response.status_code == 200


def test_moment_explorer_inspect_panel_shows_score_breakdown(client):
    response = client.get(reverse("studio:moment-explorer"))
    content = response.content.decode()
    assert 'data-action="inspect"' in content
    assert 'aria-expanded="false"' in content
    assert "Narrative importance" in content
    assert "Score breakdown" in content


def test_library_discovers_supported_video_only(source_library):
    with override_settings(SOURCE_LIBRARY_ROOT=source_library):
        videos = discover_library_videos()
    assert [video.filename for video in videos] == ["sample.mp4"]


def test_library_rejects_path_escape(source_library):
    with override_settings(SOURCE_LIBRARY_ROOT=source_library):
        with pytest.raises(ValueError, match="outside"):
            resolve_library_video(r"..\outside.mp4")


@pytest.mark.django_db
def test_project_creation_and_analysis_queue(client, source_library):
    with override_settings(SOURCE_LIBRARY_ROOT=source_library):
        response = client.post(
            reverse("studio:project-create"),
            {
                "source_relative_path": "sample.mp4",
                "title": "Test project",
                "season": "1",
                "episode": "2",
                "known_characters_text": "Daemon, Rhaenyra",
                "focus_prompt": "Explain the choice.",
                "target_duration_seconds": 45,
                "rights_accepted": "on",
            },
        )
        project = Project.objects.get()
        assert response.status_code == 302
        assert project.known_characters == ["Daemon", "Rhaenyra"]
        assert project.source.relative_path == "sample.mp4"

        response = client.post(reverse("studio:queue-analysis", args=[project.id]))
        assert response.status_code == 302
        assert Job.objects.get().status == Job.Status.QUEUED


@pytest.mark.django_db
def test_project_detail_shows_actionable_error_for_failed_job():
    project = Project.objects.create(
        title="Failed run",
        target_duration_seconds=45,
        rights_accepted_at=timezone.now(),
    )
    SourceAsset.objects.create(
        project=project,
        relative_path="sample.mp4",
        filename="sample.mp4",
        size_bytes=1024,
        container="mp4",
    )
    Job.objects.create(
        project=project,
        job_type=Job.Type.ANALYZE_SOURCE,
        status=Job.Status.FAILED,
        current_stage="Transcript",
        progress=40,
        idempotency_key="analyze:test:1",
        failure_code="TRANSCRIPT_FAILED",
        failure_detail="Whisper model output could not be validated.",
    )

    response = Client().get(reverse("studio:project-detail", args=[project.id]))
    assert response.status_code == 200
    assert b"TRANSCRIPT_FAILED" in response.content
    assert b"Whisper model output could not be validated." in response.content
