"""Reusable, read-only YouTube Data API v3 collection logic.

Shared by 04_trend_opportunity_scoring.ipynb, 05_viral_prediction.ipynb, and
00_youtube_automation_master.ipynb via a bootstrap cell in each notebook (see
`local_pipeline/youtube_bootstrap_snippet.py` for the exact bootstrap text
that is embedded into those notebooks) so all three call into ONE
implementation instead of three copies that could drift out of sync.

Security contract (do not violate when editing this file):
  - This module never reads a secret from Colab userdata or os.environ on its
    own initiative; callers pass the API key in explicitly to `build_client`.
  - Nothing here ever prints, logs, or persists the raw API key.
  - `sanitize_error_text` / `sanitize_url` MUST be applied to any exception
    text, response body, or URL before it is written to a JSON/CSV file or
    printed, because googleapiclient exceptions can include the full request
    URL (which contains the key as a query parameter).
  - This module is read-only: it never calls an endpoint that could modify,
    upload, or publish anything on YouTube.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUS_VALUES = {
    "experimental", "development_placeholder", "review_required",
    "approved", "insufficient_data", "failed", "production_ready",
}

NORMALIZED_VIDEO_COLUMNS = [
    "video_id", "title", "description", "published_at", "channel_id", "channel_title",
    "view_count", "like_count", "comment_count", "duration_iso8601", "duration_sec",
    "tags", "category_id", "default_language", "caption_available", "definition",
    "privacy_status", "is_short_duration_candidate", "collected_at",
]

DISCOVERED_VIDEO_COLUMNS = NORMALIZED_VIDEO_COLUMNS + [
    "matched_terms", "matched_query_count", "search_orders", "video_age_hours",
    "views_per_hour", "engagement_rate", "velocity_score", "engagement_score",
    "recency_score", "channel_repeat_score", "trend_score", "discovery_rank",
]

DISCOVERED_CHANNEL_COLUMNS = [
    "channel_id", "channel_title", "matched_video_count", "matched_terms",
    "total_view_count", "max_views_per_hour", "mean_trend_score",
    "max_trend_score", "discovery_rank",
]

# Since the current YouTube quota revision, search.list consumes one call from
# its own default 100-searches/day bucket. The other read endpoints consume one
# unit from the general daily quota bucket. Keep both call counts visible in
# QuotaTracker; never represent this local estimate as authoritative.
QUOTA_COST = {"channels.list": 1, "playlistItems.list": 1, "videos.list": 1, "search.list": 1}
DEFAULT_SEARCH_CALL_LIMIT_PER_DAY = 100
DISCOVERY_ALLOWED_ORDERS = ("date", "viewCount", "relevance", "rating")

# Heuristic only -- YouTube Shorts eligibility depends on aspect ratio and the
# Shorts shelf/player, not solely on duration. This threshold matches the
# public <=3 minute Shorts duration ceiling but CANNOT by itself prove a video
# is a Short (e.g. a <=3 min widescreen upload is not a Short).
SHORT_DURATION_HEURISTIC_MAX_SEC = 180

CHANNEL_ID_PATTERN = re.compile(r"^UC[\w-]{20,24}$")
_ISO8601_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?$"
)


# ---------------------------------------------------------------------------
# Credential state reporting (never the value itself)
# ---------------------------------------------------------------------------

def describe_credential_state(value) -> str:
    """Returns 'configured' or 'missing' -- NEVER the credential value."""
    return "configured" if value else "missing"


# ---------------------------------------------------------------------------
# Error / URL sanitization -- apply before printing or persisting anything
# that originated from an API exception or response.
# ---------------------------------------------------------------------------

_KEY_QUERY_PARAM_PATTERN = re.compile(r"(?i)\b(key|token|access_token|secret)=([^&\s\"']+)")


def sanitize_error_text(text, api_key: str | None = None) -> str:
    """Strips API-key-shaped query params and the literal key value (if
    supplied) from an error/exception message before it is logged or
    persisted. googleapiclient HttpError messages can embed the full request
    URL, which includes `key=<...>` -- this must never reach disk or stdout."""
    text = str(text)
    if api_key:
        text = text.replace(api_key, "***REDACTED***")
    text = _KEY_QUERY_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
    return text


def sanitize_url(url: str) -> str:
    """Removes the key= query parameter value from a URL before it is ever
    printed or logged."""
    return _KEY_QUERY_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", str(url))


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

def build_client(api_key: str):
    """Constructs a googleapiclient YouTube v3 client. Raises ValueError if
    api_key is falsy (callers should check describe_credential_state first
    and skip cleanly rather than call this with no key)."""
    if not api_key:
        raise ValueError("YOUTUBE_DATA_API_KEY yok; build_client cagrilmamali.")
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def classify_api_error(exc) -> str:
    """Best-effort classification of a googleapiclient HttpError into an
    actionable category string, used for connection-test diagnostics."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc.resp, "status", None)
            reason = ""
            try:
                reason = json.loads(exc.content.decode("utf-8")).get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                pass
            if status == 400:
                return "invalid_request"
            if status == 401:
                return "invalid_or_revoked_key"
            if status == 403:
                if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    return "quota_exceeded"
                if reason in ("accessNotConfigured", "forbidden"):
                    return "api_not_enabled_or_access_denied"
                return "forbidden"
            if status == 404:
                return "not_found"
            if status and status >= 500:
                return "server_error"
    except Exception:
        pass
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "network_failure"
    return "unknown_error"


