from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .demo_data import DEMO_MOMENTS
from .forms import FocusPromptForm, NarrationScriptForm, ProjectCreateForm, VoiceGenerationForm
from .models import (
    FocusPromptRun,
    HotMoment,
    Job,
    MomentSelection,
    NarrationScript,
    NarrationSentence,
    Project,
    RenderArtifact,
    TimelineVersion,
    VoiceOver,
    VoiceSegment,
)
from .services import (
    build_moment_selection_summary,
    discover_library_videos,
    format_bytes,
    format_timestamp,
    hot_moment_duration_seconds,
    record_audit_event,
    split_into_sentences,
)
from .voice.providers import PROVIDERS, default_provider_key


def wants_json(request: HttpRequest) -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def dashboard(request: HttpRequest) -> HttpResponse:
    projects = Project.objects.select_related("source").prefetch_related("jobs")[:6]
    videos = discover_library_videos()
    context = {
        "projects": projects,
        "videos": videos,
        "project_count": Project.objects.count(),
        "source_count": len(videos),
        "queued_count": Job.objects.filter(status=Job.Status.QUEUED).count(),
    }
    return render(request, "studio/dashboard.html", context)


def project_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            project = form.save()
            messages.success(request, "Project created. Your source video was registered in place.")
            return redirect("studio:project-detail", project_id=project.id)
    else:
        form = ProjectCreateForm()
    return render(request, "studio/project_form.html", {"form": form})


