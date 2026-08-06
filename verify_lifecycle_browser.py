#!/usr/bin/env python
"""Small real-Chromium check for project lifecycle controls."""
from __future__ import annotations

import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console import fixtures
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.server import ConsoleServer


checks = 0


def script_artifact(text):
    return {
        "schema_version": 1, "status": "review_required",
        "status_reason": "fixture", "provider": "fixture", "model": "fixture",
        "provider_attempts": [], "script_text": text,
        "human_approval": {
            "approved": False, "approved_by": None, "approved_at": None,
            "notes": None, "approved_content_sha256": None,
        },
    }


def check(condition, label):
    global checks
    checks += 1
    if not condition:
        raise AssertionError(label)
    print(f"[{label}] OK")


with tempfile.TemporaryDirectory(prefix="lifecycle_browser_") as temp:
    app = ConsoleApp(ConsoleConfig(
        storage_root=Path(temp), inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(google=False),
        **fixtures.fixture_client_factories()))
    project = app.create_project(title="Browser lifecycle", created_by="test")
    pid = project["project_id"]
    ownership = app.ownership(pid)
    service = app.service(ownership)
    service.register_source_video(
        filename="source.mp4", payload=b"source", registered_by="test")
    service.write_artifact("selected_script", script_artifact("Editable script"), actor="test")
    service.write_artifact("selected_voiceover", {
        "schema_version": 1, "status": "review_required",
        "audio_path": "missing.wav",
    }, actor="test")
    service.write_artifact("render_manifest", {
        "schema_version": 1, "status": "review_required", "output": {}, "approval": {},
    }, actor="test")
    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    server.serve_in_thread()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/?project={pid}#script", wait_until="networkidle")
            check(page.locator("[data-testid=project-lifecycle]").count() == 1,
                  "lifecycle-panel-visible")
            check(page.locator("[data-testid=script-editor]").count() == 1,
                  "script-editor-visible")
            page.locator("[data-testid=script-editor] > summary").click()
            check(page.locator("[data-testid=script-editor-text]").input_value()
                  == "Editable script", "script-editor-prefilled")
            page.fill("[data-testid=script-editor-text]", "Browser revised script")
            page.click("[data-testid=script-editor-save]")
            page.wait_for_timeout(700)
            check(service.load_artifact("selected_script")["data"]["script_text"]
                  == "Browser revised script", "script-edit-click-persists")
            check(not service.path("selected_voiceover").exists()
                  and not service.path("render_manifest").exists(),
                  "script-edit-click-archives-downstream")

            page.locator("[data-testid=project-lifecycle] > summary").click()
            page.select_option("[data-testid=lifecycle-target]", "script")
            page.click("[data-testid=lifecycle-rewind]")
            page.wait_for_timeout(700)
            check(not service.path("selected_script").exists(), "rewind-click-persists")

            # Delete through the real HTTP adapter, including its JSON body.
            if not page.locator("[data-testid=project-lifecycle]").evaluate("el => el.open"):
                page.locator("[data-testid=project-lifecycle] > summary").click()
            page.fill("#lifecycle-confirmation", "Browser lifecycle")
            page.click("[data-testid=lifecycle-delete]")
            page.wait_for_timeout(800)
            check(pid not in app.project_ids(), "delete-click-removes-project")
            check("project=" not in page.url and "#projects" in page.url,
                  "delete-clears-project-url")
            check(not errors, "no-browser-js-errors")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()

print(f"LIFECYCLE_BROWSER_OK ({checks}/{checks})")
