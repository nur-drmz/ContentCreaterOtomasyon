#!/usr/bin/env python
"""M0.5 — Uctan uca Short PoC: FINAL kalite narrated_recap zinciri.

    python verify_short_poc.py

NE KANITLIYOR
-------------
M0.5'in makineyle kanitlanabilir dort maddesi. Digerleri (script okuma, ses
dinleme, videoyu izleme) insan yargisi gerektirir ve bu paket onlari KAPATMAZ --
sadece o yargiyi verebilecegin bir cikti uretir.

Neden ayri bir paket: mevcut dogrulamalarin HEPSI `preview_mode=True` ile
render ediyor, yani 540x960. `preview_mode=False` yolu -- olcek 1.0, crf 18,
preset slow, yani gercek 1080x1920 teslim kalitesi -- bu paket yazilana kadar
hicbir testte calismamisti. Onizleme gecerken finalin patlamasi, tam da teslim
anindan once ogrenilecek en pahali hata.

Zincir: plan -> onay -> analiz -> moment -> script -> ONAY -> TTS -> ONAY ->
FINAL render. Her insan kapisi gercekten kapali olarak sinanir; hicbiri
atlanmaz.

GUVENLIK
--------
Sahte saglayici ikizleri, izole gecici depolama, gercek yerel ffmpeg. Hicbir
gercek API cagrilmaz, hicbir sey yuklenmez.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures                       # noqa: E402
from creator_console.api import ConsoleApi                 # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import render_utils                    # noqa: E402

FAILURES: list = []
CHECKS = 0

#: The plan is approved with this EXACT config; `check_config_drift` refuses a
#: render whose settings differ from the approved ones, so final quality has to
#: be a decision recorded in the plan rather than a flag passed at render time.
FINAL_RENDER_CONFIG = {
    "render_mode": "blurred_background",
    "preview_mode": False,
    "caption_style": "active_word_yellow",
    "caption_theme": "auto",
}


def check(condition, label: str, message: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ - {message}")
    return False


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


def main() -> int:
    print("=" * 74)
    print("M0.5 SHORT PoC DOGRULAMA (sahte saglayicilar, gercek ffmpeg)")
    print("=" * 74)

    ffmpeg_bin, ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
    if not ffmpeg_bin or not ffprobe_bin:
        check(False, "ffmpeg-bulunamadi", "PoC render testi calistirilamadi")
        return 1

    root = Path(tempfile.mkdtemp(prefix="short_poc_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        api = ConsoleApi(app)
        pid = fixtures.seed_demo_project(app, title="M0.5 PoC")["project_id"]
        own = app.ownership(pid)
        service = app.service(own)

        # ---------------------------------------------------- plan + approval
        section("1. narrated_recap plani FINAL kaliteyle onaylaniyor")
        status, _ = call(api, "POST", f"/api/projects/{pid}/plan", {
            "mode": "narrated_theory", "selected_by": "verify_short_poc",
            "output_count": 1,
            "theory_angle": "Bu sahnedeki kararin butun sezonu degistirdigi teorisi",
            "render_config": dict(FINAL_RENDER_CONFIG)})
        check(status == 201, "plan-olusturuldu", f"HTTP {status}")
        plan = service.load_artifact("processing_plan")["data"]
        check(plan["decision"]["render"]["preview_mode"] is False,
              "plan-FINAL-kaliteyi-kaydediyor",
              str(plan["decision"]["render"].get("preview_mode")))
        plan_hash = service.describe_plan_review()["review"]["content_sha256"]
        call(api, "POST", f"/api/projects/{pid}/approvals/processing_plan",
             {"reviewed_sha256": plan_hash, "approved_by": "verify_short_poc"})

        # ------------------------------------------------ analysis + moment
        section("2. Analiz ve moment secimi")
        call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "video_analysis",
            "params": {"whisper_model_size": "tiny", "clip_duration_sec": 4,
                       "top_n": 2}})
        hot = service.load_artifact("hot_moments")["data"]
        check(bool(hot and hot.get("hot_moments")), "aday-moment-uretildi",
              str(len((hot or {}).get("hot_moments") or [])))
        service.record_moment_selection(
            selected_rank=hot["hot_moments"][0]["rank"],
            reviewed_by="verify_short_poc", require_preview=False)

        # -------------------------------------------------- script + approval
        section("3. Script uretimi ve INSAN onayi")
        call(api, "POST", f"/api/projects/{pid}/jobs",
             {"job_type": "script_generation"})
        script_state = service.approval_state("script")
        check(script_state["present"] and not script_state["approved"],
              "script-dogustan-ONAYLI-DEGIL", str(script_state.get("approved")))
        # Render must refuse while the script is unread.
        status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "render", "params": dict(FINAL_RENDER_CONFIG),
            "run_request_id": "poc-too-early"})
        early = (payload or {}).get("job") or {}
        check(early.get("status") == "failed", "onaysiz-scriptle-render-REDDEDILDI",
              str(early.get("status")))
        call(api, "POST", f"/api/projects/{pid}/approvals/script",
             {"reviewed_sha256": script_state["content_sha256"],
              "approved_by": "verify_short_poc"})

        # ----------------------------------------------------- TTS + approval
        section("4. Seslendirme ve INSAN onayi")
        call(api, "POST", f"/api/projects/{pid}/jobs", {"job_type": "tts"})
        voice_state = service.approval_state("voiceover")
        check(voice_state["present"] and not voice_state["approved"],
              "ses-dogustan-ONAYLI-DEGIL", str(voice_state.get("approved")))
        # The hash alone does not open this gate: approving a voiceover also
        # requires the listening checklist, so a reviewer cannot approve audio
        # they never played. Walked here rather than bypassed.
        from creator_console.voice_review import CHECKLIST_KEYS
        try:
            service.approve("voiceover", reviewed_sha256=voice_state["content_sha256"],
                            approved_by="verify_short_poc")
            check(False, "liste-eksikken-ses-onayi-REDDEDILIR", "onay verildi")
        except Exception as exc:  # noqa: BLE001 - refusal is the correct outcome
            check("ONAY REDDEDILDI" in str(exc),
                  "liste-eksikken-ses-onayi-REDDEDILIR", str(exc)[:80])
        for key in CHECKLIST_KEYS:
            service.set_voice_review_item(key, checked=True,
                                          reviewed_by="verify_short_poc")
        voice_state = service.approval_state("voiceover")
        call(api, "POST", f"/api/projects/{pid}/approvals/voiceover",
             {"reviewed_sha256": voice_state["content_sha256"],
              "approved_by": "verify_short_poc"})
        check(service.approval_state("voiceover")["approved"] is True,
              "dinleme-listesi-tamamlaninca-ses-ONAYLANIYOR",
              str(service.approval_state("voiceover").get("reason")))

        # ------------------------------------------------------ FINAL render
        section("5. FINAL 1080x1920 render (onizleme degil)")
        status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "render", "params": dict(FINAL_RENDER_CONFIG),
            "run_request_id": "poc-final"})
        job = (payload or {}).get("job") or {}
        check(job.get("status") in ("review_required", "completed"),
              "final-render-isi-basarili",
              f"{job.get('status')}: {str(job.get('error'))[:180]}")
        manifest = service.load_artifact("render_manifest")["data"]
        check(manifest is not None, "render-manifesti-yazildi")
        if not manifest:
            return _finish()

        # --- M0.5: teknik dogrulama video VE ses akisini basarili bulmali ---
        section("6. M0.5: teknik dogrulama (video + ses akisi)")
        validation = (manifest.get("output") or {}).get("validation") or {}
        checks = validation.get("checks") or {}
        check(validation.get("passed") is True, "teknik-dogrulama-GECTI",
              str(checks))
        check(checks.get("has_video_stream") is True, "VIDEO-akisi-var")
        check(checks.get("has_audio_stream") is True, "SES-akisi-var",
              "anlatimli Short sessiz cikarsa PoC tamamlanmis sayilmaz")

        video_path = Path(manifest["output"]["final_video_path"])
        check(video_path.is_file() and video_path.stat().st_size > 0,
              "final-video-diskte",
              f"{video_path.stat().st_size / 1024:.0f} KB"
              if video_path.is_file() else "yok")
        probe = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(video_path)], capture_output=True, text=True).stdout.strip()
        check(probe == "1080,1920", "cikti-GERCEKTEN-1080x1920",
              f"{probe} (onizleme 540x960 verir; final olceklenmemis olmali)")
        audio_probe = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(video_path)], capture_output=True, text=True).stdout.strip()
        check(audio_probe == "audio", "dosyada-GERCEK-ses-akisi",
              repr(audio_probe))

        # --- M0.5: manifestteki ust-akis onay kosullari True olmali ---------
        section("7. M0.5: ust-akis onay kosullari")
        approval_block = manifest.get("approval") or {}
        conditions = approval_block.get("conditions") or {}
        check(bool(conditions), "onay-kosullari-kayitli", str(conditions))
        false_ones = [name for name, ok in conditions.items() if not ok]
        check(not false_ones, "TUM-ust-akis-kosullari-True", str(false_ones))
        check(not (approval_block.get("failed_conditions") or []),
              "manifest-basarisiz-kosul-BILDIRMIYOR",
              str(approval_block.get("failed_conditions")))
        chain = service.approval_chain()
        for kind in ("processing_plan", "script", "voiceover"):
            check(chain[kind]["effective_valid"] is True,
                  f"zincir-{kind}-gecerli", str(chain[kind].get("reason")))
        check(chain["render"]["approved"] is False,
              "render-INSAN-onayini-bekliyor",
              "final render kendiliginden onaylanmis gorunuyor")

        # --- M0.5: hicbir platforma otomatik yuklenmemis olmali --------------
        section("8. M0.5: otomatik yayin yok")
        check(manifest.get("not_uploaded") is True, "manifest-not_uploaded",
              str(manifest.get("not_uploaded")))
        check(service.load_artifact("publish_manifest")["data"] is None,
              "yayin-manifesti-URETILMEDI")
        uploads = [c for c in fixtures.PROVIDER_CALLS
                   if "upload" in str(c).lower() or "publish" in str(c).lower()]
        check(not uploads, "hicbir-yukleme-cagrisi-YOK", str(uploads))

        # --- M0.5: calistirma ozeti ve hata dosyalari saklanmali -------------
        section("9. M0.5: calistirma ozeti ve hata kaydi")
        jobs = app.job_store(own).list(limit=100)
        kinds = {j["job_type"] for j in jobs}
        check({"video_analysis", "script_generation", "tts", "render"} <= kinds,
              "is-gecmisi-butun-fazlari-tasiyor", str(sorted(kinds)))
        failed = [j for j in jobs if j["status"] == "failed"]
        check(any(j["job_type"] == "render" for j in failed),
              "BASARISIZ-deneme-de-saklandi",
              "reddedilen erken render kaydi silinmemeli")
        check(all(j.get("error") for j in failed),
              "basarisiz-is-NEDENINI-tasiyor",
              str([j.get("error") for j in failed][:1]))
        audit = service.audit.events(limit=200)
        check(len(audit) > 0, "audit-izi-kalici", f"{len(audit)} olay")
        blob = json.dumps(audit) + json.dumps(jobs, default=str)
        check("FAKE-CONSOLE-GEMINI" not in blob, "kayitlarda-SIR-YOK")

        return _finish()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _finish() -> int:
    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, "
          f"{len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        print("SHORT_POC_BASARISIZ")
        return 1
    print("SHORT_POC_OK (sahte saglayicilar, gercek ffmpeg; hicbir gercek API "
          "cagrilmadi, hicbir sey yuklenmedi)")
    print("NOT: M0.5'in izleme/dinleme maddeleri bu paketle KAPANMAZ; "
          "uretilen final_short izlenmelidir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
