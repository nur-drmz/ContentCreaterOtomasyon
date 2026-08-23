#!/usr/bin/env python
"""Altyazı düzenleme, sözlük ve revizyon geçmişi doğrulaması.

    python verify_transcript_editor.py

NE KANITLIYOR
-------------
Altyazı ekranı yalnızca bir özet gösteriyordu: kelime sayısı, dil, güven skoru.
Metnin kendisi görünmüyordu, dolayısıyla yanlış duyulan bir özel isim ("Rainer"
yerine "Rhaena") videoya gömülmeden önce görülemiyor, düzeltilemiyordu. Tek
düzeltme kanalı kelime-kelime bir değiştirme tablosuydu ve o tablo
"Steelers Rider!" -> "Sheepstealer's rider" gibi kelime sayısı değişen bir
düzeltmeyi yapamıyordu.

Bu paket üç iddiayı denetler:

  1. Tam transkript API'den geliyor, düzenlenebiliyor, kalıcı oluyor ve
     düzenleme ZAMAN DAMGALARINI bozmuyor.
  2. Ham ASR çıktısı asla üzerine yazılmıyor. Eski düzeltme yolu tam tersini
     yapıyordu: `artifact["words"]` zaten düzeltilmiş metni taşıdığı için bir
     düzeltme geri alınamıyordu ve ilk düzeltmeden sonra modelin ne söylediği
     kayboluyordu -- docstring'i "her zaman orijinale uygulanır" dese de.
  3. Sözlük terimleri transkripsiyon ONCESINDE modele bağlam olarak veriliyor
     (faster-whisper 1.2.1 `hotwords`), sonrasında kör değiştirme yapılmıyor.

GÜVENLİK
--------
İzole geçici depolama, sahte sağlayıcılar, sıfır gerçek API çağrısı, hiçbir şey
yüklenmez/yayınlanmaz.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.server import ConsoleServer  # noqa: E402
from local_pipeline import approval, glossary, render_utils, transcript_edit  # noqa: E402

FAILURES: list = []
CHECKS = 0
TMP: list = []

SECRET_TEXT = "Rainer flew north Steelers Rider"

RENDER_CONFIG = {"render_mode": "blurred_background", "preview_mode": True,
                 "caption_style": "active_word_yellow", "caption_theme": "auto",
                 "subtitle_whisper_model_size": "tiny"}


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


def make_app(root):
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))


def build_project(app, api, *, title):
    """A project with an APPROVED direct_clip_series plan and a selected moment.

    Needed in full because the caption screen and the render gate both branch on
    the approved plan: without one the UI shows Script Studio instead of the
    transcript, and the render refuses for a Phase 0 reason before it ever
    reaches the transcript check -- which would make check 5 prove nothing.
    """
    render_config = dict(RENDER_CONFIG)
    pid = fixtures.seed_demo_project(app, title=title)["project_id"]
    service = app.service(app.ownership(pid))
    call(api, "POST", f"/api/projects/{pid}/plan",
         {"mode": "direct_clip_series", "selected_by": "verify",
          "render_config": render_config})
    call(api, "POST", f"/api/projects/{pid}/approvals/processing_plan",
         {"reviewed_sha256": service.describe_plan_review()["review"]["content_sha256"],
          "approved_by": "verify"})
    call(api, "POST", f"/api/projects/{pid}/jobs",
         {"job_type": "video_analysis",
          "params": {"whisper_model_size": "tiny", "clip_duration_sec": 4, "top_n": 1}})
    hot = service.load_artifact("hot_moments")["data"]
    service.record_moment_selection(
        selected_rank=hot["hot_moments"][0]["rank"], reviewed_by="verify",
        require_preview=False)
    return pid, service


def seed_transcript(service, *, actor="verify"):
    """A transcript with the two real misrecognitions this work exists for."""
    words = [
        {"text": "Rainer", "start": 0.0, "end": 0.5, "probability": 0.41},
        {"text": "flew", "start": 0.5, "end": 0.9, "probability": 0.95},
        {"text": "north", "start": 0.9, "end": 1.3, "probability": 0.92},
        {"text": "Steelers", "start": 2.0, "end": 2.6, "probability": 0.38},
        {"text": "Rider!", "start": 2.6, "end": 3.2, "probability": 0.44},
    ]
    cues = [{"start": 0.0, "end": 1.3, "text": "Rainer flew north"},
            {"start": 2.0, "end": 3.2, "text": "Steelers Rider!"}]
    artifact = render_utils.build_moment_transcript_artifact(
        words, cues, source="faster-whisper-word-alignment", language="en",
        moment_rank=1, moment_selection_sha256="seed", whisper_model_size="tiny",
        initial_prompt_used=False)
    service.write_artifact("moment_transcript", artifact, actor=actor)
    return artifact


class _FakeSegment:
    def __init__(self, start, end, text, words):
        self.start, self.end, self.text, self.words = start, end, text, words


class _FakeWord:
    def __init__(self, word, start, end, probability):
        self.word, self.start, self.end, self.probability = word, start, end, probability


def install_fake_whisper(captured):
    """Replace faster_whisper with a recorder.

    The point of check 6 is that the glossary REACHES the model call. Running a
    real model would prove nothing extra about the wiring and would make the
    check depend on what a 39 MB model happens to hear.
    """
    module = types.ModuleType("faster_whisper")

    class FakeModel:
        def __init__(self, size, **kwargs):
            captured["model_size"] = size
            captured["model_kwargs"] = kwargs

        def transcribe(self, path, **kwargs):
            captured["transcribe_kwargs"] = kwargs
            words = [_FakeWord("Rhaena", 0.0, 0.5, 0.94),
                     _FakeWord("flew", 0.5, 0.9, 0.96)]
            return iter([_FakeSegment(0.0, 0.9, "Rhaena flew", words)]), object()

    module.WhisperModel = FakeModel
    previous = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = module
    return previous


def main() -> int:
    # ============================================ 1. saf duzenleme mantigi
    section("1. Segment modeli, zamanlama korunumu ve dogrulama")
    words = [{"text": "Steelers", "start": 2.0, "end": 2.6, "probability": 0.38},
             {"text": "Rider!", "start": 2.6, "end": 3.2, "probability": 0.44}]
    cues = [{"start": 2.0, "end": 3.2, "text": "Steelers Rider!"}]
    base = transcript_edit.ensure_v2({"words": words, "cues": cues})

    segments = transcript_edit.build_segments(base, low_confidence_threshold=0.6)
    check(len(segments) == 1 and segments[0]["id"] == "seg_0001", "segment-kimligi")
    check(segments[0]["start_sec"] == 2.0 and segments[0]["end_sec"] == 3.2,
          "segment-zaman-damgalari")
    check(segments[0]["average_probability"] == 0.41, "segment-ortalama-guven",
          str(segments[0]["average_probability"]))
    check(segments[0]["low_confidence_count"] == 2, "segment-dusuk-guvenli-kelimeler")

    same = transcript_edit.apply_segment_texts(base, [("seg_0001", "Sheepstealer's rider")])
    check([(w["start"], w["end"]) for w in same["words"]] == [(2.0, 2.6), (2.6, 3.2)],
          "ayni-kelime-sayisi-zamanlamayi-KORUR",
          "hizalama hayattayken tahmine gecmek vurguyu sesten ayirir")
    check(all(w.get("probability") is None for w in same["words"]),
          "duzeltilen-kelime-probability-None",
          "insanin yazdigi kelimeye ASR guveni atfetmek yaniltir")
    # Clearing the field is only half the contract: the CONSUMER must stop
    # counting it. Reading None as 0.0 kept every hand-fixed word flagged
    # "unsure" and dragged the segment average to zero, so correcting a name
    # made the transcript look worse than leaving the misrecognition in.
    same_segments = transcript_edit.build_segments(same, low_confidence_threshold=0.6)
    check(same_segments[0]["low_confidence_count"] == 0,
          "duzeltilen-kelime-DUSUK-GUVENLI-SAYILMAZ",
          str(same_segments[0]["low_confidence_words"]))
    check(same_segments[0]["average_probability"] is None,
          "elle-yazilan-segmentin-ortalamasi-0.0-DEGIL",
          str(same_segments[0]["average_probability"]))

    # Mixed segment: one hand-fixed word must not drag the surviving ASR word's
    # average down, and must not be listed as low confidence next to it.
    mixed_base = transcript_edit.ensure_v2({
        "words": [{"text": "Rainer", "start": 0.0, "end": 0.5, "probability": 0.41},
                   {"text": "flew", "start": 0.5, "end": 0.9, "probability": 0.95}],
        "cues": [{"start": 0.0, "end": 0.9, "text": "Rainer flew"}]})
    mixed = transcript_edit.apply_segment_texts(mixed_base, [("seg_0001", "Rhaenyra flew")])
    mixed_segments = transcript_edit.build_segments(mixed, low_confidence_threshold=0.6)
    check(mixed_segments[0]["average_probability"] == 0.95,
          "karisik-segment-ortalamasi-yalnizca-ASR-kelimelerinden",
          str(mixed_segments[0]["average_probability"]))
    check(mixed_segments[0]["low_confidence_count"] == 0,
          "karisik-segmentte-elle-yazilan-kelime-sayilmaz",
          str(mixed_segments[0]["low_confidence_words"]))

    grown = transcript_edit.apply_segment_texts(base, [("seg_0001", "the rider of Sheepstealer")])
    check(len(grown["words"]) == 4, "kelime-sayisi-degisebilir")
    check(grown["words"][0]["start"] == 2.0 and grown["words"][-1]["end"] == 3.2,
          "segment-siniri-degismedi", "yeniden dagitim segmentin disina tasmamali")
    check(all(w.get("retimed") for w in grown["words"]), "tahmini-zamanlama-isaretlendi",
          "tahmini bir zamanlamayi hizalama gibi gostermek sessiz bir yalan olur")
    check(grown["cues"][0]["start"] == 2.0 and grown["cues"][0]["end"] == 3.2,
          "cue-zamani-degismedi")

    problems = transcript_edit.validate_segment_texts(
        [("seg_0001", "  "), ("seg_0002", "x" * 300), ("seg_0003", "a\nb")])
    check(any("bos olamaz" in p for p in problems), "dogrulama-bos-altyazi")
    check(any("cok uzun" in p for p in problems), "dogrulama-uzun-satir")
    check(any("tek satir" in p for p in problems), "dogrulama-gecersiz-segment")
    try:
        transcript_edit.apply_segment_texts(base, [("seg_4242", "x")])
        check(False, "dogrulama-bilinmeyen-segment", "kabul edildi")
    except transcript_edit.TranscriptEditError as exc:
        check("seg_4242" in str(exc), "dogrulama-bilinmeyen-segment", str(exc)[:40])
    try:
        transcript_edit.parse_full_text(base, "bir\niki\nuc", low_confidence_threshold=0.6)
        check(False, "tam-metin-satir-sayisi-korunur", "3 satir 1 segmente kabul edildi")
    except transcript_edit.TranscriptEditError:
        check(True, "tam-metin-satir-sayisi-korunur",
              "satir eklemek zaman damgalarini uydurmak demek olurdu")

    hidden = transcript_edit.apply_segment_exclusions(
        base, ["seg_0001"], excluded=True)
    check(hidden["cues"][0].get("excluded_from_subtitles") is True,
          "sarki-segmenti-cue-dan-SILINMEDEN-gizleniyor")
    check(all(word.get("excluded_from_subtitles") for word in hidden["words"]),
          "sarki-segmenti-dinamik-kelimelerden-de-gizleniyor")
    check(hidden["cues"][0]["text"] == "Steelers Rider!",
          "gizlemek-ham-metni-YOK-ETMIYOR")
    check(approval.content_hash_for("transcript", {"words": hidden["words"],
                                                    "cues": hidden["cues"]})
          != approval.content_hash_for("transcript", base),
          "gizleme-karari-onay-hashine-bagli")

    # ================================================ 7. sozluk onerileri
    section("7. Sozluk eslesmeleri (gercek yanlis duymalar)")
    book = glossary.set_terms(glossary.new_glossary(),
                              ["Rhaena", "Sheepstealer", "Sheepstealer's rider", "Rhaenyra"],
                              actor="verify")
    found = glossary.suggest_for_text(
        "Rainer flew to the wall and the Steelers Rider! followed. "
        "Rhaenyra waited for the dragon.", book)
    mapped = {item["from"]: item["to"] for item in found}
    check(mapped.get("Rainer") == "Rhaena", "oneri-Rainer-Rhaena",
          f"{mapped.get('Rainer')!r} (Rhaenyra'ya gitmemeli)")
    check(mapped.get("Steelers Rider") == "Sheepstealer's rider",
          "oneri-SteelersRider-Sheepstealers-rider", str(mapped.get("Steelers Rider")))
    check("Rhaenyra" not in mapped and "Rhaenyra waited" not in mapped,
          "dogru-isim-bozulmuyor", "bir gercek isim baska bir gercek isme cevrilmemeli")
    check("dragon" not in mapped, "siradan-kelime-onerilmiyor", str(mapped))
    check(all(item["kind"] == "suggested" for item in found
              if item["from"] in ("Rainer", "Steelers Rider")),
          "belirsiz-eslesme-OTOMATIK-uygulanmaz",
          "kor degistirme Rhaenyra'yi Rhaena yapan hatadir")
    certain = glossary.suggest_for_text("the sheepstealer arrived", book)
    check(certain and certain[0]["kind"] == "certain"
          and certain[0]["to"] == "Sheepstealer",
          "buyuk-kucuk-harf-farki-kesin-sayilir", str(certain[:1]))
    learned = glossary.suggest_terms_from_correction(
        "Steelers Rider!", "Sheepstealer's rider", glossary.new_glossary())
    check([item["term"] for item in learned] == ["Sheepstealer's rider"],
          "duzeltmeden-yeni-terim-onerisi", str(learned))
    check(glossary.suggest_terms_from_correction("teh", "the", glossary.new_glossary()) == [],
          "yazim-hatasi-terim-sayilmaz")

    # ==================================== 6/8. ASR baglami ve sozluksuz akis
    section("6+8. Sozluk ASR cagrisina ulasiyor; sozluksuz akis bozulmuyor")
    captured: dict = {}
    previous_module = install_fake_whisper(captured)
    try:
        render_utils.transcribe_for_dynamic_captions(
            "clip.mp4", "", 42, whisper_model_size="tiny", language="en",
            initial_prompt="tema ipucu", hotwords="Rhaena, Sheepstealer")
        kwargs = captured.get("transcribe_kwargs") or {}
        check(kwargs.get("hotwords") == "Rhaena, Sheepstealer",
              "sozluk-hotwords-olarak-modele-gidiyor", str(kwargs.get("hotwords")))
        check(kwargs.get("initial_prompt") == "tema ipucu",
              "tema-ipucu-initial_prompt-olarak-ayri-kaliyor",
              "ikisini birlestirmek isim listesini ilk pencereye hapsederdi")

        captured.clear()
        render_utils.transcribe_for_dynamic_captions(
            "clip.mp4", "", 42, whisper_model_size="tiny", language="en")
        kwargs = captured.get("transcribe_kwargs") or {}
        check(kwargs.get("hotwords") is None, "sozluk-yokken-hotwords-None",
              "bos string gondermek modele bos bir baglam enjekte ederdi")
        check("hotwords" in kwargs, "hotwords-parametresi-gercekten-destekleniyor",
              "faster-whisper 1.2.1 imzasindan dogrulandi, varsayilmadi")
    finally:
        if previous_module is not None:
            sys.modules["faster_whisper"] = previous_module
        else:
            sys.modules.pop("faster_whisper", None)

    empty_context = glossary.asr_context(glossary.new_glossary())
    check(empty_context["hotwords"] is None and empty_context["term_count"] == 0,
          "sozluksuz-baglam-bos", str(empty_context["hotwords"]))

    # ============================================ 2/3/4/5/10/11/12 konsol
    section("2-5, 10-12. Konsol: kaydetme, onay, downstream, geriye uyumluluk")
    root = Path(tempfile.mkdtemp(prefix="transcript_editor_"))
    TMP.append(root)
    app = make_app(root)
    api = ConsoleApi(app)
    pid, service = build_project(app, api, title="Altyazi duzenleyici")
    # The pipeline is real up to this point; the transcript CONTENT is seeded so
    # the two misrecognitions this work exists for are actually present. The
    # fixture clip is silent, so a real recognition pass would give nothing to
    # edit and the checks below would prove nothing.
    seed_transcript(service)

    status, payload = call(api, "GET", f"/api/projects/{pid}/transcript")
    view = payload["transcript"]
    check(status == 200 and len(view["segments"]) == 2, "1-api-tam-transkripti-donuyor",
          f"HTTP {status}, {len(view.get('segments') or [])} segment")
    first = view["segments"][0]
    check(all(key in first for key in
              ("start_sec", "end_sec", "text", "average_probability",
               "low_confidence_words")),
          "segment-alanlari-tam", str(sorted(first)))
    check(view["full_text"] == "Rainer flew north\nSteelers Rider!",
          "tam-metin-alani", repr(view["full_text"]))

    before_hash = service.approval_state("transcript", 1).get("content_sha256")
    status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/moment_transcript",
                     {"reviewed_sha256": before_hash, "approved_by": "verify"})
    if status != 200:
        status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/transcript",
                         {"reviewed_sha256": before_hash, "approved_by": "verify"})
    approved_before = service.approval_state("transcript", 1)
    check(approved_before.get("approved") is True, "on-kosul-transkript-onaylandi",
          f"HTTP {status} {approved_before.get('reason')}")

    status, payload = call(api, "POST", f"/api/projects/{pid}/transcript/segments",
                           {"segments": [{"id": "seg_0002",
                                          "text": "Sheepstealer's rider"}]})
    check(status == 200, "2-segment-kaydedildi", f"HTTP {status}")
    after = service.approval_state("transcript", 1)
    check(after.get("approved") is False or after.get("valid") is False,
          "4-duzenleme-onceki-onayi-DUSURUR",
          f"approved={after.get('approved')} valid={after.get('valid')}")
    check(after.get("content_sha256") != before_hash, "4-icerik-hashi-degisti")

    _s, payload = call(api, "GET", f"/api/projects/{pid}/transcript")
    reread = payload["transcript"]
    check([s["text"] for s in reread["segments"]]
          == ["Rainer flew north", "Sheepstealer's rider"],
          "3-yenilemeden-sonra-kalici", str([s["text"] for s in reread["segments"]]))
    check(reread["revision"] == 1
          and reread["revisions"][-1]["changed_segment_ids"] == ["seg_0002"],
          "revizyon-kaydi", json.dumps(reread["revisions"][-1]))
    check(reread["revisions"][-1]["previous_content_sha256_16"] == (before_hash or "")[:16],
          "revizyon-onceki-hashe-zincirli")
    check(reread["revisions"][-1]["actor"], "revizyon-aktoru-kayitli",
          str(reread["revisions"][-1]["actor"]))

    disk = json.loads((Path(service.path("moment_transcript"))).read_text(encoding="utf-8"))
    check(disk["asr"]["text"] == "Rainer flew north Steelers Rider!",
          "12-ham-ASR-korunuyor", disk["asr"]["text"])
    check(disk["text"] == "Rainer flew north Sheepstealer's rider",
          "12-guncel-metin-ayri", disk["text"])
    edited = [w for w in disk["words"] if w["start"] >= 2.0]
    check([(w["start"], w["end"]) for w in edited] == [(2.0, 2.6), (2.6, 3.2)],
          "duzenleme-kelime-zamanlamalarini-korudu", str(edited))

    status, payload = call(api, "POST", f"/api/projects/{pid}/transcript/restore-asr", {})
    check(status == 200 and payload["transcript"]["segments"][1]["text"] == "Steelers Rider!",
          "12-ham-ASR-geri-yuklenebilir",
          str(payload.get("transcript", {}).get("segments", [{}, {}])[1].get("text")))
    call(api, "POST", f"/api/projects/{pid}/transcript/segments",
         {"segments": [{"id": "seg_0002", "text": "Sheepstealer's rider"}]})

    # --- legacy /transcript/corrections must keep the v2 fields -------------
    # The word-level path predates revisions. Rebuilding without `asr` made the
    # builder seed a fresh snapshot from the ALREADY-CORRECTED words, so "restore
    # the raw recognition" would hand back the corrections -- the one escape
    # hatch a reviewer has, quietly welded shut.
    legacy_root = Path(tempfile.mkdtemp(prefix="transcript_legacy_"))
    TMP.append(legacy_root)
    legacy_app = make_app(legacy_root)
    legacy_api = ConsoleApi(legacy_app)
    legacy_pid, legacy_svc = build_project(legacy_app, legacy_api, title="Eski duzeltme yolu")
    seed_transcript(legacy_svc)
    # One segment edit first, so there is real v2 history to lose.
    call(legacy_api, "POST", f"/api/projects/{legacy_pid}/transcript/segments",
         {"segments": [{"id": "seg_0001", "text": "Rhaena flew north"}]})
    pre = legacy_svc.load_artifact("moment_transcript")["data"]
    pre_asr_text = ((pre.get("asr") or {}).get("text") or "")
    pre_revisions = len(pre.get("revisions") or [])
    pre_edited = list(pre.get("edited_segment_ids") or [])
    check(bool(pre_asr_text) and pre_revisions >= 1 and pre_edited == ["seg_0001"],
          "eski-yol-on-kosul",
          f"asr={bool(pre_asr_text)} rev={pre_revisions} edited={pre_edited}")

    status, payload = call(legacy_api, "POST",
                           f"/api/projects/{legacy_pid}/transcript/corrections",
                           {"corrections": [{"from": "Steelers", "to": "Sheepstealer"}],
                            "edited_by": "verify"})
    check(status == 200, "eski-yol-hala-calisiyor", f"HTTP {status}")
    post = legacy_svc.load_artifact("moment_transcript")["data"]
    check(((post.get("asr") or {}).get("text") or "") == pre_asr_text,
          "eski-yol-HAM-ASR-i-KORUYOR",
          f"{((post.get('asr') or {}).get('text') or '')!r} != {pre_asr_text!r}")
    check(not (post.get("asr") or {}).get("reconstructed"),
          "eski-yol-asr-i-yeniden-uydurmadi",
          str((post.get("asr") or {}).get("reconstructed")))
    check(len(post.get("revisions") or []) > pre_revisions,
          "eski-yol-revizyon-gecmisini-KORUYOR-ve-kendini-EKLIYOR",
          f"{pre_revisions} -> {len(post.get('revisions') or [])}")
    check(list(post.get("edited_segment_ids") or []) == pre_edited,
          "eski-yol-duzenlenmis-segment-kumesini-KORUYOR",
          str(post.get("edited_segment_ids")))
    check("Sheepstealer" in (post.get("text") or ""),
          "eski-yol-duzeltmeyi-yine-de-UYGULUYOR", str(post.get("text"))[:80])
    # And the escape hatch still works after the legacy path ran.
    status, payload = call(legacy_api, "POST",
                           f"/api/projects/{legacy_pid}/transcript/restore-asr", {})
    restored = payload.get("transcript", {}).get("full_text", "")
    # Must contain the word the recogniser ACTUALLY heard ("Rainer"), not the
    # corrected one: asserting only the untouched second line let a broken
    # rebuild pass, because that line was never corrected in the first place.
    check(status == 200 and "Rainer flew north" in restored
          and "Steelers Rider!" in restored,
          "eski-yol-sonrasi-ham-ASR-GERI-YUKLENEBILIYOR", repr(restored)[:90])

    # --- re-transcription must not silently eat the manual edits -------------
    # Its OWN project: a re-run replaces the transcript wholesale, so doing this
    # on the shared fixture would pull the rug from the downstream checks below.
    retr_root = Path(tempfile.mkdtemp(prefix="transcript_retranscribe_"))
    TMP.append(retr_root)
    retr_app = make_app(retr_root)
    retr_api = ConsoleApi(retr_app)
    retr_pid, retr_svc = build_project(retr_app, retr_api, title="Yeniden transkripsiyon")
    seed_transcript(retr_svc)
    call(retr_api, "POST", f"/api/projects/{retr_pid}/transcript/segments",
         {"segments": [{"id": "seg_0002", "text": "Sheepstealer's rider"}]})
    before = retr_svc.load_artifact("moment_transcript")["data"]
    before_edited = list(before.get("edited_segment_ids") or [])
    check(before_edited == ["seg_0002"], "yeniden-transkripsiyon-on-kosul",
          str(before_edited))

    def _retranscribe(app_api, project_id):
        previous_mod = install_fake_whisper({})
        try:
            return call(app_api, "POST", f"/api/projects/{project_id}/jobs",
                        {"job_type": "transcript",
                         "params": {"subtitle_whisper_model_size": "tiny",
                                     "subtitle_language": "en"}})
        finally:
            if previous_mod is None:
                sys.modules.pop("faster_whisper", None)
            else:
                sys.modules["faster_whisper"] = previous_mod

    status, payload = _retranscribe(retr_api, retr_pid)
    job = (payload or {}).get("job") or {}
    check(job.get("status") == "review_required", "yeniden-transkripsiyon-isi-basarili",
          f"HTTP {status} {job.get('status')}: {str(job.get('error'))[:160]}")

    after = retr_svc.load_artifact("moment_transcript")["data"] or {}
    # Archiving MOVES the old file, so a crash between the move and the write
    # would leave the slot empty. Prove the replacement actually landed.
    check(bool(after.get("words")), "yeniden-transkripsiyon-artefakti-YERINDE",
          "arsivleme sonrasi transcript ortada kalmis")
    replaced = after.get("replaced_manual_edit") or {}
    check(bool(replaced), "yeniden-transkripsiyon-SESSIZ-DEGIL",
          "elle duzenleme ustune yazildi ama artefakt bunu bildirmiyor")
    check(replaced.get("edited_segment_ids") == before_edited,
          "arsiv-kaydi-hangi-segmentlerin-dustugunu-soyluyor", str(replaced))

    archived_revisions = [r for r in retr_svc.revisions(limit=100)
                          if r.get("action") == "retranscribe"]
    check(len(archived_revisions) == 1, "eski-surum-ARSIVLENDI",
          str([r.get("action") for r in retr_svc.revisions(limit=100)]))
    check(archived_revisions[0]["revision_id"] == replaced.get("revision_id"),
          "artefakt-arsivi-isaret-ediyor",
          f"{replaced.get('revision_id')} != {archived_revisions[0]['revision_id']}")

    # An archive nobody can read back is the same as a delete.
    archived_path = retr_svc.storage.resolve(
        retr_svc.ownership, ".revisions", archived_revisions[0]["revision_id"],
        retr_svc.artifact_relative("moment_transcript", 1))
    check(archived_path.is_file(), "arsivlenen-dosya-diskte", str(archived_path))
    archived_data = json.loads(archived_path.read_text(encoding="utf-8"))
    check(archived_data.get("text") == before.get("text"),
          "arsivlenen-icerik-onceki-surumun-AYNISI",
          repr(archived_data.get("text"))[:80])

    # A re-run over pure recogniser output must stay quiet: ceremony for nothing
    # trains the user to ignore the warning that matters.
    clean_root = Path(tempfile.mkdtemp(prefix="transcript_clean_rerun_"))
    TMP.append(clean_root)
    clean_app = make_app(clean_root)
    clean_api = ConsoleApi(clean_app)
    clean_pid, clean_svc = build_project(clean_app, clean_api, title="Duzenlenmemis")
    seed_transcript(clean_svc)
    _retranscribe(clean_api, clean_pid)
    clean_after = clean_svc.load_artifact("moment_transcript")["data"] or {}
    check(not clean_after.get("replaced_manual_edit"),
          "elle-emek-yokken-ARSIV-UYARISI-YOK", str(clean_after.get("replaced_manual_edit")))
    check(not [r for r in clean_svc.revisions(limit=100)
               if r.get("action") == "retranscribe"],
          "elle-emek-yokken-yeni-arsiv-YOK", "gereksiz arsiv olusturuldu")

    # 5. downstream: the render refuses while the transcript approval is stale.
    # The render must refuse for the TRANSCRIPT's sake, so it is called with the
    # approved plan's own settings -- otherwise config drift rejects it first and
    # the check would pass without proving anything.
    render_params = dict(RENDER_CONFIG)
    render_params.pop("preview_mode", None)
    try:
        service.run_render(actor="verify", slot=1, preview_mode=True, **render_params)
        check(False, "5-render-onaysiz-transkripti-reddeder", "render calisti")
    except Exception as exc:  # noqa: BLE001 - the refusal itself is the point
        message = str(exc).lower()
        check("transkript" in message or "altyazi" in message,
              "5-render-onaysiz-transkripti-reddeder", str(exc)[:100])
    # ... and it runs again once the NEW revision is approved, so "use the latest
    # approved revision" is a working state, not merely a locked one.
    fresh = service.approval_state("transcript", 1)["content_sha256"]
    status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/transcript",
                     {"reviewed_sha256": fresh, "approved_by": "verify"})
    check(status == 200 and service.approval_state("transcript", 1)["valid"],
          "5-yeni-revizyon-onaylanabiliyor", f"HTTP {status}")
    consumed = service.load_artifact("moment_transcript", slot=1)["data"]
    check(consumed["text"] == "Rainer flew north Sheepstealer's rider",
          "5-downstream-EN-SON-revizyonu-okuyor", consumed["text"])

    # Song/lyrics handling is an explicit reversible review decision.  Hide
    # every cue to prove the renderer emits a caption-free video instead of
    # crashing on an empty SRT/ASS input.
    status, payload = call(api, "POST", f"/api/projects/{pid}/transcript/exclusions",
                           {"segment_ids": ["seg_0001", "seg_0002"],
                            "excluded": True})
    hidden_view = payload.get("transcript") or {}
    check(status == 200 and hidden_view.get("excluded_cue_count") == 2,
          "sarki-satirlari-api-ile-gizleniyor", f"HTTP {status}")
    hidden_sha = service.approval_state("transcript", 1)["content_sha256"]
    call(api, "POST", f"/api/projects/{pid}/approvals/transcript",
         {"reviewed_sha256": hidden_sha, "approved_by": "verify"})
    service.run_render(actor="verify", slot=1, preview_mode=True, **render_params)
    hidden_manifest = service.load_artifact("render_manifest", slot=1)["data"] or {}
    check((hidden_manifest.get("subtitles") or {}).get("all_subtitles_suppressed") is True,
          "tum-sarki-satirlari-gizliyken-render-COKMUYOR")
    check((hidden_manifest.get("subtitles") or {}).get("format_used_for_render") == "none",
          "bos-altyazi-filtresi-yerine-guvenli-null-video-filtresi")
    status, payload = call(api, "POST", f"/api/projects/{pid}/transcript/exclusions",
                           {"segment_ids": ["seg_0001", "seg_0002"],
                            "excluded": False})
    check(status == 200 and payload["transcript"]["excluded_cue_count"] == 0,
          "gizlenen-sarki-satirlari-geri-acilabiliyor")
    restored_sha = service.approval_state("transcript", 1)["content_sha256"]
    call(api, "POST", f"/api/projects/{pid}/approvals/transcript",
         {"reviewed_sha256": restored_sha, "approved_by": "verify"})

    # A real render, then an edit underneath it. Editing CLEARS the transcript
    # approval rather than invalidating it, so without the recorded digest the
    # chain would see no stale upstream approval and the finished video would
    # keep looking current while showing the old words.
    service.run_render(actor="verify", slot=1, preview_mode=True, **render_params)
    manifest = service.load_artifact("render_manifest", slot=1)["data"] or {}
    stamped = (manifest.get("subtitles") or {}).get("transcript_content_sha256")
    check(bool(stamped), "5-manifest-hangi-revizyondan-uretildigini-kaydediyor",
          str(stamped)[:16])
    check(not (service.approval_chain(1).get("render") or {}).get("upstream_invalid"),
          "5-render-taze-transkriptle-temiz",
          json.dumps((service.approval_chain(1).get("render") or {}).get("upstream_invalid")))
    call(api, "POST", f"/api/projects/{pid}/transcript/segments",
         {"segments": [{"id": "seg_0001", "text": "Rhaena flew north"}]})
    stale = (service.approval_chain(1).get("render") or {}).get("upstream_invalid") or []
    check(any("transcript" in item for item in stale),
          "C-onayli-transkript-duzenlenince-render-BAYAT-isaretleniyor",
          json.dumps(stale)[:120])

    # 11. an old project with no revision metadata stays readable.
    legacy = {
        "schema_version": 1, "artifact_type": "moment_transcript",
        "status": "review_required", "not_uploaded": True,
        "words": [{"text": "Rainer", "start": 0.0, "end": 0.4, "probability": 0.4}],
        "cues": [{"start": 0.0, "end": 0.4, "text": "Rainer"}],
        "text": "Rainer", "word_count": 1, "cue_count": 1, "has_speech": True,
    }
    legacy_root = Path(tempfile.mkdtemp(prefix="transcript_legacy_"))
    TMP.append(legacy_root)
    legacy_app = make_app(legacy_root)
    legacy_api = ConsoleApi(legacy_app)
    legacy_pid = fixtures.seed_demo_project(legacy_app, title="Eski proje")["project_id"]
    legacy_service = legacy_app.service(legacy_app.ownership(legacy_pid))
    legacy_service.write_artifact("moment_transcript", legacy, actor="verify")
    status, payload = call(legacy_api, "GET", f"/api/projects/{legacy_pid}/transcript")
    legacy_view = payload["transcript"]
    check(status == 200 and legacy_view["segments"][0]["text"] == "Rainer",
          "11-revizyonsuz-eski-proje-okunabilir", f"HTTP {status}")
    check(legacy_view["asr_reconstructed"] is True, "11-ham-ASR-yeniden-kurgulandi-diyor",
          "eski projede modelin ne dedigi bilinmiyor; oyle iddia edilmemeli")
    check(legacy_view["revision"] == 0 and legacy_view["revisions"] == [],
          "11-eski-projeye-sahte-gecmis-uydurulmuyor")
    on_disk = json.loads(Path(legacy_service.path("moment_transcript")).read_text("utf-8"))
    check(on_disk.get("schema_version") == 1,
          "11-okumak-eski-artefakti-DISKTE-degistirmiyor", str(on_disk.get("schema_version")))
    status, _ = call(legacy_api, "POST", f"/api/projects/{legacy_pid}/transcript/segments",
                     {"segments": [{"id": "seg_0001", "text": "Rhaena"}]})
    upgraded = json.loads(Path(legacy_service.path("moment_transcript")).read_text("utf-8"))
    check(status == 200 and upgraded.get("schema_version") == 2,
          "11-ancak-duzenleyince-v2-ye-tasiniyor", f"HTTP {status}")

    # 10. logs must not carry the transcript or the glossary.
    call(api, "POST", f"/api/projects/{pid}/glossary",
         {"terms": ["Rhaena", "Sheepstealer's rider"]})
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        call(api, "POST", f"/api/projects/{pid}/transcript/segments",
             {"segments": [{"id": "seg_0001", "text": SECRET_TEXT}]})
        call(api, "POST", f"/api/projects/{pid}/transcript/segments",
             {"segments": [{"id": "seg_0001", "text": ""}]})
    logged = buffer.getvalue()
    check(SECRET_TEXT not in logged, "10-transkript-metni-loga-sizmiyor", logged[:90])
    check("Sheepstealer's rider" not in logged, "10-sozluk-terimi-loga-sizmiyor",
          logged[:90])
    audit = json.dumps(service.audit.events(limit=200))
    check(SECRET_TEXT not in audit, "10-transkript-metni-audit-e-sizmiyor")
    check("Rhaena" not in audit, "10-sozluk-terimi-audit-e-sizmiyor", audit[-120:])
    check("changed_segments" in audit, "10-audit-yine-de-NE-degistigini-soyluyor",
          "gizlemek ile hicbir sey kaydetmemek ayni sey degil")

    glossary_view = service.glossary_view()
    check(glossary_view["term_count"] == 2, "sozluk-kalici", str(glossary_view["term_count"]))
    suggested_terms = [item.get("term") for item in (glossary_view.get("suggestions") or [])]
    check(bool(suggested_terms), "manuel-duzeltme-terim-onerisi-uretti",
          json.dumps(suggested_terms)[:120])
    # The whole point of the phrase case: "Steelers Rider!" -> "Sheepstealer's
    # rider" teaches a TWO-WORD name. Proposing only "Sheepstealer's" loses the
    # half that makes it one, and "rider" alone is correctly dropped as an
    # ordinary lowercase word -- so a per-word proposal silently forgets the
    # name the human just taught.
    check("Sheepstealer's rider" in suggested_terms,
          "TAM-IFADE-terim-onerisi-uretildi", json.dumps(suggested_terms)[:160])
    check("Sheepstealer's" not in suggested_terms,
          "yarim-isim-ayrica-onerilmiyor", json.dumps(suggested_terms)[:160])

    check(bool(fixtures.PROVIDER_CALLS) is False, "sifir-saglayici-cagrisi",
          str(len(fixtures.PROVIDER_CALLS)))

    # ================================================== 1/9. gercek tarayici
    section("1+9. Tarayici: transkript gorunur, butonlar hatadan sonra geri geliyor")
    # A second project in the SAME app: switching between them exercises the
    # in-memory draft state, which a full page reload would hide.
    other_pid, other_svc = build_project(app, api, title="Ikinci proje")
    seed_transcript(other_svc)
    browser_ok = run_browser_checks(app, pid, other_pid)
    if not browser_ok:
        print("  (tarayici bolumu atlandi)")

    print("\n" + "=" * 74)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, {len(FAILURES)} basarisiz")
    if FAILURES:
        for line in FAILURES:
            print("  BASARISIZ:", line)
        return 1
    print("TRANSCRIPT_EDITOR_OK (izole depolama, sahte saglayicilar; hicbir gercek "
          "API cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
    return 0


def run_browser_checks(app, pid, other_pid) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check(False, "tarayici-kurulu-degil", "playwright yok")
        return False

    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    server.serve_in_thread()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"{base}/?project={pid}&_={int(time.time() * 1000)}#script",
                      wait_until="networkidle")
            page.wait_for_selector("[data-testid=transcript-segments]", timeout=20000)

            rows = page.locator("[data-testid^=seg-input-]")
            check(rows.count() == 2, "1-ui-tum-segmentleri-gosteriyor", f"{rows.count()} satir")
            check(page.locator("[data-testid=seg-time-seg_0001]").inner_text().strip() != "",
                  "1-ui-zaman-damgasi-gosteriyor",
                  page.locator("[data-testid=seg-time-seg_0001]").inner_text())
            check("guven" in page.locator("[data-testid=seg-conf-seg_0001]").inner_text(),
                  "1-ui-guven-skoru-gosteriyor")
            # Which segments still HAVE low-confidence words depends on what the
            # console section edited above, so pinning one segment id made this
            # check a hostage of unrelated edits. Assert the contract instead:
            # the UI marks exactly what the server counts -- no more, no fewer.
            _svc = app.service(app.ownership(pid))
            _segments = _svc.transcript_view()["segments"]
            for _seg in _segments:
                _marked = page.locator(
                    f"[data-testid=seg-low-{_seg['id']}] .transcript-word--unsure").count()
                check(_marked == _seg["low_confidence_count"],
                      f"1-ui-dusuk-guven-{_seg['id']}-sunucuyla-tutarli",
                      f"UI {_marked} isaret / sunucu {_seg['low_confidence_count']}")
            # And the point of the whole exercise: a segment a human rewrote
            # carries no ASR confidence, so it must show NO "unsure" markers --
            # re-flagging a word the reviewer already fixed sends them back to
            # re-check their own correction.
            _edited = [s for s in _segments if s.get("edited")]
            check(bool(_edited), "1-ui-duzenlenmis-segment-var",
                  "duzenlenmis segment yok; kontrol bos gecerdi")
            check(all(s["low_confidence_count"] == 0 for s in _edited),
                  "1-ui-elle-duzeltilen-segment-UNSURE-degil",
                  str([(s["id"], s["low_confidence_count"]) for s in _edited]))
            check(page.locator("[data-testid=transcript-full-input]").count() == 1,
                  "1-ui-tum-metin-duzenleyicisi-var")
            check(page.locator("[data-testid=glossary-input]").count() == 1,
                  "1-ui-sozluk-duzenlenebilir")
            # Editing must survive the job poll's re-render, exactly like the
            # revision-notes field had to learn.
            page.fill("[data-testid=seg-input-seg_0001]", "Rhaena flew north")
            page.wait_for_timeout(2200)
            check(page.input_value("[data-testid=seg-input-seg_0001]") == "Rhaena flew north",
                  "1-ui-yazilan-metin-yeniden-cizimden-sag-cikiyor",
                  page.input_value("[data-testid=seg-input-seg_0001]"))

            # 9. A failing save must leave the button usable.
            page.route("**/transcript/segments", lambda route: route.abort())
            page.click("[data-testid=transcript-save]")
            page.wait_for_timeout(1500)
            save = page.locator("[data-testid=transcript-save]")
            check(save.is_enabled(), "9-basarisiz-kayittan-sonra-buton-geri-geliyor",
                  "kalici disabled buton donmus uygulamadan ayirt edilemez")
            check(save.inner_text().strip() == "Degisiklikleri kaydet",
                  "9-buton-etiketi-geri-geliyor", save.inner_text())
            page.unroute("**/transcript/segments")

            page.route("**/glossary", lambda route: route.abort())
            page.click("[data-testid=glossary-save]")
            page.wait_for_timeout(1500)
            check(page.locator("[data-testid=glossary-save]").is_enabled(),
                  "9-sozluk-butonu-da-geri-geliyor")
            page.unroute("**/glossary")

            page.click("[data-testid=transcript-save]")
            page.wait_for_timeout(2000)
            check(page.locator("[data-testid=seg-input-seg_0001]")
                  .input_value().startswith("Rhaena"),
                  "2-ui-uzerinden-kaydetme-calisiyor",
                  page.input_value("[data-testid=seg-input-seg_0001]"))
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-testid=transcript-segments]", timeout=20000)
            check(page.input_value("[data-testid=seg-input-seg_0001]") == "Rhaena flew north",
                  "3-ui-yenilemeden-sonra-metin-duruyor",
                  page.input_value("[data-testid=seg-input-seg_0001]"))

            # --- drafts must not follow the user into another project --------
            # Segment ids repeat across projects, so an UNSAVED draft that
            # survived a project switch would sit under the other project's
            # identical id and a Save would write these words into a transcript
            # they were never typed for. Switched through the app's own
            # history/popstate path, not a reload: a reload rebuilds state from
            # scratch and would hide the bug entirely.
            leaked = "BU METIN DIGER PROJEYE GECMEMELI"
            page.fill("[data-testid=seg-input-seg_0001]", leaked)
            page.fill("[data-testid=transcript-full-input]", leaked)
            page.fill("[data-testid=glossary-input]", leaked)
            page.wait_for_timeout(300)
            page.evaluate(
                """(target) => {
                    const url = new URL(location.href);
                    url.searchParams.set('project', target);
                    history.pushState(null, '', url);
                    window.dispatchEvent(new PopStateEvent('popstate'));
                }""", other_pid)
            page.wait_for_timeout(2500)
            page.wait_for_selector("[data-testid=transcript-segments]", timeout=20000)
            check(page.input_value("[data-testid=seg-input-seg_0001]") == "Rainer flew north",
                  "proje-degisince-segment-taslagi-SIZMIYOR",
                  page.input_value("[data-testid=seg-input-seg_0001]"))
            check(leaked not in page.input_value("[data-testid=transcript-full-input]"),
                  "proje-degisince-tam-metin-taslagi-SIZMIYOR",
                  page.input_value("[data-testid=transcript-full-input]")[:60])
            check(leaked not in page.input_value("[data-testid=glossary-input]"),
                  "proje-degisince-sozluk-taslagi-SIZMIYOR",
                  page.input_value("[data-testid=glossary-input]")[:60])

            browser.close()
        return True
    finally:
        server.shutdown()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for path in TMP:
            shutil.rmtree(path, ignore_errors=True)
    sys.exit(code)
