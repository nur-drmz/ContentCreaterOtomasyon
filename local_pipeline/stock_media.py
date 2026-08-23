"""Provider-neutral stock-video search and an auditable long-form B-roll plan.

The integration is intentionally split into four explicit operations: plan,
search, select and download.  Merely opening a project never contacts a stock
provider, and a search result is never treated as an approved project asset.

The normalized records retain the public source page and creator credit.  API
credentials are accepted only at call time and are never returned or persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests

#: Where each workflow keeps its plan and its downloaded clips. The two module
#: constants below are the long-form entries by another name; they are kept so
#: existing callers and `verify_stock_media.py` are untouched.
ARTIFACT_ROOTS = {
    "long_form": "results/long_form",
    "turbo": "results/turbo",
}
BROLL_ARTIFACT = "results/long_form/broll_plan.json"
BROLL_DIRNAME = "results/long_form/broll"


def plan_artifact(kind: str = "long_form") -> str:
    """Where `kind`'s plan lives. Two workflows must not share one file."""
    root = ARTIFACT_ROOTS.get(str(kind or ""))
    if root is None:
        raise StockMediaError(f"bilinmeyen stok materyal alani: {kind!r}")
    return f"{root}/{'broll_plan' if kind == 'long_form' else 'media_plan'}.json"


def clip_dirname(kind: str = "long_form") -> str:
    root = ARTIFACT_ROOTS.get(str(kind or ""))
    if root is None:
        raise StockMediaError(f"bilinmeyen stok materyal alani: {kind!r}")
    return f"{root}/{'broll' if kind == 'long_form' else 'clips'}"
SCHEMA_VERSION = 1
PROVIDERS = ("pexels", "pixabay", "coverr")
DEFAULT_CLIP_DURATION_SEC = 5
MIN_CLIP_DURATION_SEC = 2
MAX_CLIP_DURATION_SEC = 12
MAX_RESULTS = 12
MAX_DOWNLOAD_BYTES = 750 * 1024 * 1024

_DOWNLOAD_HOSTS = {
    "pexels": frozenset({"videos.pexels.com"}),
    "pixabay": frozenset({"cdn.pixabay.com"}),
    "coverr": frozenset({"storage.coverr.co", "cdn.coverr.co"}),
}


