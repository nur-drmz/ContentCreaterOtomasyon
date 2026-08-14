"""Publication metadata: title / description / tags, generated or written by hand.

WHY THIS MODULE EXISTS
----------------------
Title and thumbnail are the largest multipliers on whether a finished video earns
anything, and until now this project produced neither. The router already had a
`metadata_generation` task, but nothing in the pipeline consumed it, so every
upload would have needed a title typed at the last second -- exactly the point
where a human is least careful.

CONTRACT
--------
- Generated metadata is born `review_required` and carries THREE title variants,
  because picking between options is a better human decision than accepting one.
- `select_metadata()` requires an explicit choice; there is no "use the first
  variant" default.
- Third-party footage forces an attribution block into the description. It is
  appended by `apply_attribution()` and `validate_metadata()` refuses metadata
  that lost it.
- Nothing is fabricated: if the provider fails, the artifact is `failed` /
  `insufficient_data` and the user can still write metadata by hand through
  `build_manual_metadata()`. A missing credential is not an error, it is a
  documented path.
- Promotion to `approved` happens only via `approve_metadata()` with the SHA-256
  of the exact selection the human reviewed.
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

MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS = 15
MIN_TITLE_CHARS = 8
TITLE_VARIANT_COUNT = 3

#: YouTube rejects `<` and `>` in titles/descriptions outright.
_FORBIDDEN_CHARS = ("<", ">")
_WHITESPACE = re.compile(r"\s+")

ATTRIBUTION_HEADER = "Kaynak / Source:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_line(value, limit: int) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    for char in _FORBIDDEN_CHARS:
        text = text.replace(char, "")
    return text[:limit].strip()


def _atomic_write_json(path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_metadata_prompt(*, script_text: str = "", transcript_excerpt: str = "",
                          source_title: str = "", target_language: str = "tr-TR",
                          is_short: bool = True, theory_angle: str = "") -> str:
    """Builds the metadata prompt. Only DERIVED text is sent (script/transcript
    excerpt), never raw media -- `ALLOW_RAW_MEDIA_TO_FREE_TIER` still holds."""
    body = (script_text or transcript_excerpt or "").strip()
    if not body:
        raise ValueError(
            "metadata prompt icin script veya transcript alintisi zorunlu; "
            "bos girdiden baslik uretmek uydurma olur."
        )
    fmt = "YouTube Shorts (dikey, <=3 dakika)" if is_short else "YouTube uzun video"
    lines = [
        f"Gorev: Bir {fmt} icin yayin metadatasi uret.",
        f"Dil: {target_language}",
        "",
        "KURALLAR:",
        f"- Tam olarak {TITLE_VARIANT_COUNT} farkli baslik varyanti uret.",
        f"- Her baslik en fazla {MAX_TITLE_CHARS} karakter olmali.",
        "- Clickbait yalan kullanma: baslik icerikte GERCEKTEN olan seyi anlatmali.",
        "- Buyuk harf bagirma (ALL CAPS) ve ard arda unlem kullanma.",
        f"- Aciklama en fazla {MAX_DESCRIPTION_CHARS} karakter, ilk satir konuyu ozetlemeli.",
        f"- En fazla {MAX_TAGS} adet tag uret; tekrar eden tag olmasin.",
        "- Cikti SADECE su JSON semasinda olsun:",
        '  {"titles": ["...", "...", "..."], "description": "...", '
        '"tags": ["..."], "hashtags": ["#..."]}',
        "",
    ]
    if source_title:
        lines.append(f"Kaynak video basligi: {source_title}")
    if theory_angle:
        lines.append(f"Anlati acisi: {theory_angle}")
    lines += ["", "ICERIK:", body[:6000]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _parse_payload(payload) -> dict:
    """Accepts the router's parsed dict, or a JSON string, and normalizes it."""
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("metadata yaniti JSON nesnesi degil")
    return payload


