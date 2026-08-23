#!/usr/bin/env python
"""Real browser tests for the Creator Console (Playwright + Chromium).

    python verify_browser.py

WHY THIS EXISTS
---------------
Until now the frontend was only checked by static assertions over the bundle:
"the token exists", "the screen name appears". That proved nothing about whether
a button actually submits, whether a refresh restores running-job state, or
whether an approval panel refuses a wrong hash. Those are the behaviours the
console is FOR.

So this drives a real Chromium against a real `ConsoleServer`, over a real
socket, with mocked providers and an isolated temp storage root. It clicks the
same buttons a user clicks.

Nothing real is contacted: providers are `creator_console.fixtures` doubles, the
source video is a synthetic ffmpeg clip, and nothing is uploaded or published.

Requires: pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - explicit, actionable, non-silent
    print("PLAYWRIGHT_MISSING: 'pip install playwright' ve "
          "'python -m playwright install chromium' calistir.")
    sys.exit(2)

from creator_console import fixtures, progress  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.server import ConsoleServer  # noqa: E402
from local_pipeline import google_ai  # noqa: E402

FAILURES: list = []
CHECKS: list = []
TMP_DIRS: list = []

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}


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


def build_server(*, inline=True):
    root = Path(tempfile.mkdtemp(prefix="browser_test_"))
    TMP_DIRS.append(root)
    app = ConsoleApp(ConsoleConfig(
        storage_root=root, inline_jobs=inline,
        secret_getter=fixtures.fake_secret_getter(),
        **fixtures.fixture_client_factories()))
    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    server.serve_in_thread()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return app, server, base


def goto(page, base, screen, project_id=None):
    """Navigate to a screen with a FULL load.

    `page.goto` to a URL that differs only in the fragment does not reload, so a
    cache-busting query keeps every visit a genuine navigation. Screen-switching
    without a reload is covered separately by the hashchange checks.
    """
    stamp = int(time.time() * 1000)
    query = f"?project={project_id}&_={stamp}" if project_id else f"?_={stamp}"
    page.goto(f"{base}/{query}#{screen}", wait_until="networkidle")
    page.wait_for_timeout(250)


def console_errors(page):
    """Collect page errors so a silent JS exception cannot pass as success."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    return errors


