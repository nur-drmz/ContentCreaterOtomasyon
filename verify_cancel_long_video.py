#!/usr/bin/env python
"""Regression checks for job cancellation and long-video focus ranges."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.ids import Ownership  # noqa: E402
from creator_console.jobs import (  # noqa: E402
    STATUS_CANCELLED, STATUS_QUEUED, STATUS_RUNNING, JobStore,
)
from creator_console.progress import OperationCancelled, ProgressTracker, load_progress  # noqa: E402
from creator_console.storage import LocalFilesystemStorage  # noqa: E402
from local_pipeline.video_analysis import (  # noqa: E402
    _run_process_checked, resolve_analysis_range,
)

checks = 0
root = Path(tempfile.mkdtemp(prefix="verify_cancel_focus_"))


def check(value, message):
    global checks
    assert value, message
    checks += 1


try:
    # Two-hour source: ten-minute windows must land exactly at start/middle/end.
    check(resolve_analysis_range(source_duration_sec=7200, focus_region="start",
                                 clip_duration_min=10) == (0.0, 600.0), "start range")
    check(resolve_analysis_range(source_duration_sec=7200, focus_region="middle",
                                 clip_duration_min=10) == (3300.0, 600.0), "middle range")
    check(resolve_analysis_range(source_duration_sec=7200, focus_region="end",
                                 clip_duration_min=10) == (6600.0, 600.0), "end range")
    check(resolve_analysis_range(source_duration_sec=7200, focus_region="custom",
                                 clip_duration_min=10, custom_start_min=30)
          == (1800.0, 600.0), "custom range")
    check(resolve_analysis_range(source_duration_sec=300, focus_region="end",
                                 clip_duration_min=10) == (0.0, 300.0), "clamp short source")
    check(resolve_analysis_range(source_duration_sec=7200, clip_start_sec=90,
                                 clip_duration_sec=240) == (90.0, 240.0), "legacy seconds")

    storage = LocalFilesystemStorage(root / "store")
    own = Ownership("usr_local", "wsp_local", "prj_canceltest")
    store = JobStore(storage, own)

    queued, _ = store.submit("video_analysis", idempotency_key="queued")
    stopped = store.request_cancel(queued["job_id"], requested_by="tester")
    check(stopped["status"] == STATUS_CANCELLED, "queued job did not stop immediately")
    check(stopped["cancel_requested_by"] == "tester", "cancel actor missing")
    retried = store.request_retry(queued["job_id"], requested_by="tester")
    check(retried["status"] == STATUS_QUEUED, "cancelled job cannot retry")
    check(not retried["cancel_requested_at"], "retry retained cancel signal")

    running, _ = store.submit("render", idempotency_key="running")
    store.claim(running["job_id"], "worker-test")
    requested = store.request_cancel(running["job_id"], requested_by="tester")
    check(requested["status"] == STATUS_RUNNING, "running job falsely reported stopped")
    check(store.cancel_requested(running["job_id"]), "running cancel signal missing")

    tracker = ProgressTracker(storage, own, "render", job_id=running["job_id"],
                              cancel_check=lambda: store.cancel_requested(running["job_id"]))
    tracker.start("render")
    try:
        tracker.stage("ffmpeg", "encoding")
        raise AssertionError("checkpoint ignored cancellation")
    except OperationCancelled:
        checks += 1
    check(load_progress(storage, own)["render"]["state"] == "stopped",
          "progress not marked stopped")

    # A real child process is terminated, not merely hidden in the UI.
    class StopNow:
        def raise_if_cancelled(self):
            raise OperationCancelled("stop")

    started = time.monotonic()
    try:
        _run_process_checked(
            [sys.executable, "-c", "import time; time.sleep(30)"], StopNow())
        raise AssertionError("child process ignored cancellation")
    except OperationCancelled:
        checks += 1
    check(time.monotonic() - started < 5, "child process was not terminated promptly")

    # Regression: waiting for process exit before reading stdout/stderr can
    # deadlock once an FFmpeg diagnostic pipe fills. It must also tolerate
    # non-UTF-8 bytes commonly encountered around multilingual MKV metadata.
    class KeepRunning:
        def raise_if_cancelled(self):
            return None

    started = time.monotonic()
    flooded = _run_process_checked([
        sys.executable, "-c",
        "import os,sys; os.write(sys.stderr.fileno(), b'\\xff' * 1048576); "
        "print('pipe-drained')",
    ], KeepRunning())
    check(flooded.returncode == 0, "pipe-flood child failed")
    check("pipe-drained" in flooded.stdout, "stdout was not drained")
    check(len(flooded.stderr) == 1048576, "stderr was truncated or undecodable")
    check(time.monotonic() - started < 5, "full diagnostic pipe deadlocked")

    # The HTTP contract exposes one ownership-scoped cancel route.
    app = ConsoleApp(ConsoleConfig(storage_root=root / "api", inline_jobs=True))
    project = app.create_project(title="cancel api", created_by="tester")
    api_own = app.ownership(project["project_id"])
    api_job, _ = app.job_store(api_own).submit("video_analysis", idempotency_key="api")
    status, payload = ConsoleApi(app).handle(
        "POST", f"/api/projects/{project['project_id']}/jobs/{api_job['job_id']}/cancel",
        body=b"{}")
    check(status == 202, f"cancel API returned {status}")
    check(payload["job"]["status"] == STATUS_CANCELLED, "cancel API status")
    app.shutdown()

    bundle = (PROJECT_ROOT / "creator_console/static/app.js").read_text(encoding="utf-8")
    check("Incelenecek sure (dakika)" in bundle, "minute input missing")
    check("Video ortasina odaklan" in bundle and "Video sonuna odaklan" in bundle,
          "focus choices missing")
    check("/cancel`" in bundle and "cancelJobAction" in bundle, "stop button not wired")
    check("Klip baslangici (sn)" not in bundle, "old seconds-first form remains")

    print(f"CANCEL_LONG_VIDEO_OK ({checks}/{checks})")
finally:
    shutil.rmtree(root, ignore_errors=True)
