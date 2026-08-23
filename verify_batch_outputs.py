#!/usr/bin/env python
"""M0.10 verification: full-source scan + batch multi-Short production.

    python verify_batch_outputs.py

WHAT THIS PROVES
----------------
Until now the engine could produce exactly ONE Short per project: `_run_render`
always wrote to `results/render/`, and `outputs()` reported slots 2..N with a
hardcoded `insufficient_data`. A plan asking for 10 Shorts produced 1. This
suite drives the real HTTP surface against a real synthetic source and checks
that N Shorts are actually produced, isolated, and independently approved.

It also checks the analysis half: a full-source scan must find candidates from
across the WHOLE film, because N Shorts cut from one 240-second window would be
N variations of the same scene.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
  * Never touches the user's projects: storage is a fresh temp root.
  * Never grants a final human approval that a human did not earn -- the
    approval paths tested here supply a real checklist first, and the suite
    asserts that skipping it is REFUSED.
  * Makes no provider call (fixture doubles that record any call).
  * Uploads and publishes nothing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from creator_console import fixtures  # noqa: E402
from creator_console.api import ConsoleApi  # noqa: E402
from creator_console.app import ConsoleApp, ConsoleConfig  # noqa: E402
from creator_console.pipeline_service import PER_SLOT_ARTIFACTS  # noqa: E402
from local_pipeline import render_utils, source_strategy, video_analysis  # noqa: E402

FAILURES: list = []
CHECKS = 0


def check(condition, label: str, message: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [{label}] OK" + (f" - {message}" if message else ""))
        return True
    FAILURES.append(f"{label}: {message}")
    print(f"  [{label}] BASARISIZ - {message}")
    return False


def call(api, method, path, body=None, headers=None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    return api.handle(method, path, body=raw, headers=headers or {})


def make_long_fixture(path: Path, ffmpeg: str, seconds: int = 150) -> Path:
    """A source long enough to have distinct regions, with a loud burst late.

    The burst sits in the last third on purpose: a window analysis starting at 0
    would never reach it, so finding it is evidence the scan really covered the
    whole file rather than the opening minutes.
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=200:duration={seconds},volume=0.02",
        "-f", "lavfi", "-i",
        f"sine=frequency=800:duration={seconds},"
        f"volume='if(between(t,{seconds - 40},{seconds - 20}),0.9,0)':eval=frame",
        "-filter_complex", "[1][2]amix=inputs=2:duration=longest[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"fixture uretilemedi: {proc.stderr[-800:]}")
    return path


