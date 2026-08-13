import io
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from studio import services
from studio.models import (
    AnalysisRun,
    FocusPromptRun,
    HotMoment,
    Job,
    MomentRanking,
    MomentSelection,
    Project,
    RenderArtifact,
    SourceAsset,
    TimelineVersion,
)


def create_project(title: str = "Real project") -> Project:
    project = Project.objects.create(
        title=title,
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
    return project


def create_analysis_run(project: Project) -> AnalysisRun:
    return AnalysisRun.objects.create(
        project=project,
        source_asset=project.source,
        pipeline_version="test-pipeline",
        config_hash="abc123",
        status=AnalysisRun.Status.SUCCEEDED,
    )


def create_hot_moment(analysis_run: AnalysisRun, **overrides) -> HotMoment:
    defaults = {
        "start_ts": 60.0,
        "end_ts": 75.0,
        "core_start_ts": 62.0,
        "core_end_ts": 73.0,
        "base_score": 88,
        "score_breakdown": [{"key": "narrative", "label": "Narrative", "weight": 22, "value": 80}],
        "summary": "A meaningful beat worth reviewing.",
        "characters": ["Daemon"],
        "spoiler_level": HotMoment.SpoilerLevel.LOW,
        "reasons": ["Character reveal"],
        "transcript_evidence": "The pause says more than the line.",
    }
    defaults.update(overrides)
    return HotMoment.objects.create(analysis_run=analysis_run, **defaults)


@pytest.mark.django_db
def test_analysis_run_and_hot_moment_creation():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)

    assert AnalysisRun.objects.count() == 1
    assert HotMoment.objects.get() == moment
    assert moment.analysis_run == analysis_run
    assert moment.is_demo is False


@pytest.mark.django_db
def test_hot_moment_rejects_start_after_end():
    project = create_project()
    analysis_run = create_analysis_run(project)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_hot_moment(analysis_run, start_ts=100.0, end_ts=50.0)


@pytest.mark.django_db
def test_hot_moment_rejects_core_range_outside_moment_range():
    project = create_project()
    analysis_run = create_analysis_run(project)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_hot_moment(
                analysis_run, start_ts=60.0, end_ts=75.0, core_start_ts=59.0, core_end_ts=73.0
            )


@pytest.mark.django_db
def test_hot_moment_rejects_score_above_100():
    project = create_project()
    analysis_run = create_analysis_run(project)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            create_hot_moment(analysis_run, base_score=150)


@pytest.mark.django_db
def test_moment_selection_unique_per_project_and_moment():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    MomentSelection.objects.create(project=project, hot_moment=moment)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MomentSelection.objects.create(project=project, hot_moment=moment)


@pytest.mark.django_db
def test_moment_selection_rejects_trim_in_after_trim_out():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MomentSelection.objects.create(
                project=project, hot_moment=moment, trim_in=10.0, trim_out=5.0
            )


@pytest.mark.django_db
def test_project_moment_explorer_returns_200_with_persisted_moment():
    project = create_project()
    analysis_run = create_analysis_run(project)
    create_hot_moment(analysis_run, summary="A very specific real summary.")

    response = Client().get(reverse("studio:project-moment-explorer", args=[project.id]))
    assert response.status_code == 200
    assert b"A very specific real summary." in response.content
    assert b"No moments yet" not in response.content


@pytest.mark.django_db
def test_project_moment_explorer_shows_honest_empty_state_with_no_moments():
    project = create_project()

    response = Client().get(reverse("studio:project-moment-explorer", args=[project.id]))
    assert response.status_code == 200
    assert b"No moments yet" in response.content
    # Must never fall back to demo content on the real project route.
    assert b"A choice made before the words arrive" not in response.content


@pytest.mark.django_db
def test_moment_from_another_project_cannot_be_selected():
    project_a = create_project("Project A")
    project_b = create_project("Project B")
    analysis_run_b = create_analysis_run(project_b)
    moment_b = create_hot_moment(analysis_run_b)

    response = Client().post(
        reverse("studio:moment-decision", args=[project_a.id, moment_b.id]),
        {"decision": "SELECTED"},
    )
    assert response.status_code == 404
    assert not MomentSelection.objects.filter(project=project_a, hot_moment=moment_b).exists()


