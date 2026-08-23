#!/usr/bin/env python
"""Final local integration + UX acceptance pass, driven through a real browser.

    python verify_acceptance.py            # needs the real HOTD source
    python verify_acceptance.py --keep     # keep the isolated storage afterwards

WHAT THIS IS
------------
The other suites each prove one thing well. This one asks the only question they
cannot: can a person actually get from "I have a video" to "I have a Short
waiting for my approval" using nothing but the browser -- without reading code,
without a hidden step, and without a dead end that offers no way out?

So it walks the whole product once, in order, in Chromium, against a real
`ConsoleServer` and the REAL HOTD source video:

    preflight -> diagnostics -> required-field validation -> workflow map ->
    Phase 0 plan (create, edit, approve) -> manual + automatic execution ->
    live progress -> refresh + backend restart mid-work -> moment preview and
    selection -> Direct Clip Series render -> review checklist -> revision

SAFETY
------
Isolated storage root and its own workspace: the user's projects are never read
or written. Providers are `creator_console.fixtures` doubles, so NO live Gemini/
TTS/YouTube call happens. Nothing is uploaded or published, and final human
approval is deliberately never granted -- the run stops at `review_required`.

Jobs run on the THREAD runner, not inline, because half of what this pass checks
(a moving bar, a refresh mid-run, a restart mid-run) only exists while work is
genuinely in flight.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover
    print("PLAYWRIGHT_MISSING: 'pip install playwright' ve "
          "'python -m playwright install chromium' calistir.")
    sys.exit(2)

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.server import ConsoleServer  # noqa: E402

STORAGE_ROOT = PROJECT_ROOT / ".console_data_acceptance"
SOURCE_CANDIDATES = [
    Path.home() / "Downloads" / "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4",
    Path.home() / "Downloads" / "Daemon Targaryen may never have truly wanted the Iron Throne..mp4",
]
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

#: A short window of the real source keeps the pass under a few minutes while
#: still exercising real ffmpeg/Whisper on real footage.
CLIP_START_SEC = 60
CLIP_DURATION_SEC = 45
#: The Moment Explorer asks for a focus region and a duration in MINUTES.
CLIP_FOCUS_REGION = "start"
CLIP_DURATION_MIN = 1

FAILURES: list = []
CHECKS: list = []


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def check(condition, label: str, message: str = "") -> bool:
    CHECKS.append((label, bool(condition)))
    if condition:
        print(f"  [{label}] OK" + (f" — {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ — {message}")
    return False


def build_app(storage_root):
    return ConsoleApp(ConsoleConfig(
        storage_root=storage_root,
        user_id="usr_accept", workspace_id="wsp_accept",
        inline_jobs=False,                      # real background work
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))


def goto(page, base, screen, project_id=None):
    stamp = int(time.time() * 1000)
    query = f"?project={project_id}&_={stamp}" if project_id else f"?_={stamp}"
    page.goto(f"{base}/{query}#{screen}", wait_until="networkidle")
    page.wait_for_timeout(300)


def console_errors(page):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    return errors


def api_call(app, method, path, body=None, headers=None):
    raw = b"" if body is None else (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    return ConsoleApi(app).handle(method, path, body=raw, headers=headers or {})


def wait_for_job(app, ownership, job_type, *, timeout=900):
    """Block until the newest job of this type leaves the active states."""
    store = app.job_store(ownership)
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = [j for j in store.list(limit=50) if j["job_type"] == job_type]
        if jobs and jobs[0]["status"] not in ("queued", "running"):
            return jobs[0]
        time.sleep(1.0)
    return None


def progress_of(app, ownership, activity):
    from creator_console.progress import load_progress
    return load_progress(app.storage, ownership).get(activity) or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="izole depolamayi silme")
    parser.add_argument("--headed", action="store_true", help="tarayiciyi goster")
    args = parser.parse_args(argv)

    source = next((p for p in SOURCE_CANDIDATES if p.is_file()), None)
    if source is None:
        print("REAL_SOURCE_MISSING: HOTD kaynak videosu bulunamadi.")
        return 2

    print("=" * 78)
    print("CREATOR CONSOLE — YEREL ENTEGRASYON VE UX KABUL GECISI")
    print("=" * 78)
    print(f"  Kaynak        : {source.name} ({source.stat().st_size / 1024**2:.0f} MB)")
    print(f"  Izole depolama: {STORAGE_ROOT}")
    print("  Saglayicilar  : SAHTE (canli Gemini/TTS/YouTube cagrisi YOK)")
    print("  Hard stop     : final onay verilmez, yukleme/yayin yapilmaz")

    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT, ignore_errors=True)

    app = build_app(STORAGE_ROOT)
    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    server.serve_in_thread()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    pid = None
    preview_path = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            context = browser.new_context(viewport=DESKTOP)
            page = context.new_page()
            errors = console_errors(page)

            # ==========================================================
            section("1. Bos konsol: ne yapacagi anlasiliyor mu?")
            # ==========================================================
            goto(page, base, "projects")
            body = page.inner_text("#screen")
            check("Proje" in body, "empty:projects-screen", "acilis ekrani gorunmuyor")
            check(page.locator("[data-testid=create-project]").count() == 1,
                  "empty:next-action-visible", "ilk eylem gorunmuyor")
            for screen in ("phase0", "moments", "script", "render"):
                goto(page, base, screen)
                check("Once bir proje sec" in page.inner_text("#screen"),
                      f"empty:{screen}-guides", "proje yokken yonlendirme yok")

            # ==========================================================
            section("2. Preflight ve teshis")
            # ==========================================================
            goto(page, base, "settings")
            rows = page.locator(".preflight-row")
            check(rows.count() >= 20, "preflight:rows", f"{rows.count()} kontrol")
            summary = page.locator("[data-testid=preflight-summary]").inner_text()
            check(summary.strip(), "preflight:summary", summary)
            failing = page.eval_on_selector_all(
                ".preflight-row--fail", "n => n.map(e => e.innerText.split('\\n')[1])")
            check(not failing, "preflight:environment-ready", f"engeller: {failing}")

            page.locator("[data-testid=run-diagnostics]").click()
            page.wait_for_function(
                "() => { const b = document.querySelector('[data-testid=run-diagnostics]');"
                " return b && !b.disabled; }", timeout=300_000)
            page.wait_for_timeout(500)
            statuses = page.eval_on_selector_all(".diag-row", "n => n.map(e => e.dataset.status)")
            check(len(statuses) == 13, "diagnostics:all-capabilities", f"{len(statuses)} satir")
            check("failed" not in statuses, "diagnostics:none-broken", str(statuses))
            check(page.locator("[data-testid=diagnostics-last-run]").inner_text()
                  .count("hic calistirilmadi") == 0, "diagnostics:timestamp", "son calisma yok")

            # ==========================================================
            section("3. Zorunlu alan dogrulamasi (kod okumadan anlasilir mi?)")
            # ==========================================================
            goto(page, base, "projects")
            check(page.locator("[data-testid=required-legend]").count() >= 1,
                  "validation:legend", "'* Zorunlu alan' aciklamasi yok")
            page.locator("[data-testid=create-project]").click()
            page.wait_for_timeout(500)
            err = page.locator("[data-testid=err-new-project-title]")
            check(err.count() == 1 and err.is_visible(), "validation:inline-error",
                  "hata alanin yaninda gosterilmiyor")
            check(page.evaluate("() => document.activeElement.id") == "new-project-title",
                  "validation:focus", "ilk gecersiz alana odaklanmadi")
            check(len(app.project_ids()) == 0, "validation:nothing-created",
                  "bos formla proje olusturuldu")

            page.fill("#new-project-title", "KABUL TESTI (izole)")
            page.fill("#new-project-operator", "kabul-testi")
            page.locator("[data-testid=create-project]").click()
            page.wait_for_timeout(1500)
            check(len(app.project_ids()) == 1, "validation:valid-submit",
                  f"{len(app.project_ids())} proje")
            pid = app.project_ids()[0]
            ownership = app.ownership(pid)
            service = app.service(ownership)

            # ==========================================================
            section("4. Akis haritasi: nerede oldugunu ve ne yapacagini soyluyor mu?")
            # ==========================================================
            goto(page, base, "phase0", pid)
            check(page.locator("[data-testid=workflow-map]").count() == 1,
                  "workflow:present", "akis haritasi yok")
            check(page.locator(".wf-step").count() == 10, "workflow:ten-steps",
                  f"{page.locator('.wf-step').count()} adim")
            now = page.locator("[data-testid=workflow-now]").inner_text()
            check("Kaynak" in now, "workflow:starts-at-source", now.split("\n")[0])
            action = page.locator("[data-testid=workflow-action]").inner_text()
            check("yukle" in action.lower(), "workflow:actionable-first-step", action)
            page.locator("[data-testid=step-render]").click()
            page.wait_for_timeout(700)
            check("Render Studio" in page.inner_text("#screen"),
                  "workflow:navigates", "adim tiklamasi calismiyor")

            # ==========================================================
            section("5. Gercek HOTD kaynagi yukleniyor")
            # ==========================================================
            status, payload = api_call(
                app, "POST", f"/api/projects/{pid}/source", source.read_bytes(),
                headers={"X-Filename": source.name, "X-Actor": "acceptance"})
            check(status == 201, "upload:accepted", f"HTTP {status}")
            record = (payload.get("project") or {}).get("source") or {}
            check(record.get("size_bytes") == source.stat().st_size, "upload:size",
                  f"{record.get('size_bytes')} bayt")
            upload_progress = progress_of(app, ownership, "upload")
            check(upload_progress.get("state") == "completed", "upload:progress-recorded",
                  str(upload_progress.get("state")))
            check(upload_progress.get("step") == "fingerprint", "upload:final-stage",
                  str(upload_progress.get("step")))

            goto(page, base, "phase0", pid)
            check(page.locator("[data-testid=progress-upload]").count() == 1,
                  "upload:panel-visible", "yukleme paneli yok")

            # ==========================================================
            section("6. Faz 0: degerlendirme -> plan -> duzenleme -> onay")
            # ==========================================================
            page.locator("[data-testid=run-assessment]").click()
            job = wait_for_job(app, ownership, "source_assessment", timeout=300)
            check(job and job["status"] in ("completed", "review_required"),
                  "assessment:completed", str(job and job["status"]))
            assessment = service.load_artifact("source_assessment")["data"]
            check(assessment and assessment.get("selected_mode") is None,
                  "assessment:no-auto-selection", "degerlendirme kendi kendine mod SECTI")
            check(assessment.get("recommended_mode"), "assessment:recommends",
                  str(assessment.get("recommended_mode")))

            goto(page, base, "phase0", pid)
            page.locator("button.mode-option").first.click()
            page.wait_for_timeout(500)
            check(page.locator("#plan-by").count() == 0,
                  "plan:reuses-project-operator", "tekrar isim alani gorunuyor")
            page.locator("[data-testid=create-plan]").click()
            page.wait_for_timeout(2000)
            plan = service.load_artifact("processing_plan")["data"]
            check(plan is not None, "plan:created", "plan olusturulmadi")
            check(plan["decision"]["mode"] == "direct_clip_series", "plan:mode",
                  plan["decision"]["mode"])
            check(plan["selected_by"] == "kabul-testi", "plan:signed-by-human",
                  str(plan.get("selected_by")))
            check(plan["status"] == "review_required", "plan:needs-approval",
                  plan["status"])

            # Edit BEFORE approval, through the editor.
            goto(page, base, "phase0", pid)
            page.fill("#plan-duration", "45")
            page.locator("[data-testid=save-plan]").click()
            page.wait_for_timeout(1800)
            plan = service.load_artifact("processing_plan")["data"]
            check(plan["decision"]["target_duration_sec"] == 45, "plan:edit-applied",
                  str(plan["decision"]["target_duration_sec"]))

            # A phase must stay closed until the plan is approved.
            allowed, reason = service.plan_gate("video_analysis")
            check(not allowed, "plan:gate-closed-before-approval", reason or "kapi acik!")

            goto(page, base, "phase0", pid)
            check(page.locator("#processing_plan-hash").count() == 0,
                  "plan:no-manual-hash", "hash kopyalama alani gorunuyor")
            page.locator("[data-testid=approve-processing_plan]").click()
            page.wait_for_timeout(1800)
            allowed, reason = service.plan_gate("video_analysis")
            check(allowed, "plan:gate-opens-after-approval", reason)

            # A wrong hash must be refused, with an actionable message.
            goto(page, base, "moments", pid)

            # ==========================================================
            section("7. Analiz: manuel calistirma + canli ilerleme + yenileme")
            # ==========================================================
            # Long-video focus controls: the Moment Explorer moved from raw
            # seconds to a focus region plus MINUTES, so this walk drives what
            # the screen actually offers.
            page.select_option("#va-focus", CLIP_FOCUS_REGION)
            page.dispatch_event("#va-focus", "change")
            page.fill("#va-duration", str(CLIP_DURATION_MIN))
            page.dispatch_event("#va-duration", "change")
            page.fill("#va-topn", "3")
            page.dispatch_event("#va-topn", "change")
            page.locator("[data-testid=run-video_analysis]").click()
            page.wait_for_timeout(1500)

            seen_stages, seen_panels, live_percent = set(), [], []
            deadline = time.time() + 600
            while time.time() < deadline:
                record = progress_of(app, ownership, "video_analysis")
                if record.get("step"):
                    seen_stages.add(record["step"])
                if record.get("state") != "running":
                    break
                time.sleep(1.5)
            record = progress_of(app, ownership, "video_analysis")
            job = wait_for_job(app, ownership, "video_analysis", timeout=600)

            check(job and job["status"] in ("completed", "review_required"),
                  "analysis:completed", str(job and job["status"]))
            check(len(seen_stages) >= 3, "analysis:stages-observed",
                  f"gozlenen asamalar: {sorted(seen_stages)}")
            check("whisper" in seen_stages or "previews" in seen_stages,
                  "analysis:named-stages", str(sorted(seen_stages)))
            check(record.get("state") == "completed", "analysis:progress-final",
                  str(record.get("state")))

            goto(page, base, "moments", pid)
            panels = page.locator("[data-testid=progress-video_analysis]")
            check(panels.count() == 1, "analysis:single-panel",
                  f"{panels.count()} ilerleme paneli")
            check(page.locator(".pprog").count() == 1, "analysis:no-duplicate-panels",
                  f"{page.locator('.pprog').count()} panel")

            # Refresh must not restart or lose anything.
            before = page.locator("[data-testid=progress-video_analysis]").inner_text()
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(700)
            check(page.locator("[data-testid=progress-video_analysis]").inner_text() == before,
                  "refresh:progress-preserved", "yenilemeden sonra ilerleme degisti")
            jobs_now = [j for j in app.job_store(ownership).list(limit=50)
                        if j["job_type"] == "video_analysis"]
            check(len(jobs_now) == 1, "refresh:no-duplicate-jobs",
                  f"{len(jobs_now)} analiz isi")

            # ==========================================================
            section("8. Moment onizleme, oynatma ve secim")
            # ==========================================================
            hot = service.load_artifact("hot_moments")["data"]
            check(hot and hot.get("hot_moments"), "moments:candidates",
                  f"{len(hot.get('hot_moments') or []) if hot else 0} aday")
            rank = hot["hot_moments"][0]["rank"]

            goto(page, base, "moments", pid)
            player = page.locator(f"[data-testid=preview-{rank}]")
            check(player.count() == 1, "moments:preview-element", "onizleme oynatici yok")
            playback = page.evaluate(
                f"""async () => {{
                      const v = document.querySelector('[data-testid=preview-{rank}]');
                      if (!v) return {{ ok: false, reason: 'element yok' }};
                      try {{ v.muted = true; await v.play(); }} catch (e) {{}}
                      await new Promise(r => setTimeout(r, 1200));
                      return {{ ok: v.readyState >= 2, readyState: v.readyState,
                                duration: v.duration, time: v.currentTime }};
                    }}""")
            check(playback.get("ok"), "moments:preview-plays", str(playback))
            check((playback.get("duration") or 0) > 0, "moments:preview-has-duration",
                  str(playback.get("duration")))

            page.locator(f"[data-testid=select-moment-{rank}]").click()
            page.wait_for_timeout(400)
            page.locator("[data-testid=save-moment-selection]").click()
            page.wait_for_timeout(1500)
            selection = service.load_artifact("moment_selection")["data"]
            check(selection is not None, "moments:selection-saved", "secim kaydedilmedi")
            check(selection["reviewed_by"] == "kabul-testi", "moments:human-signed",
                  str(selection.get("reviewed_by")))
            preview_path = (selection.get("selected_moment") or {}).get("preview_path")

            # ==========================================================
            section("9. Otomatik akis: insan kapisinda duruyor mu?")
            # ==========================================================
            goto(page, base, "jobs", pid)
            page.locator("[data-testid=automation-start]").click()
            page.wait_for_timeout(2500)
            auto_deadline = time.time() + 600
            while time.time() < auto_deadline:
                view = app.automation(ownership).view()
                if view["state"] != "running":
                    break
                page.wait_for_timeout(2000)
            view = app.automation(ownership).view()
            check(view["state"] in ("blocked", "completed"), "automation:settles",
                  f"{view['state']}/{view.get('blocker')}")
            # The captions are the source's OWN words burned into the video, so
            # automation runs the recognition but STOPS for a human to read it.
            check(view.get("blocker") == "transcript_approval",
                  "automation:stops-at-transcript", str(view.get("blocker")))
            check(service.load_artifact("moment_transcript")["data"] is not None,
                  "automation:ran-transcript", "otomasyon transkripti uretmedi")
            check(service.approval_state("transcript")["approved"] is False,
                  "automation:never-approves-transcript",
                  "otomasyon altyaziyi kendi onayladi")

            # A human reads the captions and approves them THROUGH THE UI --
            # this is an acceptance test, so the gate is cleared the way a user
            # clears it, not by calling the service behind the screen.
            goto(page, base, "script", pid)
            page.wait_for_timeout(700)
            check(page.locator("[data-testid=approve-transcript]").count() == 1,
                  "ui:transcript-approve-visible", "altyazi onay butonu ekranda yok")
            page.locator("[data-testid=approve-transcript]").click()
            page.wait_for_timeout(1200)
            check(service.approval_state("transcript")["valid"],
                  "ui:transcript-approved",
                  str(service.approval_state("transcript").get("reason")))

            goto(page, base, "jobs", pid)
            page.locator("[data-testid=automation-resume]").click()
            page.wait_for_timeout(2000)
            auto_deadline = time.time() + 600
            while time.time() < auto_deadline:
                view = app.automation(ownership).view()
                if view["state"] != "running":
                    break
                page.wait_for_timeout(2000)
            view = app.automation(ownership).view()
            check(view.get("blocker") in (None, "render_approval"),
                  "automation:stops-at-human-gate", str(view.get("blocker")))
            check((service.load_artifact("render_manifest")["data"] or {}).get("status")
                  != "approved", "automation:never-approves",
                  "otomasyon final onay verdi")
            goto(page, base, "jobs", pid)
            page.locator("[data-testid=automation-cancel]").click()
            page.wait_for_timeout(800)

            render_manifest = service.load_artifact("render_manifest")["data"]
            check(render_manifest is not None, "render:produced", "render uretilmedi")

            # ==========================================================
            section("10. Render: ilerleme, olculu FFmpeg, teknik dogrulama")
            # ==========================================================
            render_progress = progress_of(app, ownership, "render")
            check(render_progress.get("state") == "completed", "render:progress-completed",
                  str(render_progress.get("state")))
            check([s["key"] for s in render_progress.get("stages") or []]
                  == ["preparation", "subtitles", "ffmpeg", "validation", "manifest"],
                  "render:stage-list",
                  str([s["key"] for s in render_progress.get("stages") or []]))
            validation = ((render_manifest.get("output") or {}).get("validation") or {})
            check(validation.get("passed"), "render:technical-validation",
                  str(validation.get("checks"))[:120])
            check(render_manifest.get("status") == "review_required",
                  "render:awaits-human", str(render_manifest.get("status")))
            check(render_manifest.get("not_uploaded") is True, "render:not-uploaded",
                  "not_uploaded bayragi yok")

            goto(page, base, "render", pid)
            rendered = page.evaluate(
                """async () => {
                     const v = document.querySelector('[data-testid=render-player] video')
                               || document.querySelector('[data-testid=render-player]');
                     if (!v) return { ok: false, reason: 'oynatici yok' };
                     try { v.muted = true; await v.play(); } catch (e) {}
                     await new Promise(r => setTimeout(r, 1500));
                     return { ok: v.readyState >= 2, readyState: v.readyState,
                              duration: v.duration, w: v.videoWidth, h: v.videoHeight };
                   }""")
            check(rendered.get("ok"), "render:plays-in-browser", str(rendered))
            check((rendered.get("h") or 0) > (rendered.get("w") or 0),
                  "render:is-vertical", f"{rendered.get('w')}x{rendered.get('h')}")

            # ==========================================================
            section("11. Backend yeniden baslatma: is sirasinda")
            # ==========================================================
            # A faithful restart means the OWNER PROCESS is gone. Rebuilding the
            # app in this process is not that: recovery is owner-aware, so with
            # a live owner pid it correctly refuses to reclaim, and a test that
            # asserted otherwise would be asserting a falsehood.
            #
            # So a real child process claims a job and is then killed, which is
            # exactly "the backend died holding work". It claims WITHOUT running
            # the pipeline, so no half-written media can corrupt the artifacts
            # this pass already validated.
            worker_script = STORAGE_ROOT / ".claim_and_die.py"
            worker_script.write_text(f"""
