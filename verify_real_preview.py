#!/usr/bin/env python
"""Isolated end-to-end Direct Clip Series preview against the REAL source video.

    python verify_real_preview.py

WHAT THIS DOES
--------------
Drives the Creator Console HTTP path -- source assessment -> approved test plan ->
real video analysis -> test moment selection -> real render -- using the user's
actual HOTD source video, and then validates the produced MP4 with ffprobe.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
  * It never touches the user's own project: storage lives in a fresh
    `.console_data_realtest/` root and the source video is REFERENCED read-only
    (copied into the isolated namespace, original untouched).
  * It never grants final human approval. The render stays `review_required`;
    that is the whole point -- a machine may produce, only a human may approve.
  * It makes no provider call: only local ffmpeg/faster-whisper/PySceneDetect run,
    and the Google client factory is a fixture double that would raise if used.
  * It uploads and publishes nothing.

The output path is printed so a human can watch it. Browser playback is verified
separately by `verify_browser.py --real-preview`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import render_utils  # noqa: E402

#: Candidate locations for the real source, in preference order.
SOURCE_CANDIDATES = [
    Path.home() / "Downloads" / "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4",
    Path.home() / "Downloads" / "Daemon Targaryen may never have truly wanted the Iron Throne..mp4",
]

STORAGE_ROOT = PROJECT_ROOT / ".console_data_realtest"
CLIP_START_SEC = 60
CLIP_DURATION_SEC = 90
TOP_N = 3

FAILURES: list = []


def check(condition, label: str, message: str = "") -> bool:
    if condition:
        print(f"  [{label}] OK" + (f" — {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ — {message}")
    return False


def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


def main() -> int:
    source = next((p for p in SOURCE_CANDIDATES if p.is_file()), None)
    if source is None:
        print("REAL_SOURCE_MISSING: gercek kaynak video bulunamadi. Aranan:")
        for candidate in SOURCE_CANDIDATES:
            print(f"  - {candidate}")
        print("Bu test kullanicinin gercek videosunu gerektirir; atlaniyor.")
        return 2

    print("=" * 76)
    print("GERCEK KAYNAK ile IZOLE Direct Clip Series onizlemesi")
    print("=" * 76)
    print(f"  Kaynak (salt-okunur referans): {source.name}")
    print(f"  Boyut: {source.stat().st_size / 1024 ** 2:.1f} MB")
    print(f"  Izole depolama: {STORAGE_ROOT}")
    print(f"  Klip: {CLIP_START_SEC}s baslangic, {CLIP_DURATION_SEC}s sure, top-{TOP_N}")
    print()

    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT, ignore_errors=True)

    app = ConsoleApp(ConsoleConfig(
        storage_root=STORAGE_ROOT,
        user_id="usr_realtest", workspace_id="wsp_realtest",
        inline_jobs=True,
        # No real credential is read, and the client factory is a double that
        # would record a call if any provider were contacted.
        secret_getter=lambda name: None,
        **fixtures.fixture_client_factories()))
    api = ConsoleApi(app)
    fixtures.reset_provider_calls()

    # --- 1. project + source ------------------------------------------------
    status, payload = call(api, "POST", "/api/projects",
                           {"title": "GERCEK KAYNAK TESTI (izole)",
                            "operator_name": "verify_real_preview"})
    check(status == 201, "project", f"HTTP {status}")
    pid = payload["project"]["project_id"]
    ownership = app.ownership(pid)
    service = app.service(ownership)

    status, payload = api.handle(
        "POST", f"/api/projects/{pid}/source", body=source.read_bytes(),
        headers={"X-Filename": source.name, "X-Actor": "verify_real_preview"})
    check(status == 201, "source-upload", f"HTTP {status}")
    stored = payload["project"]["source"]
    check(stored["size_bytes"] == source.stat().st_size, "source-intact",
          f"{stored['size_bytes']} bayt kopyalandi")
    check(source.is_file() and source.stat().st_size == stored["size_bytes"],
          "source-untouched", "orijinal dosya degistirilmedi")

    # --- 2. source assessment (real ffprobe) --------------------------------
    t0 = time.time()
    status, payload = call(api, "POST", f"/api/projects/{pid}/jobs",
                           {"job_type": "source_assessment",
                            "run_request_id": "real-assess"})
    job = app.job_store(ownership).get(payload["job"]["job_id"])
    assessment = service.load_artifact("source_assessment")["data"]
    check(assessment is not None, "assessment", f"is durumu={job['status']}")
    if assessment:
        src = assessment["source"]
        check(src["probe_ok"] and src["duration_sec"] > 0, "assessment-probe",
              f"{src['duration_sec']:.0f}s, {src['width']}x{src['height']}, "
              f"ses={src['has_audio']}, backend={src['probe_backend']}")
        check(assessment["selected_mode"] is None and assessment["auto_selected"] is False,
              "assessment-advisory",
              f"oneri={assessment['recommended_mode']}, otomatik secim YOK")
    print(f"     degerlendirme suresi: {time.time() - t0:.1f}s")

    # --- 3. TEST plan, approved as a test operator --------------------------
    render_config = {"render_mode": "blurred_background", "preview_mode": True,
                     "caption_style": "active_word_yellow", "caption_theme": "auto"}
    status, payload = call(api, "POST", f"/api/projects/{pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "verify_real_preview",
        "output_count": 1, "target_duration_sec": 60,
        "render_config": render_config,
        "notes": "IZOLE TEST PLANI - kullanicinin gercek projesi degildir"})
    check(status == 201 and payload["plan"]["status"] == "review_required",
          "plan-created", f"HTTP {status}, status={payload['plan']['status']}")
    digest = service.describe_plan_review()["review"]["content_sha256"]
    status, payload = call(api, "POST", f"/api/projects/{pid}/approvals/processing_plan",
                           {"reviewed_sha256": digest,
                            "approved_by": "verify_real_preview"})
    check(status == 200 and payload["artifact"]["status"] == "approved",
          "plan-approved", "test plani hash ile onaylandi")

    # --- 4. real video analysis --------------------------------------------
    t0 = time.time()
    status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"clip_start_sec": CLIP_START_SEC,
                   "clip_duration_sec": CLIP_DURATION_SEC,
                   "whisper_model_size": "small", "scene_detector": "content",
                   "top_n": TOP_N},
        "run_request_id": "real-analysis"})
    job = app.job_store(ownership).get(payload["job"]["job_id"])
    hot = service.load_artifact("hot_moments")["data"]
    analysis_sec = time.time() - t0
    check(hot is not None and hot.get("hot_moments"),
          "analysis", f"is durumu={job['status']} hata={job.get('error')}")
    if not hot or not hot.get("hot_moments"):
        return _finish(app)
    moments = hot["hot_moments"]
    print(f"     analiz suresi: {analysis_sec:.1f}s, {len(moments)} aday")
    for moment in moments:
        print(f"       rank {moment['rank']}: {moment['source_start_sec']:.1f}s"
              f"->{moment['source_end_sec']:.1f}s skor={moment['score']:.4f}")
    check(hot["status"] == "review_required", "analysis-review-required",
          f"artefakt durumu={hot['status']}")

    # Previews must reach the browser as namespace-relative media routes.
    view = app.project_view(ownership)
    previews = [m.get("preview_path") for m in (view["moments"]["data"] or [])]
    check(all(p is None or not Path(str(p)).is_absolute() for p in previews),
          "analysis-preview-relative", f"onizleme yollari: {previews}")

    # --- 5. TEST moment selection ------------------------------------------
    status, payload = call(api, "POST", f"/api/projects/{pid}/moment-selection", {
        "selected_rank": moments[0]["rank"],
        "reviewed_by": "verify_real_preview",
        "notes": "IZOLE TEST SECIMI", "require_preview": False})
    check(status == 201, "moment-selection", f"rank={moments[0]['rank']}")

    # --- 6. real render -----------------------------------------------------
    t0 = time.time()
    status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "render",
        "params": {**render_config, "subtitle_whisper_model_size": "small"},
        "run_request_id": "real-render"})
    job = app.job_store(ownership).get(payload["job"]["job_id"])
    manifest = service.load_artifact("render_manifest")["data"]
    render_sec = time.time() - t0
    check(manifest is not None, "render",
          f"is durumu={job['status']} hata={job.get('error')}")
    if not manifest:
        return _finish(app)
    print(f"     render suresi: {render_sec:.1f}s")

    check(manifest["status"] == "review_required", "render-review-required",
          f"artefakt durumu={manifest['status']} (asla otomatik approved degil)")
    check(manifest["human_approval"]["approved"] is False, "render-not-approved",
          "insan onayi VERILMEDI")
    check(manifest["not_uploaded"] is True, "render-not-uploaded",
          "hicbir yere yuklenmedi")
    check(manifest["config"]["pipeline_mode"] == "original_audio_subtitles",
          "render-mode", manifest["config"]["pipeline_mode"])
    check(manifest["config"]["original_audio_preserved"] and not manifest["config"]["tts_used"],
          "render-original-audio", "orijinal ses korundu, TTS kullanilmadi")

    # --- 7. ffprobe technical validation ------------------------------------
    video_path = Path(manifest["output"]["final_video_path"])
    check(video_path.is_file() and video_path.stat().st_size > 0, "video-exists",
          f"{video_path.stat().st_size / 1024:.0f} KB" if video_path.is_file() else "yok")

    ffmpeg_bin, ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
    validation = manifest["output"]["validation"]
    check(validation["passed"], "render-validation",
          f"checks={validation['checks']}")

    if ffprobe_bin:
        proc = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(video_path)],
            capture_output=True, text=True)
        check(proc.returncode == 0, "ffprobe-readable", f"exit={proc.returncode}")
        if proc.returncode == 0:
            probe = json.loads(proc.stdout)
            streams = probe.get("streams", [])
            video = next((s for s in streams if s["codec_type"] == "video"), None)
            audio = next((s for s in streams if s["codec_type"] == "audio"), None)
            duration = float(probe["format"]["duration"])
            check(video is not None, "ffprobe-video-stream",
                  f"{video['codec_name']} {video['width']}x{video['height']}" if video else "yok")
            check(audio is not None, "ffprobe-audio-stream",
                  f"{audio['codec_name']} {audio.get('sample_rate')}Hz" if audio else "yok")
            check(video and video["width"] < video["height"], "ffprobe-vertical",
                  f"{video['width']}x{video['height']} (9:16 dikey)" if video else "")
            check(duration > 1.0, "ffprobe-duration", f"{duration:.2f}s")
    else:
        check(False, "ffprobe-readable", "ffprobe bulunamadi (kurulu degil)")

    # --- 8. contracts -------------------------------------------------------
    check(not fixtures.PROVIDER_CALLS, "no-provider-calls",
          f"saglayici cagrisi: {fixtures.PROVIDER_CALLS}")
    blob = json.dumps(app.project_view(ownership), ensure_ascii=False, default=str)
    check("production_ready" not in blob, "no-production-ready",
          "hicbir artefakt production_ready degil")

    subtitles = manifest.get("subtitles") or {}
    print()
    print("=" * 76)
    print("URETILEN ONIZLEME (INSAN INCELEMESI BEKLIYOR)")
    print("=" * 76)
    print(f"  Video          : {video_path}")
    print(f"  Manifest       : {service.path('render_manifest')}")
    print(f"  artifact_status: {manifest['status']}  (approved DEGIL)")
    print(f"  Altyazi        : {subtitles.get('source')} · {subtitles.get('cue_count')} cue "
          f"· dinamik={subtitles.get('dynamic_word_highlight')}")
    print(f"  Caption tema   : {manifest['config'].get('caption_theme_resolved')} / "
          f"{manifest['config'].get('caption_font_name')}")
    print(f"  Onay kosullari : {manifest['approval']['failed_conditions'] or 'hepsi saglandi'}")
    print()
    print("  INSAN GORSEL INCELEMESI GEREKIYOR: videoyu ac, altyazi senkronunu, crop'u")
    print("  ve orijinal ses surekliligini kendi gozunle dogrula. Bu betik final onay")
    print("  VERMEZ ve veremez.")
    print("=" * 76)
    return _finish(app)


def _finish(app) -> int:
    app.shutdown()
    print()
    if FAILURES:
        print(f"REAL_PREVIEW_FAILED ({len(FAILURES)} sorun)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("REAL_PREVIEW_OK (gercek kaynak, gercek ffmpeg/whisper; saglayici cagrisi YOK, "
          "kullanicinin projesi degistirilmedi, final onay VERILMEDI, hicbir sey yuklenmedi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
