#!/usr/bin/env python
"""API tests for the Voice Studio review experience.

    python verify_voice_review.py

WHAT IT PROVES
--------------
Voice approval used to need only a hash echo. A hash proves the reviewer saw
some bytes; it cannot prove anyone LISTENED, and no container check can hear a
mispronounced name, a robotic cadence, or audio that says something other than
the approved script. So approval now additionally requires a digest-bound
checklist, and this suite proves that gate actually holds.

It also proves the shared engine did not change render behaviour: render review
and voice review are one implementation parameterised by kind, and a regression
in one would show up in the other.

REAL AUDIO
----------
When `.console_data_livetts/` holds the WAV produced by `verify_live_tts.py`,
that file is COPIED into an isolated fixture and used as the audio under review
-- so the binding, duration and format assertions run against genuine Gemini
output rather than a synthetic tone. The original is only read, never modified.
Without it the suite falls back to a generated WAV and says so.

No live provider is called, nothing is approved beyond the test fixtures in the
throwaway workspace, nothing is uploaded or published.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures, media_review, render_review, voice_review  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import approval, google_ai  # noqa: E402

LIVE_TTS_ROOT = PROJECT_ROOT / ".console_data_livetts"
SCRIPT_TEXT = ("Bir hanedani yikan sey her zaman kilic degildir. "
               "Bazen tek bir karar, tum bir soyun kaderini degistirir.")

FAILURES: list = []
PASSED = 0
TMP: list = []


def check(condition, label: str, message: str = "") -> bool:
    global PASSED
    if condition:
        PASSED += 1
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ — {message}")
    return False


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def make_app():
    root = Path(tempfile.mkdtemp(prefix="verify_voice_"))
    TMP.append(root)
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories())), root


def call(app, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return ConsoleApi(app).handle(method, path, body=raw, headers={})


def find_live_wav():
    """The WAV `verify_live_tts.py` produced, if it is on disk."""
    if not LIVE_TTS_ROOT.is_dir():
        return None
    candidates = sorted(LIVE_TTS_ROOT.rglob("results/tts/gemini_tts.wav"))
    return candidates[0] if candidates else None


def synthetic_wav(path: Path, seconds: float = 3.0) -> Path:
    """Fallback audio: silence at the real sample rate and width."""
    frames = int(google_ai.TTS_SAMPLE_RATE * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(google_ai.pcm_to_wav_bytes(b"\x00\x00" * frames))
    return path


def seed_voiceover(app, *, audio_source: Path, script_text: str = SCRIPT_TEXT):
    """A project holding an approved script and a voiceover over REAL audio.

    The audio is copied into the project namespace; the source file is only
    read. The voiceover artifact is built by the same `build_tts_artifact` the
    pipeline uses, so nothing here invents a shape the product does not produce.
    """
    from local_pipeline import script_generation

    record = app.create_project(title="ses inceleme", created_by="test")
    ownership = app.ownership(record["project_id"])
    service = app.service(ownership)

    script = script_generation.build_selected_script_artifact(
        script_text, "fixture", "test-fixture", 0.0, 0.0,
        {"rank": 1, "source_start_sec": 0.0, "source_end_sec": 30.0},
        {"approved": True, "approved_by": "test", "automatic_selection": False},
        {"raw_video_path": "fixture://voice-review"}, "fixture://hot_moments.json")
    digest = approval.describe_for_review("script", script)["content_sha256"]
    service.write_artifact("selected_script",
                            approval.approve_artifact(script, "script", digest, "test"),
                            actor="test")

    target = app.storage.resolve(ownership, "results", "tts", "gemini_tts.wav",
                                 create_parent=True)
    shutil.copy2(audio_source, target)
    validation = google_ai.validate_audio_file(target)
    artifact = google_ai.build_tts_artifact({
        "status": "ok", "provider": "google", "model": "gemini-3.1-flash-tts-preview",
        "audio_path": str(target), "audio_validation": validation,
        "voice": "Kore", "language_code": "tr-TR", "narration_direction": None,
        "chunks": 1, "latency_sec": 1.0,
        "source_script_sha256": google_ai.sha256_text(script_text),
        "output_audio_sha256": google_ai.sha256_file(target),
        "preview_model": True, "provider_attempts": [],
    }, script_text, script_status="approved")
    artifact["audio_path_relative"] = service.relative(target)
    service.write_artifact("selected_voiceover", artifact, actor="test")
    return ownership, service, target


# ===========================================================================
def section_shared_engine():
    section("1. Ortak cekirdek: render ve ses ayni kurallari kullaniyor")
    check(render_review.SPEC.kind == "render" and voice_review.SPEC.kind == "voice",
          "two-specs", "iki medyum icin iki spec yok")
    check(render_review.evaluate.__module__ == "creator_console.render_review"
          and voice_review.evaluate.__module__ == "creator_console.voice_review",
          "thin-wrappers", "")
    check(len(voice_review.CHECKLIST_ITEMS) == 6, "six-voice-items",
          f"{len(voice_review.CHECKLIST_ITEMS)} madde")
    check(set(voice_review.CHECKLIST_KEYS)
          == {"pronunciation", "naturalness", "pacing", "tone",
              "script_match", "audio_quality"},
          "voice-item-keys", str(voice_review.CHECKLIST_KEYS))
    for item in voice_review.CHECKLIST_ITEMS:
        check(item["question"] and item["fix_hint"] and item["fix_tab"],
              f"voice-item-complete-{item['key']}", "soru/ipucu/sekme eksik")
        check(item["fix_tab"] in voice_review.SPEC.fix_tabs,
              f"voice-item-tab-{item['key']}", item["fix_tab"])

    # THE latent bug the shared engine fixed: clearing a revision used to leave
    # the verdict at needs_revision forever, permanently blocking approval.
    from creator_console.ids import Ownership

    own = Ownership("usr_t", "wsp_t", "prj_t")
    for module, kwarg in ((render_review, "video_sha256"), (voice_review, "audio_sha256")):
        review = module.new_review(own, "d" * 64)
        module.request_revision(review, requested_by="t", notes="n",
                                **{kwarg: "d" * 64},
                                target_tab=module.SPEC.fix_tabs[0])
        check(module.evaluate(review, "d" * 64, own)["verdict"] == "needs_revision",
              f"{module.SPEC.kind}-revision-opens", "")
        module.clear_revision(review, resolved_by="t", **{kwarg: "d" * 64})
        state = module.evaluate(review, "d" * 64, own)
        check(state["verdict"] != "needs_revision" and not state["open_revision"],
              f"{module.SPEC.kind}-revision-clears",
              f"kapatilan revizyon hala engelliyor: {state['verdict']}")


def section_state(app, ownership, service, audio_path, real_audio):
    section("2. Inceleme durumu: teknik ozet, script metni, hash bagi")
    state = service.voice_review_state()
    tech = state["technical"]
    check(state["done_count"] == 0 and state["total_count"] == 6, "initial-state",
          f"{state['done_count']}/{state['total_count']}")
    check(state["media_sha256"], "digest-present", "ses hash i yok")
    check(state["media_sha256"] == approval.content_hash_for(
        "voiceover", service.load_artifact("selected_voiceover")["data"]),
        "digest-matches-approval-gate", "inceleme ve onay farkli hash e bakiyor")

    for field in ("voice_id", "model", "language_code", "duration_sec", "format",
                  "sample_rate", "channels", "source_script_hash"):
        check(tech.get(field) is not None, f"tech-{field}", "alan yok")
    check(tech["sample_rate"] == google_ai.TTS_SAMPLE_RATE, "tech-sample-rate",
          str(tech["sample_rate"]))
    check(tech["channels"] == 1, "tech-channels", str(tech["channels"]))
    check("PCM" in (tech["format"] or ""), "tech-format", str(tech["format"]))
    check(tech["duration_sec"] > 0, "tech-duration", str(tech["duration_sec"]))
    if real_audio:
        check(tech["duration_sec"] > 2, "tech-real-duration",
              f"gercek Gemini sesi {tech['duration_sec']} sn")

    check(state["script_text"] == SCRIPT_TEXT, "script-text-exposed",
          "seslendirilen metin gorunume verilmiyor")
    check(state["script_status"] == "approved", "script-status", str(state["script_status"]))
    check(tech["script_hash_matches"] is True, "script-hash-binding",
          f"{tech['source_script_hash']}")
    check(state["audio_relative_path"] and not Path(state["audio_relative_path"]).is_absolute(),
          "audio-path-relative", str(state["audio_relative_path"]))


def section_gate(app, ownership, service):
    section("3. Onay kapisi: liste tamamlanmadan acilmiyor")
    voiceover = service.load_artifact("selected_voiceover")["data"]
    digest = approval.content_hash_for("voiceover", voiceover)

    try:
        service.approve("voiceover", reviewed_sha256=digest, approved_by="test")
        check(False, "approval-blocked", "liste eksikken ses ONAYLANDI")
    except Exception as exc:  # noqa: BLE001 - refusal is correct
        check("ONAY REDDEDILDI" in str(exc), "approval-blocked", str(exc)[:80])

    # Ticking requires a named human, and a wrong key is refused.
    try:
        service.set_voice_review_item("pronunciation", checked=True, reviewed_by="")
        check(False, "tick-requires-human", "isimsiz isaretleme kabul edildi")
    except Exception as exc:  # noqa: BLE001
        check("inceleyen kisi" in str(exc), "tick-requires-human", str(exc)[:60])
    try:
        service.set_voice_review_item("bilinmeyen", checked=True, reviewed_by="t")
        check(False, "unknown-key-refused", "bilinmeyen madde kabul edildi")
    except Exception as exc:  # noqa: BLE001
        check("bilinmeyen kontrol maddesi" in str(exc), "unknown-key-refused", str(exc)[:60])

    for key in voice_review.CHECKLIST_KEYS:
        state = service.set_voice_review_item(key, checked=True, reviewed_by="dinleyici")
    check(state["complete"] and state["done_count"] == 6, "checklist-completes",
          f"{state['done_count']}/6")
    check(state["verdict"] == "pass", "verdict-pass", state["verdict"])

    approved = service.approve("voiceover", reviewed_sha256=digest, approved_by="test")
    check(approved["status"] == "approved", "approval-opens", approved["status"])
    return digest


def section_staleness(app, ownership, service, audio_path):
    section("4. Ses degisince: liste bayatliyor, onay dusuyor")
    before = service.voice_review_state()
    check(before["done_count"] == 6, "complete-before", str(before["done_count"]))

    # Replace the audio with different bytes, exactly as a re-run would.
    longer = google_ai.pcm_to_wav_bytes(b"\x10\x27" * (google_ai.TTS_SAMPLE_RATE * 2))
    audio_path.write_bytes(longer)

    after = service.voice_review_state()
    check(after["media_sha256"] != before["media_sha256"], "digest-changed",
          "ses degisti ama hash ayni")
    check(after["done_count"] == 0, "checklist-reset", f"{after['done_count']}/6")
    check(after["stale_count"] == 6, "marked-stale", f"{after['stale_count']} bayat")
    check(after["verdict"] == "pending", "verdict-back-to-pending", after["verdict"])
    check("BASKA bir ses dosyasina" in after["blocker"], "stale-blocker-explains",
          after["blocker"][:70])

    approval_state = service.approval_state("voiceover")
    check(not approval_state.get("effective_valid"), "approval-invalidated",
          "ses degisti ama onay hala gecerli")

    try:
        service.approve("voiceover",
                        reviewed_sha256=approval.content_hash_for(
                            "voiceover", service.load_artifact("selected_voiceover")["data"]),
                        approved_by="test")
        check(False, "reapproval-blocked", "bayat listeyle yeniden ONAYLANDI")
    except Exception as exc:  # noqa: BLE001
        check("ONAY REDDEDILDI" in str(exc), "reapproval-blocked", str(exc)[:70])


def section_revision(app, ownership, service):
    section("5. Revizyon: kaydediliyor, onay sayilmiyor, kapatilabiliyor")
    pid = ownership.project_id
    # A revision request must change the REVIEW and nothing else. Compared
    # before/after rather than against a constant, because the artifact carries
    # a stale `approved` status from section 3 whose binding section 4 already
    # broke -- the claim under test is "requesting a revision grants nothing".
    before_artifact = json.dumps(service.load_artifact("selected_voiceover")["data"],
                                  sort_keys=True, default=str)
    before_valid = service.approval_state("voiceover").get("effective_valid")

    status, payload = call(app, "POST", f"/api/projects/{pid}/voice-review", {
        "action": "request_revision", "requested_by": "dinleyici",
        "notes": "Ozel isim yanlis okunuyor", "target_tab": "script"})
    check(status == 200, "revision-accepted", f"HTTP {status}")
    state = payload["voice_review"]
    check(state["verdict"] == "needs_revision", "revision-verdict", state["verdict"])
    check(state["open_revision"]["target_tab"] == "script", "revision-target",
          str(state["open_revision"].get("target_tab")))

    after_artifact = json.dumps(service.load_artifact("selected_voiceover")["data"],
                                 sort_keys=True, default=str)
    check(after_artifact == before_artifact, "revision-does-not-touch-artifact",
          "revizyon istegi ses artefaktini degistirdi")
    after_valid = service.approval_state("voiceover").get("effective_valid")
    check(after_valid == before_valid and not after_valid,
          "revision-is-not-approval",
          f"onay gecerliligi degisti: {before_valid} -> {after_valid}")

    status, payload = call(app, "POST", f"/api/projects/{pid}/voice-review", {
        "action": "request_revision", "requested_by": "", "notes": "x"})
    check(status == 400, "revision-requires-human", f"HTTP {status}")
    status, payload = call(app, "POST", f"/api/projects/{pid}/voice-review", {
        "action": "request_revision", "requested_by": "d", "notes": ""})
    check(status == 400, "revision-requires-note", f"HTTP {status}")
    status, _ = call(app, "POST", f"/api/projects/{pid}/voice-review", {
        "action": "request_revision", "requested_by": "d", "notes": "n",
        "target_tab": "render"})
    check(status == 400, "revision-target-restricted",
          f"ses revizyonu render sekmesini kabul etti: HTTP {status}")

    status, payload = call(app, "POST", f"/api/projects/{pid}/voice-review",
                           {"action": "clear_revision", "resolved_by": "dinleyici"})
    check(status == 200 and payload["voice_review"]["verdict"] != "needs_revision",
          "revision-clears", str(payload["voice_review"]["verdict"]))
    check(not payload["voice_review"]["open_revision"], "revision-closed",
          "kapatilan revizyon hala acik")


def section_persistence(app, ownership, root):
    section("6. Restart: liste ve inceleyen adi korunuyor")
    service = app.service(ownership)
    for key in voice_review.CHECKLIST_KEYS[:3]:
        service.set_voice_review_item(key, checked=True, reviewed_by="kalici-dinleyici")
    before = service.voice_review_state()
    app.shutdown()

    restarted = ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))
    after = restarted.service(ownership).voice_review_state()
    check(after["done_count"] == before["done_count"], "checklist-survives-restart",
          f"{before['done_count']} -> {after['done_count']}")
    ticked = [item for item in after["items"] if item["checked"]]
    check(all(item["checked_by"] == "kalici-dinleyici" for item in ticked),
          "reviewer-name-survives-restart",
          str([item.get("checked_by") for item in ticked]))
    check(restarted.project_view(ownership)["voice_review"]["done_count"] == 3,
          "in-view-model", "gorunum modelinde yok")
    restarted.shutdown()


def section_no_live_calls():
    section("7. Test sirasinda hicbir saglayici cagrilmiyor")
    touched = []

    class Exploding:
        def __getattr__(self, name):
            touched.append(name)
            raise AssertionError(f"CANLI CAGRI: {name}")

    root = Path(tempfile.mkdtemp(prefix="verify_voice_live_"))
    TMP.append(root)
    app = ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        google_client_factory=lambda: Exploding(),
        openai_client_factory=lambda: Exploding(),
        anthropic_client_factory=lambda: Exploding(),
        elevenlabs_client_factory=lambda: Exploding()))
    wav = synthetic_wav(root / "probe.wav", 2.0)
    ownership, service, _ = seed_voiceover(app, audio_source=wav)
    service.voice_review_state()
    service.set_voice_review_item("pronunciation", checked=True, reviewed_by="t")
    check(not touched, "no-provider-touched", f"dokunulan: {touched}")
    app.shutdown()


def section_frontend():
    section("8. Arayuz sozlesmesi (statik)")
    js = (PROJECT_ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "creator_console" / "static" / "app.css").read_text(encoding="utf-8")
    for needle, label in (
            ("function reviewChecklist(kind)", "shared-checklist"),
            ("function reviewRevisionPanel(kind)", "shared-revision"),
            ("const REVIEW_KINDS", "kind-registry"),
            ("function voicePlayerPanel()", "player"),
            ("function voiceTechnicalPanel()", "technical"),
            ("function voiceScriptPanel()", "script-panel"),
            ("function reviewApprovalSection(kind", "shared-approval-gate"),
            ("function voiceRerunAction()", "rerun-panel"),
            ("data-testid': 'voice-rerun-tts'", "rerun-action"),
            ("reviewerName(kind)", "reviewer-persistence")):
        check(needle in js, f"js-{label}", f"{needle!r} yok")
    # The render panels must be delegations now, not a second implementation.
    check(js.count("action: 'set_item'") == 1, "single-checklist-implementation",
          f"{js.count(chr(39) + 'set_item' + chr(39))} ayri uygulama")
    check("return reviewChecklist('render');" in js, "render-delegates", "")
    check("crossorigin" in js and "/media/" in js, "player-uses-media-route",
          "oynatici medya rotasini kullanmiyor")
    check("localStorage" in js, "reviewer-survives-refresh", "")
    check(".voice-player audio" in css, "player-style", "oynatici stili yok")
    check(js.count("approvalPanel(approvalKind, actionLabel)") == 1
          and js.count("review.complete && !review.open_revision") == 1,
          "single-approval-gate", "onay kapisi birden fazla yerde karar veriliyor")
    check("reviewApprovalSection('render'" in js and "reviewApprovalSection('voice'"
          in js, "both-screens-use-gate", "ekranlar ortak kapiyi kullanmiyor")


def main() -> int:
    print("=" * 72)
    print("VOICE STUDIO INCELEME DOGRULAMASI")
    print("=" * 72)
    live_wav = find_live_wav()
    real_audio = live_wav is not None
    print(f"  Ses kaynagi: {'GERCEK canli Gemini WAV (kopya)' if real_audio else 'sentetik WAV'}")
    if real_audio:
        print(f"               {live_wav}")

    try:
        section_shared_engine()
        app, root = make_app()
        staging = root / "audio_under_review.wav"
        if real_audio:
            shutil.copy2(live_wav, staging)
        else:
            synthetic_wav(staging, 4.0)
        ownership, service, audio_path = seed_voiceover(app, audio_source=staging)

        section_state(app, ownership, service, audio_path, real_audio)
        section_gate(app, ownership, service)
        section_staleness(app, ownership, service, audio_path)
        section_revision(app, ownership, service)
        section_persistence(app, ownership, root)
        section_no_live_calls()
        section_frontend()
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"suite: {type(exc).__name__}: {exc}")
        print(f"  COKTU — {type(exc).__name__}: {exc}")
    finally:
        for path in TMP:
            shutil.rmtree(path, ignore_errors=True)

    print()
    print("=" * 72)
    print(f"toplam {PASSED + len(FAILURES)} kontrol, {PASSED} gecti, {len(FAILURES)} basarisiz")
    if FAILURES:
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("VOICE_REVIEW_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
