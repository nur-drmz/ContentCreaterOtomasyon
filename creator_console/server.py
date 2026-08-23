"""ThreadingHTTPServer adapter around `ConsoleApi`.

Binds to 127.0.0.1 by default and refuses to bind to a public interface without
an explicit override, because pre-alpha has NO authentication: exposing it to a
network would be exposing an unauthenticated console.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__
from .api import ConsoleApi
from .redaction import scrub_text
from .storage import atomic_write_json

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY_BYTES = 4 * 1024 * 1024 * 1024
MEDIA_CHUNK_BYTES = 1024 * 1024


def _parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range, rejecting ambiguous/malformed input."""
    if not value:
        return None
    text = str(value).strip()
    if not text.startswith("bytes=") or "," in text or size <= 0:
        raise ValueError("unsupported byte range")
    bounds = text[6:].split("-", 1)
    if len(bounds) != 2:
        raise ValueError("malformed byte range")
    first, last = (part.strip() for part in bounds)
    if not first:
        if not last:
            raise ValueError("empty byte range")
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    if start < 0 or start >= size:
        raise ValueError("range start outside file")
    end = size - 1 if not last else min(int(last), size - 1)
    if end < start:
        raise ValueError("range end before start")
    return start, end


class _Handler(BaseHTTPRequestHandler):
    server_version = f"CreatorConsole/{__version__}"
    api: ConsoleApi = None  # injected per-server

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):
        # `_dispatch` emits correlated start/end records after the body has been
        # streamed. Suppress BaseHTTPRequestHandler's header-time access line.
        return

    def _read_body(self) -> bytes:
        length = self._content_length()
        if length <= 0 or length > MAX_BODY_BYTES:
            return b""
        return self.rfile.read(length)

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0

    def _dispatch(self, method: str):
        safe_path = self.path.split("?", 1)[0]
        supplied_id = str(self.headers.get("X-Request-ID") or "")
        request_id = (supplied_id if supplied_id.replace("-", "").replace("_", "").isalnum()
                      and len(supplied_id) <= 80 else f"req_{secrets.token_hex(6)}")
        started = time.monotonic()
        status = 500
        sent = 0
        range_header = self.headers.get("Range")
        self.server.request_event(
            "http.request.started", request_id=request_id, method=method,
            path=safe_path, byte_range=range_header)
        try:
            path_parts = [part for part in safe_path.split("/") if part]
            source_upload = (
                method == "POST" and len(path_parts) == 4
                and path_parts[:2] == ["api", "projects"] and path_parts[3] == "source"
            )
            if source_upload:
                length = self._content_length()
                status, payload = self.api.handle(
                    method, self.path, headers=self.headers,
                    body_stream=self.rfile, content_length=length)
                if length > self.api.app.config.max_upload_bytes:
                    # The request body was deliberately not consumed. Do not reuse
                    # this HTTP connection with unread video bytes still pending.
                    self.close_connection = True
            else:
                body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
                status, payload = self.api.handle(method, self.path, body=body, headers=self.headers)
            if isinstance(payload, dict) and "__file__" in payload:
                status, sent = self._send_file(status, payload)
                return
            if isinstance(payload, dict) and "__raw__" in payload:
                raw = payload["__raw__"]
                self.send_response(status)
                self.send_header("Content-Type", payload.get("__content_type__", "application/octet-stream"))
                self.send_header("Content-Length", str(len(raw)))
                self._security_headers()
                self.end_headers()
                try:
                    self.wfile.write(raw)
                    sent = len(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass  # Browser navigated away or closed; not a server failure.
                return
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._security_headers()
            self.end_headers()
            try:
                self.wfile.write(encoded)
                sent = len(encoded)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass  # Browser navigated away or closed; not a server failure.
        finally:
            self.server.request_event(
                "http.request.finished", request_id=request_id, method=method,
                path=safe_path, status=status, duration_ms=round(
                    (time.monotonic() - started) * 1000, 2), bytes_sent=sent,
                byte_range=range_header)

    def _send_file(self, status: int, payload: dict) -> tuple[int, int]:
        """Stream a validated API file descriptor, honoring one byte range."""
        target = Path(payload["__file__"])
        size = target.stat().st_size
        try:
            selected = _parse_byte_range(self.headers.get("Range"), size)
        except (TypeError, ValueError):
            status = 416
            self.send_response(status)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self._security_headers()
            self.end_headers()
            return status, 0

        start, end = selected if selected is not None else (0, size - 1)
        if selected is not None:
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", payload.get("__content_type__", "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if selected is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._security_headers()
        self.end_headers()

        sent = 0
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(MEDIA_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # The browser sought elsewhere or left the screen.
        return status, sent

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, *, host="127.0.0.1", port=8765, quiet=False):
        if host not in LOOPBACK_HOSTS and not app.config.extra.get("allow_public_bind"):
            raise RuntimeError(
                f"'{host}' loopback disi bir adres. Creator Console pre-alpha KIMLIK "
                "DOGRULAMASIZ calisir; agda yayinlanmamalidir. Bilerek yapiyorsan "
                "config.extra['allow_public_bind']=True ver."
            )
        self.app = app
        self.quiet = quiet
        handler = type("_BoundHandler", (_Handler,), {"api": ConsoleApi(app)})
        super().__init__((host, port), handler)

    def console_log(self, message: str) -> None:
        if not self.quiet:
            print(f"[console] {message}")

    def request_event(self, event: str, **detail) -> None:
        """Persist a scrubbed request event and mirror a concise terminal line."""
        self.app.log_runtime(event, **detail)
        if self.quiet:
            return
        phase = "START" if event.endswith("started") else "END"
        suffix = ""
        if phase == "END":
            suffix = (f" status={detail.get('status')} duration_ms={detail.get('duration_ms')}"
                      f" bytes={detail.get('bytes_sent')}")
        if detail.get("byte_range"):
            suffix += f" range={detail['byte_range']}"
        line = (f"{detail.get('request_id')} {phase} "
                f"{detail.get('method')} {detail.get('path')}{suffix}")
        print(f"[console] {scrub_text(line, max_len=500)}")

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="console-http", daemon=True)
        thread.start()
        return thread


