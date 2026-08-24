#!/usr/bin/env python
"""Creator Console verification suite.

    python verify_console.py

WHAT RUNS FOR REAL
------------------
ffmpeg (fixture video + render), faster-whisper (subtitles), PySceneDetect,
librosa, the real HTTP server over a real socket, real threads for concurrency,
real atomic file writes, real SHA-256 approval gates.

WHAT IS MOCKED
--------------
Every provider: Gemini/OpenAI/Anthropic/ElevenLabs clients are injected doubles
from `creator_console.fixtures`. No network call is made, no credential is real,
nothing is uploaded or published. All state lives in a temp directory that is
deleted at the end; the user's `results/` is never touched.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.ids import InvalidIdentifier, Ownership, safe_filename, validate_id  # noqa: E402
from creator_console.jobs import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REVIEW_REQUIRED,
    JobConflict,
    JobStore,
)
from creator_console.pipeline_service import PipelineService  # noqa: E402
from creator_console.secrets_ref import SecretResolver  # noqa: E402
from creator_console.server import ConsoleServer  # noqa: E402
from creator_console.storage import LocalFilesystemStorage, StorageViolation  # noqa: E402
from local_pipeline import approval as approval_module  # noqa: E402

FAILURES: list = []
TMP_DIRS: list = []


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def ok(label: str, detail: str = "") -> None:
    print(f"  [{label}] OK" + (f" — {detail}" if detail else ""))


def check(condition, label: str, message: str) -> None:
    if not condition:
        FAILURES.append(f"{label}: {message}")
        print(f"  [{label}] BASARISIZ — {message}")


def make_app(*, inline=True, google=True, **client_kwargs) -> ConsoleApp:
    root = Path(tempfile.mkdtemp(prefix="console_test_"))
    TMP_DIRS.append(root)
    config = ConsoleConfig(
        storage_root=root,
        inline_jobs=inline,
        secret_getter=fixtures.fake_secret_getter(google=google),
        **fixtures.fixture_client_factories(**client_kwargs),
    )
    return ConsoleApp(config)


def api_call(app, method, path, body=None, headers=None):
    api = ConsoleApi(app)
    raw = b"" if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    return api.handle(method, path, body=raw, headers=headers or {})


# ===========================================================================
section("1. Ownership identifiers and unsafe input")
# ===========================================================================
for bad in ("../escape", "a/b", "..", "", "  ", "/abs", "C:\\win", "a" * 80,
            "-lead", "nul.txt", "prj_x\x00y"):
    try:
        validate_id(bad)
    except InvalidIdentifier:
        continue
    check(False, "ids:reject-unsafe", f"gecersiz kimlik kabul edildi: {bad!r}")
ok("ids:reject-unsafe", "11 hostile kimlik reddedildi")

for hostile, forbidden in (("../../etc/passwd", ".."), ("C:\\Windows\\x.mp4", ":"),
                           ("a/b/c.mp4", "/"), ("....//x", "..")):
    cleaned = safe_filename(hostile)
    check(forbidden not in cleaned and "/" not in cleaned and "\\" not in cleaned,
          "ids:safe-filename", f"{hostile!r} -> {cleaned!r} hala tehlikeli")
check(safe_filename("con") != "con", "ids:safe-filename", "Windows ayrilmis adi korunmadi")
check(safe_filename("") != "", "ids:safe-filename", "bos ad bos kaldi")
ok("ids:safe-filename", "yol parcalari, surucu harfi ve ayrilmis adlar temizlendi")

# ===========================================================================
section("2. Storage namespace isolation")
# ===========================================================================
_iso_root = Path(tempfile.mkdtemp(prefix="console_iso_"))
TMP_DIRS.append(_iso_root)
_storage = LocalFilesystemStorage(_iso_root)
_own_a = Ownership("usr_a", "wsp_a", "prj_a")
_own_b = Ownership("usr_b", "wsp_b", "prj_b")
_storage.ensure_namespace(_own_a)
_storage.ensure_namespace(_own_b)

_storage.write_json(_own_b, "secret.json", {"owner": "b"})
for attack in ("../../wsp_b/projects/prj_b/secret.json", "../../../../etc/passwd",
               "/etc/passwd", "C:\\Windows\\System32\\config", "..\\..\\prj_b\\secret.json",
               "results/../../../prj_b/secret.json"):
    try:
        _storage.resolve(_own_a, attack)
    except StorageViolation:
        continue
    check(False, "storage:traversal", f"kacis engellenmedi: {attack!r}")
ok("storage:traversal", "6 yol-gezinme/mutlak-yol saldirisi reddedildi")

_a_root = _storage.namespace_root(_own_a).resolve()
_written = _storage.write_json(_own_a, "results/nested/deep.json", {"x": 1})
check(_a_root in _written.parents, "storage:stays-in-root", f"{_written} kok disinda")
check(_storage.read_json(_own_b, "secret.json") == {"owner": "b"},
      "storage:isolation", "B kendi dosyasini okuyamadi")
check(_storage.read_json(_own_a, "secret.json", default=None) is None,
      "storage:isolation", "A, B'nin dosyasini gordu!")
_names = _storage.list_dir(_own_a)
check("secret.json" not in _names, "storage:isolation", "listeleme capraz sizdirdi")
ok("storage:isolation", "workspace/proje namespace'leri birbirini goremiyor")

_relative = _storage.relative_to_namespace(_own_a, _written)
check(not Path(_relative).is_absolute() and str(_iso_root) not in _relative,
      "storage:no-absolute-leak", f"mutlak yol sizdi: {_relative}")
ok("storage:no-absolute-leak", "artefakt yollari namespace-goreli")

# ===========================================================================
section("3. Fixture-driven flow: source -> strategy -> job -> review -> output")
# ===========================================================================
_app = make_app()
_seed = fixtures.seed_demo_project(_app)
_pid = _seed["project_id"]
_own = _app.ownership(_pid)
_service = _app.service(_own)

check(_seed["selected_mode"] is None, "flow:no-auto-mode", "fixture kendi kendine mod secti")
_assessment = _service.load_artifact("source_assessment")
check(_assessment["data"] and _assessment["problem"] is None,
      "flow:assessment", f"degerlendirme uretilemedi: {_assessment['problem']}")
check(_assessment["data"]["status"] == "review_required", "flow:assessment", "status yanlis")
ok("flow:assessment", f"oneri={_seed['recommended_mode']}, secim yapilmadi")

# Every phase is closed until a plan is approved.
for phase in ("video_analysis", "moment_selection", "script_generation", "tts", "render"):
    allowed, reason = _service.plan_gate(phase)
    check(not allowed, "flow:gate-closed", f"{phase} plansiz acik")
ok("flow:gate-closed", "plan yokken 5 fazin hepsi kapali")
_auto_blocked = _app.automatic_next_step(_own, submit=False)
check(_auto_blocked["state"] == "blocked" and
      _auto_blocked["blocker"] == "plan_selection",
      "flow:auto-human-gate", "otomasyon insan plan secimini atladi")

_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console",
    "output_count": 1, "target_duration_sec": 45,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"},
})
check(_status == 201, "flow:plan-created", f"plan olusmadi: {_status} {_payload}")
check(_payload["plan"]["status"] == "review_required", "flow:plan-created", "plan dogustan onayli!")

_review = _service.describe_plan_review()
_plan_hash = _review["review"]["content_sha256"]
_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/approvals/processing_plan",
                             {"reviewed_sha256": "0" * 64, "approved_by": "verify_console"})
check(_status == 400, "flow:wrong-hash-rejected", f"yanlis hash kabul edildi: {_status}")
_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/approvals/processing_plan",
                             {"reviewed_sha256": _plan_hash, "approved_by": "verify_console"})
check(_status == 200 and _payload["artifact"]["status"] == "approved",
      "flow:plan-approved", f"onay basarisiz: {_status}")

# An approved Phase 0 plan remains editable, but editing must invalidate its
# old hash-bound approval and close every downstream gate until re-approval.
_status, _payload = api_call(_app, "PATCH", f"/api/projects/{_pid}/plan", {
    "updated_by": "plan_editor",
    "target_duration_sec": 46,
    "notes": "edited from Creator Console",
    "render_config": {"caption_theme": "auto"},
})
check(_status == 200, "flow:plan-edited", f"plan duzenlenemedi: {_status} {_payload}")
_edited = _payload["plan"]
check(_edited["status"] == "review_required", "flow:plan-edited",
      "duzenlenen plan eski onayi tasidi")
check(_edited["decision"]["target_duration_sec"] == 46 and
      _edited["decision"]["render"]["caption_theme"] == "auto" and
      _edited["notes"] == "edited from Creator Console",
      "flow:plan-edited", "duzenlenen alanlar kalici olmadi")
check(_edited["human_approval"]["approved"] is False,
      "flow:plan-edited", "eski human_approval temizlenmedi")
_allowed_after_edit, _reason_after_edit = _service.plan_gate("video_analysis")
check(not _allowed_after_edit, "flow:plan-edited", "plan editinden sonra faz kapisi acik kaldi")
_events_after_edit = _service.audit.events()
check(any(e.get("event_type") == "plan.updated" for e in _events_after_edit) and
      any(e.get("event_type") == "approval.invalidated" for e in _events_after_edit),
      "flow:plan-edited", "plan edit/invalidation audit kaydi eksik")

_edited_hash = _service.describe_plan_review()["review"]["content_sha256"]
_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/approvals/processing_plan",
                             {"reviewed_sha256": _edited_hash,
                              "approved_by": "verify_console"})
check(_status == 200 and _payload["artifact"]["status"] == "approved",
      "flow:plan-reapproved", "duzenlenen plan yeni hash ile onaylanamadi")
ok("flow:plan-approved", "yanlis hash reddedildi; plan editlendi ve yeni hash ile onaylandi")
_auto_ready = _app.automatic_next_step(_own, submit=False)
check(_auto_ready["state"] == "ready" and
      _auto_ready["next_job_type"] == "video_analysis",
      "flow:auto-next", f"otomasyon video analizini bulamadi: {_auto_ready}")

_same_request_a = _app._idempotency_key(
    _own, "video_analysis", {"top_n": 2}, run_request_id="manual-click-1")
_same_request_b = _app._idempotency_key(
    _own, "video_analysis", {"top_n": 2}, run_request_id="manual-click-1")
_new_request = _app._idempotency_key(
    _own, "video_analysis", {"top_n": 2}, run_request_id="manual-click-2")
check(_same_request_a == _same_request_b and _same_request_a != _new_request,
      "flow:manual-rerun", "manuel yeniden calistirma kimligi idempotent/benzersiz degil")

_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/jobs", {
    "job_type": "video_analysis",
    "params": {"whisper_model_size": "tiny", "clip_duration_sec": 6, "top_n": 2},
})
check(_status == 201, "flow:analysis-job", f"analiz isi kuyruga alinmadi: {_payload}")
_job = _payload["job"]
_job_final = _app.job_store(_own).get(_job["job_id"])
check(_job_final["status"] == STATUS_REVIEW_REQUIRED,
      "flow:analysis-job", f"beklenen review_required, alinan {_job_final['status']} "
                            f"({_job_final.get('error')})")
_hot = _service.load_artifact("hot_moments")
check(_hot["data"] and _hot["data"]["hot_moments"], "flow:analysis-job", "hot_moments uretilmedi")
_moment_view = _app.project_view(_own)["moments"]["data"][0]
check(not Path(_moment_view["preview_path"]).is_absolute() and
      str(_app.config.storage_root) not in _moment_view["preview_path"],
      "flow:moment-preview-route", "Moment Explorer mutlak host yolu sizdiriyor")
_status, _media = api_call(
    _app, "GET", f"/api/projects/{_pid}/media/{_moment_view['preview_path']}")
check(_status == 200 and Path(_media.get("__file__", "")).is_file(),
      "flow:moment-preview-route", "moment preview sahiplik-korumali rotadan acilmiyor")
ok("flow:analysis-job", f"{len(_hot['data']['hot_moments'])} aday moment (gercek ffmpeg + whisper)")

_selection = _service.record_moment_selection(
    selected_rank=_hot["data"]["hot_moments"][0]["rank"], reviewed_by="verify_console",
    require_preview=False)
check(_selection["status"] == "approved", "flow:moment-selection", "secim kaydedilmedi")
ok("flow:moment-selection", f"rank={_selection['selected_moment_rank']}")
_auto_after_selection = _app.automatic_next_step(_own, submit=False)
# direct_clip_series burns the source's OWN words in, so the transcript is a
# mandatory human-read step between selection and render -- not render.
check(_auto_after_selection["state"] == "ready" and
      _auto_after_selection["next_job_type"] == "transcript",
      "flow:auto-next", f"moment seciminden sonra transcript bulunamadi: {_auto_after_selection}")

# Produce and approve the caption transcript the render gate requires. Walked
# the way a user walks it: the gate is proven, not bypassed.
_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/jobs",
                             {"job_type": "transcript", "params": {
                                 "subtitle_whisper_model_size": "small",
                                 "subtitle_language": "auto"}})
_transcript_job = _app.job_store(_own).get(_payload["job"]["job_id"])
check(_transcript_job["status"] in (STATUS_REVIEW_REQUIRED, STATUS_COMPLETED),
      "flow:transcript",
      f"transkript isi {_transcript_job['status']}: {_transcript_job.get('error')}")
_transcript_state = _service.approval_state("transcript")
check(_transcript_state["present"] and _transcript_state.get("content_sha256"),
      "flow:transcript", f"transkript incelenemez: {_transcript_state}")
_service.approve("transcript", reviewed_sha256=_transcript_state["content_sha256"],
                 approved_by="verify")
check(_service.approval_state("transcript")["valid"], "flow:transcript-onay",
      f"transkript onayi gecerli degil: {_service.approval_state('transcript')}")
ok("flow:transcript", "transkript uretildi, okundu ve hash ile onaylandi")

_auto_after_transcript = _app.automatic_next_step(_own, submit=False)
check(_auto_after_transcript["state"] == "ready" and
      _auto_after_transcript["next_job_type"] == "render",
      "flow:auto-next-render",
      f"transkript onayindan sonra render bulunamadi: {_auto_after_transcript}")

fixtures.reset_provider_calls()
_status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/jobs",
                             {"job_type": "render", "params": {
                                 "render_mode": "blurred_background", "preview_mode": True,
                                 "subtitle_whisper_model_size": "small",
                                 "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_render_job = _app.job_store(_own).get(_payload["job"]["job_id"])
check(_render_job["status"] in (STATUS_REVIEW_REQUIRED, STATUS_COMPLETED),
      "flow:render", f"render isi {_render_job['status']}: {_render_job.get('error')}")
_outputs = _service.outputs()
check(_outputs and _outputs[0]["video"] and _outputs[0]["video"]["usable"],
      "flow:render", f"kullanilabilir video uretilmedi: {_outputs}")
ok("flow:render", f"gercek ffmpeg render, {_outputs[0]['video']['size_bytes']} bayt video")

# ===========================================================================
section("4. direct_clip_series makes ZERO provider calls")
# ===========================================================================
fixtures.reset_provider_calls()
for job_type in ("script_generation", "tts"):
    _status, _payload = api_call(_app, "POST", f"/api/projects/{_pid}/jobs",
                                 {"job_type": job_type})
    record = _app.job_store(_own).get(_payload["job"]["job_id"])
    check(record["status"] == STATUS_FAILED, "zero-api:blocked",
          f"{job_type} calisti: {record['status']}")
    check("direct_clip_series" in (record.get("error") or ""), "zero-api:blocked",
          f"{job_type} gerekcesi plan kapisi degil: {record.get('error')}")
check(not fixtures.PROVIDER_CALLS, "zero-api:no-calls",
      f"SIFIR cagri beklendi, yapilan: {fixtures.PROVIDER_CALLS}")
ok("zero-api:no-calls", "kimlik bilgisi yapilandirilmis olsa da 0 saglayici cagrisi")

# ===========================================================================
section("5. narrated_theory route + approval gating")
# ===========================================================================
_app2 = make_app()
_seed2 = fixtures.seed_demo_project(_app2, title="Narrated fixture")
_pid2 = _seed2["project_id"]
_own2 = _app2.ownership(_pid2)
_svc2 = _app2.service(_own2)

_status, _payload = api_call(_app2, "POST", f"/api/projects/{_pid2}/plan", {
    "mode": "narrated_theory", "selected_by": "verify_console", "output_count": 1,
    "theory_angle": "Bu sahnedeki karar tum sezonun donum noktasi teorisi",
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"},
})
check(_status == 201, "narrated:plan", f"plan olusmadi: {_payload}")
_hash2 = _svc2.describe_plan_review()["review"]["content_sha256"]
api_call(_app2, "POST", f"/api/projects/{_pid2}/approvals/processing_plan",
         {"reviewed_sha256": _hash2, "approved_by": "verify_console"})

api_call(_app2, "POST", f"/api/projects/{_pid2}/jobs",
         {"job_type": "video_analysis",
          "params": {"whisper_model_size": "tiny", "clip_duration_sec": 6, "top_n": 2}})
_hot2 = _svc2.load_artifact("hot_moments")["data"]
_svc2.record_moment_selection(selected_rank=_hot2["hot_moments"][0]["rank"],
                              reviewed_by="verify_console", require_preview=False)

fixtures.reset_provider_calls()
_status, _payload = api_call(_app2, "POST", f"/api/projects/{_pid2}/jobs",
                             {"job_type": "script_generation"})
_script_job = _app2.job_store(_own2).get(_payload["job"]["job_id"])
check(_script_job["status"] == STATUS_REVIEW_REQUIRED, "narrated:script",
      f"script isi {_script_job['status']}: {_script_job.get('error')}")
_script = _svc2.load_artifact("selected_script")["data"]
check(_script["status"] == "review_required", "narrated:script", "script dogustan onayli!")
check(len(fixtures.PROVIDER_CALLS) >= 1, "narrated:script", "sahte saglayici hic cagrilmadi")
ok("narrated:script", f"{len(fixtures.PROVIDER_CALLS)} sahte cagri, artefakt review_required")

fixtures.reset_provider_calls()
_status, _payload = api_call(_app2, "POST", f"/api/projects/{_pid2}/jobs", {"job_type": "tts"})
_tts_job = _app2.job_store(_own2).get(_payload["job"]["job_id"])
check(_tts_job["status"] == STATUS_FAILED, "narrated:tts-gate",
      "onaysiz scriptle TTS calisti!")
check(not any(call[0].startswith("google.generate_content") and "tts" in str(call[1])
              for call in fixtures.PROVIDER_CALLS),
      "narrated:tts-gate", "onaysiz scriptle TTS saglayicisi cagrildi")
ok("narrated:tts-gate", "script onaylanmadan TTS calismiyor")

_script_state = _svc2.approval_state("script")
api_call(_app2, "POST", f"/api/projects/{_pid2}/approvals/script",
         {"reviewed_sha256": _script_state["content_sha256"], "approved_by": "verify_console"})
fixtures.reset_provider_calls()
_status, _payload = api_call(_app2, "POST", f"/api/projects/{_pid2}/jobs", {"job_type": "tts"})
_tts_job = _app2.job_store(_own2).get(_payload["job"]["job_id"])
check(_tts_job["status"] == STATUS_REVIEW_REQUIRED, "narrated:tts",
      f"TTS isi {_tts_job['status']}: {_tts_job.get('error')}")
_voice = _svc2.load_artifact("selected_voiceover")["data"]
check(_voice["status"] == "review_required" and _voice["preview_model"] is True,
      "narrated:tts", "preview TTS ciktisi review_required degil")
ok("narrated:tts", "onayli script -> TTS -> review_required (preview model)")

# ===========================================================================
section("6. Stale approvals: upstream change invalidates downstream")
# ===========================================================================
_plan_before = _svc2.load_artifact("processing_plan")["data"]
check(approval_module.check_approval(_plan_before, "processing_plan")["valid"],
      "stale:baseline", "taze plan onayi gecersiz")

_mutated = json.loads(json.dumps(_plan_before))
_mutated["decision"]["target_duration_sec"] = 30
_svc2.write_artifact("processing_plan", _mutated)
_state = _svc2.approval_state("processing_plan")
check(_state["approved"] and not _state["valid"], "stale:plan",
      "plan karari degisti ama onay gecerli kaldi")
_invalidated = _svc2.invalidated_approvals()
check(any(item["kind"] == "processing_plan" for item in _invalidated),
      "stale:reported", "gecersiz onay raporlanmadi")
_audit_events = [e["event_type"] for e in
                 __import__("creator_console.audit", fromlist=["AuditLog"])
                 .AuditLog(_app2.storage, _own2).events()]
check("approval.granted" in _audit_events, "stale:audit-preserved",
      "gecersizlesme audit gecmisini sildi")
_svc2.write_artifact("processing_plan", _plan_before)
ok("stale:plan", "karar degisince onay dustu; audit kaydi SILINMEDI")

_script_artifact = _svc2.load_artifact("selected_script")["data"]
_script_artifact["script_text"] = _script_artifact["script_text"] + " (degistirildi)"
_svc2.write_artifact("selected_script", _script_artifact)
_state = _svc2.approval_state("script")
check(_state["approved"] and not _state["valid"], "stale:script",
      "script metni degisti ama onay gecerli kaldi")
ok("stale:script", "script icerigi degisince onay otomatik dustu")

# ===========================================================================
section("7. Idempotency, duplicate submission and safe retry")
# ===========================================================================
_app3 = make_app()
_seed3 = fixtures.seed_demo_project(_app3, title="Idempotency fixture")
_pid3, _own3 = _seed3["project_id"], None
_own3 = _app3.ownership(_pid3)
_svc3 = _app3.service(_own3)
api_call(_app3, "POST", f"/api/projects/{_pid3}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_h3 = _svc3.describe_plan_review()["review"]["content_sha256"]
api_call(_app3, "POST", f"/api/projects/{_pid3}/approvals/processing_plan",
         {"reviewed_sha256": _h3, "approved_by": "verify_console"})

_params = {"whisper_model_size": "tiny", "clip_duration_sec": 6, "top_n": 2}
_first_status, _first = api_call(_app3, "POST", f"/api/projects/{_pid3}/jobs",
                                  {"job_type": "video_analysis", "params": _params})
_second_status, _second = api_call(_app3, "POST", f"/api/projects/{_pid3}/jobs",
                                    {"job_type": "video_analysis", "params": _params})
check(_first_status == 201 and _first["created"] is True, "idempotency:first",
      "ilk gonderim yeni is olusturmadi")
check(_second_status == 200 and _second["created"] is False and _second["deduplicated"],
      "idempotency:duplicate", f"ikinci gonderim yeni is olusturdu: {_second}")
check(_first["job"]["job_id"] == _second["job"]["job_id"], "idempotency:same-job",
      "ayni girdiler farkli is uretti")
_jobs = _app3.job_store(_own3).list(job_type="video_analysis")
check(len(_jobs) == 1, "idempotency:one-record", f"{len(_jobs)} is kaydi olustu")
ok("idempotency:duplicate", "cift tiklama ikinci bir is/cagri uretmiyor")

_completed = _app3.job_store(_own3).get(_first["job"]["job_id"])
try:
    _app3.job_store(_own3).request_retry(_completed["job_id"])
    check(False, "idempotency:retry-finished", "biten is yeniden kuyruga alinabildi")
except JobConflict:
    ok("idempotency:retry-finished", "basariyla biten is tekrar calistirilamiyor")

# Concurrent claim: exactly one worker may own a job.
_store3 = _app3.job_store(_own3)
_record, _ = _store3.submit("render", idempotency_key="concurrency-test", params={})
_claims, _errors = [], []


def _claimer(worker):
    try:
        _claims.append(_store3.claim(_record["job_id"], worker)["claimed_by"])
    except JobConflict:
        _errors.append(worker)


_threads = [threading.Thread(target=_claimer, args=(f"worker-{i}",)) for i in range(6)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
check(len(_claims) == 1, "idempotency:single-claim",
      f"{len(_claims)} worker ayni isi claim etti: {_claims}")
check(len(_errors) == 5, "idempotency:single-claim", f"beklenen 5 red, alinan {len(_errors)}")
ok("idempotency:single-claim", "6 es zamanli worker'dan yalnizca 1'i isi aldi")

# ===========================================================================
section("8. Refresh / restart / crash recovery")
# ===========================================================================
_stuck, _ = _store3.submit("tts", idempotency_key="crash-test", params={})
_store3.claim(_stuck["job_id"], "doomed-worker")
_raw = _store3.get(_stuck["job_id"])
_raw["lease_expires_at"] = "2000-01-01T00:00:00+00:00"  # simulate a dead worker
_store3._save(_raw)
_recovered = _store3.recover_abandoned()
check(len(_recovered) == 1, "recovery:abandoned", f"{len(_recovered)} is kurtarildi")
_after = _store3.get(_stuck["job_id"])
check(_after["status"] == STATUS_FAILED and _after["recoverable"] is True,
      "recovery:abandoned", f"terk edilen is {_after['status']}/{_after['recoverable']}")
ok("recovery:abandoned", "worker olunce is 'running' takilmiyor, kurtarilabilir 'failed' oluyor")

# A "restart" is a brand-new app object over the SAME storage root.
_restarted = ConsoleApp(ConsoleConfig(
    storage_root=_app3.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(),
    **fixtures.fixture_client_factories()))
_view = _restarted.project_view(_restarted.ownership(_pid3))
check(_view["project"]["data"] is not None, "recovery:restart", "restart sonrasi proje kayboldu")
check(any(j["job_id"] == _first["job"]["job_id"] for j in _view["jobs"]),
      "recovery:restart", "restart sonrasi is gecmisi kayboldu")
# Recovery is OWNER-AWARE: a job whose claiming process is still ALIVE (here,
# this test process) must NOT be reclaimed -- that is what makes an external
# `creator_console.worker` safe to run beside the server. Only a provably dead
# owner is reclaimed.
_live_owner = [j for j in _view["jobs"] if j["status"] == "running"]
check(all(_store3.get(j["job_id"]).get("owner_pid") == os.getpid() for j in _live_owner),
      "recovery:restart", "yalnizca sahibi CANLI olan isler running kaldi")
_dead = _store3.get(_stuck["job_id"])
_dead.update({"status": "running", "owner_pid": 0x7FFFFFF0,
              "claimed_by": "olu-worker", "lease_expires_at": None})
_store3._save(_dead)
_restarted2 = ConsoleApp(ConsoleConfig(
    storage_root=_app3.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(),
    **fixtures.fixture_client_factories()))
_reclaimed = _restarted2.job_store(_own3).get(_stuck["job_id"])
check(_reclaimed["status"] == STATUS_FAILED and _reclaimed["recoverable"] is True,
      "recovery:restart-dead-owner",
      f"olu sahipli is kurtarilmadi: {_reclaimed['status']}")
ok("recovery:restart", "proje/is gecmisi korundu; olu sahipli is kurtarildi, "
   "canli sahipli is elinden alinmadi")

_retried = _restarted.retry_job(_restarted.ownership(_pid3), _stuck["job_id"])
check(_retried["status"] in ("queued", "failed", "review_required", "completed"),
      "recovery:safe-retry", f"beklenmedik durum: {_retried['status']}")
check(_retried["retry_of"] == _stuck["job_id"], "recovery:safe-retry", "retry zinciri kayitli degil")
ok("recovery:safe-retry", "kurtarilabilir is guvenle yeniden calistirilabiliyor")

# ===========================================================================
section("9. Cross-project / cross-workspace access is refused")
# ===========================================================================
_other_app = make_app()
_other_seed = fixtures.seed_demo_project(_other_app, title="Baska workspace")
_status, _payload = api_call(_app3, "GET", f"/api/projects/{_other_seed['project_id']}")
check(_status == 404, "isolation:cross-project", f"baska projeye erisildi: {_status}")
for hostile in ("../wsp_other", "..%2f..%2fetc", "prj_../../x", "/etc/passwd", "prj_yok"):
    _status, _ = api_call(_app3, "GET", f"/api/projects/{hostile}")
    check(_status in (400, 403, 404), "isolation:hostile-id",
          f"{hostile!r} icin beklenmedik durum: {_status}")
ok("isolation:hostile-id", "capraz proje ve dusmanca kimlikler 400/403/404 ile reddedildi")

_status, _ = api_call(_app3, "GET",
                      f"/api/projects/{_pid3}/media/../../../../etc/passwd")
check(_status in (400, 403, 404), "isolation:media-traversal",
      f"medya yol-gezinmesi engellenmedi: {_status}")
_status, _ = api_call(_app3, "GET", f"/api/projects/{_pid3}/media/project.json")
check(_status in (200, 403, 404), "isolation:media-type", "beklenmedik medya yaniti")
ok("isolation:media-traversal", "medya uc noktasi namespace disina cikamiyor")

# ===========================================================================
section("10. Secret leakage")
# ===========================================================================
_secret_value = fixtures.fake_secret_getter()("GeminiKey")
_leak_app = make_app()
_leak_seed = fixtures.seed_demo_project(_leak_app, title="Secret leak fixture")
_leak_pid = _leak_seed["project_id"]
_leak_own = _leak_app.ownership(_leak_pid)

_bodies = []
for method, path, body in (
    ("GET", "/api/workspace", None),
    ("GET", f"/api/projects/{_leak_pid}", None),
    ("GET", f"/api/projects/{_leak_pid}/jobs", None),
    ("GET", f"/api/projects/{_leak_pid}/audit", None),
    ("POST", f"/api/projects/{_leak_pid}/jobs", {"job_type": "script_generation"}),
    ("POST", f"/api/projects/{_leak_pid}/plan", {"mode": "gecersiz-mod", "selected_by": "x"}),
):
    _status, _payload = api_call(_leak_app, method, path, body)
    _bodies.append(json.dumps(_payload, ensure_ascii=False, default=str))
_all_bodies = "\n".join(_bodies)
check(_secret_value not in _all_bodies, "secrets:api", "API yaniti secret degeri icerdi!")
check("GeminiKey" in _all_bodies, "secrets:api", "alias adi hic raporlanmadi (durum gorunmuyor)")
check(not re.search(r"AIza[0-9A-Za-z_\-]{10,}", _all_bodies), "secrets:api", "anahtar deseni sizdi")
ok("secrets:api", "6 uc nokta yaniti secret degeri icermiyor, yalnizca configured/missing")

_files = []
for path in _leak_app.storage.root.rglob("*"):
    if path.is_file() and path.suffix in (".json", ".jsonl", ".txt"):
        _files.append(path.read_text(encoding="utf-8", errors="ignore"))
check(_secret_value not in "\n".join(_files), "secrets:artifacts",
      "secret degeri artefakt/audit dosyasina yazildi!")
ok("secrets:artifacts", f"{len(_files)} artefakt/audit dosyasinda secret yok")

_static_dir = PROJECT_ROOT / "creator_console" / "static"
_bundle = "\n".join(p.read_text(encoding="utf-8") for p in _static_dir.rglob("*")
                    if p.is_file() and p.suffix in (".js", ".css", ".html"))
for forbidden in ("GOOGLE_API_KEY", "GeminiKey", "api_key", "AIza", "Authorization"):
    check(forbidden not in _bundle, "secrets:bundle",
          f"frontend paketinde yasakli ifade: {forbidden}")
check("local_pipeline" not in _bundle, "frontend:no-pipeline-logic",
      "frontend local_pipeline'a atif yapiyor (notebook mantigi kopyalanmis olabilir)")
ok("secrets:bundle", "frontend paketinde secret adi/degeri veya pipeline importu yok")

_resolver = SecretResolver(fixtures.fake_secret_getter())
_public = json.dumps(_resolver.public_state())
check(_secret_value not in _public, "secrets:resolver", "SecretResolver degeri sizdirdi")
check(_resolver.credential("google_api").configured, "secrets:resolver",
      "sunucu tarafinda kimlik cozulemedi")
ok("secrets:resolver", "deger yalnizca sunucu tarafinda, public state'te yok")

# ===========================================================================
section("11. Corrupted artifacts are reported, never accepted")
# ===========================================================================
_corrupt_app = make_app()
_corrupt_seed = fixtures.seed_demo_project(_corrupt_app, title="Corruption fixture")
_corrupt_pid = _corrupt_seed["project_id"]
_corrupt_own = _corrupt_app.ownership(_corrupt_pid)
_corrupt_svc = _corrupt_app.service(_corrupt_own)
_assessment_path = _corrupt_svc.path("source_assessment")

for label, payload in (
    ("bos", b""),
    ("kesik", b'{"artifact_type": "source_ass'),
    ("bozuk-json", b"{ bu json degil }"),
    ("liste", b"[]"),
    ("eksik-alan", json.dumps({"artifact_type": "source_assessment", "status": "review_required"}).encode()),
    ("uyumsuz-versiyon", json.dumps({"artifact_type": "source_assessment", "status": "review_required",
                                      "source": {}, "mode_scores": {}, "schema_version": 99}).encode()),
    ("production_ready", json.dumps({"artifact_type": "source_assessment", "status": "production_ready",
                                      "source": {}, "mode_scores": {}, "schema_version": 1}).encode()),
):
    _assessment_path.write_bytes(payload)
    loaded = _corrupt_svc.load_artifact("source_assessment")
    check(loaded["data"] is None and loaded["problem"],
          "corrupt:detected", f"{label} bozuk artefakt kabul edildi")
ok("corrupt:detected", "7 bozulma turu (bos/kesik/bozuk/tip/eksik alan/versiyon/production_ready) yakalandi")

_status, _payload = api_call(_corrupt_app, "GET", f"/api/projects/{_corrupt_pid}")
check(_status == 200, "corrupt:view-survives", "bozuk artefakt tum sayfayi cokertti")
check(_payload["phase0"]["assessment_problem"], "corrupt:view-surfaced",
      "bozulma kullaniciya bildirilmedi")
ok("corrupt:view-survives", "bozuk artefakt sayfayi cokertmiyor, kullaniciya bildiriliyor")

# ===========================================================================
section("12. Partial render: manifest says done, media is not")
# ===========================================================================
_manifest_path = _service.path("render_manifest")
_video_path = Path((_service.load_artifact("render_manifest")["data"]["output"])["final_video_path"])
_backup = _video_path.read_bytes()
_video_path.write_bytes(b"")
_outputs = _service.outputs()
check(_outputs[0]["problem"], "partial:zero-byte", "0 baytlik video sorun olarak bildirilmedi")
check(not _outputs[0]["video"]["usable"], "partial:zero-byte", "0 baytlik video kullanilabilir sayildi")
_status, _ = api_call(_app, "GET",
                      f"/api/projects/{_pid}/media/{_outputs[0]['video']['relative_path']}")
check(_status == 422, "partial:media-endpoint", f"bos medya {_status} ile sunuldu")

_video_path.unlink()
_outputs = _service.outputs()
check("diskte yok" in (_outputs[0]["video"]["problem"] or ""), "partial:missing-media",
      f"eksik medya bildirilmedi: {_outputs[0]}")
_render_state = _service.approval_state("render")
check(not _render_state.get("reviewable"), "partial:no-approval",
      "medyasi olmayan render onaylanabilir gorunuyor")
_video_path.write_bytes(_backup)
ok("partial:missing-media", "0 bayt / eksik medya: kullanilamaz, sunulmaz, onaylanamaz")

# ===========================================================================
section("13. Real HTTP server over a real socket")
# ===========================================================================
_http_app = make_app(inline=True)
_http_seed = fixtures.seed_demo_project(_http_app, title="HTTP fixture")
_server = ConsoleServer(_http_app, host="127.0.0.1", port=0, quiet=True)
_server.serve_in_thread()
_base = f"http://127.0.0.1:{_server.server_address[1]}"
try:
    with urllib.request.urlopen(f"{_base}/api/health", timeout=10) as response:
        _health = json.loads(response.read().decode("utf-8"))
    check(_health["ok"] and _health["not_uploaded"], "http:health", "saglik yaniti yanlis")

    with urllib.request.urlopen(f"{_base}/", timeout=10) as response:
        _html = response.read().decode("utf-8")
    check("Creator Console" in _html and "app.js" in _html, "http:index", "index sunulmadi")

    with urllib.request.urlopen(f"{_base}/static/app.css", timeout=10) as response:
        _css = response.read().decode("utf-8")
    check("--c-accent" in _css, "http:static", "CSS token'lari sunulmadi")

    with urllib.request.urlopen(f"{_base}/api/workspace", timeout=10) as response:
        _ws = json.loads(response.read().decode("utf-8"))
    check(_secret_value not in json.dumps(_ws), "http:no-secret", "HTTP yaniti secret icerdi")

    try:
        urllib.request.urlopen(f"{_base}/static/../creator_console/app.py", timeout=10)
        check(False, "http:static-traversal", "statik yol gezinme engellenmedi")
    except urllib.error.HTTPError as exc:
        check(exc.code in (403, 404), "http:static-traversal", f"beklenmedik kod {exc.code}")

    try:
        ConsoleServer(_http_app, host="0.0.0.0", port=0, quiet=True)
        check(False, "http:public-bind", "kimlik dogrulamasiz public bind engellenmedi")
    except RuntimeError:
        pass
    ok("http:real-socket", "gercek soket: health, index, static, workspace, traversal, public-bind")
finally:
    _server.shutdown()
    _server.server_close()

# ===========================================================================
section("14. Frontend contract (static analysis — no headless browser)")
# ===========================================================================
_js = (_static_dir / "app.js").read_text(encoding="utf-8")
_css_text = (_static_dir / "app.css").read_text(encoding="utf-8")
_html_text = (_static_dir / "index.html").read_text(encoding="utf-8")

for token in ("--c-bg", "--c-surface-1", "--c-accent", "--c-ok", "--c-warn", "--c-danger",
              "--r-md", "--sp-4", "--shadow-1"):
    check(token in _css_text, "frontend:tokens", f"tasarim token'i eksik: {token}")
check("#ff7a18" in _css_text, "frontend:orange", "turuncu aksiyon rengi tanimli degil")
check("#0a0a0b" in _css_text or "#141416" in _css_text, "frontend:dark", "siyah/komur yuzey yok")
check(":focus-visible" in _css_text, "frontend:a11y", "gorunur odak stili yok")
check("prefers-reduced-motion" in _css_text, "frontend:a11y", "hareket azaltma destegi yok")
check("@media (max-width: 900px)" in _css_text and "@media (max-width: 560px)" in _css_text,
      "frontend:responsive", "responsive kirilma noktalari eksik")
check("skip-link" in _html_text and 'aria-live' in _html_text, "frontend:a11y",
      "atlama baglantisi / canli bolge yok")

# Server flags need a control, or they are features nobody can reach. The
# copyright gate shipped without one and made plan approval impossible; these
# two exist so that does not repeat silently.
check("'plan-theory-auto'" in _js, "frontend:auto-theory-angle-anahtari",
      "teori acisini otomatik turetme kutusu arayuzde yok")
check("theory_angle_auto" in _js, "frontend:auto-theory-angle-GONDERILIYOR",
      "kutu isaretlense de sunucuya bayrak gitmiyor")
# And the typed field has to disappear when derivation is on, otherwise the
# form asks for something the server will refuse alongside the flag.
check("!decision.theory_angle_auto" in _js,
      "frontend:otomatikken-elle-aci-alani-GIZLENIYOR",
      "ikisi birden gonderilirse sunucu reddeder")
for screen in ("projects", "phase0", "jobs", "moments", "script", "voice", "render",
               "outputs", "settings"):
    check(f"'{screen}'" in _js or f'"{screen}"' in _js, "frontend:screens",
          f"ekran tanimli degil: {screen}")
for status in ("queued", "running", "review_required", "failed", "completed"):
    check(status in _js, "frontend:job-states", f"is durumu UI'da yok: {status}")
check("aria-pressed" in _js and "aria-current" in _js, "frontend:a11y", "ARIA durumlari eksik")
check("reviewed_sha256" in _js, "frontend:approval", "hash-bagli onay alani yok")
check("planned_not_implemented" in _js, "frontend:honesty",
      "uygulanmamis yetenekler 'planlandi' olarak gosterilmiyor")
check("disabled = true" in _js, "frontend:double-click", "cift tiklama korumasi yok")
ok("frontend:contract", "token/renk/odak/responsive/9 ekran/5 durum/ARIA/hash-onay dogrulandi")

# ===========================================================================
section("15. Artifact contract preserved end to end")
# ===========================================================================
_final_view = _app.project_view(_own)
check(_final_view["not_uploaded"] is True, "contract:not-uploaded", "not_uploaded bayragi yok")
_view_blob = json.dumps(_final_view, ensure_ascii=False, default=str)
check("production_ready" not in _view_blob.replace('"production_ready": 0', ""),
      "contract:no-production-ready", "gorunumde production_ready var")
for job in _final_view["jobs"]:
    check(job["status"] in ("queued", "running", "review_required", "failed", "completed"),
          "contract:job-status", f"gecersiz is durumu: {job['status']}")
check(all(key in _final_view for key in
          ("phase0", "phase_gates", "approvals", "jobs", "outputs", "audit", "secrets")),
      "contract:view-model", "gorunum modeli eksik bolum iceriyor")
ok("contract:preserved", "execution/artifact ayrimi, not_uploaded ve durum sozlugu korundu")


# ===========================================================================
section("16. Automatic flow: persisted state machine")
# ===========================================================================
from creator_console.automation import (  # noqa: E402
    HUMAN_GATES,
    MAX_NO_PROGRESS_TICKS,
    MAX_PHASE_ATTEMPTS,
    STATE_BLOCKED,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_PAUSED,
    STATE_RUNNING,
)

_auto_app = make_app()
_auto_seed = fixtures.seed_demo_project(_auto_app, title="Automation fixture")
_auto_pid = _auto_seed["project_id"]
_auto_own = _auto_app.ownership(_auto_pid)
_auto_svc = _auto_app.service(_auto_own)
_auto_engine = _auto_app.automation(_auto_own)

_view = _auto_engine.view()
check(_view["state"] == "idle" and _view["can_start"],
      "auto:initial-idle", f"baslangic durumu={_view['state']}")

# The flow must STOP at plan selection: it may never choose a mode.
_auto_app.automation_action(_auto_own, "start")
_view = _auto_engine.view()
check(_view["state"] == STATE_BLOCKED and _view["blocker"] == "plan_selection",
      "auto:stops-at-plan", f"state={_view['state']} blocker={_view['blocker']}")
check(_auto_svc.load_artifact("processing_plan")["data"] is None,
      "auto:no-mode-chosen", "otomasyon kendi kendine plan OLUSTURMADI")
check("mod secemez" in (_view["message"] or "").lower() or "SEC" in (_view["message"] or ""),
      "auto:actionable-message", f"mesaj: {_view['message'][:60]!r}")

# Persistence: a brand-new app object over the same storage sees the same state.
_auto_restart = ConsoleApp(ConsoleConfig(
    storage_root=_auto_app.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(), **fixtures.fixture_client_factories()))
_restart_view = _auto_restart.automation(_auto_own).view()
check(_restart_view["state"] == STATE_BLOCKED and _restart_view["blocker"] == "plan_selection",
      "auto:survives-restart", f"restart sonrasi state={_restart_view['state']}")
check((_auto_app.storage.namespace_root(_auto_own) / "results" / "automation.json").is_file(),
      "auto:persisted-artifact", "automation.json diske yazildi")

# Pause / resume / cancel are real transitions, persisted each time.
_auto_app.automation_action(_auto_own, "pause")
check(_auto_engine.view()["state"] == STATE_PAUSED, "auto:pause", "duraklatildi")
_tick_while_paused = _auto_app.automation_action(_auto_own, "tick")
check(_tick_while_paused["state"] == STATE_PAUSED,
      "auto:tick-noop-when-paused", "duraklatilmisken tick is baslatmiyor")
_auto_app.automation_action(_auto_own, "cancel")
check(_auto_engine.view()["state"] == STATE_CANCELLED, "auto:cancel", "iptal edildi")

# ---- direct_clip_series: runs machine phases, stops at moment selection ----
_status, _payload = api_call(_auto_app, "POST", f"/api/projects/{_auto_pid}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_hash = _auto_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_auto_app, "POST", f"/api/projects/{_auto_pid}/approvals/processing_plan",
         {"reviewed_sha256": _hash, "approved_by": "verify_console"})

_auto_app.automation_action(_auto_own, "start")   # submits video_analysis inline
for _ in range(4):
    _view = _auto_app.automation_action(_auto_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
check(_auto_svc.load_artifact("hot_moments")["data"] is not None,
      "auto:runs-machine-phase", "otomasyon video analizini calistirdi")
check(_view["state"] == STATE_BLOCKED and _view["blocker"] == "moment_selection",
      "auto:stops-at-moment", f"state={_view['state']} blocker={_view['blocker']}")
check(_auto_svc.load_artifact("moment_selection")["data"] is None,
      "auto:no-moment-chosen", "otomasyon kendi kendine moment SECMEDI")

# Ticking a blocked flow repeatedly must not loop forever.
for _ in range(MAX_NO_PROGRESS_TICKS + 3):
    _view = _auto_app.automation_action(_auto_own, "tick")
check(_view["state"] == STATE_BLOCKED, "auto:no-infinite-loop",
      f"tekrarli tick sonrasi state={_view['state']}")
_jobs_now = _auto_app.job_store(_auto_own).list()
check(len([j for j in _jobs_now if j["job_type"] == "video_analysis"]) == 1,
      "auto:no-duplicate-submits",
      f"{len([j for j in _jobs_now if j['job_type'] == 'video_analysis'])} analiz isi")

# Human does their part; the flow resumes and reaches render approval.
_hot = _auto_svc.load_artifact("hot_moments")["data"]
_auto_svc.record_moment_selection(selected_rank=_hot["hot_moments"][0]["rank"],
                                  reviewed_by="verify_console", require_preview=False)
_auto_app.automation_action(_auto_own, "resume")
for _ in range(5):
    _view = _auto_app.automation_action(_auto_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
# The captions are the source's own words, so automation runs the recognition
# itself but must STOP for a human to read it -- it may not approve captions
# any more than it may approve a narrated script.
check(_view["blocker"] == "transcript_approval" and _view["state"] == STATE_BLOCKED,
      "auto:stops-at-transcript", f"blocker={_view['blocker']} state={_view['state']}")
check(_auto_svc.load_artifact("moment_transcript")["data"] is not None,
      "auto:runs-transcript-phase", "otomasyon transkripti kendi calistirdi")
check(_auto_svc.approval_state("transcript")["approved"] is False,
      "auto:no-auto-transcript-approval", "otomasyon transkripti ONAYLADI")

_auto_transcript_state = _auto_svc.approval_state("transcript")
_auto_svc.approve("transcript", reviewed_sha256=_auto_transcript_state["content_sha256"],
                  approved_by="verify_console")
_auto_app.automation_action(_auto_own, "resume")
for _ in range(5):
    _view = _auto_app.automation_action(_auto_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
check(_view["blocker"] == "render_approval" and _view["state"] == STATE_BLOCKED,
      "auto:stops-at-render-approval", f"blocker={_view['blocker']} state={_view['state']}")
check(_auto_svc.approval_state("render")["approved"] is False,
      "auto:no-auto-approval", "otomasyon render i ONAYLAMADI")
check(_auto_svc.load_artifact("render_manifest")["data"]["status"] == "review_required",
      "auto:render-review-required", "render artefakti review_required")

# Approve the render like a human and confirm the flow completes.
_render_state = _auto_svc.approval_state("render")
if _render_state.get("reviewable"):
    # Final approval now ALSO requires the human review checklist, so the
    # simulated human completes it before approving -- exactly the real flow.
    from creator_console.render_review import CHECKLIST_KEYS as _AUTO_CHECKS

    for _check_key in _AUTO_CHECKS:
        api_call(_auto_app, "POST", f"/api/projects/{_auto_pid}/render-review",
                 {"action": "set_item", "key": _check_key, "checked": True,
                  "reviewed_by": "verify_console"})
    api_call(_auto_app, "POST", f"/api/projects/{_auto_pid}/approvals/render",
             {"reviewed_sha256": _render_state["content_sha256"],
              "approved_by": "verify_console"})
    _auto_app.automation_action(_auto_own, "resume")
    _view = _auto_app.automation_action(_auto_own, "tick")
    check(_view["state"] == STATE_COMPLETED, "auto:completes",
          f"insan onayindan sonra state={_view['state']}")
else:
    check(False, "auto:completes",
          f"render onaylanabilir degil: {_render_state.get('blocker')}")

# ---- narrated_theory: stops at script AND voice approvals ------------------
_nt_app = make_app()
_nt_seed = fixtures.seed_demo_project(_nt_app, title="Automation narrated")
_nt_pid = _nt_seed["project_id"]
_nt_own = _nt_app.ownership(_nt_pid)
_nt_svc = _nt_app.service(_nt_own)
api_call(_nt_app, "POST", f"/api/projects/{_nt_pid}/plan", {
    "mode": "narrated_theory", "selected_by": "verify_console", "output_count": 1,
    "theory_angle": "Bu sahnedeki karar tum sezonu degistiren donum noktasidir",
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_nt_hash = _nt_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_nt_app, "POST", f"/api/projects/{_nt_pid}/approvals/processing_plan",
         {"reviewed_sha256": _nt_hash, "approved_by": "verify_console"})

_nt_app.automation_action(_nt_own, "start")
for _ in range(4):
    _view = _nt_app.automation_action(_nt_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
_nt_hot = _nt_svc.load_artifact("hot_moments")["data"]
_nt_svc.record_moment_selection(selected_rank=_nt_hot["hot_moments"][0]["rank"],
                                reviewed_by="verify_console", require_preview=False)
fixtures.reset_provider_calls()
_nt_app.automation_action(_nt_own, "resume")
for _ in range(4):
    _view = _nt_app.automation_action(_nt_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
check(_view["blocker"] == "script_approval",
      "auto:narrated-stops-at-script", f"blocker={_view['blocker']}")
check(_nt_svc.load_artifact("selected_script")["data"]["status"] == "review_required",
      "auto:script-review-required", "script review_required")

_script_state = _nt_svc.approval_state("script")
api_call(_nt_app, "POST", f"/api/projects/{_nt_pid}/approvals/script",
         {"reviewed_sha256": _script_state["content_sha256"], "approved_by": "verify_console"})
_nt_app.automation_action(_nt_own, "resume")
for _ in range(4):
    _view = _nt_app.automation_action(_nt_own, "tick")
    if _view["state"] != STATE_RUNNING:
        break
check(_view["blocker"] == "voiceover_approval",
      "auto:narrated-stops-at-voice", f"blocker={_view['blocker']}")

# ---- failure / insufficient_data must block, not loop ---------------------
_fail_app = make_app()
_fail_seed = fixtures.seed_demo_project(_fail_app, title="Automation failure")
_fail_pid = _fail_seed["project_id"]
_fail_own = _fail_app.ownership(_fail_pid)
_fail_svc = _fail_app.service(_fail_own)
api_call(_fail_app, "POST", f"/api/projects/{_fail_pid}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_fail_hash = _fail_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_fail_app, "POST", f"/api/projects/{_fail_pid}/approvals/processing_plan",
         {"reviewed_sha256": _fail_hash, "approved_by": "verify_console"})
# Corrupt the source so analysis genuinely fails.
_fail_svc.source_path().write_bytes(b"bu bir video degil")
_fail_app.automation_action(_fail_own, "start")
for _ in range(MAX_NO_PROGRESS_TICKS + 4):
    _view = _fail_app.automation_action(_fail_own, "tick")
_fail_jobs = _fail_app.job_store(_fail_own).list(job_type="video_analysis")
check(_view["state"] == STATE_BLOCKED, "auto:failure-blocks",
      f"basarisiz fazda state={_view['state']}")
check(len(_fail_jobs) <= MAX_PHASE_ATTEMPTS, "auto:phase-attempt-cap",
      f"{len(_fail_jobs)} analiz denemesi (ust sinir {MAX_PHASE_ATTEMPTS})")
check(all(j["status"] == STATUS_FAILED for j in _fail_jobs), "auto:failure-recorded",
      "basarisizlik is kaydinda")

check(set(HUMAN_GATES) == {"plan_selection", "moment_selection", "script_approval",
                            "voiceover_approval", "transcript_approval",
                            "render_approval"},
      "auto:gate-vocabulary", f"insan kapilari: {HUMAN_GATES}")
ok("auto:state-machine", "kalici durum makinesi: 5 insan kapisinda duruyor, "
   "restart'i asiyor, dongu yapmiyor, hicbir seyi onaylamiyor")

# ===========================================================================
section("17. Manual controls: deliberate rerun vs duplicate delivery")
# ===========================================================================
_man_app = make_app()
_man_seed = fixtures.seed_demo_project(_man_app, title="Manual controls")
_man_pid = _man_seed["project_id"]
_man_own = _man_app.ownership(_man_pid)
_man_svc = _man_app.service(_man_own)
api_call(_man_app, "POST", f"/api/projects/{_man_pid}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_man_hash = _man_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_man_app, "POST", f"/api/projects/{_man_pid}/approvals/processing_plan",
         {"reviewed_sha256": _man_hash, "approved_by": "verify_console"})

_params = {"whisper_model_size": "tiny", "clip_start_sec": 0, "clip_duration_sec": 6,
           "scene_detector": "content", "top_n": 2}

# Same run_request_id twice == duplicate HTTP delivery / double click.
_s1, _p1 = api_call(_man_app, "POST", f"/api/projects/{_man_pid}/jobs", {
    "job_type": "video_analysis", "params": _params, "run_request_id": "click-1"})
_s2, _p2 = api_call(_man_app, "POST", f"/api/projects/{_man_pid}/jobs", {
    "job_type": "video_analysis", "params": _params, "run_request_id": "click-1"})
check(_p1["created"] is True and _p2["created"] is False and _p2["deduplicated"],
      "manual:double-click-idempotent",
      f"created={_p1['created']}/{_p2['created']}")
check(_p1["job"]["job_id"] == _p2["job"]["job_id"], "manual:same-job",
      "ayni run_request_id ayni isi doner")

# A NEW deliberate click == new run_request_id == genuine rerun.
_s3, _p3 = api_call(_man_app, "POST", f"/api/projects/{_man_pid}/jobs", {
    "job_type": "video_analysis", "params": _params, "run_request_id": "click-2"})
check(_p3["created"] is True and _p3["job"]["job_id"] != _p1["job"]["job_id"],
      "manual:deliberate-rerun", "yeni tiklama GERCEK yeniden calistirma uretti")

# Changed parameters must also produce a new run even with the same click id.
_s4, _p4 = api_call(_man_app, "POST", f"/api/projects/{_man_pid}/jobs", {
    "job_type": "video_analysis",
    "params": {**_params, "top_n": 3}, "run_request_id": "click-2"})
check(_p4["created"] is True, "manual:param-change-reruns",
      "degisen parametre yeni is uretti")

_all_analysis = _man_app.job_store(_man_own).list(job_type="video_analysis")
check(len(_all_analysis) == 3, "manual:exact-run-count",
      f"{len(_all_analysis)} analiz isi (2 kasitli tiklama + 1 parametre degisikligi)")

# Re-analysis must invalidate a previously recorded selection.
_hot_now = _man_svc.load_artifact("hot_moments")["data"]
_man_svc.record_moment_selection(selected_rank=_hot_now["hot_moments"][0]["rank"],
                                 reviewed_by="verify_console", require_preview=False)
_selection_before = _man_svc.load_artifact("moment_selection")["data"]
check(_selection_before is not None, "manual:selection-recorded", "secim kaydedildi")
_mutated_hot = json.loads(json.dumps(_hot_now))
_mutated_hot["hot_moments"][0]["score"] = 0.00001
_man_svc.write_artifact("hot_moments", _mutated_hot)
_errors = _man_svc.load_artifact("moment_selection")
_stale = _man_app.project_view(_man_own)["moments"]["selection_problem"]
check(bool(_stale) or bool(_man_svc.approval_chain()["render"]["upstream_invalid"]),
      "manual:reanalysis-invalidates-selection",
      f"yeniden analiz sonrasi secim bayat: {bool(_stale)}")
ok("manual:controls", "kasitli yeniden calistirma gercek, cift teslimat idempotent, "
   "yeniden analiz eski secimi sessizce gecerli birakmiyor")

# ===========================================================================
section("18. Moment Explorer: no host path reaches the browser")
# ===========================================================================
_view = _man_app.project_view(_man_own)
_blob = json.dumps(_view, ensure_ascii=False, default=str)
_root_text = str(_man_app.storage.root)
check(_root_text not in _blob, "moments:no-storage-root",
      "gorunum modelinde depolama koku yok")


def _absolute_strings(value, path=""):
    """Every string in the view that is a REAL absolute path.

    Structural, not regex: a regex over the JSON blob both false-positives on
    "https://" and false-negatives on escaped Windows separators. `is_absolute()`
    plus an explicit UNC check has neither problem.
    """
    hits = []
    if isinstance(value, str):
        candidate = value.strip()
        if candidate and (Path(candidate).is_absolute()
                          or candidate.startswith("\\\\")
                          or (len(candidate) > 2 and candidate[1] == ":"
                              and candidate[0].isalpha()
                              and candidate[2] in ("\\", "/"))):
            hits.append((path, candidate[:80]))
    elif isinstance(value, dict):
        for key, item in value.items():
            hits += _absolute_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits += _absolute_strings(item, f"{path}[{index}]")
    return hits


_abs_hits = _absolute_strings(_view)
check(not _abs_hits, "moments:no-absolute-path",
      f"gorunumde mutlak yol: {_abs_hits[:3]}")

# Positive control: the checker must be able to FAIL. A test that cannot fail is
# not a test.
_control = _absolute_strings({"leak": str(PROJECT_ROOT / "x.mp4")})
check(len(_control) == 1, "moments:checker-can-fail",
      f"pozitif kontrol {len(_control)} sonuc verdi (1 bekleniyordu)")

check("/Users/" not in _blob, "moments:no-home-path",
      "yanitta ev dizini yok")
for _moment in (_view["moments"]["data"] or []):
    _preview = _moment.get("preview_path")
    if _preview:
        check(not Path(str(_preview)).is_absolute(), "moments:relative-preview",
              f"preview yolu goreli degil: {_preview}")
        _status, _ = api_call(_man_app, "GET",
                              f"/api/projects/{_man_pid}/media/{_preview}")
        check(_status == 200, "moments:preview-served",
              f"preview medya rotasindan sunulmuyor: HTTP {_status}")
        break

# A legacy absolute Windows path in the artifact must be normalised, not leaked.
_legacy = json.loads(json.dumps(_man_svc.load_artifact("hot_moments")["data"]))
_legacy["hot_moments"][0]["preview_path"] = r"C:\\Users\\someone\\results\\preview.mp4"
_man_svc.write_artifact("hot_moments", _legacy)
_legacy_view = _man_app.project_view(_man_own)
_legacy_moment = (_legacy_view["moments"]["data"] or [{}])[0]
check("C:" not in json.dumps(_legacy_view, ensure_ascii=False, default=str),
      "moments:legacy-path-normalised",
      f"eski mutlak yol sizdi: {_legacy_moment.get('preview_path')}")
check(_legacy_moment.get("preview_path") is None or
      not Path(str(_legacy_moment.get("preview_path"))).is_absolute(),
      "moments:legacy-path-safe",
      f"preview_path={_legacy_moment.get('preview_path')} "
      f"problem={_legacy_moment.get('preview_problem')}")
ok("moments:media-isolation", "ham host yolu API yanitina girmiyor; eski mutlak yollar "
   "ya goreli medya rotasina cevriliyor ya da acikca isaretleniyor")

# ===========================================================================
# ===========================================================================
section("19. Preflight checks and the pipeline workflow map")
# ===========================================================================
from creator_console import progress as progress_module  # noqa: E402
from creator_console.status import (  # noqa: E402
    STATUS_FAIL,
    STATUS_OK,
    build_workflow_map,
    run_preflight,
)

_pf_app = make_app()
_pf = _pf_app.preflight()
check(_pf["ok"] is True, "preflight:local-env-ok", _pf["summary"])
_names = {c["name"] for c in _pf["checks"]}
for _required in ("ffmpeg", "dep:faster_whisper", "dep:scenedetect", "dep:librosa",
                   "storage", "secret:google_api"):
    check(_required in _names, "preflight:covers", f"{_required} kontrolu yok")
check(all(c["status"] in ("ok", "warn", "fail") for c in _pf["checks"]),
      "preflight:status-vocabulary", "gecersiz durum degeri")
check(all(c["status"] != "fail" or c["fix"] for c in _pf["checks"]),
      "preflight:actionable", "her engel icin cozum metni var")
_pf_blob = json.dumps(_pf)
check(fixtures.fake_secret_getter()("GeminiKey") not in _pf_blob,
      "preflight:no-secret-value", "preflight secret degeri sizdirmiyor")
check("configured" in _pf_blob or "missing" in _pf_blob,
      "preflight:secret-state-only", "yalnizca durum raporlaniyor")

_pf_seed = fixtures.seed_demo_project(_pf_app, title="Preflight fixture")
_pf_own = _pf_app.ownership(_pf_seed["project_id"])
_pf_project = _pf_app.preflight(_pf_own)
check(any(c["name"] == "source_video" and c["status"] == STATUS_OK
          for c in _pf_project["checks"]),
      "preflight:source-detected", "proje kaynak videosu tespit edildi")

# --- Workflow map -----------------------------------------------------------
# Every judgement the map shows is made on the server, so this is where it is
# proven: step states, mode-aware skipping, failure surfacing, and the fact that
# it moves as real work lands.
_pf_svc = _pf_app.service(_pf_own)
_wf = build_workflow_map(_pf_svc, _pf_own)

_KEYS = [s["key"] for s in _wf["steps"]]
check(_KEYS == ["source", "assessment", "plan", "analysis", "moment",
                "transcript", "script", "voice", "render", "review"],
      "workflow:step-order", f"sira={_KEYS}")
check(all(s["state"] in ("completed", "current", "waiting", "blocked",
                          "failed", "skipped") for s in _wf["steps"]),
      "workflow:state-vocabulary",
      f"gecersiz durum: {[s['state'] for s in _wf['steps']]}")
check(len([s for s in _wf["steps"] if s["state"] == "current"]) == 1,
      "workflow:exactly-one-current",
      f"{len([s for s in _wf['steps'] if s['state'] == 'current'])} adim 'current'")
check(all(s["tab"] for s in _wf["steps"]), "workflow:every-step-has-tab",
      "sekmesiz adim var")
check(_wf["current_step"] == "plan", "workflow:current-step",
      f"su anki adim={_wf['current_step']}")
check(_wf["current_tab"] == "phase0", "workflow:current-tab",
      f"hedef sekme={_wf['current_tab']}")
check(_wf["current_action"], "workflow:actionable",
      f"eylem metni: {_wf['current_action'][:50]!r}")
check(0 < _wf["percent"] < 100, "workflow:percent",
      f"%{_wf['percent']} ({_wf['done_count']}/{_wf['total_count']})")

# Before a mode is chosen NOTHING may be marked skipped: the decision has not
# been made yet, and pre-skipping would make the workflow silently grow later.
check(_wf["mode"] is None and _wf["skipped_count"] == 0,
      "workflow:no-premature-skip",
      f"mod={_wf['mode']} atlanan={_wf['skipped_count']}")
check(_wf["total_count"] == 10, "workflow:full-length-before-mode",
      f"{_wf['total_count']} adim")

api_call(_pf_app, "POST", f"/api/projects/{_pf_own.project_id}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_pf_hash = _pf_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_pf_app, "POST", f"/api/projects/{_pf_own.project_id}/approvals/processing_plan",
         {"reviewed_sha256": _pf_hash, "approved_by": "verify_console"})
_after = build_workflow_map(_pf_svc, _pf_own)
check(_after["done_count"] > _wf["done_count"], "workflow:advances",
      f"{_wf['done_count']} -> {_after['done_count']} adim")
check(_after["current_step"] == "analysis", "workflow:next-step",
      f"siradaki adim={_after['current_step']}")

# direct_clip_series SKIPS narration, and says why.
_states = {step["key"]: step for step in _after["steps"]}
check(_states["script"]["state"] == "skipped" and _states["voice"]["state"] == "skipped",
      "workflow:direct-clip-skips-narration",
      f"script={_states['script']['state']} voice={_states['voice']['state']}")
check(_states["script"]["skipped_reason"] and _states["voice"]["skipped_reason"],
      "workflow:skip-explained", "atlanan adim gerekcesini soylemiyor")
check(_after["total_count"] == 8 and _after["skipped_count"] == 2,
      "workflow:skipped-not-counted",
      f"toplam={_after['total_count']} atlanan={_after['skipped_count']}")
check(_states["render"]["state"] == "waiting",
      "workflow:downstream-waiting", f"render={_states['render']['state']}")

# narrated_theory keeps them, on an independent project.
_nt_seed = fixtures.seed_demo_project(_pf_app, title="Narrated workflow fixture")
_nt_own = _pf_app.ownership(_nt_seed["project_id"])
_nt_svc = _pf_app.service(_nt_own)
api_call(_pf_app, "POST", f"/api/projects/{_nt_own.project_id}/plan", {
    "mode": "narrated_theory", "selected_by": "verify_console", "output_count": 1,
    "theory_angle": "Dogrulama icin gecerli uzunlukta bir teori acisi metni",
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_nt_hash = _nt_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_pf_app, "POST", f"/api/projects/{_nt_own.project_id}/approvals/processing_plan",
         {"reviewed_sha256": _nt_hash, "approved_by": "verify_console"})
_nt_wf = build_workflow_map(_nt_svc, _nt_own)
_nt_states = {step["key"]: step["state"] for step in _nt_wf["steps"]}
check(_nt_states["script"] != "skipped" and _nt_states["voice"] != "skipped",
      "workflow:narrated-keeps-narration",
      f"script={_nt_states['script']} voice={_nt_states['voice']}")
# narrated_theory writes its own script, so the source-transcript step is the
# one that drops out -- the mirror image of direct_clip_series skipping script
# and voice.
check(_nt_states["transcript"] == "skipped", "workflow:narrated-skips-transcript",
      f"transcript={_nt_states['transcript']}")
check(_nt_wf["total_count"] == 9 and _nt_wf["skipped_count"] == 1,
      "workflow:narrated-full-length",
      f"toplam={_nt_wf['total_count']} atlanan={_nt_wf['skipped_count']}")
check(_nt_wf["mode"] == "narrated_theory", "workflow:mode-reported", str(_nt_wf["mode"]))

# A FAILED job surfaces on its own step, with a repair line.
_failed_wf = build_workflow_map(_nt_svc, _nt_own, jobs=[
    {"job_type": "video_analysis", "status": "failed",
     "error": "ffmpeg cikis kodu 1 (enjekte edildi)"}])
_failed_step = {s["key"]: s for s in _failed_wf["steps"]}["analysis"]
check(_failed_step["state"] == "failed", "workflow:job-failure-surfaces",
      f"analiz adimi={_failed_step['state']}")
check("enjekte" in _failed_step["blocker"], "workflow:failure-message",
      _failed_step["blocker"])
check(_failed_step["repair"], "workflow:failure-repair", "cozum metni yok")
check(_failed_wf["current_step"] == "analysis"
      and _failed_wf["current_state"] == "failed",
      "workflow:failure-becomes-current",
      f"{_failed_wf['current_step']}/{_failed_wf['current_state']}")

# A phase that failed and was then re-run successfully is NOT failed any more.
_recovered = build_workflow_map(_nt_svc, _nt_own, jobs=[
    {"job_type": "video_analysis", "status": "completed"},
    {"job_type": "video_analysis", "status": "failed", "error": "eski hata"}])
check({s["key"]: s["state"] for s in _recovered["steps"]}["analysis"] != "failed",
      "workflow:stale-failure-ignored",
      "eski basarisiz is hala 'failed' gosteriliyor")

# A BLOCKED step: corrupt an artifact and the map must say so and offer a fix.
_blocked_seed = fixtures.seed_demo_project(_pf_app, title="Blocked workflow fixture")
_blocked_own = _pf_app.ownership(_blocked_seed["project_id"])
_blocked_svc = _pf_app.service(_blocked_own)
_blocked_svc.path("source_assessment").write_text("{bozuk json", encoding="utf-8")
_blocked_wf = build_workflow_map(_blocked_svc, _blocked_own)
_blocked_step = {s["key"]: s for s in _blocked_wf["steps"]}["assessment"]
check(_blocked_step["state"] == "blocked", "workflow:blocked-state",
      f"degerlendirme adimi={_blocked_step['state']}")
check(_blocked_step["blocker"] and _blocked_step["repair"],
      "workflow:blocked-has-blocker-and-repair",
      f"engel={_blocked_step['blocker'][:40]!r} cozum={_blocked_step['repair'][:40]!r}")
check(_blocked_wf["current_blocker"] and _blocked_wf["current_repair"],
      "workflow:blocked-surfaces-at-top", "ust ozette engel/cozum yok")

# The map survives a restart, because it is derived from persisted artifacts.
_restart_app = ConsoleApp(ConsoleConfig(
    storage_root=_pf_app.storage.root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(),
    **fixtures.fixture_client_factories()))
_restart_wf = build_workflow_map(_restart_app.service(_pf_own), _pf_own)
check([s["state"] for s in _restart_wf["steps"]] == [s["state"] for s in _after["steps"]],
      "workflow:survives-restart", "restart sonrasi durumlar degisti")
_restart_app.shutdown()

# The workflow map lives under ONE name. `progress` is a different thing --
# per-phase job progress, keyed by activity -- and must never carry steps, or a
# second indicator could be built from it by mistake.
_view = _pf_app.project_view(_pf_own)
check("workflow" in _view, "workflow:single-view-field", f"anahtarlar={sorted(_view)[:6]}")
check(_view["workflow"]["steps"][0]["key"] == "source", "workflow:in-view-model", "")
check("steps" not in (_view.get("progress") or {}),
      "workflow:progress-is-not-a-second-map",
      f"progress anahtarlari={sorted((_view.get('progress') or {}))}")
check(set(_view.get("progress") or {}) <= set(progress_module.ACTIVITY_KEYS),
      "workflow:progress-keyed-by-activity",
      f"beklenmeyen anahtar={set(_view.get('progress') or {}) - set(progress_module.ACTIVITY_KEYS)}")
ok("preflight-workflow", "yerel ortam kontrolu ve akis haritasi calisiyor")

# ===========================================================================
section("20. Render Studio review: checklist gates final approval")
# ===========================================================================
from creator_console.render_review import CHECKLIST_KEYS, RENDER_REVIEW_ARTIFACT  # noqa: E402

_rr_app = make_app()
_rr_seed = fixtures.seed_demo_project(_rr_app, title="Render review")
_rr_pid = _rr_seed["project_id"]
_rr_own = _rr_app.ownership(_rr_pid)
_rr_svc = _rr_app.service(_rr_own)
api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/plan", {
    "mode": "direct_clip_series", "selected_by": "verify_console", "output_count": 1,
    "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                       "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_rr_hash = _rr_svc.describe_plan_review()["review"]["content_sha256"]
api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/approvals/processing_plan",
         {"reviewed_sha256": _rr_hash, "approved_by": "verify_console"})
api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/jobs", {
    "job_type": "video_analysis",
    "params": {"whisper_model_size": "tiny", "clip_duration_sec": 6, "top_n": 2}})
_rr_hot = _rr_svc.load_artifact("hot_moments")["data"]
_rr_svc.record_moment_selection(selected_rank=_rr_hot["hot_moments"][0]["rank"],
                                reviewed_by="verify_console", require_preview=False)
# The render gate needs an approved transcript in this mode; walk it.
api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/jobs", {
    "job_type": "transcript",
    "params": {"subtitle_whisper_model_size": "tiny", "subtitle_language": "auto"}})
_rr_tr = _rr_svc.approval_state("transcript")
_rr_svc.approve("transcript", reviewed_sha256=_rr_tr["content_sha256"],
                approved_by="verify_console")
api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/jobs", {"job_type": "render", "params": {
    "render_mode": "blurred_background", "preview_mode": True,
    "subtitle_whisper_model_size": "small", "caption_style": "active_word_yellow",
    "caption_theme": "auto"}})

_state = _rr_svc.render_review_state()
check(_state["done_count"] == 0 and _state["total_count"] == 6,
      "review:six-items", f"{_state['done_count']}/{_state['total_count']}")
check(set(item["key"] for item in _state["items"]) == set(CHECKLIST_KEYS),
      "review:item-vocabulary", "6 sabit kontrol maddesi")
_tech = _state["technical"]
check(_tech["available"] and _tech["resolution"] and _tech["duration_sec"],
      "review:technical-summary",
      f"{_tech['resolution']} · {_tech['duration_sec']:.2f}s · {_tech['subtitle_source']}")
check(all(field in _tech for field in
          ("duration_sec", "resolution", "video_codec", "audio_present",
           "subtitle_source", "status")),
      "review:technical-fields", "sure/cozunurluk/codec/ses/altyazi/durum mevcut")

_render_approval = _rr_svc.approval_state("render")
_status, _payload = api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/approvals/render",
                             {"reviewed_sha256": _render_approval["content_sha256"],
                              "approved_by": "verify_console"})
check(_status == 400 and _payload["error"]["code"] == "approval_rejected",
      "review:gates-approval", f"HTTP {_status} kod={_payload.get('error', {}).get('code')}")
check(_rr_svc.load_artifact("render_manifest")["data"]["status"] == "review_required",
      "review:no-approval-leak", "liste eksikken render onaylanmadi")

# Repeated reviewer fields are optional; the project operator is reused.
_status, _payload = api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/render-review",
                             {"action": "set_item", "key": "crop", "checked": True,
                              "reviewed_by": ""})
check(_status == 200, "review:project-operator-reused", f"HTTP {_status}")

for _key in CHECKLIST_KEYS:
    api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/render-review",
             {"action": "set_item", "key": _key, "checked": True,
              "reviewed_by": "verify_console", "note": f"kontrol: {_key}"})
_state = _rr_svc.render_review_state()
check(_state["complete"] and _state["verdict"] == "pass",
      "review:completes", f"{_state['done_count']}/{_state['total_count']} verdict={_state['verdict']}")

# Persisted: a fresh app object over the same storage sees the same checklist.
_rr_restart = ConsoleApp(ConsoleConfig(
    storage_root=_rr_app.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(), **fixtures.fixture_client_factories()))
_restart_state = _rr_restart.service(_rr_own).render_review_state()
check(_restart_state["done_count"] == 6, "review:survives-restart",
      f"restart sonrasi {_restart_state['done_count']}/6")
check((_rr_app.storage.namespace_root(_rr_own) / "results" / "render_review.json").is_file(),
      "review:persisted-artifact", "render_review.json diske yazildi")

_status, _payload = api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/approvals/render",
                             {"reviewed_sha256": _render_approval["content_sha256"],
                              "approved_by": "verify_console"})
check(_status == 200 and _payload["artifact"]["status"] == "approved",
      "review:approval-opens", f"HTTP {_status}")

# "Needs revision" blocks again and touches nothing.
_video = Path(_rr_svc.load_artifact("render_manifest")["data"]["output"]["final_video_path"])
_size_before = _video.stat().st_size
_digest_before = _rr_svc.render_video_digest()
_status, _payload = api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/render-review",
                             {"action": "request_revision", "requested_by": "verify_console",
                              "notes": "klip cumle ortasinda basliyor", "target_tab": "moments"})
_state = _rr_svc.render_review_state()
check(_state["verdict"] == "needs_revision" and _state["open_revision"]["target_tab"] == "moments",
      "review:revision-recorded", f"verdict={_state['verdict']}")
check(_video.stat().st_size == _size_before
      and _rr_svc.render_video_digest() == _digest_before,
      "review:video-untouched", "revizyon istegi videoyu DEGISTIRMEDI")
_status, _payload = api_call(_rr_app, "POST", f"/api/projects/{_rr_pid}/render-review",
                             {"action": "request_revision", "requested_by": "x", "notes": ""})
check(_status >= 400, "review:revision-notes-required", f"notsuz revizyon HTTP {_status}")

# A NEW render must reset the checklist -- nobody reviewed that video.
_manifest = json.loads(json.dumps(_rr_svc.load_artifact("render_manifest")["data"]))
_video.write_bytes(_video.read_bytes() + b"\x00")
_state = _rr_svc.render_review_state()
check(_state["done_count"] == 0 and _state["stale_count"] == 6,
      "review:resets-on-new-video",
      f"done={_state['done_count']} stale={_state['stale_count']}")
check("BASKA bir videoya" in (_state["blocker"] or ""), "review:stale-explained",
      f"blocker: {_state['blocker'][:60]!r}")
_after_change = _rr_svc.approval_state("render")
check(_after_change["valid"] is False, "review:approval-invalidated",
      "video degisince onay da gecersiz")
ok("render-review", "6 maddelik liste final onayi kapatiyor, kalici, video degisince sifirlaniyor, "
   "revizyon istegi videoyu degistirmiyor")

for _dir in TMP_DIRS:
    shutil.rmtree(_dir, ignore_errors=True)

print()
if FAILURES:
    print(f"CONSOLE_VERIFICATION_FAILED ({len(FAILURES)} sorun)")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("CREATOR_CONSOLE_VERIFICATION_OK (sahte saglayicilar, gercek yerel ffmpeg/whisper/soket; "
      "hicbir gercek API cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
sys.exit(0)
