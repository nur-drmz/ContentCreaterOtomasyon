#!/usr/bin/env python
"""Focused project rewind/reset/edit/delete verification (no provider calls)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console import fixtures, render_review


checks = 0


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(label)
    print(f"[{label}] OK")


def call(app, method, path, payload=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return ConsoleApi(app).handle(method, path, body=body)


def script_artifact(text="Original script"):
    return {
        "schema_version": 1,
        "status": "review_required",
        "status_reason": "fixture",
        "provider": "fixture",
        "model": "fixture",
        "provider_attempts": [],
        "script_text": text,
        "human_approval": {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None,
        },
    }


with tempfile.TemporaryDirectory(prefix="project_lifecycle_") as temp:
    app = ConsoleApp(ConsoleConfig(
        storage_root=Path(temp), inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(google=False),
        **fixtures.fixture_client_factories()))
    try:
        project = app.create_project(title="Lifecycle demo", created_by="test")
        pid = project["project_id"]
        ownership = app.ownership(pid)
        service = app.service(ownership)

        # Source + a complete-looking chain of files; content need not be media
        # because lifecycle operations move bytes, never execute them.
        service.register_source_video(
            filename="source.mp4", payload=b"fixture-source", registered_by="test")
        service.write_artifact("selected_script", script_artifact(), actor="test")
        service.write_artifact("selected_voiceover", {
            "schema_version": 1, "status": "review_required",
            "audio_path": "missing.wav",
        }, actor="test")
        service.write_artifact("render_manifest", {
            "schema_version": 1, "status": "review_required",
            "output": {}, "approval": {},
        }, actor="test")
        service.storage.write_bytes(ownership, "results/tts/voice.wav", b"wav")
        service.storage.write_bytes(ownership, "results/render/final_short.mp4", b"mp4")
        service.storage.write_bytes(
            ownership, "results/render/subtitles.srt",
            ("1\n00:00:00,000 --> 00:00:02,000\nOriginal caption\n\n"
             "2\n00:00:02,000 --> 00:00:04,000\nSecond caption\n").encode("utf-8"))

        # Narrated subtitle edits preserve timings, persist outside the render
        # directory and archive the now-stale video for a deliberate re-render.
        subtitle_view = service.subtitle_view()
        check(subtitle_view["available"] and len(subtitle_view["cues"]) == 2,
              "subtitle-track-loaded-from-render")
        first_cue = subtitle_view["cues"][0]
        status, payload = call(app, "PATCH", f"/api/projects/{pid}/subtitles", {
            "cues": [{"id": first_cue["id"], "text": "Corrected caption"}],
            "updated_by": "Editor", "notes": "Corrected a name",
        })
        check(status == 200, "subtitle-edit-accepted")
        track = service.load_artifact("subtitle_track")["data"]
        check(track["cues"][0]["text"] == "Corrected caption",
              "subtitle-text-persisted")
        check(not service.path("render_manifest").exists()
              and not service.path("render_manifest").parent.joinpath("final_short.mp4").exists(),
              "subtitle-edit-archives-render")
        check(service.path("subtitle_track").exists(),
              "subtitle-track-survives-render-archive")

        # Recreate descendants so the following script edit still proves it
        # archives a live render chain rather than merely observing absence.
        service.write_artifact("render_manifest", {
            "schema_version": 1, "status": "review_required", "output": {}, "approval": {},
        }, actor="test")
        service.storage.write_bytes(ownership, "results/render/final_short.mp4", b"mp4")

        # Human script edit keeps the script but archives every descendant.
        status, payload = call(app, "PATCH", f"/api/projects/{pid}/script", {
            "script_text": "Revised script with a better ending.",
            "updated_by": "Editor", "notes": "Render review found a story issue",
        })
        check(status == 200, "script-edit-accepted")
        edited = service.load_artifact("selected_script")["data"]
        check(edited["script_text"].startswith("Revised"), "script-text-persisted")
        check(edited["status"] == "review_required"
              and not edited["human_approval"]["approved"], "script-approval-reset")
        check(not service.path("selected_voiceover").exists(), "voice-removed-from-active-chain")
        check(not service.path("render_manifest").exists(), "render-removed-from-active-chain")
        check(not service.path("subtitle_track").exists(),
              "script-edit-archives-stale-subtitle-track")
        revisions = service.revisions()
        check(revisions and revisions[0]["action"] == "script_edit",
              "script-descendants-archived")

        # Rewind script archives the current script itself and exposes the phase.
        status, _ = call(app, "POST", f"/api/projects/{pid}/lifecycle", {
            "action": "rewind", "target": "script", "actor": "Editor",
        })
        check(status == 200, "rewind-script-accepted")
        check(not service.path("selected_script").exists(), "rewind-script-clears-active-script")
        check(service.revisions()[0]["target"] == "script", "rewind-manifest-recorded")

        # Reset is title-confirmed and recoverable. Source-preserving reset keeps
        # the exact source registration but clears results/jobs/progress.
        service.storage.write_json(ownership, "results/test.json", {"x": 1})
        status, _ = call(app, "POST", f"/api/projects/{pid}/lifecycle", {
            "action": "reset", "keep_source": True,
            "confirmation": "wrong", "actor": "Editor",
        })
        check(status == 400, "reset-wrong-title-refused")
        status, _ = call(app, "POST", f"/api/projects/{pid}/lifecycle", {
            "action": "reset", "keep_source": True,
            "confirmation": "Lifecycle demo", "actor": "Editor",
        })
        check(status == 200, "reset-keep-source-accepted")
        current = service.load_artifact("project")["data"]
        check(bool(current.get("source")), "reset-keeps-source")
        check(not service.storage.resolve(ownership, "results").exists(), "reset-clears-results")

        # Active work blocks every lifecycle mutation.
        queued, _ = app.job_store(ownership).submit(
            "video_analysis", idempotency_key="active-test", params={}, submitted_by="test")
        status, _ = call(app, "POST", f"/api/projects/{pid}/lifecycle", {
            "action": "rewind", "target": "analysis", "actor": "Editor",
        })
        check(status == 409, "active-job-blocks-rewind")
        app.job_store(ownership).finish(
            queued["job_id"], "failed", error="fixture", recoverable=False)

        # Full reset archives the source too and returns to the upload step.
        status, _ = call(app, "POST", f"/api/projects/{pid}/lifecycle", {
            "action": "reset", "keep_source": False,
            "confirmation": "Lifecycle demo", "actor": "Editor",
        })
        check(status == 200, "full-reset-accepted")
        check(service.load_artifact("project")["data"].get("source") is None,
              "full-reset-clears-source-registration")
        check(not service.storage.resolve(ownership, "source_videos").exists(),
              "full-reset-archives-source-bytes")

        # Render review may now point directly to the real owner of a script or
        # voice problem discovered while watching the final video.
        review = render_review.new_review(ownership, "a" * 64)
        requested = render_review.request_revision(
            review, video_sha256="a" * 64, requested_by="Reviewer",
            notes="Script ending is wrong", target_tab="script")
        check(requested["revision_requests"][0]["target_tab"] == "script",
              "render-review-can-target-script")

        # Delete is also title-confirmed and moves the exact namespace to trash.
        status, _ = call(app, "DELETE", f"/api/projects/{pid}", {
            "confirmation": "wrong", "actor": "Editor",
        })
        check(status == 400, "delete-wrong-title-refused")
        status, deleted = call(app, "DELETE", f"/api/projects/{pid}", {
            "confirmation": "Lifecycle demo", "actor": "Editor",
        })
        check(status == 200 and deleted["deleted"], "delete-accepted")
        check(not app.storage.namespace_root(ownership).exists(), "deleted-project-hidden")
        trash = app.storage.workspace_root(ownership) / ".trash" / deleted["recovery_token"]
        check(trash.is_dir(), "deleted-project-recoverable-in-trash")
        check(pid not in app.project_ids(), "deleted-project-removed-from-list")
    finally:
        app.shutdown()

print(f"PROJECT_LIFECYCLE_OK ({checks}/{checks})")
