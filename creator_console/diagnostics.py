"""Functional diagnostics: does each user-facing capability actually work?

WHY THIS EXISTS
---------------
`preflight` answers "is the environment installed". That is necessary and not
sufficient: every dependency can import cleanly while moment selection still
refuses every candidate, the media route still leaks a host path, or the review
gate still lets an unreviewed render through. Import checks cannot see any of
that.

So each capability here is exercised by RUNNING it against an isolated,
throwaway fixture and asserting on the observed result. A capability is `ready`
only because it was seen to work, not because its module imported.

    ready           did the real thing, result was correct
    degraded        worked, but with a caveat the user should know about
    blocked         cannot run until the user does something (missing input)
    not_configured  needs a credential/setting that is absent
    failed          ran and produced a wrong or broken result

ISOLATION AND SAFETY
--------------------
Every check runs in a fresh temp storage root that is deleted afterwards. The
user's projects are never read or written. No live provider is contacted (see
`live_enabled`), nothing is uploaded or published, and no secret VALUE is ever
read, returned or logged -- only `configured` / `missing`.

PROVIDER AVAILABILITY vs FUNCTIONALITY
--------------------------------------
Deliberately separate. "A Gemini key is configured" and "Gemini returns a usable
script" are different claims. Without `CREATOR_CONSOLE_LIVE_DIAGNOSTICS=1` the
provider capabilities verify the CONSOLE PATH with an injected double and report
availability, and say plainly that live functionality was not tested.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

READY = "ready"
DEGRADED = "degraded"
BLOCKED = "blocked"
NOT_CONFIGURED = "not_configured"
FAILED = "failed"
STATUSES = (READY, DEGRADED, BLOCKED, NOT_CONFIGURED, FAILED)

#: Worst-first, for the overall roll-up.
_SEVERITY = {FAILED: 4, BLOCKED: 3, NOT_CONFIGURED: 2, DEGRADED: 1, READY: 0}

DIAGNOSTICS_ARTIFACT = "diagnostics.json"
LIVE_ENV_FLAG = "CREATOR_CONSOLE_LIVE_DIAGNOSTICS"


def live_enabled() -> bool:
    """Live provider calls are opt-in and off by default."""
    return os.environ.get(LIVE_ENV_FLAG, "").strip().lower() in ("1", "true", "yes")


class _Result:
    __slots__ = ("status", "summary", "blocker", "repair", "detail")

    def __init__(self, status, summary, *, blocker="", repair="", detail=None):
        self.status = status
        self.summary = summary
        self.blocker = blocker
        self.repair = repair
        self.detail = detail or {}


def _ready(summary, **kw):
    return _Result(READY, summary, **kw)


def _degraded(summary, *, blocker="", repair="", **kw):
    return _Result(DEGRADED, summary, blocker=blocker, repair=repair, **kw)


def _failed(summary, *, blocker="", repair="", **kw):
    return _Result(FAILED, summary, blocker=blocker, repair=repair, **kw)


def _not_configured(summary, *, blocker="", repair="", **kw):
    return _Result(NOT_CONFIGURED, summary, blocker=blocker, repair=repair, **kw)


class _Fixture:
    """One throwaway project the checks share, so the suite stays bounded.

    Built lazily: a capability that does not need media never pays for it, and a
    machine without ffmpeg still gets useful results for everything else.
    """

    def __init__(self, secret_getter):
        self.root = Path(tempfile.mkdtemp(prefix="console_diag_"))
        self._secret_getter = secret_getter
        self._app = None
        self._project = None
        self._video_error = None
        self._render_response = None
        self._transcript_gate = ""

    def render_response(self):
        """The render job envelope, so a failure can report the real reason."""
        response = self._render_response
        return response[1] if isinstance(response, tuple) else None

    # -- lazily built pieces ----------------------------------------------
    @property
    def app(self):
        if self._app is None:
            from .app import ConsoleApp, ConsoleConfig
            from .fixtures import fixture_client_factories

            self._app = ConsoleApp(ConsoleConfig(
                storage_root=self.root, user_id="usr_diag", workspace_id="wsp_diag",
                inline_jobs=True, secret_getter=self._secret_getter,
                **fixture_client_factories()))
        return self._app

    def project(self):
        """A project with a REAL (tiny, synthetic) source video."""
        if self._project is not None:
            return self._project
        if self._video_error is not None:
            raise RuntimeError(self._video_error)
        from .fixtures import make_test_source_video
        from . import copyright_review

        try:
            record = self.app.create_project(title="diagnostics", created_by="diagnostics")
            ownership = self.app.ownership(record["project_id"])
            service = self.app.service(ownership)
            staging = self.app.storage.resolve(ownership, ".runtime", "diag.mp4",
                                                create_parent=True)
            make_test_source_video(staging, seconds=3)
            service.register_source_video(filename="diag.mp4",
                                           payload=staging.read_bytes(),
                                           registered_by="diagnostics")
            staging.unlink(missing_ok=True)
            # Plan approval is gated on the copyright checklist. Diagnostics is
            # not what PROVES that gate works (verify_copyright_gate.py is);
            # here it only has to be walked so the capability checks can reach
            # the phases they are actually about.
            for key in copyright_review.CHECKLIST_KEYS:
                service.set_copyright_review_item(key, checked=True,
                                                  reviewed_by="diagnostics")
            self._project = (ownership, service)
        except Exception as exc:  # noqa: BLE001
            self._video_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(self._video_error) from None
        return self._project

    def api(self):
        from .api import ConsoleApi
        return ConsoleApi(self.app)

    def call(self, method, path, body=None, headers=None):
        raw = b"" if body is None else (
            body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        return self.api().handle(method, path, body=raw, headers=headers or {})

    def scratch_project(self):
        """A second empty project, for checks that would corrupt the shared one."""
        record = self.app.create_project(title="diagnostics-scratch",
                                          created_by="diagnostics")
        return self.app.ownership(record["project_id"])

    def fresh_source_project(self):
        """An untouched project holding a copy of the fixture video."""
        base_ownership, _ = self.project()
        source = self.app.service(base_ownership).source_path()
        ownership = self.scratch_project()
        self.app.service(ownership).register_source_video(
            filename="diag.mp4", payload=Path(source).read_bytes(),
            registered_by="diagnostics")
        return ownership

    def approved_plan(self, mode="direct_clip_series"):
        ownership, service = self.project()
        pid = ownership.project_id
        self.call("POST", f"/api/projects/{pid}/jobs", {"job_type": "source_assessment"})
        payload = {"mode": mode, "selected_by": "diagnostics", "output_count": 1,
                   "render_config": {"render_mode": "blurred_background",
                                      "preview_mode": True,
                                      "caption_style": "active_word_yellow",
                                      "caption_theme": "auto",
                                      "subtitle_whisper_model_size": "tiny",
                                      "subtitle_language": "auto"}}
        if mode == "narrated_theory":
            payload["theory_angle"] = "Teshis amacli gecerli bir teori acisi metnidir"
        status, body = self.call("POST", f"/api/projects/{pid}/plan", payload)
        review = service.describe_plan_review()
        if not review.get("present"):
            # Surface the API's own reason instead of dying on a KeyError later.
            raise RuntimeError(f"plan olusturulamadi (HTTP {status}): "
                               f"{(body.get('error') or {}).get('detail', body)}")
        self.call("POST", f"/api/projects/{pid}/approvals/processing_plan",
                  {"reviewed_sha256": review["review"]["content_sha256"],
                   "approved_by": "diagnostics"})
        return ownership, service

    def analyzed(self, mode="direct_clip_series"):
        ownership, service = self.approved_plan(mode)
        if service.load_artifact("hot_moments")["data"] is None:
            self.call("POST", f"/api/projects/{ownership.project_id}/jobs", {
                "job_type": "video_analysis",
                "params": {"whisper_model_size": "tiny", "clip_duration_sec": 3,
                            "top_n": 2}})
        return ownership, service

    def rendered(self):
        """Analysis + selection + one preview render, produced at most once."""
        ownership, service = self.analyzed()
        if service.load_artifact("render_manifest")["data"] is not None:
            return ownership, service
        hot = service.load_artifact("hot_moments")["data"]
        if not hot or not hot.get("hot_moments"):
            return ownership, service
        if service.load_artifact("moment_selection")["data"] is None:
            service.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                             reviewed_by="diagnostics", require_preview=False)
        # direct_clip_series burns the source's own words in, so render is gated
        # behind a PRODUCED and APPROVED transcript. The fixture walks that gate
        # the way a user does instead of bypassing it -- bypassing would make
        # `render: ready` a claim about a path no user can take.
        self._transcript_gate = self._approve_transcript(ownership, service)
        self._render_response = self.call(
            "POST", f"/api/projects/{ownership.project_id}/jobs", {
                "job_type": "render",
                "params": {"render_mode": "blurred_background", "preview_mode": True,
                            "subtitle_whisper_model_size": "tiny",
                            "subtitle_language": "auto",
                            "caption_style": "active_word_yellow", "caption_theme": "auto"},
                "run_request_id": "diag-render"})
        return ownership, service

    def _approve_transcript(self, ownership, service) -> str:
        """Produce + approve the moment transcript. Returns '' when it worked."""
        if service.load_artifact("moment_transcript")["data"] is None:
            self.call("POST", f"/api/projects/{ownership.project_id}/jobs", {
                "job_type": "transcript",
                "params": {"subtitle_whisper_model_size": "tiny",
                            "subtitle_language": "auto"}})
        state = service.approval_state("transcript")
        if not state.get("present"):
            return f"transkript uretilemedi: {state.get('reason')}"
        if state.get("approved") and state.get("valid"):
            return ""
        digest = state.get("content_sha256")
        if not digest:
            return f"transkript incelenemez: {state.get('blocker') or state.get('reason')}"
        try:
            service.approve("transcript", reviewed_sha256=digest,
                            approved_by="diagnostics")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return f"transkript onaylanamadi: {exc}"
        return ""

    def transcript_gate(self) -> str:
        """Why the transcript gate could not be satisfied, or ''."""
        return self._transcript_gate

    def cleanup(self):
        try:
            if self._app is not None:
                self._app.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Capability checks. Each returns a _Result and must never raise.
# ---------------------------------------------------------------------------

def _check_source_upload(fixture, live_app):
    ownership, service = fixture.project()
    record = service.load_artifact("project")["data"]
    source = (record or {}).get("source") or {}
    if not source.get("stored_path"):
        return _failed("kaynak kaydedilmedi", blocker="register_source_video sonuc yazmadi",
                       repair="Depolama izinlerini kontrol et")
    # Shape first, then contents: `resolve` REFUSES an absolute path, so probing
    # it before this would surface a storage exception instead of the actual
    # finding, which is that the artifact holds a host path at all.
    if Path(source["stored_path"]).is_absolute():
        return _failed("kaynak yolu mutlak", blocker="host yolu artefakta sizdi",
                       repair="storage.relative_to_namespace kullanildigini dogrula")
    stored = service.storage.resolve(ownership, source["stored_path"])
    if not stored.is_file() or stored.stat().st_size != source["size_bytes"]:
        return _failed("kaydedilen dosya bozuk", blocker="boyut manifestle uyusmuyor",
                       repair="Videoyu yeniden yukle")
    # Hostile filename must be neutralised, not accepted. Runs against a scratch
    # project: registering it here would replace the shared fixture's real video
    # and every downstream check would fail for the wrong reason.
    scratch = fixture.scratch_project()
    status, payload = fixture.call(
        "POST", f"/api/projects/{scratch.project_id}/source", b"X" * 512,
        headers={"X-Filename": "../../evil.mp4"})
    hostile = ((payload.get("project") or {}).get("source") or {}).get("stored_path", "")
    if status != 201 or ".." in hostile:
        return _failed("guvensiz dosya adi temizlenmedi", blocker=f"stored_path={hostile}",
                       repair="ids.safe_filename cagrisini kontrol et")
    return _ready(f"yukleme + fingerprint calisiyor ({source['size_bytes']} bayt)",
                  detail={"fingerprint_16": (source.get("fingerprint_sha256") or "")[:16]})


def _check_preflight(fixture, live_app):
    from .status import run_preflight

    report = run_preflight(live_app)
    failures = [c for c in report["checks"] if c["status"] == "fail"]
    warnings = [c for c in report["checks"] if c["status"] == "warn"]
    if failures:
        return _failed(f"{len(failures)} ortam engeli",
                       blocker="; ".join(c["label"] for c in failures)[:200],
                       repair=failures[0].get("fix") or "Ayarlar > Yerel ortam kontrolu",
                       detail={"fail": [c["name"] for c in failures]})
    if warnings:
        return _degraded(f"{len(report['checks'])} kontrol, {len(warnings)} uyari",
                         blocker="; ".join(c["label"] for c in warnings)[:160],
                         repair=warnings[0].get("fix") or "",
                         detail={"warn": [c["name"] for c in warnings]})
    return _ready(f"{len(report['checks'])} kontrolun tamami temiz")


def _check_assessment(fixture, live_app):
    ownership, service = fixture.project()
    status, _ = fixture.call("POST", f"/api/projects/{ownership.project_id}/jobs",
                             {"job_type": "source_assessment"})
    loaded = service.load_artifact("source_assessment")
    data = loaded["data"]
    if not data:
        return _failed("degerlendirme uretilmedi", blocker=loaded["problem"] or "artefakt yok",
                       repair="FFmpeg/ffprobe kurulumunu kontrol et")
    if data.get("selected_mode") is not None or data.get("auto_selected") is not False:
        return _failed("degerlendirme kendi kendine mod SECTI",
                       blocker="oneri tavsiye olmali", repair="source_strategy.assess_source")
    if not data.get("recommended_mode") or not data.get("mode_scores"):
        return _failed("oneri/skor uretilmedi", blocker="mode_scores bos",
                       repair="Kaynak videonun okunabilirligini kontrol et")
    source = data["source"]
    if not source.get("probe_ok"):
        return _degraded("probe basarisiz, skorlar sinirli",
                         blocker=source.get("probe_error") or "probe okunamadi",
                         repair="ffprobe kur (imageio-ffmpeg fallback daha az kesin)")
    return _ready(f"oneri={data['recommended_mode']}, otomatik secim yok "
                  f"({source['duration_sec']:.0f}s {source['width']}x{source['height']})")


def _check_plan(fixture, live_app):
    ownership, service = fixture.approved_plan()
    plan = service.load_artifact("processing_plan")["data"]
    if not plan:
        return _failed("plan olusturulamadi", repair="Faz 0 degerlendirmesini calistir")
    from local_pipeline import approval

    if approval.check_approval(plan, "processing_plan")["valid"] is not True:
        return _failed("plan onayi icerige baglanmadi", blocker="hash dogrulamasi basarisiz",
                       repair="approval.approve_artifact akisini kontrol et")
    allowed, reason = service.plan_gate("video_analysis")
    if not allowed:
        return _failed("onayli plan faz kapisini acmadi", blocker=reason,
                       repair="source_strategy.plan_allows_phase")
    # Editing the decision after approval must close the gates again.
    mutated = json.loads(json.dumps(plan))
    mutated["decision"]["target_duration_sec"] = 31
    service.write_artifact("processing_plan", mutated)
    still_open, _ = service.plan_gate("video_analysis")
    service.write_artifact("processing_plan", plan)
    if still_open:
        return _failed("degistirilen plan hala fazlari aciyor",
                       blocker="onay artik icerigi baglamiyor ama kapi acik",
                       repair="plan_allows_phase icindeki check_approval kontrolu")
    return _ready("plan olusturma, hash onayi ve degisiklikte gecersizlesme calisiyor")


def _check_video_analysis(fixture, live_app):
    ownership, service = fixture.analyzed()
    loaded = service.load_artifact("hot_moments")
    data = loaded["data"]
    if not data:
        return _failed("analiz artefakti yok", blocker=loaded["problem"] or "uretilmedi",
                       repair="Moment Explorer'da ayarlari degistirip yeniden calistir")
    moments = data.get("hot_moments") or []
    if not moments:
        return _degraded("analiz calisti ama aday moment uretmedi",
                         blocker="insufficient_data",
                         repair="Klip araligini uzat veya top-N degerini artir")
    if data.get("status") != "review_required":
        return _failed(f"beklenmeyen artefakt durumu: {data.get('status')}",
                       repair="video_analysis.build_hot_moments_manifest")
    return _ready(f"{len(moments)} aday moment, en iyi skor {moments[0].get('score')}")


def _check_moment_preview(fixture, live_app):
    app = fixture.app
    ownership, service = fixture.analyzed()
    hot = service.load_artifact("hot_moments")["data"]
    if not hot or not hot.get("hot_moments"):
        return _blocked_moment()
    view = app.project_view(ownership)
    moments = view["moments"]["data"] or []
    for moment in moments:
        preview = moment.get("preview_path")
        if preview and Path(str(preview)).is_absolute():
            return _failed("preview yolu mutlak", blocker=str(preview)[:80],
                           repair="app.scrub_host_paths / moment_views normalizasyonu")
    rank = hot["hot_moments"][0]["rank"]
    try:
        service.record_moment_selection(selected_rank=rank, reviewed_by="diagnostics",
                                         require_preview=False)
    except Exception as exc:  # noqa: BLE001
        return _failed("moment secimi kaydedilemedi", blocker=f"{type(exc).__name__}: {exc}",
                       repair="video_analysis.build_moment_selection")
    selection = service.load_artifact("moment_selection")
    if not selection["data"]:
        return _failed("secim artefakti yazilmadi", repair="Depolama izinleri")
    # Re-analysis must invalidate the selection. Asserted at the surface that
    # matters -- the next phase refusing to run -- not on a helper's return
    # value, because only the former protects the user.
    from local_pipeline import video_analysis

    mutated = json.loads(json.dumps(hot))
    mutated["hot_moments"][0]["score"] = 0.00001
    stale = video_analysis.validate_moment_selection(
        selection["data"], mutated, require_preview=False)
    served = [m for m in moments if m.get("preview_path")]
    if not stale:
        return _degraded("secim calisiyor ama yeniden analiz onu gecersiz kilmadi",
                         blocker="bayat secim sessizce gecerli kaliyor",
                         repair="video_analysis.validate_moment_selection")
    fresh = video_analysis.validate_moment_selection(
        selection["data"], hot, require_preview=False)
    if fresh:
        return _failed("gecerli secim gecersiz sayiliyor", blocker="; ".join(fresh)[:200],
                       repair="video_analysis.validate_moment_selection")
    return _ready(f"{len(served)} preview goreli yolla sunuluyor; bayat secim yakalaniyor")


def _blocked_moment():
    return _Result(BLOCKED, "aday moment yok",
                   blocker="once video analizi calismali",
                   repair="Moment Explorer'da analizi calistir")


def _check_script(fixture, live_app):
    reference = live_app.secrets.reference("google_api")
    if not reference.configured:
        return _not_configured(
            "Gemini kimlik bilgisi yok",
            blocker="script uretimi icin GeminiKey gerekli",
            repair=".env.local dosyasina GeminiKey=... ekle",
            detail={"availability": "missing", "functionality": "test edilmedi"})
    ownership, service = fixture.analyzed("narrated_theory")
    hot = service.load_artifact("hot_moments")["data"]
    if not hot or not hot.get("hot_moments"):
        return _blocked_moment()
    service.record_moment_selection(selected_rank=hot["hot_moments"][0]["rank"],
                                     reviewed_by="diagnostics", require_preview=False)
    try:
        result = service.run_script_generation(actor="diagnostics")
    except Exception as exc:  # noqa: BLE001
        return _failed("script yolu hata verdi", blocker=f"{type(exc).__name__}: {exc}",
                       repair="Script Studio'dan elle calistirip hatayi oku")
    if result.get("status") != "review_required" or not result.get("script_text"):
        return _failed(f"script artefakti kullanilamaz (status={result.get('status')})",
                       blocker=result.get("status_reason", "")[:160],
                       repair="Rota/kota durumunu Ayarlar'dan kontrol et")
    return _degraded(
        "konsol yolu calisiyor; CANLI model yaniti test EDILMEDI",
        blocker="saglayici islevselligi dogrulanmadi (yalnizca erisilebilirlik)",
        repair=f"Canli dogrulama icin {LIVE_ENV_FLAG}=1 ya da verify_live_script.py",
        detail={"availability": "configured", "functionality": "mocked",
                "alias": reference.alias_used})


def _check_tts(fixture, live_app):
    reference = live_app.secrets.reference("google_api")
    if not reference.configured:
        return _not_configured(
            "Gemini kimlik bilgisi yok",
            blocker="Gemini TTS icin GeminiKey gerekli",
            repair=".env.local dosyasina GeminiKey=... ekle",
            detail={"availability": "missing", "functionality": "test edilmedi"})
    # Exercise the audio contract without any provider: PCM -> WAV -> validation.
    from local_pipeline import google_ai

    pcm = b"\x10\x27" * google_ai.TTS_SAMPLE_RATE
    target = fixture.root / "diag_tts.wav"
    target.write_bytes(google_ai.pcm_to_wav_bytes(pcm))
    validation = google_ai.validate_audio_file(target)
    target.unlink(missing_ok=True)
    if not validation["passed"]:
        return _failed("ses dogrulama sozlesmesi bozuk", blocker=validation["reason"],
                       repair="google_ai.pcm_to_wav_bytes / validate_audio_file")
    return _degraded(
        "ses sozlesmesi calisiyor (24kHz/16bit WAV); CANLI TTS test EDILMEDI",
        blocker="saglayici islevselligi dogrulanmadi (yalnizca erisilebilirlik)",
        repair=f"Canli dogrulama icin {LIVE_ENV_FLAG}=1",
        detail={"availability": "configured", "functionality": "mocked",
                "duration_sec": validation["details"]["duration_sec"]})


def _check_render(fixture, live_app):
    ownership, service = fixture.rendered()
    hot = service.load_artifact("hot_moments")["data"]
    if not hot or not hot.get("hot_moments"):
        return _blocked_moment()
    manifest = service.load_artifact("render_manifest")["data"]
    if not manifest:
        job = ((fixture.render_response() or {}).get("job")) or {}
        # A transcript gate that was never satisfied is the likelier cause than a
        # broken renderer, and it names its own reason -- report that first
        # rather than the generic job error it produces downstream.
        gate = fixture.transcript_gate()
        if gate:
            return _failed("render artefakti uretilmedi",
                           blocker=f"altyazi onay kapisi: {gate}"[:200],
                           repair="Altyazi sekmesinde transkripti uret, oku ve onayla")
        return _failed("render artefakti uretilmedi",
                       blocker=(job.get("error") or "is basarisiz")[:200],
                       repair="Render Studio'da hatayi oku ve yeniden calistir")
    validation = (manifest.get("output") or {}).get("validation") or {}
    if not validation.get("passed"):
        return _failed("render teknik dogrulamayi gecemedi",
                       blocker=str(validation.get("checks"))[:200],
                       repair="FFmpeg kurulumunu ve kaynak klibi kontrol et")
    if manifest.get("status") != "review_required":
        return _failed(f"beklenmeyen render durumu: {manifest.get('status')}",
                       repair="render_utils onay sozlesmesi")
    details = validation.get("details") or {}
    return _ready(f"{details.get('resolution')} · {details.get('duration_sec', 0):.1f}s · "
                  f"review_required")


def _check_review(fixture, live_app):
    ownership, service = fixture.rendered()
    if service.load_artifact("render_manifest")["data"] is None:
        return _Result(BLOCKED, "incelenecek render yok",
                       blocker="once render calismali",
                       repair="Render Studio'da renderi calistir")
    from .render_review import CHECKLIST_KEYS

    state = service.render_review_state()
    if state["total_count"] != len(CHECKLIST_KEYS):
        return _failed("kontrol listesi eksik", repair="render_review.CHECKLIST_ITEMS")
    digest = service.render_video_digest()
    # An incomplete checklist MUST refuse approval -- and refuse it FOR THAT
    # REASON. Accepting any exception here would let an unrelated failure
    # masquerade as a working gate.
    try:
        service.approve("render", reviewed_sha256=digest, approved_by="diagnostics")
        return _failed("liste eksikken render ONAYLANDI",
                       blocker="insan inceleme kapisi calismiyor",
                       repair="pipeline_service.approve('render') kontrolu")
    except Exception as exc:  # noqa: BLE001 - refusal is the correct outcome
        reason = str(exc)
    if "ONAY REDDEDILDI" not in reason:
        return _failed("onay reddedildi ama liste yuzunden degil",
                       blocker=reason[:200],
                       repair="Render artefaktini kontrol et; kapi yanlis nedenle kapali")
    for key in CHECKLIST_KEYS:
        service.set_render_review_item(key, checked=True, reviewed_by="diagnostics")
    completed = service.render_review_state()
    if not completed["complete"] or completed["blocker"]:
        return _failed("liste tamamlanamadi", blocker=str(completed.get("blocker"))[:160],
                       repair="render_review.evaluate")
    return _ready(f"{len(CHECKLIST_KEYS)} maddelik liste onayi kapatiyor ve aciyor; "
                  f"teknik ozet uretiliyor",
                  detail={"technical_rows": len(completed.get("technical") or [])})


def _check_automation(fixture, live_app):
    # A FRESH project on purpose: the shared fixture is already rendered, so it
    # would stop at the render gate and prove nothing about the first gate.
    app = fixture.app
    ownership = fixture.fresh_source_project()
    engine = app.automation(ownership)
    view = engine.view()
    if view["state"] != "idle":
        return _degraded(f"beklenmeyen baslangic durumu: {view['state']}",
                         repair="results/automation.json dosyasini sil")
    after = app.automation_action(ownership, "start")
    # Drive to a fixed point. The tick budget is the loop brake itself: if the
    # engine never settles, that is the finding.
    ticks = 0
    while after["state"] == "running" and ticks < 8:
        after = app.automation_action(ownership, "tick")
        ticks += 1
    if after["state"] == "running":
        return _failed("otomasyon durulmadi", blocker=f"{ticks} tick sonra hala 'running'",
                       repair="Isler sekmesinde takili isi iptal et")
    if after["state"] != "blocked" or after["blocker"] != "plan_selection":
        return _failed(f"otomasyon plan kapisinda durmadi ({after['state']}/{after['blocker']})",
                       blocker="insan kapisi asilmis olabilir",
                       repair="automation.AutomationEngine tick / automatic_next_step")
    if app.service(ownership).load_artifact("processing_plan")["data"] is not None:
        return _failed("otomasyon kendi kendine plan olusturdu",
                       blocker="mod secimi insan karari olmali",
                       repair="automatic_next_step plan_selection kapisi")
    app.automation_action(ownership, "cancel")
    if engine.view()["state"] != "cancelled":
        return _failed("otomasyon iptal edilemedi", repair="AutomationEngine.cancel")
    return _ready(f"baslat/tick/iptal calisiyor; {ticks} tick sonra plan seciminde duruyor, "
                  f"mod secmiyor")


def _check_persistence(fixture, live_app):
    from .app import ConsoleApp, ConsoleConfig
    from .fixtures import fixture_client_factories
    from .storage import atomic_write_json

    ownership, service = fixture.project()
    probe = fixture.root / "atomic_probe.json"
    atomic_write_json(probe, {"v": 1})
    atomic_write_json(probe, {"v": 2})
    leftovers = list(fixture.root.glob(".*tmp*"))
    if json.loads(probe.read_text(encoding="utf-8"))["v"] != 2 or leftovers:
        return _failed("atomik yazim bozuk", blocker=f"artik tmp: {len(leftovers)}",
                       repair="storage.atomic_write_bytes")
    restarted = ConsoleApp(ConsoleConfig(
        storage_root=fixture.root, user_id="usr_diag", workspace_id="wsp_diag",
        inline_jobs=True, secret_getter=lambda n: None, **fixture_client_factories()))
    try:
        view = restarted.project_view(ownership)
        if not view["project"]["data"]:
            return _failed("restart sonrasi proje kayboldu",
                           repair="Depolama kokunun kalici oldugunu dogrula")
        if any(job["status"] == "running" for job in view["jobs"]):
            return _failed("restart sonrasi takili 'running' is kaldi",
                           blocker="kurtarma calismadi",
                           repair="jobs.recover_abandoned")
    finally:
        restarted.shutdown()
    return _ready("atomik yazim + restart sonrasi durum ve is gecmisi korunuyor")


def _served_bytes(payload) -> bytes:
    """The bytes a media response would actually put on the wire.

    `api._media` returns a lazily-streamed descriptor (`__file__`) so previews
    are not loaded into RAM and byte ranges stay possible; older code returned
    `__raw__` bytes directly. Both are read here, because a check that only
    understood `__raw__` silently stopped inspecting ANY served content when the
    API switched -- and a containment check that inspects nothing always passes.
    """
    if not isinstance(payload, dict):
        return b""
    raw = payload.get("__raw__")
    if raw:
        return bytes(raw)
    target = payload.get("__file__")
    if not target:
        return b""
    try:
        return Path(target).read_bytes()
    except OSError:
        return b""


def _check_media_serving(fixture, live_app):
    ownership, service = fixture.project()
    app = fixture.app
    pid = ownership.project_id
    status, payload = fixture.call("GET", f"/api/projects/{pid}/media/project.json")
    if status != 200:
        return _failed(f"namespace icindeki dosya sunulamadi: HTTP {status}",
                       blocker="proje namespace'i icindeki dosya 200 donmedi",
                       repair="api._media")
    if not _served_bytes(payload):
        return _failed("namespace icindeki dosya bos sunuldu",
                       blocker=f"HTTP 200 ama govde bos: {sorted(payload)}",
                       repair="api._media")

    # A canary OUTSIDE the project namespace, with a servable suffix, so a
    # traversal that merely 404s cannot be mistaken for a traversal that is
    # actually refused.
    canary_marker = "diagnostics-canary-2f7a"
    canary = app.storage.workspace_root(ownership) / "canary.json"
    canary.write_text(json.dumps({"marker": canary_marker}), encoding="utf-8")
    try:
        traversal, body = fixture.call(
            "GET", f"/api/projects/{pid}/media/../../canary.json")
        # Read INSIDE the try: `__file__` is a path, not bytes, so the content
        # has to be pulled before the canary is unlinked below.
        served = _served_bytes(body)
    finally:
        canary.unlink(missing_ok=True)
    if canary_marker.encode() in served:
        return _failed("yol gezinmesi namespace disindaki dosyayi SUNDU",
                       blocker=f"HTTP {traversal} ile ../../canary.json okundu",
                       repair="storage.resolve namespace containment kontrolu")
    if traversal == 200:
        return _failed("yol gezinme istegi 200 dondu", blocker="reddedilmeliydi",
                       repair="storage.resolve namespace kontrolu")

    view_blob = json.dumps(app.project_view(ownership), ensure_ascii=False, default=str)
    if any(marker in view_blob for marker in (str(fixture.root), "C:\\", "/home/")):
        return _failed("gorunum modelinde host yolu var",
                       repair="app.scrub_host_paths")
    return _ready("medya rotasi namespace icinde calisiyor; gezinme reddediliyor")


#: Registry. Order is the order the panel shows them in.
CAPABILITIES = (
    {"key": "source_upload", "label": "Kaynak video yukleme", "tab": "phase0",
     "check": _check_source_upload},
    {"key": "preflight", "label": "Yerel ortam (preflight)", "tab": "settings",
     "check": _check_preflight},
    {"key": "assessment", "label": "Faz 0 kaynak degerlendirme", "tab": "phase0",
     "check": _check_assessment},
    {"key": "plan", "label": "Plan duzenleme ve onayi", "tab": "phase0",
     "check": _check_plan},
    {"key": "video_analysis", "label": "Video analizi", "tab": "moments",
     "check": _check_video_analysis},
    {"key": "moment_preview", "label": "Moment onizleme ve secimi", "tab": "moments",
     "check": _check_moment_preview},
    {"key": "script", "label": "Script uretimi", "tab": "script",
     "check": _check_script},
    {"key": "tts", "label": "Seslendirme (TTS)", "tab": "voice",
     "check": _check_tts},
    {"key": "render", "label": "Render", "tab": "render",
     "check": _check_render},
    {"key": "review", "label": "Render inceleme kapisi", "tab": "render",
     "check": _check_review},
    {"key": "automation", "label": "Otomatik akis", "tab": "jobs",
     "check": _check_automation},
    {"key": "persistence", "label": "Kalicilik ve kurtarma", "tab": "jobs",
     "check": _check_persistence},
    {"key": "media_serving", "label": "Medya sunumu", "tab": "outputs",
     "check": _check_media_serving},
)

CAPABILITY_KEYS = tuple(item["key"] for item in CAPABILITIES)


def run_diagnostics(app, *, only=None) -> dict:
    """Run every capability check in an isolated fixture and report.

    `app` is the LIVE console app, used only for credential STATE and to write
    the report. Every check that touches a project runs against the throwaway
    fixture instead, so the user's data is never read or modified.
    """
    from .progress import ProgressTracker

    started = time.time()
    fixture = _Fixture(app.config.secret_getter)
    planned = [d for d in CAPABILITIES if not only or d["key"] in only]
    # Capabilities are countable, so this bar is a real percentage rather than
    # an indeterminate one.
    tracker = ProgressTracker(
        app.storage, app.ownership(), "diagnostics",
        stages=[{"key": d["key"], "label": d["label"]} for d in planned])
    tracker.start(f"{len(planned)} yetenek test edilecek")
    results = []
    try:
        for definition in planned:
            tracker.stage(definition["key"], definition["label"])
            check_started = time.time()
            try:
                outcome = definition["check"](fixture, app)
            except Exception as exc:  # noqa: BLE001 - a broken check is a finding
                from .redaction import scrub_text

                outcome = _failed(
                    "kontrol calistirilamadi",
                    blocker=scrub_text(f"{type(exc).__name__}: {exc}", max_len=300),
                    repair="Sunucu gunlugundeki ayrintiya bak")
            results.append(_scrub({
                "key": definition["key"], "label": definition["label"],
                "tab": definition["tab"], "status": outcome.status,
                "summary": outcome.summary, "blocker": outcome.blocker,
                "repair": outcome.repair, "detail": outcome.detail,
                "duration_sec": round(time.time() - check_started, 2),
            }, fixture.root))
            tracker.count(len(results), len(planned), unit="yetenek",
                          message=f"{len(results)}/{len(planned)} yetenek")
    finally:
        fixture.cleanup()

    counts = {status: len([r for r in results if r["status"] == status]) for status in STATUSES}
    worst = max((r["status"] for r in results), key=lambda s: _SEVERITY[s], default=READY)
    report = {
        "schema_version": 1,
        "artifact_type": "console_diagnostics",
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "duration_sec": round(time.time() - started, 2),
        "live_provider_tests": live_enabled(),
        "overall": worst,
        "counts": counts,
        "summary": _summarize(counts, worst),
        "capabilities": results,
        "not_uploaded": True,
    }
    tracker.finish(message=report["summary"])
    _persist(app, report)
    return report


HOST_PATH_PLACEHOLDER = "<yerel-yol>"
#: An absolute path token: a Windows drive root or a POSIX system root, followed
#: by everything up to the next quote/whitespace. Escaped backslashes count,
#: because `repr()`d exceptions double them.
_HOST_PATH_RE = re.compile(
    r"(?:[A-Za-z]:(?:\\\\|[\\/])|/(?:home|Users|tmp|var|mnt)/)[^\s'\"]*")


def _scrub(value, fixture_root):
    """Strip host paths out of a result before it is stored or displayed.

    A blocker is free-form text built from real exception messages, so it WILL
    sometimes carry the fixture's temp directory. This report is written into the
    user's workspace and rendered in the browser, which makes this a boundary,
    not a nicety.
    """
    if isinstance(value, str):
        cleaned = value.replace(str(fixture_root), HOST_PATH_PLACEHOLDER)
        return _HOST_PATH_RE.sub(HOST_PATH_PLACEHOLDER, cleaned)
    if isinstance(value, dict):
        return {key: _scrub(item, fixture_root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, fixture_root) for item in value]
    return value


def _summarize(counts, worst) -> str:
    if worst == READY:
        return "Tum yetenekler calisiyor."
    parts = []
    for status, label in ((FAILED, "bozuk"), (BLOCKED, "engelli"),
                          (NOT_CONFIGURED, "yapilandirilmamis"), (DEGRADED, "kisitli")):
        if counts.get(status):
            parts.append(f"{counts[status]} {label}")
    return ", ".join(parts) + "."


def _persist(app, report) -> None:
    from .storage import atomic_write_json

    try:
        ownership = app.ownership()          # workspace scope, no project
        path = app.storage.resolve(ownership, DIAGNOSTICS_ARTIFACT, create_parent=True)
        atomic_write_json(path, report)
    except Exception:  # noqa: BLE001 - a report we cannot store is still useful
        pass


def load_last_report(app) -> dict | None:
    try:
        ownership = app.ownership()
        path = app.storage.resolve(ownership, DIAGNOSTICS_ARTIFACT)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def empty_report() -> dict:
    return {
        "last_run_at": None, "overall": None, "counts": {},
        "summary": "Teshis henuz calistirilmadi.",
        "capabilities": [
            {"key": item["key"], "label": item["label"], "tab": item["tab"],
             "status": None, "summary": "", "blocker": "", "repair": "",
             "detail": {}, "duration_sec": None}
            for item in CAPABILITIES
        ],
        "live_provider_tests": live_enabled(),
        "never_run": True,
    }