def normalize_metadata_payload(payload) -> dict:
    """Normalizes a raw provider payload into variants/description/tags.

    Raises ValueError when the payload contains no usable title -- an empty
    string is valid JSON and still worthless, so it is rejected here rather than
    surfacing as an "approved" empty title later.
    """
    data = _parse_payload(payload)

    raw_titles = data.get("titles") or ([data.get("title")] if data.get("title") else [])
    titles, seen = [], set()
    for raw in raw_titles:
        title = _clean_line(raw, MAX_TITLE_CHARS)
        if len(title) < MIN_TITLE_CHARS or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        titles.append(title)
    if not titles:
        raise ValueError(
            f"kullanilabilir baslik uretilmedi (en az {MIN_TITLE_CHARS} karakter gerekli)"
        )

    description = str(data.get("description") or "").strip()
    for char in _FORBIDDEN_CHARS:
        description = description.replace(char, "")
    description = description[:MAX_DESCRIPTION_CHARS].strip()

    tags, tag_seen = [], set()
    for raw in (data.get("tags") or []):
        tag = _clean_line(raw, 60).lstrip("#")
        if not tag or tag.casefold() in tag_seen:
            continue
        tag_seen.add(tag.casefold())
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break

    hashtags, hash_seen = [], set()
    for raw in (data.get("hashtags") or []):
        tag = _clean_line(raw, 60)
        if not tag:
            continue
        tag = tag if tag.startswith("#") else f"#{tag}"
        if tag.casefold() in hash_seen:
            continue
        hash_seen.add(tag.casefold())
        hashtags.append(tag)

    return {
        "title_variants": titles[:TITLE_VARIANT_COUNT],
        "description": description,
        "tags": tags,
        "hashtags": hashtags[:5],
    }


def generate_metadata(*, script_text: str = "", transcript_excerpt: str = "",
                      source_title: str = "", target_language: str = "tr-TR",
                      is_short: bool = True, theory_angle: str = "",
                      generate_fn=None, **router_kwargs) -> dict:
    """Generates metadata through the canonical Google router (Route B).

    `generate_fn(prompt, **kwargs) -> route_result` is injected in tests. The
    default is `google_ai.run_google_bulk_classification`, whose docstring names
    title/description/hashtag generation as its purpose, so no new route or
    hardcoded model is introduced here.

    Never raises for a provider failure: the returned artifact is `failed` with a
    sanitized reason, and the manual path stays available.
    """
    prompt = build_metadata_prompt(
        script_text=script_text, transcript_excerpt=transcript_excerpt,
        source_title=source_title, target_language=target_language,
        is_short=is_short, theory_angle=theory_angle,
    )

    if generate_fn is None:
        from . import google_ai  # noqa: PLC0415

        generate_fn = google_ai.run_google_bulk_classification
        router_kwargs.setdefault("required_fields", ("titles", "description"))

    artifact = {
        "status": "failed",
        "status_reason": "",
        "artifact_kind": "publication_metadata",
        "created_at": _now_iso(),
        "origin": "generated",
        "language": target_language,
        "is_short": bool(is_short),
        "prompt_sha256": None,
        "provider": None,
        "model": None,
        "title_variants": [],
        "description": "",
        "tags": [],
        "hashtags": [],
        "selected": None,
        "selected_sha256": None,
        "attribution_required": False,
        "attribution_applied": False,
        "error": None,
    }

    try:
        result = generate_fn(prompt, **router_kwargs) or {}
    except Exception as exc:
        artifact["status_reason"] = "metadata uretimi basarisiz"
        artifact["error"] = str(exc)[:800]
        return artifact

    artifact["provider"] = result.get("provider")
    artifact["model"] = result.get("model")
    if result.get("status") not in ("ok", "review_required", None):
        artifact["status"] = (
            "insufficient_data" if result.get("status") == "insufficient_data" else "failed"
        )
        artifact["status_reason"] = result.get("status_reason") or "saglayici metadata uretemedi"
        return artifact

    try:
        # `execute_google_route` puts the validated payload in "output"; the
        # other keys keep an injected double or a future provider usable.
        normalized = normalize_metadata_payload(
            result.get("output") or result.get("parsed") or result.get("payload")
            or result.get("text")
        )
    except Exception as exc:
        artifact["status_reason"] = "metadata yaniti dogrulanamadi"
        artifact["error"] = str(exc)[:800]
        return artifact

    artifact.update(normalized)
    artifact["status"] = "review_required"
    artifact["status_reason"] = (
        f"{len(normalized['title_variants'])} baslik varyanti uretildi; insan bir "
        "varyant secip onaylamadan yayin yapilamaz."
    )
    return artifact


