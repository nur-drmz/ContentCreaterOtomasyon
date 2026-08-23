#!/usr/bin/env python
"""M0.0 — Telif ve donusturucu kullanim kapisi.

    python verify_copyright_gate.py

NE KANITLIYOR
-------------
Bu urun telifli film/dizi goruntusunden Short kesiyor. Muzigin lisans durumu,
stok klibin saglayicisi ve atfi vardi -- ama uzerine her seyin kuruldugu KAYNAK
videonun hicbir kontrolu yoktu. Operator bir diziyi bastan sona alip kesebilir,
altyazi gomup yukleme kapisina kadar gelebilirdi ve kimse ona bir dayanagi olup
olmadigini sormazdi.

Kapi Faz 0 plan onayinda: tek kare analiz edilmeden, tek saniye render
harcanmadan. En ucuz durma ani, is baslamadan onceki an.

Bu paket kapinin GERCEKTEN kapattigini kanitlar. Fixture'lar listeyi otomatik
dolduruyor (`fixtures.complete_copyright_review`), bu yuzden kapinin gercek
oldugu ancak burada -- fixture kisayolunu kullanmayan bir testte -- gorulebilir.

GUVENLIK
--------
Izole gecici depolama, sahte saglayicilar. Hicbir gercek API cagrilmaz.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import copyright_review, fixtures          # noqa: E402
from creator_console.api import ConsoleApi                      # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig       # noqa: E402
from creator_console.pipeline_service import PipelineServiceError  # noqa: E402

FAILURES: list = []
CHECKS = 0

PLAN_BODY = {
    "mode": "direct_clip_series", "selected_by": "verify_copyright",
    "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow",
                       "caption_theme": "auto"},
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


def raw_project(app, api, title):
    """A project WITHOUT the fixture shortcut, so the gate is really shut."""
    from creator_console.fixtures import make_test_source_video

    record = app.create_project(title=title, created_by="verify_copyright")
    ownership = app.ownership(record["project_id"])
    service = app.service(ownership)
    staging = app.storage.resolve(ownership, ".runtime", "src.mp4",
                                  create_parent=True)
    make_test_source_video(staging)
    service.register_source_video(filename="src.mp4", payload=staging.read_bytes(),
                                  registered_by="verify_copyright")
    staging.unlink(missing_ok=True)
    service.run_source_assessment(actor="verify_copyright")
    call(api, "POST", f"/api/projects/{ownership.project_id}/plan", PLAN_BODY)
    return ownership.project_id, service


def main() -> int:
    print("=" * 74)
    print("M0.0 TELIF KAPISI DOGRULAMA (sahte saglayicilar)")
    print("=" * 74)

    root = Path(tempfile.mkdtemp(prefix="copyright_gate_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        api = ConsoleApi(app)

        # ------------------------------------------------ the gate is shut
        section("1. Cevaplanmamis liste plan onayini KAPATIYOR")
        pid, service = raw_project(app, api, "Telif kapisi")
        state = service.copyright_review_state()
        check(state["total_count"] == len(copyright_review.CHECKLIST_KEYS),
              "liste-tam-uzunlukta", f"{state['total_count']} madde")
        check(state["done_count"] == 0 and not state["complete"],
              "yeni-proje-CEVAPLANMAMIS", str(state["done_count"]))
        check(state["source"]["display_name"] == "src.mp4",
              "liste-HANGI-kaynak-icin-soruldugunu-soyluyor",
              str(state["source"]))

        plan_hash = service.describe_plan_review()["review"]["content_sha256"]
        try:
            service.approve("processing_plan", reviewed_sha256=plan_hash,
                            approved_by="verify_copyright")
            check(False, "TELIFSIZ-plan-onayi-REDDEDILIR", "onay verildi")
        except PipelineServiceError as exc:
            check("ONAY REDDEDILDI" in str(exc),
                  "TELIFSIZ-plan-onayi-REDDEDILIR", str(exc)[:80])
        check(service.approval_state("processing_plan")["approved"] is False,
              "reddedilen-onay-artefakta-YAZILMADI")

        # Downstream must stay shut too: a refused plan opens no phase.
        status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "video_analysis",
            "params": {"whisper_model_size": "tiny", "clip_duration_sec": 4,
                       "top_n": 1}})
        job = (payload or {}).get("job") or {}
        check(job.get("status") == "failed", "onaysiz-planla-ANALIZ-calismiyor",
              str(job.get("status")))

        # -------------------------------------------- partial is not enough
        section("2. Yarim cevaplanmis liste de yetmiyor")
        service.set_copyright_review_item(
            copyright_review.CHECKLIST_KEYS[0], checked=True,
            reviewed_by="verify_copyright")
        state = service.copyright_review_state()
        check(state["done_count"] == 1 and not state["complete"],
              "bir-madde-YETMIYOR", f"{state['done_count']}/{state['total_count']}")
        try:
            service.approve("processing_plan", reviewed_sha256=plan_hash,
                            approved_by="verify_copyright")
            check(False, "YARIM-liste-onayi-REDDEDILIR", "onay verildi")
        except PipelineServiceError as exc:
            check("REDDEDILDI" in str(exc), "YARIM-liste-onayi-REDDEDILIR",
                  str(exc)[:70])

        # An unchecked item cannot be ticked anonymously: it is a human record.
        try:
            service.set_copyright_review_item(
                copyright_review.CHECKLIST_KEYS[1], checked=True, reviewed_by="")
            check(False, "isimsiz-isaretleme-REDDEDILIR", "kabul edildi")
        except Exception as exc:  # noqa: BLE001 - refusal is the outcome
            check(True, "isimsiz-isaretleme-REDDEDILIR", str(exc)[:60])

        # -------------------------------------------- completing opens it
        section("3. Liste tamamlaninca kapi aciliyor")
        for key in copyright_review.CHECKLIST_KEYS:
            service.set_copyright_review_item(key, checked=True,
                                              reviewed_by="verify_copyright")
        state = service.copyright_review_state()
        check(state["complete"] and not state["open_revision"],
              "liste-TAMAMLANDI", f"{state['done_count']}/{state['total_count']}")
        service.approve("processing_plan", reviewed_sha256=plan_hash,
                        approved_by="verify_copyright")
        check(service.approval_state("processing_plan")["approved"] is True,
              "tamamlanmis-listeyle-plan-ONAYLANIYOR")

        # ---------------------------------------- the answers are an artifact
        section("4. Cevaplar artefakt olarak kalici")
        artifact = service.load_artifact_path(
            copyright_review.COPYRIGHT_REVIEW_ARTIFACT)
        check(isinstance(artifact, dict), "telif-artefakti-diskte")
        check(artifact.get("artifact_type") == "copyright_review",
              "artefakt-turu-dogru", str(artifact.get("artifact_type")))
        entries = artifact.get("items") or artifact.get("entries") or {}
        blob = json.dumps(artifact)
        check("verify_copyright" in blob, "CEVAPLAYAN-kisi-kayitli")
        check(service.source_fingerprint()[:16] in blob,
              "cevaplar-KAYNAGA-bagli", "hangi kaynak icin cevaplandigi belli olmali")
        check(bool(entries), "madde-kayitlari-var", str(list(entries)[:3]))

        # ------------------------------------- changing the source resets it
        section("5. Kaynak degisince cevaplar SIFIRLANIYOR")
        from creator_console.fixtures import make_test_source_video
        staging = app.storage.resolve(app.ownership(pid), ".runtime", "other.mp4",
                                      create_parent=True)
        make_test_source_video(staging, seconds=4)
        service.register_source_video(filename="other.mp4",
                                      payload=staging.read_bytes(),
                                      registered_by="verify_copyright")
        staging.unlink(missing_ok=True)
        state = service.copyright_review_state()
        check(not state["complete"], "yeni-kaynakta-liste-GECERSIZ",
              f"{state['done_count']}/{state['total_count']} - {state.get('blocker')}")
        check("BASKA" in (state.get("blocker") or "").upper()
              or state["done_count"] == 0,
              "sifirlama-NEDENI-soyleniyor", str(state.get("blocker"))[:80])

        # --------------------------------------------- revision request path
        section("6. 'Revizyon gerekli' kaydi kapiyi kapali tutuyor")
        pid2, service2 = raw_project(app, api, "Telif revizyonu")
        for key in copyright_review.CHECKLIST_KEYS:
            service2.set_copyright_review_item(key, checked=True,
                                               reviewed_by="verify_copyright")
        service2.request_copyright_revision(
            requested_by="verify_copyright",
            notes="Kaynak uzerinde hak dayanagi netlesmedi", target_tab="phase0")
        state2 = service2.copyright_review_state()
        check(bool(state2["open_revision"]), "acik-revizyon-kaydi-var")
        hash2 = service2.describe_plan_review()["review"]["content_sha256"]
        try:
            service2.approve("processing_plan", reviewed_sha256=hash2,
                             approved_by="verify_copyright")
            check(False, "ACIK-REVIZYONLA-onay-REDDEDILIR", "onay verildi")
        except PipelineServiceError as exc:
            check("REDDEDILDI" in str(exc), "ACIK-REVIZYONLA-onay-REDDEDILIR",
                  str(exc)[:70])
        service2.clear_copyright_revision(cleared_by="verify_copyright")
        service2.approve("processing_plan", reviewed_sha256=hash2,
                         approved_by="verify_copyright")
        check(service2.approval_state("processing_plan")["approved"] is True,
              "revizyon-cozulunce-onay-ACILIYOR")

        # ------------------------------------------------ nothing is claimed
        section("7. Sistem hicbir sey iddia etmiyor")
        text = " ".join(item["question"] for item in copyright_review.CHECKLIST_ITEMS)
        for word in ("adil kullanimdir", "telifsizdir", "yasaldir", "guvenlidir"):
            check(word not in text.lower(), f"iddia-yok:{word}",
                  "sistem hukuki bir sonuca varmamali")
        check(all(item.get("fix_hint") for item in copyright_review.CHECKLIST_ITEMS),
              "her-madde-NE-YAPILACAGINI-soyluyor")

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
        print("COPYRIGHT_GATE_BASARISIZ")
        return 1
    print("COPYRIGHT_GATE_OK (izole depolama, sahte saglayicilar; hicbir gercek "
          "API cagrilmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
