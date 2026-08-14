"""Phase -1 -- trend-driven source ingest with a mandatory rights gate.

WHY THIS MODULE EXISTS
----------------------
Until now the main pipeline could only start from a file the user had already
placed on disk, and `youtube_data.py` discovery was a pure research sidecar that
was FORBIDDEN from feeding the main pipeline. That made the product's actual
goal -- "find a trending video, cut Shorts from it, publish them" -- impossible.

This module opens that path deliberately and narrowly:

    discovery result  ->  source_candidates.json   (suggestions, review_required)
    one candidate     ->  candidate_probe.json     (metadata only, NO download)
    human decision    ->  rights_decision.json     (license type + who decided)
    rights decision   ->  ingest_manifest.json     (the downloaded local file)

POLICY (do not weaken when editing this file)
---------------------------------------------
- A candidate is a SUGGESTION. `build_source_candidates()` never marks one
  selected; `selected_candidate_url` is always None and `auto_selected` False.
- `download_source()` REFUSES to run without a valid `rights_decision` whose
  `decided_by` is recorded and whose target URL matches the download target.
  There is no flag, kwarg or env var that bypasses this.
- Probing is metadata-only and never writes media. Downloading is the only
  operation that touches third-party bytes, and it is always an explicit,
  separately-recorded human act.
- The rights decision is the user's legal decision, not the software's claim.
  Every ingest manifest carries the license type, the attribution text, and an
  explicit Content-ID / terms-of-service risk notice.
- Downloaded media stays LOCAL. This module never sends it to any provider; the
  existing `ALLOW_RAW_MEDIA_TO_FREE_TIER = False` policy is unaffected.
- Nothing here uploads, publishes, or reads a secret. yt-dlp is injected so the
  whole module is testable without network access.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .approval import sha256_canonical_json

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

#: The four honest answers to "why are you allowed to use this footage?".
#: `fair_use_claim` is explicitly a CLAIM BY THE USER, not a legal finding by
#: this software -- it is stored as such so the record cannot be misread later.
LICENSE_OWN = "own"
LICENSE_CC_BY = "cc_by"
LICENSE_LICENSED = "licensed"
LICENSE_FAIR_USE_CLAIM = "fair_use_claim"
LICENSE_TYPES = (LICENSE_OWN, LICENSE_CC_BY, LICENSE_LICENSED, LICENSE_FAIR_USE_CLAIM)

#: License types that describe third-party footage and therefore REQUIRE a
#: non-empty attribution text plus an acknowledged risk notice.
THIRD_PARTY_LICENSE_TYPES = (LICENSE_CC_BY, LICENSE_LICENSED, LICENSE_FAIR_USE_CLAIM)

MIN_ATTRIBUTION_CHARS = 10
MIN_TRANSFORMATION_NOTE_CHARS = 20

#: Ingest guard rails. A 6-hour livestream VOD would exhaust local disk and make
#: every downstream phase unusable, so the limits are refused UP FRONT rather
#: than discovered halfway through a download.
DEFAULT_MAX_DURATION_SEC = 4 * 3600
DEFAULT_MAX_FILESIZE_BYTES = 8 * 1024 * 1024 * 1024

#: 1080p-capped MP4-preferred selection. Higher resolutions cost disk and render
#: time without improving a 1080x1920 Short.
DEFAULT_FORMAT_SELECTOR = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
)

CONTENT_ID_RISK_NOTICE = (
    "UYARI: ucuncu taraf icerigi indirmek ve yeniden yayimlamak platform "
    "hizmet sartlarini ihlal edebilir, Content-ID talebi, gelir kaybi, video "
    "kaldirma veya kanal kapatma ile sonuclanabilir. Bu karar ve sorumlulugu "
    "tamamen kullaniciya aittir; bu yazilim hukuki bir degerlendirme yapmaz."
)

_URL_PATTERN = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

#: yt-dlp error text can embed cookies, headers or signed URLs. Anything that
#: looks like a credential is stripped before it reaches an artifact or a log.
_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)\b(key|token|access_token|refresh_token|secret|signature|sig|cookie)=([^&\s\"']+)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_ingest_error(text) -> str:
    """Strips credential-shaped query parameters from downloader error text.

    yt-dlp routinely reports the full signed media URL on failure, which can
    contain signature/token parameters. Those must never reach an artifact.
    """
    return _SECRET_QUERY_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", str(text))


def is_supported_source_url(value) -> bool:
    """True for a plain http(s) URL. Deliberately permissive about the host:
    the user asked to be able to ingest ANY video, not a hardcoded allowlist of
    platforms. The rights gate -- not a host check -- is what protects them."""
    return bool(value) and bool(_URL_PATTERN.match(str(value).strip()))


def _atomic_write_json(path, payload: dict) -> Path:
    """Write-to-temp + os.replace, mirroring the Cache/quota-ledger pattern used
    elsewhere in `local_pipeline`, so a crash mid-write cannot leave a truncated
    artifact that a later phase would happily parse."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path