CONNECTION_TEST_EXPLANATIONS = {
    "missing_key": "YOUTUBE_DATA_API_KEY Colab Secrets'ta yok veya notebook erisimine izin verilmedi. Sol 🔑 panelinden anahtari kontrol et ve bu notebook icin 'Notebook access' anahtarini AC.",
    "invalid_or_revoked_key": "API anahtari gecersiz veya iptal edilmis. Google Cloud Console -> APIs & Services -> Credentials uzerinden anahtari kontrol et.",
    "api_not_enabled_or_access_denied": "YouTube Data API v3 bu projede etkinlestirilmemis olabilir. Google Cloud Console -> APIs & Services -> Library -> 'YouTube Data API v3' -> Enable.",
    "quota_exceeded": "Gunluk kota asilmis olabilir. Kesin kota durumu icin Google Cloud Console -> APIs & Services -> Quotas sayfasina bak (buradaki degerler sadece tahmindir).",
    "not_found": "Istenen kanal bulunamadi. Kanal ID'sinin UC... ile basladigindan veya handle'in dogru yazildigindan emin ol.",
    "network_failure": "Aga ulasilamadi (baglanti zaman asimi/DNS/proxy sorunu olabilir). Internet baglantisini kontrol edip tekrar dene.",
    "invalid_request": "Istek gecersiz (parametre hatasi). Kanal girdisinin formatini kontrol et.",
    "forbidden": "Istek reddedildi (yetki/erisim sorunu).",
    "server_error": "YouTube API tarafinda gecici bir sunucu hatasi olustu; birazdan tekrar dene.",
    "unknown_error": "Beklenmeyen bir hata olustu; asagidaki (temizlenmis) hata mesajina bak.",
}


def run_connection_test(youtube_client, api_key: str, test_input: str) -> dict:
    """Performs ONE low-cost channels.list read call and returns a result
    dict. Never includes the API key or a raw request URL in the output."""
    result = {
        "status": "failed",
        "tested_input": test_input,
        "channel_title": None,
        "channel_id": None,
        "has_uploads_playlist": False,
        "error_category": None,
        "explanation": None,
    }
    if not api_key:
        result["error_category"] = "missing_key"
        result["explanation"] = CONNECTION_TEST_EXPLANATIONS["missing_key"]
        return result
    try:
        record = resolve_channel(youtube_client, test_input, api_key_for_sanitization=api_key)
        if not record["resolved"]:
            result["error_category"] = "not_found"
            result["explanation"] = CONNECTION_TEST_EXPLANATIONS["not_found"] + f" ({record.get('error')})"
            return result
        result["status"] = "ok"
        result["channel_title"] = record["title"]
        result["channel_id"] = record["channel_id"]
        result["has_uploads_playlist"] = bool(record["uploads_playlist_id"])
        return result
    except Exception as exc:
        category = classify_api_error(exc)
        result["error_category"] = category
        result["explanation"] = CONNECTION_TEST_EXPLANATIONS.get(category, CONNECTION_TEST_EXPLANATIONS["unknown_error"])
        result["sanitized_error"] = sanitize_error_text(exc, api_key)
        return result


# ---------------------------------------------------------------------------
# Channel input resolution (channel ID or @handle -> canonical channel record)
# ---------------------------------------------------------------------------

def is_channel_id(value: str) -> bool:
    return bool(CHANNEL_ID_PATTERN.match(value.strip()))


def is_handle(value: str) -> bool:
    return value.strip().startswith("@")


