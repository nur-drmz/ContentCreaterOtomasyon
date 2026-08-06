#!/usr/bin/env python
"""Real MKV upload, probe and FFmpeg extraction regression."""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.fixtures import make_test_source_video
from creator_console.pipeline_service import is_supported_source_video_filename
from creator_console.server import ConsoleServer
from local_pipeline import render_utils, video_analysis


root = Path(tempfile.mkdtemp(prefix="verify_mkv_support_"))
checks = 0


def check(value, message):
    global checks
    assert value, message
    checks += 1


try:
    app = ConsoleApp(ConsoleConfig(storage_root=root / "store", inline_jobs=True))
    project = app.create_project(title="MKV source", created_by="tester")
    pid = project["project_id"]
    ownership = app.ownership(pid)
    service = app.service(ownership)
    ffmpeg_bin, ffprobe_bin = service.binaries()

    mp4 = make_test_source_video(root / "fixture.mp4", seconds=6)
    mkv = root / "Game of Thrones source.MKV"
    remux = subprocess.run([
        ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(mp4), "-map", "0", "-c", "copy", str(mkv),
    ], capture_output=True, text=True)
    check(remux.returncode == 0 and mkv.is_file(), f"MKV remux failed: {remux.stderr}")
    original_hash = hashlib.sha256(mkv.read_bytes()).hexdigest()
    check(is_supported_source_video_filename(mkv.name), "uppercase MKV was rejected")

    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    thread = server.serve_in_thread()
    host, port = server.server_address[:2]
    page_errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"http://{host}:{port}/?project={pid}#phase0", wait_until="networkidle")
        accept = page.locator("#source-file").get_attribute("accept") or ""
        check(".mkv" in accept.lower(), f"file picker does not advertise MKV: {accept}")
        check("MKV yuklenebilir" in page.locator("#screen").inner_text(),
              "MKV compatibility explanation missing")
        page.set_input_files("#source-file", str(mkv))
        page.get_by_role("button", name="Yukle", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#screen').textContent.includes('kayitli')",
            timeout=30000)
        check(not page_errors, f"browser JS errors: {page_errors}")
        browser.close()

    source = service.load_artifact("project", required=True)["data"]["source"]
    stored = app.storage.resolve(ownership, source["stored_path"])
    check(source["container_extension"] == ".mkv", "MKV container was not recorded")
    check(stored.suffix.lower() == ".mkv" and stored.is_file(), "stored MKV is missing")
    check(hashlib.sha256(stored.read_bytes()).hexdigest() == original_hash,
          "source MKV changed during upload")

    assessment = service.run_source_assessment(actor="tester")
    check(float(assessment["source"]["duration_sec"]) >= 5.5,
          "MKV duration was not probed")
    check(assessment["source"]["has_video"] is True, "MKV video stream missing")
    check(assessment["source"]["has_audio"] is True, "MKV audio stream missing")

    extracted_mp4 = root / "mkv_preview.mp4"
    extracted_wav = root / "mkv_audio.wav"
    video_analysis.extract_clip_and_audio(
        ffmpeg_bin, stored, extracted_mp4, extracted_wav, 0, 3)
    check(extracted_mp4.is_file() and extracted_mp4.stat().st_size > 0,
          "MKV did not produce an MP4 preview")
    check(extracted_wav.is_file() and extracted_wav.stat().st_size > 0,
          "MKV audio extraction failed")
    probe, _ = render_utils.probe_media(ffmpeg_bin, ffprobe_bin, extracted_mp4)
    streams = probe.get("streams") or []
    check(any(item.get("codec_type") == "video" for item in streams),
          "MP4 preview has no video stream")
    check(any(item.get("codec_type") == "audio" for item in streams),
          "MP4 preview has no audio stream")
    check(hashlib.sha256(stored.read_bytes()).hexdigest() == original_hash,
          "analysis modified the source MKV")

    class NeverRead(io.BytesIO):
        reads = 0

        def read(self, *args):
            self.reads += 1
            return super().read(*args)

    invalid = NeverRead(b"not a video")
    status, payload = ConsoleApi(app).handle(
        "POST", f"/api/projects/{pid}/source",
        headers={"x-filename": "notes.txt"}, body_stream=invalid,
        content_length=len(invalid.getvalue()))
    check(status == 415, f"unsupported extension returned HTTP {status}: {payload}")
    check(invalid.reads == 0, "unsupported upload body was read")

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    app.shutdown()
    print(f"MKV_SOURCE_SUPPORT_OK ({checks}/{checks})")
finally:
    shutil.rmtree(root, ignore_errors=True)