# ---------------------------------------------------------------------------
# Discovery result -> source candidates (suggestions only)
# ---------------------------------------------------------------------------

def watch_url_for_video_id(video_id) -> str | None:
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def build_source_candidates(discovery_result: dict | None, *, max_candidates: int = 25,
                            manual_urls=None, generated_at: str | None = None) -> dict:
    """Turns a `youtube_data.discover_trending_content()` result into ranked
    SOURCE CANDIDATES, optionally merged with URLs the user pasted in manually.

    This is the single point where the research sidecar becomes allowed to
    influence the main pipeline -- and it does so only by SUGGESTING. The
    artifact records `feeds_main_pipeline: True` (it is honest about its new
    role) together with `requires_rights_decision: True`, because a suggestion
    still cannot be downloaded without a human license decision.
    """
    videos = list((discovery_result or {}).get("videos") or [])
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for row in videos[: max(0, int(max_candidates))]:
        url = watch_url_for_video_id(row.get("video_id"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            "candidate_url": url,
            "origin": "trend_discovery",
            "video_id": row.get("video_id"),
            "title": row.get("title"),
            "channel_id": row.get("channel_id"),
            "channel_title": row.get("channel_title"),
            "published_at": row.get("published_at"),
            "duration_sec": row.get("duration_sec"),
            "view_count": row.get("view_count"),
            "trend_score": row.get("trend_score"),
            "views_per_hour": row.get("views_per_hour"),
            "matched_terms": row.get("matched_terms") or [],
            "discovery_rank": row.get("discovery_rank"),
            "license_hint": None,
            "rights_decision_required": True,
        })

    invalid_manual: list[str] = []
    for raw in manual_urls or []:
        url = str(raw).strip()
        if not is_supported_source_url(url):
            invalid_manual.append(url)
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            "candidate_url": url,
            "origin": "manual_url",
            "video_id": None,
            "title": None,
            "channel_id": None,
            "channel_title": None,
            "published_at": None,
            "duration_sec": None,
            "view_count": None,
            "trend_score": None,
            "views_per_hour": None,
            "matched_terms": [],
            "discovery_rank": None,
            "license_hint": None,
            "rights_decision_required": True,
        })

    if candidates:
        status = "review_required"
        status_reason = (
            f"{len(candidates)} kaynak adayi listelendi; hicbiri secilmedi. "
            "Indirme icin once haklar karari verilmeli."
        )
    else:
        status = "insufficient_data"
        status_reason = "kaynak adayi uretilemedi (kesif sonucu bos ve manuel URL verilmedi)"

    return {
        "status": status,
        "status_reason": status_reason,
        "pipeline_role": "source_candidate_shortlist",
        # Honest about the deliberate policy change: discovery may now SUGGEST a
        # source. It still cannot select, download or approve one.
        "feeds_main_pipeline": True,
        "auto_selected": False,
        "selected_candidate_url": None,
        "requires_rights_decision": True,
        "generated_at": generated_at or _now_iso(),
        "candidate_count": len(candidates),
        "invalid_manual_urls": invalid_manual,
        "candidates": candidates,
        "rights_notice": CONTENT_ID_RISK_NOTICE,
        "license_types": list(LICENSE_TYPES),
    }


# ---------------------------------------------------------------------------
# Metadata-only probe (no media is written)
# ---------------------------------------------------------------------------

