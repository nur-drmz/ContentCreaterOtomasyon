#!/usr/bin/env python
"""ONE deliberate live Gemini script call, to validate the narrated_theory path.

    python verify_live_script.py --i-authorize-one-live-call

WHY THIS IS SEPARATE
--------------------
Every other suite in this repo is mock-only and must stay that way. This script
is the single exception: it makes exactly ONE real request to the Gemini
Developer API using the configured `GeminiKey`, because a mock can never prove
that the real model returns something the parser and the schema accept.

It therefore refuses to run without the explicit flag, and it is wired so that
one request is genuinely one request:

    allow_provider_fallback   = False   (no OpenAI/Anthropic hop)
    enable_google_model_fallback = False   (no 3.5-flash / flash-lite hop)
    max_google_model_attempts    = 1
    max_google_transient_retries = 0    (no 5xx/RPM retry)

The only remaining automatic repeat is the ONE corrective retry the router does
on malformed structured output; the call counter below reports it explicitly if
it happens, so the number is never hidden.

HARD STOPS
----------
Stops at script review. Does NOT approve the script, does NOT run TTS, does NOT
render, uploads nothing, publishes nothing, and never touches the user's own
project (isolated storage root + its own workspace).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.env_file import load_env_file  # noqa: E402
from local_pipeline import google_ai  # noqa: E402

STORAGE_ROOT = PROJECT_ROOT / ".console_data_livetest"
SOURCE_CANDIDATES = [
    Path.home() / "Downloads" / "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4",
    Path.home() / "Downloads" / "Daemon Targaryen may never have truly wanted the Iron Throne..mp4",
]
THEORY_ANGLE = (
    "Daemon Targaryen'in tahtı gerçekten istemediği, her hamlesinin aslında "
    "aile içindeki yerini kanıtlama çabası olduğu teorisi"
)
CLIP_START_SEC = 60
CLIP_DURATION_SEC = 90

FAILURES: list = []
#: Every real call the counting client makes, so the number is auditable.
LIVE_CALLS: list = []


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


class CountingGoogleClient:
    """Wraps the real genai client and records every request.

    A thin proxy, not a mock: the request goes to Google. It exists so the count
    is measured rather than asserted from the code path.
    """

    def __init__(self, inner):
        self._inner = inner
        self.models = _CountingModels(inner.models)


class _CountingModels:
    def __init__(self, inner):
        self._inner = inner

    def generate_content(self, **kwargs):
        LIVE_CALLS.append({"api": "generate_content", "model": kwargs.get("model")})
        return self._inner.generate_content(**kwargs)

    def embed_content(self, **kwargs):
        LIVE_CALLS.append({"api": "embed_content", "model": kwargs.get("model")})
        return self._inner.embed_content(**kwargs)

    def list(self):
        LIVE_CALLS.append({"api": "models.list", "model": None})
        return self._inner.list()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-authorize-one-live-call", action="store_true",
                        help="ZORUNLU: gercek bir Gemini cagrisi yapilacagini onayla")
    parser.add_argument("--keep", action="store_true",
                        help="izole depolamayi silme (artefakti incelemek icin)")
    args = parser.parse_args(argv)

    if not args.i_authorize_one_live_call:
        print("REFUSED: bu betik GERCEK bir Gemini API cagrisi yapar.")
        print("Onaylamak icin: python verify_live_script.py --i-authorize-one-live-call")
        return 2

    load_env_file(PROJECT_ROOT / ".env.local")
    source = next((p for p in SOURCE_CANDIDATES if p.is_file()), None)
    if source is None:
        print("REAL_SOURCE_MISSING: HOTD kaynak videosu bulunamadi.")
        return 2

    print("=" * 76)
    print("CANLI narrated_theory DOGRULAMASI - TAM OLARAK 1 Gemini cagrisi")
    print("=" * 76)
    print(f"  Kaynak       : {source.name}")
    print(f"  Izole depolama: {STORAGE_ROOT}")
    print("  Politika     : fallback KAPALI, yeniden deneme 0, script ONAYLANMAYACAK,")
    print("                 TTS/render/yukleme/yayin YOK.")
    print()

    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT, ignore_errors=True)

    counted = {"client": None}

    def google_client_factory():
        credential = google_ai.resolve_secret("google_api", lambda n: os.environ.get(n))
        if not credential.configured:
            raise RuntimeError("GeminiKey yapilandirilmamis")
        if counted["client"] is None:
            counted["client"] = CountingGoogleClient(
                google_ai.build_google_client(credential))
        return counted["client"]

    app = ConsoleApp(ConsoleConfig(
        storage_root=STORAGE_ROOT,
        user_id="usr_livetest", workspace_id="wsp_livetest",
        inline_jobs=True,
        secret_getter=lambda name: os.environ.get(name),
        google_client_factory=google_client_factory,
        openai_client_factory=lambda: None,
        anthropic_client_factory=lambda: None,
        elevenlabs_client_factory=lambda: None))
    api = ConsoleApi(app)

    reference = app.secrets.reference("google_api")
    check(reference.configured, "credential",
          f"{reference.to_public_dict()['state']} ({reference.alias_used})")
    if not reference.configured:
        return _finish(app, args.keep)

    # --- project + source ---------------------------------------------------
    status, payload = call(api, "POST", "/api/projects",
                           {"title": "CANLI narrated_theory testi (izole)",
                            "created_by": "verify_live_script"})
    pid = payload["project"]["project_id"]
    ownership = app.ownership(pid)
    service = app.service(ownership)
    api.handle("POST", f"/api/projects/{pid}/source", body=source.read_bytes(),
               headers={"X-Filename": source.name, "X-Actor": "verify_live_script"})

    call(api, "POST", f"/api/projects/{pid}/jobs",
         {"job_type": "source_assessment", "run_request_id": "live-assess"})
    check(service.load_artifact("source_assessment")["data"] is not None,
          "assessment", "kaynak degerlendirildi (yerel ffprobe)")

    # --- narrated_theory plan, approved as a test operator ------------------
    status, payload = call(api, "POST", f"/api/projects/{pid}/plan", {
        "mode": "narrated_theory", "selected_by": "verify_live_script",
        "output_count": 1, "target_duration_sec": 60,
        "theory_angle": THEORY_ANGLE,
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"},
        "notes": "IZOLE CANLI TEST - kullanicinin projesi degildir"})
    check(status == 201 and payload["plan"]["decision"]["mode"] == "narrated_theory",
          "plan-created", f"HTTP {status}")
    digest = service.describe_plan_review()["review"]["content_sha256"]
    status, payload = call(api, "POST", f"/api/projects/{pid}/approvals/processing_plan",
                           {"reviewed_sha256": digest, "approved_by": "verify_live_script"})
    check(status == 200 and payload["artifact"]["status"] == "approved",
          "plan-approved", "test plani hash ile onaylandi")

    # --- local analysis + one moment ---------------------------------------
    call(api, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"clip_start_sec": CLIP_START_SEC,
                   "clip_duration_sec": CLIP_DURATION_SEC,
                   "whisper_model_size": "small", "scene_detector": "content", "top_n": 3},
        "run_request_id": "live-analysis"})
    hot = service.load_artifact("hot_moments")["data"]
    check(hot and hot.get("hot_moments"), "analysis",
          f"{len(hot['hot_moments']) if hot else 0} aday")
    if not hot or not hot.get("hot_moments"):
        return _finish(app, args.keep)
    moment = hot["hot_moments"][0]
    call(api, "POST", f"/api/projects/{pid}/moment-selection", {
        "selected_rank": moment["rank"], "reviewed_by": "verify_live_script",
        "notes": "CANLI TEST SECIMI", "require_preview": False})
    check(service.load_artifact("moment_selection")["data"] is not None,
          "moment-selection",
          f"rank={moment['rank']} ({moment['source_start_sec']:.0f}s-"
          f"{moment['source_end_sec']:.0f}s)")

    # --- THE one live call --------------------------------------------------
    print()
    print("  >>> GERCEK Gemini cagrisi yapiliyor (tek model, tek deneme, 0 retry)...")
    LIVE_CALLS.clear()
    result = service.run_script_generation(
        actor="verify_live_script",
        enable_google_model_fallback=False,
        max_google_model_attempts=1,
        max_google_transient_retries=0)

    generate_calls = [c for c in LIVE_CALLS if c["api"] == "generate_content"]
    print(f"  >>> tamamlandi: {len(generate_calls)} generate_content cagrisi")
    for entry in LIVE_CALLS:
        print(f"        {entry['api']} model={entry['model']}")

    check(len(generate_calls) == 1, "one-call-only",
          f"{len(generate_calls)} generate_content cagrisi yapildi")
    check(all(c["model"] == "gemini-3.6-flash" for c in generate_calls),
          "model-pinned", f"kullanilan model(ler): {[c['model'] for c in generate_calls]}")

    artifact = service.load_artifact("selected_script")
    check(artifact["data"] is not None and not artifact["problem"],
          "artifact-persisted", artifact["problem"] or "selected_script.json yazildi")
    if not artifact["data"]:
        return _finish(app, args.keep)
    script = artifact["data"]

    check(script["status"] == "review_required", "review-required",
          f"artifact_status={script['status']}")
    check(script["human_approval"]["approved"] is False, "not-approved",
          "insan onayi VERILMEDI")
    check(script.get("provider") == "google", "provider", str(script.get("provider")))
    check(bool(script.get("script_text")), "script-text",
          f"{len(script.get('script_text') or '')} karakter")
    check(script.get("theory_angle") == THEORY_ANGLE, "theory-angle-bound",
          "plan teori acisi artefakta islendi")

    attempts = script.get("provider_attempts") or []
    google_attempts = [a for a in attempts if a.get("provider") == "google"]
    check(len(google_attempts) == 1 and google_attempts[0]["ok"],
          "single-attempt-recorded", f"{len(google_attempts)} deneme kaydi")
    route = (google_attempts[0].get("google_route") or {}) if google_attempts else {}
    check(route.get("route_planned") == ["gemini-3.6-flash"], "route-pinned",
          f"planlanan rota: {route.get('route_planned')}")

    # --- contracts ----------------------------------------------------------
    for forbidden, label in ((service.load_artifact("selected_voiceover")["data"], "tts"),
                             (service.load_artifact("render_manifest")["data"], "render")):
        check(forbidden is None, f"no-{label}", f"{label} fazi CALISTIRILMADI")

    blob = json.dumps(app.project_view(ownership), ensure_ascii=False, default=str)
    secret_value = os.environ.get("GeminiKey") or ""
    check(secret_value and secret_value not in blob, "no-secret-leak",
          "API yanitinda anahtar yok")
    check("production_ready" not in blob, "no-production-ready", "production_ready yok")
    check(script.get("not_uploaded", True) is not False, "not-uploaded",
          "hicbir yere yuklenmedi")

    # --- Script Studio visibility -------------------------------------------
    view = app.project_view(ownership)
    approval_state = (view["approvals"] or {}).get("script") or {}
    check(approval_state.get("present") and approval_state.get("reviewable"),
          "script-studio-visible",
          f"present={approval_state.get('present')} reviewable={approval_state.get('reviewable')}")
    check(bool(approval_state.get("content_sha256")), "review-hash-shown",
          f"inceleme hash'i: {str(approval_state.get('content_sha256'))[:16]}...")
    workflow = view.get("workflow") or {}
    check(workflow.get("current_step") == "script", "workflow-at-script",
          f"akis haritasi su anda: {workflow.get('current_label')}")

    script_path = service.path("selected_script")
    print()
    print("=" * 76)
    print("URETILEN SCRIPT (INSAN INCELEMESI BEKLIYOR - ONAYLANMADI)")
    print("=" * 76)
    print(f"  Artefakt : {script_path}")
    print(f"  Model    : {script.get('model')}")
    print(f"  Durum    : {script['status']}")
    print(f"  Teori    : {script.get('theory_angle')}")
    print(f"  Hook     : {(script.get('hook') or '-')[:100]}")
    print()
    print("  --- script metni ---")
    for line in (script.get("script_text") or "").splitlines() or ["(bos)"]:
        print(f"  {line}")
    print("=" * 76)
    return _finish(app, args.keep)


def _finish(app, keep: bool) -> int:
    app.shutdown()
    print()
    print(f"  Gercek API cagrisi toplami: {len(LIVE_CALLS)} "
          f"({len([c for c in LIVE_CALLS if c['api'] == 'generate_content'])} generate_content)")
    if not keep:
        print(f"  Izole depolama korunuyor (inceleme icin): {STORAGE_ROOT}")
    if FAILURES:
        print(f"\nLIVE_SCRIPT_FAILED ({len(FAILURES)} sorun)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nLIVE_SCRIPT_OK (1 gercek Gemini cagrisi; script review_required olarak kaydedildi, "
          "ONAYLANMADI, TTS/render/yukleme/yayin yapilmadi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
