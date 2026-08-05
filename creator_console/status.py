"""Local preflight checks and the pipeline workflow map.

WHY THIS EXISTS
---------------
Two questions the console could not answer without reading code:

  "Is my machine even able to run this?"   -> `run_preflight()`
  "Where am I in the workflow right now?"  -> `build_workflow_map()`

Both are computed on the SERVER and returned as plain, already-decided data, so
the browser renders them without re-deriving any pipeline rule.

Preflight never reads a secret VALUE -- it reports `configured` / `missing` only,
and it never contacts a provider. It is a local capability probe, not a health
check of anything remote.
"""
from __future__ import annotations

import importlib
import shutil
from pathlib import Path

from local_pipeline import source_strategy

#: Import-only dependency probes. Kept to what the pipeline genuinely needs, so a
#: green preflight means "the local phases can run", not "everything is perfect".
REQUIRED_MODULES = (
    ("faster_whisper", "Transcript / altyazi (faster-whisper)"),
    ("scenedetect", "Sahne tespiti (PySceneDetect)"),
    ("librosa", "Ses enerjisi analizi"),
    ("numpy", "Sayisal isleme"),
    ("imageio_ffmpeg", "Paketlenmis FFmpeg binary'si"),
)

OPTIONAL_MODULES = (
    ("playwright", "Tarayici testleri (yalnizca gelistirme)"),
    ("google.genai", "Gemini rotasi (script / TTS / embedding)"),
)

#: Below this the render stage is likely to fail mid-write.
MIN_FREE_DISK_MB = 500

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


def _check(name, label, status, detail, fix="") -> dict:
    return {"name": name, "label": label, "status": status, "detail": detail, "fix": fix}


def run_preflight(app, ownership=None) -> dict:
    """Everything that must be true locally before a phase can succeed."""
    checks: list = []

    # -- FFmpeg / ffprobe --------------------------------------------------
    try:
        from local_pipeline import render_utils

        ffmpeg_bin, ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
    except Exception as exc:  # noqa: BLE001
        ffmpeg_bin = ffprobe_bin = None
        checks.append(_check("ffmpeg", "FFmpeg", STATUS_FAIL,
                             f"cozumlenemedi: {type(exc).__name__}",
                             "pip install imageio-ffmpeg veya sisteme ffmpeg kur"))
    else:
        checks.append(_check(
            "ffmpeg", "FFmpeg", STATUS_OK if ffmpeg_bin else STATUS_FAIL,
            Path(ffmpeg_bin).name if ffmpeg_bin else "bulunamadi",
            "" if ffmpeg_bin else "pip install imageio-ffmpeg"))
        # ffprobe is optional: probe_media() falls back to parsing `ffmpeg -i`.
        checks.append(_check(
            "ffprobe", "ffprobe", STATUS_OK if ffprobe_bin else STATUS_WARN,
            Path(ffprobe_bin).name if ffprobe_bin else
            "yok - FFmpeg fallback kullanilacak (daha az kesin)",
            "" if ffprobe_bin else "Sisteme ffmpeg (ffprobe dahil) kurmak dogrulugu artirir"))

    # -- Python dependencies ----------------------------------------------
    for module_name, label in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            checks.append(_check(f"dep:{module_name}", label, STATUS_FAIL,
                                 f"import edilemedi ({type(exc).__name__})",
                                 f"pip install -r requirements-local.txt"))
        else:
            checks.append(_check(f"dep:{module_name}", label, STATUS_OK, "hazir"))

    for module_name, label in OPTIONAL_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            checks.append(_check(f"opt:{module_name}", label, STATUS_WARN,
                                 "kurulu degil (opsiyonel)",
                                 f"pip install {module_name.split('.')[0]}"))
        else:
            checks.append(_check(f"opt:{module_name}", label, STATUS_OK, "hazir"))

    # -- Whisper model cache ----------------------------------------------
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    cached = sorted(p.name for p in cache.glob("models--Systran--faster-whisper-*")) if cache.is_dir() else []
    checks.append(_check(
        "whisper_cache", "Whisper model onbellegi",
        STATUS_OK if cached else STATUS_WARN,
        ", ".join(m.replace("models--Systran--faster-whisper-", "") for m in cached)
        if cached else "onbellekte model yok - ilk analiz modeli indirecek",
        "" if cached else "Ilk calistirma internet gerektirir; sonrasi cevrimdisi calisir"))

    # -- Storage ------------------------------------------------------------
    try:
        root = Path(app.storage.root)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(root)
        free_mb = usage.free // (1024 * 1024)
        checks.append(_check(
            "storage", "Depolama",
            STATUS_OK if free_mb >= MIN_FREE_DISK_MB else STATUS_WARN,
            f"yazilabilir, {free_mb} MB bos",
            "" if free_mb >= MIN_FREE_DISK_MB
            else f"Render icin en az {MIN_FREE_DISK_MB} MB bos alan onerilir"))
    except OSError as exc:
        checks.append(_check("storage", "Depolama", STATUS_FAIL,
                             f"yazilamiyor: {type(exc).__name__}",
                             "Depolama kokunun izinlerini kontrol et"))

    # -- Credentials (state only, never a value) ---------------------------
    for reference in app.secrets.public_state():
        required = reference["kind"] == "google_api"
        configured = reference["state"] == "configured"
        checks.append(_check(
            f"secret:{reference['kind']}", reference["kind"],
            STATUS_OK if configured else (STATUS_WARN if required else STATUS_OK),
            f"{reference['state']}"
            + (f" ({reference['alias_used']})" if reference.get("alias_used") else ""),
            "" if configured else
            (f"Gemini script/TTS icin gerekli. .env.local dosyasina "
             f"{reference['aliases_supported'][0]}=... ekle."
             if required else "Opsiyonel saglayici; yoksa temiz atlanir.")))

    # -- Project source video ----------------------------------------------
    if ownership is not None and ownership.project_id:
        service = app.service(ownership)
        project = service.load_artifact("project")
        source = (project["data"] or {}).get("source")
        if not source:
            checks.append(_check("source_video", "Kaynak video", STATUS_WARN,
                                 "bu projede kaynak video yok",
                                 "Faz 0 sekmesinden bir video yukle"))
        else:
            try:
                path = service.source_path()
                size_mb = path.stat().st_size / 1024 ** 2
                checks.append(_check("source_video", "Kaynak video", STATUS_OK,
                                     f"{source['display_name']} ({size_mb:.0f} MB)"))
            except Exception as exc:  # noqa: BLE001
                checks.append(_check("source_video", "Kaynak video", STATUS_FAIL,
                                     str(exc)[:160],
                                     "Videoyu yeniden yukle"))

    failures = [c for c in checks if c["status"] == STATUS_FAIL]
    warnings = [c for c in checks if c["status"] == STATUS_WARN]
    return {
        "checks": checks,
        "ok": not failures,
        "fail_count": len(failures),
        "warn_count": len(warnings),
        "summary": ("Yerel ortam hazir." if not failures and not warnings else
                    f"{len(failures)} engel, {len(warnings)} uyari."
                    if failures else f"{len(warnings)} uyari (calisabilir)."),
    }