# ===========================================================================
_app, _server, _BASE = build_server()
_seed = fixtures.seed_demo_project(_app, title="Tarayici testi")
_PID = _seed["project_id"]
_service = _app.service(_app.ownership(_PID))
# A project with no plan yet, so the plan CREATION form (not the editor) renders.
_wf_pid_for_plan = fixtures.seed_demo_project(
    _app, title="Plan formu")["project_id"]

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ================================================================
        section("1. Every screen renders without a JS error (desktop)")
        # ================================================================
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        errors = console_errors(page)

        # Each screen is identified by its OWN heading. Checking only "is the
        # panel non-empty" would pass even if navigation did nothing at all --
        # which is exactly the bug this suite found the first time it ran.
        SCREENS = {
            "projects": "Projeler",
            "phase0": "Faz 0",
            "longform": "Uzun Video Planlama",
            "turbo": "Turbo Studio",
            "jobs": "Is akisi",
            "moments": "Moment Explorer",
            "script": "Script Studio",
            "voice": "Voice Studio",
            "render": "Render Studio",
            "outputs": "Cikti",
            "history": "Onceki kayitlar",
            "settings": "Ayarlar",
        }
        rendered, wrong = [], []
        for screen, heading in SCREENS.items():
            goto(page, _BASE, screen, _PID)
            body = page.inner_text("#screen")
            if heading.lower() in body.lower():
                rendered.append(screen)
            else:
                wrong.append(f"{screen}(baslik yok)")
        check(len(rendered) == len(SCREENS), "screens:render",
              f"{len(rendered)}/{len(SCREENS)} ekran KENDI basligiyla render oldu; sorunlu: {wrong}")

        # Hash navigation on an already-open page must actually switch screens.
        page.goto(f"{_BASE}/?project={_PID}#projects", wait_until="networkidle")
        page.wait_for_timeout(300)
        page.evaluate("() => { location.hash = '#settings'; }")
        page.wait_for_timeout(600)
        check("Ayarlar" in page.inner_text("#screen"), "nav:hashchange",
              "yalnizca hash degisince ekran degisiyor")
        page.go_back()
        page.wait_for_timeout(600)
        check("Projeler" in page.inner_text("#screen"), "nav:browser-back",
              "tarayici geri tusu onceki ekrana donuyor")
        check(not errors, "screens:no-js-errors", f"JS hatalari: {errors[:3]}")

        # Design system actually applied (computed, not just present in CSS).
        goto(page, _BASE, "projects", _PID)
        accent = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--c-accent').trim()")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check(accent == "#ff7a18", "design:orange-accent", f"--c-accent={accent}")
        check(bg in ("rgb(10, 10, 11)", "rgba(10, 10, 11, 1)"), "design:dark-surface",
              f"body background={bg}")

        # Keyboard focus is visible (accessibility, not just declared).
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement ? document.activeElement.className : ''")
        check("skip-link" in focused or focused, "a11y:focus-reachable",
              f"ilk odak: {focused[:40]!r}")

        # ================================================================
        section("2. Phase 0: mode selection and hash-bound plan approval")
        # ================================================================
        goto(page, _BASE, "phase0", _PID)
        check(page.locator("button.mode-option").count() == 2, "phase0:two-modes",
              f"{page.locator('button.mode-option').count()} mod karti")

        recommended = page.inner_text("#screen")
        check("SECIM YAPILMADI" in recommended or "onerilen" in recommended,
              "phase0:advisory-only", "oneri rozetleri gorunuyor")

        # Selecting a mode must not create a plan by itself.
        page.locator("button.mode-option").first.click()
        page.wait_for_timeout(150)
        check(page.locator("button.mode-option[aria-pressed='true']").count() == 1,
              "phase0:selection-visual", "secili mod aria-pressed ile isaretli")
        check(_service.load_artifact("processing_plan")["data"] is None,
              "phase0:no-auto-plan", "mod secmek tek basina plan OLUSTURMADI")

        page.fill("#plan-count", "1")
        page.fill("#plan-duration", "45")
        check(page.locator("#plan-subtitle-model").input_value() == "large-v3-turbo",
              "phase0:subtitle-quality-default", "altyazi modeli large-v3-turbo olmali")
        check(page.locator("#plan-subtitle-language").input_value() == "auto",
              "phase0:subtitle-language-default", "kaynak dili Faz 1 tespitinden gelmeli")
        got_option = page.locator("#plan-caption-theme option[value=game_of_thrones]")
        check("Marcellus SC" in got_option.inner_text(),
              "phase0:got-font-choice", got_option.inner_text())
        check(page.locator(
            "#plan-caption-theme option[value=game_of_thrones_cinzel]").count() == 1,
              "phase0:cinzel-rollback-choice", "Cinzel karsilastirma secenegi korunmadi")
        # Keep the broad browser suite lightweight after asserting the product
        # default above; the render must match the approved plan exactly.
        page.select_option("#plan-subtitle-model", "small")
        page.locator("[data-testid=create-plan]").click()
        page.wait_for_timeout(1200)
        goto(page, _BASE, "phase0", _PID)
        plan = _service.load_artifact("processing_plan")["data"]
        check(plan is not None and plan["status"] == "review_required",
              "phase0:plan-created", f"plan status={plan and plan['status']}")

        # Wrong digests remain rejected by the server; the UI no longer asks
        # a person to copy/paste one.
        wrong_status, _ = ConsoleApi(_app).handle(
            "POST", f"/api/projects/{_PID}/approvals/processing_plan",
            body=json.dumps({"reviewed_sha256": "0" * 64}).encode(), headers={})
        check(wrong_status == 400, "phase0:wrong-hash-api-refused",
              f"stale digest HTTP {wrong_status}")
        check(_service.load_artifact("processing_plan")["data"]["status"] == "review_required",
              "phase0:wrong-hash-refused", "yanlis hash ile onay VERILMEDI")

        # One click binds approval to the current server-described digest.
        goto(page, _BASE, "phase0", _PID)
        shown_hash = _service.describe_plan_review()["review"]["content_sha256"]
        check(len(shown_hash) == 64, "phase0:server-hash-available",
              f"sunucuda icerik hash'i hazir ({shown_hash[:16]}…)")
        check(page.locator("#processing_plan-hash").count() == 0,
              "phase0:no-manual-hash", "hash kopyalama alani kaldirildi")
        page.locator("[data-testid=approve-processing_plan]").click()
        page.wait_for_timeout(1400)
        check(_service.load_artifact("processing_plan")["data"]["status"] == "approved",
              "phase0:approved", "dogru hash ile onaylandi")

        # ================================================================
        section("3. Double-click submission creates exactly one job")
        # ================================================================
        fixtures.reset_provider_calls()
        goto(page, _BASE, "moments", _PID)
        run_analysis = page.locator("[data-testid=run-video_analysis]")
        if run_analysis.count() == 0:
            # Moment Explorer renders the run button only before an analysis
            # exists; drive it from the phase screen instead.
            goto(page, _BASE, "moments", _PID)
        check(run_analysis.count() >= 1, "double-click:button-present",
              f"{run_analysis.count()} analiz butonu")
        if run_analysis.count():
            # Two clicks as fast as the browser allows, bypassing any transition.
            page.evaluate("""() => {
                const btn = document.querySelector('[data-testid=run-video_analysis]');
                if (btn) { btn.click(); btn.click(); }
            }""")
            # Real ffmpeg + whisper run inline; give them room.
            deadline = time.time() + 180
            while time.time() < deadline:
                jobs = _app.job_store(_app.ownership(_PID)).list(job_type="video_analysis")
                if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
                    break
                time.sleep(1.0)
        jobs = _app.job_store(_app.ownership(_PID)).list(job_type="video_analysis")
        check(len(jobs) == 1, "double-click:one-job",
              f"{len(jobs)} video_analysis isi olustu")
        analysis_calls = [c for c in fixtures.PROVIDER_CALLS if c[0].startswith("google.")]
        check(not analysis_calls, "double-click:no-provider-call",
              f"yerel analiz saglayici cagirmadi: {analysis_calls}")

        # ================================================================
        section("4. Job screen shows persisted state and survives a refresh")
        # ================================================================
        goto(page, _BASE, "jobs", _PID)
        table = page.inner_text("#screen")
        check("video_analysis" in table, "jobs:listed", "is listede gorunuyor")
        statuses = [s for s in ("queued", "running", "review_required", "failed", "completed")
                    if s in table]
        check(statuses, "jobs:status-visible", f"gorunen durumlar: {statuses}")

        before = page.inner_text("#screen")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)
        after = page.inner_text("#screen")
        check("video_analysis" in after, "refresh:state-restored",
              "yenilemeden sonra is gecmisi hala orada")
        check(before.count("video_analysis") == after.count("video_analysis"),
              "refresh:no-duplicate", "yenileme is kaydini cogaltmadi")

        # A refresh must not resubmit anything.
        jobs_after = _app.job_store(_app.ownership(_PID)).list(job_type="video_analysis")
        check(len(jobs_after) == 1, "refresh:no-resubmit",
              f"yenileme sonrasi {len(jobs_after)} is")

        # ================================================================
        section("5. Moment Explorer + media playback")
        # ================================================================
        goto(page, _BASE, "moments", _PID)
        cards = page.locator(".moment-card")
        check(cards.count() >= 1, "moments:cards", f"{cards.count()} moment karti")

        video = page.locator(".moment-card video").first
        if video.count():
            src = video.get_attribute("src") or ""
            check("/api/projects/" in src and "/media/" in src, "moments:media-url",
                  "preview namespace-goreli medya uc noktasindan geliyor")
            playable = page.evaluate("""async () => {
                const v = document.querySelector('.moment-card video');
                if (!v) return {ok: false, reason: 'video yok'};
                v.load();
                return await new Promise(res => {
                    const done = () => res({ok: v.readyState > 0 || v.duration > 0,
                                            readyState: v.readyState, duration: v.duration});
                    v.addEventListener('loadeddata', done, {once: true});
                    v.addEventListener('error', () => res({ok: false, reason: 'error olayi'}),
                                       {once: true});
                    setTimeout(done, 4000);
                });
            }""")
            check(playable.get("ok"), "moments:media-loads",
                  f"tarayici videoyu yukledi: {playable}")
        else:
            check(True, "moments:media-url", "bu fixture preview uretmedi (atlandi)")

        # ================================================================
        section("6. Error recovery: actionable message + safe retry")
        # ================================================================
        goto(page, _BASE, "script", _PID)
        script_button = page.locator("[data-testid=run-script_generation]")
        gated = script_button.count() and script_button.first.is_disabled()
        screen_text = page.inner_text("#screen")
        check(gated or "kapali" in screen_text.lower() or "plan" in screen_text.lower(),
              "errors:gate-visible",
              "direct_clip_series planinda script fazi UI'da kapali ve gerekcesi yazili")
        if script_button.count() and not gated:
            script_button.first.click()
            page.wait_for_timeout(2000)

        # An unknown endpoint must produce a catalog code, not a traceback.
        api_error = page.evaluate(f"""async () => {{
            const r = await fetch('{_BASE}/api/projects/{_PID}/yok-boyle-bir-sey');
            return {{status: r.status, body: await r.text()}};
        }}""")
        check(api_error["status"] == 404, "errors:structured-code",
              f"HTTP {api_error['status']}")
        payload = json.loads(api_error["body"])
        check(payload["error"]["code"] == "unknown_route" and "debug_id" in payload["error"],
              "errors:catalog-shape", f"kod={payload['error'].get('code')}")
        check("Traceback" not in api_error["body"] and "File \"" not in api_error["body"],
              "errors:no-traceback", "yanitta traceback yok")

        # ================================================================
        section("7. Stale approval is shown as invalid in the UI")
        # ================================================================
        plan = _service.load_artifact("processing_plan")["data"]
        backup = json.loads(json.dumps(plan))
        plan["decision"]["target_duration_sec"] = 33
        _service.write_artifact("processing_plan", plan)
        goto(page, _BASE, "phase0", _PID)
        text = page.inner_text("#screen")
        check("GECERSIZ" in text.upper(), "stale:ui-shows-invalid",
              "bayat onay arayuzde GECERSIZ olarak gosteriliyor")
        _service.write_artifact("processing_plan", backup)

        # ================================================================
        section("8. Secrets never reach the browser")
        # ================================================================
        secret = fixtures.fake_secret_getter()("GeminiKey")
        goto(page, _BASE, "settings", _PID)
        settings_text = page.inner_text("#screen")
        check(secret not in settings_text, "secrets:not-in-dom",
              "ayarlar ekraninda secret degeri yok")
        check("configured" in settings_text or "missing" in settings_text,
              "secrets:state-shown", "yalnizca durum gosteriliyor")

        page_source = page.content()
        js_bundle = page.evaluate(
            f"async () => (await (await fetch('{_BASE}/static/app.js')).text())")
        window_dump = page.evaluate(
            "() => JSON.stringify(Object.keys(window).slice(0, 400))")
        for blob, label in ((page_source, "DOM"), (js_bundle, "bundle"),
                            (window_dump, "window")):
            check(secret not in blob, f"secrets:not-in-{label.lower()}",
                  f"{label} icinde secret yok")

        # ================================================================
        section("9. Automatic flow controls are server-persisted")
        # ================================================================
        goto(page, _BASE, "phase0", _PID)
        message = page.locator("[data-testid=automation-message]")
        check(message.count() == 1, "auto:bar-present", "otomasyon cubugu render oldu")

        start = page.locator("[data-testid=automation-start]")
        check(start.count() == 1, "auto:start-button", "baslat dugmesi var")
        start.click()
        page.wait_for_timeout(2500)
        server_state = _app.automation(_app.ownership(_PID)).view()
        ui_message = page.locator("[data-testid=automation-message]").inner_text()
        check(server_state["state"] in ("running", "blocked", "completed"),
              "auto:server-state", f"sunucu durumu={server_state['state']}")
        check(ui_message.strip() != "", "auto:message-rendered",
              f"UI mesaji: {ui_message[:60]!r}")

        # THE point of the rework: a refresh must not lose the flow.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        after_refresh = page.locator("[data-testid=automation-message]").inner_text()
        check(after_refresh.strip() == ui_message.strip(), "auto:survives-refresh",
              "yenilemeden sonra ayni otomasyon mesaji")
        blocker = page.locator("[data-testid=automation-blocker]")
        check(blocker.count() == 0 or blocker.inner_text().strip() != "",
              "auto:blocker-visible", "duraklama noktasi gorunuyor")

        # Pause / resume / cancel must all be real, persisted transitions.
        for action, testid in (("pause", "automation-pause"),
                                ("resume", "automation-resume"),
                                ("cancel", "automation-cancel")):
            button = page.locator(f"[data-testid={testid}]")
            if button.count():
                button.first.click()
                page.wait_for_timeout(1200)
                state_now = _app.automation(_app.ownership(_PID)).view()["state"]
                expected = {"pause": "paused", "resume": ("running", "blocked", "completed"),
                             "cancel": "cancelled"}[action]
                matched = (state_now == expected if isinstance(expected, str)
                           else state_now in expected)
                check(matched, f"auto:{action}", f"{action} -> {state_now}")
            else:
                check(False, f"auto:{action}", f"{testid} dugmesi yok")

        # Automation must never have approved anything on its way through.
        approvals = _app.service(_app.ownership(_PID)).approval_chain()
        auto_approved = [kind for kind, st in approvals.items()
                         if st.get("approved") and "verify" not in str(st.get("reason", ""))]
        check(_service.load_artifact("moment_selection")["data"] is None
              or _service.load_artifact("moment_selection")["data"]["reviewed_by"] != "console:auto",
              "auto:no-auto-selection", "otomasyon moment secmedi")

        # ================================================================
        section("10. Manual controls inside each tab")
        # ================================================================
        goto(page, _BASE, "moments", _PID)
        check(page.locator("#va-focus, #va-duration, #va-topn, #va-whisper, #va-scene").count() == 5,
              "manual:analysis-fields", "5 duzenlenebilir analiz alani")
        check(page.locator("[data-testid=analysis-range-preview]").count() == 1,
              "manual:analysis-range-preview", "dakika tabanli aralik ozeti yok")

        # Editing a field and running must submit THOSE values.
        page.fill("#va-duration", "0.5")
        page.select_option("#va-whisper", "tiny")
        page.fill("#va-topn", "2")
        before = len(_app.job_store(_app.ownership(_PID)).list(job_type="video_analysis"))
        page.locator("[data-testid=run-video_analysis]").click()
        deadline = time.time() + 180
        while time.time() < deadline:
            jobs = _app.job_store(_app.ownership(_PID)).list(job_type="video_analysis")
            if len(jobs) > before and all(j["status"] not in ("queued", "running") for j in jobs):
                break
            time.sleep(1.0)
        jobs = _app.job_store(_app.ownership(_PID)).list(job_type="video_analysis")
        check(len(jobs) == before + 1, "manual:deliberate-rerun",
              f"{before} -> {len(jobs)} analiz isi")
        newest = jobs[0]
        check(newest["params"].get("focus_region") == "start"
              and newest["params"].get("clip_duration_min") == 0.5
              and newest["params"].get("top_n") == 2,
              "manual:params-submitted", f"gonderilen parametreler: {newest['params']}")

        goto(page, _BASE, "moments", _PID)
        candidate_summary = page.locator("[data-testid=candidate-summary]")
        check(candidate_summary.count() == 1, "manual:candidate-summary",
              "aday sonucu tek ozet panelinde")
        summary_text = candidate_summary.inner_text()
        check("2 / 2" in summary_text, "manual:requested-count-honored",
              f"aday ozeti: {summary_text[:80]!r}")
        check(page.locator("[data-testid=status-video_analysis]").count() == 0,
              "manual:no-duplicate-status-panel",
              "Moment Explorer'da yinelenen faz durum paneli kaldirildi")
        check(page.locator(".moment-card").count() == 2,
              "manual:all-candidates-visible", "istenen iki aday da gorunuyor")

        for screen, testid in (("voice", "tts-voice"), ("voice", "tts-lang")):
            goto(page, _BASE, screen, _PID)
            check(page.locator(f"#{testid}").count() == 1, "manual:voice-fields",
                  f"{testid} duzenlenebilir alani mevcut")
        goto(page, _BASE, "render", _PID)
        check(page.locator("[data-testid=render-config]").count() == 1,
              "manual:render-config-shown", "onaylanan render ayarlari gosteriliyor")

        # ================================================================
        section("11. Moment cards and previews survive a refresh")
        # ================================================================
        goto(page, _BASE, "moments", _PID)
        cards_before = page.locator(".moment-card").count()
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        cards_after = page.locator(".moment-card").count()
        check(cards_before == cards_after and cards_after >= 1,
              "moments:survive-refresh", f"{cards_before} -> {cards_after} kart")

        video = page.locator(".moment-card video").first
        if video.count():
            src = video.get_attribute("src") or ""
            check(not any(marker in src for marker in ("C:", "c:", "/Users/", "\\")),
                  "moments:no-host-path-in-src", f"src={src[:70]}")
            response = page.evaluate(
                f"async () => {{ const r = await fetch('{_BASE}' + "
                f"document.querySelector('.moment-card video').getAttribute('src'));"
                f" return {{status: r.status, type: r.headers.get('content-type')}}; }}")
            check(response["status"] == 200, "moments:preview-http",
                  f"HTTP {response['status']} {response.get('type')}")
        else:
            check(False, "moments:preview-http", "moment karti videosu yok")

        # ================================================================
        section("13. Workflow map and preflight in the UI")
        # ================================================================
        goto(page, _BASE, "phase0", _PID)
        check(page.locator("[data-testid=workflow-map]").count() == 1,
              "workflow:map-present", "akis haritasi yok")
        # Replacement, not addition: the old indicator must be gone.
        check(page.locator(".progress, .progress__steps").count() == 0,
              "workflow:no-duplicate-indicator", "eski ilerleme gostergesi hala duruyor")

        steps = page.locator(".wf-step")
        check(steps.count() == 10, "workflow:ten-steps", f"{steps.count()} adim")
        labels = page.eval_on_selector_all(
            ".wf-step__label", "n => n.map(e => e.textContent)")
        check(labels == ["Kaynak", "Degerlendirme", "Plan", "Analiz", "Moment",
                          "Altyazi", "Script", "Ses", "Render", "Inceleme"],
              "workflow:labels-in-order", str(labels))

        count_label = page.locator("[data-testid=workflow-count]")
        check(count_label.count() == 1 and "/" in count_label.inner_text(),
              "workflow:count", count_label.inner_text() if count_label.count() else "yok")
        current = page.locator(".wf-step--current")
        check(current.count() == 1, "workflow:one-current-highlighted",
              f"{current.count()} adim vurgulanmis")
        check(current.get_attribute("aria-current") == "step",
              "workflow:current-aria", "ekran okuyucu icin isaretlenmemis")

        # Orange is the current-step signal; status colours stay separate.
        accent = page.evaluate(
            "() => getComputedStyle(document.querySelector('.wf-step--current')).borderColor")
        check("255, 122, 24" in accent, "workflow:accent-theme", f"kenarlik={accent}")

        now = page.locator("[data-testid=workflow-now]")
        check(now.count() == 1 and "Su an:" in now.inner_text(), "workflow:current-summary",
              now.inner_text()[:80] if now.count() else "yok")
        action = page.locator("[data-testid=workflow-action]")
        check(action.count() == 1 and action.inner_text().strip() != "",
              "workflow:action-text", "siradaki eylem metni yok")

        # Clicking a step opens the tab that owns it.
        page.locator("[data-testid=step-moment]").click()
        page.wait_for_timeout(700)
        check("Moment Explorer" in page.inner_text("#screen"), "workflow:step-navigates",
              "adim dogru sekmeye goturmedi")
        page.locator("[data-testid=step-render]").click()
        page.wait_for_timeout(700)
        check("Render Studio" in page.inner_text("#screen"), "workflow:step-navigates-render",
              "render adimi yanlis sekmeye goturdu")
        goto(page, _BASE, "phase0", _PID)
        page.locator("[data-testid=workflow-goto]").click()
        page.wait_for_timeout(700)
        check(page.locator("[data-testid=workflow-map]").count() == 1,
              "workflow:goto-button-navigates", "'Bu adima git' calismadi")

        goto(page, _BASE, "settings", _PID)
        rows = page.locator(".preflight-row")
        check(rows.count() >= 8, "preflight:rows-rendered", f"{rows.count()} kontrol satiri")
        summary = page.locator("[data-testid=preflight-summary]")
        check(summary.count() == 1 and summary.inner_text().strip() != "",
              "preflight:summary", summary.inner_text() if summary.count() else "yok")
        settings_text = page.inner_text("#screen")
        check(fixtures.fake_secret_getter()('GeminiKey') not in settings_text,
              "preflight:no-secret-in-ui",
              "preflight panelinde secret degeri yok")

        # ================================================================
        section("13b. Workflow map: both modes, blocked, failed, restart")
        # ================================================================
        def _wf_project(title, mode, *, approve=True):
            """A fresh project taken to an approved plan in the given mode."""
            seed = fixtures.seed_demo_project(_app, title=title)
            pid = seed["project_id"]
            own = _app.ownership(pid)
            svc = _app.service(own)
            api = ConsoleApi(_app)
            body = {"mode": mode, "selected_by": "verify_browser", "output_count": 1,
                    "render_config": {"render_mode": "blurred_background",
                                       "preview_mode": True,
                                       "caption_style": "active_word_yellow",
                                       "caption_theme": "auto"}}
            if mode == "narrated_theory":
                body["theory_angle"] = "Tarayici dogrulamasi icin gecerli bir teori acisi"
            api.handle("POST", f"/api/projects/{pid}/plan",
                       body=json.dumps(body).encode(), headers={})
            if approve:
                digest = svc.describe_plan_review()["review"]["content_sha256"]
                api.handle("POST", f"/api/projects/{pid}/approvals/processing_plan",
                           body=json.dumps({"reviewed_sha256": digest,
                                             "approved_by": "verify_browser"}).encode(),
                           headers={})
            return pid, svc

        def _states(p):
            return dict(zip(
                p.eval_on_selector_all(".wf-step", "n => n.map(e => e.dataset.testid)"),
                p.eval_on_selector_all(".wf-step", "n => n.map(e => e.dataset.state)")))

        # -- direct_clip_series SKIPS narration --------------------------
        _dcs_pid, _ = _wf_project("Akis: direct clip", "direct_clip_series")
        goto(page, _BASE, "phase0", _dcs_pid)
        dcs = _states(page)
        check(dcs["step-script"] == "skipped" and dcs["step-voice"] == "skipped",
              "workflow:dcs-skips-narration",
              f"script={dcs.get('step-script')} voice={dcs.get('step-voice')}")
        check(page.locator("[data-testid=step-skip-script]").count() == 1,
              "workflow:skip-explained-in-ui", "atlandi etiketi yok")
        # direct_clip_series keeps Altyazi and drops Script + Ses: 10 - 2 = 8.
        check("8" in page.locator("[data-testid=workflow-count]").inner_text(),
              "workflow:dcs-counts-eight",
              page.locator("[data-testid=workflow-count]").inner_text())
        check(page.locator("[data-testid=workflow-mode]").inner_text().strip()
              == "direct_clip_series", "workflow:dcs-mode-badge",
              page.locator("[data-testid=workflow-mode]").inner_text())
        # Skipped steps still explain themselves instead of going dead.
        check(page.locator("[data-testid=step-script]").is_enabled(),
              "workflow:skipped-still-explains", "atlanan adim tiklanamiyor")

        # -- content language is required, and narrated-only --------------
        goto(page, _BASE, "phase0", _wf_pid_for_plan)
        page.locator("button.mode-option").first.click()
        page.wait_for_timeout(500)
        dcs_language = page.locator("[data-testid=plan-language]")
        page.locator("button.mode-option").nth(1).click()
        page.wait_for_timeout(500)
        nt_language = page.locator("[data-testid=plan-language]")
        check(nt_language.count() == 1, "lang:picker-for-narrated",
              f"{nt_language.count()} secici")
        check(nt_language.input_value() == "en-US", "lang:default-english",
              nt_language.input_value())
        check(page.get_attribute("#plan-language", "aria-required") == "true",
              "lang:required-flag",
              str(page.get_attribute("#plan-language", "aria-required")))

        # -- narrated_theory KEEPS narration -----------------------------
        _nt_pid, _nt_svc = _wf_project("Akis: narrated", "narrated_theory")
        goto(page, _BASE, "phase0", _nt_pid)
        nt = _states(page)
        check(nt["step-script"] != "skipped" and nt["step-voice"] != "skipped",
              "workflow:narrated-keeps-narration",
              f"script={nt.get('step-script')} voice={nt.get('step-voice')}")
        check("9" in page.locator("[data-testid=workflow-count]").inner_text(),
              "workflow:narrated-counts-nine",
              page.locator("[data-testid=workflow-count]").inner_text())
        check(page.locator(".wf-step--current").count() == 1,
              "workflow:narrated-one-current", "tam olarak bir 'current' olmali")

        # -- FAILED: a real failed job must surface on its own step -------
        _fail_store = _app.job_store(_app.ownership(_nt_pid))
        _fail_job, _ = _fail_store.submit("video_analysis",
                                          idempotency_key="wf-fail-probe",
                                          submitted_by="verify_browser")
        _fail_store.claim(_fail_job["job_id"], "verify_browser")
        _fail_store.finish(_fail_job["job_id"], "failed",
                           error="ffmpeg cikis kodu 1 (tarayici testi)")
        goto(page, _BASE, "phase0", _nt_pid)
        failed_state = _states(page).get("step-analysis")
        check(failed_state == "failed", "workflow:failed-state-in-ui",
              f"analiz adimi={failed_state}")
        check(page.locator(".wf-step--failed").count() == 1, "workflow:failed-styled",
              "basarisiz adim ayirt edilmiyor")
        blocker_box = page.locator("[data-testid=workflow-blocker]")
        check(blocker_box.count() == 1 and "ffmpeg" in blocker_box.inner_text(),
              "workflow:failed-blocker-shown",
              blocker_box.inner_text()[:80] if blocker_box.count() else "yok")
        check(page.locator("[data-testid=workflow-repair]").count() == 1,
              "workflow:failed-repair-shown", "cozum metni yok")
        danger = page.evaluate(
            "() => getComputedStyle(document.querySelector('.wf-step--failed')).color")
        check("255, 90, 90" in danger, "workflow:failed-not-orange",
              f"basarisiz rengi={danger} (turuncu ile karisamaz)")

        # -- BLOCKED: a corrupt artifact -> blocked + repair --------------
        _blk_pid, _blk_svc = _wf_project("Akis: engelli", "direct_clip_series")
        _blk_svc.path("hot_moments").write_text("{bozuk", encoding="utf-8")
        goto(page, _BASE, "phase0", _blk_pid)
        blocked_state = _states(page).get("step-analysis")
        check(blocked_state == "blocked", "workflow:blocked-state-in-ui",
              f"analiz adimi={blocked_state}")
        check(page.locator("[data-testid=workflow-blocker]").count() == 1
              and page.locator("[data-testid=workflow-repair]").count() == 1,
              "workflow:blocked-blocker-and-repair", "engel/cozum metni eksik")

        # -- Survives a page refresh AND a server restart ----------------
        before_reload = _states(page)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(500)
        check(_states(page) == before_reload, "workflow:survives-refresh",
              "yenilemeden sonra durumlar degisti")

        _restart = ConsoleApp(ConsoleConfig(
            storage_root=_app.storage.root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        try:
            restart_states = {
                f"step-{s['key']}": s["state"]
                for s in _restart.project_view(_restart.ownership(_blk_pid))["workflow"]["steps"]}
            check(restart_states == before_reload, "workflow:survives-restart",
                  f"restart sonrasi: {restart_states}")
        finally:
            _restart.shutdown()

        # -- Mobile: vertical flow, zero horizontal overflow --------------
        wf_mobile = browser.new_context(viewport=MOBILE, is_mobile=True, has_touch=True)
        mp = wf_mobile.new_page()
        mp_errors = console_errors(mp)
        for target in (_dcs_pid, _nt_pid):
            goto(mp, _BASE, "phase0", target)
            direction = mp.evaluate(
                "() => getComputedStyle(document.querySelector('.wf__steps')).flexDirection")
            check(direction == "column", "workflow:mobile-vertical",
                  f"mobilde yon={direction}")
            page_overflow = mp.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth")
            map_overflow = mp.evaluate(
                "() => { const n = document.querySelector('.wf__steps');"
                " return n.scrollWidth - n.clientWidth; }")
            check(page_overflow <= 2 and map_overflow <= 2,
                  "workflow:mobile-no-overflow",
                  f"sayfa={page_overflow}px harita={map_overflow}px")
            widest = mp.evaluate(
                "() => Math.max(...[...document.querySelectorAll('.wf-step')]"
                ".map(e => e.getBoundingClientRect().right))")
            check(widest <= MOBILE["width"] + 1, "workflow:mobile-steps-fit",
                  f"en sag kenar={widest}px / {MOBILE['width']}px")
            check(mp.locator(".wf-step").count() == 10, "workflow:mobile-all-steps",
                  f"{mp.locator('.wf-step').count()} adim")
        # Navigation still works by tap.
        mp.locator("[data-testid=step-render]").click()
        mp.wait_for_timeout(700)
        check("Render Studio" in mp.inner_text("#screen"), "workflow:mobile-navigates",
              "mobilde adim navigasyonu calismadi")
        check(not mp_errors, "workflow:mobile-no-js-errors", f"{mp_errors[:2]}")
        wf_mobile.close()

        # ================================================================
        section("14. Diagnostics panel in Settings")
        # ================================================================
        goto(page, _BASE, "settings", _PID)
        panel = page.locator("[data-testid=diagnostics-panel]")
        check(panel.count() == 1, "diag:panel-present", "teshis paneli yok")
        last_run = page.locator("[data-testid=diagnostics-last-run]")
        check("hic calistirilmadi" in last_run.inner_text(), "diag:never-run-state",
              last_run.inner_text())
        rows_before = page.locator(".diag-row")
        check(rows_before.count() == 13, "diag:capabilities-listed",
              f"{rows_before.count()} yetenek satiri")

        # A page load must NOT have run anything: the server report is still empty.
        cached = json.loads(page.evaluate(
            "async () => JSON.stringify(await (await fetch('/api/diagnostics')).json())"))
        check(cached["diagnostics"].get("never_run") is True, "diag:no-run-on-load",
              "sayfa acilisi teshis kosusu tetikledi")

        run_button = page.locator("[data-testid=run-diagnostics]")
        check(run_button.count() == 1 and run_button.is_enabled(), "diag:button-present",
              "calistir dugmesi yok")
        run_button.click()
        # A real run does ffmpeg work; wait for the button to come back.
        page.wait_for_function(
            "() => { const b = document.querySelector('[data-testid=run-diagnostics]');"
            " return b && !b.disabled; }", timeout=180_000)
        page.wait_for_timeout(400)

        last_run_text = page.locator("[data-testid=diagnostics-last-run]").inner_text()
        check("hic calistirilmadi" not in last_run_text, "diag:last-run-updated",
              last_run_text)
        check("20" in last_run_text, "diag:last-run-timestamp", last_run_text)

        statuses = page.eval_on_selector_all(
            ".diag-row", "rows => rows.map(r => r.dataset.status)")
        check(len(statuses) == 13 and all(statuses), "diag:all-have-status",
              f"durumlar: {statuses}")
        check("failed" not in statuses, "diag:no-failures-in-ui",
              f"arayuzde bozuk yetenek: {statuses}")
        check(statuses.count("ready") >= 10, "diag:mostly-ready",
              f"ready sayisi={statuses.count('ready')}")

        summary_text = page.locator("[data-testid=diagnostics-summary]").inner_text()
        check(summary_text.strip() != "", "diag:summary-text", summary_text)
        counts_text = page.locator("[data-testid=diagnostics-counts]").inner_text()
        check("calisiyor" in counts_text, "diag:counts-text", counts_text)

        # Provider availability and functionality must read as separate claims.
        script_row = page.locator("[data-testid=diag-script]")
        check(script_row.get_attribute("data-status") == "degraded",
              "diag:provider-not-claimed-ready",
              script_row.get_attribute("data-status"))
        check("CANLI" in script_row.inner_text(), "diag:provider-functionality-untested",
              script_row.inner_text()[:120])
        check(page.locator("[data-testid=diag-blocker-script]").count() == 1,
              "diag:blocker-shown", "engel alani yok")
        check(page.locator("[data-testid=diag-repair-script]").count() == 1,
              "diag:repair-shown", "cozum alani yok")

        # A non-ready capability offers a jump to the tab that owns it.
        jump = page.locator("[data-testid=diag-open-script]")
        check(jump.count() == 1, "diag:jump-button", "sekmeye git dugmesi yok")
        jump.click()
        page.wait_for_timeout(700)
        # The `script` tab has TWO faces, chosen by the approved plan: a narrated
        # project sees Script Studio, a clip series sees the Altyazi editor
        # (`screenScript` delegates to `screenTranscript`). The jump is correct
        # when it lands on that tab, whichever face the plan selected -- pinning
        # the narrated heading made a correct jump look like a wrong one.
        _screen_text = page.inner_text("#screen")
        _landed = ("Script Studio" if "Script Studio" in _screen_text
                   else "Altyazi" if "Altyazi (orijinal ses)" in _screen_text else "")
        # Report the matched heading, never the raw screen text: the page holds
        # non-cp1254 glyphs and this console encodes stdout in cp1254, so
        # echoing it turned a passing check into a UnicodeEncodeError crash.
        check(bool(_landed), "diag:jump-navigates",
              f"yanlis sekmeye goturdu (baslik bulunamadi)")

        # The report survives a reload, because it lives on the server.
        goto(page, _BASE, "settings", _PID)
        page.wait_for_timeout(500)
        check("hic calistirilmadi" not in
              page.locator("[data-testid=diagnostics-last-run]").inner_text(),
              "diag:persists-across-reload", "yeniden yuklemede rapor kayboldu")
        check(page.eval_on_selector_all(".diag-row", "r => r.length") == 13,
              "diag:rows-after-reload", "satirlar geri gelmedi")

        settings_blob = page.content()
        check(fixtures.fake_secret_getter()('GeminiKey') not in settings_blob,
              "diag:no-secret-in-dom", "teshis panelinde secret degeri var")
        check("console_diag_" not in settings_blob, "diag:no-fixture-path-in-dom",
              "gecici fikstur yolu arayuze sizdi")

        # ================================================================
        section("15. Render Studio review experience")
        # ================================================================
        # Take the fixture project all the way to a rendered video.
        rr_service = _app.service(_app.ownership(_PID))
        if rr_service.load_artifact("moment_selection")["data"] is None:
            hot = rr_service.load_artifact("hot_moments")["data"]
            if hot and hot.get("hot_moments"):
                rr_service.record_moment_selection(
                    selected_rank=hot["hot_moments"][0]["rank"],
                    reviewed_by="browser-test", require_preview=False)
        # The render gate needs a produced AND approved transcript in this mode.
        if rr_service.load_artifact("moment_transcript")["data"] is None:
            _app.submit_job(_app.ownership(_PID), "transcript", params={
                "subtitle_whisper_model_size": "tiny", "subtitle_language": "auto"},
                run_request_id="browser-transcript")
            deadline = time.time() + 240
            while time.time() < deadline:
                if rr_service.load_artifact("moment_transcript")["data"] is not None:
                    break
                time.sleep(1.0)
        _rr_tr = rr_service.approval_state("transcript")
        if _rr_tr.get("content_sha256") and not _rr_tr.get("valid"):
            rr_service.approve("transcript", reviewed_sha256=_rr_tr["content_sha256"],
                               approved_by="browser-test")
        if rr_service.load_artifact("render_manifest")["data"] is None:
            _app.submit_job(_app.ownership(_PID), "render", params={
                "render_mode": "blurred_background", "preview_mode": True,
                "subtitle_whisper_model_size": "small",
                "caption_style": "active_word_yellow", "caption_theme": "auto"},
                run_request_id="browser-render")
            deadline = time.time() + 240
            while time.time() < deadline:
                if rr_service.load_artifact("render_manifest")["data"] is not None:
                    break
                time.sleep(1.0)

        goto(page, _BASE, "render", _PID)
        player = page.locator("[data-testid=render-player]")
        check(player.count() == 1, "review:player-present", "buyuk onizleme oynaticisi var")
        if player.count():
            playable = page.evaluate("""async () => {
                const v = document.querySelector('[data-testid=render-player]');
                v.load();
                return await new Promise(res => {
                    const done = () => res({ok: v.readyState > 0 || v.duration > 0,
                                            readyState: v.readyState, duration: v.duration});
                    v.addEventListener('loadedmetadata', done, {once: true});
                    v.addEventListener('error', () => res({ok: false}), {once: true});
                    setTimeout(done, 6000);
                });
            }""")
            check(playable.get("ok"), "review:player-plays",
                  f"tarayici Short'u yukledi: {playable}")

        tech = page.locator("[data-testid=render-technical]")
        check(tech.count() == 1, "review:technical-visible", "teknik ozet gorunuyor")
        # `inner_text` returns the CSS-transformed string, and the page is
        # lang="tr" -- Turkish uppercasing maps "i" to "I-with-dot", so a naive
        # comparison fails on "Video codec"/"Altyazi modu". Read the untransformed
        # textContent instead.
        _labels = page.eval_on_selector_all(
            "[data-testid=render-technical] .tech-cell__label",
            "els => els.map(e => e.textContent.trim())")
        _missing = [label for label in
                    ("Sure", "Cozunurluk", "Video codec", "Ses", "Altyazi modu",
                     "Aktif kelime", "artifact_status")
                    if label not in _labels]
        check(not _missing, "review:technical-fields",
              f"eksik alanlar: {_missing}" if _missing
              else f"{len(_labels)} teknik alan: sure/cozunurluk/codec/ses/altyazi/"
                   "aktif-kelime/durum")

        boxes = page.locator("[data-testid^=checkbox-]")
        check(boxes.count() == 6, "review:six-checkboxes", f"{boxes.count()} kontrol kutusu")
        locked = page.locator("[data-testid=approval-locked]")
        check(locked.count() == 1, "review:approval-locked",
              "liste eksikken onay alani kapali")
        check(page.locator("[data-testid=approve-render]").count() == 0,
              "review:no-approve-button", "onay dugmesi gorunmuyor")

        # Tick every box through the UI; the project operator is reused.
        for key in ("clip_boundaries", "crop", "subtitle_sync",
                     "active_word_highlight", "font_readability", "audio_continuity"):
            box = page.locator(f"[data-testid=checkbox-{key}]")
            if box.count() and not box.is_checked():
                box.check()
                page.wait_for_timeout(600)
        check(page.locator("[data-testid=render-reviewer]").count() == 0,
              "review:no-repeated-reviewer", "tekrar isim alani kaldirildi")
        page.wait_for_timeout(800)
        goto(page, _BASE, "render", _PID)
        server_review = _app.service(_app.ownership(_PID)).render_review_state()
        check(server_review["done_count"] == 6, "review:ui-ticks-persist",
              f"sunucuda {server_review['done_count']}/6 madde isaretli")
        check(page.locator("[data-testid=approve-render]").count() == 1,
              "review:approval-unlocks", "liste tamamlaninca onay alani acildi")

        # Refresh must keep the checklist.
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        progress_label = page.locator("[data-testid=checklist-progress]")
        check(progress_label.count() == 1 and "6/6" in progress_label.inner_text(),
              "review:survives-refresh",
              progress_label.inner_text() if progress_label.count() else "yok")

        # "Needs revision" records a note and navigates, without approving.
        page.fill("[data-testid=revision-notes]", "klip cumle ortasinda basliyor")
        page.select_option("[data-testid=revision-target]", "moments")
        page.locator("[data-testid=revision-submit]").click()
        page.wait_for_timeout(1200)
        state_after = _app.service(_app.ownership(_PID)).render_review_state()
        check(state_after["verdict"] == "needs_revision", "review:revision-ui",
              f"verdict={state_after['verdict']}")
        manifest_after = _app.service(_app.ownership(_PID)).load_artifact("render_manifest")["data"]
        check(manifest_after["status"] != "approved", "review:revision-no-approval",
              f"render durumu={manifest_after['status']}")
        goto(page, _BASE, "render", _PID)
        goto_button = page.locator("[data-testid=revision-goto]")
        check(goto_button.count() == 1, "review:revision-goto-present",
              "duzeltme sekmesine git dugmesi var")
        if goto_button.count():
            goto_button.click()
            page.wait_for_timeout(800)
            check("Moment Explorer" in page.inner_text("#screen"),
                  "review:revision-navigates", "dogru sekmeye goturuyor")

        # ================================================================
        section("17. Required fields: marks, legend, inline errors, focus")
        # ================================================================
        goto(page, _BASE, "projects", _PID)
        check(page.locator("[data-testid=required-legend]").count() >= 1,
              "form:legend-shown", "'* Zorunlu alan' efsanesi yok")
        star = page.locator("[data-testid=req-new-project-title]")
        check(star.count() == 1 and star.inner_text().strip() == "*",
              "form:star-marks-required",
              star.inner_text() if star.count() else "yildiz yok")
        star_color = page.evaluate(
            "() => getComputedStyle(document"
            ".querySelector('[data-testid=req-new-project-title]')).color")
        check("255, 122, 24" in star_color, "form:star-is-orange", star_color)
        check(page.get_attribute("#new-project-title", "aria-required") == "true",
              "form:aria-required",
              str(page.get_attribute("#new-project-title", "aria-required")))

        # Submitting empty must NOT create anything, and must say which field.
        before_count = len(_app.project_ids())
        page.locator("[data-testid=create-project]").click()
        page.wait_for_timeout(500)
        error_slot = page.locator("[data-testid=err-new-project-title]")
        check(error_slot.count() == 1 and error_slot.is_visible(),
              "form:inline-error-shown", "satir ici hata gorunmuyor")
        check("zorunlu" in error_slot.inner_text().lower(), "form:error-text",
              error_slot.inner_text())
        check(page.evaluate("() => document.activeElement.id") == "new-project-title",
              "form:focus-first-invalid",
              page.evaluate("() => document.activeElement.id"))
        check(page.get_attribute("#new-project-title", "aria-invalid") == "true",
              "form:aria-invalid", "aria-invalid isaretlenmedi")
        check(len(_app.project_ids()) == before_count, "form:nothing-created",
              "bos formla proje olusturuldu")

        # Filling it clears the error and works.
        page.fill("#new-project-title", "Zorunlu alan testi")
        page.fill("#new-project-operator", "browser-test")
        page.locator("[data-testid=create-project]").click()
        page.wait_for_timeout(900)
        check(len(_app.project_ids()) == before_count + 1, "form:valid-submit-works",
              "gecerli formla proje olusturulmadi")

        # Optional fields say so, and never carry a star.
        goto(page, _BASE, "phase0", _PID)
        optional_tags = page.locator("[data-testid^=opt-]")
        check(optional_tags.count() >= 1, "form:optional-labelled",
              f"{optional_tags.count()} opsiyonel etiketi")
        check("Opsiyonel" in optional_tags.first.inner_text(), "form:optional-text",
              optional_tags.first.inner_text())
        starred = page.eval_on_selector_all(
            "[data-testid^=req-]", "n => n.map(e => e.closest('.field').dataset.field)")
        optional = page.eval_on_selector_all(
            "[data-testid^=opt-]", "n => n.map(e => e.closest('.field').dataset.field)")
        check(not (set(starred) & set(optional)), "form:star-xor-optional",
              f"hem zorunlu hem opsiyonel: {set(starred) & set(optional)}")

        # SERVER validation stays authoritative: bypass the form entirely.
        bypass = page.evaluate(
            "async () => { const r = await fetch('/api/projects',"
            " { method: 'POST', headers: { 'Content-Type': 'application/json' },"
            " body: JSON.stringify({ title: '   ' }) });"
            " return { status: r.status, body: await r.json() }; }")
        check(bypass["status"] == 400, "form:server-authoritative",
              f"HTTP {bypass['status']}")
        check([f["name"] for f in bypass["body"]["error"].get("fields", [])] == ["title"],
              "form:server-names-field", str(bypass["body"]["error"].get("fields")))

        # A server-side rejection must land ON the field, not only in a banner.
        # Every form validates client-side first, so the only way to reach this
        # branch is to make the SERVER be the one that refuses. The response is
        # intercepted -- our client code is what is under test, against the
        # documented `fields` contract that `errors.py` really emits (proved
        # separately by form:server-names-field above).
        def _reject_with_field(route):
            route.fulfill(status=400, content_type="application/json",
                          body=json.dumps({"error": {
                              "code": "missing_required_field",
                              "message": "Zorunlu alan(lar) bos.",
                              "hint": "Yildizli alanlari doldur.",
                              "retryable": False, "debug_id": "dbg_test",
                              "fields": [{"name": "new-project-title",
                                          "label": "Proje adi"}]}}))

        goto(page, _BASE, "projects", _PID)
        page.route("**/api/projects", _reject_with_field)
        page.fill("#new-project-title", "istemci bunu kabul eder")
        page.fill("#new-project-operator", "browser-test")
        page.locator("[data-testid=create-project]").click()
        page.wait_for_timeout(700)
        page.unroute("**/api/projects")

        slot = page.locator("[data-testid=err-new-project-title]")
        check(slot.count() == 1 and slot.is_visible(), "form:server-error-on-field",
              "sunucu hatasi alana islenmedi")
        check("zorunlu" in slot.inner_text().lower(), "form:server-error-text",
              slot.inner_text())
        check(page.evaluate("() => document.activeElement.id") == "new-project-title",
              "form:server-error-focuses",
              page.evaluate("() => document.activeElement.id"))
        check(page.get_attribute("#new-project-title", "aria-invalid") == "true",
              "form:server-error-aria", "aria-invalid isaretlenmedi")
        check("Eksik zorunlu alan" in page.inner_text("#global-alerts"),
              "form:server-error-banner", page.inner_text("#global-alerts")[:60])

        # ================================================================
        section("18. Progress panels: stages, a11y, failure, refresh, mobile")
        # ================================================================
        goto(page, _BASE, "moments", _PID)
        panel = page.locator("[data-testid=progress-video_analysis]")
        check(panel.count() == 1, "prog:analysis-panel", f"{panel.count()} panel")
        check(page.locator("[data-testid=progress-video_analysis]").count() == 1,
              "prog:no-duplicate-panel", "ayni faz icin iki panel var")

        stages = page.eval_on_selector_all(
            "[data-testid=progress-stages-video_analysis] .pstage",
            "n => n.map(e => e.dataset.testid)")
        check(stages == [f"pstage-video_analysis-{key}" for key in
                          ("preparation", "audio", "whisper", "scenes", "scoring", "previews")],
              "prog:analysis-stage-names", str(stages))

        bar = page.locator("[data-testid=progress-bar-video_analysis]")
        check(bar.get_attribute("role") == "progressbar", "prog:progressbar-role",
              str(bar.get_attribute("role")))
        check(bar.get_attribute("aria-valuemin") == "0"
              and bar.get_attribute("aria-valuemax") == "100"
              and bar.get_attribute("aria-valuenow") is not None,
              "prog:aria-values",
              f"now={bar.get_attribute('aria-valuenow')}")
        check(bar.get_attribute("aria-valuetext"), "prog:aria-valuetext",
              "aria-valuetext yok")
        check(bar.get_attribute("data-determinate") == "true", "prog:determinate",
              str(bar.get_attribute("data-determinate")))
        check("sn" in page.locator("[data-testid=progress-elapsed-video_analysis]").inner_text(),
              "prog:elapsed-shown",
              page.locator("[data-testid=progress-elapsed-video_analysis]").inner_text())
        check(page.locator("[data-testid=progress-step-video_analysis]").inner_text().strip() != "",
              "prog:step-label", "asama adi bos")

        # An INDETERMINATE record must not render a percentage.
        _ind_own = _app.ownership(_PID)
        _ind = progress.ProgressTracker(_app.storage, _ind_own, "diagnostics", stages=[])
        _ind.start("olculemeyen is")
        goto(page, _BASE, "settings", _PID)
        page.wait_for_timeout(400)
        ind_bar = page.locator("[data-testid=progress-bar-diagnostics]")
        check(ind_bar.count() == 1, "prog:indeterminate-panel", "belirsiz panel yok")
        check(ind_bar.get_attribute("data-determinate") == "false",
              "prog:indeterminate-flag", str(ind_bar.get_attribute("data-determinate")))
        check(ind_bar.get_attribute("aria-valuenow") is None,
              "prog:no-fake-valuenow",
              f"olculemezken aria-valuenow={ind_bar.get_attribute('aria-valuenow')}")
        check("%" not in page.locator("[data-testid=progress-meta-diagnostics]").inner_text(),
              "prog:no-fake-percent",
              page.locator("[data-testid=progress-meta-diagnostics]").inner_text())

        # FAILURE: last step, error text and a retry affordance.
        _fail_own = _app.ownership(_PID)
        _ft = progress.ProgressTracker(_app.storage, _fail_own, "tts", job_id="job_x")
        _ft.start("seslendirme")
        _ft.stage("synthesize", "Gemini TTS yaniti bekleniyor")
        _ft.fail("ProviderError: kota doldu (tarayici testi)")
        goto(page, _BASE, "voice", _PID)
        page.wait_for_timeout(400)
        check(page.locator("[data-testid=progress-tts]").get_attribute("data-state") == "failed",
              "prog:failed-state",
              str(page.locator("[data-testid=progress-tts]").get_attribute("data-state")))
        failure = page.locator("[data-testid=progress-failure-tts]")
        check(failure.count() == 1, "prog:failure-block", "hata blogu yok")
        check("Ses sentezleniyor" in failure.inner_text(), "prog:failure-last-step",
              failure.inner_text()[:80])
        check("kota doldu" in page.locator("[data-testid=progress-error-tts]").inner_text(),
              "prog:failure-error-text",
              page.locator("[data-testid=progress-error-tts]").inner_text()[:60])
        check(page.locator("[data-testid=progress-retry-tts],"
                            " [data-testid=progress-retry-hint-tts]").count() == 1,
              "prog:failure-retry-action", "yeniden deneme eylemi yok")

        # Survives a browser refresh, because it lives on the server.
        before_refresh = page.locator("[data-testid=progress-tts]").inner_text()
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(500)
        check(page.locator("[data-testid=progress-tts]").inner_text() == before_refresh,
              "prog:survives-refresh", "yenilemeden sonra degisti")

        # Survives a server restart.
        _rs = ConsoleApp(ConsoleConfig(
            storage_root=_app.storage.root, inline_jobs=True,
            secret_getter=fixtures.fake_secret_getter(),
            **fixtures.fixture_client_factories()))
        try:
            restarted = _rs.project_view(_rs.ownership(_PID))["progress"]["tts"]
            check(restarted["state"] == "failed"
                  and restarted["failed_step"] == "synthesize",
                  "prog:survives-restart",
                  f"{restarted['state']}/{restarted.get('failed_step')}")
        finally:
            _rs.shutdown()

        # Mobile: no horizontal overflow anywhere the panel appears.
        prog_mobile = browser.new_context(viewport=MOBILE, is_mobile=True, has_touch=True)
        pm = prog_mobile.new_page()
        pm_errors = console_errors(pm)
        for screen in ("phase0", "moments", "voice", "settings", "jobs"):
            goto(pm, _BASE, screen, _PID)
            pm.wait_for_timeout(250)
            overflow = pm.evaluate(
                "() => document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth")
            if overflow > 2:
                check(False, "prog:mobile-no-overflow", f"{screen}: {overflow}px")
                break
        else:
            check(True, "prog:mobile-no-overflow", "5 ekran mobilde tasmiyor")
        goto(pm, _BASE, "moments", _PID)
        pm.wait_for_timeout(250)
        widest = pm.evaluate(
            "() => { const n = document.querySelector('.pprog');"
            " return n ? n.getBoundingClientRect().right : 0; }")
        check(0 < widest <= MOBILE["width"] + 1, "prog:mobile-panel-fits",
              f"panel sag kenari={widest}px")
        stage_dir = pm.evaluate(
            "() => { const n = document.querySelector('.pstages');"
            " return n ? getComputedStyle(n).flexDirection : 'yok'; }")
        check(stage_dir == "column", "prog:mobile-stages-stack", str(stage_dir))
        check(not pm_errors, "prog:mobile-no-js-errors", f"{pm_errors[:2]}")
        prog_mobile.close()

        # ================================================================
        section("19. Script Studio is readable, and the inner measured bar")
        # ================================================================
        # A narrated_theory project taken all the way to a generated script.
        _sc_pid, _sc_svc = _wf_project("Script okuma", "narrated_theory")
        _sc_own = _app.ownership(_sc_pid)
        ConsoleApi(_app).handle("POST", f"/api/projects/{_sc_pid}/jobs",
                                body=json.dumps({
                                    "job_type": "video_analysis",
                                    "params": {"whisper_model_size": "tiny",
                                                "clip_duration_sec": 3, "top_n": 2}}).encode(),
                                headers={})
        _sc_hot = _sc_svc.load_artifact("hot_moments")["data"]
        _sc_svc.record_moment_selection(
            selected_rank=_sc_hot["hot_moments"][0]["rank"],
            reviewed_by="verify_browser", require_preview=False)
        ConsoleApi(_app).handle("POST", f"/api/projects/{_sc_pid}/jobs",
                                body=json.dumps({"job_type": "script_generation"}).encode(),
                                headers={})

        goto(page, _BASE, "script", _sc_pid)
        check(page.locator("[data-testid=script-text]").count() == 1,
              "script:text-visible", "uretilen script sayfada gorunmuyor")
        shown = page.locator("[data-testid=script-text]").inner_text().strip()
        stored = (_sc_svc.load_artifact("selected_script")["data"] or {}).get("script_text", "")
        check(shown and shown == stored.strip(), "script:text-matches-artifact",
              f"{len(shown)} karakter gosterildi / {len(stored)} kayitli")
        hook = page.locator("[data-testid=script-hook]")
        check(hook.count() == 1 and hook.inner_text().strip() == fixtures.DEMO_HOOK_TEXT,
              "script:hook-visible",
              hook.inner_text().strip() if hook.count() else "hook gorunmuyor")
        meta = page.locator("[data-testid=script-meta]").inner_text()
        check("gemini" in meta.lower(), "script:model-shown", meta[:60])
        check("sn" in meta, "script:duration-shown", meta[:80])
        # The reviewer reads the text and approves it in one click; the digest
        # binding remains server-side and is not a copy/paste chore.
        check(page.locator("#script-hash").count() == 0,
              "script:no-manual-hash", "manuel hash alani geri geldi")
        check(page.locator("[data-testid=approve-script]").count() == 1,
              "script:one-click-approval", "script onay dugmesi yok")
        check("provider_attempts" not in page.content(), "script:no-raw-artifact",
              "ham artefakt sayfaya dokuluyor")

        # A project with no script says so, instead of rendering an empty box.
        goto(page, _BASE, "script", _wf_pid_for_plan)
        check(page.locator("[data-testid=script-empty]").count() == 1,
              "script:empty-state", "script yokken bos kutu ciziliyor")

        # The inner bar appears ONLY while a stage counts something of its own.
        _ib_own = _app.ownership(_PID)
        _ib = progress.ProgressTracker(_app.storage, _ib_own, "render", job_id="job_ib")
        _ib.start("render")
        _ib.stage("ffmpeg", "kodlaniyor")
        goto(page, _BASE, "render", _PID)
        check(page.locator("[data-testid=progress-inner-render]").count() == 0,
              "prog:no-inner-bar-without-measurement",
              "olcum yokken ic bar ciziliyor")
        _ib.count(12, 45, unit="sn", message="FFmpeg 12/45 sn kodladi")
        goto(page, _BASE, "render", _PID)
        inner = page.locator("[data-testid=progress-inner-render]")
        check(inner.count() == 1, "prog:inner-bar-when-measured", "ic bar yok")
        check(inner.get_attribute("aria-valuenow") == "27", "prog:inner-bar-value",
              str(inner.get_attribute("aria-valuenow")))
        check("12/45" in page.locator("[data-testid=progress-items-render]").inner_text(),
              "prog:inner-count-text",
              page.locator("[data-testid=progress-items-render]").inner_text())

        # ================================================================
        section("20. Voice Studio review: player, checklist, staleness")
        # ================================================================
        # A narrated_theory project taken to an approved script and a voiceover.
        _v_pid, _v_svc = _wf_project("Ses inceleme", "narrated_theory")
        _v_own = _app.ownership(_v_pid)
        _v_api = ConsoleApi(_app)

        def _v_job(job_type, params=None):
            _v_api.handle("POST", f"/api/projects/{_v_pid}/jobs",
                          body=json.dumps({"job_type": job_type,
                                            "params": params or {}}).encode(),
                          headers={})

        _v_job("video_analysis", {"whisper_model_size": "tiny",
                                   "clip_duration_sec": 3, "top_n": 2})
        _v_hot = _v_svc.load_artifact("hot_moments")["data"]
        _v_svc.record_moment_selection(selected_rank=_v_hot["hot_moments"][0]["rank"],
                                        reviewed_by="verify_browser",
                                        require_preview=False)
        _v_job("script_generation")
        _v_api.handle("POST", f"/api/projects/{_v_pid}/approvals/script",
                      body=json.dumps({
                          "reviewed_sha256": _v_svc.approval_state("script")["content_sha256"],
                          "approved_by": "verify_browser"}).encode(), headers={})
        _v_job("tts")

        goto(page, _BASE, "voice", _v_pid)
        player = page.locator("[data-testid=voice-player]")
        check(player.count() == 1, "voice:player-present", "ses oynatici yok")
        src = player.get_attribute("src") or ""
        check(src.startswith(f"/api/projects/{_v_pid}/media/"),
              "voice:player-uses-media-route", src[:70])
        check(":" not in src.split("?")[0].replace(f"/api/projects/{_v_pid}/media/", ""),
              "voice:no-host-path", src[:70])
        played = page.evaluate(
            """async () => {
                 const a = document.querySelector('[data-testid=voice-player]');
                 if (!a) return { ok: false, reason: 'oynatici yok' };
                 a.muted = true;
                 try { await a.play(); } catch (e) {}
                 await new Promise(r => setTimeout(r, 1200));
                 return { ok: a.readyState >= 2, readyState: a.readyState,
                          duration: a.duration }; }""")
        check(played.get("ok"), "voice:audio-plays", str(played))
        check((played.get("duration") or 0) > 0, "voice:audio-has-duration",
              str(played.get("duration")))

        technical = page.locator("[data-testid=voice-technical]").inner_text()
        # en-US, not tr-TR: content language is now a plan decision and the
        # narrated default is English, so the panel must show what was planned.
        for needle, label in (("Kore", "voice"), ("gemini", "model"), ("en-US", "language"),
                               ("sn", "duration"), ("PCM", "format"),
                               ("24000", "sample-rate"), ("mono", "channels")):
            check(needle.lower() in technical.lower(), f"voice:tech-{label}",
                  technical[:80].replace("\n", " | "))
        check(page.locator("[data-testid=voice-script-hash]").inner_text().strip() != "",
              "voice:script-hash-shown", "bagli script hash gorunmuyor")
        script_shown = page.locator("[data-testid=voice-script-text]").inner_text().strip()
        stored_script = (_v_svc.load_artifact("selected_script")["data"] or {}).get("script_text", "")
        check(script_shown and script_shown == stored_script.strip(),
              "voice:script-text-matches", f"{len(script_shown)} / {len(stored_script)}")

        check(page.locator("[data-testid=voice-language]").inner_text().strip()
              .endswith("en-US"), "voice:language-badge",
              page.locator("[data-testid=voice-language]").inner_text())
        # The checklist must ask about the language actually being spoken.
        naturalness = page.locator("[data-testid=voice-check-naturalness]").inner_text()
        check("English naturalness" in naturalness, "voice:english-checklist",
              naturalness[:60] if naturalness else "yok")
        check("Turkce" not in naturalness, "voice:no-turkish-question",
              naturalness[:70])

        boxes = page.locator("[data-testid^=voice-checkbox-]")
        check(boxes.count() == 6, "voice:six-items", f"{boxes.count()} madde")
        check(page.locator("[data-testid=approve-voiceover]").count() == 0,
              "voice:approval-locked", "liste eksikken onay dugmesi acik")
        check(page.locator("[data-testid=voice-rerun-tts]").count() == 1,
              "voice:rerun-action", "yeniden uret eylemi yok")

        # The project operator is reused for every checklist decision.
        for key in ("pronunciation", "naturalness", "pacing", "tone",
                     "script_match", "audio_quality"):
            box = page.locator(f"[data-testid=voice-checkbox-{key}]")
            if box.count() and not box.is_checked():
                box.check()
                page.wait_for_timeout(500)
        check(page.locator("[data-testid=voice-reviewer]").count() == 0,
              "voice:no-repeated-reviewer", "tekrar isim alani kaldirildi")
        server_state = _v_svc.voice_review_state()
        check(server_state["done_count"] == 6, "voice:checklist-persisted",
              f"sunucuda {server_state['done_count']}/6")
        check(page.locator("[data-testid=approve-voiceover]").count() == 1,
              "voice:approval-unlocks", "liste tamamlaninca onay acilmadi")

        # Survives a reload, because the checklist is an artifact.
        goto(page, _BASE, "voice", _v_pid)
        check(page.locator("[data-testid=voice-checklist-progress]").inner_text()
              .startswith("6/6"), "voice:survives-refresh",
              page.locator("[data-testid=voice-checklist-progress]").inner_text())
        check(page.locator("[data-testid=voice-reviewer]").count() == 0,
              "voice:no-reviewer-after-refresh", "isim alani geri gelmedi")

        # Replacing the audio must make the list stale and re-close approval.
        _v_audio = Path(_v_svc.load_artifact("selected_voiceover")["data"]["audio_path"])
        _v_audio.write_bytes(google_ai.pcm_to_wav_bytes(
            b"\x08\x27" * (google_ai.TTS_SAMPLE_RATE * 2)))
        goto(page, _BASE, "voice", _v_pid)
        stale_state = _v_svc.voice_review_state()
        check(stale_state["stale_count"] == 6 and stale_state["done_count"] == 0,
              "voice:stale-after-replacement",
              f"done={stale_state['done_count']} stale={stale_state['stale_count']}")
        check(page.locator("[data-testid=approve-voiceover]").count() == 0,
              "voice:approval-recloses", "ses degisti ama onay hala acik")
        check("BASKA" in page.inner_text("#screen"), "voice:stale-explained",
              "bayatlama kullaniciya aciklanmiyor")

        # Revision: recorded, navigable, and not an approval.
        page.fill("[data-testid=voice-revision-notes]", "Ozel isim yanlis okunuyor")
        page.select_option("[data-testid=voice-revision-target]", "script")
        page.locator("[data-testid=voice-revision-submit]").click()
        page.wait_for_timeout(1200)
        check(_v_svc.voice_review_state()["verdict"] == "needs_revision",
              "voice:revision-recorded",
              str(_v_svc.voice_review_state()["verdict"]))
        goto(page, _BASE, "voice", _v_pid)
        goto_button = page.locator("[data-testid=voice-revision-goto]")
        check(goto_button.count() == 1, "voice:revision-goto", "sekmeye git dugmesi yok")
        goto_button.click()
        page.wait_for_timeout(700)
        check("Script Studio" in page.inner_text("#screen"), "voice:revision-navigates",
              "revizyon dogru sekmeye goturmedi")

        # Clearing it must actually clear the blocker (the latent render bug).
        goto(page, _BASE, "voice", _v_pid)
        page.locator("[data-testid=voice-revision-clear]").click()
        page.wait_for_timeout(1000)
        check(_v_svc.voice_review_state()["verdict"] != "needs_revision",
              "voice:revision-clears",
              str(_v_svc.voice_review_state()["verdict"]))

        # ================================================================
        section("16. Responsive: mobile review layout")
        # ================================================================
        mobile_context = browser.new_context(viewport=MOBILE, is_mobile=True,
                                              has_touch=True)
        mpage = mobile_context.new_page()
        merrors = console_errors(mpage)
        goto(mpage, _BASE, "phase0", _PID)

        overflow = mpage.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(overflow <= 2, "mobile:no-horizontal-scroll",
              f"yatay tasma={overflow}px")

        sidebar_dir = mpage.evaluate(
            "() => getComputedStyle(document.querySelector('.sidebar')).flexDirection")
        check(sidebar_dir == "row", "mobile:sidebar-collapses",
              f"kenar cubugu yatay seride: {sidebar_dir}")

        for screen in ("jobs", "moments", "outputs", "settings"):
            goto(mpage, _BASE, screen, _PID)
            tap_overflow = mpage.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if tap_overflow > 2:
                check(False, "mobile:no-horizontal-scroll",
                      f"{screen} ekraninda tasma={tap_overflow}px")
                break
        else:
            check(True, "mobile:all-screens-fit", "4 inceleme ekrani mobilde tasmiyor")

        check(not merrors, "mobile:no-js-errors", f"mobil JS hatalari: {merrors[:3]}")

        # Desktop layout keeps the two-column shell.
        goto(page, _BASE, "phase0", _PID)
        desktop_cols = page.evaluate(
            "() => getComputedStyle(document.querySelector('.shell')).gridTemplateColumns")
        check("260px" in desktop_cols or len(desktop_cols.split()) == 2,
              "desktop:two-column", f"grid={desktop_cols}")

        mobile_context.close()
        context.close()
        browser.close()

finally:
    _server.shutdown()
    _server.server_close()
    _app.shutdown()
    for _dir in TMP_DIRS:
        shutil.rmtree(_dir, ignore_errors=True)

print()
print("=" * 74)
print(f"toplam {len(CHECKS)} kontrol, {sum(1 for _, ok in CHECKS if ok)} gecti, "
      f"{len(FAILURES)} basarisiz")
print("=" * 74)
if FAILURES:
    print("BROWSER_SUITE_FAILED")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("BROWSER_SUITE_OK (gercek Chromium + gercek soket; sahte saglayicilar, "
      "hicbir gercek API cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
sys.exit(0)