@pytest.mark.django_db
def test_select_reject_restore_persist_decisions():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    url = reverse("studio:moment-decision", args=[project.id, moment.id])
    client = Client()

    response = client.post(url, {"decision": "SELECTED"})
    assert response.status_code == 302
    selection = MomentSelection.objects.get(project=project, hot_moment=moment)
    assert selection.decision == MomentSelection.Decision.SELECTED

    response = client.post(url, {"decision": "REJECTED"})
    assert response.status_code == 302
    selection.refresh_from_db()
    assert selection.decision == MomentSelection.Decision.REJECTED

    response = client.post(url, {"decision": "UNDECIDED"})
    assert response.status_code == 302
    selection.refresh_from_db()
    assert selection.decision == MomentSelection.Decision.UNDECIDED


@pytest.mark.django_db
def test_moment_decision_json_response_for_xhr():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    url = reverse("studio:moment-decision", args=[project.id, moment.id])

    response = Client().post(
        url,
        {"decision": "SELECTED"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "SELECTED"
    assert payload["selected_count"] == 1


@pytest.mark.django_db
def test_get_cannot_mutate_a_selection():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    url = reverse("studio:moment-decision", args=[project.id, moment.id])

    response = Client().get(url)
    assert response.status_code == 405
    assert not MomentSelection.objects.filter(project=project, hot_moment=moment).exists()


@pytest.mark.django_db
def test_unknown_decision_value_returns_400():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    url = reverse("studio:moment-decision", args=[project.id, moment.id])

    response = Client().post(url, {"decision": "MAYBE_LATER"})
    assert response.status_code == 400
    assert not MomentSelection.objects.filter(project=project, hot_moment=moment).exists()


@pytest.mark.django_db
def test_demo_seeding_is_idempotent_and_never_touches_source():
    project = create_project()

    out = io.StringIO()
    call_command("seed_demo_moments", str(project.id), stdout=out)
    first_count = HotMoment.objects.filter(analysis_run__project=project).count()
    assert first_count > 0
    assert all(
        moment.is_demo for moment in HotMoment.objects.filter(analysis_run__project=project)
    )

    out = io.StringIO()
    call_command("seed_demo_moments", str(project.id), stdout=out)
    second_count = HotMoment.objects.filter(analysis_run__project=project).count()
    assert second_count == first_count
    assert AnalysisRun.objects.filter(project=project, pipeline_version="demo-preview").count() == 1
    assert "already exist" in out.getvalue()


# --- AnalysisRun project/source integrity -----------------------------------


@pytest.mark.django_db
def test_create_analysis_run_accepts_matching_project_and_source():
    project = create_project()
    analysis_run = services.create_analysis_run(
        project=project,
        source_asset=project.source,
        pipeline_version="test-pipeline",
    )
    assert analysis_run.pk is not None
    assert analysis_run.project_id == project.id
    assert analysis_run.source_asset_id == project.source.id


@pytest.mark.django_db
def test_create_analysis_run_rejects_mismatched_project_and_source():
    project_a = create_project("Project A")
    project_b = create_project("Project B")

    with pytest.raises(ValidationError):
        services.create_analysis_run(
            project=project_a,
            source_asset=project_b.source,
            pipeline_version="test-pipeline",
        )

    assert not AnalysisRun.objects.filter(project=project_a).exists()
    assert not AnalysisRun.objects.filter(project=project_b).exists()


@pytest.mark.django_db
def test_analysis_run_clean_rejects_mismatched_project_and_source_directly():
    project_a = create_project("Project A")
    project_b = create_project("Project B")
    analysis_run = AnalysisRun(
        project=project_a,
        source_asset=project_b.source,
        pipeline_version="test-pipeline",
    )
    with pytest.raises(ValidationError):
        analysis_run.clean()


@pytest.mark.django_db
def test_seeder_rolls_back_partial_analysis_run_and_moments():
    project = create_project()
    original_create = HotMoment.objects.create
    call_count = {"n": 0}

    def flaky_create(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated failure while writing HotMoment rows")
        return original_create(*args, **kwargs)

    with patch.object(HotMoment.objects, "create", side_effect=flaky_create):
        with pytest.raises(RuntimeError):
            call_command("seed_demo_moments", str(project.id))

    assert AnalysisRun.objects.filter(project=project).count() == 0
    assert HotMoment.objects.filter(analysis_run__project=project).count() == 0

    # The command must still be usable (and idempotent) after the rollback.
    out = io.StringIO()
    call_command("seed_demo_moments", str(project.id), stdout=out)
    assert HotMoment.objects.filter(analysis_run__project=project).count() > 0

    out = io.StringIO()
    call_command("seed_demo_moments", str(project.id), stdout=out)
    assert AnalysisRun.objects.filter(project=project, pipeline_version="demo-preview").count() == 1
    assert "already exist" in out.getvalue()


# --- Authoritative JSON summary values ---------------------------------------


@pytest.mark.django_db
def test_json_select_response_contains_all_authoritative_summary_fields():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run, start_ts=0.0, end_ts=20.0, core_start_ts=1.0, core_end_ts=19.0)
    url = reverse("studio:moment-decision", args=[project.id, moment.id])

    response = Client().post(url, {"decision": "SELECTED"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {
        "hot_moment_id",
        "decision",
        "selected_count",
        "rejected_count",
        "selected_duration",
        "target_duration",
        "duration_percent",
    }
    assert payload["hot_moment_id"] == str(moment.id)
    assert payload["decision"] == "SELECTED"
    assert payload["selected_count"] == 1
    assert payload["rejected_count"] == 0
    assert payload["selected_duration"] == 20
    assert payload["target_duration"] == project.target_duration_seconds
    expected_percent = round(20 / project.target_duration_seconds * 100)
    assert payload["duration_percent"] == expected_percent


@pytest.mark.django_db
def test_json_reject_response_updates_both_counters():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment_one = create_hot_moment(
        analysis_run, start_ts=0.0, end_ts=20.0, core_start_ts=1.0, core_end_ts=19.0
    )
    moment_two = create_hot_moment(
        analysis_run, start_ts=30.0, end_ts=40.0, core_start_ts=31.0, core_end_ts=39.0
    )
    client = Client()

    client.post(
        reverse("studio:moment-decision", args=[project.id, moment_one.id]),
        {"decision": "SELECTED"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    response = client.post(
        reverse("studio:moment-decision", args=[project.id, moment_two.id]),
        {"decision": "REJECTED"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    payload = response.json()
    assert payload["decision"] == "REJECTED"
    assert payload["selected_count"] == 1
    assert payload["rejected_count"] == 1
    assert payload["selected_duration"] == 20


@pytest.mark.django_db
def test_json_restore_response_returns_zero_updated_totals():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(
        analysis_run, start_ts=0.0, end_ts=20.0, core_start_ts=1.0, core_end_ts=19.0
    )
    url = reverse("studio:moment-decision", args=[project.id, moment.id])
    client = Client()

    client.post(url, {"decision": "SELECTED"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
    response = client.post(url, {"decision": "UNDECIDED"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    payload = response.json()
    assert payload["decision"] == "UNDECIDED"
    assert payload["selected_count"] == 0
    assert payload["rejected_count"] == 0
    assert payload["selected_duration"] == 0
    assert payload["duration_percent"] == 0


@pytest.mark.django_db
def test_selected_duration_calculated_from_persisted_selected_moments():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment_a = create_hot_moment(
        analysis_run, start_ts=0.0, end_ts=15.0, core_start_ts=1.0, core_end_ts=14.0
    )
    moment_b = create_hot_moment(
        analysis_run, start_ts=100.0, end_ts=122.0, core_start_ts=101.0, core_end_ts=121.0
    )
    # An unselected moment must not contribute to the duration total.
    create_hot_moment(
        analysis_run, start_ts=200.0, end_ts=260.0, core_start_ts=201.0, core_end_ts=259.0
    )
    client = Client()

    client.post(
        reverse("studio:moment-decision", args=[project.id, moment_a.id]),
        {"decision": "SELECTED"},
    )
    client.post(
        reverse("studio:moment-decision", args=[project.id, moment_b.id]),
        {"decision": "SELECTED"},
    )

    summary = services.build_moment_selection_summary(project)
    assert summary["selected_duration"] == 15 + 22
    assert summary["selected_count"] == 2


@pytest.mark.django_db
def test_duration_percent_is_clamped_at_100():
    project = create_project()  # target_duration_seconds defaults to 45
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run, start_ts=0.0, end_ts=500.0, core_start_ts=1.0, core_end_ts=499.0)
    Client().post(
        reverse("studio:moment-decision", args=[project.id, moment.id]),
        {"decision": "SELECTED"},
    )

    summary = services.build_moment_selection_summary(project)
    assert summary["selected_duration"] == 500
    assert summary["duration_percent"] == 100


# --- Template rendering: stable ids and the honest Restore fallback --------


@pytest.mark.django_db
def test_project_moment_explorer_renders_stable_summary_element_ids():
    project = create_project()
    analysis_run = create_analysis_run(project)
    create_hot_moment(analysis_run)

    response = Client().get(reverse("studio:project-moment-explorer", args=[project.id]))
    content = response.content.decode()
    assert 'id="pm-selected-count"' in content
    assert 'id="pm-rejected-count"' in content
    assert 'id="pm-selected-duration"' in content
    assert 'id="pm-target-duration"' in content
    assert 'id="pm-duration-fill"' in content
    assert 'data-role="selection-summary"' in content


@pytest.mark.django_db
def test_restore_form_exists_for_undecided_card_but_begins_hidden():
    project = create_project()
    analysis_run = create_analysis_run(project)
    create_hot_moment(analysis_run)

    response = Client().get(reverse("studio:project-moment-explorer", args=[project.id]))
    content = response.content.decode()
    assert 'class="decision-form restore-form" hidden' in content


@pytest.mark.django_db
def test_restore_form_is_visible_once_selected():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    client = Client()
    client.post(
        reverse("studio:moment-decision", args=[project.id, moment.id]),
        {"decision": "SELECTED"},
    )

    response = client.get(reverse("studio:project-moment-explorer", args=[project.id]))
    content = response.content.decode()
    restore_form_tag = content.split('class="decision-form restore-form"')[1].split(">")[0]
    assert "hidden" not in restore_form_tag


@pytest.mark.django_db
def test_queue_rerank_updates_prompt_and_creates_job():
    project = create_project()
    analysis_run = create_analysis_run(project)
    create_hot_moment(analysis_run)

    response = Client().post(
        reverse("studio:queue-rerank", args=[project.id]),
        {"focus_prompt": "why does the dragon matter"},
    )

    assert response.status_code == 302
    project.refresh_from_db()
    assert project.focus_prompt == "why does the dragon matter"
    job = Job.objects.get(project=project, job_type=Job.Type.RERANK_MOMENTS)
    assert job.status == Job.Status.QUEUED


@pytest.mark.django_db
def test_queue_rerank_without_moments_does_not_create_job():
    project = create_project()
    response = Client().post(
        reverse("studio:queue-rerank", args=[project.id]),
        {"focus_prompt": "anything"},
    )
    assert response.status_code == 302
    assert Job.objects.filter(project=project, job_type=Job.Type.RERANK_MOMENTS).count() == 0


@pytest.mark.django_db
def test_queue_rerank_does_not_duplicate_active_job():
    project = create_project()
    analysis_run = create_analysis_run(project)
    create_hot_moment(analysis_run)
    client = Client()
    client.post(reverse("studio:queue-rerank", args=[project.id]), {"focus_prompt": "a"})
    client.post(reverse("studio:queue-rerank", args=[project.id]), {"focus_prompt": "b"})

    assert Job.objects.filter(project=project, job_type=Job.Type.RERANK_MOMENTS).count() == 1


@pytest.mark.django_db
def test_moment_explorer_orders_by_latest_ranking_when_present():
    project = create_project()
    analysis_run = create_analysis_run(project)
    low_base = create_hot_moment(analysis_run, base_score=40)
    high_base = create_hot_moment(analysis_run, base_score=90)

    focus_prompt_run = FocusPromptRun.objects.create(
        project=project,
        analysis_run=analysis_run,
        prompt_text="test",
        formula_version="focus-prompt-v1",
    )
    MomentRanking.objects.create(
        focus_prompt_run=focus_prompt_run, hot_moment=low_base, prompt_relevance=1.0,
        final_score=99.0, rank=1,
    )
    MomentRanking.objects.create(
        focus_prompt_run=focus_prompt_run, hot_moment=high_base, prompt_relevance=0.0,
        final_score=67.5, rank=2,
    )

    response = Client().get(reverse("studio:project-moment-explorer", args=[project.id]))
    moments = response.context["moments"]
    assert moments[0]["id"] == low_base.id
    assert moments[1]["id"] == high_base.id


@pytest.mark.django_db
def test_moment_lock_toggle_flips_locked_state():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    selection = MomentSelection.objects.create(
        project=project, hot_moment=moment, decision=MomentSelection.Decision.SELECTED
    )
    assert selection.locked is False

    client = Client()
    response = client.post(reverse("studio:moment-lock-toggle", args=[project.id, moment.id]))
    assert response.status_code == 302
    selection.refresh_from_db()
    assert selection.locked is True

    client.post(reverse("studio:moment-lock-toggle", args=[project.id, moment.id]))
    selection.refresh_from_db()
    assert selection.locked is False


@pytest.mark.django_db
def test_moment_lock_toggle_404s_for_unselected_moment():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    response = Client().post(reverse("studio:moment-lock-toggle", args=[project.id, moment.id]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_queue_timeline_requires_selected_moments():
    project = create_project()
    response = Client().post(reverse("studio:queue-timeline", args=[project.id]))
    assert response.status_code == 302
    assert Job.objects.filter(project=project, job_type=Job.Type.PROPOSE_TIMELINE).count() == 0


@pytest.mark.django_db
def test_queue_timeline_creates_job_when_moments_selected():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    MomentSelection.objects.create(
        project=project, hot_moment=moment, decision=MomentSelection.Decision.SELECTED
    )
    response = Client().post(reverse("studio:queue-timeline", args=[project.id]))
    assert response.status_code == 302
    job = Job.objects.get(project=project, job_type=Job.Type.PROPOSE_TIMELINE)
    assert job.status == Job.Status.QUEUED


@pytest.mark.django_db
def test_queue_render_requires_timeline():
    project = create_project()
    response = Client().post(reverse("studio:queue-render", args=[project.id]))
    assert response.status_code == 302
    assert Job.objects.filter(project=project, job_type=Job.Type.RENDER_ROUGH_CUT).count() == 0


@pytest.mark.django_db
def test_queue_render_creates_job_when_timeline_exists():
    project = create_project()
    TimelineVersion.objects.create(project=project, version_number=1, target_duration_seconds=45, clips=[])
    response = Client().post(reverse("studio:queue-render", args=[project.id]))
    assert response.status_code == 302
    job = Job.objects.get(project=project, job_type=Job.Type.RENDER_ROUGH_CUT)
    assert job.status == Job.Status.QUEUED


@pytest.mark.django_db
def test_project_timeline_view_renders_clips_and_render_preview_link():
    project = create_project()
    analysis_run = create_analysis_run(project)
    moment = create_hot_moment(analysis_run)
    timeline_version = TimelineVersion.objects.create(
        project=project,
        version_number=1,
        target_duration_seconds=45,
        clips=[{"hot_moment_id": str(moment.id), "start_ts": 0.0, "end_ts": 10.0, "locked": False}],
    )
    artifact = RenderArtifact.objects.create(
        timeline_version=timeline_version,
        relative_path="C:/fake/out.mp4",
        width=720,
        height=1280,
        codec="h264/aac",
        duration_seconds=10.0,
        render_config_hash="abc",
    )

    response = Client().get(reverse("studio:project-timeline", args=[project.id]))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Version 1" in content
    assert reverse("studio:render-preview", args=[project.id, artifact.id]) in content


@pytest.mark.django_db
def test_render_preview_404s_when_file_missing():
    project = create_project()
    timeline_version = TimelineVersion.objects.create(
        project=project, version_number=1, target_duration_seconds=45, clips=[]
    )
    artifact = RenderArtifact.objects.create(
        timeline_version=timeline_version,
        relative_path="C:/does/not/exist.mp4",
        width=720,
        height=1280,
        codec="h264/aac",
        duration_seconds=10.0,
        render_config_hash="abc",
    )
    response = Client().get(reverse("studio:render-preview", args=[project.id, artifact.id]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_render_preview_404s_for_artifact_from_another_project():
    project = create_project()
    other_project = create_project(title="Other project")
    timeline_version = TimelineVersion.objects.create(
        project=other_project, version_number=1, target_duration_seconds=45, clips=[]
    )
    artifact = RenderArtifact.objects.create(
        timeline_version=timeline_version,
        relative_path="C:/does/not/exist.mp4",
        width=720,
        height=1280,
        codec="h264/aac",
        duration_seconds=10.0,
        render_config_hash="abc",
    )
    response = Client().get(reverse("studio:render-preview", args=[project.id, artifact.id]))
    assert response.status_code == 404