def _load_ytdlp(ytdlp_module=None):
    """Returns the yt_dlp module, or raises an actionable error.

    Injectable so every test in this repo can exercise the real control flow
    with a double instead of hitting the network.
    """
    if ytdlp_module is not None:
        return ytdlp_module
    try:
        import yt_dlp  # noqa: PLC0415  (optional dependency, imported on demand)
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp kurulu degil. Kurulum: pip install -r requirements-local.txt"
        ) from exc
    return yt_dlp


def _extract_info(ytdlp_module, url: str, options: dict, *, download: bool):
    with ytdlp_module.YoutubeDL(options) as downloader:
        return downloader.extract_info(url, download=download)


def probe_candidate(url: str, *, ytdlp_module=None, extra_options: dict | None = None,
                    probed_at: str | None = None) -> dict:
    """Reads duration / resolution / availability for ONE candidate WITHOUT
    downloading it, so a human can judge suitability and cost before any
    third-party bytes touch the disk.

    Never raises for a normal failure (private/removed/geo-blocked video): the
    result carries `status='failed'` and a sanitized reason instead.
    """
    if not is_supported_source_url(url):
        return {
            "status": "failed",
            "status_reason": f"gecersiz kaynak URL: {url!r}",
            "candidate_url": url,
            "probed_at": probed_at or _now_iso(),
            "downloaded": False,
        }

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    options.update(extra_options or {})

    try:
        ytdlp = _load_ytdlp(ytdlp_module)
        info = _extract_info(ytdlp, url, options, download=False) or {}
    except Exception as exc:
        return {
            "status": "failed",
            "status_reason": sanitize_ingest_error(exc),
            "candidate_url": url,
            "probed_at": probed_at or _now_iso(),
            "downloaded": False,
        }

    duration = info.get("duration")
    record = {
        "status": "review_required",
        "status_reason": (
            "kaynak meta verisi okundu; hicbir medya indirilmedi. Indirme icin "
            "haklar karari gerekli."
        ),
        "candidate_url": url,
        "probed_at": probed_at or _now_iso(),
        "downloaded": False,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "source_id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "uploader_url": info.get("uploader_url") or info.get("channel_url"),
        "duration_sec": duration,
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "age_limit": info.get("age_limit"),
        "availability": info.get("availability"),
        # yt-dlp reports the platform's own license label when it exposes one.
        # It is a HINT for the human decision, never the decision itself.
        "reported_license": info.get("license"),
        "has_subtitles": bool(info.get("subtitles")),
        "has_automatic_captions": bool(info.get("automatic_captions")),
        "estimated_filesize_bytes": info.get("filesize") or info.get("filesize_approx"),
        "rights_decision_required": True,
        "rights_notice": CONTENT_ID_RISK_NOTICE,
    }

    blockers = []
    if record["is_live"]:
        blockers.append("kaynak canli yayin; once yayin bitmeli")
    if duration is None:
        blockers.append("kaynak suresi okunamadi")
    record["ingest_blockers"] = blockers
    if blockers:
        record["status"] = "insufficient_data"
        record["status_reason"] = f"indirmeye uygun degil: {'; '.join(blockers)}"
    return record


# ---------------------------------------------------------------------------
# Rights gate
# ---------------------------------------------------------------------------