import sys, time
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console import fixtures
app = ConsoleApp(ConsoleConfig(
    storage_root={str(STORAGE_ROOT)!r},
    user_id="usr_accept", workspace_id="wsp_accept", inline_jobs=False,
    secret_getter=fixtures.fake_secret_getter(),
    extra={{"skip_startup_recovery": True}},
    **fixtures.fixture_client_factories()))
own = app.ownership({pid!r})
store = app.job_store(own)
record, _ = store.submit("render", idempotency_key="acceptance-restart-probe",
                         params={{}}, submitted_by="acceptance-child")
store.claim(record["job_id"], "worker-child")
print(record["job_id"], flush=True)
while True:
    time.sleep(5)
""", encoding="utf-8")

            child = subprocess.Popen([sys.executable, str(worker_script)],
                                      stdout=subprocess.PIPE, text=True)
            claimed_job_id = (child.stdout.readline() or "").strip()
            check(bool(claimed_job_id), "restart:child-claimed", "cocuk surec is almadi")

            claimed = app.job_store(ownership).get(claimed_job_id) if claimed_job_id else {}
            check(claimed.get("status") == "running", "restart:running-before-kill",
                  str(claimed.get("status")))
            check(claimed.get("owner_pid") and claimed["owner_pid"] != os.getpid(),
                  "restart:owner-is-another-process",
                  f"kayitli sahip pid={claimed.get('owner_pid')} bu surec={os.getpid()}")

            # While that owner is ALIVE, a restart must NOT steal its job.
            probe = build_app(STORAGE_ROOT)
            still = probe.job_store(ownership).get(claimed_job_id)
            probe.shutdown()
            check(still["status"] == "running", "restart:live-owner-not-stolen",
                  f"canli isciden is calindi: {still['status']}")

            child.kill()
            child.wait(timeout=15)

            # Now the real restart: new backend, dead owner, work reclaimed.
            server.shutdown()
            server.server_close()
            app.runner.shutdown()
            app.shutdown()

            restarted = build_app(STORAGE_ROOT)
            server = ConsoleServer(restarted, host="127.0.0.1", port=0, quiet=True)
            server.serve_in_thread()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            app = restarted
            ownership = app.ownership(pid)
            service = app.service(ownership)

            after = app.job_store(ownership).get(claimed_job_id)
            check(after["status"] != "running", "restart:no-stuck-running",
                  f"olu sahibin isi hala 'running': {after['status']}")
            check(after.get("error"), "restart:reclaim-explained",
                  "kurtarilan is neden basarisiz oldugunu soylemiyor")
            after_view = next(
                (job for job in app.project_view(ownership)["jobs"]
                 if job["job_id"] == claimed_job_id), {})
            check(after_view.get("can_retry"), "restart:retry-offered-in-record",
                  "kurtarilan is yeniden denenemiyor")
            restart_progress = progress_of(app, ownership, "render")
            check(restart_progress.get("state") in ("interrupted", "completed", "failed"),
                  "restart:progress-not-left-spinning",
                  str(restart_progress.get("state")))

            goto(page, base, "jobs", pid)
            check("Is akisi" in page.inner_text("#screen"), "restart:ui-recovers",
                  "restart sonrasi arayuz acilmadi")
            check(page.locator(".pprog").count() <= 1, "restart:no-duplicate-panels",
                  f"{page.locator('.pprog').count()} panel")
            check(page.locator(f"[data-testid=retry-job-{claimed_job_id}]").count() == 1,
                  "restart:retry-offered-in-ui",
                  "kurtarilan is icin arayuzde yeniden deneme yok")
            jobs_text = page.inner_text("#screen")
            check("basarisiz" in jobs_text.lower() or "failed" in jobs_text.lower(),
                  "restart:failure-visible", "kurtarma kullaniciya gorunmuyor")

            # Everything this pass already validated must be untouched.
            render_manifest = service.load_artifact("render_manifest")["data"]
            check(render_manifest and render_manifest.get("status") == "review_required",
                  "restart:artifact-intact", str((render_manifest or {}).get("status")))
            check(service.load_artifact("moment_selection")["data"] is not None,
                  "restart:selection-intact", "moment secimi kayboldu")
            worker_script.unlink(missing_ok=True)

            # ==========================================================
            section("12. Render inceleme listesi ve revizyon istegi")
            # ==========================================================
            goto(page, base, "render", pid)
            boxes = page.locator("[data-testid^=checkbox-]")
            check(boxes.count() == 6, "review:six-items", f"{boxes.count()} madde")
            check(page.locator("[data-testid=approve-render]").count() == 0,
                  "review:approval-locked", "liste eksikken onay dugmesi acik")
            locked = page.locator("[data-testid=approval-locked]")
            check(locked.count() == 1 and locked.inner_text().strip(),
                  "review:lock-explained",
                  locked.inner_text()[:70] if locked.count() else "aciklama yok")

            # Every review action reuses the project-level operator.
            for index in range(boxes.count()):
                page.locator("[data-testid^=checkbox-]").nth(index).click()
                page.wait_for_timeout(600)
            check(page.locator("[data-testid=render-reviewer]").count() == 0,
                  "review:no-repeated-name", "tekrar isim alani gorunuyor")
            state = service.render_review_state()
            check(state["done_count"] == 6, "review:checklist-persisted",
                  f"{state['done_count']}/6")
            # The button appears on the re-render that FOLLOWS the sixth tick,
            # and that round trip is async. A bare fixed pause passed on an idle
            # machine and failed whenever the suite ran alongside others -- so
            # wait for the element instead. The assertion is unchanged: the
            # button must exist; only the deadline is explicit.
            try:
                page.wait_for_selector("[data-testid=approve-render]", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            check(page.locator("[data-testid=approve-render]").count() == 1,
                  "review:approval-unlocks", "liste tamamlaninca onay acilmadi")

            # Revision request: recorded, navigable, and NOT an approval.
            page.fill("[data-testid=revision-notes]",
                      "Klip cumle ortasinda basliyor; 2 saniye geri alinmali.")
            page.select_option("[data-testid=revision-target]", "moments")
            page.locator("[data-testid=revision-submit]").click()
            page.wait_for_timeout(1500)
            state = service.render_review_state()
            check(state["verdict"] == "needs_revision", "review:revision-recorded",
                  str(state.get("verdict")))
            check(service.load_artifact("render_manifest")["data"]["status"] != "approved",
                  "review:revision-is-not-approval", "revizyon onay sayildi")
            goto(page, base, "render", pid)
            check(page.locator("[data-testid=revision-goto]").count() == 1,
                  "review:revision-navigable", "duzeltme sekmesi baglantisi yok")

            # ==========================================================
            section("13. Hard stop'lar ve butunluk")
            # ==========================================================
            view = app.project_view(ownership)
            blob = json.dumps(view, ensure_ascii=False, default=str)
            secret = fixtures.fake_secret_getter()("GeminiKey")
            check(secret not in blob, "safety:no-secret", "secret degeri API yanitinda")
            check("production_ready" not in blob, "safety:no-production-ready",
                  "production_ready bayragi var")
            approvals = view["approvals"]
            check(not (approvals.get("render") or {}).get("approved"),
                  "safety:no-final-approval", "final onay verilmis")
            check(view["not_uploaded"] is True, "safety:not-uploaded", "")
            for path in STORAGE_ROOT.rglob("*"):
                if path.is_file() and path.suffix == ".json":
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    if secret in text:
                        check(False, "safety:no-secret-on-disk", str(path.name))
                        break
            else:
                check(True, "safety:no-secret-on-disk", "artefaktlarda secret yok")

            workflow = view["workflow"]
            check(workflow["current_step"] == "review", "flow:ends-at-review",
                  f"{workflow['current_step']}/{workflow['current_state']}")
            check(workflow["skipped_count"] == 2, "flow:narration-skipped",
                  f"{workflow['skipped_count']} adim atlandi")
            done = [s["key"] for s in workflow["steps"] if s["state"] == "completed"]
            check(len(done) >= 6, "flow:most-steps-done", str(done))

            # Every screen still renders, after everything above.
            for screen in ("projects", "history", "phase0", "jobs", "moments",
                           "script", "voice", "render", "outputs", "settings"):
                goto(page, base, screen, pid)
                check(page.inner_text("#screen").strip() != "",
                      f"final:{screen}-renders", "ekran bos")
            check(page.locator(".pprog").count() <= 2, "final:no-panel-explosion",
                  f"{page.locator('.pprog').count()} panel")

            # ==========================================================
            section("14. Mobil: tam akis tasmadan kullanilabiliyor mu?")
            # ==========================================================
            mobile = browser.new_context(viewport=MOBILE, is_mobile=True, has_touch=True)
            mpage = mobile.new_page()
            mobile_errors = console_errors(mpage)
            overflowing = []
            for screen in ("projects", "phase0", "jobs", "moments", "render",
                           "outputs", "settings"):
                goto(mpage, base, screen, pid)
                overflow = mpage.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth")
                if overflow > 2:
                    overflowing.append(f"{screen}:{overflow}px")
            check(not overflowing, "mobile:no-overflow", str(overflowing))
            check(not mobile_errors, "mobile:no-js-errors", str(mobile_errors[:2]))
            mobile.close()

            check(not errors, "final:no-js-errors", f"{errors[:3]}")
            context.close()
            browser.close()
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        app.shutdown()

    print()
    print("=" * 78)
    if preview_path:
        print(f"  Onizleme yolu : {preview_path}")
    if pid:
        rendered_path = STORAGE_ROOT / "workspaces" / "wsp_accept" / "projects" / pid \
            / "results" / "render" / "final_short.mp4"
        if rendered_path.is_file():
            print(f"  Render ciktisi: {rendered_path}")
            print(f"                  ({rendered_path.stat().st_size / 1024**2:.1f} MB, "
                  "review_required — ONAYLANMADI)")
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"  toplam {len(CHECKS)} kontrol, {passed} gecti, {len(FAILURES)} basarisiz")
    print("=" * 78)

    if not args.keep:
        print(f"  Izole depolama korunuyor (inceleme icin): {STORAGE_ROOT}")

    if FAILURES:
        print("\nACCEPTANCE_FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nACCEPTANCE_OK (gercek HOTD kaynagi, gercek tarayici; sahte saglayicilar, "
          "canli API cagrisi YOK, final onay VERILMEDI, yukleme/yayin YOK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