def main() -> int:
    ffmpeg, ffprobe = render_utils.resolve_ffmpeg_binaries()
    storage_root = Path(tempfile.mkdtemp(prefix="batch_verify_"))
    staging = Path(tempfile.mkdtemp(prefix="batch_src_"))
    source = make_long_fixture(staging / "long_source.mp4", ffmpeg)

    print("=" * 78)
    print("M0.10 - Tam kaynak taramasi ve batch coklu Short uretimi")
    print("=" * 78)
    print(f"  Kaynak (sentetik): {source.name}, {source.stat().st_size / 1024:.0f} KB")
    print(f"  Izole depolama:    {storage_root}")
    print()

    app = ConsoleApp(ConsoleConfig(
        storage_root=storage_root,
        user_id="usr_batch", workspace_id="wsp_batch",
        inline_jobs=True,
        secret_getter=lambda name: None,
        **fixtures.fixture_client_factories()))
    api = ConsoleApi(app)
    fixtures.reset_provider_calls()

    OUTPUT_COUNT = 3

    # ---------------------------------------------------------------- 1. setup
    print("--- 1. Proje, kaynak ve onayli 3 ciktili plan ---")
    status, payload = call(api, "POST", "/api/projects",
                           {"title": "Batch testi (izole)", "operator_name": "verify_batch",
                            "created_by": "verify_batch"})
    check(status == 201, "project", f"HTTP {status}")
    pid = payload["project"]["project_id"]
    ownership = app.ownership(pid)
    service = app.service(ownership)

    status, _ = api.handle("POST", f"/api/projects/{pid}/source", body=source.read_bytes(),
                           headers={"X-Filename": source.name, "X-Actor": "verify_batch"})
    check(status == 201, "source-upload", f"HTTP {status}")

    status, _ = call(api, "POST", f"/api/projects/{pid}/jobs",
                     {"job_type": "source_assessment", "run_request_id": "assess"})
    assessment = service.load_artifact("source_assessment")["data"]
    check(assessment is not None and assessment["source"]["duration_sec"] > 100,
          "assessment", f"{(assessment or {}).get('source', {}).get('duration_sec')}s")

    render_config = {"render_mode": "blurred_background", "preview_mode": True,
                     "caption_style": "active_word_yellow", "caption_theme": "auto",
                     "subtitle_whisper_model_size": "tiny"}
    status, payload = call(api, "POST", f"/api/projects/{pid}/plan", {
        "mode": "direct_clip_series", "selected_by": "verify_batch",
        "output_count": OUTPUT_COUNT, "target_duration_sec": 20,
        "render_config": render_config, "notes": "IZOLE BATCH TESTI"})
    check(status == 201, "plan-created", f"HTTP {status}")
    plan = payload["plan"]
    check(len(plan["outputs"]) == OUTPUT_COUNT, "plan-slots",
          f"{len(plan['outputs'])} yuva")

    # Slot 1 keeps the historical path; only 2..N get their own directory.
    slot_dirs = [Path(s["output_dir"]).name for s in plan["outputs"]]
    check(slot_dirs[0] == "render", "slot1-historical-path", f"slot1 dizini={slot_dirs[0]}")
    check(slot_dirs[1:] == ["output_02", "output_03"], "slot-n-isolated-dirs",
          f"{slot_dirs}")

    digest = service.describe_plan_review()["review"]["content_sha256"]
    status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/processing_plan",
                     {"reviewed_sha256": digest, "approved_by": "verify_batch"})
    check(status == 200, "plan-approved", f"HTTP {status}")

    # ------------------------------------------------- 2. full-source analysis
    print()
    print("--- 2. Tam kaynak taramasi (iki gecisli) ---")
    t0 = time.time()
    status, payload = call(api, "POST", f"/api/projects/{pid}/jobs", {
        "job_type": "video_analysis",
        "params": {"analysis_scope": "full_source", "whisper_model_size": "tiny",
                   "scene_detector": "content", "top_n": OUTPUT_COUNT},
        "run_request_id": "full-scan"})
    job = app.job_store(ownership).get(payload["job"]["job_id"])
    hot = service.load_artifact("hot_moments")["data"]
    check(hot is not None and hot.get("hot_moments"),
          "full-scan-ran", f"is={job['status']} hata={job.get('error')}")
    if not hot or not hot.get("hot_moments"):
        return finish(app, storage_root, staging)
    moments = hot["hot_moments"]
    scan = hot.get("full_source_scan") or {}
    print(f"     tarama suresi: {time.time() - t0:.1f}s")
    check(hot.get("analysis_scope") == "full_source", "full-scan-scope",
          str(hot.get("analysis_scope")))
    check(len(moments) == OUTPUT_COUNT, "full-scan-candidates", f"{len(moments)} aday")
    check(scan.get("coarse_pass", {}).get("frame_count", 0) >= 140,
          "full-scan-whole-film",
          f"{scan.get('coarse_pass', {}).get('frame_count')} saniyelik enerji profili")
    # This fixture is shorter than one default region, so covering it takes
    # exactly one region -- subdividing a film the scan already sees end to end
    # would only split Whisper and risk cutting a moment at a seam.
    check(scan.get("regions_scanned") == 1, "full-scan-short-source-single-region",
          f"{scan.get('regions_scanned')} bolge (kaynak {scan.get('source_duration_sec')}s "
          f"< bolge {scan.get('region_duration_sec')}s)")
    check(scan.get("regions_with_candidates") == scan.get("regions_scanned"),
          "full-scan-regions-productive",
          f"{scan.get('regions_with_candidates')}/{scan.get('regions_scanned')} bolge aday uretti")

    # Candidates must be spread across the film, not clustered in one window.
    starts = sorted(m["source_start_sec"] for m in moments)
    check(max(starts) - min(starts) > 40, "full-scan-spread",
          f"aday araligi {min(starts):.0f}s..{max(starts):.0f}s")
    check(max(starts) > 60, "full-scan-reaches-late-film",
          f"en gec aday {max(starts):.0f}s (240s pencere disinda kalirdi)")
    check(all(m.get("region_score") is not None for m in moments),
          "full-scan-region-score-kept", "her adayda bolge skoru duruyor")
    check(all("analysis_region" in m for m in moments),
          "full-scan-region-recorded", "her aday hangi bolgeden geldigini tasiyor")

    # Previews were cut from the raw source at absolute times.
    previews = [m.get("preview_path") for m in moments]
    check(all(p and Path(p).is_file() and Path(p).stat().st_size > 0 for p in previews),
          "full-scan-previews", f"{len(previews)} onizleme uretildi")

    # ------------------------------------------------ 3. per-slot moment choice
    print()
    print("--- 3. Yuva basina insan moment secimi ---")
    for index in range(1, OUTPUT_COUNT + 1):
        status, payload = call(api, "POST", f"/api/projects/{pid}/moment-selection", {
            "selected_rank": index, "reviewed_by": "verify_batch", "slot": index,
            "require_preview": True})
        check(status == 201, f"slot{index}-moment-selected", f"HTTP {status}")

    # Each slot's selection is a SEPARATE artifact.
    paths = {index: service.artifact_relative("moment_selection", index)
             for index in range(1, OUTPUT_COUNT + 1)}
    check(paths[1] == "results/moment_selection.json", "slot1-selection-path", paths[1])
    check(paths[2] == "results/render/output_02/moment_selection.json",
          "slot2-selection-path", paths[2])
    check(len(set(paths.values())) == OUTPUT_COUNT, "selection-paths-distinct", str(paths))
    ranks = {index: service.load_artifact("moment_selection", slot=index)["data"]
             ["selected_moment_rank"] for index in range(1, OUTPUT_COUNT + 1)}
    check(ranks == {1: 1, 2: 2, 3: 3}, "selections-independent", str(ranks))

    # The same moment twice in one batch is a mistake, and is refused.
    status, payload = call(api, "POST", f"/api/projects/{pid}/moment-selection", {
        "selected_rank": 1, "reviewed_by": "verify_batch", "slot": 3})
    check(status >= 400, "duplicate-moment-refused", f"HTTP {status}")
    check(service.load_artifact("moment_selection", slot=3)["data"]["selected_moment_rank"] == 3,
          "duplicate-refusal-left-slot-intact", "yuva 3 hala rank 3")

    # A slot the plan never defined cannot be invented.
    status, _ = call(api, "POST", f"/api/projects/{pid}/moment-selection", {
        "selected_rank": 1, "reviewed_by": "verify_batch", "slot": 47})
    check(status >= 400, "unknown-slot-refused", f"HTTP {status}")
    check(not list(storage_root.rglob("output_47")), "unknown-slot-no-directory",
          "output_47 dizini olusmadi")

    # ------------------------------------------- 3b. per-slot caption approval
    # Each slot burns ITS OWN moment's words in, so each slot needs its own
    # transcript produced and read. One approval must not cover three Shorts.
    print()
    print("--- 3b. Yuva basina altyazi onayi ---")
    for index in range(1, OUTPUT_COUNT + 1):
        status, _ = call(api, "POST", f"/api/projects/{pid}/jobs", {
            "job_type": "transcript",
            "params": {"slot": index, "subtitle_whisper_model_size": "tiny",
                        "subtitle_language": "auto"}})
        state = service.approval_state("transcript", index)
        check(state["present"] and state.get("content_sha256"),
              f"slot{index}-transcript-produced", f"HTTP {status}, {state.get('reason')}")
        service.approve("transcript", reviewed_sha256=state["content_sha256"],
                        approved_by="verify_batch", slot=index)
        check(service.approval_state("transcript", index)["valid"],
              f"slot{index}-transcript-approved",
              str(service.approval_state("transcript", index).get("reason")))

    tr_paths = {index: service.artifact_relative("moment_transcript", index)
                for index in range(1, OUTPUT_COUNT + 1)}
    check(len(set(tr_paths.values())) == OUTPUT_COUNT, "transcript-paths-distinct",
          str(tr_paths))

    # ----------------------------------------------------------- 4. batch board
    print()
    print("--- 4. Batch panosu ve kuyruk ---")
    status, payload = call(api, "GET", f"/api/projects/{pid}/batch")
    batch = payload["batch"]
    check(status == 200 and batch["output_count"] == OUTPUT_COUNT, "batch-state",
          f"HTTP {status}, {batch['output_count']} yuva")
    check(sorted(batch["runnable_slots"]) == [1, 2, 3], "batch-all-runnable",
          str(batch["runnable_slots"]))
    check(all(s["state"] == "ready_to_render" for s in batch["slots"]),
          "batch-states-ready", str([s["state"] for s in batch["slots"]]))

    t0 = time.time()
    status, payload = call(api, "POST", f"/api/projects/{pid}/batch",
                           {"run_request_id": "batch-1", "submitted_by": "verify_batch"})
    check(status == 202, "batch-submitted", f"HTTP {status}")
    check(payload["submitted_count"] == OUTPUT_COUNT, "batch-submitted-count",
          f"{payload['submitted_count']} render kuyruga alindi")
    print(f"     batch render suresi: {time.time() - t0:.1f}s")

    # A double-clicked batch button must not produce a second set of renders.
    status, payload = call(api, "POST", f"/api/projects/{pid}/batch",
                           {"run_request_id": "batch-1", "submitted_by": "verify_batch"})
    created = [row for row in payload["submitted"] if row.get("created")]
    check(not created, "batch-idempotent", f"{len(created)} yeni is (0 olmali)")

    # --------------------------------------------------- 5. slot isolation
    print()
    print("--- 5. Slot izolasyonu: N ayri Short gercekten uretildi ---")
    outputs = service.outputs()
    check(len(outputs) == OUTPUT_COUNT, "outputs-count", f"{len(outputs)} cikti")
    usable = [o for o in outputs if o.get("video") and o["video"]["usable"]]
    check(len(usable) == OUTPUT_COUNT, "outputs-all-rendered",
          f"{len(usable)}/{OUTPUT_COUNT} video kullanilabilir")

    videos = {}
    for entry in outputs:
        index = entry["index"]
        manifest = service.load_artifact("render_manifest", slot=index)["data"]
        check(manifest is not None, f"slot{index}-manifest", "manifest yazildi")
        if not manifest:
            continue
        check(manifest.get("output_slot") == index, f"slot{index}-manifest-slot",
              f"output_slot={manifest.get('output_slot')}")
        path = Path(manifest["output"]["final_video_path"])
        videos[index] = path
        check(path.is_file() and path.stat().st_size > 0, f"slot{index}-video-exists",
              f"{path.stat().st_size / 1024:.0f} KB" if path.is_file() else "yok")

    # The decisive isolation check: distinct files, distinct directories, and
    # distinct CONTENT (three Shorts of the same scene would defeat the point).
    check(len(set(str(p) for p in videos.values())) == OUTPUT_COUNT,
          "videos-distinct-paths", str([p.parent.name for p in videos.values()]))
    digests = {i: video_analysis._sha256_file(p) for i, p in videos.items()}
    check(len(set(digests.values())) == OUTPUT_COUNT, "videos-distinct-content",
          f"{len(set(digests.values()))} farkli SHA-256")
    check(videos[1].parent.name == "render", "slot1-video-historical-dir",
          videos[1].parent.name)
    check(videos[2].parent.name == "output_02", "slot2-video-own-dir",
          videos[2].parent.name)

    # Each Short covers its own moment range.
    ranges = {}
    for index in videos:
        manifest = service.load_artifact("render_manifest", slot=index)["data"]
        ranges[index] = manifest["inputs"]["selected_moment_rank"]
    check(len(set(ranges.values())) == OUTPUT_COUNT, "shorts-cover-distinct-moments",
          str(ranges))

    # Every Short is a real 9:16 video with audio. Use the production probe
    # wrapper because imageio_ffmpeg installations often have no standalone
    # ffprobe binary; that supported environment must not crash the verifier.
    for index, path in videos.items():
        info, backend = render_utils.probe_media(ffmpeg, ffprobe, path)
        streams = info.get("streams", [])
        video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
        audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)
        check(video_stream is not None and audio_stream is not None,
              f"slot{index}-streams", f"video+ses akisi var ({backend})")
        if video_stream:
            check(video_stream["height"] > video_stream["width"], f"slot{index}-vertical",
                  f"{video_stream['width']}x{video_stream['height']}")

    # ------------------------------------------- 6. independent review + approval
    print()
    print("--- 6. Her Short ayri ayri incelenir ve onaylanir ---")
    review_paths = {i: service.render_review_relative(i) for i in range(1, OUTPUT_COUNT + 1)}
    check(len(set(review_paths.values())) == OUTPUT_COUNT, "review-paths-distinct",
          str(review_paths))

    # Approving slot 2 without watching it is REFUSED.
    slot2_digest = service.render_video_digest(2)
    status, payload = call(api, "POST", f"/api/projects/{pid}/approvals/render",
                           {"reviewed_sha256": slot2_digest, "approved_by": "verify_batch",
                            "slot": 2})
    check(status >= 400, "slot2-approval-refused-without-checklist", f"HTTP {status}")

    # Complete slot 1's checklist only.
    from creator_console import render_review as rr
    for item in rr.CHECKLIST_ITEMS:
        call(api, "POST", f"/api/projects/{pid}/render-review",
             {"action": "set_item", "key": item["key"], "checked": True,
              "reviewed_by": "verify_batch", "slot": 1})
    state1 = service.render_review_state(1)
    state2 = service.render_review_state(2)
    check(state1["complete"], "slot1-checklist-complete",
          f"{state1['done_count']}/{state1['total_count']}")
    check(not state2["complete"] and state2["done_count"] == 0,
          "slot2-checklist-untouched",
          f"{state2['done_count']}/{state2['total_count']} - slot1 ticks did not leak")

    status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/render",
                     {"reviewed_sha256": service.render_video_digest(1),
                      "approved_by": "verify_batch", "slot": 1})
    check(status == 200, "slot1-approved", f"HTTP {status}")

    # Slot 2 is STILL refused: one approval is one Short.
    status, _ = call(api, "POST", f"/api/projects/{pid}/approvals/render",
                     {"reviewed_sha256": service.render_video_digest(2),
                      "approved_by": "verify_batch", "slot": 2})
    check(status >= 400, "slot2-still-refused-after-slot1-approval", f"HTTP {status}")

    chain1 = service.approval_chain(1)
    chain2 = service.approval_chain(2)
    check(chain1["render"]["approved"] and chain1["render"]["effective_valid"],
          "slot1-chain-valid", "slot1 render onayi baglayici")
    check(not chain2["render"]["approved"], "slot2-chain-unapproved",
          "slot2 render onayi yok")

    batch = service.batch_state()
    check(batch["approved_count"] == 1, "batch-approved-count",
          f"{batch['approved_count']}/{OUTPUT_COUNT}")
    states = {s["index"]: s["state"] for s in batch["slots"]}
    check(states[1] == "approved" and states[2] == "needs_review",
          "batch-states-after-approval", str(states))

    # ------------------------------------------ 7. multi-region scan (library)
    print()
    print("--- 7. Cok bolgeli tarama (kutuphane seviyesi) ---")
    # The console path uses 180s regions, which a short fixture cannot exercise.
    # Driving the scanner directly with small regions proves the part that only
    # a feature-length source would otherwise reach: several regions really are
    # extracted, transcribed and merged onto one absolute timeline.
    multi_runtime = Path(tempfile.mkdtemp(prefix="batch_multi_"))
    t0 = time.time()
    multi = video_analysis.run_full_source_analysis(
        ffmpeg, source, multi_runtime, source_duration_sec=150.0,
        whisper_model_size="tiny", scene_detector="content", top_n=3,
        region_duration_sec=35.0, max_regions=3,
        preview_dir=multi_runtime / "previews")
    multi_scan = multi["full_source_scan"]
    print(f"     cok bolgeli tarama: {time.time() - t0:.1f}s")
    check(multi_scan["regions_scanned"] == 3, "multi-region-count",
          f"{multi_scan['regions_scanned']} bolge")
    check(len({r["start_sec"] for r in multi_scan["regions"]}) == 3,
          "multi-region-distinct-starts",
          str([r["start_sec"] for r in multi_scan["regions"]]))
    region_indexes = {m["analysis_region"]["index"] for m in multi["hot_moments"]}
    check(len(region_indexes) >= 2, "multi-region-candidates-from-several-regions",
          f"adaylar {sorted(region_indexes)} numarali bolgelerden geldi")
    check(multi_scan["candidates_before_merge"] >= len(multi["hot_moments"]),
          "multi-region-merge-accounted",
          f"{multi_scan['candidates_before_merge']} aday -> {len(multi['hot_moments'])} moment")
    # Ranks are assigned by the GLOBAL score, not by the order regions were
    # scanned -- otherwise the film's opening would always outrank its climax.
    check([m["rank"] for m in multi["hot_moments"]] == [1, 2, 3],
          "multi-region-reranked", str([m["rank"] for m in multi["hot_moments"]]))
    scores = [m["global_score"] for m in multi["hot_moments"]]
    check(scores == sorted(scores, reverse=True), "multi-region-rank-follows-score",
          str(scores))
    check(multi["hot_moments"][0]["analysis_region"]["index"] != 1,
          "multi-region-winner-not-just-first-region",
          f"kazanan bolge {multi['hot_moments'][0]['analysis_region']['index']}")
    check(multi_scan["ranking_policy"] == "full-source-energy-blend-v1",
          "multi-region-ranking-declared", multi_scan["ranking_policy"])
    # Absolute timeline: every merged moment sits inside the real source.
    check(all(0 <= m["source_start_sec"] < m["source_end_sec"] <= 151
              for m in multi["hot_moments"]), "multi-region-absolute-times",
          str([(m["source_start_sec"], m["source_end_sec"]) for m in multi["hot_moments"]]))
    # No overlapping Shorts survive the merge.
    spans = sorted((m["source_start_sec"], m["source_end_sec"]) for m in multi["hot_moments"])
    check(all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1)),
          "multi-region-no-overlap", str(spans))
    check(not list(multi_runtime.glob("*.wav")), "multi-region-scratch-cleaned",
          "tam kaynak WAV'i silindi")
    shutil.rmtree(multi_runtime, ignore_errors=True)

    # ---------------------------------------------------- 8. hard stops intact
    print()
    print("--- 8. Degismeyen sinirlar ---")
    check(fixtures.PROVIDER_CALLS == [], "zero-provider-calls",
          f"{len(fixtures.PROVIDER_CALLS)} cagri")
    for index in range(1, OUTPUT_COUNT + 1):
        manifest = service.load_artifact("render_manifest", slot=index)["data"] or {}
        check(manifest.get("not_uploaded") is True, f"slot{index}-not-uploaded",
              "not_uploaded=True")
        check(manifest.get("status") != "production_ready",
              f"slot{index}-no-production-ready", str(manifest.get("status")))
    check(source.is_file(), "source-untouched", "orijinal kaynak dosya duruyor")
    check(PER_SLOT_ARTIFACTS == frozenset({"moment_selection", "moment_transcript",
                                            "render_manifest"}),
          "per-slot-artifact-set", str(sorted(PER_SLOT_ARTIFACTS)))

    return finish(app, storage_root, staging)


def finish(app, storage_root, staging) -> int:
    print()
    print("-" * 78)
    print(f"toplam {CHECKS} kontrol, {CHECKS - len(FAILURES)} gecti, {len(FAILURES)} basarisiz")
    shutil.rmtree(storage_root, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    if FAILURES:
        print()
        print("BATCH_OUTPUTS_VERIFICATION_FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print()
    print("BATCH_OUTPUTS_OK (izole temp depolama, sentetik kaynak, sahte saglayicilar; "
          "hicbir gercek API cagrilmadi, hicbir sey yuklenmedi/yayinlanmadi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
