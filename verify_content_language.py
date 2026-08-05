#!/usr/bin/env python
"""Tests for content language as an end-to-end narrated_theory setting.

    python verify_content_language.py

WHAT IT PROVES
--------------
"Generate it in English" is not one setting in one place -- it is a decision
that has to survive four handoffs: the plan, the script prompt, the TTS request
and the voice checklist. A language that is chosen in Phase 0 but never reaches
TTS produces an English script narrated in Turkish, and nothing in the pipeline
would notice, because every individual artifact would look internally valid.

So this suite follows the value across every handoff, and then proves the thing
that makes it safe to change: because the language lives in the plan DECISION,
changing it changes the plan's approval hash, which invalidates the script, the
voiceover and the render along the existing chain. Nothing is translated; the
old content simply stops being approved.

`direct_clip_series` keeps the original audio and therefore has no content
language at all -- proved here too, because a stray field in its decision block
would change its approval hash and invalidate plans that never used narration.

Mocked providers, isolated temp roots, no live call, no upload, no publish.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures, voice_review  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from local_pipeline import approval, script_generation, source_strategy  # noqa: E402

FAILURES: list = []
PASSED = 0
TMP: list = []

EN_ANGLE = "This scene is not a show of power but of quiet desperation"


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
    root = Path(tempfile.mkdtemp(prefix="verify_lang_"))
    TMP.append(root)
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))


def call(app, method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return ConsoleApi(app).handle(method, path, body=raw, headers={})


def narrated_project(app, *, language="en-US", preset=None):
    seed = fixtures.seed_demo_project(app, title="dil akisi")
    pid = seed["project_id"]
    service = app.service(app.ownership(pid))
    call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "narrated_theory", "selected_by": "test",
        "content_language": language, "theory_angle": EN_ANGLE,
        "narration_preset": preset,
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    plan = service.load_artifact("processing_plan")["data"]
    call(app, "POST", f"/api/projects/{pid}/approvals/processing_plan", {
        "reviewed_sha256": approval.content_hash_for("processing_plan", plan),
        "approved_by": "test"})
    return pid, app.ownership(pid), service


def run_to_voiceover(app, pid, service):
    call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"whisper_model_size": "tiny", "clip_duration_sec": 3, "top_n": 2}})
    hot = service.load_artifact("hot_moments")["data"]
    service.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                     reviewed_by="test", require_preview=False)
    call(app, "POST", f"/api/projects/{pid}/jobs", {"job_type": "script_generation"})
    call(app, "POST", f"/api/projects/{pid}/approvals/script", {
        "reviewed_sha256": service.approval_state("script")["content_sha256"],
        "approved_by": "test"})
    call(app, "POST", f"/api/projects/{pid}/jobs", {"job_type": "tts"})


# ===========================================================================
def section_plan():
    section("1. Plan: dil zorunlu, kapali kume, karar hash'inde")
    check(set(source_strategy.CONTENT_LANGUAGES) == {"en-US", "tr-TR"},
          "closed-set", str(sorted(source_strategy.CONTENT_LANGUAGES)))
    check(source_strategy.DEFAULT_CONTENT_LANGUAGE == "en-US", "default-en",
          source_strategy.DEFAULT_CONTENT_LANGUAGE)

    app = make_app()
    pid, ownership, service = narrated_project(app, language="en-US")
    plan = service.load_artifact("processing_plan")["data"]
    check(plan["decision"]["content_language"] == "en-US", "in-decision",
          str(plan["decision"].get("content_language")))
    check("content_language" in json.dumps(
        plan[approval.PROCESSING_PLAN_DECISION_KEY] if hasattr(
            approval, "PROCESSING_PLAN_DECISION_KEY") else plan["decision"]),
        "inside-hashed-block", "dil karar blogunda degil")

    # The hash the approval binds to must actually move when the language does.
    before = approval.content_hash_for("processing_plan", plan)
    other = json.loads(json.dumps(plan))
    other["decision"]["content_language"] = "tr-TR"
    check(approval.content_hash_for("processing_plan", other) != before,
          "language-changes-hash", "dil degisince plan hash'i degismiyor")

    status, payload = call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "narrated_theory", "selected_by": "test",
        "content_language": "de-DE", "theory_angle": EN_ANGLE})
    check(status == 400, "unsupported-language-refused", f"HTTP {status}")

    errors = source_strategy.validate_processing_plan(
        {**plan, "decision": {**plan["decision"], "content_language": None}})
    check(any("content_language" in e for e in errors), "missing-language-invalid",
          str(errors[:2]))
    app.shutdown()


def section_direct_unchanged():
    section("2. direct_clip_series degismiyor")
    app = make_app()
    seed = fixtures.seed_demo_project(app, title="direct")
    pid = seed["project_id"]
    service = app.service(app.ownership(pid))
    call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "test",
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    plan = service.load_artifact("processing_plan")["data"]
    check(plan["decision"]["content_language"] is None, "direct-has-no-language",
          str(plan["decision"].get("content_language")))
    # Even an explicit request must not attach one -- the original audio is kept.
    call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "test", "content_language": "tr-TR",
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    plan = service.load_artifact("processing_plan")["data"]
    check(plan["decision"]["content_language"] is None, "direct-ignores-language",
          str(plan["decision"].get("content_language")))
    check(not source_strategy.validate_processing_plan(plan), "direct-plan-valid",
          str(source_strategy.validate_processing_plan(plan)[:2]))
    errors = source_strategy.validate_processing_plan(
        {**plan, "decision": {**plan["decision"], "content_language": "tr-TR"}})
    check(any("icerik dili tasimaz" in e for e in errors), "direct-rejects-language",
          str(errors[:2]))
    app.shutdown()


def section_prompt():
    section("3. Script prompt: dil kurali ve hook ayni dilde isteniyor")
    moment = {"rank": 1, "source_start_sec": 0, "source_end_sec": 30,
              "reason": "r", "transcript": "t"}
    english = script_generation.build_user_prompt(moment, "m", "t", 60,
                                                   content_language="en-US")
    turkish = script_generation.build_user_prompt(moment, "m", "t", 60,
                                                   content_language="tr-TR")
    check(english.startswith("Write the script and the hook"), "en-rule-first",
          english[:60])
    check("English" in english and "en-US" in english, "en-named", english[:80])
    check("hook" in english.lower(), "en-covers-hook", "hook ayni dilde istenmiyor")
    check(turkish.startswith("Scripti ve hook'u"), "tr-rule-first", turkish[:60])
    check(english != turkish, "prompts-differ", "iki dil ayni prompt'u uretti")
    default = script_generation.build_user_prompt(moment, "m", "t", 60)
    check(default == english, "default-is-english", "varsayilan Ingilizce degil")


def section_flow():
    section("4. Uctan uca: plan -> script -> TTS -> inceleme")
    app = make_app()
    pid, ownership, service = narrated_project(app, language="en-US")
    run_to_voiceover(app, pid, service)

    script = service.load_artifact("selected_script")["data"]
    check(script.get("content_language") == "en-US", "script-records-language",
          str(script.get("content_language")))

    voiceover = service.load_artifact("selected_voiceover")["data"]
    check(voiceover.get("language_code") == "en-US", "tts-uses-plan-language",
          str(voiceover.get("language_code")))

    state = service.voice_review_state()
    check(state["content_language"] == "en-US", "review-knows-language",
          str(state.get("content_language")))
    naturalness = next(i for i in state["items"] if i["key"] == "naturalness")
    check(naturalness["label"] == "English naturalness", "review-english-label",
          naturalness["label"])
    check("Turk" not in naturalness["question"], "review-no-turkish-question",
          naturalness["question"][:60])
    check([i["key"] for i in state["items"]] == list(voice_review.CHECKLIST_KEYS),
          "review-keys-unchanged", str([i["key"] for i in state["items"]]))
    check(state["language_mismatch"] is False, "no-mismatch-when-aligned", "")
    return app, pid, ownership, service


def section_tts_ignores_caller_default(app, pid, service):
    section("5. TTS cagiran tarafin varsayilanini degil plani dinliyor")
    # A caller passing the old Turkish default must NOT silently re-narrate an
    # English script in Turkish.
    result = service.run_tts(voice="Kore", language_code="tr-TR", actor="test")
    check(result.get("language_code") == "en-US", "plan-wins-over-caller",
          str(result.get("language_code")))


def section_language_change(app, pid, ownership, service):
    section("6. Dil degisimi: onaylar dusuyor, icerik CEVRILMIYOR")
    before_script = service.load_artifact("selected_script")["data"]["script_text"]
    before_voice = service.load_artifact("selected_voiceover")["data"]
    before_audio = (before_voice.get("output_sha256")
                    or before_voice.get("output_audio_sha256")
                    or before_voice.get("audio_path"))
    chain_before = service.approval_chain()
    check(chain_before["script"]["effective_valid"], "script-approved-before", "")

    status, _ = call(app, "PATCH", f"/api/projects/{pid}/plan",
                     {"updated_by": "test", "content_language": "tr-TR"})
    check(status == 200, "language-edit-accepted", f"HTTP {status}")

    plan = service.load_artifact("processing_plan")["data"]
    check(plan["decision"]["content_language"] == "tr-TR", "language-changed",
          str(plan["decision"].get("content_language")))
    check(plan["status"] == "review_required", "plan-needs-reapproval", plan["status"])

    chain = service.approval_chain()
    for kind in ("processing_plan", "script", "voiceover"):
        check(not chain[kind].get("effective_valid"), f"{kind}-invalidated",
              f"{kind} onayi dil degisimine ragmen gecerli")

    # Nothing was translated: the old content is untouched, just unapproved.
    after_script = service.load_artifact("selected_script")["data"]["script_text"]
    after_voice = service.load_artifact("selected_voiceover")["data"]
    after_audio = (after_voice.get("output_sha256")
                   or after_voice.get("output_audio_sha256")
                   or after_voice.get("audio_path"))
    check(after_script == before_script, "script-not-translated",
          "mevcut script sessizce cevrildi")
    check(after_audio == before_audio, "audio-not-regenerated",
          "mevcut ses sessizce yeniden uretildi")

    state = service.voice_review_state()
    check(state["language_mismatch"] is True, "mismatch-reported",
          f"plan={state.get('planned_language')} ses={state.get('spoken_language')}")
    check(state["planned_language"] == "tr-TR" and state["spoken_language"] == "en-US",
          "mismatch-detail", str((state.get("planned_language"),
                                   state.get("spoken_language"))))
    naturalness = next(i for i in state["items"] if i["key"] == "naturalness")
    check(naturalness["label"] == "Turkce dogalligi", "review-relabelled",
          naturalness["label"])
    app.shutdown()


def section_checklist_survives_relabel():
    section("7. Yeniden etiketleme isaretleri silmiyor")
    app = make_app()
    pid, ownership, service = narrated_project(app, language="en-US")
    run_to_voiceover(app, pid, service)
    for key in voice_review.CHECKLIST_KEYS:
        service.set_voice_review_item(key, checked=True, reviewed_by="listener")
    check(service.voice_review_state()["done_count"] == 6, "complete-in-english", "")
    call(app, "PATCH", f"/api/projects/{pid}/plan",
         {"updated_by": "test", "content_language": "tr-TR"})
    state = service.voice_review_state()
    # The AUDIO did not change, so the ticks still bind -- a re-worded question
    # is not a different audio file.
    check(state["done_count"] == 6, "ticks-survive-relabel",
          f"{state['done_count']}/6")
    check(state["stale_count"] == 0, "not-marked-stale", str(state["stale_count"]))
    app.shutdown()


def section_frontend():
    section("8. Arayuz sozlesmesi (statik)")
    js = (PROJECT_ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    check("const CONTENT_LANGUAGES" in js, "js-language-list", "")
    check(js.count("data-testid': 'plan-language'") == 2, "js-both-plan-forms",
          f"{js.count(chr(39) + 'plan-language' + chr(39))} yerde")
    check("aria-required': 'true'" in js.split("plan-language")[0][-400:]
          or "'aria-required': 'true'" in js, "js-required-flag", "")
    check("content_language:" in js, "js-sends-language", "")
    check("review.language_mismatch" in js, "js-mismatch-alert", "")
    check("data-testid': 'voice-language'" in js, "js-language-badge", "")
    check("OTOMATIK CEVRILMEZ" in js, "js-no-silent-translation-warning",
          "dil degisiminin cevirmedigi soylenmiyor")
    check("function narrationPresetField(" in js, "js-preset-picker", "")
    check(js.count("narrationPresetField(") == 3, "js-preset-both-forms",
          str(js.count("narrationPresetField(")) + " kullanim")
    check("narration_preset:" in js, "js-sends-preset", "")
    check("review.preset_mismatch" in js, "js-preset-mismatch-alert", "")
    check("preset-rationale" in js, "js-voice-rationale-shown",
          "ses secim gerekcesi gosterilmiyor")


def section_narration_preset():
    section("9. deep_storyteller on ayari")
    from local_pipeline import narration_presets as presets
    from local_pipeline.google_ai import GEMINI_TTS_VOICES

    # The voice must EXIST. An invented id is the failure this guards.
    check(not presets.validate_presets(), "presets-use-real-voices",
          str(presets.validate_presets()))
    deep = presets.NARRATION_PRESETS["deep_storyteller"]
    check(deep["voice"] == "Charon", "selected-voice", deep["voice"])
    check(deep["voice"] in GEMINI_TTS_VOICES, "voice-in-registry", deep["voice"])
    check(presets.VOICE_CHARACTERISTICS[deep["voice"]] == "Informative",
          "voice-characteristic-recorded",
          presets.VOICE_CHARACTERISTICS.get(deep["voice"]))
    check(deep["language_code"] == "en-US", "preset-is-english", deep["language_code"])

    direction = deep["narration_direction"].lower()
    for needle, label in (("slower", "slower"), ("5-8%", "slower-amount"),
                          ("cinematic", "cinematic"), ("natural", "natural"),
                          ("pause", "pauses"), ("trailer", "no-trailer"),
                          ("do not rush", "no-rush"), ("do not shout", "no-shout")):
        check(needle in direction, "direction-" + label, direction[:70])
    # The tuned intent, stated as things the direction must NOT ask for.
    check("10-15" not in direction, "direction-not-old-pace", direction[:70])
    check("emphasis fall where it naturally would" in direction,
          "direction-natural-emphasis", direction[:90])
    check("do not deliberately stress" in direction, "direction-no-forced-stress",
          direction[:90])
    check("short and unforced" in direction, "direction-shorter-pauses",
          direction[:90])
    check("slight beat" in direction and "slightly longer beat" not in direction,
          "direction-shorter-final-beat", direction[:90])

    app = make_app()
    pid, ownership, service = narrated_project(app, language="en-US",
                                                preset="deep_storyteller")
    plan = service.load_artifact("processing_plan")["data"]
    narration = plan["decision"]["narration"]
    check(narration["preset"] == "deep_storyteller", "preset-in-decision",
          str(narration.get("preset")))
    check(narration["voice"] == "Charon", "voice-in-decision",
          str(narration.get("voice")))
    check(narration["narration_direction"] == deep["narration_direction"],
          "direction-in-decision", "yonerge planda saklanmiyor")

    before = approval.content_hash_for("processing_plan", plan)
    other = json.loads(json.dumps(plan))
    other["decision"]["narration"]["preset"] = "default"
    check(approval.content_hash_for("processing_plan", other) != before,
          "preset-changes-hash", "on ayar plan hash'ini degistirmiyor")

    run_to_voiceover(app, pid, service)
    voiceover = service.load_artifact("selected_voiceover")["data"]
    check(voiceover["voice_id"] == "Charon", "tts-uses-preset-voice",
          str(voiceover.get("voice_id")))
    check(voiceover.get("narration_preset") == "deep_storyteller",
          "voiceover-records-preset", str(voiceover.get("narration_preset")))
    check(voiceover.get("narration_direction") == deep["narration_direction"],
          "tts-uses-preset-direction", "yonerge TTS'e gitmedi")

    # A caller passing the old default must not override the approved delivery.
    result = service.run_tts(voice="Kore", narration_direction="", actor="test")
    check(result.get("voice_id") == "Charon", "plan-voice-wins-over-caller",
          str(result.get("voice_id")))

    # The checklist asks about what the preset actually promised.
    state = service.voice_review_state()
    check(state["narration_preset"] == "deep_storyteller", "review-knows-preset",
          str(state.get("narration_preset")))
    by_key = {item["key"]: item for item in state["items"]}
    check("proper names" in by_key["pronunciation"]["question"].lower(),
          "review-checks-proper-names", by_key["pronunciation"]["question"][:60])
    for name in ("Daemon", "Rhaenyra", "Targaryen"):
        check(name in by_key["pronunciation"]["question"],
              "review-names-" + name.lower(), by_key["pronunciation"]["question"][:70])
    tone = by_key["tone"]["question"].lower()
    # The question must track the preset: natural sentence-level emphasis, NOT
    # deliberate per-word stress, or the reviewer certifies the wrong delivery.
    check("naturally at sentence level" in tone, "review-checks-natural-emphasis",
          by_key["tone"]["question"][:80])
    check("every important word" in tone, "review-warns-against-forced-stress",
          by_key["tone"]["question"][:80])
    check("slightly slower" in tone, "review-checks-moderate-pace",
          by_key["tone"]["question"][:80])
    check("trailer" in tone, "review-checks-no-trailer", by_key["tone"]["question"][:70])
    check([i["key"] for i in state["items"]] == list(voice_review.CHECKLIST_KEYS),
          "review-keys-unchanged", str([i["key"] for i in state["items"]]))

    # Switching preset invalidates the voice approval and translates nothing.
    call(app, "POST", "/api/projects/" + pid + "/approvals/voiceover", {
        "reviewed_sha256": service.approval_state("voiceover")["content_sha256"],
        "approved_by": "test"})
    before_audio = service.load_artifact("selected_voiceover")["data"]["audio_path"]
    call(app, "PATCH", "/api/projects/" + pid + "/plan",
         {"updated_by": "test", "narration_preset": "default"})
    chain = service.approval_chain()
    for kind in ("processing_plan", "voiceover"):
        check(not chain[kind].get("effective_valid"), "preset-invalidates-" + kind,
              kind + " onayi on ayar degisimine ragmen gecerli")
    after = service.voice_review_state()
    check(after["preset_mismatch"] is True, "preset-mismatch-reported",
          str((after.get("planned_voice"), after.get("spoken_voice"))))
    check(service.load_artifact("selected_voiceover")["data"]["audio_path"]
          == before_audio, "audio-not-regenerated-on-preset-change",
          "on ayar degisince ses sessizce yeniden uretildi")

    # direct_clip_series still carries no narration at all.
    seed = fixtures.seed_demo_project(app, title="direct preset")
    dpid = seed["project_id"]
    dsvc = app.service(app.ownership(dpid))
    call(app, "POST", "/api/projects/" + dpid + "/plan", {
        "mode": "direct_clip_series", "selected_by": "test",
        "narration_preset": "deep_storyteller",
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    dplan = dsvc.load_artifact("processing_plan")["data"]
    check(dplan["decision"]["narration"] is None, "direct-has-no-narration",
          str(dplan["decision"].get("narration")))
    app.shutdown()


def main() -> int:
    print("=" * 72)
    print("ICERIK DILI (narrated_theory) UCTAN UCA DOGRULAMA")
    print("=" * 72)
    try:
        section_plan()
        section_direct_unchanged()
        section_prompt()
        app, pid, ownership, service = section_flow()
        section_tts_ignores_caller_default(app, pid, service)
        section_language_change(app, pid, ownership, service)
        section_checklist_survives_relabel()
        section_narration_preset()
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
    print("CONTENT_LANGUAGE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
