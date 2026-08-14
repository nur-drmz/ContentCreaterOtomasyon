"""YouTube publication provider: OAuth 2.0 + resumable upload + visibility change.

WHY THIS MODULE EXISTS
----------------------
`youtube_data.py` is deliberately read-only and key-based. Publishing needs
something categorically different: a USER-authorized OAuth 2.0 credential with
the `youtube.upload` scope, a refresh token that must survive restarts, and a
write endpoint (`videos.insert`) that costs 1600 quota units per call -- roughly
six uploads per day on the default 10,000-unit quota.

This module implements the `publish.PublishProvider` contract for YouTube and
nothing more. Every gate lives in `publish.py`; this file only performs the
operations those gates have already authorized.

SECURITY CONTRACT (do not violate when editing this file)
---------------------------------------------------------
- The refresh token is written to its own file with 0600 permissions, OUTSIDE
  `.env.local`, and is never returned by any function, never logged, never
  written to an artifact. `describe_credential_state()` reports only
  `configured` / `missing` plus non-secret metadata.
- Access tokens live in memory only.
- Every error string passes through `sanitize_publish_error` before it reaches an
  artifact, because Google error bodies can echo the request URL and tokens.
- The HTTP layer is injected, so the whole flow is testable without network
  access and without a real credential.
- The first upload is ALWAYS `privacyStatus=private` (`publish.INITIAL_PRIVACY`);
  this module refuses to send anything else on insert, even if asked.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .publish import (
    INITIAL_PRIVACY,
    PLATFORM_YOUTUBE,
    VISIBILITY_VALUES,
    PublishProvider,
    PublishResult,
    PublishTarget,
)

OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
OAUTH_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"

#: Documented quota costs. `videos.insert` at 1600 units is the single most
#: expensive call this project can make -- about 6 uploads/day on the default
#: 10,000-unit daily quota. Surfaced so the console can warn BEFORE uploading.
QUOTA_UNITS = {
    "videos.insert": 1600,
    "videos.update": 50,
    "channels.list": 1,
}
DEFAULT_DAILY_QUOTA_UNITS = 10_000

#: 8 MiB chunks: large enough to keep throughput high, small enough that a
#: dropped connection does not lose much progress. Must be a multiple of 256 KiB
#: per Google's resumable-upload requirement.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_CHUNK_ALIGNMENT = 256 * 1024

DEFAULT_TOKEN_FILENAME = "youtube_oauth_token.json"
DEFAULT_CATEGORY_ID = "22"  # People & Blogs
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS_TOTAL_CHARS = 500

_SECRET_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|id_token|code|key|token)"
    r"[\"'\s:=]+([A-Za-z0-9._\-/+]{8,})"
)


class PublishAuthError(RuntimeError):
    """Credential/authorization failure. Never carries a token value."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_publish_error(text) -> str:
    """Removes token-shaped values from any error text before it is persisted."""
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=***REDACTED***", str(text))


# ---------------------------------------------------------------------------
# Client secrets (the app identity, still not the user credential)
# ---------------------------------------------------------------------------

def load_client_secrets(path=None, environ=None) -> dict:
    """Loads an installed-app OAuth client secrets JSON.

    Returns {'client_id', 'client_secret', 'redirect_uris'}. Raises
    PublishAuthError with an actionable message when absent -- the user has to
    create the OAuth client in Google Cloud Console themselves.
    """
    env = os.environ if environ is None else environ
    path = path or env.get("YOUTUBE_OAUTH_CLIENT_SECRETS_PATH")
    if not path:
        raise PublishAuthError(
            "YOUTUBE_OAUTH_CLIENT_SECRETS_PATH ayarlanmamis. Google Cloud Console -> "
            "APIs & Services -> Credentials -> Create OAuth client ID -> Desktop app "
            "ile bir istemci olustur, indirilen JSON dosyasinin yolunu .env.local "
            "icine yaz. (YouTube Data API v3 etkin olmali.)"
        )
    file_path = Path(path)
    if not file_path.is_file():
        raise PublishAuthError(f"OAuth client secrets dosyasi bulunamadi: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishAuthError(
            f"OAuth client secrets dosyasi okunamadi: {sanitize_publish_error(exc)}"
        ) from None

    block = payload.get("installed") or payload.get("web") or payload
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        raise PublishAuthError(
            "OAuth client secrets dosyasinda client_id/client_secret yok; "
            "'Desktop app' tipinde bir istemci indirdiginden emin ol."
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": list(block.get("redirect_uris") or ["http://localhost"]),
    }


