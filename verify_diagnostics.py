#!/usr/bin/env python
"""Tests for the capability diagnostics system.

    python verify_diagnostics.py

Two halves, and the second is the important one:

POSITIVE  -- on a healthy tree every capability reports a real, correct result,
             the report persists, and the API/UI surfaces it.

FAILURE INJECTION -- each capability is BROKEN on purpose and the check must
             notice. A diagnostics suite that only ever passes is worthless:
             it would report `ready` for a pipeline that no longer works. Every
             injection below is a positive control proving the corresponding
             check can actually fail.

Mocked providers, isolated temp roots, no live call, no upload, no publish.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import diagnostics  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.fixtures import fixture_client_factories  # noqa: E402

FAILURES: list = []
PASSED = 0


def check(condition, label: str, message: str = "") -> bool:
    global PASSED
    if condition:
        PASSED += 1
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ — {message}")
    return False


@contextmanager
def console(secrets=None):
    root = Path(tempfile.mkdtemp(prefix="verify_diag_"))
    app = ConsoleApp(ConsoleConfig(
        storage_root=root, user_id="usr_t", workspace_id="wsp_t", inline_jobs=True,
        secret_getter=lambda name: (secrets or {}).get(name),
        **fixture_client_factories()))
    try:
        yield app, root
    finally:
        app.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def run(app, only=None):
    return diagnostics.run_diagnostics(app, only=only)


def by_key(report):
    return {item["key"]: item for item in report["capabilities"]}


def status_of(app, key):
    return by_key(run(app, only=[key]))[key]


# ---------------------------------------------------------------------------
# 1. Registry shape
# ---------------------------------------------------------------------------
def section_registry_tabs():
    print("\n[1b] Her yetenek gercek bir sekmeye baglaniyor")
    source = (PROJECT_ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    block = source.split("const SCREENS = [")[1].split("];")[0]
    screens = {line.split("'")[1] for line in block.splitlines() if "key:" in line}
    bad = [item["tab"] for item in diagnostics.CAPABILITIES if item["tab"] not in screens]
    check(not bad, "tabs-resolve", f"bilinmeyen sekme: {bad}")


# ---------------------------------------------------------------------------
# 2. Positive: healthy tree
# ---------------------------------------------------------------------------
def section_positive():
    print("\n[2] Saglam agacta tam kosu")
    with console({"GeminiKey": "x" * 39}) as (app, root):
        report = run(app)
        results = by_key(report)

        check(set(results) == set(diagnostics.CAPABILITY_KEYS), "all-reported",
              "her yetenek raporlanmadi")
        for key, item in results.items():
            check(item["status"] in diagnostics.STATUSES, f"status-valid-{key}",
                  f"gecersiz durum {item['status']!r}")
            check(bool(item["summary"]), f"summary-present-{key}", "kisa sonuc bos")
            if item["status"] != diagnostics.READY:
                check(bool(item["blocker"]) and bool(item["repair"]),
                      f"blocker-repair-{key}",
                      "ready olmayan yetenek engel VE cozum bildirmeli")

        working = {"source_upload", "assessment", "plan", "video_analysis",
                   "moment_preview", "render", "review", "automation",
                   "persistence", "media_serving"}
        broken = {k: results[k]["status"] for k in working
                  if results[k]["status"] != diagnostics.READY}
        check(not broken, "healthy-tree-ready", f"saglam agacta ready degil: {broken}")

        # Availability and functionality must stay separate claims.
        for key in ("script", "tts"):
            item = results[key]
            check(item["status"] == diagnostics.DEGRADED, f"{key}-degraded-not-ready",
                  f"canli test yokken {item['status']} bildirildi")
            check(item["detail"].get("functionality") == "mocked",
                  f"{key}-functionality-flagged", str(item["detail"]))
            check("CANLI" in item["summary"], f"{key}-summary-honest", item["summary"])
        check(report["live_provider_tests"] is False, "live-off-by-default",
              "canli saglayici testleri varsayilan olarak kapali olmali")

        check(report["overall"] == diagnostics.DEGRADED, "overall-rollup",
              f"beklenen degraded, gelen {report['overall']}")
        check(report["counts"][diagnostics.FAILED] == 0, "no-failures",
              str(report["counts"]))
        check(report["not_uploaded"] is True, "not-uploaded", "")


def section_isolation():
    print("\n[3] Izolasyon: kullanicinin verisi okunmaz/yazilmaz")
    with console() as (app, root):
        record = app.create_project(title="kullanici projesi", created_by="user")
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        report = run(app)
        after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        added = set(after) - set(before)
        # Two legitimate writes, both at WORKSPACE level and never inside a
        # project: the report itself, and the live progress record that lets the
        # Settings panel show which capability is being tested.
        allowed = {f"workspaces/wsp_t/{diagnostics.DIAGNOSTICS_ARTIFACT}",
                   "workspaces/wsp_t/progress/diagnostics.json"}
        check(added == allowed, "only-report-written",
              f"beklenmeyen yazim: {sorted(added - allowed)}")
        check(not any("/projects/" in path for path in added),
              "nothing-written-into-a-project", f"{sorted(added)}")
        check(set(before) - set(after) == set(), "nothing-deleted", "dosya silindi")

        # The temp fixture root must be gone, not merely emptied.
        leftovers = list(Path(tempfile.gettempdir()).glob("console_diag_*"))
        check(not leftovers, "fixture-cleaned", f"{len(leftovers)} gecici kok kaldi")

        blob = json.dumps(report, ensure_ascii=False)
        check(record["project_id"] not in blob, "no-user-project-id",
              "kullanicinin proje kimligi rapora sizdi")
        check(tempfile.gettempdir().replace("\\", "/") not in blob.replace("\\", "/"),
              "no-temp-path", "gecici host yolu rapora sizdi")


def section_no_live_calls():
    print("\n[4] Hicbir canli saglayici cagrilmiyor")
    calls = []

    class Exploding:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"CANLI CAGRI: {name}")

    root = Path(tempfile.mkdtemp(prefix="verify_diag_live_"))
    app = ConsoleApp(ConsoleConfig(
        storage_root=root, user_id="usr_t", workspace_id="wsp_t", inline_jobs=True,
        secret_getter=lambda n: "x" * 39,
        google_client_factory=lambda: Exploding(),
        openai_client_factory=lambda: Exploding(),
        anthropic_client_factory=lambda: Exploding(),
        elevenlabs_client_factory=lambda: Exploding()))
    try:
        report = run(app, only=["script", "tts"])
        results = by_key(report)
        # The real client factory would explode; the fixture's must be used
        # instead, so the console path is still exercised.
        check(not calls, "no-provider-attribute-touched", f"dokunulan: {calls}")
        check(results["script"]["status"] in (diagnostics.DEGRADED, diagnostics.FAILED),
              "script-reported", results["script"]["status"])
    finally:
        app.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def section_credentials():
    print("\n[5] Kimlik bilgisi durumu vs islevsellik")
    with console({}) as (app, _):
        results = by_key(run(app, only=["script", "tts"]))
        for key in ("script", "tts"):
            item = results[key]
            check(item["status"] == diagnostics.NOT_CONFIGURED, f"{key}-not-configured",
                  item["status"])
            check(item["detail"].get("availability") == "missing",
                  f"{key}-availability-missing", str(item["detail"]))
            check(bool(item["repair"]), f"{key}-repair-present", "onarim adimi yok")

    secret = "AIzaSy" + "S" * 33
    with console({"GeminiKey": secret}) as (app, _):
        report = run(app, only=["script", "tts", "preflight"])
        blob = json.dumps(report, ensure_ascii=False)
        check(secret not in blob, "no-secret-value", "SECRET DEGERI rapora sizdi")
        check("GeminiKey" in blob, "alias-shown", "takma ad gosterilmiyor (isim sir degil)")


# ---------------------------------------------------------------------------
# 6. Failure injection -- the positive controls
# ---------------------------------------------------------------------------
def _patch(module, name, value):
    original = getattr(module, name)
    setattr(module, name, value)
    return lambda: setattr(module, name, original)


def section_injection_ffmpeg():
    print("\n[6a] Enjeksiyon: FFmpeg cozumlenemiyor")
    from local_pipeline import render_utils

    def explode(*_a, **_kw):
        raise RuntimeError("FFmpeg bulunamadi (enjekte edildi)")

    undo = _patch(render_utils, "resolve_ffmpeg_binaries", explode)
    try:
        with console() as (app, _):
            report = run(app, only=["preflight", "assessment", "render"])
            results = by_key(report)
            check(results["preflight"]["status"] == diagnostics.FAILED,
                  "ffmpeg-preflight-fails", results["preflight"]["status"])
            check(results["render"]["status"] in (diagnostics.FAILED, diagnostics.BLOCKED),
                  "ffmpeg-render-not-ready", results["render"]["status"])
            check(bool(results["preflight"]["repair"]), "ffmpeg-repair-offered", "")
            check(report["overall"] == diagnostics.FAILED, "ffmpeg-overall-failed",
                  report["overall"])
    finally:
        undo()


def section_injection_approval_gate():
    print("\n[6b] Enjeksiyon: plan onayi icerige baglanmiyor")
    from local_pipeline import source_strategy

    undo = _patch(source_strategy, "plan_allows_phase", lambda *_a, **_kw: (True, "ok"))
    try:
        with console() as (app, _):
            item = status_of(app, "plan")
            check(item["status"] == diagnostics.FAILED, "plan-gate-detected", item["status"])
            check("kapi" in item["summary"].lower() or "kapi" in item["blocker"].lower()
                  or "acik" in item["summary"].lower(),
                  "plan-gate-message", item["summary"])
    finally:
        undo()


def section_injection_review_gate():
    print("\n[6c] Enjeksiyon: eksik listeyle render onaylaniyor")
    from creator_console import render_review

    undo = _patch(render_review, "evaluate", lambda *_a, **_kw: {
        "complete": True, "open_revision": None, "blocker": "", "total_count": 6,
        "checked_count": 6, "items": [], "entries": {}})
    try:
        with console() as (app, _):
            item = status_of(app, "review")
            check(item["status"] == diagnostics.FAILED, "review-gate-detected", item["status"])
            check("ONAYLANDI" in item["summary"] or "liste" in item["summary"].lower(),
                  "review-gate-message", item["summary"])
    finally:
        undo()


def section_injection_stale_selection():
    print("\n[6d] Enjeksiyon: bayat moment secimi gecerli sayiliyor")
    from local_pipeline import video_analysis

    undo = _patch(video_analysis, "validate_moment_selection", lambda *_a, **_kw: [])
    try:
        with console() as (app, _):
            item = status_of(app, "moment_preview")
            check(item["status"] == diagnostics.DEGRADED, "stale-selection-detected",
                  item["status"])
            check("bayat" in item["blocker"].lower(), "stale-selection-message",
                  item["blocker"])
    finally:
        undo()


def section_injection_path_leak():
    print("\n[6e] Enjeksiyon: mutlak host yolu artefakta siziyor")
    from creator_console import pipeline_service

    original = pipeline_service.PipelineService.register_source_video

    def leaky(self, *, filename, payload, registered_by):
        record = original(self, filename=filename, payload=payload,
                          registered_by=registered_by)
        data = self.load_artifact("project")["data"]
        data["source"]["stored_path"] = str(self.path("project").parent / filename)
        self.write_artifact("project", data, actor=registered_by)
        return record

    pipeline_service.PipelineService.register_source_video = leaky
    try:
        with console() as (app, _):
            item = status_of(app, "source_upload")
            check(item["status"] == diagnostics.FAILED, "path-leak-detected", item["status"])
            check("mutlak" in item["summary"].lower(), "path-leak-message", item["summary"])
    finally:
        pipeline_service.PipelineService.register_source_video = original


def section_injection_traversal():
    print("\n[6f] Enjeksiyon: yol gezinmesi engellenmiyor")
    from creator_console import storage

    original = storage.LocalFilesystemStorage.resolve

    def permissive(self, ownership, *parts, create_parent=False):
        # normpath WITHOUT a containment check -- the classic mistake. '..' is
        # honoured, so the request reads a real file outside the namespace
        # instead of being refused.
        import os

        target = Path(os.path.normpath(
            os.path.join(str(self.namespace_root(ownership)), *[str(p) for p in parts])))
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    storage.LocalFilesystemStorage.resolve = permissive
    try:
        with console() as (app, _):
            item = status_of(app, "media_serving")
            check(item["status"] == diagnostics.FAILED, "traversal-detected", item["status"])
            check("SUNDU" in item["summary"] or "gezinme" in item["summary"].lower(),
                  "traversal-message", item["summary"])
    finally:
        storage.LocalFilesystemStorage.resolve = original


def section_injection_automation_gate():
    print("\n[6g] Enjeksiyon: otomasyon insan kapisini asiyor")
    from creator_console import automation

    original = automation.AutomationEngine._next_step

    def skipping(self):
        decision = original(self)
        # Walk straight past the mode-selection gate, as a regression that
        # let automation pick a strategy on its own would.
        if decision.get("blocker") == "plan_selection":
            return {"kind": "done", "message": "kapi asildi (enjekte edildi)"}
        return decision

    automation.AutomationEngine._next_step = skipping
    try:
        with console() as (app, _):
            item = status_of(app, "automation")
            check(item["status"] == diagnostics.FAILED, "automation-gate-detected",
                  item["status"])
            check("plan kapisinda durmadi" in item["summary"], "automation-gate-message",
                  item["summary"])
    finally:
        automation.AutomationEngine._next_step = original


def section_injection_atomic_write():
    print("\n[6h] Enjeksiyon: atomik yazim gecici dosya birakiyor")
    from creator_console import storage

    original = storage.atomic_write_bytes

    def leaky(path, payload):
        original(path, payload)
        Path(path).parent.joinpath(f".{Path(path).name}.tmp-leak").write_bytes(b"x")

    undo = _patch(storage, "atomic_write_bytes", leaky)
    try:
        with console() as (app, _):
            item = status_of(app, "persistence")
            check(item["status"] == diagnostics.FAILED, "atomic-leak-detected", item["status"])
            check("tmp" in item["blocker"], "atomic-leak-message", item["blocker"])
    finally:
        undo()


@contextmanager
def patched_capability(key, replacement):
    """Swap one registry entry's check. The registry holds live function
    references, so patching the module attribute would not be used."""
    entry = next(item for item in diagnostics.CAPABILITIES if item["key"] == key)
    original = entry["check"]
    entry["check"] = replacement
    try:
        yield
    finally:
        entry["check"] = original


def section_injection_check_crashes():
    print("\n[6i] Enjeksiyon: kontrolun kendisi patliyor")
    secret = "AIzaSy" + "C" * 33

    def crashing(fixture, live_app):
        raise RuntimeError(f"beklenmeyen cokme, anahtar {secret} loglandi")

    with patched_capability("render", crashing):
        with console({"GeminiKey": secret}) as (app, _):
            report = run(app, only=["render"])
            item = by_key(report)["render"]
            check(item["status"] == diagnostics.FAILED, "crash-becomes-failed", item["status"])
            check(secret not in json.dumps(report), "crash-blocker-scrubbed",
                  "cokme mesajindaki sir raporlandi")
            check("RuntimeError" in item["blocker"], "crash-type-reported", item["blocker"])
            check(len(report["capabilities"]) == 1, "crash-does-not-abort-run",
                  "cokme tum kosuyu durdurdu")


def section_scrubbing():
    print("\n[6k] Rapora host yolu sizmiyor")

    def leaky_path(fixture, live_app):
        raise RuntimeError(f"acilamadi: {fixture.root / 'deep' / 'file.mp4'}")

    with patched_capability("render", leaky_path):
        with console() as (app, _):
            report = run(app, only=["render"])
            blob = json.dumps(report, ensure_ascii=False)
            check(diagnostics.HOST_PATH_PLACEHOLDER in blob, "host-path-replaced",
                  by_key(report)["render"]["blocker"])
            check("console_diag_" not in blob, "no-fixture-root",
                  "fikstur koku rapora sizdi")


def section_injection_assessment_autoselects():
    print("\n[6j] Enjeksiyon: degerlendirme kendi kendine mod seciyor")
    from local_pipeline import source_strategy

    original = source_strategy.assess_source

    def auto(*args, **kwargs):
        data = original(*args, **kwargs)
        data["selected_mode"] = data.get("recommended_mode")
        data["auto_selected"] = True
        return data

    undo = _patch(source_strategy, "assess_source", auto)
    try:
        with console() as (app, _):
            item = status_of(app, "assessment")
            check(item["status"] == diagnostics.FAILED, "autoselect-detected", item["status"])
            check("SECTI" in item["summary"], "autoselect-message", item["summary"])
    finally:
        undo()


# ---------------------------------------------------------------------------
# 7. API surface
# ---------------------------------------------------------------------------
def section_api():
    print("\n[7] API yuzeyi")
    with console({"GeminiKey": "x" * 39}) as (app, _):
        api = ConsoleApi(app)

        status, payload = api.handle("GET", "/api/diagnostics", body=b"", headers={})
        check(status == 200, "get-status", str(status))
        report = payload["diagnostics"]
        check(report.get("never_run") is True and report["last_run_at"] is None,
              "get-before-run-empty", "hic calismadan rapor dondu")
        check(len(report["capabilities"]) == len(diagnostics.CAPABILITIES),
              "empty-lists-capabilities", "bos rapor yetenekleri listelemiyor")

        status, payload = api.handle("POST", "/api/diagnostics",
                                     body=json.dumps({"only": ["persistence"]}).encode(),
                                     headers={})
        check(status == 200, "post-status", str(status))
        check([c["key"] for c in payload["diagnostics"]["capabilities"]] == ["persistence"],
              "post-only-filter", "only filtresi uygulanmadi")

        status, payload = api.handle("GET", "/api/diagnostics?run=1&only=media_serving",
                                     body=b"", headers={})
        check(status == 200 and [c["key"] for c in payload["diagnostics"]["capabilities"]]
              == ["media_serving"], "get-run-query", str(status))

        status, payload = api.handle("GET", "/api/diagnostics", body=b"", headers={})
        cached = payload["diagnostics"]
        check(cached.get("last_run_at") is not None, "cached-after-run",
              "kosudan sonra onbellek bos")
        check(cached["capabilities"][0]["key"] == "media_serving", "cached-is-last-run",
              "onbellek son kosuyu yansitmiyor")

        status, _ = api.handle("DELETE", "/api/diagnostics", body=b"", headers={})
        check(status == 404, "unknown-method-rejected", str(status))


def section_persistence_of_report():
    print("\n[8] Rapor kaliciligi")
    root = Path(tempfile.mkdtemp(prefix="verify_diag_persist_"))
    try:
        app = ConsoleApp(ConsoleConfig(
            storage_root=root, user_id="usr_t", workspace_id="wsp_t", inline_jobs=True,
            secret_getter=lambda n: None, **fixture_client_factories()))
        first = run(app, only=["persistence"])
        app.shutdown()

        restarted = ConsoleApp(ConsoleConfig(
            storage_root=root, user_id="usr_t", workspace_id="wsp_t", inline_jobs=True,
            secret_getter=lambda n: None, **fixture_client_factories()))
        loaded = restarted.last_diagnostics()
        restarted.shutdown()
        check(loaded["last_run_at"] == first["last_run_at"], "survives-restart",
              "rapor yeniden baslatmada kayboldu")
        check(loaded.get("never_run") is not True, "restart-not-empty", "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def section_frontend():
    print("\n[9] Arayuz sozlesmesi (statik)")
    source = (PROJECT_ROOT / "creator_console" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "creator_console" / "static" / "app.css").read_text(encoding="utf-8")
    for needle, label in (
            ("data-testid': 'run-diagnostics'", "run-button"),
            ("data-testid': 'diagnostics-last-run'", "last-run"),
            ("data-testid': 'diagnostics-panel'", "panel"),
            ("diag-blocker-", "blocker-field"),
            ("diag-repair-", "repair-field"),
            ("diagnosticsPanel()", "panel-mounted")):
        check(needle in source, f"js-{label}", f"{needle!r} yok")
    check("diagnosticsPanel()" in source.split("function screenSettings")[1],
          "panel-in-settings", "panel Ayarlar ekraninda degil")
    for status in diagnostics.STATUSES:
        check(f".diag-row--{status}" in css, f"css-{status}", "durum stili yok")
    # A page load must never trigger a run.
    refresh_body = source.split("async function refresh()")[1].split("\n}")[0]
    check("api.post('/api/diagnostics'" not in refresh_body, "no-run-on-load",
          "sayfa yuklenmesi teshis kosusu tetikliyor")
    check("api.get('/api/diagnostics')" in refresh_body, "cached-on-load",
          "onbellekli rapor yuklenmiyor")


def main() -> int:
    print("=" * 72)
    print("YETENEK TESHISI DOGRULAMASI")
    print("=" * 72)
    sections = [
        section_registry_tabs, section_positive, section_isolation,
        section_no_live_calls, section_credentials,
        section_injection_ffmpeg, section_injection_approval_gate,
        section_injection_review_gate, section_injection_stale_selection,
        section_injection_path_leak, section_injection_traversal,
        section_injection_automation_gate, section_injection_atomic_write,
        section_injection_check_crashes, section_scrubbing,
        section_injection_assessment_autoselects,
        section_api, section_persistence_of_report, section_frontend,
    ]
    # Registry coverage first, without the optional import probe.
    print("\n[1] Yetenek kaydi")
    required = {"source_upload", "preflight", "assessment", "plan", "video_analysis",
                "moment_preview", "script", "tts", "render", "review", "automation",
                "persistence", "media_serving"}
    keys = set(diagnostics.CAPABILITY_KEYS)
    check(required <= keys, "registry-covers-all", f"eksik: {sorted(required - keys)}")
    check(len(keys) == len(diagnostics.CAPABILITIES), "registry-unique", "yinelenen anahtar")

    for section in sections:
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            FAILURES.append(f"{section.__name__}: {type(exc).__name__}: {exc}")
            print(f"  [{section.__name__}] COKTU — {type(exc).__name__}: {exc}")

    print()
    print("=" * 72)
    print(f"toplam {PASSED + len(FAILURES)} kontrol, {PASSED} gecti, {len(FAILURES)} basarisiz")
    if FAILURES:
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("DIAGNOSTICS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