def resolve_channel(youtube_client, raw_input: str, api_key_for_sanitization: str | None = None) -> dict:
    """Resolves ONE channel input (a 'UC...' channel ID or an '@handle') to
    canonical channel metadata via channels.list (never search.list). Returns
    a dict; on failure `resolved` is False and `error` explains why -- the
    caller is expected to record this and continue with other channels
    rather than aborting the whole batch."""
    value = raw_input.strip()
    record = {
        "input": raw_input, "resolved": False, "channel_id": None, "title": None,
        "uploads_playlist_id": None, "subscriber_count": None, "video_count": None,
        "error": None,
    }
    try:
        if is_channel_id(value):
            resp = youtube_client.channels().list(part="id,snippet,contentDetails,statistics", id=value).execute()
        elif is_handle(value):
            resp = youtube_client.channels().list(part="id,snippet,contentDetails,statistics", forHandle=value).execute()
        else:
            record["error"] = f"gecersiz girdi formati (UC... kanal ID'si veya @handle bekleniyor): {raw_input!r}"
            return record

        items = resp.get("items", [])
        if not items:
            record["error"] = f"kanal bulunamadi: {raw_input!r}"
            return record

        item = items[0]
        record["resolved"] = True
        record["channel_id"] = item["id"]
        record["title"] = item.get("snippet", {}).get("title")
        record["uploads_playlist_id"] = (
            item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        )
        stats = item.get("statistics", {})
        record["subscriber_count"] = stats.get("subscriberCount")
        record["video_count"] = stats.get("videoCount")
    except Exception as exc:
        record["error"] = sanitize_error_text(exc, api_key_for_sanitization)
    return record


def resolve_channels(youtube_client, raw_inputs: list[str], api_key_for_sanitization: str | None = None) -> tuple[list[dict], list[dict]]:
    """Resolves a list of channel inputs. Returns (resolved_records,
    failed_records). Deduplicates by resolved channel_id (first occurrence
    wins) AFTER resolution -- two different inputs (e.g. an ID and its own
    handle) that resolve to the same channel collapse to one entry. A failure
    on one input never stops the others from being attempted."""
    resolved, failed, seen_channel_ids = [], [], set()
    for raw_input in raw_inputs:
        record = resolve_channel(youtube_client, raw_input, api_key_for_sanitization)
        if not record["resolved"]:
            failed.append(record)
            continue
        if record["channel_id"] in seen_channel_ids:
            record["duplicate_of_already_resolved"] = True
            continue
        seen_channel_ids.add(record["channel_id"])
        resolved.append(record)
    return resolved, failed


# ---------------------------------------------------------------------------
# ISO-8601 duration parsing
# ---------------------------------------------------------------------------

