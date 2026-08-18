#!/usr/bin/env python
"""Focused external project archive verification (no providers, no media tools)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console import fixtures


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


with tempfile.TemporaryDirectory(prefix="project_archive_") as temp:
    base = Path(temp)
    active_root = base / "active"
    archive_root = base / "external_archive"
    archive_root.mkdir()
    app = ConsoleApp(ConsoleConfig(
        storage_root=active_root,
        archive_root=archive_root,
        inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(google=False),
        **fixtures.fixture_client_factories(),
    ))
    try:
        project = app.create_project(title="Archive demo", created_by="Tester")
        pid = project["project_id"]
        ownership = app.ownership(pid)
        service = app.service(ownership)
        registered = service.register_source_video(
            filename="source.mp4", payload=b"source-bytes", registered_by="Tester")
        source_relative = registered["source"]["stored_path"]
        service.storage.write_bytes(
            ownership, "results/render/final_short.mp4", b"render-bytes")

        workspace = app.workspace_view()
        check(workspace["archive"]["configured"], "archive-configured")
        check(workspace["archive"]["available"], "archive-root-available")
        check("aktif calisma" in workspace["storage_policy"]["working_location_name"],
              "working-storage-visible")
        check(workspace["storage_policy"]["archive_available"],
              "archive-policy-visible")

        status, _ = call(app, "POST", f"/api/projects/{pid}/archive", {
            "confirmation": "wrong", "actor": "Tester",
        })
        check(status == 400, "archive-wrong-title-refused")
        check(app.storage.namespace_root(ownership).is_dir(), "wrong-title-keeps-project")

        queued, _ = app.job_store(ownership).submit(
            "video_analysis", idempotency_key="archive-active", params={},
            submitted_by="Tester")
        status, _ = call(app, "POST", f"/api/projects/{pid}/archive", {
            "confirmation": "Archive demo", "actor": "Tester",
        })
        check(status == 409, "active-job-blocks-archive")
        app.job_store(ownership).finish(
            queued["job_id"], "failed", error="fixture", recoverable=False)

        status, archived = call(app, "POST", f"/api/projects/{pid}/archive", {
            "confirmation": "Archive demo", "actor": "Tester",
        })
        check(status == 200 and archived["archived"], "archive-accepted")
        check(not app.storage.namespace_root(ownership).exists(), "active-project-moved")
        check(pid not in app.project_ids(), "archived-project-hidden-from-active-list")

        destination = (archive_root / "CreatorConsoleArchive" / "wsp_local"
                       / archived["archive_id"])
        check(destination.is_dir(), "archive-directory-created")
        check((destination / Path(source_relative)).read_bytes()
              == b"source-bytes", "source-bytes-preserved")
        check((destination / "results" / "render" / "final_short.mp4").read_bytes()
              == b"render-bytes", "render-bytes-preserved")
        manifest = json.loads(
            (destination / "archive_manifest.json").read_text(encoding="utf-8"))
        check(manifest["project_id"] == pid and manifest["title"] == "Archive demo",
              "archive-manifest-written")

        archive_view = app.workspace_view()["archive"]
        check(len(archive_view["projects"]) == 1, "archive-listed-in-workspace")
        check(archive_view["projects"][0]["project_id"] == pid,
              "archive-list-project-id")
        check("external_archive" in archive_view["location_name"],
              "archive-location-is-path-safe-label")

        status, _ = call(app, "GET", f"/api/projects/{pid}")
        check(status == 404, "archived-project-not-openable-as-active")
    finally:
        app.shutdown()

print(f"PROJECT_ARCHIVE_OK ({checks}/{checks})")