# ---------------------------------------------------------------------------
# Token store (refresh token at rest)
# ---------------------------------------------------------------------------

class OAuthTokenStore:
    """Stores the refresh token on disk with 0600 permissions.

    Deliberately NOT in `.env.local`: that file is human-edited and displayed in
    the console's settings flow, while this one is machine-managed. No method
    ever returns the refresh token to a caller that is not performing a refresh.
    """

    def __init__(self, path=None, environ=None):
        env = os.environ if environ is None else environ
        resolved = path or env.get("YOUTUBE_OAUTH_TOKEN_PATH")
        if not resolved:
            resolved = Path(__file__).resolve().parent.parent / ".secrets" / DEFAULT_TOKEN_FILENAME
        self.path = Path(resolved)

    def exists(self) -> bool:
        return self.path.is_file()

    def describe_state(self) -> dict:
        """Non-secret status only: never includes a token value."""
        if not self.exists():
            return {
                "configured": False,
                "path_configured": True,
                "scopes": [],
                "channel_id": None,
                "channel_title": None,
                "authorized_at": None,
            }
        payload = self._read()
        return {
            "configured": bool(payload.get("refresh_token")),
            "path_configured": True,
            "scopes": list(payload.get("scopes") or []),
            "channel_id": payload.get("channel_id"),
            "channel_title": payload.get("channel_title"),
            "authorized_at": payload.get("authorized_at"),
        }

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt token file is treated as "not authorized" rather than
            # raising, so the user can simply re-authorize.
            return {}

    def save(self, refresh_token: str, *, scopes=(), channel_id=None,
             channel_title=None) -> dict:
        if not refresh_token:
            raise PublishAuthError(
                "refresh_token bos. Google yalnizca ilk yetkilendirmede refresh token "
                "dondurur; yetkilendirme URL'ini access_type=offline ve prompt=consent "
                "ile uret."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "refresh_token": refresh_token,
            "scopes": list(scopes or OAUTH_SCOPES),
            "channel_id": channel_id,
            "channel_title": channel_title,
            "authorized_at": _now().isoformat(),
        }
        tmp_path = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        # Create with restrictive permissions BEFORE any bytes are written, so
        # the token is never briefly world-readable.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows/exotic filesystems: best effort, never fatal.
        return {"saved": True, "path_configured": True}

    def _refresh_token(self) -> str:
        token = self._read().get("refresh_token")
        if not token:
            raise PublishAuthError(
                "YouTube yetkilendirmesi yok. Once OAuth akisini tamamla "
                "(build_authorization_url -> exchange_code)."
            )
        return token

    def clear(self) -> dict:
        self.path.unlink(missing_ok=True)
        return {"cleared": True}


# ---------------------------------------------------------------------------
# OAuth flow (installed app / loopback)
# ---------------------------------------------------------------------------