def parse_iso8601_duration(duration_str) -> float | None:
    """Parses an ISO-8601 duration string (e.g. 'PT4M13S') into seconds.
    Returns None if the string is missing or unparseable -- never raises."""
    if not duration_str:
        return None
    match = _ISO8601_DURATION_PATTERN.match(duration_str.strip())
    if not match:
        return None
    parts = match.groupdict()
    days = float(parts["days"] or 0)
    hours = float(parts["hours"] or 0)
    minutes = float(parts["minutes"] or 0)
    seconds = float(parts["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def is_short_duration_candidate(duration_sec) -> bool | None:
    """HEURISTIC ONLY. A duration <= SHORT_DURATION_HEURISTIC_MAX_SEC is
    necessary but not sufficient for a video to be a YouTube Short (aspect
    ratio and the Shorts player/shelf also matter, and the Data API does not
    expose a direct 'is Short' flag). Returns None if duration is unknown."""
    if duration_sec is None:
        return None
    return duration_sec <= SHORT_DURATION_HEURISTIC_MAX_SEC


# ---------------------------------------------------------------------------
# Disk cache (atomic writes, TTL-aware, corrupt-file-safe)
# ---------------------------------------------------------------------------

class Cache:
    """A simple per-endpoint JSON disk cache with TTL support and cache-stat
    tracking (hit/miss/refresh), used so repeated runs don't re-spend quota
    unnecessarily while still refreshing statistics that go stale.

    Never deletes files it doesn't own; corrupt cache entries are simply
    treated as a miss and safely overwritten (atomic write: write to a temp
    file in the same directory, then os.replace) rather than raising.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"hits": 0, "misses": 0, "refreshes": 0, "corrupt_replaced": 0}

    def _path(self, endpoint: str, cache_key: str) -> Path:
        # Uniqueness MUST depend on the full (endpoint, cache_key) pair via a
        # cryptographic digest -- two distinct videos.list batches that share
        # the same first 180 sanitized characters (e.g. two 50-ID batches that
        # only differ near the end) previously collided on the same cache
        # file, silently returning one batch's cached response for the other.
        # The short sanitized/truncated prefix is kept ONLY for human-readable
        # filenames; it is never relied upon for uniqueness.
        digest = hashlib.sha256(f"{endpoint}\x00{cache_key}".encode("utf-8")).hexdigest()
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", cache_key)[:60]
        return self.cache_dir / f"{endpoint}_{safe_key}_{digest}.json"

    def get(self, endpoint: str, cache_key: str, ttl_hours: float | None):
        """Returns the cached payload dict if present and (when ttl_hours is
        given) not older than ttl_hours; otherwise returns None. ttl_hours=None
        means 'never expires' (used for channel identity / uploads-playlist
        metadata, which changes rarely)."""
        path = self._path(endpoint, cache_key)
        if not path.is_file():
            self.stats["misses"] += 1
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
            cached_at = datetime.fromisoformat(envelope["cached_at"])
        except Exception:
            self.stats["corrupt_replaced"] += 1
            self.stats["misses"] += 1
            return None
        if ttl_hours is not None:
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600.0
            if age_hours > ttl_hours:
                self.stats["refreshes"] += 1
                return None
        self.stats["hits"] += 1
        return envelope["data"]

    def set(self, endpoint: str, cache_key: str, data) -> None:
        path = self._path(endpoint, cache_key)
        envelope = {"cached_at": datetime.now(timezone.utc).isoformat(), "data": data}
        tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic on both POSIX and Windows


# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------

class QuotaTracker:
    """Tracks estimated quota usage by endpoint, plus cache hits (which cost
    zero quota because no API call was made). These numbers are ESTIMATES
    based on documented per-call costs at the time this module was written;
    Google Cloud Console's Quotas page is the authoritative source, never
    this tracker."""

    def __init__(self):
        self.calls = {k: 0 for k in QUOTA_COST}
        self.cache_hits = {k: 0 for k in QUOTA_COST}

    def record_call(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def record_cache_hit(self, endpoint: str) -> None:
        self.cache_hits[endpoint] = self.cache_hits.get(endpoint, 0) + 1

    def estimated_total(self) -> int:
        return sum(self.calls[k] * QUOTA_COST.get(k, 1) for k in self.calls)

    def to_dict(self) -> dict:
        search_calls = self.calls.get("search.list", 0)
        return {
            "note": (
                "bu degerler TAHMINDIR; search.list ayri arama-cagrisi kotasindan, "
                "diger read endpointleri genel kotadan dusulur. Kesin/guncel durum icin "
                "Google Cloud Console -> APIs & Services -> Quotas sayfasina bak"
            ),
            "calls_by_endpoint": dict(self.calls),
            "quota_cost_per_call": dict(QUOTA_COST),
            "estimated_quota_units_used": self.estimated_total(),
            "search_query_calls_used": search_calls,
            "search_query_default_daily_limit": DEFAULT_SEARCH_CALL_LIMIT_PER_DAY,
            "search_query_calls_remaining_estimate": max(
                0, DEFAULT_SEARCH_CALL_LIMIT_PER_DAY - search_calls
            ),
            "cache_hits_by_endpoint": dict(self.cache_hits),
        }


# ---------------------------------------------------------------------------
# Cached, quota-tracked API call wrapper
# ---------------------------------------------------------------------------

def cached_call(cache: Cache, quota: QuotaTracker, endpoint: str, cache_key: str,
                 ttl_hours: float | None, request_factory, api_key: str | None = None):
    """Looks up (endpoint, cache_key) in cache; on miss/expiry, calls
    request_factory() (a zero-arg callable returning a googleapiclient
    request), executes it, records quota usage, caches the result, and
    returns it. request_factory is only invoked on a cache miss."""
    cached = cache.get(endpoint, cache_key, ttl_hours)
    if cached is not None:
        quota.record_cache_hit(endpoint)
        return cached
    request = request_factory()
    response = request.execute()
    quota.record_call(endpoint)
    cache.set(endpoint, cache_key, response)
    return response


# ---------------------------------------------------------------------------
# Upload-history collection
# ---------------------------------------------------------------------------

def get_uploads_playlist_id(youtube_client, cache: Cache, quota: QuotaTracker, channel_id: str,
                             channel_cache_ttl_hours: float | None, api_key: str | None = None):
    response = cached_call(
        cache, quota, "channels.list", channel_id, channel_cache_ttl_hours,
        lambda: youtube_client.channels().list(part="contentDetails", id=channel_id),
        api_key,
    )
    items = response.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_playlist_video_ids(youtube_client, cache: Cache, quota: QuotaTracker, playlist_id: str,
                            max_videos: int, channel_cache_ttl_hours: float | None,
                            api_key: str | None = None) -> list[str]:
    video_ids: list[str] = []
    page_token = None
    page = 0
    while len(video_ids) < max_videos:
        page += 1
        cache_key = f"{playlist_id}_p{page}"
        response = cached_call(
            cache, quota, "playlistItems.list", cache_key, channel_cache_ttl_hours,
            lambda pt=page_token: youtube_client.playlistItems().list(
                part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=pt,
            ),
            api_key,
        )
        video_ids.extend(item["contentDetails"]["videoId"] for item in response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return video_ids[:max_videos]


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_video_item(item: dict, collected_at_iso: str) -> dict:
    """Converts one raw videos.list item into a NORMALIZED_VIDEO_COLUMNS row.
    Handles hidden likes/comments (statistics.*Count simply absent when
    hidden) and any other missing field without raising."""
    snippet = item.get("snippet", {}) or {}
    stats = item.get("statistics", {}) or {}
    content = item.get("contentDetails", {}) or {}
    status = item.get("status", {}) or {}
    duration_iso = content.get("duration")
    duration_sec = parse_iso8601_duration(duration_iso)
    return {
        "video_id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "published_at": snippet.get("publishedAt"),
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "view_count": _safe_int(stats.get("viewCount")),
        "like_count": _safe_int(stats.get("likeCount")),  # None = hidden or absent, not zero
        "comment_count": _safe_int(stats.get("commentCount")),  # None = hidden/disabled
        "duration_iso8601": duration_iso,
        "duration_sec": duration_sec,
        "tags": snippet.get("tags", []),
        "category_id": snippet.get("categoryId"),
        "default_language": snippet.get("defaultLanguage") or snippet.get("defaultAudioLanguage"),
        "caption_available": content.get("caption") == "true",
        "definition": content.get("definition"),
        "privacy_status": status.get("privacyStatus"),
        "is_short_duration_candidate": is_short_duration_candidate(duration_sec),
        "collected_at": collected_at_iso,
    }


def get_videos_details(youtube_client, cache: Cache, quota: QuotaTracker, video_ids: list[str],
                        video_stats_ttl_hours: float, api_key: str | None = None) -> list[dict]:
    """Fetches videos.list details in batches of at most 50 IDs (per official
    quota-efficiency guidance), applying the shorter, configurable TTL used
    for statistics specifically (view/like/comment counts go stale)."""
    collected_at_iso = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for batch_start in range(0, len(video_ids), 50):
        batch = video_ids[batch_start:batch_start + 50]
        cache_key = "_".join(sorted(batch))
        response = cached_call(
            cache, quota, "videos.list", cache_key, video_stats_ttl_hours,
            lambda b=batch: youtube_client.videos().list(part="snippet,statistics,contentDetails,status", id=",".join(b)),
            api_key,
        )
        for item in response.get("items", []):
            rows.append(normalize_video_item(item, collected_at_iso))
    return rows


# ---------------------------------------------------------------------------
# Keyword-based discovery (read-only search -> detail -> heuristic ranking)
# ---------------------------------------------------------------------------

def _normalize_discovery_terms(terms: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw_term in terms or []:
        term = " ".join(str(raw_term).split()).strip()
        key = term.casefold()
        if term and key not in seen:
            normalized.append(term)
            seen.add(key)
    return normalized


def _parse_datetime_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high <= low:
        return [1.0 if high > 0 else 0.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def search_video_ids(
    youtube_client,
    cache: Cache,
    quota: QuotaTracker,
    term: str,
    order: str,
    published_after: str,
    max_results: int,
    cache_ttl_hours: float,
    region_code: str | None = None,
    relevance_language: str | None = None,
    safe_search: str = "moderate",
    api_key: str | None = None,
) -> list[str]:
    """Runs exactly one search.list page and returns video IDs.

    Pagination is intentionally absent: every extra page consumes another
    search call. The caller controls the total through max_search_calls.
    """
    if order not in DISCOVERY_ALLOWED_ORDERS:
        raise ValueError(f"Desteklenmeyen YouTube search order: {order}")
    limit = max(1, min(50, int(max_results)))
    params = {
        "part": "snippet",
        "q": term,
        "type": "video",
        "order": order,
        "publishedAfter": published_after,
        "maxResults": limit,
        "safeSearch": safe_search,
    }
    if region_code:
        params["regionCode"] = region_code
    if relevance_language:
        params["relevanceLanguage"] = relevance_language
    cache_key = json.dumps(params, ensure_ascii=False, sort_keys=True)
    response = cached_call(
        cache,
        quota,
        "search.list",
        cache_key,
        cache_ttl_hours,
        lambda: youtube_client.search().list(**params),
        api_key,
    )
    video_ids = []
    for item in response.get("items", []):
        identifier = item.get("id", {})
        video_id = identifier.get("videoId") if isinstance(identifier, dict) else None
        if video_id:
            video_ids.append(video_id)
    return video_ids


def score_discovered_videos(
    videos: list[dict],
    matches_by_video: dict[str, dict],
    now: datetime | None = None,
    shorts_only: bool = True,
) -> list[dict]:
    """Ranks candidates with transparent, local signals (not an LLM claim).

    Score weights: view velocity 55%, recency 20%, engagement 15%, repeated
    appearance across keyword/order queries 10%. Missing public statistics
    are treated as unavailable, never fabricated.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prepared = []
    for source in deduplicate_videos(videos):
        if shorts_only and source.get("is_short_duration_candidate") is not True:
            continue
        row = dict(source)
        published = _parse_datetime_utc(row.get("published_at"))
        age_hours = max(1.0, (now - published).total_seconds() / 3600) if published else 1.0
        views = row.get("view_count")
        likes = row.get("like_count")
        comments = row.get("comment_count")
        views_per_hour = max(0, views or 0) / age_hours
        known_engagement = sum(value for value in (likes, comments) if value is not None)
        engagement_rate = known_engagement / views if views and views > 0 else 0.0
        match = matches_by_video.get(row.get("video_id"), {})
        terms = sorted(match.get("terms", []), key=str.casefold)
        orders = sorted(match.get("orders", []), key=str.casefold)
        row.update({
            "matched_terms": terms,
            "matched_query_count": int(match.get("query_count", 0)),
            "search_orders": orders,
            "video_age_hours": round(age_hours, 3),
            "views_per_hour": round(views_per_hour, 3),
            "engagement_rate": round(engagement_rate, 6),
        })
        prepared.append(row)

    channel_counts = {}
    for row in prepared:
        channel_id = row.get("channel_id")
        if channel_id:
            channel_counts[channel_id] = channel_counts.get(channel_id, 0) + 1

    velocity_norm = _minmax([math.log1p(row["views_per_hour"]) for row in prepared])
    engagement_norm = _minmax([row["engagement_rate"] for row in prepared])
    max_channel_count = max(channel_counts.values(), default=1)
    max_query_count = max((row["matched_query_count"] for row in prepared), default=1)
    for index, row in enumerate(prepared):
        age_hours = row["video_age_hours"]
        recency = 2 ** (-age_hours / (24 * 7))
        channel_repeat = channel_counts.get(row.get("channel_id"), 0) / max_channel_count
        query_repeat = row["matched_query_count"] / max_query_count
        repeat = (channel_repeat + query_repeat) / 2
        score = 100 * (
            0.55 * velocity_norm[index]
            + 0.20 * recency
            + 0.15 * engagement_norm[index]
            + 0.10 * repeat
        )
        row.update({
            "velocity_score": round(velocity_norm[index] * 100, 3),
            "engagement_score": round(engagement_norm[index] * 100, 3),
            "recency_score": round(recency * 100, 3),
            "channel_repeat_score": round(repeat * 100, 3),
            "trend_score": round(score, 3),
        })

    prepared.sort(
        key=lambda row: (row["trend_score"], row["views_per_hour"], row.get("video_id") or ""),
        reverse=True,
    )
    for rank, row in enumerate(prepared, 1):
        row["discovery_rank"] = rank
    return prepared


def rank_discovered_channels(
    videos: list[dict],
    top_n: int = 10,
    min_matched_videos: int = 1,
) -> list[dict]:
    groups = {}
    for video in videos:
        channel_id = video.get("channel_id")
        if not channel_id:
            continue
        group = groups.setdefault(channel_id, {
            "channel_id": channel_id,
            "channel_title": video.get("channel_title"),
            "videos": [],
            "terms": set(),
        })
        group["videos"].append(video)
        group["terms"].update(video.get("matched_terms") or [])

    rows = []
    for group in groups.values():
        matched_count = len(group["videos"])
        if matched_count < max(1, int(min_matched_videos)):
            continue
        scores = [float(video.get("trend_score") or 0) for video in group["videos"]]
        rows.append({
            "channel_id": group["channel_id"],
            "channel_title": group["channel_title"],
            "matched_video_count": matched_count,
            "matched_terms": sorted(group["terms"], key=str.casefold),
            "total_view_count": sum(int(video.get("view_count") or 0) for video in group["videos"]),
            "max_views_per_hour": round(max(float(video.get("views_per_hour") or 0) for video in group["videos"]), 3),
            "mean_trend_score": round(sum(scores) / len(scores), 3),
            "max_trend_score": round(max(scores), 3),
        })
    rows.sort(
        key=lambda row: (
            row["max_trend_score"], row["mean_trend_score"],
            row["matched_video_count"], row["channel_id"],
        ),
        reverse=True,
    )
    rows = rows[:max(0, int(top_n))]
    for rank, row in enumerate(rows, 1):
        row["discovery_rank"] = rank
    return rows


def discover_trending_content(
    youtube_client,
    cache: Cache,
    quota: QuotaTracker,
    terms: list[str],
    lookback_days: int = 14,
    region_code: str = "US",
    relevance_language: str = "en",
    orders: tuple[str, ...] = ("date", "viewCount"),
    max_results_per_query: int = 25,
    max_search_calls: int = 10,
    top_videos: int = 50,
    top_channels: int = 10,
    min_channel_matched_videos: int = 1,
    shorts_only: bool = True,
    cache_ttl_hours: float = 6,
    safe_search: str = "moderate",
    api_key: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Discovers recent keyword-matched videos and candidate channels.

    This is a heuristic opportunity shortlist, not a claim that YouTube has
    labelled the results as globally trending. It never uploads or mutates.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    terms = _normalize_discovery_terms(terms)
    orders = tuple(dict.fromkeys(orders or ("date",)))
    invalid_orders = [order for order in orders if order not in DISCOVERY_ALLOWED_ORDERS]
    if invalid_orders:
        raise ValueError(f"Desteklenmeyen YouTube search order: {invalid_orders}")
    max_search_calls = max(0, min(DEFAULT_SEARCH_CALL_LIMIT_PER_DAY, int(max_search_calls)))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    published_after = (now - timedelta(days=max(1, int(lookback_days)))).isoformat().replace("+00:00", "Z")
    plan = [(term, order) for term in terms for order in orders][:max_search_calls]
    matches_by_video = {}
    errors = []
    executed = 0
    for term, order in plan:
        try:
            ids = search_video_ids(
                youtube_client, cache, quota, term, order, published_after,
                max_results_per_query, cache_ttl_hours, region_code,
                relevance_language, safe_search, api_key,
            )
            executed += 1
            for video_id in ids:
                match = matches_by_video.setdefault(
                    video_id, {"terms": set(), "orders": set(), "query_count": 0}
                )
                match["terms"].add(term)
                match["orders"].add(order)
                match["query_count"] += 1
        except Exception as exc:
            errors.append({
                "stage": "search.list", "term": term, "order": order,
                "error": sanitize_error_text(exc, api_key),
            })

    video_ids = list(matches_by_video)
    details = get_videos_details(
        youtube_client, cache, quota, video_ids, cache_ttl_hours, api_key
    ) if video_ids else []
    videos = score_discovered_videos(details, matches_by_video, now, shorts_only)
    videos = videos[:max(0, int(top_videos))]
    for rank, row in enumerate(videos, 1):
        row["discovery_rank"] = rank
    channels = rank_discovered_channels(videos, top_channels, min_channel_matched_videos)
    if videos and channels:
        status = "review_required"
        status_reason = (
            f"{len(videos)} aday video ve {len(channels)} aday kanal bulundu; "
            "trend skoru sezgiseldir, insan incelemesi gerekli"
        )
    elif video_ids and not videos:
        status = "insufficient_data"
        status_reason = "arama sonucu bulundu fakat filtrelerden gecen video/kanal kalmadi"
    elif errors and not video_ids:
        status = "failed"
        status_reason = "tum YouTube arama sorgulari basarisiz oldu"
    else:
        status = "insufficient_data"
        status_reason = "anahtar kelimeler icin video bulunamadi"
    return {
        "status": status,
        "status_reason": status_reason,
        "pipeline_role": "research_sidecar",
        "feeds_main_pipeline": False,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "terms": terms, "lookback_days": int(lookback_days),
            "region_code": region_code, "relevance_language": relevance_language,
            "orders": list(orders), "max_results_per_query": int(max_results_per_query),
            "max_search_calls": max_search_calls, "top_videos": int(top_videos),
            "top_channels": int(top_channels),
            "min_channel_matched_videos": int(min_channel_matched_videos),
            "shorts_only": bool(shorts_only), "safe_search": safe_search,
        },
        "search_calls_planned": len(plan),
        "search_calls_executed": executed,
        "raw_search_result_count": sum(
            match["query_count"] for match in matches_by_video.values()
        ),
        "unique_video_count": len(video_ids),
        "videos": videos,
        "channels": channels,
        "errors": errors,
    }


def collect_channel_uploads(youtube_client, cache: Cache, quota: QuotaTracker, channel_record: dict,
                             max_videos_per_channel: int, channel_cache_ttl_hours: float,
                             video_stats_ttl_hours: float, api_key: str | None = None) -> tuple[list[dict], str | None]:
    """Collects up to max_videos_per_channel normalized video rows for one
    RESOLVED channel record (as returned by resolve_channel). Returns
    (rows, error) -- error is None on success; a failure here does not raise,
    so the caller can continue with other channels."""
    try:
        playlist_id = channel_record.get("uploads_playlist_id")
        if not playlist_id:
            playlist_id = get_uploads_playlist_id(
                youtube_client, cache, quota, channel_record["channel_id"], channel_cache_ttl_hours, api_key,
            )
        if not playlist_id:
            return [], f"kanal icin uploads playlist bulunamadi: {channel_record.get('channel_id')}"
        video_ids = get_playlist_video_ids(
            youtube_client, cache, quota, playlist_id, max_videos_per_channel, channel_cache_ttl_hours, api_key,
        )
        rows = get_videos_details(youtube_client, cache, quota, video_ids, video_stats_ttl_hours, api_key)
        return rows, None
    except Exception as exc:
        return [], sanitize_error_text(exc, api_key)


def deduplicate_videos(rows: list[dict]) -> list[dict]:
    """Deduplicates by video_id, first occurrence wins."""
    seen = set()
    out = []
    for row in rows:
        vid = row.get("video_id")
        if vid in seen:
            continue
        seen.add(vid)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Output artifact writers
# ---------------------------------------------------------------------------

def write_normalized_videos_csv(path: Path, videos: list[dict]) -> None:
    """Always writes a stable-schema CSV (header row present) even when
    `videos` is empty, so downstream readers never hit
    pandas.errors.EmptyDataError."""
    import pandas as pd

    df = pd.DataFrame(videos, columns=NORMALIZED_VIDEO_COLUMNS) if videos else pd.DataFrame(columns=NORMALIZED_VIDEO_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _write_stable_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    import pandas as pd

    df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def write_discovered_videos_csv(path: Path, videos: list[dict]) -> None:
    """Writes discovery candidates with a stable schema, including empty runs."""
    _write_stable_csv(path, videos, DISCOVERED_VIDEO_COLUMNS)


def write_discovered_channels_csv(path: Path, channels: list[dict]) -> None:
    """Writes ranked channel candidates with a stable schema."""
    _write_stable_csv(path, channels, DISCOVERED_CHANNEL_COLUMNS)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_collection_manifest(
    status: str, status_reason: str, started_at: str, completed_at: str,
    requested_channel_count: int, resolved_channel_count: int, failed_channel_count: int,
    video_count: int, date_range: dict, cache_statistics: dict, connection_test_status,
    output_paths: dict, discovery: dict | None = None,
) -> dict:
    assert status in STATUS_VALUES, f"Bilinmeyen status: {status}"
    return {
        "status": status,
        "status_reason": status_reason,
        "pipeline_role": "research_sidecar",
        "feeds_main_pipeline": False,
        "started_at": started_at,
        "completed_at": completed_at,
        "requested_channel_count": requested_channel_count,
        "resolved_channel_count": resolved_channel_count,
        "failed_channel_count": failed_channel_count,
        "video_count": video_count,
        "date_range": date_range,
        "cache_statistics": cache_statistics,
        "connection_test_status": connection_test_status,
        "discovery": discovery,
        "output_paths": {k: str(v) for k, v in output_paths.items()},
    }


def compute_date_range(videos: list[dict]) -> dict:
    published = [v.get("published_at") for v in videos if v.get("published_at")]
    if not published:
        return {"start": None, "end": None}
    return {"start": min(published), "end": max(published)}


# ---------------------------------------------------------------------------
# Path resolution (local Windows / Colab-with-Drive / Colab-without-Drive)
# ---------------------------------------------------------------------------

def resolve_project_paths(is_colab: bool, use_google_drive: bool, project_root: Path,
                           drive_project_root: str = "/content/drive/MyDrive/AVRUPA YAKASI/YoutubeOtomationv0") -> dict:
    """Returns a dict of Path objects: results_dir, youtube_data_dir,
    cache_dir, source_videos_dir, drive_mounted (bool). Mounts Google Drive
    ONLY when is_colab and use_google_drive are both True. Never assumes a
    Windows filesystem path exists in Colab, and never assumes /content
    exists locally."""
    drive_mounted = False
    if is_colab and use_google_drive:
        try:
            from google.colab import drive as colab_drive

            colab_drive.mount("/content/drive", force_remount=False)
            drive_mounted = True
        except Exception as exc:
            print(f"UYARI: Google Drive mount edilemedi ({exc}); yerel/gecici /content yoluna dusuluyor.")

    if drive_mounted:
        base = Path(drive_project_root)
    elif is_colab:
        base = Path("/content")
    else:
        base = Path(project_root)

    paths = {
        "base": base,
        "results_dir": base / "results",
        "youtube_data_dir": base / "results" / "youtube_data",
        "cache_dir": base / "cache" / "youtube_data",
        "source_videos_dir": base / "source_videos",
        "drive_mounted": drive_mounted,
    }
    for key in ("results_dir", "youtube_data_dir", "cache_dir", "source_videos_dir"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def find_normalized_videos_csv(candidate_dirs: list) -> Path | None:
    """Searches a list of candidate directories (in order) for
    normalized_videos.csv and returns the first match, or None."""
    for d in candidate_dirs:
        if not d:
            continue
        candidate = Path(d) / "normalized_videos.csv"
        if candidate.is_file():
            return candidate
    return None
