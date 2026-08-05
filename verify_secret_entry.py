#!/usr/bin/env python
"""GitHub secret-entry verification: local file, API and real browser."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console import fixtures
from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.env_file import load_env_file, read_env_file
from creator_console.server import ConsoleServer


count = 0


def check(condition, label):
    global count
    count += 1
    if not condition:
        raise AssertionError(label)
    print(f"[{label}] OK")


def call(app, payload):
    return ConsoleApi(app).handle(
        "POST", "/api/settings/secrets",
        body=json.dumps(payload).encode("utf-8"))


with tempfile.TemporaryDirectory(prefix="secret_entry_") as temp:
    env_path = Path(temp) / ".env.local"
    env_path.write_text("GeminiKey=EXISTING_TEST_VALUE\n", encoding="utf-8")
    values = {"GeminiKey": "EXISTING_TEST_VALUE"}
    getter = lambda name: values.get(name)
    setter = lambda name, value: values.__setitem__(name, value)
    app = ConsoleApp(ConsoleConfig(
        storage_root=Path(temp) / "data", inline_jobs=True,
        secret_getter=getter, secret_setter=setter, env_file_path=env_path,
        **fixtures.fixture_client_factories()))
    server = None
    try:
        status, body = call(app, {"kind": "github", "secret_value": ""})
        check(status == 400, "empty-secret-refused")

        first = "TEST_GITHUB_" + "G" * 40
        status, body = call(app, {"kind": "github", "secret_value": first})
        blob = json.dumps(body)
        check(status == 200 and body["saved"], "secret-save-accepted")
        check(first not in blob, "secret-not-in-api-response")
        check(body["secret"]["state"] == "configured"
              and body["secret"]["alias_used"] == "GITHUB_TOKEN",
              "configured-state-only")
        parsed = read_env_file(env_path)
        check(parsed["GITHUB_TOKEN"] == first, "secret-written-locally")
        check(parsed["GeminiKey"] == "EXISTING_TEST_VALUE", "other-secrets-preserved")
        check(env_path.read_text(encoding="utf-8").count("GITHUB_TOKEN=") == 1,
              "single-key-no-duplicate")
        public = json.dumps(app.workspace_view())
        check(first not in public, "secret-not-in-workspace-view")
        github = next(item for item in app.workspace_view()["secrets"]
                      if item["kind"] == "github")
        check(github["state"] == "configured", "github-listed-in-settings")

        # A fresh process can load the saved credential without returning it.
        restarted_environment = {}
        summary = load_env_file(env_path, environ=restarted_environment)
        check(summary["loaded_count"] == 2
              and restarted_environment["GITHUB_TOKEN"] == first,
              "saved-secret-survives-restart")

        # Actual UI path over loopback HTTP.
        server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
        server.serve_in_thread()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/#settings", wait_until="networkidle")
            check(page.locator("[data-testid=github-secret-entry]").count() == 1,
                  "browser-entry-visible")
            second = "TEST_GITHUB_" + "H" * 40
            page.fill("[data-testid=github-token]", second)
            page.click("[data-testid=github-token-save]")
            page.wait_for_timeout(600)
            check(page.locator("[data-testid=github-token]").input_value() == "",
                  "browser-field-cleared")
            check(second not in page.content(), "secret-not-left-in-dom")
            check(read_env_file(env_path)["GITHUB_TOKEN"] == second,
                  "browser-save-updates-local-file")
            check(not errors, "browser-no-js-errors")
            browser.close()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        app.shutdown()

print(f"SECRET_ENTRY_OK ({count}/{count})")
