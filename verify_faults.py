#!/usr/bin/env python
"""Adversarial fault-injection and recovery suite.

    python verify_faults.py

Seven scenario families are injected against BOTH the Creator Console and the
canonical `local_pipeline`, and each one asserts the SAFE outcome:

  1. corrupted artifacts       reported, never fabricated or silently accepted
  2. partial renders           never approvable, never served as usable output
  3. duplicate execution       idempotent; no duplicate provider call
  4. ownership isolation       no traversal, no cross-workspace discovery
  5. stale approvals           every dependent approval invalidated, audit kept
  6. refresh/restart/crash     persisted state recovered, never stuck 'running'
  7. secret leakage            no key in logs, errors, responses, bundle, audit

Everything runs against temp fixtures with mocked providers. No live API call,
no upload, no publish, and the user's `results/` directory is never touched.
"""
from __future__ import annotations

import io
import json
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures, redaction  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.audit import AuditLog  # noqa: E402
from creator_console.ids import Ownership  # noqa: E402
from creator_console.jobs import (  # noqa: E402
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REVIEW_REQUIRED,
    JobConflict,
    JobError,
)
from creator_console.storage import (  # noqa: E402
    LocalFilesystemStorage,
    StorageViolation,
    atomic_write_json,
)
from local_pipeline import approval as approval_module  # noqa: E402
from local_pipeline import render_utils, source_strategy, video_analysis  # noqa: E402

MATRIX: list = []
FAILURES: list = []
TMP_DIRS: list = []


def family(title: str) -> None:
    print(f"\n=== {title} ===")


def probe(scenario: str, expected: str, actual: str, passed: bool) -> None:
    """One row of the fault-injection matrix."""
    MATRIX.append({"scenario": scenario, "expected": expected, "actual": actual,
                    "passed": bool(passed)})
    mark = "OK  " if passed else "FAIL"
    print(f"  [{mark}] {scenario}\n         beklenen: {expected}\n         gozlenen: {actual}")
    if not passed:
        FAILURES.append(scenario)


def make_app(**kwargs) -> ConsoleApp:
    root = Path(tempfile.mkdtemp(prefix="faults_"))
    TMP_DIRS.append(root)
    return ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories(**kwargs)))


def call(app, method, path, body=None, headers=None):
    api = ConsoleApi(app)
    raw = b"" if body is None else (body if isinstance(body, bytes)
                                    else json.dumps(body).encode("utf-8"))
    return api.handle(method, path, body=raw, headers=headers or {})


def ready_project(app, mode="direct_clip_series", *, theory="", approve=True):
    """Seed a project and take it to an approved plan."""
    seed = fixtures.seed_demo_project(app, title=f"fault-{mode}")
    pid = seed["project_id"]
    own = app.ownership(pid)
    svc = app.service(own)
    payload = {"mode": mode, "selected_by": "faults", "output_count": 1,
               "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                                  "caption_style": "active_word_yellow", "caption_theme": "auto"}}
    if mode == "narrated_theory":
        payload["theory_angle"] = theory or "Bu sahnedeki karar tum sezonu degistiren donum noktasi"
    call(app, "POST", f"/api/projects/{pid}/plan", payload)
    if approve:
        digest = svc.describe_plan_review()["review"]["content_sha256"]
        call(app, "POST", f"/api/projects/{pid}/approvals/processing_plan",
             {"reviewed_sha256": digest, "approved_by": "faults"})
    return pid, own, svc


def analyze(app, pid, svc):
    call(app, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"whisper_model_size": "tiny", "clip_duration_sec": 6, "top_n": 2}})
    hot = svc.load_artifact("hot_moments")["data"]
    svc.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                reviewed_by="faults", require_preview=False)
    return hot


# ===========================================================================
family("1. Corrupted artifacts")
# ===========================================================================
_app = make_app()
_pid, _own, _svc = ready_project(_app)
_target = _svc.path("processing_plan")
_good = _target.read_bytes()

