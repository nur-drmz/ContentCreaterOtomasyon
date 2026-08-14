"""Shared fixtures and doubles for the Phase 1 (ingest + publish) test suite.

Every provider here is a DOUBLE. No test in this file or its siblings performs a
network call, reads a real credential, downloads third-party media, or uploads
anything -- the same policy the existing `verify_*.py` suites document.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# yt-dlp double
# ---------------------------------------------------------------------------

class FakeYoutubeDL:
    """Mimics the `yt_dlp.YoutubeDL` context-manager surface only."""

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        module = self._module
        module.calls.append({"url": url, "download": download, "options": self.options})
        if module.raise_exc is not None:
            raise module.raise_exc
        info = dict(module.info)
        if download and module.write_bytes is not None:
            outtmpl = self.options.get("outtmpl") or ""
            target = Path(str(outtmpl).replace("%(id)s", info.get("id", "vid"))
                          .replace("%(ext)s", "mp4"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(module.write_bytes)
            info["requested_downloads"] = [{"filepath": str(target)}]
        return info


class FakeYtDlpModule:
    """Injected in place of the real `yt_dlp` module."""

    def __init__(self, info=None, write_bytes=b"", raise_exc=None):
        self.info = info if info is not None else DEFAULT_PROBE_INFO
        self.write_bytes = write_bytes
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        bound = type("BoundYoutubeDL", (FakeYoutubeDL,), {"_module": self})
        self.YoutubeDL = bound


DEFAULT_PROBE_INFO = {
    "id": "abc123XYZ_9",
    "title": "Trend Video Basligi",
    "extractor_key": "Youtube",
    "uploader": "Ornek Kanal",
    "uploader_url": "https://www.youtube.com/@ornek",
    "duration": 620,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "is_live": False,
    "was_live": False,
    "age_limit": 0,
    "availability": "public",
    "license": None,
    "subtitles": {"tr": [{"ext": "vtt"}]},
    "automatic_captions": {},
    "filesize_approx": 42_000_000,
}


@pytest.fixture
def fake_ytdlp():
    return FakeYtDlpModule(write_bytes=b"\x00\x01fake-mp4-bytes" * 64)


# ---------------------------------------------------------------------------
# Artifact builders
# ---------------------------------------------------------------------------

@pytest.fixture
def rights_decision():
    from local_pipeline import trend_ingest

    return trend_ingest.build_rights_decision(
        "https://www.youtube.com/watch?v=abc123XYZ_9",
        trend_ingest.LICENSE_FAIR_USE_CLAIM,
        "nur",
        attribution="Ornek Kanal - https://www.youtube.com/@ornek",
        transformation_note="Sahne yorumu ve elestirel analiz eklenmis kisa derleme",
        risk_acknowledged=True,
    )


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "final_short.mp4"
    path.write_bytes(b"video-bytes" * 4096)
    return path


@pytest.fixture
def approved_render(video_file):
    """A render manifest that passes `approval.check_approval(..., 'render')`."""
    digest = hashlib.sha256(video_file.read_bytes()).hexdigest()
    return {
        "status": "approved",
        "config": {"pipeline_mode": "original_audio_subtitles"},
        "output": {
            "final_video_path": str(video_file),
            "validation": {"passed": True},
        },
        "approval": {
            "conditions": {
                "technical_validation_passed": True,
                "moment_selection_approved": True,
            },
            "failed_conditions": [],
        },
        "human_approval": {
            "approved": True,
            "approved_by": "nur",
            "approved_at": "2026-08-13T12:00:00+00:00",
            "approved_content_sha256": digest,
        },
    }


@pytest.fixture
def approved_metadata():
    from local_pipeline import metadata_studio

    artifact = metadata_studio.build_manual_metadata(
        title="Bu sahnede gozden kacan detay",
        description="Sahnenin neden onemli oldugunu adim adim inceliyoruz.",
        tags=["analiz", "sahne"],
    )
    artifact = metadata_studio.select_metadata(artifact, 0, category_id="22")
    return metadata_studio.approve_metadata(
        artifact, artifact["selected_sha256"], "nur",
    )


@pytest.fixture
def ingest_manifest(rights_decision):
    return {
        "artifact_kind": "ingest_manifest",
        "status": "review_required",
        "candidate_url": "https://www.youtube.com/watch?v=abc123XYZ_9",
        "uploader": "Ornek Kanal",
        "rights": {
            "license_type": rights_decision["decision"]["license_type"],
            "attribution": rights_decision["decision"]["attribution"],
            "transformation_note": rights_decision["decision"]["transformation_note"],
            "decided_by": rights_decision["decided_by"],
            "decided_at": rights_decision["decided_at"],
            "decision_sha256": rights_decision["decision_sha256"],
        },
    }


# ---------------------------------------------------------------------------
# HTTP doubles for the YouTube provider
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttp:
    """Scripted HTTP double. `handler(method, url, **kwargs) -> FakeResponse`."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, **kwargs)


