#!/usr/bin/env python
"""Prepare one isolated narrated_theory project on the real HOTD source.

    python prepare_hotd_project.py

WHAT THIS IS
------------
Not a test -- a setup script. It builds a real, reviewable project the way the
console would, and then STOPS at the first gate that needs a human: choosing
which candidate moment to make a Short from.

It runs only the phases that are entirely local: Phase 0 source assessment
(ffprobe) and video analysis (ffmpeg + faster-whisper + PySceneDetect). Both
touch no provider at all. To make that a guarantee rather than a claim, every
provider client factory below raises on first attribute access, so a call that
was not supposed to happen fails loudly instead of quietly spending quota.

DELIBERATELY NOT DONE
---------------------
No moment is selected, no script is generated, no TTS or render runs, and no
approval past the processing plan is granted. The plan approval IS granted --
it is what opens the analysis gate, and it is the artifact this script exists to
produce alongside the candidates.

The user's own projects are untouched: separate storage root, separate
workspace, and the source video is only READ.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.env_file import load_env_file  # noqa: E402
from local_pipeline import approval  # noqa: E402

STORAGE_ROOT = PROJECT_ROOT / ".console_data_hotd"
WORKSPACE_ID = "wsp_hotd"
SOURCE_CANDIDATES = [
    Path.home() / "Downloads" / "THE ABSOLUTE BEST OF DAEMON TARGARYEN - ABC CINEMA (1080p).mp4",
    Path.home() / "Downloads" / "Daemon Targaryen may never have truly wanted the Iron Throne..mp4",
]

THEORY_ANGLE = (
    "Daemon Targaryen's willingness to act without hesitation is his greatest "
    "strength, but also the reason he can never become a stable ruler."
)
CONTENT_LANGUAGE = "en-US"
NARRATION_PRESET = "deep_storyteller"
OPERATOR = "hotd-setup"

#: Analysis window. Starts past the opening so the first candidate is not an
#: intro card, and is wide enough for five genuinely different moments without
#: making a `small` Whisper pass run for an unreasonable time.
CLIP_START_SEC = 60
CLIP_DURATION_SEC = 300
TOP_N = 5
WHISPER_MODEL = "small"
SCENE_DETECTOR = "content"


class NoLiveProviders:
    """Any attribute access is a provider call that should not be happening."""

    def __getattr__(self, name):
        raise AssertionError(
            f"CANLI SAGLAYICI CAGRISI ENGELLENDI: {name}. Bu betik yalnizca yerel "
            "fazlari (degerlendirme + analiz) calistirir.")


def call(api, method, path, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers=headers or {})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="mevcut izole calisma alanini silip bastan kur")
    parser.add_argument("--clip-start", type=int, default=CLIP_START_SEC)
    parser.add_argument("--clip-duration", type=int, default=CLIP_DURATION_SEC)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    args = parser.parse_args(argv)

    load_env_file(PROJECT_ROOT / ".env.local")
    source = next((p for p in SOURCE_CANDIDATES if p.is_file()), None)
    if source is None:
        print("REAL_SOURCE_MISSING: HOTD kaynak videosu bulunamadi.")
        print("  Arananlar:")
        for candidate in SOURCE_CANDIDATES:
            print(f"    {candidate}")
        return 2

    if args.reset and STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT, ignore_errors=True)

    print("=" * 78)
    print("IZOLE narrated_theory PROJESI HAZIRLANIYOR (gercek HOTD kaynagi)")
    print("=" * 78)
    print(f"  Kaynak         : {source.name} ({source.stat().st_size / 1024**2:.0f} MB)")
    print(f"  Izole depolama : {STORAGE_ROOT}")
    print(f"  Calisma alani  : {WORKSPACE_ID} (kullanicinin projeleri ayri)")
    print(f"  Dil / on ayar  : {CONTENT_LANGUAGE} / {NARRATION_PRESET}")
    print("  Saglayicilar   : ENGELLI (canli cagri yapilamaz)")
    print("  Durak noktasi  : moment secimi (insan karari) — SECIM YAPILMAZ")
    print()

    app = ConsoleApp(ConsoleConfig(
        storage_root=STORAGE_ROOT,
        user_id="usr_hotd", workspace_id=WORKSPACE_ID,
        inline_jobs=True,
        secret_getter=lambda name: None,
        google_client_factory=lambda: NoLiveProviders(),
        openai_client_factory=lambda: NoLiveProviders(),
        anthropic_client_factory=lambda: NoLiveProviders(),
        elevenlabs_client_factory=lambda: NoLiveProviders()))
    api = ConsoleApi(app)

    try:
        # --- project + source ------------------------------------------
        status, payload = call(api, "POST", "/api/projects", {
            "title": "HOTD — Daemon Targaryen (narrated_theory)",
            "created_by": OPERATOR})
        if status != 201:
            print(f"PROJE OLUSTURULAMADI: HTTP {status} {payload}")
            return 1
        pid = payload["project"]["project_id"]
        ownership = app.ownership(pid)
        service = app.service(ownership)
        print(f"  [1/4] Proje olusturuldu: {pid}")

        started = time.time()
        api.handle("POST", f"/api/projects/{pid}/source", body=source.read_bytes(),
                   headers={"X-Filename": source.name, "X-Actor": OPERATOR})
        record = service.load_artifact("project")["data"]["source"]
        print(f"  [2/4] Kaynak kaydedildi: {record['size_bytes'] / 1024**2:.0f} MB, "
              f"parmak izi {record['fingerprint_sha256'][:16]} "
              f"({time.time() - started:.0f}s)")

        # --- Phase 0 assessment (local ffprobe) ------------------------
        started = time.time()
        status, payload = call(api, "POST", f"/api/projects/{pid}/jobs",
                               {"job_type": "source_assessment",
                                "run_request_id": "hotd-assess"})
        assessment = service.load_artifact("source_assessment")["data"]
        if not assessment:
            print(f"DEGERLENDIRME BASARISIZ: {payload}")
            return 1
        media = assessment["source"]
        print(f"  [3/4] Degerlendirme: oneri={assessment['recommended_mode']}, "
              f"otomatik secim={assessment['auto_selected']} "
              f"({time.time() - started:.0f}s)")
        print(f"        {media['duration_sec']:.0f}s · {media['width']}x{media['height']} · "
              f"ses={'var' if media.get('has_audio') else 'yok'}")

        # --- narrated_theory plan, approved by hash --------------------
        status, payload = call(api, "POST", f"/api/projects/{pid}/plan", {
            "mode": "narrated_theory", "selected_by": OPERATOR,
            "content_language": CONTENT_LANGUAGE,
            "narration_preset": NARRATION_PRESET,
            "theory_angle": THEORY_ANGLE,
            "output_count": 1, "target_duration_sec": 60,
            "render_config": {"render_mode": "blurred_background", "preview_mode": True,
                               "caption_style": "active_word_yellow",
                               "caption_theme": "auto"},
            "notes": "IZOLE HAZIRLIK — moment secimi ve sonrasi INSAN kararidir"})
        if status != 201:
            print(f"PLAN OLUSTURULAMADI: HTTP {status} "
                  f"{(payload.get('error') or {}).get('message')}")
            return 1
        plan = service.load_artifact("processing_plan")["data"]
        digest = approval.content_hash_for("processing_plan", plan)
        status, payload = call(api, "POST",
                               f"/api/projects/{pid}/approvals/processing_plan",
                               {"reviewed_sha256": digest, "approved_by": OPERATOR})
        if status != 200:
            print(f"PLAN ONAYLANAMADI: HTTP {status}")
            return 1
        plan = service.load_artifact("processing_plan")["data"]
        allowed, reason = service.plan_gate("video_analysis")
        print(f"  [4/4] Plan onaylandi (hash {digest[:16]}), analiz kapisi "
              f"{'ACIK' if allowed else 'KAPALI: ' + reason}")

        # --- video analysis (local ffmpeg + whisper + scenedetect) -----
        print()
        print(f"  Analiz calisiyor: {args.clip_start}s-"
              f"{args.clip_start + args.clip_duration}s penceresi, "
              f"whisper={WHISPER_MODEL}, top-N={args.top_n}...")
        started = time.time()
        status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "video_analysis",
            "params": {"clip_start_sec": args.clip_start,
                        "clip_duration_sec": args.clip_duration,
                        "whisper_model_size": WHISPER_MODEL,
                        "scene_detector": SCENE_DETECTOR,
                        "top_n": args.top_n},
            "run_request_id": "hotd-analysis"})
        hot = service.load_artifact("hot_moments")["data"]
        if not hot or not hot.get("hot_moments"):
            job = payload.get("job") or {}
            print(f"ANALIZ ADAY URETMEDI: {job.get('error') or payload}")
            return 1
        elapsed = time.time() - started
        moments = hot["hot_moments"]
        print(f"  Analiz tamamlandi: {len(moments)} aday ({elapsed:.0f}s)")

        # --- report ----------------------------------------------------
        decision = plan["decision"]
        narration = decision.get("narration") or {}
        project_dir = app.storage.namespace_root(ownership)

        print()
        print("=" * 78)
        print("PROJE YOLU")
        print("=" * 78)
        print(f"  {project_dir}")

        print()
        print("=" * 78)
        print("ONAYLANAN PLAN OZETI")
        print("=" * 78)
        print(f"  Mod              : {decision['mode']}")
        print(f"  Icerik dili      : {decision['content_language']}")
        print(f"  Anlatim on ayari : {narration.get('preset')} "
              f"(ses: {narration.get('voice')})")
        print(f"  Teori acisi      : {decision['theory_angle']}")
        print(f"  Cikti / sure     : {decision['output_count']} adet, "
              f"{decision['target_duration_sec']}s hedef")
        print(f"  Render           : {decision['render'].get('render_mode')}, "
              f"preview={decision['render'].get('preview_mode')}, "
              f"caption={decision['render'].get('caption_style')}")
        print(f"  Durum            : {plan['status']} · onay hash {digest[:32]}")
        print(f"  Onaylayan        : {plan['human_approval'].get('approved_by')} "
              f"@ {plan['human_approval'].get('approved_at')}")
        print(f"  Acik fazlar      : {', '.join(plan['enabled_phases'])}")
        print(f"  Kapali fazlar    : {', '.join(plan['disabled_phases']) or '(yok)'}")

        print()
        print("=" * 78)
        print(f"ADAY MOMENTLER ({len(moments)}) — SECIM YAPILMADI")
        print("=" * 78)
        for moment in moments:
            print(f"  #{moment['rank']}  skor {moment.get('score')}  "
                  f"{moment['source_start_sec']:.1f}s → {moment['source_end_sec']:.1f}s  "
                  f"({moment['source_end_sec'] - moment['source_start_sec']:.1f}s)")
            print(f"       gerekce: {moment.get('reason', '-')}")
            transcript = (moment.get("transcript") or "").strip()
            if transcript:
                print(f"       replik : {transcript[:150]}")

        print()
        print("=" * 78)
        print("ONIZLEME DOSYALARI")
        print("=" * 78)
        playable = 0
        for moment in moments:
            preview = moment.get("preview_path")
            if preview and Path(preview).is_file() and Path(preview).stat().st_size > 0:
                playable += 1
                print(f"  #{moment['rank']}  {Path(preview)}")
                print(f"       {Path(preview).stat().st_size / 1024**2:.1f} MB")
            else:
                print(f"  #{moment['rank']}  URETILEMEDI: "
                      f"{moment.get('preview_error', 'bilinmeyen')[:100]}")
        print(f"  Oynatilabilir: {playable}/{len(moments)}")

        # --- stop-line verification ------------------------------------
        print()
        print("=" * 78)
        print("DURAK NOKTASI DOGRULAMASI")
        print("=" * 78)
        stopped = {
            "moment secimi": service.load_artifact("moment_selection")["data"],
            "script": service.load_artifact("selected_script")["data"],
            "seslendirme": service.load_artifact("selected_voiceover")["data"],
            "render": service.load_artifact("render_manifest")["data"],
        }
        for label, artifact in stopped.items():
            print(f"  {label:<16}: {'URETILDI (!)' if artifact else 'yok — beklendigi gibi'}")
        chain = service.approval_chain()
        for kind in ("script", "voiceover", "render"):
            state = chain.get(kind) or {}
            print(f"  {kind + ' onayi':<16}: "
                  f"{'ONAYLI (!)' if state.get('approved') else 'verilmedi'}")
        workflow = app.project_view(ownership)["workflow"]
        print(f"  Akis su anda    : {workflow['current_step']} "
              f"({workflow['current_label']}) — {workflow['done_count']}/"
              f"{workflow['total_count']} adim")
        print(f"  Siradaki eylem  : {workflow['current_action']}")

        problems = [label for label, artifact in stopped.items() if artifact]
        if problems:
            print()
            print(f"BEKLENMEYEN URETIM: {problems}")
            return 1
        print()
        print("HOTD_PROJECT_READY (yerel fazlar tamam; moment secimi INSAN karari, "
              "canli saglayici cagrilmadi, TTS/render/yukleme/yayin yok)")
        return 0
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