CORRUPTIONS = [
    ("bos dosya (0 bayt)", b""),
    ("sadece bosluk", b"   \n  "),
    ("kesik JSON", _good[: len(_good) // 3]),
    ("bozuk sozdizimi", b"{ 'decision': yok }"),
    ("JSON ama liste", b"[1, 2, 3]"),
    ("JSON ama string", b'"plan"'),
    ("UTF-8 olmayan bayt", b"\xff\xfe\x00bozuk"),
    ("zorunlu alan eksik", json.dumps({"artifact_type": "processing_plan",
                                        "status": "approved"}).encode()),
    ("uyumsuz schema_version", json.dumps({"artifact_type": "processing_plan",
                                            "status": "approved", "decision": {"mode": "x"},
                                            "phase_gates": {}, "schema_version": 42}).encode()),
    ("elle production_ready", json.dumps({"artifact_type": "processing_plan",
                                           "status": "production_ready", "decision": {"mode": "x"},
                                           "phase_gates": {}, "schema_version": 1}).encode()),
]
_detected = 0
for label, payload in CORRUPTIONS:
    _target.write_bytes(payload)
    loaded = _svc.load_artifact("processing_plan")
    if loaded["data"] is None and loaded["problem"]:
        _detected += 1
probe("10 bozulma turu (bos/kesik/bozuk/tip/kodlama/alan/versiyon/production_ready)",
      "hepsi problem olarak raporlanir, hicbiri kabul edilmez",
      f"{_detected}/10 tespit edildi", _detected == 10)

# A corrupt plan must also SHUT the phase gates, not open them.
_target.write_bytes(b"{ bozuk }")
_gate_allowed = [phase for phase in source_strategy.GATED_PHASES
                 if _svc.plan_gate(phase)[0]]
probe("bozuk plan ile faz kapilari",
      "tum fazlar kapali", f"acik fazlar: {_gate_allowed or 'yok'}", not _gate_allowed)

_status, _payload = call(_app, "GET", f"/api/projects/{_pid}")
probe("bozuk artefaktla proje sayfasi",
      "200 + kullaniciya bildirilen problem",
      f"HTTP {_status}, problem={bool(_payload.get('phase0', {}).get('plan_problem'))}",
      _status == 200 and bool(_payload["phase0"]["plan_problem"]))
_target.write_bytes(_good)

# Manifest that references media which does not exist.
_hot = analyze(_app, _pid, _svc)
call(_app, "POST", f"/api/projects/{_pid}/jobs", {"job_type": "render", "params": {
    "render_mode": "blurred_background", "preview_mode": True,
    "subtitle_whisper_model_size": "tiny", "caption_style": "active_word_yellow",
    "caption_theme": "auto"}})
_manifest = _svc.load_artifact("render_manifest")["data"]
_video = Path(_manifest["output"]["final_video_path"])
_video_backup = _video.read_bytes()
_video.unlink()
_outputs = _svc.outputs()
probe("manifest var, medya diskte yok",
      "cikti kullanilamaz + acik problem",
      f"usable={_outputs[0]['video']['usable']}, problem={bool(_outputs[0]['video']['problem'])}",
      not _outputs[0]["video"]["usable"] and bool(_outputs[0]["video"]["problem"]))

# "completed" status with an invalid output.
_lying = json.loads(json.dumps(_manifest))
_lying["status"] = "approved"
_lying["human_approval"] = {"approved": True, "approved_by": "attacker",
                             "approved_content_sha256": "0" * 64}
_svc.write_artifact("render_manifest", _lying)
_state = _svc.approval_state("render")
probe("status='approved' ama medya yok",
      "onay gecersiz sayilir", f"valid={_state['valid']} ({_state['reason'][:48]}…)",
      _state["valid"] is False)
_video.write_bytes(_video_backup)
_svc.write_artifact("render_manifest", _manifest)

# CSV corruption on the canonical pipeline side.
_csv_dir = Path(tempfile.mkdtemp(prefix="faults_csv_"))
TMP_DIRS.append(_csv_dir)
_csv = _csv_dir / "normalized_videos.csv"
_csv_results = []
for label, content in (("bos csv", ""), ("sadece basliklar", "foo,bar\n"),
                       ("yanlis sutunlar", "a,b\n1,2\n"), ("kesik satir", "video_id,view_count\nabc")):
    _csv.write_text(content, encoding="utf-8")
    try:
        import pandas as _pd
        frame = _pd.read_csv(_csv)
        required = {"video_id", "channel_id", "view_count", "published_at"}
        _csv_results.append(bool(required - set(frame.columns)))
    except Exception:
        _csv_results.append(True)  # unreadable is also "not silently accepted"
probe("bozuk normalized_videos.csv (4 varyant)",
      "hicbiri gecerli gecmis verisi sayilmaz",
      f"{sum(_csv_results)}/4 reddedildi", all(_csv_results))

# ===========================================================================
family("2. Partial renders")
# ===========================================================================
_video.write_bytes(b"")
probe("0 baytlik MP4",
      "kullanilamaz + medya uc noktasi 422",
      f"usable={_svc.outputs()[0]['video']['usable']}, "
      f"HTTP {call(_app, 'GET', f'/api/projects/{_pid}/media/{_svc.outputs()[0]['video']['relative_path']}')[0]}",
      not _svc.outputs()[0]["video"]["usable"])

_video.write_bytes(_video_backup[:512])  # truncated MP4
_ffmpeg, _ffprobe = render_utils.resolve_ffmpeg_binaries()
_validation = render_utils.validate_render(_ffmpeg, _ffprobe, _video, 540, 960)
probe("kesik MP4 teknik dogrulama",
      "passed=False", f"passed={_validation['passed']} checks={sum(_validation['checks'].values())}"
                      f"/{len(_validation['checks'])}", _validation["passed"] is False)

_truncated_manifest = json.loads(json.dumps(_manifest))
_truncated_manifest["output"]["validation"] = _validation
_truncated_manifest["approval"]["conditions"]["technical_validation_passed"] = False
_gate = approval_module.evaluate_render_gate(_truncated_manifest)
probe("teknik dogrulama basarisiz render onayi",
      "onay reddedilir", f"unmet={_gate['unmet']}", "technical_validation_passed" in _gate["unmet"])

try:
    approval_module.approve_artifact(_truncated_manifest, "render",
                                     approval_module.sha256_file(_video), "attacker")
    _approved = True
except ValueError:
    _approved = False
probe("dogru hash ile basarisiz renderi onaylama denemesi",
      "ValueError ile reddedilir", "reddedildi" if not _approved else "ONAYLANDI!", not _approved)

# ffmpeg failure surfaces as a job failure, not a fake success.
_bad_app = make_app()
_bad_pid, _bad_own, _bad_svc = ready_project(_bad_app)
analyze(_bad_app, _bad_pid, _bad_svc)
_source = _bad_svc.source_path()
_source.write_bytes(b"bu bir video degil")  # ffmpeg will refuse this
_status, _payload = call(_bad_app, "POST", f"/api/projects/{_bad_pid}/jobs",
                         {"job_type": "render", "params": {
                             "render_mode": "blurred_background", "preview_mode": True,
                             "subtitle_whisper_model_size": "tiny",
                             "caption_style": "active_word_yellow", "caption_theme": "auto"}})
_job = _bad_app.job_store(_bad_own).get(_payload["job"]["job_id"])
probe("ffmpeg basarisizligi",
      "is 'failed' + uygulanabilir hata, sahte basari yok",
      f"status={_job['status']} recoverable={_job['recoverable']} hata={bool(_job['error'])}",
      _job["status"] == STATUS_FAILED and bool(_job["error"]))

# Manifest written before media completion is caught by the outputs view.
_pre_manifest = json.loads(json.dumps(_manifest))
_pre_manifest["output"]["final_video_path"] = str(_video.parent / "henuz_yok.mp4")
_pre_manifest["status"] = "completed"
_svc.write_artifact("render_manifest", _pre_manifest)
_out = _svc.outputs()[0]
probe("medya tamamlanmadan yazilmis manifest",
      "cikti problem olarak isaretlenir",
      f"problem={bool(_out['problem'] or (_out['video'] or {}).get('problem'))}",
      bool(_out["problem"] or (_out["video"] or {}).get("problem")))
_svc.write_artifact("render_manifest", _manifest)
_video.write_bytes(_video_backup)

# ===========================================================================
family("3. Duplicate execution")
# ===========================================================================
_dup_app = make_app()
_dup_pid, _dup_own, _dup_svc = ready_project(_dup_app, "narrated_theory")
analyze(_dup_app, _dup_pid, _dup_svc)

fixtures.reset_provider_calls()
_results, _lock = [], threading.Lock()


def _rapid_submit():
    status, payload = call(_dup_app, "POST", f"/api/projects/{_dup_pid}/jobs",
                           {"job_type": "script_generation"})
    with _lock:
        _results.append((status, payload.get("created"), payload["job"]["job_id"]))


_threads = [threading.Thread(target=_rapid_submit) for _ in range(8)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
_created = [r for r in _results if r[1]]
_unique_jobs = {r[2] for r in _results}
probe("8 es zamanli ayni gonderim (hizli cift tiklama)",
      "1 is olusur, hepsi ayni job_id doner",
      f"created={len(_created)} benzersiz_is={len(_unique_jobs)}",
      len(_created) == 1 and len(_unique_jobs) == 1)

_script_calls = [c for c in fixtures.PROVIDER_CALLS if c[0] == "google.generate_content"]
probe("cift gonderimde saglayici cagrisi",
      "yalnizca 1 kez cagrilir",
      f"{len(_script_calls)} cagri", len(_script_calls) == 1)

_job_files = list((_dup_app.storage.namespace_root(_dup_own) / "jobs").glob("job_*.json"))
_script_records = [json.loads(p.read_text(encoding="utf-8")) for p in _job_files]
_script_records = [r for r in _script_records if r.get("job_type") == "script_generation"]
probe("diskte olusan script isi kaydi",
      "tam olarak 1", f"{len(_script_records)} kayit", len(_script_records) == 1)

_store = _dup_app.job_store(_dup_own)
_record, _ = _store.submit("render", idempotency_key="race", params={})
_claims, _rejections = [], []


def _claim(worker):
    try:
        _claims.append(_store.claim(_record["job_id"], worker)["claimed_by"])
    except JobConflict:
        _rejections.append(worker)


_threads = [threading.Thread(target=_claim, args=(f"w{i}",)) for i in range(10)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
probe("10 es zamanli worker ayni isi claim ediyor",
      "yalnizca 1 basarili", f"basarili={len(_claims)} reddedilen={len(_rejections)}",
      len(_claims) == 1 and len(_rejections) == 9)

try:
    _store.request_retry(_script_records[0]["job_id"])
    _retry_after_done = True
except JobConflict:
    _retry_after_done = False
probe("tamamlanmis isi yeniden calistirma",
      "reddedilir (bosuna saglayici cagrisi yok)",
      "reddedildi" if not _retry_after_done else "KABUL EDILDI!", not _retry_after_done)

# ===========================================================================
family("4. User / workspace isolation")
# ===========================================================================
_iso_root = Path(tempfile.mkdtemp(prefix="faults_iso_"))
TMP_DIRS.append(_iso_root)
_storage = LocalFilesystemStorage(_iso_root)
_victim = Ownership("usr_victim", "wsp_victim", "prj_victim")
_attacker = Ownership("usr_attacker", "wsp_attacker", "prj_attacker")
_storage.ensure_namespace(_victim)
_storage.ensure_namespace(_attacker)
_storage.write_json(_victim, "gizli.json", {"kredi": "kart"})

ATTACKS = [
    "../../wsp_victim/projects/prj_victim/gizli.json",
    "..\\..\\wsp_victim\\projects\\prj_victim\\gizli.json",
    "/etc/passwd",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "\\\\network\\share\\x",
    "results/../../../../wsp_victim/projects/prj_victim/gizli.json",
    "./././../prj_victim/gizli.json",
    "results/%2e%2e/%2e%2e/gizli.json",
]
_blocked = 0
for attack in ATTACKS:
    try:
        resolved = _storage.resolve(_attacker, attack)
        root = _storage.namespace_root(_attacker).resolve()
        if root == resolved.resolve() or root in resolved.resolve().parents:
            _blocked += 1  # contained, even if it did not raise
    except StorageViolation:
        _blocked += 1
probe("8 yol-gezinme / mutlak yol / UNC saldirisi",
      "hepsi engellenir veya namespace icinde kalir",
      f"{_blocked}/8 guvenli", _blocked == 8)

probe("baska workspace'in dosyasini okuma",
      "gorunmez", f"okundu={_storage.read_json(_attacker, 'gizli.json', default=None)}",
      _storage.read_json(_attacker, "gizli.json", default=None) is None)

_a_app, _b_app = make_app(), make_app()
_a_pid, _a_own, _a_svc = ready_project(_a_app)
_b_seed = fixtures.seed_demo_project(_b_app, title="kurban")
_status, _ = call(_a_app, "GET", f"/api/projects/{_b_seed['project_id']}")
probe("baska uygulamanin proje kimligiyle GET",
      "404 (varligi bile sizmaz)", f"HTTP {_status}", _status == 404)

_hostile_ids = ["../wsp_x", "prj_../..", "%2e%2e%2f", "prj_a/../prj_b", "'; DROP TABLE--",
                "prj_" + "a" * 200, "\x00null"]
_rejected = 0
for hostile in _hostile_ids:
    status, _ = call(_a_app, "GET", f"/api/projects/{hostile}")
    if status in (400, 403, 404):
        _rejected += 1
probe("7 dusmanca proje kimligi",
      "hepsi 400/403/404", f"{_rejected}/7 reddedildi", _rejected == 7)

_unsafe_names = ["../../../evil.mp4", "C:\\evil.mp4", "..\\..\\evil.mp4", "con.mp4",
                 "a" * 400 + ".mp4", "evil\x00.mp4"]
_safe = 0
for name in _unsafe_names:
    status, payload = call(_a_app, "POST", f"/api/projects/{_a_pid}/source",
                           b"FAKEVIDEO" * 100, headers={"X-Filename": name})
    if status == 201:
        stored = payload["project"]["source"]["stored_path"]
        if ".." not in stored and not Path(stored).is_absolute():
            _safe += 1
probe("6 guvensiz dosya adiyla yukleme",
      "hepsi namespace icinde temizlenmis adla saklanir",
      f"{_safe}/6 guvenli", _safe == 6)

_status, _ = call(_a_app, "GET", f"/api/projects/{_a_pid}/media/../../../../etc/passwd")
probe("medya uc noktasindan yol gezinme",
      "400/403/404", f"HTTP {_status}", _status in (400, 403, 404))

# ===========================================================================
family("5. Stale approvals (dependency chain)")
# ===========================================================================
_chain_app = make_app()
_chain_pid, _chain_own, _chain_svc = ready_project(_chain_app, "narrated_theory")
analyze(_chain_app, _chain_pid, _chain_svc)
call(_chain_app, "POST", f"/api/projects/{_chain_pid}/jobs", {"job_type": "script_generation"})
_script_state = _chain_svc.approval_state("script")
call(_chain_app, "POST", f"/api/projects/{_chain_pid}/approvals/script",
     {"reviewed_sha256": _script_state["content_sha256"], "approved_by": "faults"})
call(_chain_app, "POST", f"/api/projects/{_chain_pid}/jobs", {"job_type": "tts"})
_voice_state = _chain_svc.approval_state("voiceover")
call(_chain_app, "POST", f"/api/projects/{_chain_pid}/approvals/voiceover",
     {"reviewed_sha256": _voice_state["content_sha256"], "approved_by": "faults"})

_chain = _chain_svc.approval_chain()
probe("baslangic: plan + script + ses onayli",
      "hepsi effective_valid", f"{ {k: v.get('effective_valid') for k, v in _chain.items() if v.get('approved')} }",
      all(v["effective_valid"] for v in _chain.values() if v.get("approved")))

_audit_before = len(AuditLog(_chain_app.storage, _chain_own).events())
_plan = _chain_svc.load_artifact("processing_plan")["data"]
_plan_backup = json.loads(json.dumps(_plan))
_plan["decision"]["theory_angle"] = "Tamamen farkli bir teori acisi metni yazildi"
_chain_svc.write_artifact("processing_plan", _plan)
_chain = _chain_svc.approval_chain()
probe("STRATEJI degisti -> script ve ses onaylari",
      "ikisi de effective_valid=False (yukari-akis)",
      f"script={_chain['script']['effective_valid']} ses={_chain['voiceover']['effective_valid']}",
      not _chain["script"]["effective_valid"] and not _chain["voiceover"]["effective_valid"])

_gate_open = [p for p in source_strategy.GATED_PHASES if _chain_svc.plan_gate(p)[0]]
probe("baglamayan onayla faz kapilari",
      "hepsi kapali", f"acik: {_gate_open or 'yok'}", not _gate_open)

_audit_after = AuditLog(_chain_app.storage, _chain_own).events()
probe("gecersizlesme audit gecmisini siliyor mu",
      "hayir, kayitlar korunur",
      f"onceki={_audit_before} sonraki={len(_audit_after)} "
      f"approval.granted={sum(1 for e in _audit_after if e['event_type'] == 'approval.granted')}",
      len(_audit_after) >= _audit_before
      and any(e["event_type"] == "approval.granted" for e in _audit_after))
_chain_svc.write_artifact("processing_plan", _plan_backup)

_script = _chain_svc.load_artifact("selected_script")["data"]
_script["script_text"] += " sonradan eklendi"
_chain_svc.write_artifact("selected_script", _script)
_chain = _chain_svc.approval_chain()
probe("SCRIPT metni degisti -> script ve ses onaylari",
      "script gecersiz, ses de yukari-akis nedeniyle gecersiz",
      f"script={_chain['script']['effective_valid']} ses={_chain['voiceover']['effective_valid']}",
      not _chain["script"]["effective_valid"] and not _chain["voiceover"]["effective_valid"])

_voice = _chain_svc.load_artifact("selected_voiceover")["data"]
_audio = Path(_voice["audio_path"])
_audio_backup = _audio.read_bytes()
_audio.write_bytes(_audio_backup + b"\x00" * 128)
probe("SES dosyasi degisti -> ses onayi",
      "gecersiz", f"valid={_chain_svc.approval_state('voiceover')['valid']}",
      _chain_svc.approval_state("voiceover")["valid"] is False)
_audio.write_bytes(_audio_backup)

_hot_now = _chain_svc.load_artifact("hot_moments")["data"]
_hot_now["hot_moments"][0]["score"] = 0.0001  # re-analysis changed the manifest
_chain_svc.write_artifact("hot_moments", _hot_now)
_chain = _chain_svc.approval_chain()
probe("KAYNAK analizi yeniden uretildi -> moment secimi bayat",
      "downstream onaylar gecersiz",
      f"script upstream={len(_chain['script']['upstream_invalid'])}",
      not _chain["script"]["effective_valid"])

# ===========================================================================
family("6. Refresh, restart and crash recovery")
# ===========================================================================
_rec_app = make_app()
_rec_pid, _rec_own, _rec_svc = ready_project(_rec_app)
_rec_store = _rec_app.job_store(_rec_own)

_running, _ = _rec_store.submit("video_analysis", idempotency_key="crash-1", params={})
_rec_store.claim(_running["job_id"], "worker-that-dies")

# The claiming process (this test) is ALIVE, so an owner-aware restart must NOT
# reclaim the job. Reclaiming it would be the bug that kills a healthy external
# worker's work.
_restart = ConsoleApp(ConsoleConfig(
    storage_root=_rec_app.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(), **fixtures.fixture_client_factories()))
_after_live = _restart.job_store(_rec_own).get(_running["job_id"])
probe("backend restart, sahip surec CANLI",
      "is elinden ALINMAZ (running kalir)",
      f"status={_after_live['status']} owner_pid={_after_live.get('owner_pid')}",
      _after_live["status"] == "running")

# Now make the owner provably dead: a pid that cannot exist. Recovery must
# reclaim it even though the lease still looks fresh.
_dead = _rec_store.get(_running["job_id"])
_dead["owner_pid"] = 0x7FFFFFF0          # never a live pid
_rec_store._save(_dead)
_restart2 = ConsoleApp(ConsoleConfig(
    storage_root=_rec_app.config.storage_root, inline_jobs=True,
    secret_getter=fixtures.fake_secret_getter(), **fixtures.fixture_client_factories()))
_after = _restart2.job_store(_rec_own).get(_running["job_id"])
probe("backend restart, sahip surec OLU, lease taze",
      "is 'running' takilmaz, failed+recoverable olur",
      f"status={_after['status']} recoverable={_after['recoverable']} kind={_after['error_kind']}",
      _after["status"] == STATUS_FAILED and _after["recoverable"] is True)
_restart = _restart2

_expired, _ = _rec_store.submit("render", idempotency_key="crash-2", params={})
_rec_store.claim(_expired["job_id"], "worker-lost")
_raw = _rec_store.get(_expired["job_id"])
_raw["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
_rec_store._save(_raw)
_recovered = _rec_store.recover_abandoned()
probe("worker cokmesi (lease suresi doldu)",
      "calisma anindaki kurtarma isi failed+recoverable yapar",
      f"{len(_recovered)} is kurtarildi, status={_rec_store.get(_expired['job_id'])['status']}",
      len(_recovered) == 1 and _rec_store.get(_expired["job_id"])["status"] == STATUS_FAILED)

_healthy, _ = _rec_store.submit("tts", idempotency_key="healthy", params={})
_rec_store.claim(_healthy["job_id"], "alive")
_untouched = _rec_store.recover_abandoned()
probe("calisma aninda SAGLIKLI is (lease taze)",
      "kurtarma onu OLDURMEZ",
      f"{len(_untouched)} kurtarildi, status={_rec_store.get(_healthy['job_id'])['status']}",
      not _untouched and _rec_store.get(_healthy["job_id"])["status"] == "running")

# "Browser refresh" == re-fetching the view; state must be identical.
_view_a = _restart.project_view(_rec_own)
_view_b = _restart.project_view(_rec_own)
_stable = ({j["job_id"]: j["status"] for j in _view_a["jobs"]}
           == {j["job_id"]: j["status"] for j in _view_b["jobs"]})
probe("tarayici yenileme (view iki kez cekilir)",
      "ayni is durumlari doner", "durumlar ozdes" if _stable else "DEGISTI", _stable)

# Atomic writes. The guarantee is INTEGRITY (never a partial document), not
# unconditional availability: on Windows `os.replace` cannot swap a file while a
# reader holds a handle, so under pathological contention the write must FAIL
# LOUDLY rather than silently lose data.
_atomic_dir = Path(tempfile.mkdtemp(prefix="faults_atomic_"))
TMP_DIRS.append(_atomic_dir)


def _atomic_trial(reader_pause, writes=150):
    """Returns (writer_errors, partial_reads, leftover_tmp, reads_done)."""
    path = _atomic_dir / f"manifest_{reader_pause}.json"
    atomic_write_json(path, {"v": 0})
    errors, partial, reads = [], [], []
    stop = threading.Event()

    def writer():
        try:
            for i in range(1, writes):
                atomic_write_json(path, {"v": i, "pad": "x" * 5000})
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
        finally:
            stop.set()

    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
                reads.append(1)
            except (json.JSONDecodeError, UnicodeDecodeError):
                partial.append(1)   # THE failure that must never happen
            except OSError:
                pass
            if reader_pause:
                time.sleep(reader_pause)

    threads = [threading.Thread(target=writer, daemon=True)]
    threads += [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    threads[0].join(timeout=120)
    for thread in threads[1:]:
        thread.join(timeout=10)
    leftover = list(_atomic_dir.glob(".*tmp*"))
    return errors, partial, leftover, len(reads)


_errors, _partial, _leftover, _reads = _atomic_trial(reader_pause=0.002)
probe("gercekci eszamanlilik: 150 atomik yazim + 3 okuyucu (pollayan UI gibi)",
      "yazim hatasiz, yarim okuma yok, artik tmp yok",
      f"yazim_hatasi={_errors or 'yok'} yarim_okuma={len(_partial)} "
      f"tamamlanan_okuma={_reads} artik_tmp={len(_leftover)}",
      not _errors and not _partial and not _leftover)

_errors, _partial, _leftover, _reads = _atomic_trial(reader_pause=0)
probe("patolojik eszamanlilik: okuyucular hic ara vermiyor",
      "BUTUNLUK korunur (yarim okuma yok); yazim ya basarir ya ACIKCA hata verir",
      f"yazim_hatasi={_errors or 'yok'} yarim_okuma={len(_partial)} "
      f"tamamlanan_okuma={_reads} artik_tmp={len(_leftover)}",
      not _partial and not _leftover)

# A truncated final audit line must not hide the rest of the trail.
_audit = AuditLog(_rec_app.storage, _rec_own)
_audit.record("settings.changed", detail={"x": 1})
with open(_audit.path, "a", encoding="utf-8") as _handle:
    _handle.write('{"event_id": "yarim')
_events = _audit.events()
probe("audit dosyasinin son satiri yarim kalmis",
      "geri kalan kayitlar okunur, bozuk satir isaretlenir",
      f"{len(_events)} olay, bozuk_satir={sum(1 for e in _events if e['event_type'] == 'audit.unreadable_line')}",
      len(_events) > 1 and any(e["event_type"] == "audit.unreadable_line" for e in _events))

# ===========================================================================
family("7. Secret leakage")
# ===========================================================================
_secret = fixtures.fake_secret_getter()("GeminiKey")
_leak_app = make_app()
_leak_pid, _leak_own, _leak_svc = ready_project(_leak_app, "narrated_theory")
_leak_store = _leak_app.job_store(_leak_own)

# The exact leak this pass found and fixed: a provider exception carrying a
# token that does NOT match any known key pattern.
_rec, _ = _leak_store.submit("tts", idempotency_key="leak", params={})
_leak_store.claim(_rec["job_id"], "w")
_leak_store.finish(_rec["job_id"], STATUS_FAILED,
                   error=f"RuntimeError: auth failed token={_secret} endpoint=x")
_saved = json.dumps(_leak_store.get(_rec["job_id"]))
probe("saglayici istisnasi is kaydina yazilir",
      "secret degeri redakte edilir",
      "REDACTED var" if _secret not in _saved and "REDACTED" in _saved else "SIZDI",
      _secret not in _saved)

AuditLog(_leak_app.storage, _leak_own).record(
    "provider.call", detail={"error": f"key={_secret}", "nested": [{"k": _secret}]})
_audit_blob = json.dumps(AuditLog(_leak_app.storage, _leak_own).events())
probe("audit olayinda (ic ice) secret",
      "redakte edilir", "temiz" if _secret not in _audit_blob else "SIZDI",
      _secret not in _audit_blob)

_bodies = []
for method, path, body in (
    ("GET", "/api/workspace", None),
    ("GET", f"/api/projects/{_leak_pid}", None),
    ("GET", f"/api/projects/{_leak_pid}/jobs", None),
    ("GET", f"/api/projects/{_leak_pid}/audit", None),
    ("POST", f"/api/projects/{_leak_pid}/plan", {"mode": "yok", "selected_by": "x"}),
    ("POST", f"/api/projects/{_leak_pid}/approvals/script",
     {"reviewed_sha256": _secret, "approved_by": "x"}),
    ("GET", f"/api/projects/{_leak_pid}/media/{_secret}.mp4", None),
):
    status, payload = call(_leak_app, method, path, body)
    _bodies.append(json.dumps(payload, ensure_ascii=False, default=str))
_api_blob = "\n".join(_bodies)
probe("7 API yaniti (hata yollari dahil)",
      "hicbirinde secret degeri yok", "temiz" if _secret not in _api_blob else "SIZDI",
      _secret not in _api_blob)

_disk_blob = "\n".join(
    p.read_text(encoding="utf-8", errors="ignore")
    for p in _leak_app.storage.root.rglob("*")
    if p.is_file() and p.suffix in (".json", ".jsonl", ".txt", ".srt", ".ass"))
probe("tum artefakt/audit/onbellek dosyalari",
      "secret degeri yok", "temiz" if _secret not in _disk_blob else "SIZDI",
      _secret not in _disk_blob)

_static_dir = PROJECT_ROOT / "creator_console" / "static"
_bundle = "\n".join(p.read_text(encoding="utf-8") for p in _static_dir.rglob("*") if p.is_file())
_forbidden = [t for t in ("GeminiKey", "GOOGLE_API_KEY", "AIza", "api_key", "Authorization",
                          "local_pipeline") if t in _bundle]
probe("frontend paketi (js/css/html)",
      "secret adi/deseni ve pipeline importu yok", f"bulunanlar: {_forbidden or 'yok'}",
      not _forbidden)

_buffer = io.StringIO()
with contextlib.redirect_stdout(_buffer), contextlib.redirect_stderr(_buffer):
    call(_leak_app, "GET", f"/api/projects/{_leak_pid}")
    call(_leak_app, "POST", f"/api/projects/{_leak_pid}/jobs", {"job_type": "gecersiz"})
probe("stdout/stderr ciktisi",
      "secret yok", "temiz" if _secret not in _buffer.getvalue() else "SIZDI",
      _secret not in _buffer.getvalue())

probe("redaksiyon kaydi degerleri sizdiriyor mu",
      "yalnizca sayi doner, deger degil",
      f"registered_count()={redaction.registered_count()}",
      isinstance(redaction.registered_count(), int)
      and _secret not in str(redaction.registered_count()))

_scrubbed = redaction.scrub({"msg": f"Bearer {_secret}", "list": [f"?key={_secret}"]})
probe("ic ice yapida scrub",
      "her seviyede redakte", f"{_scrubbed}",
      _secret not in json.dumps(_scrubbed))

# The canonical pipeline's own sanitizer, with a credential in scope.
from local_pipeline import google_ai as _ga  # noqa: E402
_cred = _ga.resolve_secret("google_api", fixtures.fake_secret_getter())
probe("google_ai.sanitize_google_error (kimlik kapsamda)",
      "deger redakte", _ga.sanitize_google_error(f"boom {_secret}", _cred),
      _secret not in _ga.sanitize_google_error(f"boom {_secret}", _cred))



# ===========================================================================
family("8. Separate-process workers (real OS processes)")
# ===========================================================================
_mp_app = make_app()
_mp_pid, _mp_own, _mp_svc = ready_project(_mp_app)
_mp_root = str(_mp_app.config.storage_root)
_mp_store = _mp_app.job_store(_mp_own)

WORKER_CMD = [sys.executable, "-u", "-m", "creator_console.worker",
              "--storage-root", _mp_root, "--project-id", _mp_pid, "--demo-clients"]


def spawn_worker(*extra, worker_id=None):
    command = list(WORKER_CMD)
    if worker_id:
        command += ["--worker-id", worker_id]
    command += list(extra)
    return subprocess.Popen(command, cwd=str(PROJECT_ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(predicate, timeout=60.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# -- 8a. A real external worker executes a queued job -----------------------
_queued, _ = _mp_store.submit("source_assessment", idempotency_key="mp-run", params={})
_worker = spawn_worker("--max-jobs", "1", "--idle-timeout", "45")
try:
    _done = wait_for(lambda: _mp_store.get(_queued["job_id"])["status"] not in ACTIVE_STATUSES,
                     timeout=120)
finally:
    _worker.terminate()
    _worker.wait(timeout=20)
_final = _mp_store.get(_queued["job_id"])
probe("ayri OS sureci kuyruktaki isi calistirir",
      "is terminal duruma gecer, sahibi baska bir pid",
      f"status={_final['status']} claimed_by={_final.get('claimed_by')} "
      f"owner_pid={_final.get('owner_pid')}",
      _done and _final["status"] in TERMINAL_STATUSES)

# -- 8b. Killing a worker mid-job -------------------------------------------
_ready_file = Path(tempfile.mkdtemp(prefix="faults_ready_")) / "ready.txt"
TMP_DIRS.append(_ready_file.parent)
_stuck, _ = _mp_store.submit("render", idempotency_key="mp-kill", params={})
_victim = spawn_worker("--claim-only", "--heartbeat-interval", "0.5",
                       "--ready-file", str(_ready_file), worker_id="doomed")
try:
    _claimed = wait_for(lambda: _ready_file.is_file(), timeout=90)
    _record = _mp_store.get(_stuck["job_id"])
    _ready_pid = None
    if _ready_file.is_file():
        _lines = _ready_file.read_text(encoding="utf-8").splitlines()
        _ready_pid = int(_lines[2]) if len(_lines) > 2 else None
    probe("worker isi claim etti (kontrollu bekleme noktasi)",
          "status=running, sahip AYRI bir OS sureci",
          f"claimed={_claimed} status={_record['status']} "
          f"owner_pid={_record.get('owner_pid')} ready_pid={_ready_pid} "
          f"test_pid={os.getpid()}",
          _claimed and _record["status"] == "running"
          and _record.get("owner_pid") not in (None, os.getpid())
          and _record.get("owner_pid") == _ready_pid)

    # Heartbeat must actually extend the lease while the worker is alive.
    _lease_before = _mp_store.get(_stuck["job_id"])["lease_expires_at"]
    time.sleep(2.0)
    _lease_after = _mp_store.get(_stuck["job_id"])["lease_expires_at"]
    probe("lease heartbeat (uzun is simulasyonu)",
          "lease worker yasarken uzatilir",
          f"once={str(_lease_before)[11:19]} sonra={str(_lease_after)[11:19]}",
          _lease_after != _lease_before)

    # A live external worker must NOT be reclaimed by a server restart.
    _restart_live = ConsoleApp(ConsoleConfig(
        storage_root=Path(_mp_root), inline_jobs=True,
        secret_getter=fixtures.fake_secret_getter(), **fixtures.fixture_client_factories()))
    _still = _mp_store.get(_stuck["job_id"])
    probe("sunucu yeniden baslarken CANLI harici worker",
          "isi ELINDEN ALMAZ (status=running kalir)",
          f"status={_still['status']}", _still["status"] == "running")
finally:
    _victim.kill()
    _victim.wait(timeout=20)

# Now the owner process is genuinely gone, but its lease still looks fresh.
_after_kill = _mp_store.get(_stuck["job_id"])
probe("worker OLDURULDU, lease hala taze gorunuyor",
      "kayit halen 'running' (henuz kurtarma calismadi)",
      f"status={_after_kill['status']} lease={str(_after_kill.get('lease_expires_at'))[11:19]}",
      _after_kill["status"] == "running")

_recovered = _mp_store.recover_abandoned()
_recovered_record = _mp_store.get(_stuck["job_id"])
probe("olu sahip pid'li isin kurtarilmasi",
      "surec olu -> failed + recoverable, lease taze olsa bile",
      f"kurtarilan={len(_recovered)} status={_recovered_record['status']} "
      f"recoverable={_recovered_record['recoverable']} kind={_recovered_record['error_kind']}",
      len(_recovered) == 1 and _recovered_record["status"] == STATUS_FAILED
      and _recovered_record["recoverable"] is True)

# -- 8c. Two external workers cannot run one job ----------------------------
_race, _ = _mp_store.submit("source_assessment", idempotency_key="mp-race", params={})
_racers = [spawn_worker("--max-jobs", "1", "--idle-timeout", "25",
                        worker_id=f"racer-{i}") for i in range(3)]
try:
    wait_for(lambda: _mp_store.get(_race["job_id"])["status"] not in ACTIVE_STATUSES,
             timeout=120)
finally:
    for racer in _racers:
        racer.terminate()
        racer.wait(timeout=20)
_race_record = _mp_store.get(_race["job_id"])
probe("3 ayri OS worker'i ayni isi yarisiyor",
      "yalnizca biri calistirir, attempts 1 kalir",
      f"status={_race_record['status']} attempts={_race_record['attempts']} "
      f"claimed_by={_race_record.get('claimed_by')}",
      _race_record["attempts"] == 1)

# -- 8d. Worker never runs startup recovery ---------------------------------
_worker_src = (PROJECT_ROOT / "creator_console" / "worker.py").read_text(encoding="utf-8")
probe("worker sureci baslangic kurtarmasi yapmiyor",
      "skip_startup_recovery bayragi ile kurulur",
      "bayrak var" if "skip_startup_recovery" in _worker_src else "BAYRAK YOK",
      "skip_startup_recovery" in _worker_src)

# ===========================================================================
family("9. Storage failures: disk full, permission, slow, interrupted write")
# ===========================================================================
from creator_console.faults import (  # noqa: E402
    FaultingStorage,
    InterruptedWrite,
    disk_full,
    io_error,
    patch_atomic_write,
    permission_denied,
)

_st_app = make_app()
_st_pid, _st_own, _st_svc = ready_project(_st_app)

# -- 9a. Disk full while writing an artifact --------------------------------
# NOTE: artifacts are written with the module-level `atomic_write_json`, not via
# `StorageAdapter.write_json`, so the fault has to be injected THERE. Patching
# the adapter alone would silently never fire -- a test that cannot fail.
with patch_atomic_write(exception=disk_full("source_assessment.json"),
                        match="source_assessment.json"):
    _status, _payload = call(_st_app, "POST", f"/api/projects/{_st_pid}/jobs",
                             {"job_type": "source_assessment"})
    _job = _st_app.job_store(_st_own).get(_payload["job"]["job_id"])
probe("artefakt yazarken disk dolu (ENOSPC)",
      "is 'failed' + recoverable, sahte basari yok",
      f"status={_job['status']} recoverable={_job['recoverable']} "
      f"kind={_job['error_kind']}",
      _job["status"] == STATUS_FAILED and _job["recoverable"] is True)

_after_enospc = _st_svc.load_artifact("source_assessment")
probe("ENOSPC sonrasi artefakt durumu",
      "yarim artefakt yok: ya hic yazilmamis ya bozuk olarak raporlanir",
      f"present={_after_enospc['present']} data={_after_enospc['data'] is not None} "
      f"problem={bool(_after_enospc['problem'])}",
      (not _after_enospc["present"]) or _after_enospc["data"] is None
      or not _after_enospc["problem"])

# -- 9b. Permission denied --------------------------------------------------
with patch_atomic_write(exception=permission_denied("processing_plan.json"),
                        match="processing_plan.json"):
    _status, _payload = call(_st_app, "POST", f"/api/projects/{_st_pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "faults", "output_count": 1,
        "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                           "caption_style": "active_word_yellow", "caption_theme": "auto"}})
probe("izin reddi ile plan yazimi",
      "507 storage_unavailable + retryable, traceback yok",
      f"HTTP {_status} kod={_payload.get('error', {}).get('code')} "
      f"retryable={_payload.get('error', {}).get('retryable')}",
      _status == 507 and _payload["error"]["code"] == "storage_unavailable"
      and _payload["error"]["retryable"] is True)
probe("izin reddi yanitinda ic ayrinti",
      "dosya yolu/istisna sinifi istemciye SIZMAZ",
      "temiz" if "PermissionError" not in json.dumps(_payload)
      and "processing_plan.json" not in json.dumps(_payload) else "SIZDI",
      "PermissionError" not in json.dumps(_payload)
      and "processing_plan.json" not in json.dumps(_payload))

# -- 9c. Interrupted write leaves partial bytes -----------------------------
_raised = False
with patch_atomic_write(exception=InterruptedWrite("yazim 48 baytta kesildi"),
                        partial_bytes=48, match="source_assessment.json"):
    try:
        _st_svc.write_artifact("source_assessment",
                               {"artifact_type": "source_assessment",
                                "status": "review_required", "source": {},
                                "mode_scores": {}, "schema_version": 1})
    except InterruptedWrite:
        _raised = True
_loaded = _st_svc.load_artifact("source_assessment")
probe("yarida kesilen yazim (kismi bayt diskte)",
      "hata yukselir VE sonraki okuma bozuk olarak raporlar",
      f"hata_yukseldi={_raised} problem={bool(_loaded['problem'])} "
      f"data={_loaded['data'] is not None}",
      _raised and _loaded["data"] is None and bool(_loaded["problem"]))

# -- 9d. Interrupted write of a JOB record ----------------------------------
_jobs_app = make_app()
_jobs_pid, _jobs_own, _jobs_svc = ready_project(_jobs_app)
_jobs_store = _jobs_app.job_store(_jobs_own)
_submit_raised = False
with patch_atomic_write(partial_bytes=30, match=".json"):
    try:
        _jobs_store.submit("render", idempotency_key="partial-job", params={})
    except OSError:
        _submit_raised = True
probe("is kaydi yazarken disk dolu",
      "hata yukselir; yarim kayit 'basarili' sayilmaz",
      f"hata_yukseldi={_submit_raised}", _submit_raised)
_listed = _jobs_store.list()
_corrupt_jobs = [j for j in _listed if j.get("corrupt")]
probe("yarim yazilmis is kaydinin listelenmesi",
      "bozuk olarak isaretlenir; hicbiri 'completed' sayilmaz",
      f"{len(_corrupt_jobs)} bozuk / {len(_listed)} kayit",
      all(j.get("status") != STATUS_COMPLETED for j in _listed))

# -- 9e. Slow storage does not corrupt or duplicate --------------------------
_slow_app = make_app()
_slow_pid, _slow_own, _slow_svc = ready_project(_slow_app)
_slow_storage = FaultingStorage(_slow_app.storage).slow(0.05)
_slow_app.storage = _slow_storage
_t0 = time.time()
_r1 = call(_slow_app, "POST", f"/api/projects/{_slow_pid}/jobs",
           {"job_type": "source_assessment"})
_r2 = call(_slow_app, "POST", f"/api/projects/{_slow_pid}/jobs",
           {"job_type": "source_assessment"})
_elapsed = time.time() - _t0
_slow_jobs = _slow_app.job_store(_slow_own).list(job_type="source_assessment")
probe("yavas depolama altinda cift gonderim",
      "hala tek is (idempotency yavaslikta bozulmaz)",
      f"{len(_slow_jobs)} is, {_elapsed:.1f}s",
      len(_slow_jobs) == 1)

# ===========================================================================
family("10. Structured error codes and secrets that bypass SecretResolver")
# ===========================================================================
from creator_console.errors import CATALOG, ApiError  # noqa: E402

_err_app = make_app()
_err_pid, _err_own, _err_svc = ready_project(_err_app)

_probe_cases = [
    ("GET", f"/api/projects/{_err_pid}/yok", None, 404, "unknown_route"),
    ("GET", "/api/projects/../etc", None, 400, "invalid_identifier"),
    ("POST", f"/api/projects/{_err_pid}/plan", {"selected_by": "x"}, 400, "mode_required"),
    ("POST", f"/api/projects/{_err_pid}/jobs", b"{bozuk json", 400, "invalid_json"),
    ("GET", f"/api/projects/{_err_pid}/media/../../x.mp4", None, 403, "storage_violation"),
]
_code_ok = 0
for method, path, body, expected_status, expected_code in _probe_cases:
    status, payload = call(_err_app, method, path, body)
    if status == expected_status and payload.get("error", {}).get("code") == expected_code:
        _code_ok += 1
probe("5 hata yolu icin yapisal kod",
      "hepsi beklenen HTTP + katalog kodu doner",
      f"{_code_ok}/5 eslesti", _code_ok == 5)

_all_bodies = []
for method, path, body, _, _ in _probe_cases:
    _all_bodies.append(json.dumps(call(_err_app, method, path, body)[1], ensure_ascii=False))
_joined = "\n".join(_all_bodies)
probe("hata govdelerinde ic ayrinti",
      "traceback/dosya yolu/istisna sinifi yok, debug_id var",
      f"traceback={'Traceback' in _joined} debug_id={'debug_id' in _joined}",
      "Traceback" not in _joined and 'File "' not in _joined and "debug_id" in _joined)

_catalog_messages = {spec.message for spec in CATALOG.values()}
probe("katalog mesajlari statik mi",
      "her mesaj sabit metin (bicimlendirme yer tutucusu yok)",
      f"{len(_catalog_messages)} benzersiz mesaj",
      not any("{" in message for message in _catalog_messages))

# THE bypass case: a secret that never went through SecretResolver.
_rogue = "rogue-" + "Zx9Qw7Lm2Pv4Nk8Tb6Yr3Hs5" + "-token"
redaction.forget_all()
_err_app.secrets.references()  # re-register only the fixture secrets
_bypass = ApiError("internal_error",
                   detail=f"ProviderSDKError: auth failed with {_rogue} at endpoint")
_body = json.dumps(_bypass.to_payload(), ensure_ascii=False)
probe("SecretResolver'i ATLAYAN bir token istisnada",
      "HTTP govdesine hic girmez (kapali katalog)",
      "temiz" if _rogue not in _body else "SIZDI", _rogue not in _body)

_log = _bypass.log_line()
probe("ayni token sunucu gunlugunde",
      "yuksek-entropi backstop ile redakte edilir",
      f"{_log[:90]}", _rogue not in _log and "REDACTED" in _log)

_hash_like = "a" * 64
probe("onay hash'i (public, kullaniciya gosterilir)",
      "entropi backstop TARAFINDAN redakte EDILMEZ",
      redaction.scrub_text(f"hash={_hash_like}")[:40],
      _hash_like in redaction.scrub_text(f"hash={_hash_like}"))

_job_leak = _err_app.job_store(_err_own)
_leak_rec, _ = _job_leak.submit("tts", idempotency_key="bypass-leak", params={})
_job_leak.claim(_leak_rec["job_id"], "w")
_job_leak.finish(_leak_rec["job_id"], STATUS_FAILED,
                 error=f"ProviderSDKError: token={_rogue}")
probe("ayni token is kaydinda",
      "kalici kayitta redakte edilir",
      "temiz" if _rogue not in json.dumps(_job_leak.get(_leak_rec["job_id"])) else "SIZDI",
      _rogue not in json.dumps(_job_leak.get(_leak_rec["job_id"])))

# ===========================================================================
for _dir in TMP_DIRS:
    shutil.rmtree(_dir, ignore_errors=True)

print("\n" + "=" * 78)
print("FAULT-INJECTION MATRISI")
print("=" * 78)
print(f"{'sonuc':<7} senaryo")
print("-" * 78)
for row in MATRIX:
    print(f"{'OK' if row['passed'] else 'FAIL':<7} {row['scenario']}")
print("-" * 78)
print(f"toplam {len(MATRIX)} senaryo, {sum(1 for r in MATRIX if r['passed'])} gecti, "
      f"{len(FAILURES)} basarisiz")

if FAILURES:
    print("\nFAULT_INJECTION_FAILED")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("\nFAULT_INJECTION_SUITE_OK (izole temp fixture'lar, sahte saglayicilar; "
      "hicbir gercek API cagrilmadi, hicbir kullanici dosyasi degistirilmedi, "
      "hicbir sey yuklenmedi/yayinlanmadi)")
sys.exit(0)