def build_rights_decision(candidate_url: str, license_type: str, decided_by: str, *,
                          attribution: str = "", transformation_note: str = "",
                          risk_acknowledged: bool = False, source_title: str = "",
                          source_channel: str = "", notes: str = "",
                          decided_at: str | None = None) -> dict:
    """Records the human's answer to "why may this footage be used?".

    Raises ValueError on anything incomplete, because a half-filled rights
    record that still unlocks a download would be worse than no gate at all.
    The returned artifact is born `approved` -- unlike media artifacts there is
    nothing further to review here: the human decision IS the content, and it is
    bound to `decision_sha256` so editing it later invalidates every ingest that
    referenced it.
    """
    license_type = (license_type or "").strip().lower()
    if license_type not in LICENSE_TYPES:
        raise ValueError(
            f"gecersiz license_type: {license_type!r}. Beklenen: {LICENSE_TYPES}"
        )
    if not is_supported_source_url(candidate_url):
        raise ValueError(f"gecersiz kaynak URL: {candidate_url!r}")
    if not (decided_by or "").strip():
        raise ValueError("decided_by zorunlu: haklar kararini kimin verdigi kayda gecmeli.")

    attribution = (attribution or "").strip()
    transformation_note = (transformation_note or "").strip()

    if license_type in THIRD_PARTY_LICENSE_TYPES:
        if len(attribution) < MIN_ATTRIBUTION_CHARS:
            raise ValueError(
                f"license_type={license_type} icin attribution zorunlu "
                f"(en az {MIN_ATTRIBUTION_CHARS} karakter): kaynak sahibi ve link yaz."
            )
        if risk_acknowledged is not True:
            raise ValueError(
                "risk_acknowledged=True zorunlu: ucuncu taraf icerigi icin "
                "Content-ID / hizmet sartlari riski aciklikla kabul edilmeli. "
                + CONTENT_ID_RISK_NOTICE
            )
    if license_type == LICENSE_FAIR_USE_CLAIM and len(transformation_note) < MIN_TRANSFORMATION_NOTE_CHARS:
        raise ValueError(
            "license_type=fair_use_claim icin transformation_note zorunlu "
            f"(en az {MIN_TRANSFORMATION_NOTE_CHARS} karakter): icerigin nasil "
            "donusturuldugu (yorum, analiz, elestiri) yazilmali. Bu bir hukuki "
            "gorus degil, kullanicinin beyanidir."
        )

    decision = {
        "candidate_url": candidate_url.strip(),
        "license_type": license_type,
        "attribution": attribution or None,
        "transformation_note": transformation_note or None,
        "risk_acknowledged": bool(risk_acknowledged),
        "source_title": (source_title or "").strip() or None,
        "source_channel": (source_channel or "").strip() or None,
    }
    return {
        "status": "approved",
        "status_reason": f"haklar karari {decided_by} tarafindan verildi",
        "artifact_kind": "rights_decision",
        "decision": decision,
        "decision_sha256": sha256_canonical_json(decision),
        "decided_by": decided_by.strip(),
        "decided_at": decided_at or _now_iso(),
        "notes": (notes or "").strip() or None,
        "rights_notice": CONTENT_ID_RISK_NOTICE,
        "legal_disclaimer": (
            "Bu kayit kullanicinin beyanidir. Bu yazilim telif, adil kullanim "
            "veya lisans uygunlugu hakkinda hukuki bir degerlendirme yapmaz."
        ),
    }


def validate_rights_decision(rights_decision, candidate_url: str | None = None) -> list[str]:
    """Reasons a persisted rights decision must not unlock a download.

    Returns a list of human-readable errors; an empty list is the ONLY state in
    which `download_source()` proceeds.
    """
    errors: list[str] = []
    if not isinstance(rights_decision, dict) or not rights_decision:
        return ["haklar karari yok: indirme icin rights_decision zorunlu"]

    if rights_decision.get("artifact_kind") != "rights_decision":
        errors.append("artefakt bir rights_decision degil")
    if rights_decision.get("status") != "approved":
        errors.append(f"haklar karari onayli degil (status={rights_decision.get('status')!r})")
    if not (rights_decision.get("decided_by") or "").strip():
        errors.append("decided_by kaydi yok: kararin sahibi belirsiz")

    decision = rights_decision.get("decision")
    if not isinstance(decision, dict) or not decision:
        errors.append("rights_decision icinde 'decision' blogu yok")
        return errors

    recorded = rights_decision.get("decision_sha256")
    current = sha256_canonical_json(decision)
    if not recorded:
        errors.append("decision_sha256 yok: karar icerigine baglanmamis")
    elif str(recorded).lower() != current.lower():
        errors.append(
            f"HAKLAR KARARI GECERSIZ: karar icerigi degismis "
            f"(kayitli={str(recorded)[:16]}..., suanki={current[:16]}...)"
        )

    if decision.get("license_type") not in LICENSE_TYPES:
        errors.append(f"gecersiz license_type: {decision.get('license_type')!r}")
    if decision.get("license_type") in THIRD_PARTY_LICENSE_TYPES:
        if not (decision.get("attribution") or "").strip():
            errors.append("ucuncu taraf lisansi icin attribution bos")
        if decision.get("risk_acknowledged") is not True:
            errors.append("ucuncu taraf lisansi icin risk_acknowledged kaydi yok")

    if candidate_url is not None:
        decided_url = (decision.get("candidate_url") or "").strip()
        if decided_url != str(candidate_url).strip():
            errors.append(
                "HAKLAR KARARI BASKA BIR KAYNAK ICIN VERILMIS: "
                f"karar={decided_url!r}, indirilmek istenen={str(candidate_url).strip()!r}"
            )
    return errors


