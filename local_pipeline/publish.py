"""Provider-agnostic publication contract and its human gates.

WHY THIS MODULE EXISTS
----------------------
Publishing is the first operation in this project that is IRREVERSIBLE from the
outside world's point of view: once a video is public, deleting it does not undo
the impressions, the strikes or the Content-ID claim. So publication is not one
step but THREE recorded human decisions, and this module owns the contract for
all of them regardless of platform:

    1. render approval            (existing, `approval.approve_artifact`)
    2. publish intent approval    -> upload as PRIVATE only
    3. visibility approval        -> private -> public / scheduled

`PublishProvider` exists so YouTube is not special-cased: TikTok, Instagram
Reels, Facebook or an aggregator implement the same three methods and inherit
the same gates for free.

POLICY (do not weaken when editing this file)
---------------------------------------------
- `INITIAL_PRIVACY` is `private`. No provider may perform its first upload with
  any other visibility, whatever the caller asks for.
- `validate_publish_preconditions()` is the single source of truth for "may this
  artifact leave the machine". It re-derives the render approval instead of
  trusting a recorded flag, and it refuses a render whose rights record is
  missing when the source was ingested from a third party.
- Every gate is bound to a SHA-256 of the exact thing the human reviewed, using
  the same `approval` helpers as the rest of the pipeline. Editing the metadata
  after approving publication invalidates that approval.
- Nothing here contacts a network service or reads a secret; providers do that.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .approval import check_approval, sha256_canonical_json, sha256_file

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

#: Non-negotiable: the first upload of any artifact, on any platform, is private.
INITIAL_PRIVACY = "private"

VISIBILITY_PRIVATE = "private"
VISIBILITY_UNLISTED = "unlisted"
VISIBILITY_PUBLIC = "public"
VISIBILITY_VALUES = (VISIBILITY_PRIVATE, VISIBILITY_UNLISTED, VISIBILITY_PUBLIC)

#: Visibility changes that expose content to an audience and therefore need
#: their own, separate human decision.
AUDIENCE_EXPOSING_VISIBILITIES = (VISIBILITY_UNLISTED, VISIBILITY_PUBLIC)

PLATFORM_YOUTUBE = "youtube"
#: Declared but NOT implemented. Listing them here keeps the roadmap honest
#: without pretending a provider exists; `resolve_provider` refuses them.
PLANNED_PLATFORMS = ("tiktok", "instagram", "facebook", "x")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PublishTarget:
    """Where and how ONE artifact should be published.

    `visibility` is the visibility the human eventually WANTS. It is not what the
    first upload uses -- that is always `INITIAL_PRIVACY`.
    """
    platform: str
    account_ref: str = ""
    visibility: str = VISIBILITY_PRIVATE
    publish_at: str | None = None
    made_for_kids: bool = False
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.platform:
            raise ValueError("PublishTarget.platform zorunlu")
        if self.visibility not in VISIBILITY_VALUES:
            raise ValueError(
                f"gecersiz visibility: {self.visibility!r} (beklenen: {VISIBILITY_VALUES})"
            )
        if self.publish_at and self.visibility != VISIBILITY_PRIVATE:
            # YouTube's own rule, generalized: a scheduled video must be private
            # until its scheduled time, otherwise the schedule is meaningless.
            raise ValueError(
                "publish_at verildiginde visibility='private' olmali; zamanlanmis "
                "yayin, zamani gelene kadar private kalir."
            )


@dataclass(frozen=True)
class PublishResult:
    """The outcome of one publish attempt. `status` uses the project-wide
    vocabulary; a provider may never invent a success it did not observe."""
    platform: str
    status: str
    status_reason: str = ""
    remote_id: str | None = None
    remote_url: str | None = None
    visibility: str | None = None
    published_at: str | None = None
    quota_units_estimated: int = 0
    error: str | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "status": self.status,
            "status_reason": self.status_reason,
            "remote_id": self.remote_id,
            "remote_url": self.remote_url,
            "visibility": self.visibility,
            "published_at": self.published_at,
            "quota_units_estimated": self.quota_units_estimated,
            "error": self.error,
            "raw": dict(self.raw or {}),
        }


class PublishProvider:
    """Interface every platform adapter implements.

    Subclasses MUST NOT implement their own gate logic: they are only reached
    after `validate_publish_preconditions()` and the two approval gates in this
    module have already passed.
    """

    platform = ""

    def describe_credential_state(self) -> dict:
        """Returns {'configured': bool, ...} -- NEVER a credential value."""
        raise NotImplementedError

    def upload_private(self, video_path, metadata: dict, target: PublishTarget,
                       progress_callback=None) -> PublishResult:
        """Uploads the file with visibility=INITIAL_PRIVACY."""
        raise NotImplementedError

    def set_visibility(self, remote_id: str, visibility: str,
                       publish_at: str | None = None) -> PublishResult:
        """Changes visibility of an already-uploaded item."""
        raise NotImplementedError


_PROVIDER_REGISTRY: dict[str, PublishProvider] = {}


def register_provider(provider: PublishProvider) -> None:
    if not getattr(provider, "platform", ""):
        raise ValueError("provider.platform zorunlu")
    _PROVIDER_REGISTRY[provider.platform] = provider


def resolve_provider(platform: str) -> PublishProvider:
    """Returns the registered provider, or raises an honest error.

    A planned-but-unimplemented platform gets a distinct message so it can never
    be mistaken for a misconfiguration.
    """
    provider = _PROVIDER_REGISTRY.get(platform)
    if provider is not None:
        return provider
    if platform in PLANNED_PLATFORMS:
        raise NotImplementedError(
            f"platform '{platform}' henuz uygulanmadi (planlandi). Su an yalnizca "
            f"'{PLATFORM_YOUTUBE}' yayin saglayicisi mevcuttur."
        )
    raise ValueError(f"bilinmeyen yayin platformu: {platform!r}")


def registered_platforms() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDER_REGISTRY))


# ---------------------------------------------------------------------------
# Preconditions: may this artifact leave the machine at all?
# ---------------------------------------------------------------------------

def validate_publish_preconditions(render_manifest: dict, metadata_artifact: dict | None,
                                   ingest_manifest: dict | None = None) -> list[str]:
    """Reasons this render must NOT be uploaded anywhere yet.

    Returns a list of human-readable errors; an empty list is the ONLY state in
    which a publish intent may be created.

    The render approval is RE-DERIVED via `approval.check_approval` rather than
    read from a flag, so a re-render or a revoked upstream approval blocks
    publication automatically.
    """
    errors: list[str] = []

    if not isinstance(render_manifest, dict) or not render_manifest:
        return ["render manifesti yok: yayinlanacak bir cikti bulunamadi"]

    render_check = check_approval(render_manifest, "render")
    if not render_check.get("approved"):
        errors.append(f"render onayli degil: {render_check.get('reason')}")
    elif not render_check.get("valid"):
        errors.append(f"render onayi gecersiz: {render_check.get('reason')}")

    video_path = (render_manifest.get("output") or {}).get("final_video_path")
    if not video_path:
        errors.append("render manifestinde final_video_path yok")
    elif not Path(video_path).is_file():
        errors.append(f"render dosyasi diskte bulunamadi: {video_path!r}")
    elif Path(video_path).stat().st_size <= 0:
        errors.append(f"render dosyasi bos: {video_path!r}")

    if not isinstance(metadata_artifact, dict) or not metadata_artifact:
        errors.append("yayin metadatasi yok: baslik/aciklama onayi olmadan yukleme yapilmaz")
    else:
        if metadata_artifact.get("status") != "approved":
            errors.append(
                "yayin metadatasi onayli degil "
                f"(status={metadata_artifact.get('status')!r})"
            )
        selected = metadata_artifact.get("selected") or {}
        if not (selected.get("title") or "").strip():
            errors.append("yayin metadatasinda baslik bos")

    # A third-party ingest must carry its rights record all the way to publish;
    # this is the point where the legal exposure actually materializes.
    if isinstance(ingest_manifest, dict) and ingest_manifest:
        rights = ingest_manifest.get("rights") or {}
        if not rights.get("license_type"):
            errors.append("ingest manifestinde haklar kaydi yok: license_type eksik")
        if not rights.get("decided_by"):
            errors.append("ingest manifestinde haklar kararinin sahibi (decided_by) yok")

    return errors


# ---------------------------------------------------------------------------
# Gate 2: publish intent (upload as private)
# ---------------------------------------------------------------------------

def build_publish_intent(render_manifest: dict, metadata_artifact: dict,
                         target: PublishTarget, *, ingest_manifest: dict | None = None,
                         created_at: str | None = None) -> dict:
    """Builds `publish_intent.json`: exactly what would be uploaded, where.

    Born `review_required`. The decision block covers the video file hash, the
    metadata, the platform, the account and the desired visibility, so changing
    any of them invalidates an approval given earlier.

    Raises ValueError when preconditions are unmet -- an intent that could never
    legally be executed must not exist as an artifact.
    """
    errors = validate_publish_preconditions(render_manifest, metadata_artifact, ingest_manifest)
    if errors:
        raise ValueError("YAYIN NIYETI OLUSTURULAMADI: " + "; ".join(errors))

    video_path = (render_manifest.get("output") or {}).get("final_video_path")
    selected = metadata_artifact.get("selected") or {}
    decision = {
        "platform": target.platform,
        "account_ref": target.account_ref or None,
        "requested_visibility": target.visibility,
        "publish_at": target.publish_at,
        "made_for_kids": bool(target.made_for_kids),
        "initial_visibility": INITIAL_PRIVACY,
        "video_sha256": sha256_file(video_path),
        "metadata": {
            "title": selected.get("title"),
            "description": selected.get("description"),
            "tags": list(selected.get("tags") or []),
            "category_id": selected.get("category_id"),
            "language": selected.get("language"),
        },
        "rights_decision_sha256": ((ingest_manifest or {}).get("rights") or {}).get(
            "decision_sha256"
        ),
    }
    return {
        "status": "review_required",
        "status_reason": (
            "yayin niyeti olusturuldu. Onaylanana kadar hicbir yere yukleme yapilmaz; "
            "onaydan sonraki ilk yukleme her zaman private olur."
        ),
        "artifact_kind": "publish_intent",
        "created_at": created_at or _now_iso(),
        "decision": decision,
        "decision_sha256": sha256_canonical_json(decision),
        "source_video_path": video_path,
        "upload_performed": False,
        "visibility_change_performed": False,
    }


def approve_publish_intent(intent: dict, reviewed_decision_sha256: str,
                           approved_by: str, notes: str = "") -> dict:
    """Gate 2. Promotes a publish intent to `approved`, which authorizes ONE
    private upload -- and nothing else.

    Returns a NEW dict. Raises ValueError on any refusal.
    """
    if not isinstance(intent, dict) or intent.get("artifact_kind") != "publish_intent":
        raise ValueError("artefakt bir publish_intent degil")
    if not (approved_by or "").strip():
        raise ValueError("approved_by zorunlu: yayin onayini kimin verdigi kayda gecmeli.")
    if not reviewed_decision_sha256:
        raise ValueError(
            "reviewed_decision_sha256 zorunlu. Once yayin niyetini incele ve "
            "gosterilen SHA-256 degerini buraya yaz."
        )
    current = sha256_canonical_json(intent.get("decision") or {})
    if str(reviewed_decision_sha256).strip().lower() != current.lower():
        raise ValueError(
            f"ONAY REDDEDILDI: incelenen yayin niyeti hash'i "
            f"({str(reviewed_decision_sha256)[:16]}...) suanki niyetle "
            f"({current[:16]}...) eslesmiyor. Metadata veya hedef degismis olabilir."
        )

    approved = dict(intent)
    approved["status"] = "approved"
    approved["status_reason"] = (
        f"private yukleme icin {approved_by} tarafindan onaylandi "
        "(gorunurluk degisikligi AYRI bir onay gerektirir)"
    )
    approved["publish_approval"] = {
        "approved": True,
        "approved_by": approved_by.strip(),
        "approved_at": _now_iso(),
        "notes": (notes or "").strip() or None,
        "approved_decision_sha256": current,
        "authorizes": f"single upload with visibility={INITIAL_PRIVACY}",
    }
    return approved


def check_publish_intent_approval(intent: dict) -> dict:
    """Re-derives the intent hash and reports whether the approval still binds."""
    result = {"approved": False, "valid": False, "reason": ""}
    if not isinstance(intent, dict) or intent.get("status") != "approved":
        result["reason"] = f"yayin niyeti onayli degil (status={(intent or {}).get('status')!r})"
        return result
    result["approved"] = True
    recorded = (intent.get("publish_approval") or {}).get("approved_decision_sha256")
    if not recorded:
        result["reason"] = "onay karar icerigine baglanmamis (approved_decision_sha256 yok)"
        return result
    current = sha256_canonical_json(intent.get("decision") or {})
    if str(recorded).lower() != current.lower():
        result["reason"] = (
            f"ONAY GECERSIZ: yayin niyeti onaydan sonra degismis "
            f"(onaylanan={str(recorded)[:16]}..., suanki={current[:16]}...)"
        )
        return result
    result["valid"] = True
    result["reason"] = "onay suanki yayin niyetine bagli ve gecerli"
    return result


# ---------------------------------------------------------------------------
# Gate 3: visibility (private -> public / scheduled)
# ---------------------------------------------------------------------------

def approve_visibility(publish_manifest: dict, target_visibility: str,
                       reviewed_remote_id: str, approved_by: str, *,
                       publish_at: str | None = None, notes: str = "") -> dict:
    """Gate 3. Authorizes exposing an ALREADY-UPLOADED private item.

    Deliberately requires `reviewed_remote_id`: the human must have opened the
    private upload on the platform and looked at it before making it public.
    """
    if not isinstance(publish_manifest, dict) or not publish_manifest:
        raise ValueError("publish manifesti yok")
    if target_visibility not in VISIBILITY_VALUES:
        raise ValueError(f"gecersiz visibility: {target_visibility!r}")
    if target_visibility == VISIBILITY_PRIVATE and not publish_at:
        raise ValueError(
            "visibility zaten private; gorunurluk onayi yalnizca izleyiciye acilirken "
            "veya bir yayin zamani ayarlanirken gereklidir."
        )
    if not (approved_by or "").strip():
        raise ValueError("approved_by zorunlu: gorunurluk onayini kimin verdigi kayda gecmeli.")

    upload = publish_manifest.get("upload") or {}
    if upload.get("status") != "review_required" and upload.get("status") != "approved":
        raise ValueError(
            f"yukleme durumu gorunurluk degisikligine uygun degil (status={upload.get('status')!r})"
        )
    remote_id = upload.get("remote_id")
    if not remote_id:
        raise ValueError("publish manifestinde remote_id yok: henuz bir yukleme yapilmamis")
    if str(reviewed_remote_id).strip() != str(remote_id).strip():
        raise ValueError(
            "ONAY REDDEDILDI: incelendigi bildirilen yayin kimligi "
            f"({str(reviewed_remote_id)[:24]!r}) manifestteki yukleme kimligiyle "
            f"({str(remote_id)[:24]!r}) eslesmiyor. Once private yuklemeyi platformda ac ve izle."
        )

    approved = dict(publish_manifest)
    approved["visibility_approval"] = {
        "approved": True,
        "approved_by": approved_by.strip(),
        "approved_at": _now_iso(),
        "target_visibility": target_visibility,
        "publish_at": publish_at,
        "reviewed_remote_id": str(remote_id),
        "notes": (notes or "").strip() or None,
    }
    return approved


def visibility_change_allowed(publish_manifest: dict, target_visibility: str) -> tuple[bool, str]:
    """(allowed, reason) for actually calling the provider's `set_visibility`."""
    approval_block = (publish_manifest or {}).get("visibility_approval") or {}
    if approval_block.get("approved") is not True:
        return False, (
            "gorunurluk onayi yok: private yukleme izlenip approve_visibility(...) "
            "cagrilmadan icerik izleyiciye acilamaz"
        )
    if approval_block.get("target_visibility") != target_visibility:
        return False, (
            f"onaylanan gorunurluk {approval_block.get('target_visibility')!r}, "
            f"istenen {target_visibility!r}"
        )
    if target_visibility in AUDIENCE_EXPOSING_VISIBILITIES and not approval_block.get("approved_by"):
        return False, "onaylayan kisi kaydi yok"
    return True, ""