def build_manual_metadata(*, title: str, description: str = "", tags=(),
                          language: str = "tr-TR", is_short: bool = True) -> dict:
    """The always-available path: the human writes the metadata themselves.

    Born `review_required` like the generated variant, so the approval gate is
    identical no matter who wrote the text.
    """
    clean_title = _clean_line(title, MAX_TITLE_CHARS)
    if len(clean_title) < MIN_TITLE_CHARS:
        raise ValueError(f"baslik en az {MIN_TITLE_CHARS} karakter olmali")
    clean_tags, seen = [], set()
    for raw in tags or []:
        tag = _clean_line(raw, 60).lstrip("#")
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            clean_tags.append(tag)
    return {
        "status": "review_required",
        "status_reason": "metadata elle yazildi; onay bekliyor",
        "artifact_kind": "publication_metadata",
        "created_at": _now_iso(),
        "origin": "manual",
        "language": language,
        "is_short": bool(is_short),
        "provider": None,
        "model": None,
        "title_variants": [clean_title],
        "description": str(description or "")[:MAX_DESCRIPTION_CHARS].strip(),
        "tags": clean_tags[:MAX_TAGS],
        "hashtags": [],
        "selected": None,
        "selected_sha256": None,
        "attribution_required": False,
        "attribution_applied": False,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Attribution (third-party footage)
# ---------------------------------------------------------------------------

def build_attribution_block(ingest_manifest: dict | None) -> str:
    """Builds the description block crediting the original source.

    Returns "" for own footage: there is nothing to attribute, and inventing a
    credit line would be as wrong as omitting a required one.
    """
    rights = (ingest_manifest or {}).get("rights") or {}
    license_type = rights.get("license_type")
    if not license_type or license_type == "own":
        return ""
    parts = [ATTRIBUTION_HEADER]
    attribution = (rights.get("attribution") or "").strip()
    if attribution:
        parts.append(attribution)
    url = (ingest_manifest or {}).get("candidate_url")
    if url:
        parts.append(url)
    uploader = (ingest_manifest or {}).get("uploader")
    if uploader:
        parts.append(f"Kanal: {uploader}")
    parts.append(f"Lisans beyani: {license_type}")
    note = (rights.get("transformation_note") or "").strip()
    if note:
        parts.append(f"Donusum: {note}")
    return "\n".join(parts)


def apply_attribution(metadata_artifact: dict, ingest_manifest: dict | None) -> dict:
    """Appends the attribution block to the description, idempotently.

    Returns a NEW dict. Marks `attribution_required` so `validate_metadata()`
    can refuse a later edit that strips the credit.
    """
    block = build_attribution_block(ingest_manifest)
    updated = dict(metadata_artifact)
    updated["attribution_required"] = bool(block)
    if not block:
        updated["attribution_applied"] = False
        return updated

    description = str(updated.get("description") or "").strip()
    if ATTRIBUTION_HEADER not in description:
        description = (description + "\n\n" + block).strip()
    updated["description"] = description[:MAX_DESCRIPTION_CHARS]
    updated["attribution_applied"] = ATTRIBUTION_HEADER in updated["description"]
    # A selection made before attribution was applied no longer matches.
    if updated.get("selected"):
        updated["selected"] = None
        updated["selected_sha256"] = None
        updated["status"] = "review_required"
        updated["status_reason"] = (
            "attribution eklendi; secim ve onay sifirlandi, yeniden incelenmeli."
        )
    return updated


# ---------------------------------------------------------------------------
# Selection + validation + approval
# ---------------------------------------------------------------------------

def select_metadata(metadata_artifact: dict, title_index: int, *,
                    description_override: str | None = None,
                    tags_override=None, category_id: str | None = None) -> dict:
    """Locks in ONE title variant. There is deliberately no default index."""
    variants = list(metadata_artifact.get("title_variants") or [])
    if not variants:
        raise ValueError("secilecek baslik varyanti yok")
    try:
        index = int(title_index)
    except (TypeError, ValueError):
        raise ValueError("title_index tam sayi olmali") from None
    if index < 0 or index >= len(variants):
        raise ValueError(
            f"gecersiz title_index={index}; 0..{len(variants) - 1} araliginda olmali"
        )

    description = (
        str(description_override) if description_override is not None
        else str(metadata_artifact.get("description") or "")
    )
    for char in _FORBIDDEN_CHARS:
        description = description.replace(char, "")
    tags = list(tags_override if tags_override is not None else (metadata_artifact.get("tags") or []))

    selected = {
        "title": variants[index],
        "title_index": index,
        "description": description[:MAX_DESCRIPTION_CHARS].strip(),
        "tags": [str(tag).lstrip("#") for tag in tags][:MAX_TAGS],
        "category_id": str(category_id) if category_id else None,
        "language": metadata_artifact.get("language"),
    }

    updated = dict(metadata_artifact)
    updated["selected"] = selected
    updated["selected_sha256"] = sha256_canonical_json(selected)
    updated["status"] = "review_required"
    updated["status_reason"] = (
        "baslik secildi; gosterilen SHA-256 ile onaylanmadan yayin yapilamaz."
    )
    # A selection invalidates any previous approval.
    updated.pop("metadata_approval", None)
    return updated


def validate_metadata(metadata_artifact: dict) -> list[str]:
    """Reasons this metadata must not be used for a publish intent."""
    errors: list[str] = []
    if not isinstance(metadata_artifact, dict) or not metadata_artifact:
        return ["yayin metadatasi yok"]

    selected = metadata_artifact.get("selected")
    if not isinstance(selected, dict) or not selected:
        return ["baslik secimi yapilmamis (select_metadata cagrilmali)"]

    title = str(selected.get("title") or "").strip()
    if len(title) < MIN_TITLE_CHARS:
        errors.append(f"baslik en az {MIN_TITLE_CHARS} karakter olmali")
    if len(title) > MAX_TITLE_CHARS:
        errors.append(f"baslik {MAX_TITLE_CHARS} karakteri asiyor ({len(title)})")
    if any(char in title for char in _FORBIDDEN_CHARS):
        errors.append("baslik '<' veya '>' karakteri iceriyor; YouTube reddeder")

    description = str(selected.get("description") or "")
    if len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(f"aciklama {MAX_DESCRIPTION_CHARS} karakteri asiyor")

    if len(selected.get("tags") or []) > MAX_TAGS:
        errors.append(f"tag sayisi {MAX_TAGS} sinirini asiyor")

    if metadata_artifact.get("attribution_required") and ATTRIBUTION_HEADER not in description:
        errors.append(
            "ucuncu taraf kaynagi icin attribution blogu aciklamadan silinmis; "
            "apply_attribution(...) tekrar uygulanmali"
        )

    recorded = metadata_artifact.get("selected_sha256")
    current = sha256_canonical_json(selected)
    if not recorded:
        errors.append("selected_sha256 yok: secim icerige baglanmamis")
    elif str(recorded).lower() != current.lower():
        errors.append(
            f"secim icerigi degismis (kayitli={str(recorded)[:16]}..., suanki={current[:16]}...)"
        )
    return errors


def approve_metadata(metadata_artifact: dict, reviewed_selection_sha256: str,
                     approved_by: str, notes: str = "") -> dict:
    """Promotes metadata to `approved` when the reviewed hash matches.

    Returns a NEW dict; raises ValueError on any refusal.
    """
    if not (approved_by or "").strip():
        raise ValueError("approved_by zorunlu: metadata onayini kimin verdigi kayda gecmeli.")
    errors = validate_metadata(metadata_artifact)
    if errors:
        raise ValueError("ONAY REDDEDILDI: " + "; ".join(errors))
    current = sha256_canonical_json(metadata_artifact.get("selected") or {})
    if not reviewed_selection_sha256:
        raise ValueError(
            "reviewed_selection_sha256 zorunlu. Once secimi incele ve gosterilen "
            "SHA-256 degerini buraya yaz."
        )
    if str(reviewed_selection_sha256).strip().lower() != current.lower():
        raise ValueError(
            f"ONAY REDDEDILDI: incelenen secim ({str(reviewed_selection_sha256)[:16]}...) "
            f"suanki secimle ({current[:16]}...) eslesmiyor."
        )

    approved = dict(metadata_artifact)
    approved["status"] = "approved"
    approved["status_reason"] = f"metadata {approved_by} tarafindan onaylandi"
    approved["metadata_approval"] = {
        "approved": True,
        "approved_by": approved_by.strip(),
        "approved_at": _now_iso(),
        "notes": (notes or "").strip() or None,
        "approved_selection_sha256": current,
    }
    return approved


def write_metadata(metadata_artifact: dict, results_dir) -> Path:
    return _atomic_write_json(
        Path(results_dir) / "publication_metadata.json", metadata_artifact
    )
