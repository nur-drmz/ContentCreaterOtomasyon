#!/usr/bin/env python
"""Real HTTP regression for streamed, cancellable large source uploads."""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.progress import load_progress
from creator_console.server import ConsoleServer


root = Path(tempfile.mkdtemp(prefix="verify_large_upload_"))
checks = 0


def check(value, message):
    global checks
    assert value, message
    checks += 1


try:
    app = ConsoleApp(ConsoleConfig(storage_root=root / "store", inline_jobs=True,
                                   max_upload_bytes=64 * 1024 * 1024))
    project = app.create_project(title="stream upload", created_by="tester")
    pid = project["project_id"]
    own = app.ownership(pid)

    # Larger than the server's 8 MiB copy chunk: proves more than one streamed read.
    source = root / "türkiye uzun video.mp4"
    block = bytes(range(256)) * 4096
    with open(source, "wb") as handle:
        for _ in range(12):
            handle.write(block)
    expected_size = source.stat().st_size
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    thread = server.serve_in_thread()
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.putrequest("POST", f"/api/projects/{pid}/source")
    conn.putheader("Content-Length", str(expected_size))
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("X-Filename", "long%20video.mp4")
    conn.endheaders()
    with open(source, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            conn.send(chunk)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    check(response.status == 201, f"HTTP {response.status}: {payload}")
    record = payload["project"]["source"]
    check(record["size_bytes"] == expected_size, "stored size mismatch")
    check(record["content_sha256"] == expected_hash, "stream hash mismatch")
    check(record["display_name"] == "long_video.mp4", "encoded filename lost")
    stored = app.storage.resolve(own, record["stored_path"])
    check(stored.is_file() and stored.stat().st_size == expected_size, "stored file missing")
    check(not list(stored.parent.glob("*.part")), "partial file remained")
    progress = load_progress(app.storage, own)["upload"]
    check(progress["state"] == "completed", "upload progress not completed")
    check(progress["item_total"] is None and progress["percent"] == 100,
          "final stage progress contract")
    conn.close()

    # Oversized requests are rejected before the stream is touched.
    class NeverRead(io.BytesIO):
        reads = 0

        def read(self, *args):
            self.reads += 1
            return super().read(*args)

    oversized = NeverRead(b"not read")
    status, _ = ConsoleApi(app).handle(
        "POST", f"/api/projects/{pid}/source", headers={"x-filename": "big.mp4"},
        body_stream=oversized, content_length=app.config.max_upload_bytes + 1)
    check(status == 413 and oversized.reads == 0, "oversize body was read")

    # A disconnected upload is rejected and its .part file is cleaned.
    before = set(stored.parent.iterdir())
    status, _ = ConsoleApi(app).handle(
        "POST", f"/api/projects/{pid}/source", headers={"x-filename": "partial.mp4"},
        body_stream=io.BytesIO(b"short"), content_length=100)
    after = set(stored.parent.iterdir())
    check(status == 400, f"partial upload returned HTTP {status}")
    check(before == after, "partial upload left a file behind")

    bundle = (Path(__file__).parent / "creator_console/static/app.js").read_text("utf-8")
    check("XMLHttpRequest" in bundle and "xhr.upload.onprogress" in bundle,
          "browser upload progress missing")
    check("cancel-upload" in bundle and ".abort()" in bundle, "upload cancel missing")
    check("streamed_to_disk" not in bundle, "frontend re-derived server capability")

    # The real browser path uses XMLHttpRequest (for upload progress) and must
    # reach the same streaming endpoint without producing an empty body.
    browser_project = app.create_project(title="browser upload", created_by="tester")
    browser_pid = browser_project["project_id"]
    page_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"http://{host}:{port}/?project={browser_pid}#phase0",
                  wait_until="networkidle")
        page.set_input_files("#source-file", str(source))
        page.get_by_role("button", name="Yukle", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#screen').textContent.includes('kayitli')",
            timeout=30000)
        check(not page_errors, f"browser JS errors: {page_errors}")
        check("12.0 MB" in page.locator("#screen").inner_text(), "browser size missing")
        browser.close()
    browser_record = app.service(app.ownership(browser_pid)).load_artifact("project")["data"]
    check((browser_record.get("source") or {}).get("size_bytes") == expected_size,
          "browser XHR upload was empty or truncated")

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    app.shutdown()
    print(f"LARGE_UPLOAD_STREAMING_OK ({checks}/{checks}, {expected_size} bytes)")
finally:
    shutil.rmtree(root, ignore_errors=True)
