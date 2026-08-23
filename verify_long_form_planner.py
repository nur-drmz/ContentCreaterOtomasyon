#!/usr/bin/env python
"""Long-form planning artifact + real browser verification (no model calls)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console import fixtures
from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.server import ConsoleServer


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


with tempfile.TemporaryDirectory(prefix="long_form_planner_") as temp:
    app = ConsoleApp(ConsoleConfig(
        storage_root=Path(temp) / "active", inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(google=False),
        **fixtures.fixture_client_factories(),
    ))
    project = app.create_project(title="Long form demo", created_by="Planner")
    pid = project["project_id"]
    ownership = app.ownership(pid)
    payload = {
        "topic": "Why dragon bonds shape the Targaryen civil war",
        "content_type": "explainer",
        "target_duration_min": 18,
        "chapter_count": 6,
        "language": "tr-TR",
        "tone": "documentary",
        "audience": "Fantastik kurgu izleyicileri",
        "angle": "Ejderha gucunden cok binici baginin siyasi etkisini anlat.",
        "narration_mode": "tts",
        "include_subtitles": True,
        "notes": "Bilinen olaylarla teori kisimlarini acikca ayir.",
        "updated_by": "Planner",
    }
    try:
        status, initial = call(app, "GET", f"/api/projects/{pid}")
        check(status == 200 and initial["long_form"]["data"] is None,
              "long-form-empty-before-save")

        status, response = call(
            app, "PATCH", f"/api/projects/{pid}/long-form-plan", payload)
        plan = response["long_form"]
        check(status == 200, "long-form-plan-accepted")
        check(plan["planning_only"] and not plan["generation_started"],
              "long-form-plan-does-not-generate")
        check(plan["estimated_word_range"] == {"minimum": 2340, "maximum": 2700},
              "long-form-word-range-derived")
        check(plan["target_duration_min"] == 18 and plan["chapter_count"] == 6,
              "long-form-structure-persisted")
        check(app.service(ownership).path("long_form_plan").is_file(),
              "long-form-artifact-written")

        status, view = call(app, "GET", f"/api/projects/{pid}")
        check(status == 200 and view["long_form"]["data"]["topic"] == payload["topic"],
              "long-form-plan-returned-in-project-view")

        invalid = dict(payload, target_duration_min=14)
        status, _ = call(app, "PATCH", f"/api/projects/{pid}/long-form-plan", invalid)
        check(status == 400, "long-form-duration-guard")
        current = app.service(ownership).load_artifact("long_form_plan")["data"]
        check(current["target_duration_min"] == 18, "invalid-save-keeps-last-plan")
        fractional = dict(payload, chapter_count=5.5)
        status, _ = call(app, "PATCH", f"/api/projects/{pid}/long-form-plan", fractional)
        check(status == 400, "long-form-integer-structure-guard")

        server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
        server.serve_in_thread()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(f"{base}/?project={pid}#longform", wait_until="networkidle")
                check(page.locator("[data-testid=longform-screen]").count() == 1,
                      "long-form-tab-renders")
                check("Yalnizca planlama" in page.inner_text("#screen"),
                      "long-form-ui-declares-planning-only")
                check(page.locator("[data-testid=longform-topic]").input_value()
                      == payload["topic"], "long-form-ui-prefills-saved-plan")

                page.fill("[data-testid=longform-topic]", "A new long-form topic")
                page.fill("[data-testid=longform-duration]", "20")
                check("2600-3000" in page.locator("#longform-word-range").inner_text(),
                      "long-form-ui-updates-word-estimate")
                page.select_option("#longform-content-type", "theory")
                page.select_option("#longform-tone", "cinematic")
                page.click("[data-testid=longform-save]")
                page.wait_for_timeout(700)
                saved = app.service(ownership).load_artifact("long_form_plan")["data"]
                check(saved["topic"] == "A new long-form topic"
                      and saved["target_duration_min"] == 20,
                      "long-form-browser-save-persists")
                check(saved["content_type"] == "theory" and saved["tone"] == "cinematic",
                      "long-form-browser-selects-persist")
                page.reload(wait_until="networkidle")
                check(page.locator("[data-testid=longform-topic]").input_value()
                      == "A new long-form topic", "long-form-browser-reload-restores-plan")
                check(not errors, "long-form-no-browser-js-errors")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        app.shutdown()

print(f"LONG_FORM_PLANNER_OK ({checks}/{checks})")