def run(app, *, host="127.0.0.1", port=8765, quiet=False):
    server = ConsoleServer(app, host=host, port=port, quiet=quiet)
    bound_host, bound_port = server.server_address[:2]
    control_path = app.storage.root / ".creator_console_control.json"
    stop_path = app.storage.root / ".creator_console_stop"
    shutdown_token = secrets.token_urlsafe(32)
    atomic_write_json(control_path, {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": bound_host,
        "port": bound_port,
        "python_executable": sys.executable,
        "shutdown_token": shutdown_token,
    })
    stop_monitor = threading.Event()

    def monitor_stop_request():
        """Graceful, local-file shutdown channel used by the Windows stopper.

        The random token prevents a stale/unrelated file from stopping the
        process. Nothing is exposed over HTTP and no other Python process is
        searched for or terminated.
        """
        while not stop_monitor.wait(0.2):
            try:
                if not stop_path.is_file():
                    continue
                requested_token = stop_path.read_text(encoding="utf-8").strip()
                try:
                    stop_path.unlink()
                except OSError:
                    pass
                if secrets.compare_digest(requested_token, shutdown_token):
                    server.shutdown()
                    return
            except OSError:
                time.sleep(0.2)

    monitor_thread = threading.Thread(
        target=monitor_stop_request,
        name="console-stop-monitor",
        daemon=True,
    )
    monitor_thread.start()
    print("=" * 72)
    print(f"{APP_NAME} {__version__} (pre-alpha, tek kullanici, kimlik dogrulamasiz)")
    print("=" * 72)
    print(f"  Adres        : http://{bound_host}:{bound_port}/")
    print(f"  Depolama     : {app.storage.root}")
    print(f"  Is calistirma: {'inline (senkron)' if app.config.inline_jobs else 'thread worker'}")
    print("  Politika     : hicbir sey yuklenmez/yayinlanmaz; API anahtarlari "
          "tarayiciya GONDERILMEZ.")
    for reference in app.secrets.public_state():
        print(f"    - {reference['kind']:22s} {reference['state']}")
    print("  Durdurmak icin Ctrl+C")
    print("=" * 72)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] kapatiliyor...")
    finally:
        stop_monitor.set()
        server.shutdown()
        server.server_close()
        app.shutdown()
        try:
            current = json.loads(control_path.read_text(encoding="utf-8"))
            if current.get("shutdown_token") == shutdown_token:
                control_path.unlink()
        except (OSError, ValueError, TypeError):
            pass
        try:
            stop_path.unlink()
        except OSError:
            pass
    return 0
