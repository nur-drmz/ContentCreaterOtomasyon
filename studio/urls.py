from django.urls import path

from . import views


app_name = "studio"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("moments/", views.moment_explorer, name="moment-explorer"),
    path("projects/new/", views.project_create, name="project-create"),
    path("projects/<uuid:project_id>/", views.project_detail, name="project-detail"),
    path(
        "projects/<uuid:project_id>/analyze/",
        views.queue_analysis,
        name="queue-analysis",
    ),
    path(
        "projects/<uuid:project_id>/moments/",
        views.project_moment_explorer,
        name="project-moment-explorer",
    ),
    path(
        "projects/<uuid:project_id>/moments/<uuid:moment_id>/decision/",
        views.moment_decision,
        name="moment-decision",
    ),
    path(
        "projects/<uuid:project_id>/moments/<uuid:moment_id>/lock/",
        views.moment_lock_toggle,
        name="moment-lock-toggle",
    ),
    path(
        "projects/<uuid:project_id>/rerank/",
        views.queue_rerank,
        name="queue-rerank",
    ),
    path(
        "projects/<uuid:project_id>/timeline/",
        views.project_timeline,
        name="project-timeline",
    ),
    path(
        "projects/<uuid:project_id>/timeline/propose/",
        views.queue_timeline,
        name="queue-timeline",
    ),
    path(
        "projects/<uuid:project_id>/timeline/render/",
        views.queue_render,
        name="queue-render",
    ),
    path(
        "projects/<uuid:project_id>/renders/<uuid:artifact_id>/",
        views.render_preview,
        name="render-preview",
    ),
    path(
        "projects/<uuid:project_id>/voice/",
        views.voice_studio,
        name="voice-studio",
    ),
    path(
        "projects/<uuid:project_id>/voice/generate/",
        views.queue_voiceover,
        name="queue-voiceover",
    ),
    path(
        "projects/<uuid:project_id>/voice/segments/<uuid:segment_id>/",
        views.voice_segment_preview,
        name="voice-segment-preview",
    ),
    path("health/", views.health, name="health"),
]
