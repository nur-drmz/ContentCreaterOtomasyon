"""Turbo Studio acceptance checks. No real provider is ever contacted.

Every provider client is a fixture twin and `fixtures.PROVIDER_CALLS` records
what a real one WOULD have been asked, so "opening the tab spends nothing" is
checked against an observation rather than against a hope.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_pipeline import turbo                      # noqa: E402
from local_pipeline import stage_machine              # noqa: E402
from local_pipeline import render_utils              # noqa: E402

CHECKS = 0
FAILURES = 0


def check(condition, label, message=""):
    global CHECKS, FAILURES
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
    else:
        FAILURES += 1
        print(f"  [{label}] BASARISIZ" + (f" - {message}" if message else ""))


def section(title):
    print(f"\n--- {title} ---")


# ===================================================================== A
def test_stage_machine():
    section("A. Asama makinesi ve ayristirma")

    check(len(turbo.STAGES) == 14, "14-asama", str(len(turbo.STAGES)))
    check(len(turbo.AUTOMATIC_STAGES) == 7 and len(turbo.HUMAN_STAGES) == 7,
          "7-otomatik-7-kapi",
          f"{len(turbo.AUTOMATIC_STAGES)}/{len(turbo.HUMAN_STAGES)}")
    check("final_render" not in turbo.AUTOMATIC_STAGES,
          "final-render-kendiliginden-calismaz",
          "onizleme bitince final otomatik baslamamali")

    # The extraction must not have left long-form sharing Turbo's stage list.
    from local_pipeline import long_form
    check(long_form.STAGE_KEYS != turbo.STAGE_KEYS
          and len(long_form.STAGE_KEYS) == 12,
          "long-form-kendi-listesini-korudu",
          f"{len(long_form.STAGE_KEYS)} asama")
    try:
        long_form.set_stage(long_form.new_workflow(), "media_plan", "completed")
        leaked = True
    except long_form.LongFormError:
        leaked = False
    check(not leaked, "asama-listeleri-sizmiyor",
          "long_form Turbo asamasini kabul etmemeli")

    # A machine may not advance past a human gate.
    record = turbo.new_workflow()
    record = turbo.set_stage(record, "script", turbo.COMPLETED)
    decision = turbo.runnable_stage(record)
    check(decision["stage"] == "script_review" and decision["blocked"],
          "insan-kapisi-yuruyusu-durdurur", decision["reason"])

    # Waiting is not failing.
    from datetime import datetime, timedelta, timezone
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    waiting = turbo.set_stage(turbo.new_workflow(), "script",
                              turbo.WAITING_RATE_LIMIT, resume_at=later)
    decision = turbo.runnable_stage(waiting)
    check(decision.get("waiting") and decision.get("wait_seconds", 0) > 0,
          "kota-beklemesi-hata-degil",
          f"{decision.get('wait_seconds')}s sonra devam")
    check(turbo.overall_status(waiting) == turbo.WAITING_RATE_LIMIT,
          "genel-durum-beklemede", turbo.overall_status(waiting))


def test_brief_validation():
    section("B. Brief dogrulama")

    ok = turbo.validate_brief({"topic": "Roma imparatorlugunun cokusu",
                               "duration_sec": 60})
    check(ok["width"] == 1080 and ok["height"] == 1920,
          "9:16-1080p", f"{ok['width']}x{ok['height']}")
    check(ok["estimates"]["clip_count"] == 12,
          "klip-sayisi-tahmini", str(ok["estimates"]["clip_count"]))
    check(len(ok["brief_sha256"]) == 64, "brief-hash-uretildi")

    # A cosmetic edit must not invalidate work.
    same = turbo.validate_brief({"topic": "Roma imparatorlugunun cokusu",
                                 "duration_sec": 60, "notes": "sonradan not"})
    check(same["brief_sha256"] == ok["brief_sha256"],
          "kozmetik-duzenleme-hash-degistirmez")
    other = turbo.validate_brief({"topic": "Roma imparatorlugunun cokusu",
                                  "duration_sec": 90})
    check(other["brief_sha256"] != ok["brief_sha256"],
          "anlamli-degisiklik-hash-degistirir")

    rejects = [
        ({"topic": "kisa", "duration_sec": 60}, "konu", "kisa-konu-reddedilir"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 5},
         "sure", "kisa-sure-reddedilir"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 99999},
         "sure", "uzun-sure-reddedilir"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 60,
          "aspect_ratio": "4:3"}, "en-boy", "bilinmeyen-oran-reddedilir"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 60,
          "clip_duration_sec": 0.5}, "klip suresi", "kisa-klip-reddedilir"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 1500,
          "clip_duration_sec": 2.0}, "klip", "klip-sayisi-tavani"),
        ({"topic": "yeterince uzun bir konu", "duration_sec": 60,
          "transition": "fade", "transition_sec": 9.0},
         "gecis", "asiri-gecis-suresi-reddedilir"),
    ]
    for payload, needle, label in rejects:
        try:
            turbo.validate_brief(payload)
            check(False, label, "kabul edildi, reddedilmeliydi")
        except turbo.TurboError as exc:
            check(needle in str(exc).lower(), label, str(exc)[:70])


def test_revision_matrix():
    section("C. Revizyon -> gecersizlestirme matrisi")

    # The claim that costs the most if wrong: re-recording one narration chunk
    # must not discard footage that is already downloaded.
    effect = turbo.revision_effect("narration_chunk")
    for preserved in ("media_plan", "media", "script"):
        check(preserved in effect["preserved_stages"],
              f"anlatim-yenilemesi-{preserved}-korur",
              "indirilmis klipler korunmali")
    for stale in ("audio_concat", "subtitles", "timeline", "preview_render"):
        check(stale in effect["stale_stages"],
              f"anlatim-yenilemesi-{stale}-bayatlatir")

    # `subtitle_style` is deliberately NOT in this group any more: the caption
    # look now lives in the ASS file's own styles, so changing the theme means
    # regenerating the subtitles, not re-rendering with a force_style patched
    # over the top.
    for kind in ("frame", "retime", "music_volume", "music_remove"):
        effect = turbo.revision_effect(kind)
        check(effect["stale_stages"] == turbo.RENDER_STAGES,
              f"{kind}-yalnizca-render-bayatlatir",
              str(effect["stale_stages"]))
        check(effect["manifest_only"], f"{kind}-manifest-ici")
        check("media" in effect["preserved_stages"]
              and "tts" in effect["preserved_stages"],
              f"{kind}-indirmeleri-ve-sesi-korur")

    effect = turbo.revision_effect("subtitle_style")
    check("subtitles" in effect["stale_stages"],
          "tema-degisimi-ALTYAZIYI-bayatlatir",
          "tema degisip ASS yeniden uretilmezse eski gorunum yakilir")
    check(not effect["manifest_only"], "tema-degisimi-manifest-ici-DEGIL",
          str(effect))
    check("media" in effect["preserved_stages"]
          and "tts" in effect["preserved_stages"],
          "tema-degisimi-indirmeleri-ve-sesi-korur",
          str(effect["preserved_stages"]))

    effect = turbo.revision_effect("replace_clip")
    check("tts" in effect["preserved_stages"]
          and "subtitles" in effect["preserved_stages"]
          and "media_plan" in effect["preserved_stages"],
          "klip-degisimi-ses-altyazi-plan-korur")
    check("timeline" in effect["stale_stages"],
          "klip-degisimi-timeline-bayatlatir",
          "yeni klibin kaynak suresi tekrar kullanimi degistirir")

    effect = turbo.revision_effect("script_edit")
    check(set(effect["stale_stages"]) >= {"tts", "media_plan", "media",
                                          "timeline"},
          "script-duzenlemesi-genis-bayatlatir")

    try:
        turbo.revision_effect("uydurma")
        check(False, "bilinmeyen-revizyon-reddedilir")
    except turbo.TurboError:
        check(True, "bilinmeyen-revizyon-reddedilir")

    # Graph closure, not position: `music` has no inputs so nothing upstream
    # can make it stale.
    check("music" not in turbo.stale_stages(["script"]),
          "muzik-scriptten-etkilenmez")
    check(turbo.stale_stages([]) == (), "degisiklik-yoksa-bayatlama-yok")


# ===================================================================== D
def make_app(root):
    from creator_console import fixtures
    from creator_console.app import ConsoleApp, ConsoleConfig
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))


def call(api, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers={})


def test_console_integration():
    section("D. Konsol entegrasyonu (sifir saglayici cagrisi)")

    from creator_console import fixtures
    from creator_console.api import ConsoleApi

    root = Path(tempfile.mkdtemp(prefix="turbo_verify_"))
    try:
        app = make_app(root)
        api = ConsoleApi(app)
        pid = app.create_project(title="Turbo", created_by="verify")["project_id"]
        base = f"/api/projects/{pid}"

        fixtures.PROVIDER_CALLS.clear()
        before = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}

        status, payload = call(api, "GET", f"{base}/turbo")
        view = payload["turbo_studio"]
        after = {p.relative_to(root) for p in root.rglob("*") if p.is_file()}

        check(status == 200 and "progress" in view, "turbo-gorunumu-aciliyor",
              str(status))
        check(list(fixtures.PROVIDER_CALLS) == [],
              "sekme-acilisi-SIFIR-saglayici-cagrisi",
              f"{len(fixtures.PROVIDER_CALLS)} cagri")
        check(after == before, "sekme-acilisi-dosya-yaratmaz",
              f"yeni: {sorted(str(p) for p in (after - before))[:3]}")
        check(view["started"] is False and view["brief"] is None,
              "brief-yokken-baslamamis-gorunur")
        check(view["progress"]["percent"] == 0
              and view["progress"]["total_count"] == 14,
              "ilerleme-sifirdan-baslar")

        status, _ = call(api, "POST", f"{base}/turbo/brief",
                         {"topic": "kisa", "duration_sec": 60})
        check(status == 400, "gecersiz-brief-400", str(status))
        check(call(api, "GET", f"{base}/turbo")[1]["turbo_studio"]["started"]
              is False, "reddedilen-brief-is-akisi-yaratmaz")

        status, payload = call(api, "POST", f"{base}/turbo/brief",
                               {"topic": "Roma imparatorlugunun cokusu",
                                "duration_sec": 60, "aspect_ratio": "9:16"})
        view = payload["turbo_studio"]
        check(status == 200 and view["started"], "brief-kaydedildi", str(status))
        check(view["brief"]["width"] == 1080 and view["brief"]["height"] == 1920,
              "uretim-cozunurlugu-1080p",
              f"{view['brief']['width']}x{view['brief']['height']}")
        check(list(fixtures.PROVIDER_CALLS) == [],
              "brief-kaydi-da-saglayici-cagirmaz",
              f"{len(fixtures.PROVIDER_CALLS)} cagri")

        status, _ = call(api, "POST", f"{base}/turbo/approve",
                         {"stage": "timeline_review"})
        check(status == 400, "sirasiz-onay-reddedilir", str(status))
        status, _ = call(api, "POST", f"{base}/turbo/approve", {"stage": "tts"})
        check(status == 400, "otomatik-asama-elle-onaylanamaz", str(status))
        status, payload = call(api, "POST", f"{base}/turbo/approve",
                               {"stage": "script"})
        check(status == 400, "tamamlanmamis-onceki-asama-onayi-engeller",
              str(status))

        # Restart resumability: a brand-new app over the same storage root.
        api2 = ConsoleApi(make_app(root))
        view2 = call(api2, "GET", f"{base}/turbo")[1]["turbo_studio"]
        check(view2["brief"]["brief_sha256"] == view["brief"]["brief_sha256"],
              "yeniden-baslatmada-brief-korunur")
        check(view2["progress"]["next"]["stage"] == "script",
              "yeniden-baslatmada-dogru-asamadan-devam",
              str(view2["progress"]["next"]["stage"]))

        status, payload = call(api, "POST", f"{base}/turbo/brief",
                               {"topic": "Roma imparatorlugunun cokusu",
                                "duration_sec": 60, "aspect_ratio": "9:16",
                                "notes": "kozmetik"})
        check(payload["saved"] is False,
              "kozmetik-duzenleme-gecersizlestirmez")
        status, payload = call(api, "POST", f"{base}/turbo/brief",
                               {"topic": "Roma imparatorlugunun cokusu",
                                "duration_sec": 120, "aspect_ratio": "16:9"})
        check(payload["saved"] is True, "anlamli-degisiklik-gecersizlestirir")
        check(payload["turbo_studio"]["brief"]["width"] == 1920,
              "16:9-genisligi", str(payload["turbo_studio"]["brief"]["width"]))

        blob = json.dumps(payload, ensure_ascii=False)
        check("fake-" not in blob and "api_key" not in blob,
              "yanitta-kimlik-bilgisi-yok")
        check(str(root) not in blob and str(root).replace("\\", "/") not in blob,
              "yanitta-yerel-mutlak-yol-yok")

        fixtures.PROVIDER_CALLS.clear()
        status, payload = call(
            api, "GET", f"{base}/turbo/revision-preview?kind=narration_chunk")
        check(status == 200 and "media" in payload["effect"]["preserved_stages"],
              "revizyon-onizlemesi-korunanlari-bildirir", str(status))
        check(list(fixtures.PROVIDER_CALLS) == [],
              "revizyon-onizlemesi-saglayici-cagirmaz")

        events = call(api, "GET", f"{base}/turbo")[1]["turbo_studio"]["events"]
        names = sorted({e["event"] for e in events})
        check("turbo.brief_saved" in names, "brief-olayi-kaydedildi", str(names))
        check("turbo.brief_rejected" in names, "red-olayi-da-kaydedildi")
        blob = json.dumps(events, ensure_ascii=False)
        check("fake-" not in blob and "Roma" not in blob,
              "olaylarda-sir-ve-serbest-metin-yok")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===================================================================== F
def wav_bytes(seconds: float = 4.0) -> bytes:
    """A real, valid WAV body -- validation is part of what is being tested."""
    from local_pipeline import google_ai
    return b"\x00\x00" * int(google_ai.TTS_SAMPLE_RATE * seconds)


class FakeResponse:
    def __init__(self, payload=None, audio=None):
        self.text = payload
        self.usage_metadata = None
        if audio is not None:
            blob = type("Blob", (), {"data": audio, "mime_type": "audio/L16"})()
            part = type("Part", (), {"inline_data": blob, "text": None})()
        else:
            part = type("Part", (), {"inline_data": None, "text": payload})()
        content = type("Content", (), {"parts": [part]})()
        self.candidates = [type("Cand", (), {"content": content,
                                             "finish_reason": "STOP"})()]


class FakeGoogle:
    """Records every request and can be told to fail specific ones."""

    def __init__(self, *, beats=4, fail_tts_sequence=None, tts_error=None,
                 words_per_beat=9):
        # Narration length is what decides the CHUNK count, so a test that
        # exercises multi-chunk behaviour has to produce narration long enough
        # to actually be split. `beats` alone no longer implies chunks.
        self.beats = beats
        self.words_per_beat = words_per_beat
        self.calls = []
        self.tts_calls = 0
        self.text_calls = 0
        self.fail_tts_sequence = set(fail_tts_sequence or ())
        self.tts_error = tts_error or "500 simulated provider error"
        self.models = self

    def generate_content(self, *, model, contents, config=None, **_kwargs):
        is_tts = bool(config and "AUDIO" in (config.get("response_modalities") or []))
        self.calls.append({"model": model, "tts": is_tts})
        if is_tts:
            self.tts_calls += 1
            if self.tts_calls in self.fail_tts_sequence:
                raise RuntimeError(self.tts_error)
            return FakeResponse(audio=wav_bytes(4.0))
        self.text_calls += 1
        filler = " ".join(["anlatim"] * max(0, self.words_per_beat - 8))
        sentences = [
            f"Bu {i}. sahnenin anlatim cumlesidir ve yeterince uzundur {filler}."
            for i in range(1, self.beats + 1)]
        return FakeResponse(json.dumps({
            "hook": "Roma neden coktu?",
            "narration": " ".join(sentences),
            "scene_beats": [
                {"index": i + 1, "text": sentences[i],
                 "search_query": f"ancient roman scene {i + 1}",
                 "keywords": ["roma", "tarih"]}
                for i in range(self.beats)],
        }, ensure_ascii=False))


def turbo_app(root, client, *, stock_keys=False):
    from creator_console import fixtures
    from creator_console.app import ConsoleApp, ConsoleConfig
    base = fixtures.fake_secret_getter()

    def secrets(name):
        # Obviously fake values. They exist only so the code path that REQUIRES
        # a configured credential can be exercised; no request is ever sent.
        if stock_keys and name in ("PEXELS_API_KEY", "PIXABAY_API_KEY"):
            return "FAKE-STOCK-" + "0" * 10
        return base(name)

    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True, secret_getter=secrets,
        **{**fixtures.fixture_client_factories(),
           "google_client_factory": (lambda: client)}))


def seeded_turbo(root, client, *, stock_keys=False, **brief):
    from creator_console.api import ConsoleApi
    app = turbo_app(root, client, stock_keys=stock_keys)
    api = ConsoleApi(app)
    pid = app.create_project(title="Turbo", created_by="verify")["project_id"]
    payload = {"topic": "Roma imparatorlugunun cokusu", "duration_sec": 30,
               "aspect_ratio": "9:16", "clip_duration_sec": 5.0}
    payload.update(brief)
    call(api, "POST", f"/api/projects/{pid}/turbo/brief", payload)
    service = app.service(app.ownership(pid))
    return app, api, pid, service.turbo_studio()


def test_script_and_tts():
    section("F. Script ve TTS (sahte saglayici)")

    root = Path(tempfile.mkdtemp(prefix="turbo_script_"))
    try:
        # Three beats, because the fixture TTS model allows 3 requests per
        # minute: a fourth would exercise the rate limiter, which section G
        # tests deliberately, instead of the pass itself.
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client)

        script = studio.run_script()
        check(client.text_calls == 1, "script-TEK-rota-cagrisi",
              f"{client.text_calls} cagri")
        check(script["beat_count"] == 3 and len(script["scene_beats"]) == 3,
              "sahne-plani-uretildi", str(script["beat_count"]))
        check(all(b["search_query"] for b in script["scene_beats"]),
              "her-sahnenin-arama-sorgusu-var")
        check(all(len(b["search_query"].split()) <= 5
                  for b in script["scene_beats"]),
              "arama-sorgulari-kisa-tutuldu")
        check(len(script["script_sha256"]) == 64, "script-hash-uretildi")
        check(turbo.stage_state(studio.workflow(), "script") == turbo.COMPLETED,
              "script-asamasi-tamamlandi")

        # The gate after it must still block the walk.
        decision = turbo.runnable_stage(studio.workflow())
        check(decision["stage"] == "script_review" and decision["blocked"],
              "script-sonrasi-insan-kapisi", decision["reason"])
        studio.approve_stage("script_review")

        # The assertion whose absence let the real defect through: the planner
        # must not cost more requests than the estimate promised. Feeding the
        # scene beats in as "chapters" made a 60-second video plan 12 TTS
        # requests while the estimate said 1, because `plan_chunks` never lets a
        # chunk straddle a chapter and a Turbo beat is ~12 words.
        planned = studio.plan_tts()
        estimated = studio.brief()["estimates"]["tts_chunks_estimate"]
        actual = len(planned["chunks"])
        check(actual <= estimated,
              "TTS-parca-sayisi-TAHMINI-ASMIYOR",
              f"tahmin {estimated}, planlanan {actual}")
        check(actual <= 3, "kisa-videoda-parca-sayisi-makul", f"{actual} parca")
        words = [c["word_count"] for c in planned["chunks"]]
        check(min(words) > 30 or len(words) == 1,
              "parcalar-cumle-kirintisi-degil", f"kelime sayilari: {words}")

        outcome = studio.run_tts()
        readiness = outcome["readiness"]
        check(readiness["ready"], "tum-parcalar-uretildi",
              f"{readiness['completed_count']}/{readiness['chunk_count']}")
        check(client.tts_calls == readiness["chunk_count"],
              "parca-basina-TEK-cagri",
              f"{client.tts_calls} cagri / {readiness['chunk_count']} parca")

        first_pass_calls = client.tts_calls
        again = studio.run_tts()
        check(client.tts_calls == first_pass_calls,
              "tamamlanan-parcalar-TEKRAR-SESLENDIRILMEZ",
              f"{client.tts_calls - first_pass_calls} fazladan cagri")
        check(again["produced"] == 0, "ikinci-gecis-sifir-uretim")

        combined = studio.concat_audio()
        check(combined["duration_sec"] > 0, "anlatim-birlestirildi",
              f"{combined['duration_sec']} sn")
        check("drift_sec" in combined, "olculen-sure-brief-hedefiyle-karsilastirildi",
              f"sapma {combined.get('drift_sec')} sn")
        check(str(root) not in json.dumps(studio.chunk_manifest().get("combined", {}).get(
            "relative_path", ""), ensure_ascii=False),
              "birlesik-ses-yolu-goreli")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tts_resumability():
    section("G. TTS kurtarilabilirligi ve kota isaretleri")

    # A mid-list failure must not cost the chunks before it.
    root = Path(tempfile.mkdtemp(prefix="turbo_resume_"))
    try:
        # ~540 words of narration -> 3 chunks at 180 words each, which is what
        # a "chunk 2 failed, chunks 1 and 3 survive" test actually needs.
        client = FakeGoogle(beats=3, fail_tts_sequence={2}, words_per_beat=180)
        _app, _api, _pid, studio = seeded_turbo(root, client, duration_sec=240)
        studio.run_script()
        studio.approve_stage("script_review")
        outcome = studio.run_tts()
        manifest = outcome["manifest"]
        done = [c["id"] for c in manifest["chunks"] if c["status"] == "completed"]
        check(len(done) >= 1, "hatadan-onceki-parcalar-korundu",
              f"{len(done)} parca tamamlandi")
        check(not outcome["readiness"]["ready"], "eksik-parca-hazir-sayilmaz")

        try:
            studio.concat_audio()
            check(False, "eksik-parcayla-birlestirme-reddedilir")
        except turbo.TurboError as exc:
            check("eksik" in str(exc), "eksik-parcayla-birlestirme-reddedilir",
                  str(exc)[:60])

        # Retry: only the missing chunks are spoken again.
        before = client.tts_calls
        studio.run_tts()
        spoken_again = client.tts_calls - before
        missing_then = outcome["readiness"]["chunk_count"] - len(done)
        check(spoken_again <= missing_then,
              "yeniden-denemede-yalnizca-eksikler-seslendirildi",
              f"{spoken_again} cagri / {missing_then} eksik")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # A 429 is a WAIT, not a failure -- retrying it spends quota that ran out.
    root = Path(tempfile.mkdtemp(prefix="turbo_429_"))
    try:
        client = FakeGoogle(beats=3, fail_tts_sequence={1}, words_per_beat=180,
                            tts_error="429 RESOURCE_EXHAUSTED quota exceeded")
        _app, _api, _pid, studio = seeded_turbo(root, client, duration_sec=240)
        studio.run_script()
        studio.approve_stage("script_review")
        studio.run_tts()
        state = turbo.stage_state(studio.workflow(), "tts")
        check(state in turbo.WAITING_STATES or state == turbo.FAILED,
              "429-sonrasi-durum-kaydedildi", state)
        if state in turbo.WAITING_STATES:
            entry = studio.workflow()["stages"]["tts"]
            check(bool(entry.get("resume_at")), "beklemede-resume_at-var",
                  str(entry.get("resume_at")))
            decision = turbo.runnable_stage(studio.workflow())
            check(decision.get("waiting") is True or decision.get("blocked"),
                  "beklerken-otomatik-yeniden-denenmez", decision["reason"])
        else:
            check(False, "429-FAILED-degil-WAITING-olmali",
                  "kota reddi hata olarak kaydedildi")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_script_revision_preserves_audio():
    section("H. Script revizyonu tamamlanan sesi korur")

    root = Path(tempfile.mkdtemp(prefix="turbo_revise_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client)
        studio.run_script()
        studio.approve_stage("script_review")
        studio.run_tts()
        first = {c["id"]: c.get("output_sha256")
                 for c in studio.chunk_manifest()["chunks"]
                 if c["status"] == "completed"}
        check(len(first) > 0, "ilk-gecis-ses-uretti", f"{len(first)} parca")

        # Re-planning against an unchanged script must reuse every chunk: the
        # identity hash is what makes a revision cheap.
        calls_before = client.tts_calls
        studio.plan_tts()
        studio.run_tts()
        check(client.tts_calls == calls_before,
              "degismeyen-script-hicbir-parcayi-yeniden-seslendirmez",
              f"{client.tts_calls - calls_before} fazladan cagri")
        after = {c["id"]: c.get("output_sha256")
                 for c in studio.chunk_manifest()["chunks"]
                 if c["status"] == "completed"}
        check(after == first, "parca-hashleri-degismedi")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===================================================================== I
def fake_pexels_payload(count=3, *, portrait=True):
    w, h = (1080, 1920) if portrait else (1920, 1080)
    return {"videos": [
        {"id": 100 + i, "width": w, "height": h, "duration": 12,
         "url": f"https://www.pexels.com/video/sample-{100 + i}/",
         "user": {"name": f"Uretici {i}", "url": f"https://www.pexels.com/@u{i}"},
         "video_files": [{"link": f"https://videos.pexels.com/clip{i}.mp4",
                          "width": w, "height": h, "file_type": "video/mp4"}]}
        for i in range(count)]}


def test_stock_media_stage():
    section("I. Stok materyal (acik eylemler, sifir otomatik indirme)")

    from local_pipeline import stock_media

    # The two plans must be able to coexist in one project.
    check(stock_media.plan_artifact("turbo") == "results/turbo/media_plan.json",
          "turbo-plani-kendi-yolunda", stock_media.plan_artifact("turbo"))
    check(stock_media.BROLL_ARTIFACT == "results/long_form/broll_plan.json",
          "long-form-yolu-degismedi", stock_media.BROLL_ARTIFACT)
    check(stock_media.clip_dirname("turbo") != stock_media.BROLL_DIRNAME,
          "klip klasorleri ayri")
    try:
        stock_media.plan_artifact("uydurma")
        check(False, "bilinmeyen-alan-reddedilir")
    except stock_media.StockMediaError:
        check(True, "bilinmeyen-alan-reddedilir")

    root = Path(tempfile.mkdtemp(prefix="turbo_media_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client, stock_keys=True)
        studio.run_script()

        searches = []
        original = stock_media.search_videos

        def spy(provider, query, api_key, **kwargs):
            searches.append({"provider": provider, "query": query,
                             "orientation": kwargs.get("orientation")})
            return {"provider": provider, "query": query, "count": 3,
                    "candidates": original.__wrapped__(provider, query, api_key,
                                                       **kwargs)
                    if hasattr(original, "__wrapped__") else
                    stock_media.parse_pexels(
                        fake_pexels_payload(),
                        query=query,
                        orientation=kwargs.get("orientation", "landscape")),
                    "searched_at": "2026-08-22T00:00:00+00:00"}

        stock_media.search_videos = spy
        try:
            plan = studio.run_media_plan()
            check(len(plan["slots"]) == 3, "sahne-basina-bir-slot",
                  str(len(plan["slots"])))
            check(searches == [], "plan-olusturmak-ARAMA-YAPMAZ",
                  f"{len(searches)} arama")
            check(all(not slot["download"] for slot in plan["slots"]),
                  "plan-olusturmak-INDIRME-YAPMAZ")
            check(plan["orientation"] == "portrait",
                  "9:16-icin-portre-materyal-aranir", plan["orientation"])
            check(plan["aspect_ratio"] == "9:16", "plan-brief-oranini-tasiyor")

            studio.search_slot("beat-001", provider="pexels")
            check(len(searches) == 1, "arama-yalnizca-ACIK-eylemle",
                  f"{len(searches)} arama")
            check(searches[0]["orientation"] == "portrait",
                  "arama-portre-istedi", str(searches[0]["orientation"]))

            plan = studio.media_plan()
            slot = next(s for s in plan["slots"] if s["id"] == "beat-001")
            check(len(slot["candidates"]) == 3, "adaylar-kaydedildi",
                  str(len(slot["candidates"])))
            check(not slot["download"], "arama-INDIRME-TETIKLEMEZ")
            check(all(c.get("attribution") and c.get("source_page")
                      for c in slot["candidates"]),
                  "her-adayin-atfi-ve-kaynak-sayfasi-var")

            plan = studio.select_clip("beat-001", slot["candidates"][0]["id"])
            slot = next(s for s in plan["slots"] if s["id"] == "beat-001")
            check(slot["selected"] is not None, "aday-secildi")
            check(not slot["download"], "secim-INDIRME-TETIKLEMEZ")
            check(len(searches) == 1, "secim-YENI-ARAMA-YAPMAZ")

            # No credential may reach the response or the artifact.
            blob = json.dumps(plan, ensure_ascii=False)
            check("fake-" not in blob and "Authorization" not in blob,
                  "planda-kimlik-bilgisi-yok")

            readiness = studio.media_readiness()
            check(readiness["downloaded_count"] == 0 and not readiness["ready"],
                  "hicbir-klip-kendiliginden-inmedi")
            check(len(readiness["missing"]) == 3, "eksikler-bildiriliyor",
                  str(readiness["missing"]))

            # A script change must make the plan stale and block the actions.
            client.beats = 4
            studio.run_script()
            check(studio.media_readiness()["stale"],
                  "script-degisince-plan-BAYAT")
            for label, action in (
                    ("arama", lambda: studio.search_slot("beat-001",
                                                         provider="pexels")),
                    ("secim", lambda: studio.select_clip("beat-001", "x")),
                    ("indirme", lambda: studio.download_clip("beat-001"))):
                try:
                    action()
                    check(False, f"bayat-planda-{label}-reddedilir")
                except turbo.TurboError as exc:
                    check("degisti" in str(exc), f"bayat-planda-{label}-reddedilir",
                          str(exc)[:50])

            # Re-planning preserves what is still valid.
            searches_before = len(searches)
            studio.run_media_plan()
            check(len(searches) == searches_before,
                  "plani-yenilemek-ARAMA-YAPMAZ")
            check(not studio.media_readiness()["stale"],
                  "yenilenen-plan-taze")
        finally:
            stock_media.search_videos = original

        # Both plans coexist: writing Turbo's must not touch long-form's.
        lf = studio._path(stock_media.BROLL_ARTIFACT)
        tb = studio._path(stock_media.plan_artifact("turbo"))
        check(tb.is_file() and not lf.is_file(),
              "turbo-plani-long-form-dosyasina-DOKUNMADI",
              f"turbo={tb.is_file()} long_form={lf.is_file()}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===================================================================== J
def _sources(names, *, duration=8.0):
    return {n: {"relative_path": f"{n}.mp4", "sha256": "x" * 64,
                "provider": "pexels", "asset_id": n,
                "source_page": f"https://www.pexels.com/video/{n}/",
                "creator": {"name": "Uretici"},
                "attribution": f"Video by Uretici on Pexels",
                "license_note": "Pexels License",
                "probed": {"duration_sec": duration}} for n in names}


def _manifest(clips, order, *, total, width=1080, height=1920, music=False):
    from local_pipeline import timeline as tl
    audio = {"narration_volume": 1.0,
             "music": ({"present": True, "volume": 0.18, "loop": True}
                       if music else {"present": False})}
    return tl.new_manifest(
        clips=clips,
        narration={"duration_sec": total, "relative_path": "nar.wav",
                   "sha256": "y" * 64},
        video={"width": width, "height": height, "fps": 30},
        audio=audio,
        ordering={"mode": "sequential", "seed": None, "clip_id_order": order},
        settings_snapshot={"encode": {}})


class _FakeWhisperWord:
    def __init__(self, word, start, end, probability=0.9):
        self.word, self.start, self.end = word, start, end
        self.probability = probability


class _FakeWhisperSegment:
    def __init__(self, start, end, text, words):
        self.start, self.end, self.text, self.words = start, end, text, words


def install_fake_whisper(*, with_words: bool):
    """Replace faster_whisper so the caption stage is testable without a model.

    `with_words=False` returns segments that carry NO word timestamps, which is
    exactly how `transcribe_for_dynamic_captions` reports the degraded path
    (`faster-whisper-segment-fallback`). Running a real 39 MB model here would
    prove nothing extra and would make the assertions depend on what it happens
    to hear.
    """
    import types

    module = types.ModuleType("faster_whisper")

    class FakeModel:
        def __init__(self, size, **kwargs):
            pass

        def transcribe(self, path, **kwargs):
            words = ([_FakeWhisperWord("Roma", 0.0, 0.6),
                       _FakeWhisperWord("neden", 0.6, 1.2),
                       _FakeWhisperWord("coktu", 1.2, 2.0)]
                     if with_words else [])
            segment = _FakeWhisperSegment(0.0, 2.0, "Roma neden coktu", words)
            return iter([segment]), object()

    module.WhisperModel = FakeModel
    previous = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = module
    return previous


def _restore_whisper(previous):
    if previous is None:
        sys.modules.pop("faster_whisper", None)
    else:
        sys.modules["faster_whisper"] = previous


def _turbo_up_to_audio(root, *, beats=3):
    """A studio taken as far as a joined narration, ready for the caption stage."""
    client = FakeGoogle(beats=beats)
    _app, _api, _pid, studio = seeded_turbo(root, client)
    studio.run_script()
    studio.approve_stage("script_review")
    studio.run_tts()
    studio.concat_audio()
    return studio


def test_subtitles_stage():
    section("S. Altyazi asamasi: dinamik ASS ve bozunma yolu")

    # --- word alignment succeeds -> burned ASS with an active word ----------
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_ass_"))
    try:
        studio = _turbo_up_to_audio(root)
        previous = install_fake_whisper(with_words=True)
        try:
            record = studio.run_subtitles()
        finally:
            _restore_whisper(previous)

        check(record["format"] == "ass", "altyazi-hizalamada-ASS",
              str(record.get("format")))
        check(record["dynamic_word_highlight"] is True,
              "aktif-kelime-vurgusu-bildiriliyor", str(record))
        check(record["word_count"] > 0, "kelime-sayisi-kaydedildi",
              str(record.get("word_count")))
        check(record["degraded"] is False, "hizalama-bozunma-DEGIL",
              str(record.get("source")))
        # ASS carries its own [V4+ Styles]; a force_style would override the
        # theme and flatten the highlight, so the style dict must stay empty.
        check(record["style"] == {}, "ASS-yolunda-force_style-YOK",
              str(record.get("style")))

        ass_path = studio._path(turbo.SUBTITLE_ASS_PATH)
        check(ass_path.is_file(), "ASS-dosyasi-diskte", str(ass_path.name))
        body = ass_path.read_text(encoding="utf-8")
        check("[V4+ Styles]" in body and "Dialogue:" in body,
              "ASS-gecerli-bolumler-iceriyor", body[:60])
        check(studio.subtitles() == ass_path, "yakilacak-dosya-ASS",
              str(studio.subtitles()))
        # The SRT is still written as the degraded-path artifact.
        check(studio._path(turbo.SUBTITLE_PATH).is_file(),
              "SRT-de-yaninda-duruyor")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- no word alignment -> plain SRT, and it SAYS it degraded ------------
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_srt_"))
    try:
        studio = _turbo_up_to_audio(root)
        previous = install_fake_whisper(with_words=False)
        try:
            record = studio.run_subtitles()
        finally:
            _restore_whisper(previous)

        check(record["format"] == "srt", "hizalama-yoksa-SRT",
              str(record.get("format")))
        check(record["degraded"] is True, "bozunma-ACIKCA-bildiriliyor",
              str(record.get("source")))
        check(record["dynamic_word_highlight"] is False,
              "bozunmada-vurgu-iddia-EDILMIYOR", str(record))
        check(record["word_count"] == 0, "bozunmada-kelime-sayisi-sifir",
              str(record.get("word_count")))
        check(record["style"], "SRT-yolunda-force_style-KORUNUYOR",
              str(record.get("style")))
        check(not studio._path(turbo.SUBTITLE_ASS_PATH).is_file(),
              "bozunmada-ASS-URETILMEZ")
        check(studio.subtitles() == studio._path(turbo.SUBTITLE_PATH),
              "yakilacak-dosya-SRT", str(studio.subtitles()))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- a degraded RERUN must not leave the earlier ASS behind -------------
    # The renderer prefers ASS whenever it exists, so a stale one from a
    # previously successful pass would keep being burned while the record says
    # "srt" -- captions from an older narration, silently.
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_stale_"))
    try:
        studio = _turbo_up_to_audio(root)
        previous = install_fake_whisper(with_words=True)
        try:
            studio.run_subtitles()
        finally:
            _restore_whisper(previous)
        check(studio._path(turbo.SUBTITLE_ASS_PATH).is_file(),
              "on-kosul-ASS-uretildi")

        previous = install_fake_whisper(with_words=False)
        try:
            record = studio.run_subtitles()
        finally:
            _restore_whisper(previous)
        check(record["format"] == "srt", "bayat-kosu-SRT-e-donuyor",
              str(record.get("format")))
        check(not studio._path(turbo.SUBTITLE_ASS_PATH).is_file(),
              "BAYAT-ASS-SILINDI",
              "eski ASS kalirsa render yeni SRT yerine onu yakar")
        check(studio.subtitles() == studio._path(turbo.SUBTITLE_PATH),
              "bayat-kosudan-sonra-SRT-yakilir", str(studio.subtitles()))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- subtitles disabled by the brief -----------------------------------
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_off_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client,
                                                subtitles_enabled=False)
        studio.run_script()
        studio.approve_stage("script_review")
        studio.run_tts()
        studio.concat_audio()
        record = studio.run_subtitles()
        check(record["enabled"] is False, "brief-altyaziyi-kapatabiliyor",
              str(record))
        check(not studio._path(turbo.SUBTITLE_ASS_PATH).is_file(),
              "kapaliyken-ASS-uretilmez")
    finally:
        shutil.rmtree(root, ignore_errors=True)


    # --- the theme decides the look, on BOTH paths ---------------------------
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_theme_"))
    try:
        studio = _turbo_up_to_audio(root)
        previous = install_fake_whisper(with_words=True)
        try:
            default_record = studio.run_subtitles()
            default_body = studio._path(turbo.SUBTITLE_ASS_PATH).read_text(
                encoding="utf-8")

            # Switch the theme the way the operator does -- through the
            # revision, not by hand-editing the brief.
            studio.apply_revision("subtitle_style",
                                  values={"caption_theme": "game_of_thrones"})
            check(studio.brief()["caption_theme"] == "game_of_thrones",
                  "revizyon-temayi-briefe-yazdi",
                  str(studio.brief().get("caption_theme")))
            check(turbo.stage_state(studio.workflow(), "subtitles")
                  != turbo.COMPLETED,
                  "tema-degisimi-altyazi-asamasini-acar",
                  turbo.stage_state(studio.workflow(), "subtitles"))

            themed_record = studio.run_subtitles()
            themed_body = studio._path(turbo.SUBTITLE_ASS_PATH).read_text(
                encoding="utf-8")
        finally:
            _restore_whisper(previous)

        check(themed_record["caption_theme"] == "game_of_thrones",
              "yeni-tema-kayda-yazildi", str(themed_record.get("caption_theme")))
        got_font = render_utils.CAPTION_THEME_PROFILES["game_of_thrones"]["font_name"]
        check(got_font in themed_body and got_font not in default_body,
              "ASS-YENI-TEMAYLA-yeniden-uretildi",
              f"{got_font!r} ASS icinde bulunmali")
        check(themed_record["style"] == {}, "tema-degisse-de-force_style-YOK",
              str(themed_record.get("style")))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # An unknown theme is refused BEFORE anything is invalidated.
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_badtheme_"))
    try:
        studio = _turbo_up_to_audio(root)
        previous = install_fake_whisper(with_words=True)
        try:
            studio.run_subtitles()
        finally:
            _restore_whisper(previous)
        before_state = turbo.stage_state(studio.workflow(), "subtitles")
        try:
            studio.apply_revision("subtitle_style",
                                  values={"caption_theme": "yok-boyle-tema"})
            check(False, "bilinmeyen-tema-REDDEDILIR", "kabul edildi")
        except turbo.TurboError as exc:
            check("tema" in str(exc).lower(), "bilinmeyen-tema-REDDEDILIR",
                  str(exc)[:70])
        check(turbo.stage_state(studio.workflow(), "subtitles") == before_state,
              "reddedilen-revizyon-HICBIR-SEYI-bayatlatmaz",
              "gecersiz istek isi geri almamali")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # The degraded path also follows the theme instead of a frozen look.
    root = Path(tempfile.mkdtemp(prefix="turbo_subs_srttheme_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client,
                                                caption_theme="game_of_thrones")
        studio.run_script()
        studio.approve_stage("script_review")
        studio.run_tts()
        studio.concat_audio()
        previous = install_fake_whisper(with_words=False)
        try:
            record = studio.run_subtitles()
        finally:
            _restore_whisper(previous)
        profile = render_utils.CAPTION_THEME_PROFILES["game_of_thrones"]
        check(record["style"].get("font_name") == profile["font_name"],
              "SRT-force_style-de-TEMADAN-turetiliyor", str(record.get("style")))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_render_helpers():
    section("J. Render yardimcilari (etiket catismasi ve yatay kadraj)")

    from local_pipeline import render_utils as ru

    # The Shorts path must be untouched, byte for byte.
    check(ru.build_vertical_filtergraph("center_crop", 1080, 1920)
          == "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920:(iw-ow)*0.5000:(ih-oh)*0.5000[framed]",
          "varsayilanlar-Shorts-ciktisini-DEGISTIRMEDI")
    check(ru.build_audio_filtergraph(False, False, 8.0, 0.2, 1.0)
          == "[1:a]volume=1.0[aout]",
          "ses-grafigi-varsayilani-degismedi")

    # The landscape overflow the fit mode used to produce.
    landscape = ru.build_vertical_filtergraph("blurred_background", 1920, 1080)
    check("scale=1920:1080:force_original_aspect_ratio=decrease" in landscape,
          "yatay-hedefte-on-plan-tasmasi-DUZELDI")
    check("scale=1920:-2" not in landscape, "eski-tasiran-ifade-kalmadi")

    # Five labels, not one: the collision class that broke scene_editor.
    a = ru.build_vertical_filtergraph("blurred_background", 1080, 1920,
                                      in_label="pre_0", out_label="framed_0",
                                      label_suffix="_0")
    b = ru.build_vertical_filtergraph("blurred_background", 1080, 1920,
                                      in_label="pre_1", out_label="framed_1",
                                      label_suffix="_1")
    import re
    shared = set(re.findall(r"\[(\w+)\]", a)) & set(re.findall(r"\[(\w+)\]", b))
    check(not shared, "iki-klip-HIC-etiket-paylasmiyor", str(sorted(shared)))


def test_timeline_packing():
    section("K. Zaman cizelgesi paketleme ve determinizm")

    from local_pipeline import timeline as tl

    sources = _sources(["a", "b", "c"])
    order = tl.order_clip_ids(sources, mode="sequential", seed=None)
    clips = tl.pack_clips(sources, total_duration_sec=23.0,
                          clip_duration_sec=5.0, fps=30, order=order,
                          frame_defaults={"mode": "crop", "zoom": 1.0,
                                          "focus_x": 0.5, "focus_y": 0.5})

    check(clips[0]["timeline_start_sec"] == 0.0, "ilk-klip-sifirda-baslar")
    gaps = [round(b["timeline_start_sec"] - a["timeline_end_sec"], 4)
            for a, b in zip(clips, clips[1:])]
    check(set(gaps) == {0.0}, "BOSLUK-VE-CAKISMA-YOK", str(sorted(set(gaps))))
    check(clips[-1]["timeline_end_sec"] == 23.0,
          "zaman-cizelgesi-suresi-ANLATIMA-esit",
          str(clips[-1]["timeline_end_sec"]))
    check(sum(1 for c in clips if c["reuse_index"]) == 2,
          "yetersiz-klip-TEKRAR-olarak-kaydedildi",
          f"{sum(1 for c in clips if c['reuse_index'])} tekrar")
    check(clips[3]["source_in_sec"] != clips[0]["source_in_sec"],
          "tekrar-kaynagin-BASKA-bolumunden-alir",
          f"{clips[0]['source_in_sec']} -> {clips[3]['source_in_sec']}")

    # A manifest that disagrees with itself is rejected, not rendered.
    broken = [dict(c) for c in clips]
    broken[1]["timeline_start_sec"] = 6.0
    try:
        tl.assert_gapless(broken, 23.0)
        check(False, "tutarsiz-cizelge-REDDEDILIR")
    except tl.TimelineError as exc:
        check("bosluk" in str(exc).lower(), "tutarsiz-cizelge-REDDEDILIR",
              str(exc)[:60])

    short = [dict(c) for c in clips]
    short[-1]["timeline_end_sec"] = 20.0
    try:
        tl.assert_gapless(short, 23.0)
        check(False, "anlatimdan-sapan-cizelge-REDDEDILIR")
    except tl.TimelineError as exc:
        check("sapiyor" in str(exc), "anlatimdan-sapan-cizelge-REDDEDILIR",
              str(exc)[:60])

    # Determinism: same seed, same order and same graph.
    seed = tl.resolve_seed(project_id="p1", script_sha256="abc", seed_input="7")
    one = tl.order_clip_ids(sources, mode="seeded_random", seed=seed)
    two = tl.order_clip_ids(sources, mode="seeded_random", seed=seed)
    check(one == two, "ayni-seed-AYNI-sira", f"{one} / {two}")
    other = tl.resolve_seed(project_id="p1", script_sha256="abc", seed_input="8")
    check(tl.order_clip_ids(sources, mode="seeded_random", seed=other) != one
          or len(sources) < 3,
          "farkli-seed-farkli-sira")
    check(tl.resolve_seed(project_id="p2", script_sha256="abc", seed_input="7")
          != seed, "ayni-seed-metni-farkli-projede-CAKISMAZ")

    m1 = _manifest(clips, order, total=23.0)
    m2 = _manifest(tl.pack_clips(sources, total_duration_sec=23.0,
                                 clip_duration_sec=5.0, fps=30, order=order,
                                 frame_defaults={"mode": "crop", "zoom": 1.0,
                                                 "focus_x": 0.5, "focus_y": 0.5}),
                   order, total=23.0)
    check(tl.build_filter_complex(m1) == tl.build_filter_complex(m2),
          "ayni-manifest-BIT-BIT-ayni-grafik")
    check(m1["render_fingerprint"] == m2["render_fingerprint"],
          "ayni-manifest-ayni-parmak-izi")

    # The graph must never reuse an output label.
    graph = tl.build_filter_complex(m1)
    outputs = re.findall(r"\[(\w+)\](?=;|$)", graph)
    dupes = sorted({name for name in outputs if outputs.count(name) > 1})
    check(not dupes, "grafikte-cift-uretilen-etiket-YOK", str(dupes))
    check(graph.count("[0:v]") == 1, "[0:v]-yalnizca-ilk-klipte",
          str(graph.count("[0:v]")))
    check("setsar=1" in graph and graph.count("setsar=1") >= len(clips) * 2,
          "her-klipte-SAR-normallestirmesi",
          f"{graph.count('setsar=1')} kez")
    check(f"fps=30" in graph, "her-klipte-fps-normallestirmesi")
    check(":a]" not in graph.split("concat=")[0],
          "stok-klip-sesi-HIC-referans-edilmiyor")

    over = _sources([f"s{i}" for i in range(3)])
    try:
        tl.pack_clips(over, total_duration_sec=3000.0, clip_duration_sec=2.0,
                      fps=30, order=sorted(over), frame_defaults={"mode": "crop"})
        check(False, "klip-tavani-asilinca-reddedilir")
    except tl.TimelineError as exc:
        check("ust sinir" in str(exc), "klip-tavani-asilinca-reddedilir",
              str(exc)[:50])


def test_timeline_revisions():
    section("L. Zaman cizelgesi revizyonlari (manifest ici)")

    from local_pipeline import timeline as tl
    sources = _sources(["a", "b", "c"])
    order = tl.order_clip_ids(sources, mode="sequential", seed=None)
    clips = tl.pack_clips(sources, total_duration_sec=15.0,
                          clip_duration_sec=5.0, fps=30, order=order,
                          frame_defaults={"mode": "crop", "zoom": 1.0,
                                          "focus_x": 0.5, "focus_y": 0.5})
    manifest = _manifest(clips, order, total=15.0)
    original = manifest["render_fingerprint"]

    framed = tl.set_frame(manifest, "c001", mode="fit", zoom=1.5)
    check(framed["clips"][0]["frame"]["mode"] == "fit", "kadraj-degisti")
    check(framed["render_fingerprint"] != original,
          "kadraj-revizyonu-PARMAK-IZINI-degistirir")
    check(framed["clips"][1]["frame"] == manifest["clips"][1]["frame"],
          "kadraj-revizyonu-diger-klipleri-BOZMAZ")
    check(framed["duration_sec"] == manifest["duration_sec"],
          "kadraj-revizyonu-sureyi-degistirmez")
    try:
        tl.set_frame(manifest, "c001", zoom=5.0)
        check(False, "asiri-zoom-reddedilir")
    except tl.TimelineError:
        check(True, "asiri-zoom-reddedilir")

    retimed = tl.retime_clip(manifest, "c001", 7.0)
    check(retimed["clips"][0]["timeline_end_sec"] == 7.0,
          "klip-yeniden-zamanlandi",
          str(retimed["clips"][0]["timeline_end_sec"]))
    check(retimed["clips"][-1]["timeline_end_sec"] == 15.0,
          "TOPLAM-SURE-KORUNDU (anlatimla senkron)",
          str(retimed["clips"][-1]["timeline_end_sec"]))
    tl.assert_gapless(retimed["clips"], 15.0)
    check(True, "yeniden-zamanlama-sonrasi-hala-BOSLUKSUZ")
    try:
        tl.retime_clip(manifest, "c003", 9.0)
        check(False, "son-klip-yeniden-zamanlanamaz")
    except tl.TimelineError as exc:
        check(True, "son-klip-yeniden-zamanlanamaz", str(exc)[:50])
    try:
        tl.retime_clip(manifest, "c001", 14.5)
        check(False, "emilemeyecek-uzatma-reddedilir")
    except tl.TimelineError as exc:
        check("ememez" in str(exc), "emilemeyecek-uzatma-reddedilir",
              str(exc)[:50])


def test_real_render():
    section("M. Gercek ffmpeg render (heterojen kaynaklar)")

    from local_pipeline import timeline as tl, render_utils as ru
    ffmpeg_bin, ffprobe_bin = ru.resolve_ffmpeg_binaries()
    if not ffmpeg_bin:
        check(False, "ffmpeg-bulunamadi", "render testi calistirilamadi")
        return

    root = Path(tempfile.mkdtemp(prefix="turbo_render_"))
    try:
        # Deliberately mismatched: different sizes, frame rates and a non-square
        # pixel aspect ratio. This is what actually breaks `concat`.
        specs = [("a", "testsrc=size=1920x1080:duration=8:rate=25", []),
                 ("b", "testsrc=size=1080x1920:duration=8:rate=30", []),
                 ("c", "smptebars=size=1440x1080:duration=8:rate=24",
                  ["-vf", "setsar=4/3"])]
        for name, source, extra in specs:
            subprocess.run([ffmpeg_bin, "-y", "-f", "lavfi", "-i", source,
                            *extra, "-pix_fmt", "yuv420p",
                            str(root / f"{name}.mp4")],
                           check=True, capture_output=True)
        subprocess.run([ffmpeg_bin, "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=14",
                        "-ar", "24000", "-ac", "1", str(root / "nar.wav")],
                       check=True, capture_output=True)

        sources = _sources(["a", "b", "c"])
        order = tl.order_clip_ids(sources, mode="sequential", seed=None)
        clips = tl.pack_clips(sources, total_duration_sec=14.0,
                              clip_duration_sec=5.0, fps=30, order=order,
                              frame_defaults={"mode": "fit", "zoom": 1.0,
                                              "focus_x": 0.5, "focus_y": 0.5},
                              transition="fade", transition_sec=0.4)
        manifest = _manifest(clips, order, total=14.0)
        graph_path = tl.write_graph(tl.build_filter_complex(manifest),
                                    root / "graph.txt")
        args = tl.build_ffmpeg_args(
            manifest, resolve=lambda c: root / c["source"]["relative_path"],
            narration_path=root / "nar.wav", output_path=root / "final.mp4",
            graph_path=graph_path, preset="final")
        check("-/filter_complex" in args,
              "grafik-DOSYADAN-okunuyor (komut satiri siniri)")
        check("-an" not in args, "render-SESSIZ-degil")

        ru.run_ffmpeg(ffmpeg_bin, args)
        check((root / "final.mp4").is_file(),
              "heterojen-kaynaklar-GERCEKTEN-render-oldu")

        probe = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,sample_aspect_ratio,nb_frames",
             "-of", "csv=p=0", str(root / "final.mp4")],
            capture_output=True, text=True).stdout.strip()
        width, height, sar, frames = probe.split(",")
        check((width, height) == ("1080", "1920"), "cikti-9:16-1080p",
              f"{width}x{height}")
        check(sar == "1:1", "cikti-SAR-kare", sar)
        check(abs(int(frames) - 14 * 30) <= 1,
              "SURE-ANLATIMLA-AYNI (gecisler kisaltmadi)",
              f"{frames} kare, beklenen {14 * 30}")

        audio = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,channels", "-of", "csv=p=0",
             str(root / "final.mp4")], capture_output=True, text=True).stdout.strip()
        check(audio.startswith("aac") and audio.endswith("2"),
              "anlatim-iki-kanalli-aac-olarak-mukslandi", audio)

        # Preview must be cheaper and smaller, and separate from final.
        preview_args = tl.build_ffmpeg_args(
            manifest, resolve=lambda c: root / c["source"]["relative_path"],
            narration_path=root / "nar.wav", output_path=root / "preview.mp4",
            graph_path=graph_path, preset="preview")
        check("ultrafast" in preview_args and "30" in preview_args,
              "onizleme-ucuz-profil-kullanir")
        check("540x960" in " ".join(preview_args),
              "onizleme-yari-olcek",
              [a for a in preview_args if "x" in a and a[0].isdigit()][:1])
        check("slow" in args and "18" in args, "final-kaliteli-profil-kullanir")
        check("1080x1920" in " ".join(args), "final-TAM-1080p")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===================================================================== N
def test_music_licensing():
    section("N. Muzik lisansi (hicbir parca telifsiz ilan edilmez)")

    from local_pipeline import music_library as ml

    root = Path(tempfile.mkdtemp(prefix="turbo_music_"))
    try:
        (root / "tam.mp3").write_bytes(b"ID3" + b"\x00" * 64)
        (root / "tam.mp3.license.json").write_text(json.dumps({
            "title": "Sakin Parca", "artist": "Bir Sanatci",
            "license_id": "CC0-1.0",
            "source_url": "https://example.org/track",
            "verified_by": "operator", "verified_at": "2026-08-22",
            "attribution_required": False}), encoding="utf-8")
        (root / "eksik.mp3").write_bytes(b"ID3" + b"\x00" * 64)
        (root / "eksik.mp3.license.json").write_text(json.dumps({
            "title": "Yarim Kayit", "artist": ""}), encoding="utf-8")
        (root / "kayitsiz.mp3").write_bytes(b"ID3" + b"\x00" * 64)

        library = ml.scan_library(root)
        by_name = {t["filename"]: t for t in library["tracks"]}
        check(len(library["tracks"]) == 3, "tum-parcalar-LISTELENIR",
              f"{len(library['tracks'])} parca")
        check(by_name["tam.mp3"]["license"]["status"] == ml.STATUS_VERIFIED,
              "tam-kayit-verified")
        check(by_name["eksik.mp3"]["license"]["status"] == ml.STATUS_UNVERIFIED,
              "eksik-kayit-unverified",
              str(by_name["eksik.mp3"]["license"]["missing"]))
        check(by_name["kayitsiz.mp3"]["license"]["status"] == ml.STATUS_MISSING,
              "kayitsiz-parca-missing")
        check(by_name["kayitsiz.mp3"]["needs_review"] is True,
              "kayitsiz-parca-inceleme-istiyor")
        check(by_name["tam.mp3"]["needs_review"] is False,
              "dogrulanmis-parca-inceleme-istemiyor")

        # The claim this module exists to prevent. Scanned over the PER-TRACK
        # data only: the library note legitimately contains "telifsiz" inside a
        # denial ("bu proje hicbir parcayi telifsiz olarak nitelemez"), and a
        # blunt substring search cannot tell a claim from a disclaimer.
        blob = json.dumps(library["tracks"], ensure_ascii=False).lower()
        for banned in ("telifsiz", "royalty-free", "royalty free",
                       "copyright-safe", "guvenle kullan", "kullanmakta serbest"):
            check(banned not in blob, f"parca-verisinde-'{banned}'-YOK")
        note = library["note"].lower()
        check("telifsiz" in note and "nitelemez" in note,
              "kutuphane-notu-telifsizlik-iddiasini-ACIKCA-reddediyor")
        check("safe" not in json.dumps(
            [t["license"] for t in library["tracks"]]).lower(),
              "lisans-kaydinda-'safe'-alani-YOK")

        check(ml.absent()["present"] is False, "muziksiz-secim-temsil-edilebilir")
        check("secti" in ml.absent()["reason"],
              "muziksiz-secim-CEVAPSIZ-soru-degil", ml.absent()["reason"])

        missing_root = ml.scan_library(root / "yok")
        check(missing_root["tracks"] == [] and not missing_root["root_exists"],
              "olmayan-kok-cokmez")

        try:
            ml.describe_track(root / "tam.mp3.license.json", origin="x")
            check(False, "ses-olmayan-dosya-reddedilir")
        except ml.MusicLibraryError as exc:
            check("bicim" in str(exc), "ses-olmayan-dosya-reddedilir",
                  str(exc)[:50])

        # Project upload: the second music source the operator asked for.
        upload_root = Path(tempfile.mkdtemp(prefix="turbo_upload_"))
        try:
            client = FakeGoogle(beats=3)
            _a, _i, _p, studio = seeded_turbo(upload_root, client)
            track = studio.upload_music(filename="benim_parcam.mp3",
                                        payload=b"ID3" + bytes(2048),
                                        content_length=2051)
            check(track["origin"] == "project_upload", "proje-yuklemesi-calisiyor")
            check(track["license"]["status"] == ml.STATUS_MISSING
                  and track["needs_review"],
                  "lisanssiz-yukleme-INCELEME-istiyor")
            listed = [t["filename"] for t in studio.music_choices()["uploaded"]]
            check("benim_parcam.mp3" in listed, "yuklenen-parca-listeleniyor",
                  str(listed))
            for bad, label in ((("video.mp4", b"x" * 10), "video-uzantisi"),
                               (("belge.txt", b"x" * 10), "metin-uzantisi")):
                try:
                    studio.upload_music(filename=bad[0], payload=bad[1],
                                        content_length=len(bad[1]))
                    check(False, f"{label}-reddedilir")
                except turbo.TurboError as exc:
                    check("bicim" in str(exc), f"{label}-reddedilir",
                          str(exc)[:45])
            try:
                studio.upload_music(filename="bos.mp3", payload=b"",
                                    content_length=0)
                check(False, "bos-yukleme-reddedilir")
            except turbo.TurboError:
                check(True, "bos-yukleme-reddedilir")
            leftovers = list((studio._path("results/turbo/music")).glob("*.part"))
            check(not leftovers, "reddedilen-yukleme-yarim-dosya-BIRAKMADI",
                  str([p.name for p in leftovers]))
            readiness = studio.archive_readiness()
            check("Arsivle" in readiness["control"],
                  "arsiv-mevcut-proje-kontrolune-YONLENDIRIYOR")
        finally:
            shutil.rmtree(upload_root, ignore_errors=True)

        entry = ml.manifest_entry(by_name["kayitsiz.mp3"],
                                  relative_path="results/turbo/music/k.mp3",
                                  sha256="z" * 64, duration_sec=120.0)
        check(entry["needs_review"] and entry["license"]["status"] == "missing",
              "manifest-girdisi-lisans-durumunu-TASIYOR")
        try:
            ml.manifest_entry(by_name["tam.mp3"], relative_path="x",
                              sha256="z", duration_sec=1.0, volume=3.0)
            check(False, "gecersiz-ses-duzeyi-reddedilir")
        except ml.MusicLibraryError:
            check(True, "gecersiz-ses-duzeyi-reddedilir")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_render_gating():
    section("O. Onizleme ve final ayri (ve final kendiliginden calismaz)")

    from local_pipeline import timeline as tl

    # The walker must never reach `final_render` on its own.
    record = turbo.new_workflow()
    for key in turbo.STAGE_KEYS[:turbo.STAGE_KEYS.index("final_render")]:
        record = turbo.set_stage(record, key, turbo.COMPLETED)
    decision = turbo.runnable_stage(record)
    check(decision["stage"] == "final_render" and decision["blocked"],
          "onizlemeden-sonra-final-OTOMATIK-baslamaz", decision["reason"])

    root = Path(tempfile.mkdtemp(prefix="turbo_gate_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client)
        try:
            studio.render(preset="final")
            check(False, "cizelgesiz-render-reddedilir")
        except turbo.TurboError as exc:
            check("zaman cizelgesi" in str(exc), "cizelgesiz-render-reddedilir",
                  str(exc)[:50])
        try:
            studio.render(preset="uydurma")
            check(False, "bilinmeyen-profil-reddedilir")
        except turbo.TurboError:
            check(True, "bilinmeyen-profil-reddedilir")

        # Fabricate a completed timeline so the preview gate can be tested
        # without spending anything on the stages before it.
        sources = _sources(["a", "b"])
        order = tl.order_clip_ids(sources, mode="sequential", seed=None)
        clips = tl.pack_clips(sources, total_duration_sec=8.0,
                              clip_duration_sec=4.0, fps=30, order=order,
                              frame_defaults={"mode": "crop", "zoom": 1.0})
        manifest = _manifest(clips, order, total=8.0)
        studio._write(turbo.TIMELINE_ARTIFACT, manifest, actor="verify")
        try:
            studio.render(preset="final")
            check(False, "onizleme-izlenmeden-final-REDDEDILIR")
        except turbo.TurboError as exc:
            check("onizleme" in str(exc), "onizleme-izlenmeden-final-REDDEDILIR",
                  str(exc)[:60])
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_env_contract():
    section("P. Ortam degiskeni sozlesmesi")

    from creator_console import env_file
    keys = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", Path(".env.example")
                          .read_text(encoding="utf-8"), re.M))
    unsupported = sorted(k for k in keys
                         if k not in env_file.SUPPORTED_LOCAL_ENV_KEYS)
    check(not unsupported,
          ".env.example-oldugu-gibi-KOPYALANABILIR", str(unsupported))
    check("CREATOR_CONSOLE_MUSIC_ROOT" in env_file.SUPPORTED_LOCAL_ENV_KEYS,
          "muzik-kokü-allowlistte")
    check("CREATOR_CONSOLE_MUSIC_ROOT" in keys, "muzik-kokü-belgelenmis")


# ===================================================================== Q
def test_revision_application():
    section("Q. Revizyon uygulamasi (silme yok, arsivleme var)")

    from local_pipeline import timeline as tl

    root = Path(tempfile.mkdtemp(prefix="turbo_rev_"))
    try:
        client = FakeGoogle(beats=3)
        _app, _api, _pid, studio = seeded_turbo(root, client)
        studio.run_script()

        sources = _sources(["a", "b"])
        order = tl.order_clip_ids(sources, mode="sequential", seed=None)
        clips = tl.pack_clips(sources, total_duration_sec=10.0,
                              clip_duration_sec=5.0, fps=30, order=order,
                              frame_defaults={"mode": "crop", "zoom": 1.0,
                                              "focus_x": 0.5, "focus_y": 0.5})
        manifest = _manifest(clips, order, total=10.0)
        studio._write(turbo.TIMELINE_ARTIFACT, manifest, actor="verify")
        before_print = manifest["render_fingerprint"]

        # Pretend both renders exist, so the archiving path has something real.
        record = studio._require_workflow()
        for key in ("script", "script_review", "tts", "audio_concat",
                    "audio_review", "media_plan", "media", "music",
                    "subtitles", "timeline", "timeline_review",
                    "preview_render", "final_render"):
            record = turbo.set_stage(record, key, turbo.COMPLETED)
        studio._save_workflow(record, actor="verify")
        for preset in ("preview", "final"):
            out = studio._path(f"results/turbo/renders/{preset}.mp4")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"MP4-" + preset.encode())
            studio._write(f"results/turbo/{preset}_render.json",
                          {"artifact_type": f"turbo_{preset}_render",
                           "status": "review_required", "preset": preset},
                          actor="verify")

        result = studio.apply_revision("frame", target="c001",
                                       values={"mode": "fit", "zoom": 1.4})
        after = studio.timeline_manifest()
        check(after["clips"][0]["frame"]["mode"] == "fit",
              "kadraj-revizyonu-manifeste-YAZILDI")
        check(after["render_fingerprint"] != before_print,
              "revizyon-PARMAK-IZINI-degistirdi")
        check(set(result["stale_stages"]) == set(turbo.RENDER_STAGES),
              "kadraj-revizyonu-yalnizca-renderlari-bayatlatti",
              str(result["stale_stages"]))

        after_record = studio.workflow()
        check(turbo.stage_state(after_record, "media") == turbo.COMPLETED,
              "INDIRILMIS-KLIPLER-hala-tamam")
        check(turbo.stage_state(after_record, "tts") == turbo.COMPLETED,
              "SESLENDIRME-hala-tamam")
        check(turbo.stage_state(after_record, "timeline") == turbo.COMPLETED,
              "zaman-cizelgesi-yeniden-calistirilmiyor")
        check(turbo.stage_state(after_record, "preview_render")
              == turbo.NOT_STARTED, "onizleme-yeniden-render-bekliyor")
        check(turbo.stage_state(after_record, "final_render")
              == turbo.NOT_STARTED, "final-yeniden-render-bekliyor")

        check(sorted(result["archived"]) == [
                  "results/turbo/final_render.json",
                  "results/turbo/preview_render.json",
                  "results/turbo/renders/final.mp4",
                  "results/turbo/renders/preview.mp4"],
              "SADECE-eski-renderlar-arsivlendi", str(result["archived"]))
        leftover = list(studio._path("results/turbo/renders").glob("*.mp4"))
        check(not leftover, "eski-render-aktif-klasorden-KALDIRILDI",
              str([p.name for p in leftover]))

        # Music removal keeps its own stage answered.
        studio._write_music({"present": True, "volume": 0.2,
                             "license": {"status": "missing"}}, actor="verify")
        manifest = studio.timeline_manifest()
        manifest["audio"]["music"] = {"present": True, "volume": 0.2}
        studio._write(turbo.TIMELINE_ARTIFACT, manifest, actor="verify")
        record = turbo.set_stage(studio.workflow(), "music", turbo.COMPLETED)
        studio._save_workflow(record, actor="verify")
        studio.apply_revision("music_remove")
        check(turbo.stage_state(studio.workflow(), "music") == turbo.COMPLETED,
              "muzigi-kaldirmak-muzik-asamasini-YENIDEN-ACMAZ",
              "cevaplanmis bir soru tekrar sorulmamali")
        check(studio.timeline_manifest()["audio"]["music"]["present"] is False,
              "manifest-muziksiz")

        try:
            studio.apply_revision("uydurma")
            check(False, "bilinmeyen-revizyon-reddedilir")
        except turbo.TurboError:
            check(True, "bilinmeyen-revizyon-reddedilir")

        readiness = studio.archive_readiness()
        check(readiness["total_bytes"] > 0, "arsiv-boyutu-ONCEDEN-bildiriliyor",
              f"{readiness['total_bytes']} bayt")
        check(readiness["ready"] is False,
              "final-render-yokken-arsivleme-hazir-degil", readiness["reason"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ===================================================================== R
def test_api_routes():
    section("R. API rotalari (hepsi bagli ve dogru reddediyor)")

    from creator_console.api import ConsoleApi

    root = Path(tempfile.mkdtemp(prefix="turbo_routes_"))
    try:
        client = FakeGoogle(beats=3)
        app, api, pid, studio = seeded_turbo(root, client, stock_keys=True)
        base = f"/api/projects/{pid}"

        # Every route must EXIST. A 404 here means the UI has a dead button.
        routes = [
            ("GET", f"{base}/turbo", None),
            ("POST", f"{base}/turbo/brief",
             {"topic": "Roma imparatorlugunun cokusu", "duration_sec": 30}),
            ("GET", f"{base}/turbo/revision-preview?kind=frame", None),
            ("POST", f"{base}/turbo/stage", {}),
            ("POST", f"{base}/turbo/media", {"action": "plan"}),
            ("POST", f"{base}/turbo/music", {"origin": "none"}),
            ("POST", f"{base}/turbo/approve", {"stage": "script_review"}),
            ("POST", f"{base}/turbo/render", {"preset": "preview"}),
            ("POST", f"{base}/turbo/revision", {"kind": "frame",
                                                "target": "c001"}),
        ]
        for method, path, payload in routes:
            status, body = call(api, method, path, payload)
            check(status != 404, f"rota-var:{path.split('/turbo')[-1] or '/turbo'}",
                  f"{method} -> {status}")

        # A refusal must name a field the error catalog can label, otherwise
        # `FIELD_LABELS[name]` raises a KeyError instead of returning a 400.
        from creator_console.errors import FIELD_LABELS
        for field in ("topic", "stage", "slot_id", "candidate_id", "kind"):
            check(field in FIELD_LABELS, f"alan-etiketi-var:{field}")

        status, body = call(api, "POST", f"{base}/turbo/media",
                            {"action": "uydurma"})
        check(status == 400, "bilinmeyen-materyal-eylemi-400", str(status))

        status, body = call(api, "POST", f"{base}/turbo/music",
                            {"origin": "uydurma"})
        check(status == 400, "bilinmeyen-muzik-kaynagi-400", str(status))

        # The stage runner must refuse to jump a human gate through the API.
        studio2 = app.service(app.ownership(pid)).turbo_studio()
        status, body = call(api, "POST", f"{base}/turbo/stage", {})
        decision = body.get("decision") or {}
        # The real invariant, rather than a hardcoded list of allowed stages:
        # whatever ran must have had every earlier stage already completed.
        ran = body.get("ran")
        record = studio2.workflow()
        if ran:
            earlier = turbo.STAGE_KEYS[:turbo.STAGE_KEYS.index(ran)]
            unfinished = [k for k in earlier
                          if turbo.stage_state(record, k) != turbo.COMPLETED]
            check(not unfinished,
                  "asama-calistirici-INSAN-KAPISINI-atlamadi",
                  f"{ran} calisti; tamamlanmamis onceki: {unfinished}")
        else:
            check(True, "asama-calistirici-INSAN-KAPISINI-atlamadi",
                  "calistirilabilir asama yoktu")
        if decision.get("blocked"):
            check(bool(decision.get("reason")),
                  "engellenen-asama-GEREKCE-bildiriyor", decision["reason"])
        else:
            check(True, "engellenen-asama-GEREKCE-bildiriyor", "engel yok")

        # Nothing anywhere in the surface may leak a credential or host path.
        status, view = call(api, "GET", f"{base}/turbo")
        blob = json.dumps(view, ensure_ascii=False)
        check("FAKE-STOCK" not in blob and "fake-" not in blob.lower(),
              "gorunumde-kimlik-bilgisi-YOK")
        check(str(root) not in blob, "gorunumde-mutlak-yol-YOK")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ui_wiring():
    section("E. Arayuz baglantisi (statik)")
    app_js = Path("creator_console/static/app.js").read_text(encoding="utf-8")
    check("turbo-screen" in app_js, "app.js-turbo-ekrani-var")
    check("'turbo'" in app_js or '"turbo"' in app_js, "app.js-turbo-sekmesi-var")
    check("screenTurbo" in app_js, "app.js-turbo-render-fonksiyonu-var")

    # A route with no control is a feature nobody can reach. Every action route
    # must be called from somewhere in the screen.
    for route in ("/brief", "/stage", "/approve", "/media", "/music",
                  "/render", "/revision"):
        called = (f"turboPost('{route}'" in app_js
                  or f"/turbo{route}" in app_js)
        check(called, f"arayuz-{route}-rotasini-cagiriyor")

    for testid in ("turbo-save-brief", "turbo-run-stage", "turbo-media-plan",
                   "turbo-music-none", "turbo-render-preview-btn",
                   "turbo-render-final-btn", "turbo-caption-theme"):
        check(f"'{testid}'" in app_js, f"kontrol-var:{testid}")

    # The theme list comes from the server, never a second copy in JS: a
    # hardcoded option could offer a look `validate_brief` refuses.
    check("options.caption_themes" in app_js,
          "tema-secenekleri-SUNUCUDAN-geliyor",
          "app.js kendi tema listesini tutmamali")
    for theme in render_utils.CAPTION_THEME_PROFILES:
        check(f"'{theme}'" not in app_js.split("function turboBriefCard")[1][:2000],
              f"tema-{theme}-arayuze-GOMULMEMIS")

    # The Phase 1 placeholder must be gone now that the controls are real.
    check("turbo-pending" not in app_js,
          "faz-1-'henuz-baglanmadi'-karti-KALDIRILDI")
    check("sonraki fazlarda baglanacak" not in app_js,
          "bayat-'sonraki-fazlarda'-metni-yok")

    # Disabled controls must carry a reason, not just be dead.
    check("title: disabled ? reason" in app_js,
          "kapali-kontrol-gerekce-tasiyor")


def main():
    print("=" * 74)
    print("TURBO STUDIO DOGRULAMA (sahte saglayicilar; hicbir gercek cagri yok)")
    print("=" * 74)
    test_stage_machine()
    test_brief_validation()
    test_revision_matrix()
    test_console_integration()
    test_script_and_tts()
    test_tts_resumability()
    test_script_revision_preserves_audio()
    test_stock_media_stage()
    test_subtitles_stage()
    test_render_helpers()
    test_timeline_packing()
    test_timeline_revisions()
    test_real_render()
    test_music_licensing()
    test_render_gating()
    test_env_contract()
    test_revision_application()
    test_api_routes()
    test_ui_wiring()
    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - FAILURES} gecti, {FAILURES} basarisiz")
    if FAILURES:
        print("TURBO_STUDIO_BASARISIZ")
        return 1
    print("TURBO_STUDIO_OK (sahte saglayici ikizleri; HICBIR gercek API cagrilmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