class ScriptedResumableHttp(FakeHttp):
    """Resumable-upload double that deliberately accepts only PART of the first
    chunk, so the 308 `Range` resume path is genuinely exercised."""

    def __init__(self, accept_first_bytes: int):
        self.accept_first_bytes = accept_first_bytes
        self.offsets: list[int] = []
        self._accepted = 0
        super().__init__(self._handle)

    def _handle(self, method, url, **kwargs):
        if method == "POST":
            return FakeResponse(200, {}, headers={"Location": "https://upload.example/s"})
        content_range = (kwargs.get("headers") or {}).get("Content-Range", "")
        start = int(content_range.split(" ")[1].split("-")[0])
        total = int(content_range.split("/")[-1])
        self.offsets.append(start)
        chunk_len = len(kwargs.get("data") or b"")
        if self._accepted == 0:
            self._accepted = min(self.accept_first_bytes, chunk_len)
        else:
            self._accepted = start + chunk_len
        if self._accepted >= total:
            return FakeResponse(200, {
                "id": "resumedVideo",
                "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
            })
        return FakeResponse(308, None, headers={"Range": f"bytes=0-{self._accepted - 1}"})


@pytest.fixture
def scripted_resumable_http():
    return ScriptedResumableHttp


@pytest.fixture
def fake_http():
    def _make(handler):
        return FakeHttp(handler)

    return _make


@pytest.fixture
def fake_response():
    return FakeResponse


@pytest.fixture
def token_store(tmp_path):
    from local_pipeline.publish_youtube import OAuthTokenStore

    return OAuthTokenStore(path=tmp_path / ".secrets" / "youtube_oauth_token.json")


@pytest.fixture
def client_secrets():
    return {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret-value",
        "redirect_uris": ["http://localhost"],
    }


@pytest.fixture
def upload_http():
    """Successful resumable-upload script: session init -> single chunk -> 200."""

    def handler(method, url, **kwargs):
        if method == "POST" and "upload/youtube/v3/videos" in url:
            return FakeResponse(
                200, {}, headers={"Location": "https://upload.example/session/1"}
            )
        if method == "PUT" and url.startswith("https://upload.example/session/"):
            return FakeResponse(200, {
                "id": "newVideoId123",
                "snippet": {"publishedAt": "2026-08-13T12:00:00Z"},
                "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
            })
        if method == "PUT" and "youtube/v3/videos" in url:
            body = kwargs.get("json") or {}
            return FakeResponse(200, {"id": body.get("id"), "status": body.get("status")})
        if method == "GET" and "youtube/v3/channels" in url:
            return FakeResponse(200, {
                "items": [{"id": "UC_test_channel", "snippet": {"title": "Test Kanal"}}]
            })
        raise AssertionError(f"beklenmeyen istek: {method} {url}")

    return FakeHttp(handler)


@pytest.fixture
def youtube_provider(client_secrets, token_store, upload_http):
    from local_pipeline.publish_youtube import YouTubeProvider

    token_store.save("fake-refresh-token", channel_id="UC_test_channel",
                     channel_title="Test Kanal")
    return YouTubeProvider(
        client_secrets=client_secrets,
        token_store=token_store,
        http=upload_http,
        http_post=lambda url, data: {"access_token": "fake-access", "expires_in": 3600},
    )


@pytest.fixture
def read_json():
    def _read(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    return _read
