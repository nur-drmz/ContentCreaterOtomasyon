#!/usr/bin/env python
"""ONE deliberate live Gemini TTS call, to validate the Route D path.

    set CREATOR_CONSOLE_LIVE_TTS=1
    python verify_live_tts.py --i-authorize-one-live-call

WHY THIS IS SEPARATE
--------------------
Sibling of `verify_live_script.py`, same shape and same reasoning: every other
suite in this repo is mock-only and must stay that way. A mock can prove the
console calls the router correctly, but it can never prove that the real Gemini
TTS preview model returns audio the WAV writer, the validator and the approval
gate all accept. That is the one thing this script exists to find out.

It is therefore double-guarded -- the env flag AND the explicit argument -- and
wired so one request is genuinely one request:

    enable_model_fallback = False   (no 2.5-flash-preview-tts hop)
    max_model_attempts    = 1
    max_transient_retries = 0       (no 5xx/RPM retry)

Nothing about the current routing is overridden: the model is whatever Route D
already puts first, and the voice is the console's own default. This validates
the shipped configuration, it does not invent a new one.

HARD STOPS
----------
Isolated storage root and its own workspace. The approved script it feeds to TTS
is a TEST FIXTURE written into that workspace -- the user's own script artifacts
are never read, written or approved. The produced audio stays `review_required`:
it is not approved, not rendered, not uploaded, not published. No secret value
and no audio payload is ever printed or stored anywhere but the WAV itself.
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

from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.env_file import load_env_file  # noqa: E402
from local_pipeline import approval, google_ai, script_generation  # noqa: E402

STORAGE_ROOT = PROJECT_ROOT / ".console_data_livetts"
LIVE_ENV_FLAG = "CREATOR_CONSOLE_LIVE_TTS"

#: Short and fixed on purpose: one chunk, one request, a predictable duration
#: window. English, because `en-US` is now the narrated workflow's content
#: language and the point of a live check is to exercise the SHIPPED path.
TEST_SCRIPT_TEXT = (
    "Daemon Targaryen knew that Rhaenyra’s claim could reshape the fate of "
    "House Targaryen. But one reckless decision could bring the entire dynasty "
    "to its knees."
)
TEST_LANGUAGE = "en-US"
#: The narration style under test. Its voice comes from the preset registry,
#: which only ever names a voice the API actually supports.
TEST_PRESET = "deep_storyteller"
EXPECTED_MIN_DURATION_SEC = 2.0
EXPECTED_MAX_DURATION_SEC = 60.0

FAILURES: list = []
#: Every real request the counting client makes, so the number is measured.
LIVE_CALLS: list = []


def check(condition, label: str, message: str = "") -> bool:
    if condition:
        print(f"  [{label}] OK" + (f" — {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ — {message}")
    return False


class CountingGoogleClient:
    """Thin proxy over the real genai client that records every request.

    Not a mock: the request goes to Google. It exists so "exactly one call" is
    a measurement rather than a claim about the code path. It records the model
    and nothing else -- never the response, which carries the audio payload.
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


def prepare_isolated_project(app, service, ownership, actor: str) -> dict:
    """Take the throwaway project through the REAL Phase 0 gates.

    TTS refuses to run without an approved `narrated_theory` plan, and that gate
    is the point -- so it is satisfied properly rather than bypassed. Everything
    here is local: a tiny synthetic clip, local ffprobe scoring, and the same
    hash-bound approval the console uses. No provider is contacted.
    """
    from creator_console.fixtures import make_test_source_video

    staging = app.storage.resolve(ownership, ".runtime", "tts_fixture.mp4",
                                   create_parent=True)
    make_test_source_video(staging, seconds=4)
    service.register_source_video(filename="tts_fixture.mp4",
                                   payload=staging.read_bytes(), registered_by=actor)
    staging.unlink(missing_ok=True)

    service.run_source_assessment(actor=actor)
    service.create_processing_plan(
        mode="narrated_theory", selected_by=actor, output_count=1,
        target_duration_sec=30,
        content_language=TEST_LANGUAGE,
        narration_preset=TEST_PRESET,
        theory_angle="A fixed English test theory angle for live TTS verification",
        render_config={"render_mode": "blurred_background", "preview_mode": True,
                        "caption_style": "active_word_yellow", "caption_theme": "auto"},
        notes="IZOLE CANLI TTS TESTI — kullanicinin projesi degildir")
    digest = service.describe_plan_review()["review"]["content_sha256"]
    return service.approve("processing_plan", reviewed_sha256=digest, approved_by=actor)