#: The workflow, in order. `modes` limits a step to certain processing modes;
#: `phase` is the pipeline phase whose job failure marks the step failed.
WORKFLOW_STEPS = (
    {"key": "source", "label": "Kaynak", "long_label": "Kaynak video",
     "tab": "phase0", "modes": None, "phase": None},
    {"key": "assessment", "label": "Degerlendirme", "long_label": "Faz 0 degerlendirme",
     "tab": "phase0", "modes": None, "phase": "source_assessment"},
    {"key": "plan", "label": "Plan", "long_label": "Isleme plani onayi",
     "tab": "phase0", "modes": None, "phase": None},
    {"key": "analysis", "label": "Analiz", "long_label": "Video analizi",
     "tab": "moments", "modes": None, "phase": "video_analysis"},
    {"key": "moment", "label": "Moment", "long_label": "Moment secimi",
     "tab": "moments", "modes": None, "phase": None},
    {"key": "script", "label": "Script", "long_label": "Script onayi",
     "tab": "script", "modes": (source_strategy.MODE_NARRATED_THEORY,),
     "phase": "script_generation"},
    {"key": "voice", "label": "Ses", "long_label": "Seslendirme onayi",
     "tab": "voice", "modes": (source_strategy.MODE_NARRATED_THEORY,),
     "phase": "tts"},
    {"key": "render", "label": "Render", "long_label": "Render",
     "tab": "render", "modes": None, "phase": "render"},
    {"key": "review", "label": "Inceleme", "long_label": "Final inceleme ve onay",
     "tab": "render", "modes": None, "phase": None},
)

STEP_COMPLETED = "completed"
STEP_CURRENT = "current"
STEP_WAITING = "waiting"
STEP_BLOCKED = "blocked"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_STATES = (STEP_COMPLETED, STEP_CURRENT, STEP_WAITING, STEP_BLOCKED,
               STEP_FAILED, STEP_SKIPPED)

