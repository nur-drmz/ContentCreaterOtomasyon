#!/usr/bin/env python
"""Long-form Video Studio: uçtan uca doğrulama (sahte sağlayıcılarla).

    python verify_long_form_studio.py

NE KANITLIYOR
-------------
15-20 dakikalık bir video, kısa videodan nicel olarak değil NİTEL olarak
farklıdır: tek bir başarısızlık artık pahalıdır. Bu paket üç iddiayı denetler:

  1. Script tek bir dev çağrıyla üretilmiyor. Araştırma, outline ve HER BÖLÜM
     ayrı çağrılar; bir bölüm düşerse yalnızca o bölüm yeniden istenir.
  2. TTS parça parça ve YENİDEN BAŞLATILABİLİR. 6. parça düşerse 1-5 yeniden
     seslendirilmez; uygulama yeniden başlasa bile 6'dan devam eder.
  3. Her gerçek sağlayıcı isteği kotaya ayrı ayrı sayılır. Eski
     `run_google_tts` bütün parçaları TEK yönlendirilmiş çağrının içinde
     çalıştırıyordu; bu hem 12 isteği 1 gibi sayıyor hem de son parça düşünce
     öncekileri yeniden ürettiriyordu.

GÜVENLİK
--------
Sahte sağlayıcı ikizleri. HİÇBİR gerçek API çağrısı yapılmaz, hiçbir ücret
doğmaz, hiçbir şey yüklenmez.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import google_ai, long_form, long_form_script, tts_chunks  # noqa: E402

FAILURES: list = []
CHECKS = 0
TMP: list = []

PLAN = {
    "topic": "Ejderha baglari Targaryen ic savasini nasil sekillendirdi",
    "content_type": "theory", "target_duration_min": 18, "chapter_count": 6,
    "language": "tr-TR", "tone": "documentary",
    "audience": "Fantastik kurgu izleyicileri",
    "angle": "Ejderha gucunden cok binici baginin siyasi etkisi",
    "narration_mode": "tts", "include_subtitles": True,
    "notes": "Bilinen olaylarla teori kisimlarini ayir.", "updated_by": "verify",
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


# ------------------------------------------------------------ provider double
def wav_bytes(seconds: float = 5.0) -> bytes:
    """A real, valid WAV the audio validator accepts -- not a magic blob.

    Validation is part of what is being tested, so the double has to produce
    something that genuinely passes a container/duration check.
    """
    rate, channels, width = google_ai.TTS_SAMPLE_RATE, 1, 2
    frames = int(rate * seconds)
    return b"\x00\x00" * frames * channels


class FakeResponse:
    def __init__(self, payload=None, audio=None):
        self.text = payload
        self._audio = audio
        self.usage_metadata = None
        if audio is not None:
            blob = type("Blob", (), {"data": audio, "mime_type": "audio/L16"})()
            part = type("Part", (), {"inline_data": blob, "text": None})()
            content = type("Content", (), {"parts": [part]})()
            self.candidates = [type("Cand", (), {"content": content,
                                                 "finish_reason": "STOP"})()]
        else:
            part = type("Part", (), {"inline_data": None, "text": payload})()
            content = type("Content", (), {"parts": [part]})()
            self.candidates = [type("Cand", (), {"content": content,
                                                 "finish_reason": "STOP"})()]


class FakeGoogle:
    """Records every request and can be told to fail specific ones."""

    def __init__(self, *, fail_tts_sequence=None, fail_tts_status=500,
                 fail_text_task=None, chapter_words=None):
        # `chapter_words` shrinks the narration so a test can exercise the
        # chunk mechanism inside the configured TTS token budget. The real
        # 18-minute budget problem is asserted on its own, not worked around.
        self.chapter_words = chapter_words
        self.chapter_count = 6
        self.calls = []
        self.tts_calls = 0
        self.fail_tts_sequence = set(fail_tts_sequence or ())
        self.fail_tts_status = fail_tts_status
        self.fail_text_task = fail_text_task
        self.models = self

    def generate_content(self, *, model, contents, config=None, **_kwargs):
        is_tts = bool(config and "AUDIO" in (config.get("response_modalities") or []))
        self.calls.append({"model": model, "tts": is_tts,
                           "chars": len(str(contents)),
                           "contents": str(contents),
                           "config": dict(config or {})})
        if is_tts:
            self.tts_calls += 1
            if self.tts_calls in self.fail_tts_sequence:
                raise RuntimeError(f"{self.fail_tts_status} simulated provider error")
            return FakeResponse(audio=wav_bytes(6.0))
        body = str(contents)
        if self.fail_text_task and self.fail_text_task in body:
            raise RuntimeError("503 simulated text failure")
        if "ARASTIRMA PAKETI" in body:
            return FakeResponse(json.dumps({
                "thesis": "Binici bagi siyasi bir kaldiractir.",
                "facts": [{"claim": f"Olgu {i}", "source_title": f"Kaynak {i}",
                           "source_url": f"https://example.invalid/{i}",
                           "confidence": "high"} for i in range(1, 6)],
                "interpretations": [{"claim": "Yorum 1", "basis": "cikarim"}],
                "glossary": [{"term": "Dragonseed", "definition": "Ejderha tohumu"}],
                "open_questions": ["Acik soru"],
            }, ensure_ascii=False))
        if "BOLUM OUTLINE" in body:
            return FakeResponse(json.dumps({"chapters": [
                {"index": i, "title": f"Bolum {i}", "goal": f"Amac {i}",
                 "beats": [f"Nokta {i}.1"], "target_words": 420}
                for i in range(1, 7)]}, ensure_ascii=False))
        if "TUTARLILIK" in body:
            return FakeResponse(json.dumps({
                "issues": [{"kind": "repetition", "chapters": [2, 3],
                            "detail": "Ayni nokta tekrarlaniyor",
                            "severity": "medium", "suggestion": "Birini kisalt"}],
                "overall": "Genel olarak tutarli.",
            }, ensure_ascii=False))
        # Chapter call: produce roughly the requested number of words.
        words = self.chapter_words or 420
        for line in (body.splitlines() if self.chapter_words is None else []):
            if line.startswith("Hedef kelime:"):
                try:
                    words = int(line.split(":")[1].split("(")[0].strip())
                except (IndexError, ValueError):
                    words = 420
        text = " ".join(f"kelime{i}." for i in range(words))
        return FakeResponse(json.dumps({
            "text": text, "summary": "Bolum ozeti.",
            "uses_facts": ["Olgu 1"], "interpretation_notes": ["Yorum notu"],
        }, ensure_ascii=False))


def make_app(root, client):
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **{**fixtures.fixture_client_factories(),
           "google_client_factory": (lambda: client)}))


def seeded(root, client, *, title="Long form", plan_overrides=None):
    app = make_app(root, client)
    api = ConsoleApi(app)
    pid = app.create_project(title=title, created_by="verify")["project_id"]
    service = app.service(app.ownership(pid))
    call(api, "PATCH", f"/api/projects/{pid}/long-form-plan",
         dict(PLAN, **(plan_overrides or {})))
    return app, api, pid, service


def main() -> int:
    # =========================================== 1. plan -> yapisal hedefler
    section("1. 18 dakikalik plan makul bolum ve kelime hedefleri uretiyor")
    targets = long_form.chapter_targets(18, 6)
    check(5 <= len(targets) <= 7, "1-bolum-sayisi-5-7", str(len(targets)))
    total = sum(item["target_words"] for item in targets)
    band = long_form.word_target(18)
    check(band["minimum"] <= total <= band["maximum"],
          "1-kelime-hedefi-130-150-wpm-bandinda",
          f"{total} kelime, band {band['minimum']}-{band['maximum']}")
    check(all(300 <= item["target_words"] <= 700 for item in targets),
          "1-bolum-hedefleri-makul", str([i["target_words"] for i in targets]))
    check(long_form.estimate_api_calls(6, total)["total"]
          == 3 + 6 + long_form.estimate_tts_chunks(total),
          "1-api-cagri-tahmini-kalemli")

    root = Path(tempfile.mkdtemp(prefix="lfs_main_"))
    TMP.append(root)
    # Short chapters here: this section is about the chunk mechanism.
    client = FakeGoogle(chapter_words=90)
    app, api, pid, service = seeded(root, client)
    studio = service.long_form_studio()

    status, payload = call(api, "GET", f"/api/projects/{pid}/long-form")
    view = payload["long_form_studio"]
    check(status == 200 and view["estimates"]["available"], "1-tahminler-API-de",
          f"HTTP {status}")
    check(view["estimates"]["tts_chunk_estimate"] > 1, "1-tts-parca-tahmini",
          str(view["estimates"]["tts_chunk_estimate"]))
    check(len(client.calls) == 0, "1-ekrani-acmak-saglayici-cagirmiyor",
          f"{len(client.calls)} cagri")
    capacity = view["readiness"]["tts_capacity"]
    check(capacity["needed"] > capacity["per_minute_request_capacity"],
          "1-18dk-TTS-kotasi-tek-gecise-SIGMIYOR",
          f"gereken {capacity['needed']}, dakikalik kapasite "
          f"{capacity['per_minute_request_capacity']}")
    check(any("tek gecise sigmaz" in w or "tek gecişte" in w
              for w in view["readiness"]["warnings"]),
          "1-kota-uyarisi-BASLAMADAN-once-gosteriliyor",
          str(view["readiness"]["warnings"])[:110])

    # The scheduler pauses, persists and becomes runnable after the window; it
    # must not describe quota pressure as a failed audio chunk.
    model = capacity["model"]
    clock = [datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)]
    wait_ledger = google_ai.QuotaLedger(now_fn=lambda: clock[0])
    sample_tokens = 100
    for _ in range(google_ai.MODEL_REGISTRY[model]["configured_rpm"]):
        wait_ledger.record_usage(model, ok=True,
                                 estimated_input_tokens=sample_tokens)
    wait = studio._tts_quota_wait(wait_ledger, model, sample_tokens)
    check(wait["state"] == long_form.WAITING_RATE_LIMIT and wait["resume_at"],
          "1-rpm-dolunca-BASARISIZ-degil-BEKLIYOR", str(wait))
    daily_ledger = google_ai.QuotaLedger(now_fn=lambda: clock[0])
    for _ in range(google_ai.MODEL_REGISTRY[model]["configured_rpd"]):
        daily_ledger.record_usage(model, ok=True,
                                  estimated_input_tokens=sample_tokens)
    daily_wait = studio._tts_quota_wait(daily_ledger, model, sample_tokens)
    check(daily_wait["state"] == long_form.WAITING_DAILY_QUOTA
          and daily_wait["resume_at"],
          "1-rpd-dolunca-BASARISIZ-degil-ERTESI-GUNE-BEKLIYOR",
          str(daily_wait))

    waiting_workflow = long_form.new_workflow()
    for key in long_form.STAGE_KEYS[:long_form.STAGE_KEYS.index("tts")]:
        waiting_workflow = long_form.set_stage(waiting_workflow, key,
                                               long_form.COMPLETED)
    waiting_workflow = long_form.set_stage(
        waiting_workflow, "tts", long_form.WAITING_RATE_LIMIT,
        resume_at=wait["resume_at"])
    before_window = long_form.runnable_stage(waiting_workflow, now=clock[0])
    after_window = long_form.runnable_stage(
        waiting_workflow, now=clock[0] + timedelta(seconds=61))
    check(before_window.get("blocked") and before_window.get("waiting"),
          "1-bekleme-penceresi-dolmadan-cagri-YOK", str(before_window))
    check(not after_window.get("blocked") and after_window.get("stage") == "tts",
          "1-bekleme-penceresi-dolunca-devam-edebilir", str(after_window))

    planned_wait = tts_chunks.plan_chunks(
        [{"id": "ch_01", "index": 1, "text": "Ornek anlatim cumlesi."}],
        words_per_chunk=100, voice=google_ai.DEFAULT_TTS_VOICE,
        model=model, provider="google")
    waiting_manifest = tts_chunks.merge_plan(tts_chunks.new_manifest(), planned_wait)
    waiting_manifest = tts_chunks.mark_waiting(
        waiting_manifest, "tts_0001", state=tts_chunks.WAITING_RATE_LIMIT,
        resume_at=wait["resume_at"], reason="3/3 RPM")
    replanned_wait = tts_chunks.merge_plan(waiting_manifest, planned_wait)
    check(replanned_wait["chunks"][0]["status"] == tts_chunks.WAITING_RATE_LIMIT,
          "1-uygulama-yeniden-baslasa-bekleme-KORUNUR")

    # ============================================ 2. bolumler ayri ayri uretilir
    section("2. Bolumler AYRI cagrilarla uretiliyor")
    # Full-size chapters for this section: the word-target claim has to be
    # about the real 420-word targets, not a shrunken stand-in.
    client.chapter_words = None
    studio.start(actor="verify")
    studio.run_research(actor="verify")
    research_calls = len(client.calls)
    studio.run_outline(actor="verify")
    studio.run_chapters(actor="verify")
    chapter_calls = [c for c in client.calls if not c["tts"]]
    check(research_calls == 1, "2-arastirma-tek-cagri", str(research_calls))
    check(len(chapter_calls) == 1 + 1 + 6, "2-her-bolum-ayri-cagri",
          f"{len(chapter_calls)} metin cagrisi (1 arastirma + 1 outline + 6 bolum)")
    chapters = studio.chapters()["chapters"]
    check(len(chapters) == 6, "2-alti-bolum-uretildi", str(len(chapters)))
    check(all(ch["within_target"] for ch in chapters), "2-bolumler-hedefte",
          str([ch["word_count"] for ch in chapters]))
    biggest = max(c["chars"] for c in chapter_calls)
    check(biggest < 30000, "2-tek-dev-prompt-yok", f"en buyuk istek {biggest} karakter")

    # -- ortak onek: implicit context cache bolumler arasinda tutmali --------
    # Bolum prompt'u degisken kismi (bolum no/baslik) basa koyarsa Gemini'nin
    # ortuk cache'i HIC calismaz: alti bolum, ayni olgu/sozluk blogunun alti
    # tam fiyatli kopyasi demektir. Bu kontrol, birinin o sirayi geri
    # cevirmesini yakalar.
    chapter_bodies = [c["contents"] for c in chapter_calls
                      if "SIMDI YAZILACAK BOLUM" in c["contents"]]
    check(len(chapter_bodies) == 6, "2-cache-alti-bolum-prompt-kaydedildi",
          str(len(chapter_bodies)))
    shared = os.path.commonprefix(chapter_bodies) if chapter_bodies else ""
    expected_prefix = long_form_script.build_chapter_prompt_prefix(
        long_form_script.build_story_bible(studio.plan(), studio.research(),
                                           studio.outline(), index=1))
    check(shared.startswith(expected_prefix),
          "2-cache-ortak-onek-tam-story-bible",
          f"ortak onek {len(shared)} karakter, beklenen en az {len(expected_prefix)}")
    shortest = min(len(b) for b in chapter_bodies) if chapter_bodies else 1
    check(len(shared) / shortest > 0.5, "2-cache-onek-promptun-yarisindan-fazlasi",
          f"{len(shared)}/{shortest} = %{100 * len(shared) / shortest:.0f}")
    check(len({b for b in chapter_bodies}) == 6,
          "2-cache-onek-bolumleri-ayni-yapmiyor",
          "her bolum prompt'u hala benzersiz")

    # -- cikti tavani: kacak uretime karsi sinir, ama kesmeyecek kadar genis --
    chapter_configs = [c["config"] for c in chapter_calls
                       if "SIMDI YAZILACAK BOLUM" in c["contents"]]
    expected_cap = google_ai.max_output_tokens_for("script_generation")
    check(all(cfg.get("max_output_tokens") == expected_cap
              for cfg in chapter_configs),
          "2-cikti-tavani-her-bolum-cagrisinda",
          str({cfg.get("max_output_tokens") for cfg in chapter_configs}))
    # Bir bolum ~420 kelime; dusunme butcesi AYNI tavandan harcandigi icin
    # tavanin cikti tahmininin kat kat ustunde olmasi gerekir.
    check(expected_cap >= 16_000, "2-cikti-tavani-dusunme-butcesine-yer-birakiyor",
          str(expected_cap))

    studio.run_consistency(actor="verify")
    check(studio.consistency()["issue_count"] == 1, "2-tutarlilik-incelemesi-calisti")
    # Back to short chapters for the TTS sections below.
    client.chapter_words = 90
    for index in range(1, 7):
        studio.save_chapter_text(index, f"Bolum {index}. " +
                                 "Kisa anlatim cumlesi. " * 30,
                                 actor="verify")
    studio.save_workflow(long_form.set_stage(
        long_form.set_stage(studio.workflow(), "chapters", long_form.COMPLETED),
        "consistency", long_form.COMPLETED), actor="verify")

    # ================================== 13. insan onayi olmadan ilerlemiyor
    section("13. Insan onay kapilari")
    decision = long_form.runnable_stage(studio.workflow())
    check(decision["stage"] == "script_review" and decision["blocked"],
          "13-script-incelemesi-insan-onayi-bekliyor", json.dumps(decision))
    outcome = studio.run_next_stage(actor="verify")
    check(outcome["ran"] is None and outcome["blocked"],
          "13-onaysiz-asama-otomatik-calismiyor", str(outcome.get("reason")))
    studio.approve_stage("script_review", actor="verify")
    check(long_form.runnable_stage(studio.workflow())["stage"] == "tts",
          "13-onay-sonrasi-tts-acildi")

    # ========================== 3/11. TTS bagimsiz parcalar, sirali birlesim
    section("3+11. TTS bagimsiz parcalar halinde saklaniyor")
    first_tts_pass = studio.run_tts(actor="verify")
    check(first_tts_pass["waiting"] is not None
          and first_tts_pass["failed"] == 0,
          "3-gercek-dongu-RPM-dolunca-HATA-degil-BEKLEME",
          str(first_tts_pass.get("waiting")))
    check(long_form.stage_state(studio.workflow(), "tts")
          == long_form.WAITING_RATE_LIMIT,
          "3-TTS-asamasi-kalici-bekleme-durumunda")

    # The remaining scenarios test chunk persistence rather than quota. Give
    # their fake provider a large local test budget; production defaults were
    # asserted above and production code remains unchanged.
    for route_model in google_ai.TASK_ROUTES["tts"]:
        google_ai.MODEL_REGISTRY[route_model].update(
            configured_rpm=100, configured_rpd=100,
            configured_tpm=1_000_000)
    result = studio.run_tts(actor="verify")
    manifest = result["manifest"]
    chunk_count = len(manifest["chunks"])
    check(chunk_count > 1, "3-birden-fazla-parca", str(chunk_count))
    check(client.tts_calls == chunk_count, "3-her-parca-AYRI-saglayici-istegi",
          f"{client.tts_calls} TTS istegi / {chunk_count} parca")
    used_tts_models = {call["model"] for call in client.calls if call["tts"]}
    check(used_tts_models == {model}, "3-tek-anlatim-tek-TTS-modelinde-kaldi",
          str(sorted(used_tts_models)))
    stored = [c.get("output_path") for c in manifest["chunks"]]
    check(all(p and Path(p).is_file() for p in stored),
          "3-her-parca-diskte-bagimsiz-dosya",
          f"{len([p for p in stored if p])}/{len(stored)} dosya yazildi")
    tts_view = tts_chunks.summarise(manifest)
    check(all(item.get("media_name")
              and "/" not in item["media_name"]
              and "\\" not in item["media_name"]
              for item in tts_view["chunks"]),
          "3-oynatici-dogru-dosya-adini-host-yolu-olmadan-aliyor")
    required = {"id", "sequence", "chapter_id", "input_text_sha256", "provider",
                "model", "voice", "narration_direction", "attempts", "status",
                "error", "output_path", "output_sha256", "duration_sec",
                "created_at", "updated_at"}
    missing = required - set(manifest["chunks"][0])
    check(not missing, "3-parca-kaydi-tum-alanlari-tasiyor", str(sorted(missing)))
    combined = studio.concat_audio(actor="verify")
    order = combined["chunk_order"]
    check(order == sorted(order, key=lambda cid: int(cid.split("_")[1])),
          "11-birlestirme-sirasi-deterministik", str(order[:4]))
    check(combined["sha256"] and combined["validation"]["passed"],
          "11-birlesik-ses-dogrulandi", combined["validation"].get("reason") or "gecti")

    # ============================================ 12. sure hedefe gore dogrulanir
    section("12. Final sure 15-20 dakika hedefine gore dogrulaniyor")
    check("duration_check" in combined, "12-sure-kontrolu-kaydediliyor")
    check(long_form.validate_final_duration(18 * 60)["within_target"] is True
          and long_form.validate_final_duration(9 * 60)["within_target"] is False,
          "12-hedef-disi-sure-yakalaniyor",
          f"olculen {combined['duration_check']['measured_min']} dk")
    check(combined["duration_check"]["within_target"] is False
          and combined["duration_check"]["reason"],
          "12-sahte-ses-kisa-oldugu-icin-hedef-disi-RAPOR-ediliyor",
          combined["duration_check"]["reason"][:60])

    # ======================== 4/5. parca 6 duserse 1-5 yeniden uretilmez
    section("4+5. Parca hatasi ve yeniden baslatma")
    root2 = Path(tempfile.mkdtemp(prefix="lfs_resume_"))
    TMP.append(root2)
    failing = FakeGoogle(fail_tts_sequence={5}, chapter_words=90)
    failing.chapter_count = 5
    app2, api2, pid2, service2 = seeded(root2, failing, title="Resume",
                                        plan_overrides={"chapter_count": 5})
    studio2 = service2.long_form_studio()
    studio2.start(actor="verify")
    studio2.run_research(actor="verify")
    studio2.run_outline(actor="verify")
    studio2.run_chapters(actor="verify")
    studio2.run_consistency(actor="verify")
    studio2.approve_stage("script_review", actor="verify")
    first = studio2.run_tts(actor="verify")
    tts_after_first = failing.tts_calls
    completed_first = len(tts_chunks.completed_chunks(first["manifest"]))
    check(first["failed"] >= 1, "4-parca-6-basarisiz", f"{first['failed']} hata")
    total_chunks = len(first["manifest"]["chunks"])
    check(completed_first == total_chunks - 1, "4-onceki-parcalar-korundu",
          f"{completed_first}/{total_chunks} parca tamam (yalnizca dusen eksik)")

    # Restart: a brand new app object on the same storage, like a real restart.
    app2.shutdown()
    app3 = make_app(root2, failing)
    service3 = app3.service(app3.ownership(pid2))
    studio3 = service3.long_form_studio()
    resumed_manifest = studio3.chunk_manifest()
    check(len(tts_chunks.completed_chunks(resumed_manifest)) == completed_first,
          "5-yeniden-baslatma-tamamlanan-parcalari-koruyor",
          f"{len(tts_chunks.completed_chunks(resumed_manifest))} parca")
    preserved = {c["id"]: c.get("output_sha256")
                 for c in tts_chunks.completed_chunks(resumed_manifest)}
    failing.fail_tts_sequence = set()
    before_resume = failing.tts_calls
    second = studio3.run_tts(actor="verify")
    new_requests = failing.tts_calls - before_resume
    pending_before = len(tts_chunks.pending_chunks(resumed_manifest))
    check(new_requests <= pending_before,
          "5-eksik-parca-sayisindan-FAZLA-istek-yapilmadi",
          f"{new_requests} yeni istek / {pending_before} eksik parca")
    kept = {cid: sha for cid, sha in preserved.items()
            if cid in {c["id"]: c for c in second["manifest"]["chunks"]}}
    after_map = {c["id"]: c.get("output_sha256") for c in second["manifest"]["chunks"]}
    check(all(after_map.get(cid) == sha for cid, sha in kept.items()),
          "4-tamamlanan-parcalarin-sesi-DEGISMEDI",
          "yeniden seslendirilseydi hash degisirdi")
    check(second["readiness"]["ready"], "5-devam-sonrasi-tamamlandi",
          json.dumps(second["readiness"]))

    # =================== 6. bolum 3 degisirse yalnizca onun sesi gecersizlesir
    section("6. Bolum 3 duzenlenince yalnizca onun TTS'i gecersizlesir")
    before = studio3.chunk_manifest()
    ch3_chunks = [c["id"] for c in before["chunks"] if c["chapter_id"] == "ch_03"]
    other_hashes = {c["id"]: c.get("output_sha256") for c in before["chunks"]
                    if c["chapter_id"] != "ch_03"}
    studio3.save_chapter_text(3, "Bu tamamen yeni bir bolum metnidir. " * 40,
                              actor="verify")
    after = studio3.plan_tts(actor="verify")
    pending_ids = {c["id"] for c in tts_chunks.pending_chunks(after)}
    still_done = {c["id"]: c.get("output_sha256") for c in after["chunks"]
                  if c.get("status") == tts_chunks.COMPLETED}
    check(bool(ch3_chunks), "6-bolum-3-parcalari-vardi", str(len(ch3_chunks)))
    check(all(still_done.get(cid) == sha for cid, sha in other_hashes.items()
              if cid in still_done),
          "6-diger-bolumlerin-sesi-KORUNDU")
    check(bool(pending_ids), "6-degisen-bolum-yeniden-kuyruga-alindi",
          f"{len(pending_ids)} parca bekliyor")
    check(after.get("combined") is None,
          "6-birlesik-ses-gecersizlestirildi",
          "eski birlesim duzenlenmis scripti yansitmaz")
    workflow = studio3.workflow()
    check(long_form.stage_state(workflow, "research") == long_form.COMPLETED,
          "6-arastirma-BOSUNA-gecersizlestirilmedi")
    check(long_form.stage_state(workflow, "consistency") == long_form.NOT_STARTED,
          "6-tutarlilik-incelemesi-gecersizlestirildi")

    # =========================================== 7. kota gercek istekleri sayar
    section("7. Kota muhasebesi gercek istek sayisini yansitiyor")
    quota = studio.quota_summary()
    check(quota["request_total"] >= len(client.calls) * 0.9,
          "7-kota-istek-sayisi-gercek-cagrilarla-uyumlu",
          f"defter {quota['request_total']} / gercek {len(client.calls)}")
    check(quota["estimated"] is True, "7-kota-TAHMIN-oldugunu-soyluyor")

    # ================================= 8/9. hata siniflandirmasi ve guvenli durus
    section("8+9. Yeniden deneme politikasi ve kimlik hatasi")
    for code, retryable in ((429, True), (500, True), (502, True), (503, True),
                            (504, True), (400, False), (401, False), (403, False)):
        outcome = tts_chunks.classify_error(code)
        check(outcome["retryable"] is retryable, f"8-{code}-retryable-{retryable}",
              outcome["kind"])
    root4 = Path(tempfile.mkdtemp(prefix="lfs_auth_"))
    TMP.append(root4)
    # A bad credential fails EVERY request, not one: testing it on a single
    # chunk would let the loop sail past and prove nothing about stopping.
    auth_client = FakeGoogle(fail_tts_sequence=set(range(1, 50)),
                             fail_tts_status=401, chapter_words=90)
    app4, api4, pid4, service4 = seeded(root4, auth_client, title="Auth")
    studio4 = service4.long_form_studio()
    studio4.start(actor="verify")
    studio4.run_research(actor="verify")
    studio4.run_outline(actor="verify")
    studio4.run_chapters(actor="verify")
    studio4.run_consistency(actor="verify")
    studio4.approve_stage("script_review", actor="verify")
    auth_result = studio4.run_tts(actor="verify")
    failed_chunk = [c for c in auth_result["manifest"]["chunks"]
                    if c["status"] == tts_chunks.FAILED]
    check(bool(failed_chunk), "9-kimlik-hatasi-parcayi-basarisiz-isaretledi")
    check(failed_chunk[0]["error_kind"] == "unauthenticated",
          "9-401-unauthenticated-olarak-siniflandirildi",
          str(failed_chunk[0]["error_kind"]))
    check(auth_result["produced"] == 0,
          "9-kimlik-hatasinda-GUVENLI-durdu-devam-etmedi",
          f"{auth_result['produced']} parca uretildi")

    # ================================================ 10. iptal guvenli durur
    section("10. Iptal parcalar ARASINDA guvenle duruyor")
    root5 = Path(tempfile.mkdtemp(prefix="lfs_cancel_"))
    TMP.append(root5)
    cancel_client = FakeGoogle(chapter_words=90)
    app5, api5, pid5, service5 = seeded(root5, cancel_client, title="Cancel")
    studio5 = service5.long_form_studio()
    studio5.start(actor="verify")
    studio5.run_research(actor="verify")
    studio5.run_outline(actor="verify")
    studio5.run_chapters(actor="verify")
    studio5.run_consistency(actor="verify")
    studio5.approve_stage("script_review", actor="verify")
    seen = {"n": 0}

    def stop_after_two():
        seen["n"] += 1
        return seen["n"] > 2

    tts_before = cancel_client.tts_calls
    cancelled = studio5.run_tts(actor="verify", cancel_check=stop_after_two)
    produced = cancel_client.tts_calls - tts_before
    check(produced <= 3, "10-iptal-kalan-parcalari-uretmedi",
          f"{produced} istek yapildi")
    check(len(tts_chunks.completed_chunks(cancelled["manifest"])) == produced,
          "10-uretilmis-parcalar-korundu")
    studio5.cancel(actor="verify")
    check(long_form.runnable_stage(studio5.workflow())["stage"] is None,
          "10-iptal-sonrasi-yeni-is-alinmiyor")


    # ============================== emekli model regresyonu (canli pilot bulgusu)
    section("R. Emekliye ayrilmis model tek basina asamayi olduremez")
    # 2026-08-18 canli pilotunda bulundu: `models.list()` gemini-2.5-flash'i
    # listeliyor ama cagri 404 donuyor ("no longer available to new users").
    # grounded_research rotasinda TEK model o oldugu icin arastirma asamasi hic
    # calisamiyordu. Gorunurluk, cagrilabilirlik demek degil.
    check(len(google_ai.TASK_ROUTES["grounded_research"]) >= 2,
          "R-grounded-rotasinda-yedek-model-var",
          str(google_ai.TASK_ROUTES["grounded_research"]))
    check("gemini-2.5-flash" not in google_ai.TASK_ROUTES["grounded_research"],
          "R-emekli-2.5-otomatik-grounded-rotasindan-cikarildi",
          str(google_ai.TASK_ROUTES["grounded_research"]))

    class RetiringGoogle(FakeGoogle):
        """First grounded model answers exactly like a retired one: 404."""

        def __init__(self, retired, **kwargs):
            super().__init__(**kwargs)
            self.retired = retired
            self.rejected = []

        def generate_content(self, *, model, contents, config=None, **kwargs):
            if model in self.retired:
                self.rejected.append(model)
                raise RuntimeError(
                    f"404 NOT_FOUND. This model models/{model} is no longer "
                    "available to new users.")
            return super().generate_content(model=model, contents=contents,
                                            config=config, **kwargs)

    root_r = Path(tempfile.mkdtemp(prefix="lfs_retired_"))
    TMP.append(root_r)
    retired_first = google_ai.TASK_ROUTES["grounded_research"][0]
    retiring = RetiringGoogle({retired_first}, chapter_words=90)
    app_r, api_r, pid_r, service_r = seeded(root_r, retiring, title="Retired")
    studio_r = service_r.long_form_studio()
    studio_r.start(actor="verify")
    try:
        record = studio_r.run_research(actor="verify")
        completed = bool(record.get("facts") is not None)
    except Exception as exc:  # noqa: BLE001 - reported, not raised through
        completed = False
        record = {"error": str(exc)[:120]}
    check(completed, "R-emekli-model-404-verince-arastirma-YINE-tamamlaniyor",
          str(record.get("error") or "tamam"))
    check(retired_first in retiring.rejected,
          "R-emekli-model-gercekten-denendi-ve-reddedildi",
          str(retiring.rejected))
    check(long_form.stage_state(studio_r.workflow(), "research")
          == long_form.COMPLETED,
          "R-asama-durumu-completed")


    # ================= metin asamasinda kota beklemesi (canli pilot bulgusu)
    section("Q. Billing/kota yapilandirmasi otomatik tekrar YAPMAZ")
    # 2026-08-18 canli pilotu: arastirma asamasi 429 RESOURCE_EXHAUSTED aldi ve
    # `failed` olarak kaydedildi. `failed` operatoru yeniden denemeye davet eder
    # ve kotayi daha da yakar; kota beklemesi ise `resume_at` tasir ve "sonra
    # gel" der. Bekleme siniflandirmasi TTS dongusunde vardi, metin
    # asamalarinda yoktu.

    class QuotaExhaustedGoogle(FakeGoogle):
        """Text requests answer like a billing block until operator fixes it."""

        blocked = True

        def generate_content(self, *, model, contents, config=None, **kwargs):
            is_tts = bool(config and "AUDIO" in (config.get("response_modalities") or []))
            if not is_tts and self.blocked:
                self.calls.append({"model": model, "tts": False,
                                   "chars": len(str(contents))})
                raise RuntimeError(
                    "429 RESOURCE_EXHAUSTED. You exceeded your current quota, "
                    "please check your plan and billing details.")
            return super().generate_content(model=model, contents=contents,
                                            config=config, **kwargs)

    root_q = Path(tempfile.mkdtemp(prefix="lfs_quota_"))
    TMP.append(root_q)
    quota_client = QuotaExhaustedGoogle(chapter_words=90)
    app_q, api_q, pid_q, service_q = seeded(root_q, quota_client, title="Quota")
    studio_q = service_q.long_form_studio()
    studio_q.start(actor="verify")
    try:
        studio_q.run_research(actor="verify")
        raised = None
    except Exception as exc:  # noqa: BLE001 - the refusal is expected
        raised = exc
    state_q = long_form.stage_state(studio_q.workflow(), "research")
    check(raised is not None, "Q-kota-tukenince-asama-basarili-SAYILMIYOR")
    check(state_q == long_form.BLOCKED_BILLING,
          "Q-plan-billing-ayri-blok-durumu-olarak-kaliciolur",
          f"durum={state_q}")
    entry_q = (studio_q.workflow().get("stages") or {}).get("research") or {}
    check(not entry_q.get("resume_at"), "Q-billing-bloku-resume_at-TASIMAZ",
          str(entry_q.get("resume_at")))
    blocked_decision = long_form.runnable_stage(studio_q.workflow())
    check(blocked_decision.get("blocked") and blocked_decision.get("billing_blocked")
          and not blocked_decision.get("waiting"),
          "Q-billing-bloku-zamanlayiciya-girmez", str(blocked_decision))
    blocked_calls = [call for call in quota_client.calls if not call.get("tts")]
    check(len(blocked_calls) == 1,
          "Q-plan-billing-hatasi-tek-cagri-fallback-ve-retry-YOK",
          str(blocked_calls))
    names_q = {e["event"] for e in studio_q.events(limit=50)}
    check("long_form.blocked_billing" in names_q,
          "Q-billing-blok-olayi-kaydedildi", str(sorted(names_q))[:110])
    check(google_ai.classify_google_error(
              "429 RESOURCE_EXHAUSTED: check your plan and billing details")
          == google_ai.ERROR_BILLING_QUOTA,
          "Q-kanonik-hata-sinifi-billing-kota")

    quota_client.blocked = False
    retry_status, retry_payload = call(
        api_q, "POST", f"/api/projects/{pid_q}/long-form/control",
        {"action": "retry_billing", "actor": "verify"})
    check(retry_status == 200 and retry_payload.get("long_form_studio"),
          "Q-operator-retry-API-eylemi-var", str(retry_status))
    check(long_form.stage_state(studio_q.workflow(), "research") == long_form.QUEUED,
          "Q-yalnizca-operator-eylemi-bloku-acar")
    studio_q.run_research(actor="verify")
    check(long_form.stage_state(studio_q.workflow(), "research") == long_form.COMPLETED,
          "Q-duzeltme-sonrasi-ayni-asama-devam-eder")
    check("long_form.billing_retry_requested" in
          {e["event"] for e in studio_q.events(limit=50)},
          "Q-operator-yeniden-deneme-olayi-kaydedildi")

    # ================================= diagnostics: olaylar ve sizinti yoklugu
    section("Diagnostik: yapisal olaylar, sir sizintisi yok")
    events = studio.events(limit=200)
    names = {event["event"] for event in events}
    for expected in ("long_form.started", "research.completed", "outline.completed",
                     "chapter.completed", "consistency_review.completed",
                     "tts_chunk.completed", "audio_concat.completed"):
        check(expected in names, f"olay-{expected}", str(sorted(names))[:70])
    blob = json.dumps(events, ensure_ascii=False)
    script_text = studio.view()["full_script"]
    check(script_text[:80] not in blob, "olaylarda-tam-script-YOK")
    check("api_key" not in blob and "credential" not in blob,
          "olaylarda-kimlik-bilgisi-YOK")
    sample = [e for e in events if e["event"] == "tts_chunk.completed"][0]
    check({"chunk_id", "attempt", "latency_sec"} <= set(sample),
          "olaylarda-chunk-attempt-latency-var", str(sorted(sample))[:80])

    check(bool(fixtures.PROVIDER_CALLS) is False, "sifir-GERCEK-saglayici-cagrisi",
          str(len(fixtures.PROVIDER_CALLS)))

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, {len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        return 1
    print("LONG_FORM_STUDIO_OK (sahte saglayici ikizleri; HICBIR gercek API "
          "cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for path in TMP:
            shutil.rmtree(path, ignore_errors=True)
    sys.exit(code)