def project_detail(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(
        Project.objects.select_related("source").prefetch_related("jobs"),
        id=project_id,
    )
    latest_job = project.jobs.first()
    preflight_summary = None
    if latest_job:
        checkpoints = {
            checkpoint.stage: checkpoint.result
            for checkpoint in latest_job.checkpoints.filter(status="SUCCEEDED")
        }
        probe = checkpoints.get("FFPROBE")
        cuda = checkpoints.get("CUDA")
        if probe and cuda:
            preflight_summary = {
                "duration": format_timestamp(probe["duration_seconds"]),
                "resolution": f"{probe['width']}×{probe['height']}",
                "video_codec": probe["video_codec"].upper(),
                "audio_codec": probe["audio_codec"].upper() or "No audio",
                "file_size": format_bytes(probe["size_bytes"]),
                "gpu_name": cuda["gpu_name"],
                "free_vram": format_bytes(cuda["free_vram_bytes"]),
            }
    context = {
        "project": project,
        "source_size": format_bytes(project.source.size_bytes),
        "latest_job": latest_job,
        "preflight_summary": preflight_summary,
    }
    return render(request, "studio/project_detail.html", context)


def moment_explorer(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "studio/moment_explorer.html",
        {
            "moments": DEMO_MOMENTS,
            "source_name": (
                "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4"
            ),
        },
    )


def project_moment_explorer(request: HttpRequest, project_id: str) -> HttpResponse:
    """Real, per-project Moment Explorer.

    Unlike ``moment_explorer`` (the always-on `/moments/` design preview),
    this view only ever renders persisted ``HotMoment`` rows that belong to
    this project. If none exist yet it shows an honest empty state rather
    than falling back to demo content.
    """
    project = get_object_or_404(Project.objects.select_related("source"), id=project_id)
    hot_moments = list(
        HotMoment.objects.filter(analysis_run__project=project)
        .select_related("analysis_run")
        .order_by("-base_score", "start_ts")
    )
    selections_by_moment = {
        selection.hot_moment_id: selection
        for selection in MomentSelection.objects.filter(project=project)
    }

    latest_ranking_run = (
        FocusPromptRun.objects.filter(project=project).order_by("-created_at").first()
    )
    ranking_by_moment: dict = {}
    if latest_ranking_run:
        ranking_by_moment = {
            ranking.hot_moment_id: ranking for ranking in latest_ranking_run.rankings.all()
        }
        hot_moments.sort(
            key=lambda moment: ranking_by_moment[moment.id].rank
            if moment.id in ranking_by_moment
            else len(hot_moments) + 1
        )

    moments = []
    for rank, moment in enumerate(hot_moments, start=1):
        selection = selections_by_moment.get(moment.id)
        decision = selection.decision if selection else MomentSelection.Decision.UNDECIDED
        ranking = ranking_by_moment.get(moment.id)
        moments.append(
            {
                "id": moment.id,
                "rank": rank,
                "start": format_timestamp(moment.start_ts),
                "end": format_timestamp(moment.end_ts),
                "duration": hot_moment_duration_seconds(moment),
                "score": moment.base_score,
                "score_components": moment.score_breakdown,
                "characters": moment.characters,
                "spoiler": moment.spoiler_level,
                "reasons": moment.reasons,
                "summary": moment.summary,
                "transcript": moment.transcript_evidence,
                "is_demo": moment.is_demo,
                "decision": decision,
                "locked": selection.locked if selection else False,
                "prompt_relevance": ranking.prompt_relevance if ranking else None,
            }
        )

    summary = build_moment_selection_summary(project)
    active_rerank_job = project.jobs.filter(
        job_type=Job.Type.RERANK_MOMENTS,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).exists()

    context = {
        "project": project,
        "moments": moments,
        "has_moments": bool(moments),
        "focus_prompt_form": FocusPromptForm(instance=project),
        "latest_ranking_run": latest_ranking_run,
        "active_rerank_job": active_rerank_job,
        **summary,
    }
    return render(request, "studio/project_moment_explorer.html", context)


@require_POST
@transaction.atomic
def moment_decision(request: HttpRequest, project_id: str, moment_id: str) -> HttpResponse:
    """Persist a select/reject/restore decision for one project moment.

    The moment lookup is filtered by ``analysis_run__project`` rather than
    fetched independently, so a moment id from another project resolves to
    a plain 404 instead of leaking whether it exists elsewhere.
    """
    project = get_object_or_404(Project, id=project_id)
    hot_moment = get_object_or_404(
        HotMoment.objects.filter(analysis_run__project=project), id=moment_id
    )

    decision = request.POST.get("decision", "")
    if decision not in MomentSelection.Decision.values:
        return HttpResponse("Unknown decision value.", status=400)

    selection, _created = MomentSelection.objects.get_or_create(
        project=project,
        hot_moment=hot_moment,
    )
    if (
        decision == MomentSelection.Decision.SELECTED
        and selection.decision != MomentSelection.Decision.SELECTED
    ):
        selection.order_index = MomentSelection.objects.filter(
            project=project, decision=MomentSelection.Decision.SELECTED
        ).count()
    selection.decision = decision
    selection.full_clean()
    selection.save()

    if wants_json(request):
        summary = build_moment_selection_summary(project)
        return JsonResponse(
            {
                "hot_moment_id": str(hot_moment.id),
                "decision": selection.decision,
                **summary,
            }
        )
    return redirect("studio:project-moment-explorer", project_id=project.id)


@require_POST
@transaction.atomic
def moment_lock_toggle(request: HttpRequest, project_id: str, moment_id: str) -> HttpResponse:
    """Toggle whether a selected moment's trim points are protected from timeline regeneration."""
    project = get_object_or_404(Project, id=project_id)
    hot_moment = get_object_or_404(
        HotMoment.objects.filter(analysis_run__project=project), id=moment_id
    )
    selection = get_object_or_404(MomentSelection, project=project, hot_moment=hot_moment)
    selection.locked = not selection.locked
    selection.full_clean()
    selection.save(update_fields=["locked", "updated_at"])

    if wants_json(request):
        return JsonResponse({"hot_moment_id": str(hot_moment.id), "locked": selection.locked})
    return redirect("studio:project-moment-explorer", project_id=project.id)


@require_POST
@transaction.atomic
def queue_analysis(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project, id=project_id)
    active_job = project.jobs.filter(
        job_type=Job.Type.ANALYZE_SOURCE,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING, Job.Status.PAUSED],
    ).first()
    if active_job:
        messages.info(request, "Analysis is already queued, running, or paused after preflight.")
    else:
        attempt = project.jobs.filter(job_type=Job.Type.ANALYZE_SOURCE).count() + 1
        Job.objects.create(
            project=project,
            job_type=Job.Type.ANALYZE_SOURCE,
            current_stage="Waiting for local worker",
            idempotency_key=f"analyze:{project.id}:{attempt}",
        )
        messages.success(request, "Analysis queued. No model has been started yet.")
    return redirect("studio:project-detail", project_id=project.id)


