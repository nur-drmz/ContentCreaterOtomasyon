#!/usr/bin/env python
"""Tek klipli Video Editor -- klip araligi duzenleme.

    python verify_clip_bounds.py

NE KANITLIYOR
-------------
Secilen momentin araligi bugune kadar KESINDI. `scene_editor.trim_clip` yalnizca
mevcut araligin ICINE daraltiyordu: bir saniye gec baslayan bir moment icin tek
care analizi bastan calistirmakti, o da transkripti ve scripti birlikte cope
atiyordu.

Bu paket sunlari kanitlar:

  1. `set_clip_source_bounds` klibi kaynagin herhangi bir yerine TASIYABILIR ve
     eski sinirlarin DISINA genisletebilir; `trim_clip` daraltma semantigini
     korur (mevcut cagiranlari kirmaz).
  2. `shift_clip` sureyi KORUYARAK kaydirir ve kaynak sinirinda durur.
  3. Ana render duzenlenen araligi kullanir; `hot_moments` DEGISMEZ -- narrated
     pipeline zaman damgalarini o dosyaya karsi dogruladigi icin onu yamalamak
     render'i reddettirirdi.
  4. Override yalnizca ORIJINAL kaynakta gecerlidir. Analiz klibi zaten
     kirpilmistir; orada mutlak saniye baska bir yere denk gelir, bu yuzden
     sessizce yanlis yeri render etmek yerine REDDEDILIR.
  5. Gecersiz kilma matrisi: direct modda aralik degisince transkript onayi
     duser; narrated modda script/ses/altyazi KORUNUR.
  6. Slot izolasyonu: bir slotun araligi digerininkini degistirmez.

NEGATIF KONTROL
---------------
Her davranis icin duzeltmeyi geri alan bir kontrol var.

GUVENLIK
--------
Izole gecici depolama, sahte saglayicilar. Hicbir gercek API cagrilmaz, hicbir
video render edilmez.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures                              # noqa: E402
from creator_console.api import ConsoleApi                        # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig         # noqa: E402
from creator_console.pipeline_service import PipelineServiceError  # noqa: E402
from creator_console.storage import atomic_write_json             # noqa: E402
from local_pipeline import render_utils, scene_editor             # noqa: E402

FAILURES: list = []
CHECKS = 0


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


# --------------------------------------------------------------- 1. sinirlar
def test_bounds_arithmetic() -> None:
    section("1. Klip kaynagin herhangi bir yerine tasinabilir")

    edl = scene_editor.build_edl("/src.mp4", 60.0)
    clip = scene_editor.trim_clip(edl.clips[0], 10.0, 20.0, 60.0)

    wider = scene_editor.set_clip_source_bounds(clip, 5.0, 25.0, 60.0)
    check(wider.source_start_sec == 5.0 and wider.source_end_sec == 25.0,
          "ESKI-SINIRLARIN-DISINA-genisletilebilir",
          f"{wider.source_start_sec}-{wider.source_end_sec}")

    moved = scene_editor.set_clip_source_bounds(clip, 40.0, 50.0, 60.0)
    check(moved.source_start_sec == 40.0,
          "klip-baska-bir-yere-TASINABILIR", str(moved.source_start_sec))

    # NEGATIF KONTROL: trim daraltma semantigini KORUMALI. Korumasaydi
    # mevcut cagiranlarin davranisini sessizce degistirmis olurduk.
    try:
        scene_editor.trim_clip(clip, 5.0, 25.0, 60.0)
        check(False, "NEGATIF-trim_clip-hala-DARALTMA", "genisletmeye izin verdi")
    except ValueError as exc:
        check(True, "NEGATIF-trim_clip-hala-DARALTMA", str(exc)[:50])

    shifted = scene_editor.shift_clip(clip, -8.0, 60.0)
    check(shifted.source_start_sec == 2.0
          and round(shifted.source_end_sec - shifted.source_start_sec, 6) == 10.0,
          "kaydirma-SUREYI-korur",
          f"{shifted.source_start_sec}-{shifted.source_end_sec}")

    clamped = scene_editor.shift_clip(clip, -999.0, 60.0)
    check(clamped.source_start_sec == 0.0
          and round(clamped.source_end_sec - clamped.source_start_sec, 6) == 10.0,
          "kaynak-basinda-DURUR-hata-vermez",
          f"{clamped.source_start_sec}-{clamped.source_end_sec}")

    end_clamped = scene_editor.shift_clip(clip, 999.0, 60.0)
    check(end_clamped.source_end_sec == 60.0,
          "kaynak-sonunda-DURUR", str(end_clamped.source_end_sec))

    for start, end, label in (
        (-1.0, 5.0, "negatif-baslangic-REDDEDILIR"),
        (5.0, 5.0, "sifir-sure-REDDEDILIR"),
        (5.0, 5.1, "cok-kisa-klip-REDDEDILIR"),
        (50.0, 70.0, "kaynak-disina-tasma-REDDEDILIR"),
        (float("nan"), 5.0, "NaN-REDDEDILIR"),
    ):
        try:
            scene_editor.set_clip_source_bounds(clip, start, end, 60.0)
            check(False, label, "kabul edildi")
        except ValueError as exc:
            check(True, label, str(exc)[:50])


# ----------------------------------------------------------- 2. render hooku
def test_render_override() -> None:
    section("2. Ana render duzenlenen araligi kullanir")

    apply = render_utils.apply_source_bounds_override
    check(apply(10.0, 5.0, "raw", None) == (10.0, 5.0),
          "override-YOKKEN-davranis-DEGISMEZ")
    check(apply(10.0, 5.0, "raw",
                {"source_start_sec": 3.0, "source_end_sec": 9.0}) == (3.0, 6.0),
          "override-seek-baslangicini-ve-suresini-BELIRLER")

    # Analiz klibi zaten kirpilmis: mutlak saniye orada baska yere denk gelir.
    for mode in ("analysis_clip", "preview"):
        try:
            apply(10.0, 5.0, mode,
                  {"source_start_sec": 3.0, "source_end_sec": 9.0})
            check(False, f"{mode}-override-REDDEDILIR", "kabul edildi")
        except ValueError as exc:
            check("orijinal kaynak" in str(exc), f"{mode}-override-REDDEDILIR",
                  str(exc)[:50])

    try:
        apply(10.0, 5.0, "raw", {"source_start_sec": 9.0, "source_end_sec": 3.0})
        check(False, "ters-aralik-REDDEDILIR", "kabul edildi")
    except ValueError as exc:
        check(True, "ters-aralik-REDDEDILIR", str(exc)[:50])

    # Her iki pipeline da parametreyi kabul etmeli, yoksa duzenleme yalnizca
    # bir modda calisir ve digerinde sessizce yok sayilir.
    import inspect
    for func in (render_utils.run_original_audio_subtitle_pipeline,
                 render_utils.run_render_pipeline):
        params = inspect.signature(func).parameters
        check("source_bounds_override" in params,
              f"{func.__name__}-parametreyi-KABUL-EDIYOR")


# ------------------------------------------------------ 3. gecersiz kilma
def _project(app, mode: str, outputs: int = 1):
    seeded = fixtures.seed_demo_project(app, title=f"Klip araligi {mode}")
    ownership = app.ownership(seeded["project_id"])
    service = app.service(ownership)
    kwargs = {"mode": mode, "selected_by": "verify", "output_count": outputs,
              "render_config": {"render_mode": "blurred_background",
                                "preview_mode": True,
                                "caption_style": "active_word_yellow",
                                "caption_theme": "auto"}}
    if mode == "narrated_theory":
        kwargs["theory_angle"] = "Bu sahnedeki karar aslinda bir tuzaktan ibaret."
    service.create_processing_plan(**kwargs)
    return seeded["project_id"], ownership, service


def _fake_analysis(app, ownership, service, *, slot: int = 1) -> None:
    """A hot_moments + selection pair, so a clip exists to edit.

    The source LENGTH is written into `source_assessment`, where Phase 0's
    ffprobe actually records it -- NOT into `hot_moments.source`, which carries
    paths and the analysis window but no duration. A fixture that invents a
    duration where the product never stores one proves only that the fixture
    and the code agree with each other.
    """
    assessment = service.load_artifact("source_assessment")["data"] or {}
    assessment.setdefault("source", {})["duration_sec"] = 60.0
    atomic_write_json(
        app.storage.resolve(ownership, "results/source_assessment.json",
                            create_parent=True),
        assessment)
    atomic_write_json(
        app.storage.resolve(ownership, "results/hot_moments.json",
                            create_parent=True),
        {"status": "ok", "hot_moments": [
            {"rank": 1, "source_start_sec": 10.0, "source_end_sec": 20.0,
             "aligned_start_sec": 10.0, "aligned_end_sec": 20.0}],
         "source": {"raw_video_path": str(service.source_path())}})
    atomic_write_json(
        app.storage.resolve(ownership,
                            service.artifact_relative("moment_selection", slot),
                            create_parent=True),
        {"artifact_type": "moment_selection", "status": "ok",
         "selected_moment_rank": 1})


def _fake_transcript(app, ownership, service, *, slot: int = 1) -> Path:
    path = app.storage.resolve(
        ownership, service.artifact_relative("moment_transcript", slot),
        create_parent=True)
    atomic_write_json(path, {"artifact_type": "moment_transcript", "status": "ok",
                             "words": [], "cues": []})
    return path


def _fake_script(app, ownership, service) -> Path:
    path = app.storage.resolve(ownership, "results/selected_script.json",
                               create_parent=True)
    atomic_write_json(path, {"status": "ok", "provider_attempts": []})
    return path


def test_invalidation_matrix() -> None:
    section("3. Gecersiz kilma: direct modda transkript duser, narrated'da durur")

    root = Path(tempfile.mkdtemp(prefix="clip_bounds_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))

        # ---- direct mod: altyazi metni BU aralikten geliyor
        _pid, ownership, service = _project(app, "direct_clip_series")
        _fake_analysis(app, ownership, service)
        transcript = _fake_transcript(app, ownership, service)

        bounds = service.clip_bounds(1)
        check(bounds["source_start_sec"] == 10.0 and not bounds["edited"],
              "duzenlenmemis-aralik-MOMENTTEN-gelir",
              f"{bounds['source_start_sec']}-{bounds['source_end_sec']}")
        check(service.clip_bounds_override(1) is None,
              "duzenleme-yokken-override-YOK")

        result = service.save_clip_bounds(start=8.0, end=22.0, actor="verify", slot=1)
        check(result["transcript_invalidated"] is True,
              "DIRECT-modda-transkript-GECERSIZ-kilindi")
        check(not transcript.exists(),
              "transkript-ARSIVE-tasindi (silinmedi)")
        check(any("moment_transcript" in path
                  for path in result["archived"]["moved_paths"]),
              "arsiv-manifesti-transkripti-ISIMLENDIRIYOR",
              str(result["archived"]["moved_paths"]))
        check(service.clip_bounds_override(1) == {"source_start_sec": 8.0,
                                                  "source_end_sec": 22.0},
              "override-RENDER'A-gidiyor", str(service.clip_bounds_override(1)))
        check(service.clip_bounds(1)["edited"] is True, "aralik-DUZENLENDI-isareti")

        # hot_moments DOKUNULMAMIS olmali: narrated pipeline onu dogruluyor.
        hot = service.load_artifact("hot_moments")["data"]
        check(hot["hot_moments"][0]["source_start_sec"] == 10.0,
              "HOT_MOMENTS-degismedi", str(hot["hot_moments"][0]["source_start_sec"]))

        # ---- narrated mod: anlatim degismedi, script/ses KORUNMALI
        _pid2, own2, svc2 = _project(app, "narrated_theory")
        _fake_analysis(app, own2, svc2)
        script = _fake_script(app, own2, svc2)
        result2 = svc2.save_clip_bounds(start=8.0, end=22.0, actor="verify", slot=1)
        check(result2["transcript_invalidated"] is False,
              "NARRATED-modda-transkript-DUSMEZ")
        check(script.exists(), "NARRATED-modda-script-KORUNDU")
        check(not any("selected_script" in path
                      for path in (result2["archived"] or {}).get("moved_paths", [])),
              "arsivde-script-YOK")

        # Ayni aralik tekrar: hicbir sey arsivlenmemeli.
        again = svc2.save_clip_bounds(start=8.0, end=22.0, actor="verify", slot=1)
        check(again["unchanged"] is True and again["archived"] is None,
              "AYNI-aralik-hicbir-sey-ARSIVLEMEZ")

        # Faz 0 momentine donus.
        svc2.reset_clip_bounds(actor="verify", slot=1)
        check(svc2.clip_bounds(1)["source_start_sec"] == 10.0,
              "momente-DONUS-calisiyor",
              str(svc2.clip_bounds(1)["source_start_sec"]))

        # Gecersiz aralik reddedilmeli ve hicbir sey arsivlememeli.
        before = svc2.clip_bounds(1)["source_start_sec"]
        try:
            svc2.save_clip_bounds(start=50.0, end=90.0, actor="verify", slot=1)
            check(False, "kaynak-disi-aralik-REDDEDILIR", "kabul edildi")
        except PipelineServiceError as exc:
            check(True, "kaynak-disi-aralik-REDDEDILIR", str(exc)[:50])
        check(svc2.clip_bounds(1)["source_start_sec"] == before,
              "REDDEDILEN-duzenleme-hicbir-sey-DEGISTIRMEDI")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------- 4. slot izolasyonu
def test_slot_isolation_and_api() -> None:
    section("4. Slot izolasyonu ve API")

    root = Path(tempfile.mkdtemp(prefix="clip_bounds_slots_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        pid, ownership, service = _project(app, "direct_clip_series", outputs=2)
        _fake_analysis(app, ownership, service, slot=1)
        _fake_analysis(app, ownership, service, slot=2)
        api = ConsoleApi(app)

        service.save_clip_bounds(start=8.0, end=18.0, actor="verify", slot=1)
        check(service.clip_bounds(2)["edited"] is False,
              "NEGATIF-slot-2-araligi-ETKILENMEDI")
        check(service.clip_bounds(2)["source_start_sec"] == 10.0,
              "slot-2-hala-MOMENT-araliginda")
        check(service.clip_bounds_relative(1) != service.clip_bounds_relative(2),
              "her-slotun-AYRI-dosyasi")

        status, payload = call(api, "GET", f"/api/projects/{pid}/clip-bounds?slot=2")
        check(status == 200 and payload["clip_bounds"]["available"] is True,
              "GET-clip-bounds-200", str(status))
        view = payload["clip_bounds"]
        check(view["source_duration_sec"] == 60.0,
              "gorunum-KAYNAK-SURESINI-tasiyor", str(view["source_duration_sec"]))
        check(service.source_duration_sec() == 60.0,
              "sure-SOURCE_ASSESSMENT'ten-okunuyor",
              str(service.source_duration_sec()))
        check(view["transcript_will_be_invalidated"] is True,
              "gorunum-transkriptin-DUSECEGINI-onceden-soyluyor")
        check(view["min_duration_sec"] == scene_editor.MIN_CLIP_DURATION_SEC,
              "gorunum-ASGARI-sureyi-tasiyor")

        status, _ = call(api, "POST", f"/api/projects/{pid}/clip-bounds",
                         {"action": "save", "slot": 2, "start": 5.0, "end": 5.05})
        check(status >= 400, "API-cok-kisa-klip-4xx", str(status))

        status, _ = call(api, "POST", f"/api/projects/{pid}/clip-bounds",
                         {"action": "save", "slot": 2, "start": "abc", "end": 9.0})
        check(status >= 400, "API-sayisal-olmayan-4xx", str(status))

        status, _ = call(api, "POST", f"/api/projects/{pid}/clip-bounds",
                         {"action": "save", "slot": 2, "end": 9.0})
        check(status >= 400, "API-eksik-alan-4xx", str(status))

        status, payload = call(api, "POST", f"/api/projects/{pid}/clip-bounds",
                               {"action": "save", "slot": 2, "start": 4.0, "end": 19.0})
        check(status == 200 and payload["clip_bounds"]["bounds"]["edited"] is True,
              "API-gecerli-duzenleme-200", str(status))
        check(service.clip_bounds(1)["source_start_sec"] == 8.0,
              "NEGATIF-slot-1-slot-2-kaydindan-ETKILENMEDI",
              str(service.clip_bounds(1)["source_start_sec"]))

        trail = "\n".join(json.dumps(e) for e in service.audit.events(limit=200))
        check("clip_bounds.saved" in trail, "audit-kaydi-YAZILDI")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    print("=" * 74)
    print("KLIP ARALIGI DOGRULAMA (sahte saglayicilar)")
    print("=" * 74)
    test_bounds_arithmetic()
    test_render_override()
    test_invalidation_matrix()
    test_slot_isolation_and_api()

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, "
          f"{len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        print("CLIP_BOUNDS_BASARISIZ")
        return 1
    print("CLIP_BOUNDS_OK (izole depolama, sahte saglayicilar; render "
          "calistirilmadi, gercek API cagrilmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
