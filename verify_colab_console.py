#!/usr/bin/env python
"""Offline verification for the Drive-hosted Creator Console Colab bundle.

No Google API, Gemini API, upload or publish action is performed. The test uses
a temporary extracted bundle and a loopback-only HTTP server.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tools import build_colab_console_bundle as builder  # noqa: E402

checks = 0


def check(condition, label: str) -> None:
    global checks
    checks += 1
    if not condition:
        raise AssertionError(label)
    print(f"[{label}] OK")


temp = Path(tempfile.mkdtemp(prefix="yta_colab_bundle_"))
process = None
try:
    bundle = temp / "creator_console_colab.zip"
    built = builder.build(bundle)
    verified = builder.verify(bundle)
    check(built["sha256"] == verified["sha256"], "bundle-hash-stable")
    check(built["file_count"] == verified["file_count"] >= 40,
          "bundle-manifest-complete")

    with zipfile.ZipFile(bundle, "r") as archive:
        names = archive.namelist()
        forbidden = [name for name in names if (
            name.startswith((".git/", ".venv/", ".console_data", "results/",
                             "source_videos/", "cache/"))
            or Path(name).name.startswith(".env")
            or name.endswith((".mp4", ".mkv", ".wav", ".mp3"))
        )]
        check(not forbidden, "bundle-has-no-media-projects-or-secrets")
        check("creator_console/long_form_studio.py" in names,
              "bundle-includes-long-form-studio")
        check("local_pipeline/long_form.py" in names,
              "bundle-includes-long-form-engine")
        archive.extractall(temp / "app")

    notebook_path = ROOT / "experiments" / "colab" / "07_creator_console.ipynb"
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    source = "\n".join("".join(cell.get("source") or [])
                       for cell in notebook["cells"])
    check(all(not cell.get("outputs") for cell in notebook["cells"]
              if cell["cell_type"] == "code"), "notebook-has-no-saved-output")
    check("drive.mount('/content/drive')" in source,
          "notebook-requires-explicit-drive-mount")
    check("127.0.0.1" in source and "serve_kernel_port_as_iframe" in source,
          "console-remains-loopback-behind-colab-session")
    check("ngrok" not in source.lower() and "cloudflared" not in source.lower(),
          "notebook-creates-no-public-tunnel")
    check("DRIVE_ROOT / 'projects'" in source
          and "DRIVE_ROOT / 'archive'" in source,
          "projects-and-archive-derived-from-drive-root")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    storage = temp / "storage"
    library = temp / "source_videos"
    library.mkdir()
    library_video = library / "drive_source.mp4"
    library_video.write_bytes(b"safe-library-fixture")
    command = [
        sys.executable, "-m", "creator_console", "--host", "127.0.0.1",
        "--port", str(port), "--storage-root", str(storage),
        "--archive-root", str(temp / "archive"), "--no-env-file", "--quiet",
        "--source-library-root", str(library),
    ]
    process = subprocess.Popen(
        command, cwd=temp / "app", stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    healthy = False
    for _ in range(80):
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
                healthy = response.status == 200
                if healthy:
                    break
        except Exception:
            time.sleep(0.1)
    check(healthy, "extracted-bundle-serves-console-health")

    def api(method: str, path: str, payload=None):
        body = (json.dumps(payload).encode("utf-8")
                if payload is not None else None)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body, method=method,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())

    status, workspace = api("GET", "/api/workspace")
    library_view = workspace.get("source_library") or {}
    check(status == 200 and [item["name"] for item in library_view["videos"]]
          == ["drive_source.mp4"], "source-library-is-path-safe-and-visible")
    status, created = api("POST", "/api/projects", {
        "title": "Colab Library", "operator_name": "verify",
    })
    project_id = created["project"]["project_id"]
    status, _imported = api(
        "POST", f"/api/projects/{project_id}/source-library",
        {"filename": "drive_source.mp4", "actor": "verify"})
    status2, project_view = api("GET", f"/api/projects/{project_id}")
    source = ((project_view.get("project") or {}).get("data") or {}).get("source") or {}
    check(status == 201 and status2 == 200
          and source.get("display_name") == "drive_source.mp4"
          and source.get("size_bytes") == library_video.stat().st_size,
          "library-video-imports-server-side-without-browser-upload")
    try:
        api("POST", f"/api/projects/{project_id}/source-library",
            {"filename": "../escape.mp4", "actor": "verify"})
        traversal_rejected = False
    except urllib.error.HTTPError as exc:
        traversal_rejected = exc.code == 400
    check(traversal_rejected, "source-library-path-traversal-rejected")
finally:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    shutil.rmtree(temp, ignore_errors=True)

print(f"COLAB_CONSOLE_BUNDLE_OK ({checks}/{checks}; offline, no real API calls)")