def build_authorization_url(client_secrets: dict, redirect_uri: str, *,
                            state: str = "", scopes=OAUTH_SCOPES) -> str:
    """Builds the consent URL. Performs no network call and reads no secret file.

    `access_type=offline` + `prompt=consent` are mandatory: without them Google
    returns no refresh token on re-authorization and the console would silently
    lose upload access after one hour.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": client_secrets["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{OAUTH_AUTH_ENDPOINT}?{urlencode(params)}"


def _post_form(url: str, data: dict, *, http_post=None) -> dict:
    """POSTs an x-www-form-urlencoded body and returns the parsed JSON.

    `http_post(url, data)` is injected in tests; the default uses `requests`.
    """
    if http_post is not None:
        return http_post(url, data)
    try:
        import requests  # noqa: PLC0415
    except ImportError as exc:
        raise PublishAuthError(
            "requests kurulu degil. Kurulum: pip install -r requirements-local.txt"
        ) from exc
    response = requests.post(url, data=data, timeout=60)
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": "invalid_response", "error_description": response.text[:500]}
    if response.status_code >= 400:
        raise PublishAuthError(
            "OAuth token istegi basarisiz "
            f"({response.status_code}): {sanitize_publish_error(payload.get('error_description') or payload.get('error'))}"
        )
    return payload


def exchange_code(client_secrets: dict, code: str, redirect_uri: str, *,
                  http_post=None) -> dict:
    """Exchanges the one-time authorization code for tokens.

    Returns {'access_token', 'refresh_token', 'expires_in', 'scope'}. The caller
    is expected to hand the refresh token straight to `OAuthTokenStore.save`
    and never store it anywhere else.
    """
    if not code:
        raise PublishAuthError("authorization code bos")
    payload = _post_form(
        OAUTH_TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client_secrets["client_id"],
            "client_secret": client_secrets["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        http_post=http_post,
    )
    if not payload.get("refresh_token"):
        raise PublishAuthError(
            "Google refresh_token dondurmedi. Yetkilendirme URL'i access_type=offline "
            "ve prompt=consent icermeli; aksi halde yukleme yetkisi bir saat sonra kaybolur."
        )
    return payload


class AccessTokenProvider:
    """Exchanges the stored refresh token for short-lived access tokens.

    Access tokens are held in memory only and re-fetched shortly before expiry.
    """

    #: Refresh this many seconds before the reported expiry, so a long upload
    #: does not die mid-chunk on a token that expired two seconds ago.
    EXPIRY_MARGIN_SEC = 120

    def __init__(self, client_secrets: dict, token_store: OAuthTokenStore, *, http_post=None):
        self.client_secrets = client_secrets
        self.token_store = token_store
        self._http_post = http_post
        self._access_token = None
        self._expires_at = None

    def _expired(self) -> bool:
        if not self._access_token or self._expires_at is None:
            return True
        return _now() >= self._expires_at

    def access_token(self) -> str:
        if not self._expired():
            return self._access_token
        payload = _post_form(
            OAUTH_TOKEN_ENDPOINT,
            {
                "client_id": self.client_secrets["client_id"],
                "client_secret": self.client_secrets["client_secret"],
                "refresh_token": self.token_store._refresh_token(),
                "grant_type": "refresh_token",
            },
            http_post=self._http_post,
        )
        token = payload.get("access_token")
        if not token:
            raise PublishAuthError(
                "access_token alinamadi; yetkilendirme iptal edilmis olabilir. "
                "OAuth akisini tekrar calistir."
            )
        expires_in = int(payload.get("expires_in") or 3600)
        self._access_token = token
        self._expires_at = _now() + timedelta(
            seconds=max(60, expires_in - self.EXPIRY_MARGIN_SEC)
        )
        return token


# ---------------------------------------------------------------------------
# Metadata shaping
# ---------------------------------------------------------------------------

def build_video_resource(metadata: dict, target: PublishTarget) -> dict:
    """Builds the `videos.insert` request body.

    `status.privacyStatus` is hardcoded to `INITIAL_PRIVACY`: the insert call is
    structurally incapable of publishing publicly, regardless of what the target
    eventually wants.
    """
    title = (metadata.get("title") or "").strip()
    if not title:
        raise ValueError("yayin basligi bos olamaz")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"baslik {MAX_TITLE_CHARS} karakteri asiyor ({len(title)})")

    description = (metadata.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"aciklama {MAX_DESCRIPTION_CHARS} karakteri asiyor ({len(description)})"
        )

    tags = [str(tag).strip() for tag in (metadata.get("tags") or []) if str(tag).strip()]
    while tags and sum(len(tag) + 1 for tag in tags) > MAX_TAGS_TOTAL_CHARS:
        tags.pop()

    snippet = {
        "title": title,
        "description": description,
        "tags": tags,
        "categoryId": str(metadata.get("category_id") or DEFAULT_CATEGORY_ID),
    }
    language = metadata.get("language")
    if language:
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language

    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": INITIAL_PRIVACY,
            "selfDeclaredMadeForKids": bool(target.made_for_kids),
            "embeddable": True,
            "license": "youtube",
        },
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class YouTubeProvider(PublishProvider):
    """`publish.PublishProvider` implementation for YouTube Data API v3.

    Contains NO gate logic. It is only reachable through
    `publish.execute_private_upload` / `publish.execute_visibility_change`,
    which have already verified the approval chain.
    """

    platform = PLATFORM_YOUTUBE

    def __init__(self, *, client_secrets=None, token_store=None, http=None,
                 http_post=None, chunk_bytes: int = UPLOAD_CHUNK_BYTES,
                 environ=None):
        self._client_secrets = client_secrets
        self._token_store = token_store or OAuthTokenStore(environ=environ)
        self._environ = environ
        # `http` is a minimal object with .request(method, url, headers=, data=,
        # json=, timeout=) returning an object with .status_code, .headers and
        # .json()/.text -- i.e. a requests.Session, or a double in tests.
        self._http = http
        self._http_post = http_post
        self._chunk_bytes = self._align_chunk(chunk_bytes)
        self._token_provider = None

    @staticmethod
    def _align_chunk(value: int) -> int:
        """Google requires every non-final chunk to be a multiple of 256 KiB."""
        value = max(_CHUNK_ALIGNMENT, int(value))
        return value - (value % _CHUNK_ALIGNMENT)

    # -- credentials -------------------------------------------------------

    def describe_credential_state(self) -> dict:
        """Reports readiness without touching the network or revealing a value."""
        state = {
            "platform": self.platform,
            "client_secrets_configured": False,
            "client_secrets_error": None,
        }
        try:
            self._secrets()
            state["client_secrets_configured"] = True
        except PublishAuthError as exc:
            state["client_secrets_error"] = str(exc)
        token_state = self._token_store.describe_state()
        state.update({
            "configured": bool(state["client_secrets_configured"] and token_state["configured"]),
            "authorized": token_state["configured"],
            "scopes": token_state["scopes"],
            "channel_id": token_state["channel_id"],
            "channel_title": token_state["channel_title"],
            "authorized_at": token_state["authorized_at"],
            "upload_quota_units_per_video": QUOTA_UNITS["videos.insert"],
            "estimated_uploads_per_day": DEFAULT_DAILY_QUOTA_UNITS // QUOTA_UNITS["videos.insert"],
        })
        return state

    def _secrets(self) -> dict:
        if self._client_secrets is None:
            self._client_secrets = load_client_secrets(environ=self._environ)
        return self._client_secrets

    def _tokens(self) -> AccessTokenProvider:
        if self._token_provider is None:
            self._token_provider = AccessTokenProvider(
                self._secrets(), self._token_store, http_post=self._http_post,
            )
        return self._token_provider

    def _session(self):
        if self._http is not None:
            return self._http
        try:
            import requests  # noqa: PLC0415
        except ImportError as exc:
            raise PublishAuthError(
                "requests kurulu degil. Kurulum: pip install -r requirements-local.txt"
            ) from exc
        self._http = requests.Session()
        return self._http

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._tokens().access_token()}"}

    # -- authorization helpers (used by the console) ------------------------

    def authorization_url(self, redirect_uri: str, *, state: str = "") -> str:
        return build_authorization_url(self._secrets(), redirect_uri, state=state)

    def complete_authorization(self, code: str, redirect_uri: str) -> dict:
        """Exchanges the code, persists the refresh token, and records which
        channel was authorized. Returns non-secret state only."""
        payload = exchange_code(
            self._secrets(), code, redirect_uri, http_post=self._http_post,
        )
        self._token_provider = None
        channel = {"channel_id": None, "channel_title": None}
        # Resolve the channel with the freshly issued access token so the console
        # can show WHICH channel is connected; a failure here must not lose the
        # refresh token, so it is caught.
        self._token_store.save(
            payload["refresh_token"],
            scopes=(payload.get("scope") or "").split() or OAUTH_SCOPES,
        )
        try:
            channel = self.fetch_authorized_channel()
            self._token_store.save(
                payload["refresh_token"],
                scopes=(payload.get("scope") or "").split() or OAUTH_SCOPES,
                channel_id=channel.get("channel_id"),
                channel_title=channel.get("channel_title"),
            )
        except Exception:
            pass
        return self.describe_credential_state()

    def fetch_authorized_channel(self) -> dict:
        """One `channels.list?mine=true` call (1 quota unit) to identify the
        connected channel."""
        response = self._session().request(
            "GET", CHANNELS_ENDPOINT,
            params={"part": "snippet", "mine": "true"},
            headers=self._auth_headers(), timeout=60,
        )
        payload = _json_or_error(response)
        items = payload.get("items") or []
        if not items:
            return {"channel_id": None, "channel_title": None}
        item = items[0]
        return {
            "channel_id": item.get("id"),
            "channel_title": (item.get("snippet") or {}).get("title"),
        }

    # -- publish contract --------------------------------------------------

    def upload_private(self, video_path, metadata: dict, target: PublishTarget,
                       progress_callback=None) -> PublishResult:
        """Resumable `videos.insert` with `privacyStatus=private`.

        Returns a `PublishResult`; a failure is reported as `status='failed'`
        with a sanitized error rather than raising, so the caller can persist a
        manifest that records exactly what happened.
        """
        video_path = Path(video_path)
        if not video_path.is_file():
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="yuklenecek dosya bulunamadi",
                error=f"dosya yok: {video_path.name}",
            )
        total_bytes = video_path.stat().st_size
        if total_bytes <= 0:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="yuklenecek dosya bos", error=f"bos dosya: {video_path.name}",
            )

        try:
            body = build_video_resource(metadata, target)
            session = self._session()
            init_response = session.request(
                "POST", UPLOAD_ENDPOINT,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Length": str(total_bytes),
                    "X-Upload-Content-Type": "video/*",
                },
                json=body, timeout=120,
            )
            if getattr(init_response, "status_code", 500) >= 400:
                payload = _safe_json(init_response)
                return PublishResult(
                    platform=self.platform, status="failed",
                    status_reason="resumable yukleme oturumu acilamadi",
                    error=sanitize_publish_error(_error_message(payload, init_response)),
                    quota_units_estimated=0,
                )
            upload_url = (getattr(init_response, "headers", {}) or {}).get("Location") \
                or (getattr(init_response, "headers", {}) or {}).get("location")
            if not upload_url:
                return PublishResult(
                    platform=self.platform, status="failed",
                    status_reason="resumable oturum URL'i (Location) alinamadi",
                    error="Location header yok",
                )

            payload = self._upload_chunks(
                session, upload_url, video_path, total_bytes, progress_callback,
            )
        except PublishAuthError as exc:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="yetkilendirme hatasi", error=sanitize_publish_error(exc),
            )
        except Exception as exc:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="yukleme sirasinda beklenmeyen hata",
                error=sanitize_publish_error(exc),
                quota_units_estimated=QUOTA_UNITS["videos.insert"],
            )

        video_id = payload.get("id")
        if not video_id:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="yukleme yanitinda video id yok",
                error=sanitize_publish_error(_error_message(payload, None)),
                quota_units_estimated=QUOTA_UNITS["videos.insert"],
            )
        privacy = ((payload.get("status") or {}).get("privacyStatus")) or INITIAL_PRIVACY
        return PublishResult(
            platform=self.platform,
            status="review_required",
            status_reason=(
                "video PRIVATE olarak yuklendi. Izleyiciye acilmasi icin ayri bir "
                "gorunurluk onayi gerekir."
            ),
            remote_id=video_id,
            remote_url=f"https://www.youtube.com/watch?v={video_id}",
            visibility=privacy,
            published_at=(payload.get("snippet") or {}).get("publishedAt"),
            quota_units_estimated=QUOTA_UNITS["videos.insert"],
            raw={
                "upload_status": (payload.get("status") or {}).get("uploadStatus"),
                "privacy_status": privacy,
            },
        )

    def _upload_chunks(self, session, upload_url: str, video_path: Path,
                       total_bytes: int, progress_callback=None) -> dict:
        """PUTs the file in aligned chunks, honoring 308 Resume Incomplete.

        A 308 response's `Range` header is authoritative about how much the
        server actually stored, so the next offset is taken from it rather than
        assumed -- otherwise a partially-accepted chunk corrupts the upload.
        """
        offset = 0
        with open(video_path, "rb") as handle:
            while offset < total_bytes:
                handle.seek(offset)
                chunk = handle.read(self._chunk_bytes)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                response = session.request(
                    "PUT", upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total_bytes}",
                    },
                    data=chunk, timeout=600,
                )
                code = getattr(response, "status_code", 500)
                if code in (200, 201):
                    if progress_callback:
                        progress_callback(total_bytes, total_bytes)
                    return _safe_json(response) or {}
                if code == 308:
                    headers = getattr(response, "headers", {}) or {}
                    range_header = headers.get("Range") or headers.get("range")
                    if range_header and "-" in range_header:
                        try:
                            offset = int(range_header.split("-")[-1]) + 1
                        except ValueError:
                            offset = end + 1
                    else:
                        offset = end + 1
                    if progress_callback:
                        progress_callback(min(offset, total_bytes), total_bytes)
                    continue
                payload = _safe_json(response)
                raise RuntimeError(
                    f"chunk yukleme basarisiz ({code}): {_error_message(payload, response)}"
                )
        raise RuntimeError("yukleme tamamlanmadi: sunucu son parcayi onaylamadi")

    def set_visibility(self, remote_id: str, visibility: str,
                       publish_at: str | None = None) -> PublishResult:
        """`videos.update` on the status part (50 quota units).

        Only reachable after `publish.visibility_change_allowed` returned True.
        """
        if visibility not in VISIBILITY_VALUES:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason=f"gecersiz visibility: {visibility!r}",
                error="invalid_visibility",
            )
        if not remote_id:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="remote_id yok", error="missing_remote_id",
            )

        status_block = {"privacyStatus": visibility}
        if publish_at:
            # YouTube requires privacyStatus=private for a scheduled release.
            status_block = {"privacyStatus": "private", "publishAt": publish_at}

        try:
            response = self._session().request(
                "PUT", VIDEOS_ENDPOINT,
                params={"part": "status"},
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={"id": remote_id, "status": status_block}, timeout=120,
            )
            payload = _json_or_error(response)
        except Exception as exc:
            return PublishResult(
                platform=self.platform, status="failed",
                status_reason="gorunurluk degistirilemedi",
                error=sanitize_publish_error(exc),
                quota_units_estimated=QUOTA_UNITS["videos.update"],
            )

        applied = (payload.get("status") or {}).get("privacyStatus") or status_block["privacyStatus"]
        return PublishResult(
            platform=self.platform,
            status="production_ready" if applied == "public" else "review_required",
            status_reason=(
                f"gorunurluk '{applied}' olarak ayarlandi"
                + (f", yayin zamani {publish_at}" if publish_at else "")
            ),
            remote_id=remote_id,
            remote_url=f"https://www.youtube.com/watch?v={remote_id}",
            visibility=applied,
            published_at=(payload.get("status") or {}).get("publishAt") or publish_at,
            quota_units_estimated=QUOTA_UNITS["videos.update"],
            raw={"privacy_status": applied},
        )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _safe_json(response) -> dict:
    if response is None:
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {"data": payload}


def _error_message(payload: dict, response) -> str:
    error = (payload or {}).get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or error)
    if error:
        return str(error)
    text = getattr(response, "text", "") if response is not None else ""
    return str(text)[:500] or "bilinmeyen hata"


def _json_or_error(response) -> dict:
    code = getattr(response, "status_code", 500)
    payload = _safe_json(response)
    if code >= 400:
        message = sanitize_publish_error(_error_message(payload, response))
        if code in (401, 403):
            raise PublishAuthError(f"YouTube yetki hatasi ({code}): {message}")
        raise RuntimeError(f"YouTube API hatasi ({code}): {message}")
    return payload
