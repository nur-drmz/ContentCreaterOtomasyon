#!/usr/bin/env python
"""API tests for required-field validation and phase progress.

    python verify_forms_progress.py

Two subjects, both server-side, because both must be true even if the browser is
bypassed entirely:

  1. REQUIRED FIELDS -- the server rejects missing input and NAMES the field.
     The client-side asterisk is a courtesy; this is the part that is
     load-bearing. Includes the regression that mattered most: the API used to
     sign a plan "console" when the human forgot their name, forging exactly the
     accountability the approval gate exists to create.

  2. PROGRESS -- persisted, honest, and survives a restart. `determinate` must be
     false whenever nothing countable was counted, and no code path may invent a
     percentage.

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

from creator_console import fixtures, progress  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.errors import FIELD_LABELS  # noqa: E402

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


def make_app(root=None):
    root = root or Path(tempfile.mkdtemp(prefix="verify_fp_"))
    TMP.append(root)
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories())), root


def call(app, method, path, body=None, headers=None):
    raw = b"" if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    return ConsoleApi(app).handle(method, path, body=raw, headers=headers or {})


def error_fields(payload):
    return [item["name"] for item in (payload.get("error") or {}).get("fields", [])]


# ===========================================================================
def section_required_fields():
    section("1. Zorunlu alanlar sunucuda reddediliyor ve isimlendiriliyor")
    app, _ = make_app()

    for body, label in (({}, "anahtar yok"), ({"title": ""}, "bos"),
                        ({"title": "   "}, "sadece bosluk")):
        status, payload = call(app, "POST", "/api/projects", body)
        check(status == 400, f"title-rejected-{label}", f"HTTP {status}")
        check(payload["error"]["code"] == "missing_required_field",
              f"title-code-{label}", payload["error"]["code"])
        check(error_fields(payload) == ["title"], f"title-named-{label}",
              str(error_fields(payload)))
    status, payload = call(app, "POST", "/api/projects", {"title": "Gecerli"})
    check(status == 400, "operator-required-once", f"HTTP {status}")
    check(error_fields(payload) == ["operator_name"], "operator-named",
          str(error_fields(payload)))
    status, payload = call(app, "POST", "/api/projects", {
        "title": "Gecerli", "operator_name": "bix"})
    check(status == 201, "title-and-operator-accepted", f"HTTP {status}")

    seed = fixtures.seed_demo_project(app, title="alan testi")
    pid = seed["project_id"]

    # The project operator is intentionally reused for later human actions.
    status, payload = call(app, "POST", f"/api/projects/{pid}/plan",
                           {"mode": "direct_clip_series"})
    check(status == 201, "plan-reuses-project-operator", f"HTTP {status}")
    plan = app.service(app.ownership(pid)).load_artifact("processing_plan")["data"]
    check(plan["selected_by"] == "fixtures", "plan-signed-by-project-operator",
          str(plan.get("selected_by")))

    status, payload = call(app, "POST", f"/api/projects/{pid}/plan",
                           {"mode": "direct_clip_series", "selected_by": "   "})
    check(status == 400, "plan-whitespace-name-rejected", f"HTTP {status}")

    # narrated_theory needs a theory angle; the server, not only the form.
    status, payload = call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "narrated_theory", "selected_by": "tester", "theory_angle": "kisa"})
    check(status == 400, "theory-angle-enforced", f"HTTP {status}")

    # Approval also reuses the project operator when no duplicate field is sent.
    service = app.service(app.ownership(pid))
    digest = service.approval_state("processing_plan")["content_sha256"]
    status, _ = call(app, "POST", f"/api/projects/{pid}/approvals/processing_plan",
                     {"reviewed_sha256": digest})
    check(status == 200, "plan-approved-with-project-operator", f"HTTP {status}")
    approved = service.load_artifact("processing_plan")["data"]
    check(approved["human_approval"]["approved_by"] == "fixtures",
          "approval-signed-by-project-operator",
          str(approved["human_approval"].get("approved_by")))

    check(set(error_fields(payload)) <= set(FIELD_LABELS),
          "fields-from-closed-set", "kapali kume disinda alan adi dondu")
    app.shutdown()


def section_no_value_echo():
    section("2. Hata govdesi kullanici degerini yankilamiyor")
    app, _ = make_app()
    secret_ish = "COK-GIZLI-DEGER-1234"
    status, payload = call(app, "POST", "/api/projects",
                           {"title": "", "created_by": secret_ish})
    blob = json.dumps(payload, ensure_ascii=False)
    check(secret_ish not in blob, "no-value-echo", "girdi degeri yanitta")
    check("Proje adi" in blob, "label-present", "alan etiketi yok")
    check(set((payload.get("error") or {}).keys())
          <= {"code", "message", "hint", "retryable", "debug_id", "fields"},
          "payload-shape", str(list((payload.get("error") or {}).keys())))
    app.shutdown()


def section_progress_records():
    section("3. Ilerleme kaydi: asamalar, yuzde, gecen sure")
    app, _ = make_app()
    seed = fixtures.seed_demo_project(app, title="ilerleme")
    pid = seed["project_id"]
    own = app.ownership(pid)
    service = app.service(own)

    call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "tester",
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    digest = service.describe_plan_review()["review"]["content_sha256"]
    call(app, "POST", f"/api/projects/{pid}/approvals/processing_plan",
         {"reviewed_sha256": digest, "approved_by": "tester"})
    call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"whisper_model_size": "tiny", "clip_duration_sec": 3, "top_n": 2}})

    status, payload = call(app, "GET", f"/api/projects/{pid}/progress")
    check(status == 200, "progress-route", f"HTTP {status}")
    records = payload["progress"]

    assessment = records.get("source_assessment")
    check(assessment and assessment["state"] == "completed", "assessment-recorded",
          str((assessment or {}).get("state")))
    check([s["key"] for s in assessment["stages"]] == ["probe", "scoring", "write"],
          "assessment-stages", str([s["key"] for s in assessment["stages"]]))

    analysis = records.get("video_analysis")
    check(analysis and analysis["state"] == "completed", "analysis-recorded",
          str((analysis or {}).get("state")))
    check([s["key"] for s in analysis["stages"]]
          == ["preparation", "audio", "whisper", "scenes", "scoring", "previews"],
          "analysis-stage-list", str([s["key"] for s in analysis["stages"]]))
    check(all(s["state"] == "completed" for s in analysis["stages"]),
          "analysis-stages-completed", str([s["state"] for s in analysis["stages"]]))
    for field in ("step", "completed", "total", "percent", "message",
                  "elapsed_sec", "updated_at"):
        check(field in analysis, f"analysis-has-{field}", "alan yok")
    check(analysis["percent"] == 100 and analysis["determinate"] is True,
          "analysis-percent", f"%{analysis['percent']} det={analysis['determinate']}")
    check(analysis["elapsed_sec"] > 0, "analysis-elapsed",
          f"{analysis['elapsed_sec']}s")
    check(analysis["total"] == 6 and analysis["unit"] == "asama",
          "analysis-unit-is-stages",
          f"{analysis['completed']}/{analysis['total']} {analysis['unit']}")

    # Render stages, on the mode this project actually uses.
    hot = service.load_artifact("hot_moments")["data"]
    service.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                     reviewed_by="tester", require_preview=False)
    call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "render",
        "params": {"render_mode": "blurred_background", "preview_mode": True,
                    "subtitle_whisper_model_size": "small",
                    "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    render = call(app, "GET", f"/api/projects/{pid}/progress")[1]["progress"]["render"]
    check([s["key"] for s in render["stages"]]
          == ["preparation", "subtitles", "ffmpeg", "validation", "manifest"],
          "render-stage-list", str([s["key"] for s in render["stages"]]))
    check(render["state"] == "completed", "render-completed", render["state"])
    return app, pid, own


def section_honesty(app, pid):
    section("4. Sahte yuzde yok")
    records = call(app, "GET", f"/api/projects/{pid}/progress")[1]["progress"]
    for key, record in records.items():
        if record.get("determinate"):
            total = record.get("total")
            check(isinstance(total, int) and total > 0, f"{key}-real-total",
                  f"determinate ama total={total!r}")
            check(record["percent"] == round(100 * record["completed"] / total),
                  f"{key}-percent-derived",
                  f"%{record['percent']} != {record['completed']}/{total}")
        else:
            check(record.get("percent") is None, f"{key}-indeterminate-has-no-percent",
                  f"olculemez ama %{record.get('percent')}")

    # A tracker with no stage list must stay indeterminate, not guess.
    own = app.ownership(pid)
    tracker = progress.ProgressTracker(app.storage, own, "diagnostics", stages=[])
    tracker.start("olculemez is")
    record = progress.load_progress(app.storage, own)["diagnostics"]
    check(record["determinate"] is False and record["percent"] is None,
          "no-stages-indeterminate",
          f"det={record['determinate']} %{record['percent']}")
    tracker.note("hala calisiyor")
    record = progress.load_progress(app.storage, own)["diagnostics"]
    check(record["percent"] is None, "note-does-not-invent-percent",
          f"%{record['percent']}")


def section_failure(app, pid):
    section("5. Basarisizlik: son adim, hata, yeniden deneme")
    own = app.ownership(pid)
    service = app.service(own)
    # Corrupt the selection so render fails a precondition on a REAL run.
    service.path("moment_selection").write_text("{bozuk", encoding="utf-8")
    status, payload = call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "render", "run_request_id": "fail-probe",
        "params": {"render_mode": "blurred_background", "preview_mode": True,
                    "subtitle_whisper_model_size": "tiny",
                    "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    record = call(app, "GET", f"/api/projects/{pid}/progress")[1]["progress"]["render"]
    check(record["state"] == "failed", "render-failure-recorded", record["state"])
    check(bool(record["error"]), "failure-has-error", "hata metni yok")
    check("failed_step" in record and "failed_step_label" in record,
          "failure-has-last-step", str(record.get("failed_step")))
    check(record["finished_at"], "failure-finished-at", "bitis zamani yok")

    job_id = (payload.get("job") or {}).get("job_id")
    if job_id:
        app.retry_job(own, job_id, requested_by="tester")
        retried = call(app, "GET", f"/api/projects/{pid}/progress")[1]["progress"]["render"]
        check(retried["attempt"] >= 2 and retried["is_retry"] is True,
              "retry-marks-attempt",
              f"attempt={retried['attempt']} is_retry={retried['is_retry']}")


def section_restart():
    section("6. Yeniden baslatmadan sonra korunuyor")
    app, root = make_app()
    seed = fixtures.seed_demo_project(app, title="restart")
    pid = seed["project_id"]
    own = app.ownership(pid)
    call(app, "POST", f"/api/projects/{pid}/jobs", {"job_type": "source_assessment"})
    before = call(app, "GET", f"/api/projects/{pid}/progress")[1]["progress"]
    app.shutdown()

    restarted = ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))
    after = call(restarted, "GET", f"/api/projects/{pid}/progress")[1]["progress"]
    check(set(after) == set(before), "restart-same-activities",
          f"{sorted(after)} != {sorted(before)}")
    check(after["source_assessment"]["step"] == before["source_assessment"]["step"],
          "restart-same-step", str(after["source_assessment"]["step"]))
    check(after["source_assessment"]["started_at"]
          == before["source_assessment"]["started_at"],
          "restart-same-start", "baslangic zamani degisti")
    check(restarted.project_view(own)["progress"]["source_assessment"]["state"]
          == "completed", "restart-in-view-model", "gorunum modelinde yok")
    restarted.shutdown()


def section_interrupted():
    section("7. Olen isci: sonsuz donen bar yok")
    app, _ = make_app()
    seed = fixtures.seed_demo_project(app, title="kesinti")
    pid = seed["project_id"]
    own = app.ownership(pid)
    tracker = progress.ProgressTracker(app.storage, own, "video_analysis",
                                        job_id="job_dead")
    tracker.start("analiz basladi")
    tracker.stage("whisper", "transkript")
    check(progress.load_progress(app.storage, own)["video_analysis"]["state"] == "running",
          "running-before", "")

    progress.mark_interrupted(app.storage, own, "video_analysis", reason="isci oldu")
    record = progress.load_progress(app.storage, own)["video_analysis"]
    check(record["state"] == "interrupted", "interrupted-state", record["state"])
    check(record["failed_step"] == "whisper", "interrupted-keeps-last-step",
          record["failed_step"])
    check(record["finished_at"], "interrupted-finished", "bitis zamani yok")

    # A completed record must NOT be rewritten by a later recovery pass.
    tracker2 = progress.ProgressTracker(app.storage, own, "render")
    tracker2.start()
    tracker2.finish(message="bitti")
    progress.mark_interrupted(app.storage, own, "render", reason="gec kalan kurtarma")
    check(progress.load_progress(app.storage, own)["render"]["state"] == "completed",
          "completed-not-clobbered", "tamamlanan kayit ustune yazildi")
    app.shutdown()


def section_stopped_is_not_failed():
    section("8. Insan kapisinda bekleme 'basarisiz' degil")
    app, _ = make_app()
    seed = fixtures.seed_demo_project(app, title="otomasyon")
    pid = seed["project_id"]
    own = app.ownership(pid)
    app.automation_action(own, "start")
    record = progress.load_progress(app.storage, own).get("automation")
    check(record is not None, "automation-recorded", "otomasyon ilerlemesi yok")
    if record:
        check(record["state"] == "stopped", "automation-stopped-not-failed",
              record["state"])
        check(record["state"] != "failed", "waiting-is-not-red", record["state"])
        check(bool(record.get("blocker")), "automation-blocker", "engel metni yok")
        check(record["determinate"] is True and record["total"],
              "automation-countable",
              f"det={record['determinate']} total={record['total']}")
    app.shutdown()


def section_frontend_contract():
    section("9. Arayuz sozlesmesi (statik)")
    js = (PROJECT_ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "creator_console" / "static" / "app.css").read_text(encoding="utf-8")

    check("function field(spec)" in js, "shared-field-helper", "ortak alan yardimcisi yok")
    check("function validateFields(rules)" in js, "shared-validator", "ortak dogrulayici yok")
    check("function applyServerFieldErrors(error)" in js, "server-error-binding",
          "sunucu alan hatasi baglanmiyor")
    check("class: 'field' }" not in js, "no-raw-fields",
          "hala elle kurulmus alan var")
    check("aria-required" in js, "aria-required", "aria-required yok")
    check("field__star" in css and "var(--c-accent)" in css.split("field__star")[1][:80],
          "star-is-orange", "yildiz turuncu degil")
    check("requiredLegend" in js, "legend-helper", "efsane yardimcisi yok")
    check("(Opsiyonel)" in js, "optional-label", "opsiyonel etiketi yok")

    # The silent-default bugs must be gone for good.
    check("|| null" not in js.split("function screenPhase0")[-1][:6000]
          or "Number(document.getElementById('plan-count').value) || null" not in js,
          "no-silent-number-default", "sessiz sayi varsayilani hala var")
    check("selected_by: document.getElementById" not in js, "no-raw-selected-by",
          "selected_by dogrulanmadan gonderiliyor")
    check("new-project-operator" in js and "operator_name:" in js,
          "project-operator-captured-once", "proje ureticisi kaydedilmiyor")
    repeated_ids = ("plan-by", "moment-by", "script-editor-by", "lifecycle-actor",
                    "render-reviewer", "voice-reviewer")
    check(not any(value in js for value in repeated_ids),
          "no-repeated-actor-fields", "tekrar isim alani bulundu")
    check("`${kind}-hash`" not in js and "hash i buraya yapistir" not in js,
          "no-manual-hash-field", "manuel hash kopyalama alani bulundu")
    check("reviewed_sha256: approval.content_sha256" in js,
          "approval-still-hash-bound", "onay guncel icerik hash'ine bagli degil")

    check("role: 'progressbar'" in js, "progressbar-role", "role=progressbar yok")
    check("'aria-valuenow': determinate" in js, "valuenow-only-when-determinate",
          "olculemezken de aria-valuenow veriliyor")
    check("renderedProgress" in js, "duplicate-guard", "kopya panel korumasi yok")
    check(js.count("phaseProgress(") >= 7, "panels-mounted",
          f"{js.count('phaseProgress(')} montaj")
    check("pbar--indeterminate" in css, "indeterminate-style", "belirsiz bar stili yok")
    check("function scriptReadingPanel()" in js, "script-reading-panel",
          "script okuma paneli yok")
    check("data-testid': 'script-text'" in js or 'data-testid\': `script-text' in js
          or "script-text" in js, "script-text-rendered", "script metni cizilmiyor")
    check("script-hook-missing" in js, "hook-absence-stated",
          "hook yoklugu sessizce gizleniyor")
    check("pbar--inner" in js and "pbar--inner" in css, "inner-bar",
          "ic olculu bar yok")
    check("record.item_total && record.state === 'running'" in js,
          "inner-bar-only-when-measured", "ic bar olcum olmadan da ciziliyor")


def section_measured_ffmpeg():
    section("10. FFmpeg gercek kodlama ilerlemesi")
    from local_pipeline import render_utils

    app, _ = make_app()
    seed = fixtures.seed_demo_project(app, title="ffmpeg olcum")
    pid = seed["project_id"]
    own = app.ownership(pid)
    service = app.service(own)

    # A recording observer, so what run_ffmpeg reports is inspected directly
    # rather than inferred from a artifact written later.
    class Recorder:
        def __init__(self):
            self.counts = []

        def stage(self, key, message=""):
            pass

        def note(self, message):
            pass

        def count(self, completed, total, *, unit="", message=""):
            self.counts.append((completed, total, unit))

    ffmpeg_bin, _ = render_utils.resolve_ffmpeg_binaries()
    clip = Path(tempfile.mkdtemp(prefix="verify_ff_"))
    TMP.append(clip)
    source = clip / "src.mp4"
    fixtures.make_test_source_video(source, seconds=4)
    recorder = Recorder()
    render_utils.run_ffmpeg(ffmpeg_bin, [
        "-i", str(source), "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", str(clip / "out.mp4")],
        progress=recorder, total_duration_sec=4)

    check(recorder.counts, "ffmpeg-reports-progress", "hic ilerleme bildirilmedi")
    check(all(unit == "sn" for _, _, unit in recorder.counts), "ffmpeg-unit",
          str({unit for _, _, unit in recorder.counts}))
    check(all(0 <= done <= total for done, total, _ in recorder.counts),
          "ffmpeg-never-exceeds-total", str(recorder.counts[-3:]))
    check(recorder.counts[-1][0] == recorder.counts[-1][1],
          "ffmpeg-ends-at-total", str(recorder.counts[-1]))
    monotonic = all(a[0] <= b[0] for a, b in zip(recorder.counts, recorder.counts[1:]))
    check(monotonic, "ffmpeg-monotonic", "ilerleme geri gitti")
    check(Path(clip / "out.mp4").is_file(), "ffmpeg-still-encodes",
          "olculu yolda cikti uretilmedi")

    # No observer -> the original single-shot path, byte-for-byte behaviour.
    plain = render_utils.run_ffmpeg(ffmpeg_bin, [
        "-i", str(source), "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
        str(clip / "plain.mp4")])
    check(plain.returncode == 0 and (clip / "plain.mp4").is_file(),
          "ffmpeg-unmeasured-path-intact", "gozlemcisiz yol bozuldu")

    # A failing measured run must still raise with ffmpeg's own stderr.
    try:
        render_utils.run_ffmpeg(ffmpeg_bin, ["-i", str(clip / "yok.mp4"),
                                             str(clip / "nope.mp4")],
                                 progress=Recorder(), total_duration_sec=4)
        check(False, "ffmpeg-failure-raises", "hata firlatilmadi")
    except RuntimeError as exc:
        check("ffmpeg basarisiz" in str(exc), "ffmpeg-failure-raises", str(exc)[:60])
        check(len(str(exc)) > 30, "ffmpeg-failure-keeps-stderr",
              "stderr kaybedildi")
    app.shutdown()


def section_script_hook():
    section("11. Script hook artefakta ve gorunume tasiniyor")
    from local_pipeline import script_generation

    check(script_generation.script_extras(
        {"script": "x", "hook": "  Bir cumle. ", "estimated_duration_sec": "45"})
        == {"hook": "Bir cumle.", "estimated_duration_sec": 45},
        "extras-normalised", "normalizasyon yanlis")
    check(script_generation.script_extras({"script": "x"}) == {},
          "extras-absent-stays-absent", "olmayan alan uyduruldu")
    check(script_generation.script_extras(
        {"script": "x", "hook": "   ", "estimated_duration_sec": "abc"}) == {},
        "extras-invalid-dropped", "gecersiz deger tasindi")

    app, _ = make_app()
    seed = fixtures.seed_demo_project(app, title="hook akisi")
    pid = seed["project_id"]
    own = app.ownership(pid)
    service = app.service(own)
    call(app, "POST", f"/api/projects/{pid}/plan", {
        "mode": "narrated_theory", "selected_by": "tester",
        "theory_angle": "Bu sahnedeki karar bir guc gosterisi degil, caresizlik",
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
    digest = service.describe_plan_review()["review"]["content_sha256"]
    call(app, "POST", f"/api/projects/{pid}/approvals/processing_plan",
         {"reviewed_sha256": digest, "approved_by": "tester"})
    call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"whisper_model_size": "tiny", "clip_duration_sec": 3, "top_n": 2}})
    hot = service.load_artifact("hot_moments")["data"]
    service.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                     reviewed_by="tester", require_preview=False)
    call(app, "POST", f"/api/projects/{pid}/jobs", {"job_type": "script_generation"})

    artifact = service.load_artifact("selected_script")["data"]
    check(artifact.get("hook") == fixtures.DEMO_HOOK_TEXT, "hook-in-artifact",
          repr(artifact.get("hook")))
    check(artifact.get("estimated_duration_sec"), "duration-in-artifact",
          str(artifact.get("estimated_duration_sec")))

    view = app.project_view(own)["script"]
    check(view["data"]["hook"] == fixtures.DEMO_HOOK_TEXT, "hook-in-view",
          repr(view["data"]["hook"]))
    check(view["data"]["script_text"], "text-in-view", "script metni gorunumde yok")
    check(view["data"]["character_count"] == len(artifact["script_text"]),
          "character-count", str(view["data"]["character_count"]))
    # The reviewer view is a curated slice, not the raw artifact.
    check("provider_attempts" not in view["data"], "view-is-curated",
          "ham artefakt sayfaya dokuluyor")

    # A model that returns NO hook must produce a null hook, not a guess.
    from creator_console.app import ConsoleApp as _CA, ConsoleConfig as _CC
    import creator_console.fixtures as _fx
    no_hook = script_generation.build_selected_script_artifact(
        "metin", "google", "gemini-3.6-flash", 0.0, 0.1,
        {"rank": 1, "source_start_sec": 0, "source_end_sec": 5},
        {"approved": True, "approved_by": "t"}, {}, "hot.json")
    check(no_hook["hook"] is None, "no-hook-is-null", repr(no_hook["hook"]))
    check(no_hook["script_text"] == "metin", "no-hook-keeps-script", "")
    app.shutdown()


def main() -> int:
    print("=" * 72)
    print("ZORUNLU ALAN + FAZ ILERLEMESI DOGRULAMASI")
    print("=" * 72)
    try:
        section_required_fields()
        section_no_value_echo()
        app, pid, own = section_progress_records()
        section_honesty(app, pid)
        section_failure(app, pid)
        app.shutdown()
        section_restart()
        section_interrupted()
        section_stopped_is_not_failed()
        section_measured_ffmpeg()
        section_script_hook()
        section_frontend_contract()
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
    print("FORMS_PROGRESS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