@require_POST
@transaction.atomic
def queue_rerank(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project, id=project_id)
    form = FocusPromptForm(request.POST, instance=project)
    if form.is_valid():
        form.save()
    else:
        messages.error(request, "Could not update the focus prompt.")
        return redirect("studio:project-moment-explorer", project_id=project.id)

    if not HotMoment.objects.filter(analysis_run__project=project).exists():
        messages.info(request, "Analyze the source before re-ranking moments.")
        return redirect("studio:project-moment-explorer", project_id=project.id)

    active_job = project.jobs.filter(
        job_type=Job.Type.RERANK_MOMENTS,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()
    if active_job:
        messages.info(request, "A re-rank is already queued or running.")
    else:
        attempt = project.jobs.filter(job_type=Job.Type.RERANK_MOMENTS).count() + 1
        Job.objects.create(
            project=project,
            job_type=Job.Type.RERANK_MOMENTS,
            current_stage="Waiting for local worker",
            idempotency_key=f"rerank:{project.id}:{attempt}",
        )
        messages.success(request, "Re-rank queued. This does not repeat analysis.")
    return redirect("studio:project-moment-explorer", project_id=project.id)


def project_timeline(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project.objects.select_related("source"), id=project_id)
    timeline_version = (
        TimelineVersion.objects.filter(project=project)
        .order_by("-version_number")
        .prefetch_related("render_artifacts")
        .first()
    )

    clips = []
    latest_render = None
    if timeline_version:
        moments_by_id = {
            str(moment.id): moment
            for moment in HotMoment.objects.filter(
                id__in=[clip["hot_moment_id"] for clip in timeline_version.clips]
            )
        }
        for clip in timeline_version.clips:
            moment = moments_by_id.get(clip["hot_moment_id"])
            clips.append(
                {
                    "start": format_timestamp(clip["start_ts"]),
                    "end": format_timestamp(clip["end_ts"]),
                    "duration": round(clip["end_ts"] - clip["start_ts"]),
                    "locked": clip["locked"],
                    "summary": moment.summary if moment else "",
                }
            )
        latest_render = timeline_version.render_artifacts.order_by("-created_at").first()

    active_timeline_job = project.jobs.filter(
        job_type=Job.Type.PROPOSE_TIMELINE,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).exists()
    active_render_job = project.jobs.filter(
        job_type=Job.Type.RENDER_ROUGH_CUT,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).exists()
    has_selected_moments = MomentSelection.objects.filter(
        project=project, decision=MomentSelection.Decision.SELECTED
    ).exists()

    context = {
        "project": project,
        "timeline_version": timeline_version,
        "clips": clips,
        "latest_render": latest_render,
        "active_timeline_job": active_timeline_job,
        "active_render_job": active_render_job,
        "has_selected_moments": has_selected_moments,
    }
    return render(request, "studio/project_timeline.html", context)


@require_POST
@transaction.atomic
def queue_timeline(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project, id=project_id)
    if not MomentSelection.objects.filter(
        project=project, decision=MomentSelection.Decision.SELECTED
    ).exists():
        messages.info(request, "Select at least one moment before proposing a timeline.")
        return redirect("studio:project-timeline", project_id=project.id)

    active_job = project.jobs.filter(
        job_type=Job.Type.PROPOSE_TIMELINE,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()
    if active_job:
        messages.info(request, "A timeline proposal is already queued or running.")
    else:
        attempt = project.jobs.filter(job_type=Job.Type.PROPOSE_TIMELINE).count() + 1
        Job.objects.create(
            project=project,
            job_type=Job.Type.PROPOSE_TIMELINE,
            current_stage="Waiting for local worker",
            idempotency_key=f"timeline:{project.id}:{attempt}",
        )
        messages.success(request, "Timeline proposal queued.")
    return redirect("studio:project-timeline", project_id=project.id)


@require_POST
@transaction.atomic
def queue_render(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project, id=project_id)
    if not TimelineVersion.objects.filter(project=project).exists():
        messages.info(request, "Propose a timeline before rendering a rough cut.")
        return redirect("studio:project-timeline", project_id=project.id)

    active_job = project.jobs.filter(
        job_type=Job.Type.RENDER_ROUGH_CUT,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()
    if active_job:
        messages.info(request, "A render is already queued or running.")
    else:
        attempt = project.jobs.filter(job_type=Job.Type.RENDER_ROUGH_CUT).count() + 1
        Job.objects.create(
            project=project,
            job_type=Job.Type.RENDER_ROUGH_CUT,
            current_stage="Waiting for local worker",
            idempotency_key=f"render:{project.id}:{attempt}",
        )
        messages.success(request, "Render queued.")
    return redirect("studio:project-timeline", project_id=project.id)


def render_preview(request: HttpRequest, project_id: str, artifact_id: str) -> HttpResponse:
    """Stream a rendered rough cut from disk, scoped to its owning project."""
    project = get_object_or_404(Project, id=project_id)
    artifact = get_object_or_404(
        RenderArtifact.objects.filter(timeline_version__project=project), id=artifact_id
    )
    path = Path(artifact.relative_path)
    if not path.is_file():
        raise Http404("The rendered file is no longer available on disk.")
    return FileResponse(open(path, "rb"), content_type="video/mp4")


def _sync_narration_sentences(script: NarrationScript, sentence_texts: list[str]) -> None:
    """Upsert sentences by position so unchanged sentences keep their identity.

    Keeping the same ``NarrationSentence`` row for an unchanged sentence is
    what makes segment-level regeneration possible: ``VoiceSegment`` rows are
    keyed to the sentence, and the worker skips regeneration when the
    sentence text (and voice settings) hash hasn't changed.
    """
    existing_by_index = {sentence.order_index: sentence for sentence in script.sentences.all()}
    for index, text in enumerate(sentence_texts):
        sentence = existing_by_index.pop(index, None)
        if sentence is None:
            NarrationSentence.objects.create(script=script, order_index=index, text=text)
        elif sentence.text != text:
            sentence.text = text
            sentence.save(update_fields=["text"])
    for leftover in existing_by_index.values():
        leftover.delete()


def voice_studio(request: HttpRequest, project_id: str) -> HttpResponse:
    project = get_object_or_404(Project, id=project_id)
    script, _created = NarrationScript.objects.get_or_create(project=project)
    sentences = list(script.sentences.all())
    voice_over = VoiceOver.objects.filter(script=script).order_by("-created_at").first()
    segments_by_sentence = {}
    if voice_over:
        segments_by_sentence = {
            segment.sentence_id: segment for segment in voice_over.segments.select_related("sentence")
        }

    sentence_rows = [
        {
            "text": sentence.text,
            "segment": segments_by_sentence.get(sentence.id),
        }
        for sentence in sentences
    ]

    active_job = project.jobs.filter(
        job_type=Job.Type.GENERATE_VOICEOVER,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()

    context = {
        "project": project,
        "script_form": NarrationScriptForm(instance=script),
        "generation_form": VoiceGenerationForm(
            initial={"provider_key": (voice_over.provider_key if voice_over else default_provider_key())}
        ),
        "sentence_rows": sentence_rows,
        "voice_over": voice_over,
        "active_job": active_job,
        "cost_limit": settings.VOICE_COST_LIMIT_USD,
    }
    return render(request, "studio/voice_studio.html", context)


@require_POST
@transaction.atomic
def queue_voiceover(request: HttpRequest, project_id: str) -> HttpResponse:
    """Save the narration script, record explicit approval, and queue generation.

    Per docs/pre-alpha-local/06-security-privacy.md §3, a job is only queued
    when the user has explicitly approved this specific request (server-side
    enforced via the required ``approved`` field, not just a UI checkbox) -
    there is no way to reach the provider without this happening first.
    """
    project = get_object_or_404(Project, id=project_id)
    script, _created = NarrationScript.objects.get_or_create(project=project)

    script_form = NarrationScriptForm(request.POST, instance=script)
    generation_form = VoiceGenerationForm(request.POST)
    if not script_form.is_valid() or not generation_form.is_valid():
        messages.error(request, "Could not queue voice-over generation. Check the form below.")
        return redirect("studio:voice-studio", project_id=project.id)

    script = script_form.save()
    sentence_texts = split_into_sentences(script.text)
    _sync_narration_sentences(script, sentence_texts)
    if not sentence_texts:
        messages.info(request, "Write a narration script before generating voice-over audio.")
        return redirect("studio:voice-studio", project_id=project.id)

    provider_key = generation_form.cleaned_data["provider_key"]
    voice_over = VoiceOver.objects.create(script=script, provider_key=provider_key, voice_settings={})
    record_audit_event(
        project,
        "voice.generate.requested",
        provider=provider_key,
        sentence_count=len(sentence_texts),
        approved=True,
    )

    active_job = project.jobs.filter(
        job_type=Job.Type.GENERATE_VOICEOVER,
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING],
    ).first()
    if active_job:
        messages.info(request, "A voice-over generation job is already queued or running.")
    else:
        attempt = project.jobs.filter(job_type=Job.Type.GENERATE_VOICEOVER).count() + 1
        Job.objects.create(
            project=project,
            job_type=Job.Type.GENERATE_VOICEOVER,
            current_stage="Waiting for local worker",
            idempotency_key=f"voiceover:{project.id}:{attempt}",
        )
        messages.success(
            request,
            f"Voice-over queued via {PROVIDERS[provider_key].display_name}. "
            "Only changed sentences will be regenerated.",
        )
    return redirect("studio:voice-studio", project_id=voice_over.script.project_id)


def voice_segment_preview(request: HttpRequest, project_id: str, segment_id: str) -> HttpResponse:
    """Stream one generated voice segment's audio, scoped to its owning project."""
    project = get_object_or_404(Project, id=project_id)
    segment = get_object_or_404(
        VoiceSegment.objects.filter(voice_over__script__project=project), id=segment_id
    )
    path = Path(segment.audio_relative_path) if segment.audio_relative_path else None
    if not path or not path.is_file():
        raise Http404("This segment has not been generated yet.")
    return FileResponse(open(path, "rb"), content_type="audio/mpeg")


def health(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain")