def write_approved_script_fixture(service, actor: str) -> dict:
    """A TEST-ONLY approved script, written into the isolated workspace.

    TTS refuses to run on an unapproved script, and rightly so. Rather than
    weaken that gate for a test, the fixture goes through the SAME approval
    function the console uses, in a workspace that belongs to nobody.
    """
    artifact = script_generation.build_selected_script_artifact(
        TEST_SCRIPT_TEXT, "fixture", "test-fixture", 0.0, 0.0,
        {"rank": 1, "source_start_sec": 0.0, "source_end_sec": 30.0,
         "score": 0.0, "reason": "canli TTS dogrulamasi icin sabit test fikstürü"},
        {"approved": True, "approved_by": actor, "automatic_selection": False},
        {"raw_video_path": "fixture://live-tts-test"},
        "fixture://hot_moments.json",
        hook="One reckless decision could bring an entire dynasty to its knees.",
        estimated_duration_sec=12,
    )
    digest = approval.describe_for_review("script", artifact)["content_sha256"]
    approved = approval.approve_artifact(artifact, "script", digest, actor,
                                          "canli TTS dogrulamasi icin test fikstürü")
    service.write_artifact("selected_script", approved, actor=actor)
    return approved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-authorize-one-live-call", action="store_true",
                        help="ZORUNLU: gercek bir Gemini TTS cagrisi yapilacagini onayla")
    parser.add_argument("--keep", action="store_true",
                        help="izole depolamayi silme (sesi dinlemek icin)")
    args = parser.parse_args(argv)

    load_env_file(PROJECT_ROOT / ".env.local")

    # Two independent guards, so neither a stray flag nor a stray env var alone
    # can spend quota. The env var is the one the test runner would never set.
    if os.environ.get(LIVE_ENV_FLAG, "").strip() not in ("1", "true", "yes"):
        print(f"SKIPPED: {LIVE_ENV_FLAG}=1 ayarlanmadigi icin canli TTS cagrisi YAPILMADI.")
        print(f"  Calistirmak icin: set {LIVE_ENV_FLAG}=1  ve  "
              "python verify_live_tts.py --i-authorize-one-live-call")
        return 0
    if not args.i_authorize_one_live_call:
        print("REFUSED: bu betik GERCEK bir Gemini TTS cagrisi yapar.")
        print("Onaylamak icin: python verify_live_tts.py --i-authorize-one-live-call")
        return 2

    print("=" * 78)
    print("CANLI GEMINI TTS DOGRULAMASI — TAM OLARAK 1 cagri")
    print("=" * 78)
    print(f"  Izole depolama: {STORAGE_ROOT}")
    print("  Politika      : model fallback KAPALI, yeniden deneme 0,")
    print("                  ses ONAYLANMAYACAK, render/yukleme/yayin YOK.")
    print(f"  Test metni    : {len(TEST_SCRIPT_TEXT)} karakter (sabit, {TEST_LANGUAGE})")
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
        user_id="usr_livetts", workspace_id="wsp_livetts",
        inline_jobs=True,
        secret_getter=lambda name: os.environ.get(name),
        google_client_factory=google_client_factory,
        openai_client_factory=lambda: None,
        anthropic_client_factory=lambda: None,
        elevenlabs_client_factory=lambda: None))

    try:
        reference = app.secrets.reference("google_api")
        check(reference.configured, "credential",
              f"{reference.to_public_dict()['state']} ({reference.alias_used})")
        if not reference.configured:
            return _finish(app, args.keep)

        record = app.create_project(title="CANLI TTS testi (izole)",
                                     created_by="verify_live_tts")
        ownership = app.ownership(record["project_id"])
        service = app.service(ownership)

        # --- isolated project through the real Phase 0 gates ------------
        plan = prepare_isolated_project(app, service, ownership, "verify_live_tts")
        check(plan["status"] == "approved", "plan-approved", plan["status"])
        allowed, reason = service.plan_gate("tts")
        check(allowed, "tts-gate-open", reason)

        # --- test-only approved script ---------------------------------
        script = write_approved_script_fixture(service, "verify_live_tts")
        check(script["status"] == "approved", "fixture-approved", script["status"])
        binding = approval.check_approval(script, "script")
        check(binding["valid"], "fixture-binding", binding.get("reason", ""))
        check(script["script_text"] == TEST_SCRIPT_TEXT, "fixture-text",
              f"{len(script['script_text'])} karakter")

        # The console's own default routing, not a special one for the test.
        route = google_ai.TASK_ROUTES["tts"]
        decision = ((service.load_artifact("processing_plan")["data"] or {})
                    .get("decision") or {})
        narration = decision.get("narration") or {}
        voice = narration.get("voice")
        check(narration.get("preset") == TEST_PRESET, "preset-in-plan",
              str(narration.get("preset")))
        check(voice in google_ai.GEMINI_TTS_VOICES, "voice-is-supported",
              f"{voice!r} kayitli ses listesinde")
        check(bool(narration.get("narration_direction")), "direction-in-plan",
              "anlatim yonergesi planda yok")
        # Taken from the plan, not hardcoded: the point is that what the plan
        # decided is what TTS actually receives.
        language_code = decision.get("content_language")
        check(language_code == TEST_LANGUAGE, "plan-language",
              f"plan dili {language_code!r}")
        print(f"  Rota (Route D): {list(route)}")
        print(f"  Kullanilacak  : {route[0]} · ses={voice} · dil={language_code}")
        print(f"  On ayar       : {narration.get('preset')}")
        print(f"  Yonerge       : {(narration.get('narration_direction') or '')[:72]}...")

        # --- THE one live call -----------------------------------------
        print()
        print("  >>> GERCEK Gemini TTS cagrisi yapiliyor (tek model, tek deneme, 0 retry)...")
        LIVE_CALLS.clear()
        try:
            artifact = service.run_tts(
                voice=voice, language_code=language_code, actor="verify_live_tts",
                enable_google_model_fallback=False,
                max_google_model_attempts=1,
                max_google_transient_retries=0)
        except Exception as exc:  # noqa: BLE001 - report sanitized, never raw
            sanitized = google_ai.sanitize_google_error(
                exc, credential=google_ai.resolve_secret(
                    "google_api", lambda n: os.environ.get(n)))
            print(f"  >>> CAGRI BASARISIZ: {sanitized}")
            FAILURES.append(f"live-call: {sanitized}")
            return _finish(app, args.keep)

        generate_calls = [c for c in LIVE_CALLS if c["api"] == "generate_content"]
        print(f"  >>> tamamlandi: {len(generate_calls)} generate_content cagrisi")
        for entry in LIVE_CALLS:
            print(f"        {entry['api']} model={entry['model']}")

        check(len(generate_calls) == 1, "one-call-only",
              f"{len(generate_calls)} generate_content cagrisi yapildi")
        check(all(c["model"] == route[0] for c in generate_calls), "model-pinned",
              f"kullanilan model(ler): {[c['model'] for c in generate_calls]}")

        # An auth/quota/provider failure stops here with a sanitized reason.
        if artifact.get("status") in ("failed", "insufficient_data"):
            reason = str(artifact.get("status_reason") or "")[:300]
            print(f"  >>> SAGLAYICI HATASI: {reason}")
            FAILURES.append(f"provider: {artifact.get('error_kind')}: {reason}")
            return _finish(app, args.keep)

        # --- artifact and audio validation -----------------------------
        loaded = service.load_artifact("selected_voiceover")
        check(loaded["data"] is not None and not loaded["problem"],
              "artifact-persisted", loaded["problem"] or "selected_voiceover.json yazildi")
        if not loaded["data"]:
            return _finish(app, args.keep)
        voiceover = loaded["data"]

        check(voiceover["status"] == "review_required", "review-required",
              f"artifact_status={voiceover['status']}")
        check(voiceover["human_approval"]["approved"] is False, "not-approved",
              "insan onayi VERILMEDI")
        check(voiceover.get("voice_id") == voice, "voice-recorded",
              str(voiceover.get("voice_id")))
        check(voiceover.get("narration_preset") == TEST_PRESET, "preset-recorded",
              str(voiceover.get("narration_preset")))
        check(voiceover.get("narration_direction")
              == narration.get("narration_direction"),
              "direction-used", "plandaki yonerge kullanilmadi")
        check(voiceover.get("language_code") == language_code, "language-recorded",
              str(voiceover.get("language_code")))
        check(voiceover.get("model") == route[0], "model-recorded",
              str(voiceover.get("model")))

        audio_path = Path(voiceover["audio_path"])
        check(audio_path.is_file(), "audio-exists", str(audio_path.name))
        if not audio_path.is_file():
            return _finish(app, args.keep)

        validation = google_ai.validate_audio_file(audio_path)
        details = validation["details"]
        checks = validation["checks"]
        check(validation["passed"], "audio-validation", validation.get("reason", ""))
        check(checks["container_wav"], "container-wav", "RIFF/WAVE degil")
        check(checks["codec_pcm_s16le"], "codec-pcm-s16le",
              f"{details.get('bits_per_sample')} bit")
        check(details["sample_rate"] == google_ai.TTS_SAMPLE_RATE, "sample-rate",
              f"{details['sample_rate']} Hz")
        check(details["channels"] == 1, "channels", f"{details['channels']} kanal")
        duration = details["duration_sec"] or 0
        check(EXPECTED_MIN_DURATION_SEC <= duration <= EXPECTED_MAX_DURATION_SEC,
              "duration-plausible",
              f"{duration:.2f} sn ({EXPECTED_MIN_DURATION_SEC}-{EXPECTED_MAX_DURATION_SEC} bekleniyor)")
        check(details["size_bytes"] > 1024, "audio-nonempty",
              f"{details['size_bytes']} bayt")

        # --- script-hash binding ---------------------------------------
        expected_full = google_ai.sha256_text(TEST_SCRIPT_TEXT)
        check(voiceover.get("source_script_sha256") == expected_full,
              "script-hash-binding", "ses, seslendirdigi metne bagli degil")
        check(voiceover.get("source_script_hash") == expected_full[:16],
              "script-hash-short-form", str(voiceover.get("source_script_hash")))
        check(voiceover.get("source_script_status") == "approved",
              "script-status-recorded", str(voiceover.get("source_script_status")))
        # Changing the script must invalidate the pairing -- proved, not assumed.
        check(google_ai.sha256_text(TEST_SCRIPT_TEXT + " ") != expected_full,
              "hash-is-content-sensitive", "hash icerige duyarli degil")

        # --- hard stops -------------------------------------------------
        check(service.load_artifact("render_manifest")["data"] is None, "no-render",
              "render fazi CALISTIRILMADI")
        state = service.approval_state("voiceover")
        check(not state.get("approved"), "voiceover-not-approved",
              "seslendirme onaylanmis")
        check(voiceover.get("not_uploaded") is not False, "not-uploaded",
              "hicbir yere yuklenmedi")

        # --- nothing sensitive escaped ----------------------------------
        secret_value = os.environ.get("GeminiKey") or ""
        blob = json.dumps(app.project_view(ownership), ensure_ascii=False, default=str)
        check(secret_value and secret_value not in blob, "no-secret-leak",
              "API yanitinda anahtar yok")
        artifact_text = service.path("selected_voiceover").read_text(encoding="utf-8")
        check(secret_value and secret_value not in artifact_text, "no-secret-in-artifact",
              "artefaktta anahtar yok")
        # The audio payload lives ONLY in the WAV, never inside a JSON record.
        check("audio_base64" not in artifact_text and "data:audio" not in artifact_text,
              "no-audio-payload-in-json", "ses verisi JSON'a gomulmus")
        check("production_ready" not in blob, "no-production-ready", "production_ready yok")

        print()
        print("=" * 78)
        print("URETILEN SES (INSAN INCELEMESI BEKLIYOR — ONAYLANMADI)")
        print("=" * 78)
        print(f"  Dosya    : {audio_path}")
        print(f"  Model    : {voiceover.get('model')}")
        print(f"  Ses      : {voiceover.get('voice_id')} · {voiceover.get('language_code')}"
              f" · on ayar: {voiceover.get('narration_preset')}")
        print(f"  Sure     : {duration:.2f} sn")
        print(f"  Bicim    : {details['sample_rate']} Hz · {details['channels']} kanal · "
              f"{details['bits_per_sample']} bit PCM · {details['size_bytes']} bayt")
        print(f"  Durum    : {voiceover['status']}")
        print(f"  Metin    : {TEST_SCRIPT_TEXT}")
        print("=" * 78)
        return _finish(app, args.keep)
    finally:
        pass


def _finish(app, keep: bool) -> int:
    app.shutdown()
    generate_calls = len([c for c in LIVE_CALLS if c["api"] == "generate_content"])
    print()
    print(f"  Gercek API cagrisi toplami: {len(LIVE_CALLS)} ({generate_calls} generate_content)")
    if not keep:
        print(f"  Izole depolama korunuyor (sesi dinlemek icin): {STORAGE_ROOT}")
    if FAILURES:
        print(f"\nLIVE_TTS_FAILED ({len(FAILURES)} sorun)")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nLIVE_TTS_OK (1 gercek Gemini TTS cagrisi; ses review_required olarak kaydedildi, "
          "ONAYLANMADI, render/yukleme/yayin yapilmadi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