# ---------------------------------------------------------------------------
# Download (the only operation that touches third-party bytes)
# ---------------------------------------------------------------------------

def _resolve_downloaded_path(info: dict, output_dir: Path) -> Path | None:
    """Finds the file yt-dlp actually produced.

    `requested_downloads[0].filepath` is authoritative when present (it already
    accounts for merging/remuxing changing the extension); the other keys are
    fallbacks for older/edge-case extractor output.
    """
    requested = (info or {}).get("requested_downloads") or []
    for entry in requested:
        for key in ("filepath", "_filename", "filename"):
            value = (entry or {}).get(key)
            if value and Path(value).is_file():
                return Path(value)
    for key in ("filepath", "_filename", "filename"):
        value = (info or {}).get(key)
        if value and Path(value).is_file():
            return Path(value)
    candidates = sorted(
        (p for p in output_dir.glob("*") if p.is_file() and not p.name.endswith(".part")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def download_source(candidate_url: str, output_dir, rights_decision: dict, *,
                    downloaded_by: str = "",
                    ytdlp_module=None,
                    format_selector: str = DEFAULT_FORMAT_SELECTOR,
                    max_duration_sec: int = DEFAULT_MAX_DURATION_SEC,
                    max_filesize_bytes: int = DEFAULT_MAX_FILESIZE_BYTES,
                    probe_record: dict | None = None,
                    extra_options: dict | None = None,
                    progress_hook=None,
                    started_at: str | None = None) -> dict:
    """Downloads ONE candidate into `output_dir` and returns an ingest manifest.

    Raises PermissionError when the rights gate is not satisfied -- a refused
    download must be impossible to mistake for a successful one, so it is an
    exception rather than a `status='failed'` record.

    The returned manifest is `review_required`: the file exists locally, but
    Phase 0 (`source_strategy.assess_source`) still has to accept it and a human
    still has to choose a processing mode.
    """
    started_at = started_at or _now_iso()
    rights_errors = validate_rights_decision(rights_decision, candidate_url)
    if rights_errors:
        raise PermissionError(
            "INDIRME REDDEDILDI -- haklar kapisi saglanmadi: "
            + "; ".join(rights_errors)
            + ". " + CONTENT_ID_RISK_NOTICE
        )
    if not is_supported_source_url(candidate_url):
        raise ValueError(f"gecersiz kaynak URL: {candidate_url!r}")

    probe_record = probe_record or {}
    probed_duration = probe_record.get("duration_sec")
    if probed_duration is not None and max_duration_sec and probed_duration > max_duration_sec:
        raise ValueError(
            f"kaynak suresi limiti asiyor: {probed_duration}s > {max_duration_sec}s. "
            "Once kisa bir kaynak sec veya max_duration_sec degerini bilincli olarak yukselt."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": format_selector,
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        # Fail fast instead of streaming gigabytes past the guard rail.
        "max_filesize": int(max_filesize_bytes) if max_filesize_bytes else None,
        "retries": 3,
        "overwrites": False,
    }
    if progress_hook is not None:
        options["progress_hooks"] = [progress_hook]
    options.update(extra_options or {})

    manifest = {
        "status": "failed",
        "status_reason": "",
        "artifact_kind": "ingest_manifest",
        "pipeline_role": "source_ingest",
        "feeds_main_pipeline": True,
        "candidate_url": candidate_url.strip(),
        "started_at": started_at,
        "completed_at": None,
        "downloaded_by": (downloaded_by or "").strip() or None,
        "source_path": None,
        "filesize_bytes": None,
        "duration_sec": probed_duration,
        "width": probe_record.get("width"),
        "height": probe_record.get("height"),
        "extractor": probe_record.get("extractor"),
        "source_id": probe_record.get("source_id"),
        "title": probe_record.get("title"),
        "uploader": probe_record.get("uploader"),
        "format_selector": format_selector,
        "limits": {
            "max_duration_sec": int(max_duration_sec) if max_duration_sec else None,
            "max_filesize_bytes": int(max_filesize_bytes) if max_filesize_bytes else None,
        },
        # The rights record is referenced by HASH, so a later edit to the
        # decision is detectable against every ingest that relied on it.
        "rights": {
            "license_type": (rights_decision.get("decision") or {}).get("license_type"),
            "attribution": (rights_decision.get("decision") or {}).get("attribution"),
            "transformation_note": (rights_decision.get("decision") or {}).get("transformation_note"),
            "decided_by": rights_decision.get("decided_by"),
            "decided_at": rights_decision.get("decided_at"),
            "decision_sha256": rights_decision.get("decision_sha256"),
        },
        "rights_notice": CONTENT_ID_RISK_NOTICE,
        "raw_media_submitted_to_provider": False,
        "error": None,
    }

    try:
        ytdlp = _load_ytdlp(ytdlp_module)
        info = _extract_info(ytdlp, candidate_url, options, download=True) or {}
    except Exception as exc:
        manifest["status_reason"] = "indirme basarisiz"
        manifest["error"] = sanitize_ingest_error(exc)
        manifest["completed_at"] = _now_iso()
        return manifest

    path = _resolve_downloaded_path(info, output_dir)
    if path is None:
        manifest["status_reason"] = (
            "indirme tamamlandigini bildirdi fakat diskte dosya bulunamadi"
        )
        manifest["completed_at"] = _now_iso()
        return manifest

    size = path.stat().st_size
    if size <= 0:
        manifest["status_reason"] = f"indirilen dosya bos: {path.name}"
        manifest["completed_at"] = _now_iso()
        return manifest

    manifest.update({
        "status": "review_required",
        "status_reason": (
            "kaynak yerel diske indirildi. Faz 0 kaynak degerlendirmesi ve mod "
            "secimi yapilmadan uretime giremez."
        ),
        "source_path": str(path),
        "filesize_bytes": size,
        "duration_sec": info.get("duration", probed_duration),
        "width": info.get("width", manifest["width"]),
        "height": info.get("height", manifest["height"]),
        "extractor": info.get("extractor_key") or info.get("extractor") or manifest["extractor"],
        "source_id": info.get("id") or manifest["source_id"],
        "title": info.get("title") or manifest["title"],
        "uploader": info.get("uploader") or info.get("channel") or manifest["uploader"],
        "completed_at": _now_iso(),
    })
    return manifest


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------

def write_source_candidates(candidates_artifact: dict, results_dir) -> Path:
    return _atomic_write_json(Path(results_dir) / "source_candidates.json", candidates_artifact)


def write_rights_decision(rights_decision: dict, results_dir) -> Path:
    return _atomic_write_json(Path(results_dir) / "rights_decision.json", rights_decision)


def write_ingest_manifest(manifest: dict, results_dir) -> Path:
    return _atomic_write_json(Path(results_dir) / "ingest_manifest.json", manifest)


def load_json_artifact(path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Human-facing panel (no secret, no auto-selection)
# ---------------------------------------------------------------------------

def format_candidates_panel(candidates_artifact: dict, limit: int = 10) -> str:
    """Console/notebook panel listing candidates with an explicit reminder that
    nothing has been selected and nothing may be downloaded yet."""
    lines = [
        "KAYNAK ADAYLARI (oneri -- secim yapilmadi)",
        f"  durum          : {candidates_artifact.get('status')}",
        f"  aday sayisi    : {candidates_artifact.get('candidate_count')}",
        "",
    ]
    for row in (candidates_artifact.get("candidates") or [])[: max(0, int(limit))]:
        duration = row.get("duration_sec")
        lines.append(
            f"  #{row.get('discovery_rank') or '-'} [{row.get('origin')}] "
            f"{row.get('title') or row.get('candidate_url')}"
        )
        lines.append(
            f"      kanal={row.get('channel_title') or '-'} "
            f"sure={'-' if duration is None else str(duration) + 's'} "
            f"trend={row.get('trend_score') or '-'}"
        )
        lines.append(f"      url={row.get('candidate_url')}")
    lines += [
        "",
        f"  lisans secenekleri: {', '.join(LICENSE_TYPES)}",
        "  INDIRME KAPALI: build_rights_decision(...) ile haklar karari verilmeden",
        "  download_source(...) calismaz.",
        f"  {CONTENT_ID_RISK_NOTICE}",
    ]
    return "\n".join(lines)