#: Job phases whose most recent failure should surface on the map.
_FAILABLE_PHASES = tuple(step["phase"] for step in WORKFLOW_STEPS if step["phase"])


def _latest_failures(jobs) -> dict:
    """Phase -> its newest job, but only if that newest job failed.

    Newest-only on purpose: a phase that failed and was then re-run successfully
    is not failed any more, and showing it as failed would send the user off to
    fix something that is already fixed.
    """
    newest: dict = {}
    for job in jobs or []:
        phase = job.get("job_type")
        if phase not in _FAILABLE_PHASES or phase in newest:
            continue
        newest[phase] = job          # jobs arrive newest-first
    return {phase: job for phase, job in newest.items() if job.get("status") == "failed"}


def build_workflow_map(service, ownership, jobs=None) -> dict:
    """The whole pipeline as one decided, renderable structure.

    Every judgement the map shows -- which step is current, which is skipped for
    this mode, what to do next, why a step is blocked and how to repair it -- is
    made HERE. The browser only paints it. That is deliberate: re-deriving these
    rules in JavaScript is how a UI starts disagreeing with the pipeline it
    claims to describe.
    """
    project = service.load_artifact("project")
    assessment = service.load_artifact("source_assessment")
    plan = service.load_artifact("processing_plan")
    hot = service.load_artifact("hot_moments")
    selection = service.load_artifact("moment_selection")
    script = service.load_artifact("selected_script")
    voice = service.load_artifact("selected_voiceover")
    render = service.load_artifact("render_manifest")
    approvals = service.approval_chain()
    failures = _latest_failures(jobs)

    mode = ((plan["data"] or {}).get("decision") or {}).get("mode")
    source = (project["data"] or {}).get("source") or {}

    def outcome(done, *, action="", problem="", repair="", detail="", failed=""):
        return {"done": bool(done), "action": action, "problem": problem,
                "repair": repair, "detail": detail, "failed": failed}

    outcomes = {}

    outcomes["source"] = outcome(
        bool(source),
        action="Faz 0 sekmesinden kaynak videoyu yukle.",
        problem=project["problem"] or "",
        repair="Proje artefakti okunamiyor; projeyi yeniden ac.",
        detail=source.get("display_name", ""))

    assessment_data = assessment["data"] or {}
    outcomes["assessment"] = outcome(
        bool(assessment["data"]),
        action="Faz 0'da 'Kaynagi degerlendir' calistir.",
        problem=assessment["problem"] or "",
        repair="Degerlendirmeyi yeniden calistir (FFmpeg gerekir).",
        detail=(f"oneri: {assessment_data['recommended_mode']}"
                if assessment_data.get("recommended_mode") else ""))

    plan_state = approvals.get("processing_plan") or {}
    outcomes["plan"] = outcome(
        bool(plan["data"]) and plan_state.get("effective_valid"),
        action=("Faz 0'da modu SEC, plani olustur ve gosterilen hash ile ONAYLA."
                if not plan["data"] else
                "Faz 0'da plani gosterilen hash ile ONAYLA."),
        problem=plan["problem"] or "",
        repair="Plani yeniden olustur ve gosterilen hash ile onayla.",
        detail=mode or "")

    moments = (hot["data"] or {}).get("hot_moments") or []
    outcomes["analysis"] = outcome(
        bool(moments),
        action="Moment Explorer'da video analizini calistir.",
        problem=hot["problem"] or "",
        repair="Klip araligini veya top-N degerini degistirip analizi yeniden calistir.",
        detail=f"{len(moments)} aday" if moments else "",
        failed=("Analiz hic aday moment uretmedi."
                if hot["data"] and not moments else ""))

    outcomes["moment"] = outcome(
        bool(selection["data"]) and not selection["problem"],
        action="Moment Explorer'da bir adayi izle ve secimi kaydet.",
        problem=selection["problem"] or "",
        repair="Analiz degistiyse momenti yeniden incele ve secimi guncelle.",
        detail=(f"rank {selection['data']['selected_moment_rank']}"
                if selection["data"] else ""))

    script_state = approvals.get("script") or {}
    script_data = script["data"] or {}
    outcomes["script"] = outcome(
        script_state.get("effective_valid"),
        action=("Script Studio'da scripti uret." if not script["data"] else
                "Script Studio'da scripti oku ve hash ile onayla."),
        problem=script["problem"] or "",
        repair="Scripti yeniden uret, sonra gosterilen hash ile onayla.",
        detail=script_data.get("model", ""),
        failed=((script_data.get("status_reason") or "Script uretimi basarisiz.")
                if script_data.get("status") == "failed" else ""))

    voice_state = approvals.get("voiceover") or {}
    voice_data = voice["data"] or {}
    outcomes["voice"] = outcome(
        voice_state.get("effective_valid"),
        action=("Voice Studio'da seslendirmeyi uret." if not voice["data"] else
                "Voice Studio'da sesi dinle ve hash ile onayla."),
        problem=voice["problem"] or "",
        repair="Seslendirmeyi yeniden uret, sonra hash ile onayla.",
        detail=voice_data.get("voice", ""),
        failed=((voice_data.get("status_reason") or "Seslendirme uretilemedi.")
                if voice_data.get("status") in ("failed", "insufficient_data") else ""))

    render_data = render["data"] or {}
    render_details = ((render_data.get("output") or {}).get("validation") or {}).get("details") or {}
    outcomes["render"] = outcome(
        bool(render["data"]) and render_data.get("status") != "failed",
        action="Render Studio'da renderi calistir.",
        problem=render["problem"] or "",
        repair="Render ayarlarini kontrol edip yeniden calistir.",
        detail=render_details.get("resolution", ""),
        failed=("Render basarisiz." if render_data.get("status") == "failed" else ""))

    review = service.render_review_state() if render["data"] else None
    outcomes["review"] = outcome(
        (approvals.get("render") or {}).get("effective_valid"),
        action="Render Studio'da videoyu izle, 6 maddelik listeyi doldur ve final onayi ver.",
        repair="Inceleme listesini tamamla; revizyon acikca istendiyse once onu kapat.",
        detail=(f"{review['done_count']}/{review['total_count']} madde" if review else ""))

    steps, current_found = [], False
    for definition in WORKFLOW_STEPS:
        result = outcomes[definition["key"]]
        # A step counts as skipped only once the MODE IS KNOWN. Before a plan
        # exists nothing is decided, so script/voice are still ahead of the user
        # rather than excluded -- calling them skipped would misreport the
        # workflow and then silently grow it when a mode is finally chosen.
        if definition["modes"] is not None and mode and mode not in definition["modes"]:
            state = STEP_SKIPPED
        elif result["done"]:
            state = STEP_COMPLETED
        elif result["problem"]:
            state = STEP_BLOCKED
            current_found = True
        elif definition["phase"] in failures or result["failed"]:
            state = STEP_FAILED
            current_found = True
        elif not current_found:
            state = STEP_CURRENT
            current_found = True
        else:
            state = STEP_WAITING

        blocker = ""
        if state == STEP_BLOCKED:
            blocker = result["problem"]
        elif state == STEP_FAILED:
            job = failures.get(definition["phase"]) or {}
            blocker = result["failed"] or job.get("error") or "Faz basarisiz oldu."

        steps.append({
            "key": definition["key"], "label": definition["label"],
            "long_label": definition["long_label"], "tab": definition["tab"],
            "state": state,
            "action": "" if state in (STEP_COMPLETED, STEP_SKIPPED) else result["action"],
            "blocker": blocker,
            "repair": result["repair"] if state in (STEP_BLOCKED, STEP_FAILED) else "",
            "detail": result["detail"] if state == STEP_COMPLETED else "",
            "skipped_reason": ("direct_clip_series modunda kullanilmaz: klipler "
                               "orijinal sesiyle kesilir."
                               if state == STEP_SKIPPED else ""),
        })

    active = [step for step in steps if step["state"] != STEP_SKIPPED]
    done_count = len([step for step in active if step["state"] == STEP_COMPLETED])
    current = next((step for step in steps
                    if step["state"] in (STEP_CURRENT, STEP_BLOCKED, STEP_FAILED)), None)
    return {
        "steps": steps,
        "states": list(STEP_STATES),
        "mode": mode,
        "mode_locked": bool(mode),
        "done_count": done_count,
        "total_count": len(active),
        "skipped_count": len(steps) - len(active),
        "percent": round(100 * done_count / max(1, len(active))),
        "current_step": current["key"] if current else None,
        "current_label": current["long_label"] if current else "Tamamlandi",
        "current_state": current["state"] if current else STEP_COMPLETED,
        "current_action": (current or {}).get("action", ""),
        "current_blocker": (current or {}).get("blocker", ""),
        "current_repair": (current or {}).get("repair", ""),
        "current_tab": (current or {}).get("tab", "outputs"),
        "complete": current is None,
    }