class StockMediaError(ValueError):
    """A safe operator-facing stock provider error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=500) -> str:
    return str(value or "").strip()[:limit]


def _number(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def public_url(value) -> str | None:
    """Return a display-safe URL without credentials or query parameters."""
    try:
        parsed = urlsplit(_text(value, 4000))
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _download_url(value, provider: str) -> str | None:
    """Accept only the official provider CDN used for MP4 downloads."""
    try:
        parsed = urlsplit(_text(value, 8000))
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname not in _DOWNLOAD_HOSTS.get(provider, ())):
        return None
    return parsed.geturl()


def validate_clip_duration(value) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError) as exc:
        raise StockMediaError("B-roll klip suresi tam sayi olmali") from exc
    if not MIN_CLIP_DURATION_SEC <= duration <= MAX_CLIP_DURATION_SEC:
        raise StockMediaError(
            f"B-roll klip suresi {MIN_CLIP_DURATION_SEC}-{MAX_CLIP_DURATION_SEC} "
            "saniye olmali")
    return duration


def _query(value) -> str:
    value = " ".join(_text(value, 180).split())
    if len(value) < 2:
        raise StockMediaError("stok video arama metni en az 2 karakter olmali")
    return value


def _candidate_id(provider: str, asset_id) -> str:
    return f"{provider}:{_text(asset_id, 80)}"


def matches_orientation(width, height, orientation: str) -> bool:
    """One predicate for every orientation gate in this module.

    Three separate `width <= height` checks used to encode "landscape only" in
    three places. Routing them all through here is what keeps a portrait search
    from returning results that the parser then silently discards.
    """
    try:
        width, height = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    if orientation == "portrait":
        return height > width
    if orientation == "square":
        return 0.9 <= width / height <= 1.1
    return width > height


def _best_rendition(renditions, provider: str,
                    orientation: str = "landscape") -> dict | None:
    """Prefer landscape MP4 nearest to 1080p without upscaling metadata."""
    eligible = []
    for raw in renditions:
        url = _download_url(raw.get("url") or raw.get("link"), provider)
        width, height = _number(raw.get("width")), _number(raw.get("height"))
        if (not url or not matches_orientation(width, height, orientation)
                or min(width, height) < 360 or max(width, height) < 640):
            continue
        file_type = _text(raw.get("file_type") or "video/mp4", 80).lower()
        if "mp4" not in file_type:
            continue
        eligible.append({
            "download_url": url,
            "width": width,
            "height": height,
            "bytes": max(0, _number(raw.get("size"))),
            "quality": _text(raw.get("quality"), 40) or None,
            "thumbnail_url": public_url(raw.get("thumbnail")),
        })
    if not eligible:
        return None
    return min(eligible, key=lambda item: (
        abs(item["height"] - 1080), abs(item["width"] - 1920),
        -item["width"] * item["height"]))


def parse_pexels(payload: dict, *, query: str,
                 orientation: str = "landscape") -> list[dict]:
    candidates = []
    for item in (payload or {}).get("videos") or []:
        width, height = _number(item.get("width")), _number(item.get("height"))
        if not matches_orientation(width, height, orientation):
            continue
        rendition = _best_rendition(item.get("video_files") or [], "pexels",
                                    orientation)
        asset_id = item.get("id")
        if not rendition or asset_id in (None, ""):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        pictures = item.get("video_pictures") or []
        preview = public_url((pictures[0] or {}).get("picture")) if pictures else None
        candidates.append({
            "id": _candidate_id("pexels", asset_id),
            "provider": "pexels", "asset_id": str(asset_id),
            "query": query, "source_page": public_url(item.get("url")),
            "creator": {"name": _text(user.get("name"), 160),
                        "profile_page": public_url(user.get("url"))},
            "duration_sec": max(0, _number(item.get("duration"))),
            "width": rendition["width"], "height": rendition["height"],
            "bytes": rendition["bytes"], "quality": rendition["quality"],
            "preview_url": preview,
            "download_url": rendition["download_url"],
            "attribution": (f"Video by {_text(user.get('name'), 160)} on Pexels"
                            if _text(user.get("name"), 160)
                            else "Video provided by Pexels"),
        })
    return candidates


def parse_pixabay(payload: dict, *, query: str,
                  orientation: str = "landscape") -> list[dict]:
    candidates = []
    for item in (payload or {}).get("hits") or []:
        renditions = []
        for quality, raw in ((item.get("videos") or {}).items()):
            if isinstance(raw, dict):
                renditions.append(dict(raw, quality=quality))
        # Pixabay has no orientation query parameter, so the filter is
        # applied here against what it actually returned.
        rendition = _best_rendition(renditions, "pixabay", orientation)
        asset_id = item.get("id")
        if not rendition or asset_id in (None, ""):
            continue
        creator = _text(item.get("user"), 160)
        creator_id = _number(item.get("user_id"))
        profile = (f"https://pixabay.com/users/{quote(creator)}-{creator_id}/"
                   if creator and creator_id else None)
        candidates.append({
            "id": _candidate_id("pixabay", asset_id),
            "provider": "pixabay", "asset_id": str(asset_id),
            "query": query, "source_page": public_url(item.get("pageURL")),
            "creator": {"name": creator, "profile_page": public_url(profile)},
            "duration_sec": max(0, _number(item.get("duration"))),
            "width": rendition["width"], "height": rendition["height"],
            "bytes": rendition["bytes"], "quality": rendition["quality"],
            "preview_url": rendition.get("thumbnail_url"),
            "download_url": rendition["download_url"],
            "attribution": (f"Video by {creator} on Pixabay" if creator
                            else "Video provided by Pixabay"),
        })
    return candidates


def parse_coverr(payload: dict, *, query: str,
                 orientation: str = "landscape") -> list[dict]:
    """Normalise a Coverr search response into the shared candidate shape.

    Coverr returns ONE downloadable rendition per video rather than a ladder,
    so the rendition list handed to `_best_rendition` has a single entry. That
    is deliberate: the size/orientation gates still apply, so an unusable clip
    is dropped here instead of failing at download time.

    NOTE: written against Coverr's published response shape and exercised with
    recorded fixtures -- no live call has confirmed it. Field lookups are
    therefore forgiving, and a video missing any required field is skipped
    rather than half-built.
    """
    candidates = []
    for item in (payload or {}).get("hits") or []:
        urls = item.get("urls") or {}
        asset_id = item.get("id")
        download = urls.get("mp4_download") or urls.get("mp4")
        if not download or asset_id in (None, ""):
            continue
        rendition = _best_rendition([{
            "url": download,
            "width": item.get("max_width") or item.get("width"),
            "height": item.get("max_height") or item.get("height"),
            "size": item.get("file_size"),
            "quality": "mp4",
            "file_type": "video/mp4",
            "thumbnail": item.get("poster") or item.get("thumbnail"),
        }], "coverr", orientation)
        if not rendition:
            continue
        creator = _text((item.get("creator") or {}).get("name")
                        if isinstance(item.get("creator"), dict)
                        else item.get("creator"), 160)
        candidates.append({
            "id": _candidate_id("coverr", asset_id),
            "provider": "coverr", "asset_id": str(asset_id),
            "query": query,
            "source_page": public_url(
                item.get("url") or f"https://coverr.co/videos/{asset_id}"),
            "creator": {"name": creator, "profile_page": None},
            "duration_sec": max(0, _number(item.get("duration"))),
            "width": rendition["width"], "height": rendition["height"],
            "bytes": rendition["bytes"], "quality": rendition["quality"],
            "preview_url": rendition.get("thumbnail_url"),
            "download_url": rendition["download_url"],
            "attribution": (f"Video by {creator} on Coverr" if creator
                            else "Video provided by Coverr"),
        })
    return candidates


def search_videos(provider: str, query: str, api_key: str, *, per_page: int = 6,
                  session=None, timeout: float = 20.0,
                  orientation: str = "landscape") -> dict:
    """Run one explicit provider search and return normalized public records.

    `orientation` defaults to landscape, which is what every existing caller
    wants and what the module has always done. A 9:16 Turbo asks for portrait
    instead, because centre-cropping a 1920x1080 source into 1080x1920 throws
    away most of the frame -- fine as a fallback, wasteful as the default.
    """
    if orientation not in ("landscape", "portrait", "square"):
        raise StockMediaError(f"bilinmeyen yonelim: {orientation!r}")
    provider = _text(provider, 40).lower()
    if provider not in PROVIDERS:
        raise StockMediaError(f"desteklenmeyen stok video saglayicisi: {provider!r}")
    query = _query(query)
    key = _text(api_key, 1000)
    if not key:
        raise StockMediaError(f"{provider} API anahtari tanimli degil")
    try:
        count = int(per_page)
    except (TypeError, ValueError) as exc:
        raise StockMediaError("aday sayisi tam sayi olmali") from exc
    count = max(3, min(MAX_RESULTS, count))
    client = session or requests
    try:
        if provider == "pexels":
            url = "https://api.pexels.com/v1/videos/search?" + urlencode({
                "query": query, "orientation": orientation, "per_page": count})
            response = client.get(url, headers={"Authorization": key},
                                  timeout=timeout)
        elif provider == "coverr":
            # Coverr takes the key in a Bearer header, so unlike Pixabay the
            # request URL carries no secret. `urls=true` is what makes the
            # response include the downloadable rendition at all.
            url = "https://api.coverr.co/videos?" + urlencode({
                "query": query, "page_size": count, "urls": "true"})
            response = client.get(url, headers={"Authorization": f"Bearer {key}"},
                                  timeout=timeout)
        else:
            # Pixabay requires the key in the query. This URL is deliberately
            # never returned, persisted or included in an exception message.
            url = "https://pixabay.com/api/videos/?" + urlencode({
                "key": key, "q": query, "per_page": count,
                "safesearch": "true"})
            response = client.get(url, timeout=timeout)
        if response.status_code == 429:
            raise StockMediaError(f"{provider} arama kotasi doldu; daha sonra dene")
        if response.status_code in {401, 403}:
            raise StockMediaError(f"{provider} API anahtari reddedildi")
        if response.status_code < 200 or response.status_code >= 300:
            raise StockMediaError(
                f"{provider} aramasi HTTP {response.status_code} ile basarisiz oldu")
        payload = response.json()
    except StockMediaError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise StockMediaError(
            f"{provider} aramasina ulasilamadi ({type(exc).__name__})") from None
    parser = {"pexels": parse_pexels, "pixabay": parse_pixabay,
              "coverr": parse_coverr}[provider]
    candidates = parser(payload, query=query, orientation=orientation)
    return {
        "provider": provider, "query": query,
        "candidates": candidates[:count], "count": min(len(candidates), count),
        "searched_at": _now(),
        "rate_limit": ({
            "limit": response.headers.get("X-Ratelimit-Limit"),
            "remaining": response.headers.get("X-Ratelimit-Remaining"),
            "reset": response.headers.get("X-Ratelimit-Reset"),
        } if provider == "pexels" else None),
    }


def new_plan(chapters: dict, *, clip_duration_sec=DEFAULT_CLIP_DURATION_SEC,
             ownership=None) -> dict:
    """Create two editable search slots per chapter, in script order."""
    duration = validate_clip_duration(clip_duration_sec)
    rows = (chapters or {}).get("chapters") or []
    if not rows:
        raise StockMediaError("B-roll plani icin once bolum scriptlerini uret")
    slots = []
    for chapter in rows:
        index = _number(chapter.get("index"))
        title = _text(chapter.get("title"), 180) or f"chapter {index}"
        body = " ".join(_text(chapter.get("text"), 12000).split())
        midpoint = max(1, len(body) // 2)
        excerpts = (body[:midpoint], body[midpoint:])
        for part, excerpt in enumerate(excerpts, 1):
            slots.append({
                "id": f"ch{index:02d}-{part}", "sequence": len(slots) + 1,
                "chapter_index": index, "chapter_title": title,
                "script_excerpt": excerpt[:260], "query": title,
                "status": "planned", "provider": None,
                "candidates": [], "selected": None, "download": None,
                "searched_at": None,
            })
    return new_plan_from_slots(
        slots, clip_duration_sec=duration, source_sha256=script_sha256(chapters),
        artifact_type="long_form_broll_plan", aspect_ratio="16:9",
        ownership=ownership)


def new_plan_from_slots(slots, *, clip_duration_sec, source_sha256: str,
                        artifact_type: str = "long_form_broll_plan",
                        aspect_ratio: str = "16:9", ownership=None) -> dict:
    """Wrap an already-built slot list in a plan record.

    Split out of `new_plan` so Turbo can reuse the record shape -- and with it
    the freshness hash, the attribution requirement and the explicit-action
    policy -- while building its slots differently. Turbo has one slot per scene
    beat; long-form splits each chapter in two. Neither is right for the other,
    but everything around the slots is identical.

    `new_plan`'s output is unchanged by this refactor, byte for byte.
    """
    record = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": artifact_type, "status": "draft",
        "aspect_ratio": aspect_ratio,
        "clip_duration_sec": validate_clip_duration(clip_duration_sec),
        "script_sha256": source_sha256,
        "slots": list(slots), "created_at": _now(), "updated_at": _now(),
        "not_uploaded": True,
        "attribution_required": True,
        "policy": ("Arama, secim ve indirme ayri operator eylemleridir. Kaynak "
                   "sayfasi ve uretici kredisi final manifestte korunur."),
    }
    if ownership is not None:
        record.update(ownership.to_dict())
    return record


def is_stale_against(plan: dict | None, digest: str) -> bool:
    """Freshness against a caller-computed digest.

    `is_stale` keeps its exact signature because long-form and
    `verify_stock_media.py` both pin it; Turbo hashes its own script and asks
    this instead.
    """
    return bool(plan) and plan.get("script_sha256") != digest


def script_sha256(chapters: dict | None) -> str:
    rows = [{"index": item.get("index"), "title": item.get("title"),
             "text": item.get("text")}
            for item in ((chapters or {}).get("chapters") or [])]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_stale(plan: dict | None, chapters: dict | None) -> bool:
    return bool(plan) and plan.get("script_sha256") != script_sha256(chapters)


def _slot(plan: dict, slot_id: str) -> tuple[list, dict]:
    slots = [dict(item) for item in (plan or {}).get("slots") or []]
    wanted = _text(slot_id, 80)
    target = next((item for item in slots if item.get("id") == wanted), None)
    if target is None:
        raise StockMediaError(f"B-roll slotu bulunamadi: {wanted}")
    return slots, target


def store_search(plan: dict, slot_id: str, result: dict) -> dict:
    slots, target = _slot(plan, slot_id)
    target.update({"query": result["query"], "provider": result["provider"],
                   "candidates": list(result.get("candidates") or []),
                   "selected": None, "download": None,
                   "searched_at": result.get("searched_at"),
                   "status": "candidates_ready"})
    return _replace_slot(plan, slots, target)


def select_candidate(plan: dict, slot_id: str, candidate_id: str) -> dict:
    slots, target = _slot(plan, slot_id)
    candidate = next((dict(item) for item in target.get("candidates") or []
                      if item.get("id") == candidate_id), None)
    if candidate is None:
        raise StockMediaError("secilecek B-roll adayi bu slotta bulunamadi")
    target.update({"selected": candidate, "download": None,
                   "status": "selected"})
    return _replace_slot(plan, slots, target)


def record_download(plan: dict, slot_id: str, download: dict) -> dict:
    slots, target = _slot(plan, slot_id)
    if not target.get("selected"):
        raise StockMediaError("once bir B-roll adayi sec")
    target.update({"download": dict(download), "status": "downloaded"})
    return _replace_slot(plan, slots, target)


def _replace_slot(plan: dict, slots: list, target: dict) -> dict:
    updated = dict(plan)
    updated["slots"] = [target if item.get("id") == target.get("id") else item
                        for item in slots]
    updated["updated_at"] = _now()
    downloaded = sum(1 for item in updated["slots"] if item.get("download"))
    selected = sum(1 for item in updated["slots"] if item.get("selected"))
    updated["status"] = ("ready_for_review" if downloaded == len(updated["slots"])
                         else "in_progress" if selected or downloaded else "draft")
    return updated


def download_candidate(candidate: dict, destination, *, session=None,
                       timeout: float = 60.0,
                       max_bytes: int = MAX_DOWNLOAD_BYTES) -> dict:
    """Download one explicitly selected MP4 atomically into project storage."""
    provider = _text((candidate or {}).get("provider"), 40)
    url = _download_url((candidate or {}).get("download_url"), provider)
    if not url:
        raise StockMediaError("aday indirme adresi saglayici CDN beyaz listesinde degil")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    client = session or requests
    digest = hashlib.sha256()
    written = 0
    try:
        response = client.get(url, stream=True, timeout=timeout)
        if response.status_code < 200 or response.status_code >= 300:
            raise StockMediaError(
                f"{provider} video indirmesi HTTP {response.status_code} ile basarisiz")
        declared = _number(response.headers.get("Content-Length"))
        if declared > max_bytes:
            raise StockMediaError("stok video izin verilen indirme boyutunu asiyor")
        with open(temporary, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise StockMediaError("stok video izin verilen indirme boyutunu asiyor")
                digest.update(chunk)
                handle.write(chunk)
        if written < 12:
            raise StockMediaError("indirilen stok video bos veya gecersiz")
        with open(temporary, "rb") as handle:
            header = handle.read(12)
        if b"ftyp" not in header:
            raise StockMediaError("indirilen dosya MP4 kapsayicisi degil")
        temporary.replace(destination)
    except StockMediaError:
        if temporary.exists():
            temporary.unlink()
        raise
    except (requests.RequestException, OSError) as exc:
        if temporary.exists():
            temporary.unlink()
        raise StockMediaError(
            f"stok video indirilemedi ({type(exc).__name__})") from None
    return {"local_file": destination.name, "bytes": written,
            "sha256": digest.hexdigest(), "downloaded_at": _now()}


def safe_filename(candidate: dict) -> str:
    provider = _text(candidate.get("provider"), 20)
    asset_id = re.sub(r"[^A-Za-z0-9_-]", "_", _text(candidate.get("asset_id"), 80))
    if provider not in PROVIDERS or not asset_id:
        raise StockMediaError("stok video kimligi gecersiz")
    return f"{provider}_{asset_id}.mp4"


def summary(plan: dict | None, chapters: dict | None = None) -> dict:
    plan = plan or {}
    slots = plan.get("slots") or []
    return {
        "available": bool(plan), "status": plan.get("status"),
        "stale": is_stale(plan, chapters) if chapters is not None else False,
        "aspect_ratio": plan.get("aspect_ratio") or "16:9",
        "clip_duration_sec": plan.get("clip_duration_sec"),
        "slot_count": len(slots),
        "searched_count": sum(1 for item in slots if item.get("searched_at")),
        "selected_count": sum(1 for item in slots if item.get("selected")),
        "downloaded_count": sum(1 for item in slots if item.get("download")),
        "slots": slots, "policy": plan.get("policy"),
        "attribution_required": plan.get("attribution_required", True),
    }
