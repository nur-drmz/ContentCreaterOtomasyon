"""Canonical post-generation human-approval gate.

WHY THIS MODULE EXISTS
----------------------
Approval must be a SEPARATE step that happens AFTER the user has actually seen
the generated artifact. Passing `script_approved=True` into the function that
generates the script means the artifact is stamped "approved" in the same call
that created it -- before any human could have read it. That is not an approval
gate, it is a rubber stamp.

So the generation entry points in `script_generation.py`, `tts.py` and
`render_utils.py` deliberately accept NO approval argument and can only ever
emit `review_required` (or `failed` / `insufficient_data` /
`development_placeholder`). Promotion to `approved` happens exclusively here,
and only when the caller supplies the SHA-256 of the exact content it reviewed.

BINDING APPROVAL TO CONTENT
---------------------------
Every approval records `approved_content_sha256`, the digest of the reviewed
bytes (script text / audio file / video file). `check_approval()` re-derives
that digest from the artifact's current content, so regenerating a script,
re-running TTS, or re-rendering the video automatically invalidates the previous
approval instead of silently carrying it forward.

Nothing here uploads, publishes, or contacts a network service.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

APPROVABLE_KINDS = ("processing_plan", "script", "voiceover", "transcript", "render")

# The processing plan is a DECISION artifact, not a media artifact: what a human
# reviews is the decision itself (mode, source, moments, theory angle, output
# count, render config), so its content hash is taken over `plan["decision"]`.
# Every field that could change what the pipeline does must live in that block --
# anything outside it is descriptive and does not invalidate an approval.
PROCESSING_PLAN_DECISION_KEY = "decision"

# gTTS is a free, robotic development baseline. Policy: it can never be
# approved or production_ready, no matter what a caller passes in.
NEVER_APPROVABLE_PROVIDERS = {"gtts"}

# A render may only be approved when its ENTIRE upstream chain is approved.
# Approving a video built from an unapproved script and a development-placeholder
# voiceover would launder those unapproved inputs into an "approved" artifact,
# which breaks the artifact contract. Every name here must be present AND True in
# render_manifest["approval"]["conditions"]; a MISSING field counts as False.
REQUIRED_RENDER_CONDITIONS = (
    "technical_validation_passed",
    "script_approved",
    "voiceover_approved",
    "voiceover_hash_matches_script",
)

ORIGINAL_AUDIO_RENDER_CONDITIONS = (
    "technical_validation_passed",
    "moment_selection_approved",
)


def evaluate_render_gate(artifact: dict) -> dict:
    """Evaluates every precondition for approving a render.

    Returns {'unmet': [names], 'conditions': {...}, 'failed_conditions': [...]}.
    An empty 'unmet' list is the ONLY state in which a render may be approved.

    Both sources are validated independently, per the artifact contract:
      - `approval.conditions`      -- each required flag must be exactly True
                                      (missing => False, never "approved")
      - `approval.failed_conditions` -- must be present AND empty
      - `output.validation.passed` -- the live technical result, re-checked here
                                      rather than trusting only the recorded flag
    """
    unmet: list[str] = []

    validation = (artifact.get("output") or {}).get("validation") or {}
    if validation.get("passed") is not True:
        unmet.append("technical_validation_passed")

    approval_block = artifact.get("approval") or {}
    conditions = approval_block.get("conditions") or {}
    pipeline_mode = (artifact.get("config") or {}).get("pipeline_mode", "narrated_recap")
    required_conditions = (
        ORIGINAL_AUDIO_RENDER_CONDITIONS
        if pipeline_mode == "original_audio_subtitles"
        else REQUIRED_RENDER_CONDITIONS
    )
    for name in required_conditions:
        if conditions.get(name) is not True and name not in unmet:
            unmet.append(name)

    failed_conditions = approval_block.get("failed_conditions")
    if not isinstance(failed_conditions, list):
        # Cannot confirm the list is empty -> treat as unmet, never as approved.
        if "failed_conditions_missing" not in unmet:
            unmet.append("failed_conditions_missing")
    else:
        for name in failed_conditions:
            if name not in unmet:
                unmet.append(name)

    return {
        "unmet": unmet,
        "conditions": dict(conditions),
        "failed_conditions": failed_conditions,
        "required_conditions": list(required_conditions),
        "pipeline_mode": pipeline_mode,
    }


def sha256_text(text: str) -> str:
    """Full (untruncated) SHA-256 hex digest of a string's UTF-8 bytes."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_canonical_json(obj) -> str:
    """SHA-256 of an object's CANONICAL JSON encoding (sorted keys, no spaces).

    Canonical so that re-serializing the same decision always yields the same
    digest regardless of key order or whitespace -- otherwise a plan reloaded
    from disk could appear "changed" and invalidate a still-valid approval.
    """
    encoded = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path) -> str:
    """Full (untruncated) SHA-256 hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash_for(kind: str, artifact: dict) -> str:
    """Returns the SHA-256 of the content a human is expected to review for
    this artifact kind:
      processing_plan -> the DECISION block (mode, source, moments, theory, config)
      script          -> the script text itself
      transcript      -> the caption words WITH their timings
      voiceover       -> the generated audio FILE bytes
      render          -> the final video FILE bytes

    Raises ValueError when the artifact carries no reviewable content yet (e.g.
    a prompt-only script package), because there is nothing to approve.
    """
    if kind not in APPROVABLE_KINDS:
        raise ValueError(f"bilinmeyen artefakt turu: {kind!r} (beklenen: {APPROVABLE_KINDS})")

    if kind == "processing_plan":
        decision = artifact.get(PROCESSING_PLAN_DECISION_KEY)
        if not isinstance(decision, dict) or not decision:
            raise ValueError(
                "processing_plan.json icinde 'decision' blogu yok; onaylanacak bir "
                "isleme karari uretilmemis."
            )
        if not decision.get("mode"):
            raise ValueError(
                "processing_plan kararinda 'mode' secilmemis; oneri tek basina secim degildir."
            )
        return sha256_canonical_json(decision)

    if kind == "script":
        script_text = artifact.get("script_text")
        if not script_text:
            raise ValueError(
                "selected_script.json icinde script_text yok (sadece prompt paketi); "
                "onaylanacak bir icerik uretilmemis."
            )
        return sha256_text(script_text)

    if kind == "transcript":
        # The reviewable content is the caption text itself, WITH its word
        # timings: approving "the same words at different times" would approve a
        # caption the reviewer never saw. Corrections are inside the words, so a
        # later correction correctly invalidates the approval.
        words = artifact.get("words") or []
        cues = artifact.get("cues") or []
        if not words and not cues:
            raise ValueError(
                "moment_transcript.json icinde kelime/cue yok; bu momentte konusma "
                "tespit edilmemis, onaylanacak bir altyazi metni uretilmemis."
            )
        payload = ([{"t": w.get("text"), "s": w.get("start"), "e": w.get("end"),
                     "excluded": bool(w.get("excluded_from_subtitles"))} for w in words]
                   or [{"t": c.get("text"), "s": c.get("start"), "e": c.get("end"),
                        "excluded": bool(c.get("excluded_from_subtitles"))} for c in cues])
        return sha256_canonical_json(payload)

    if kind == "voiceover":
        audio_path = artifact.get("audio_path")
        if not audio_path or not Path(audio_path).is_file():
            raise ValueError(f"seslendirme dosyasi bulunamadi: {audio_path!r}")
        return sha256_file(audio_path)

    video_path = (artifact.get("output") or {}).get("final_video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"render dosyasi bulunamadi: {video_path!r}")
    return sha256_file(video_path)


def describe_for_review(kind: str, artifact: dict) -> dict:
    """Returns what the user needs in order to review, including the exact
    hash they must echo back to approve. Never mutates the artifact."""
    info = {
        "kind": kind,
        "artifact_status": artifact.get("status"),
        "content_sha256": None,
        "reviewable": False,
        "blocker": None,
        "blocking_conditions": [],
    }
    try:
        info["content_sha256"] = content_hash_for(kind, artifact)
        info["reviewable"] = True
    except ValueError as exc:
        info["blocker"] = str(exc)
        return info

    if kind == "voiceover" and artifact.get("provider") in NEVER_APPROVABLE_PROVIDERS:
        info["reviewable"] = False
        info["blocker"] = (
            f"saglayici '{artifact.get('provider')}' politika geregi asla onaylanamaz "
            "(development_placeholder)."
        )
    if kind == "render":
        # The video may still be PLAYED for review (content_sha256 stays set, so a
        # separate "I watched it" record can be kept via record_render_review),
        # but it must not be presented as approvable while anything upstream is
        # unapproved.
        gate = evaluate_render_gate(artifact)
        if gate["unmet"]:
            info["reviewable"] = False
            info["blocking_conditions"] = list(gate["unmet"])
            info["blocker"] = (
                "FINAL ONAY ENGELLI -- saglanmayan onay kosullari: "
                f"{', '.join(gate['unmet'])}. Render onayi ancak bu moda ait tum "
                "teknik ve yukari-akis kosullari tamamlandiginda verilebilir."
            )
    return info


def approve_artifact(artifact: dict, kind: str, reviewed_content_sha256: str,
                      approved_by: str, notes: str = "") -> dict:
    """Promotes `artifact` to status='approved' -- but ONLY if
    `reviewed_content_sha256` matches the artifact's current content digest.

    Returns a NEW dict (the input is not mutated). Raises ValueError on any
    refusal, so a failed approval can never be mistaken for a successful one.

    The hash argument is mandatory and is the whole point: the caller must have
    looked at the review output (which prints the digest) to obtain it. A stale
    or guessed hash is rejected.

    For kind='render' the ENTIRE upstream chain must additionally be approved
    (see REQUIRED_RENDER_CONDITIONS). Approving a video built from an unapproved
    script or a development-placeholder voiceover is refused outright -- that
    would launder unapproved inputs into an "approved" artifact.
    """
    if kind not in APPROVABLE_KINDS:
        raise ValueError(f"bilinmeyen artefakt turu: {kind!r}")
    if not approved_by:
        raise ValueError("approved_by zorunlu: onayi kimin verdigi kayda gecmeli.")
    if not reviewed_content_sha256:
        raise ValueError(
            "reviewed_content_sha256 zorunlu. Once inceleme hucresini calistirip "
            "gosterilen SHA-256 degerini buraya yaz -- bu, icerigi gercekten "
            "gordugunun kaydidir."
        )

    # Checked BEFORE anything else for renders, so the refusal names the exact
    # unmet conditions. Raising here leaves the caller's artifact untouched.
    if kind == "render":
        gate = evaluate_render_gate(artifact)
        if gate["unmet"]:
            raise ValueError(
                "ONAY REDDEDILDI: render onayi icin gerekli kosullar saglanmadi -- "
                f"saglanmayan kosullar: {gate['unmet']}. "
                "Render ancak secili pipeline modunun teknik ve yukari-akis "
                "onaylari tamamlandiginda onaylanabilir. "
                "Sadece izledigini kaydetmek istiyorsan record_render_review() kullan."
            )

    current_hash = content_hash_for(kind, artifact)  # raises if nothing reviewable
    if reviewed_content_sha256.strip().lower() != current_hash.lower():
        raise ValueError(
            f"ONAY REDDEDILDI: incelendigi bildirilen icerik hash'i ({reviewed_content_sha256[:16]}...) "
            f"artefaktin SUANKI icerigiyle ({current_hash[:16]}...) eslesmiyor. Icerik inceledikten "
            "sonra yeniden uretilmis olabilir; inceleme hucresini tekrar calistir ve yeni hash'i kullan."
        )

    if kind == "voiceover" and artifact.get("provider") in NEVER_APPROVABLE_PROVIDERS:
        raise ValueError(
            f"ONAY REDDEDILDI: saglayici '{artifact.get('provider')}' politika geregi asla "
            "onaylanamaz (development_placeholder olarak kalir)."
        )

    approved = dict(artifact)
    approved["status"] = "approved"
    approved["status_reason"] = f"human approved after review by {approved_by}"
    approved["human_approval"] = {
        "approved": True,
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "notes": notes or None,
        "approved_content_sha256": current_hash,
    }
    return approved


def check_approval(artifact: dict, kind: str) -> dict:
    """Re-derives the artifact's content digest and compares it to the digest
    recorded at approval time.

    Returns {'approved', 'valid', 'reason'}:
      approved=False              -> never approved (nothing to invalidate)
      approved=True, valid=True   -> approval still binds the current content
      approved=True, valid=False  -> content changed since approval; the
                                     approval is STALE and must not be trusted

    For kind='render' the upstream approval conditions are ALSO re-evaluated, so
    an approval goes invalid when e.g. the script approval is later revoked --
    even though the video bytes themselves never changed.
    """
    result = {"approved": False, "valid": False, "reason": ""}
    if artifact.get("status") != "approved":
        result["reason"] = f"artefakt onayli degil (status={artifact.get('status')!r})"
        return result

    result["approved"] = True
    recorded = (artifact.get("human_approval") or {}).get("approved_content_sha256")
    if not recorded:
        result["reason"] = (
            "status='approved' ama approved_content_sha256 kaydi yok; bu onay "
            "icerige baglanmamis, gecersiz sayilmali."
        )
        return result

    try:
        current = content_hash_for(kind, artifact)
    except ValueError as exc:
        result["reason"] = f"suanki icerik hash'i hesaplanamadi: {exc}"
        return result

    if current.lower() != recorded.lower():
        result["reason"] = (
            f"ONAY GECERSIZ: icerik onaydan sonra degismis "
            f"(onaylanan={recorded[:16]}..., suanki={current[:16]}...). Yeniden incelenip onaylanmali."
        )
        return result

    # Video bytes unchanged is NOT sufficient for a render: the upstream chain
    # must still be approved and technical validation must still pass.
    if kind == "render":
        gate = evaluate_render_gate(artifact)
        if gate["unmet"]:
            result["reason"] = (
                "ONAY GECERSIZ: video degismemis olsa da render onay kosullari artik "
                f"saglanmiyor -- saglanmayan kosullar: {gate['unmet']}."
            )
            return result

    result["valid"] = True
    result["reason"] = "onay suanki icerige bagli ve gecerli"
    return result


def record_render_review(artifact: dict, reviewed_content_sha256: str, reviewed_by: str,
                          notes: str = "") -> dict:
    """Records that a human WATCHED this render, WITHOUT approving it.

    This exists so the fact that a review happened is not lost when the upstream
    chain is still unapproved. It writes a separate `render_review` block and
    deliberately does NOT touch `status` or `human_approval` -- a watched render
    whose script/voiceover are unapproved stays `review_required`.

    Returns a NEW dict; raises ValueError if the reviewed hash does not match the
    video currently on disk.
    """
    if not reviewed_by:
        raise ValueError("reviewed_by zorunlu: incelemeyi kimin yaptigi kayda gecmeli.")
    current_hash = content_hash_for("render", artifact)
    if not reviewed_content_sha256 or reviewed_content_sha256.strip().lower() != current_hash.lower():
        raise ValueError(
            f"INCELEME KAYDI REDDEDILDI: bildirilen hash ({str(reviewed_content_sha256)[:16]}...) "
            f"suanki video ({current_hash[:16]}...) ile eslesmiyor."
        )

    reviewed = dict(artifact)
    reviewed["render_review"] = {
        "reviewed": True,
        "reviewed_by": reviewed_by,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_content_sha256": current_hash,
        "notes": notes or None,
    }
    # status intentionally untouched.
    return reviewed


def load_and_check(path, kind: str) -> tuple[dict, dict]:
    """Convenience: loads a JSON artifact from disk and returns
    (artifact, check_approval(...)) so a downstream phase can refuse to treat a
    stale approval as valid."""
    with open(path, "r", encoding="utf-8") as handle:
        artifact = json.load(handle)
    return artifact, check_approval(artifact, kind)
