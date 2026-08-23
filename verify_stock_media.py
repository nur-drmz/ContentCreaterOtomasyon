#!/usr/bin/env python
"""Offline verification for the MoneyPrinterTurbo-inspired stock-media layer.

No real Pexels/Pixabay request is made and no credential is uploaded.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import long_form_script, stock_media  # noqa: E402

FAILURES = []
CHECKS = 0


def check(condition, label, message=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK")
    else:
        FAILURES.append(f"{label}: {message}")
        print(f"  [{label}] FAILED - {message}")


def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


class Response:
    def __init__(self, payload=None, *, status=200, headers=None, chunks=None):
        self._payload = payload or {}
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=0):
        yield from self._chunks


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def pexels_payload():
    return {"videos": [{
        "id": 101, "width": 1920, "height": 1080, "duration": 14,
        "url": "https://www.pexels.com/video/example-101/?tracking=x",
        "user": {"name": "Ada", "url": "https://www.pexels.com/@ada?x=1"},
        "video_pictures": [{"picture": "https://images.pexels.com/videos/101/p.jpg"}],
        "video_files": [
            {"link": "https://videos.pexels.com/video-files/101/low.mp4",
             "file_type": "video/mp4", "width": 640, "height": 360},
            {"link": "https://videos.pexels.com/video-files/101/full.mp4?x=1",
             "file_type": "video/mp4", "width": 1920, "height": 1080},
        ],
    }, {
        "id": 102, "width": 1080, "height": 1920,
        "video_files": [{"link": "https://videos.pexels.com/portrait.mp4",
                         "file_type": "video/mp4", "width": 1080, "height": 1920}],
    }]}


def pixabay_payload():
    return {"hits": [{
        "id": 202, "pageURL": "https://pixabay.com/videos/id-202/?key=NOPE",
        "duration": 9, "user": "Lin", "user_id": 7,
        "videos": {
            "large": {"url": "https://cdn.pixabay.com/video/202_large.mp4",
                      "width": 1920, "height": 1080, "size": 4000,
                      "thumbnail": "https://cdn.pixabay.com/video/202_large.jpg"},
            "tiny": {"url": "https://cdn.pixabay.com/video/202_tiny.mp4",
                     "width": 480, "height": 270, "size": 500},
        },
    }]}


def main():
    print("\n--- provider normalization ---")
    pexels = stock_media.parse_pexels(pexels_payload(), query="dragon flight")
    check(len(pexels) == 1, "landscape-filter", str(pexels))
    check((pexels[0]["width"], pexels[0]["height"]) == (1920, 1080),
          "best-1080p-rendition")
    check(pexels[0]["source_page"] == "https://www.pexels.com/video/example-101/",
          "public-page-query-stripped", pexels[0]["source_page"])
    check(pexels[0]["creator"]["name"] == "Ada" and "Pexels" in pexels[0]["attribution"],
          "creator-attribution")
    pixabay = stock_media.parse_pixabay(pixabay_payload(), query="castle")
    check(len(pixabay) == 1 and pixabay[0]["provider"] == "pixabay",
          "pixabay-normalized")
    check("key=" not in json.dumps(pixabay), "pixabay-key-not-persisted")

    print("\n--- explicit searches and secret handling ---")
    p_session = Session(Response(pexels_payload(), headers={
        "X-Ratelimit-Limit": "200", "X-Ratelimit-Remaining": "199"}))
    result = stock_media.search_videos(
        "pexels", "dragon flight", "SECRET_PEXELS", session=p_session)
    check(result["count"] == 1, "pexels-search-result")
    check(p_session.calls[0][1]["headers"]["Authorization"] == "SECRET_PEXELS",
          "pexels-auth-header")
    check("SECRET_PEXELS" not in json.dumps(result), "pexels-secret-not-returned")
    x_session = Session(Response(pixabay_payload()))
    x_result = stock_media.search_videos(
        "pixabay", "castle", "SECRET_PIXABAY", session=x_session)
    check(x_result["count"] == 1, "pixabay-search-result")
    check("SECRET_PIXABAY" in x_session.calls[0][0], "pixabay-key-used-on-wire")
    check("SECRET_PIXABAY" not in json.dumps(x_result), "pixabay-secret-not-returned")
    denied = Session(Response(status=401))
    try:
        stock_media.search_videos("pixabay", "castle", "SECRET", session=denied)
        safe_error = ""
    except stock_media.StockMediaError as exc:
        safe_error = str(exc)
    check("SECRET" not in safe_error and "reddedildi" in safe_error,
          "provider-error-sanitized", safe_error)

    print("\n--- plan, selection, atomic download ---")
    chapters = {"chapters": [
        {"index": 1, "title": "Dragon bonds", "text": "First half. Second half."},
        {"index": 2, "title": "The war", "text": "War changes every alliance."},
    ]}
    plan = stock_media.new_plan(chapters, clip_duration_sec=5)
    check(len(plan["slots"]) == 4, "two-slots-per-chapter")
    check([item["sequence"] for item in plan["slots"]] == [1, 2, 3, 4],
          "script-order-deterministic")
    check(not stock_media.is_stale(plan, chapters), "fresh-plan-hash-matches-script")
    edited_chapters = json.loads(json.dumps(chapters))
    edited_chapters["chapters"][0]["text"] += " Edited."
    check(stock_media.is_stale(plan, edited_chapters), "chapter-edit-makes-plan-stale")
    plan = stock_media.store_search(plan, "ch01-1", result)
    plan = stock_media.select_candidate(plan, "ch01-1", "pexels:101")
    check(plan["slots"][0]["selected"]["asset_id"] == "101", "candidate-selected")
    temp_root = Path(tempfile.mkdtemp(prefix="stock_media_"))
    destination = temp_root / "pexels_101.mp4"
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"x" * 40
    d_session = Session(Response(headers={"Content-Length": str(len(mp4))}, chunks=[mp4]))
    downloaded = stock_media.download_candidate(
        plan["slots"][0]["selected"], destination, session=d_session)
    check(destination.is_file() and downloaded["bytes"] == len(mp4),
          "atomic-mp4-download")
    check(len(downloaded["sha256"]) == 64, "download-sha256")
    check(not destination.with_suffix(".mp4.part").exists(), "no-part-file-after-success")

    print("\n--- Creator Console API and UI contract ---")
    secrets = fixtures.fake_secret_getter()
    values = {name: secrets(name) for name in (
        "GeminiKey", "GOOGLE_API_KEY", "YoutubeKEY", "YOUTUBE_DATA_API_KEY")}
    values.update({"PEXELS_API_KEY": "SERVER_ONLY_PEXELS"})
    app_root = temp_root / "console"
    app = ConsoleApp(ConsoleConfig(
        storage_root=app_root, inline_jobs=True,
        secret_getter=lambda name: values.get(name),
        **fixtures.fixture_client_factories()))
    api = ConsoleApi(app)
    pid = app.create_project(title="Stock B-roll", created_by="verify")["project_id"]
    service = app.service(app.ownership(pid))
    studio = service.long_form_studio()
    studio._write(long_form_script.CHAPTERS_ARTIFACT, chapters, actor="verify")
    status, payload = call(api, "POST", f"/api/projects/{pid}/long-form/broll",
                           {"action": "plan", "clip_duration_sec": 6})
    view = payload.get("long_form_studio") or {}
    check(status == 200 and view["broll"]["slot_count"] == 4,
          "api-plan-no-provider-call", str(payload)[:160])
    check(view["broll"]["providers"][0]["configured"] is True,
          "provider-readiness-public")
    check("SERVER_ONLY_PEXELS" not in json.dumps(payload), "api-never-returns-secret")
    original_search = stock_media.search_videos
    captured = []
    try:
        def fake_search(provider, query, api_key, **kwargs):
            captured.append((provider, query, api_key))
            return dict(result, query=query, provider=provider)
        stock_media.search_videos = fake_search
        status, payload = call(api, "POST", f"/api/projects/{pid}/long-form/broll", {
            "action": "search", "slot_id": "ch01-1", "provider": "pexels",
            "query": "dragon flight", "per_page": 6})
    finally:
        stock_media.search_videos = original_search
    check(status == 200 and len(captured) == 1, "search-only-on-explicit-post")
    check(captured[0][2] == "SERVER_ONLY_PEXELS", "server-resolves-credential")
    check("SERVER_ONLY_PEXELS" not in json.dumps(payload), "search-response-secret-free")
    app_js = (ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    check("lf-broll-plan" in app_js and "lf-broll-search" in app_js,
          "ui-plan-and-explicit-search-controls")
    check("Kaynak sayfasi" in app_js and "Secili videoyu indir" in app_js,
          "ui-provenance-and-download-controls")

    shutil.rmtree(temp_root, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        for failure in FAILURES:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