# ---------------------------------------------------------------------------
# Publish manifest
# ---------------------------------------------------------------------------

def build_publish_manifest(intent: dict, result: PublishResult, *,
                           ingest_manifest: dict | None = None,
                           uploaded_by: str = "") -> dict:
    """Records what actually happened, referencing the approval chain by hash."""
    decision = intent.get("decision") or {}
    return {
        "status": result.status,
        "status_reason": result.status_reason,
        "artifact_kind": "publish_manifest",
        "created_at": _now_iso(),
        "platform": result.platform,
        "uploaded_by": (uploaded_by or "").strip() or None,
        "upload": result.to_dict(),
        "requested_visibility": decision.get("requested_visibility"),
        "initial_visibility": INITIAL_PRIVACY,
        "current_visibility": result.visibility,
        "approval_chain": {
            "publish_intent_sha256": intent.get("decision_sha256"),
            "publish_approved_by": (intent.get("publish_approval") or {}).get("approved_by"),
            "publish_approved_at": (intent.get("publish_approval") or {}).get("approved_at"),
            "video_sha256": decision.get("video_sha256"),
            "rights_decision_sha256": decision.get("rights_decision_sha256"),
        },
        "rights": (ingest_manifest or {}).get("rights"),
        "visibility_approval": None,
    }


def write_publish_intent(intent: dict, results_dir) -> Path:
    return _atomic_write_json(Path(results_dir) / "publish_intent.json", intent)


def write_publish_manifest(manifest: dict, results_dir) -> Path:
    return _atomic_write_json(Path(results_dir) / "publish_manifest.json", manifest)


# ---------------------------------------------------------------------------
# Orchestration (still gate-bound; no gate logic lives in providers)
# ---------------------------------------------------------------------------

def execute_private_upload(intent: dict, *, provider: PublishProvider | None = None,
                           ingest_manifest: dict | None = None, uploaded_by: str = "",
                           progress_callback=None) -> dict:
    """Performs the ONE private upload an approved intent authorizes.

    Raises PermissionError when the intent approval is missing or stale, so a
    refused upload can never be mistaken for a completed one.
    """
    check = check_publish_intent_approval(intent)
    if not check["valid"]:
        raise PermissionError("YUKLEME REDDEDILDI: " + check["reason"])
    if intent.get("upload_performed"):
        raise PermissionError(
            "YUKLEME REDDEDILDI: bu yayin niyeti zaten kullanilmis. Tekrar yayin icin "
            "yeni bir publish_intent olusturulup yeniden onaylanmali."
        )

    decision = intent.get("decision") or {}
    video_path = intent.get("source_video_path")
    if not video_path or not Path(video_path).is_file():
        raise PermissionError(f"YUKLEME REDDEDILDI: render dosyasi bulunamadi: {video_path!r}")

    # The bytes must still be the bytes that were approved.
    current_hash = sha256_file(video_path)
    if current_hash.lower() != str(decision.get("video_sha256") or "").lower():
        raise PermissionError(
            "YUKLEME REDDEDILDI: video dosyasi onaydan sonra degismis "
            f"(onaylanan={str(decision.get('video_sha256'))[:16]}..., suanki={current_hash[:16]}...)"
        )

    target = PublishTarget(
        platform=decision.get("platform"),
        account_ref=decision.get("account_ref") or "",
        visibility=VISIBILITY_PRIVATE,
        publish_at=None,
        made_for_kids=bool(decision.get("made_for_kids")),
    )
    provider = provider or resolve_provider(target.platform)
    result = provider.upload_private(
        video_path, dict(decision.get("metadata") or {}), target,
        progress_callback=progress_callback,
    )
    return build_publish_manifest(
        intent, result, ingest_manifest=ingest_manifest, uploaded_by=uploaded_by,
    )


def execute_visibility_change(publish_manifest: dict, *, provider: PublishProvider | None = None) -> dict:
    """Applies the visibility approved in gate 3. Raises PermissionError when
    that approval is absent or does not match the requested change."""
    approval_block = (publish_manifest or {}).get("visibility_approval") or {}
    target_visibility = approval_block.get("target_visibility")
    allowed, reason = visibility_change_allowed(publish_manifest, target_visibility)
    if not allowed:
        raise PermissionError("GORUNURLUK DEGISIKLIGI REDDEDILDI: " + reason)

    platform = publish_manifest.get("platform")
    provider = provider or resolve_provider(platform)
    result = provider.set_visibility(
        (publish_manifest.get("upload") or {}).get("remote_id"),
        target_visibility,
        publish_at=approval_block.get("publish_at"),
    )
    updated = dict(publish_manifest)
    updated["visibility_change"] = result.to_dict()
    if result.status in ("review_required", "approved", "production_ready"):
        updated["current_visibility"] = result.visibility or target_visibility
        updated["status"] = result.status
        updated["status_reason"] = result.status_reason
    else:
        updated["status"] = result.status
        updated["status_reason"] = result.status_reason
    return updated
