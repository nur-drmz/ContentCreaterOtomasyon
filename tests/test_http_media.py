"""HTTP media streaming and persistent runtime diagnostics regressions."""
from __future__ import annotations

import http.client
import json

from creator_console.api import ConsoleApi
from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.server import ConsoleServer


def _request(host, port, path, *, headers=None):
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_media_route_streams_ranges_and_persists_correlated_logs(tmp_path):
    app = ConsoleApp(ConsoleConfig(
        storage_root=tmp_path,
        inline_jobs=True,
        extra={"skip_startup_recovery": True},
    ))
    project = app.create_project(
        title="Range fixture", created_by="tester", operator_name="tester")
    ownership = app.ownership(project["project_id"])
    media = app.storage.resolve(
        ownership, "results", "moment_previews", "candidate.mp4",
        create_parent=True)
    payload = bytes(range(256)) * 64
    media.write_bytes(payload)
    path = f"/api/projects/{project['project_id']}/media/results/moment_previews/candidate.mp4"

    # The transport-neutral API returns a validated file descriptor, not a
    # potentially huge in-memory bytes object.
    status, descriptor = ConsoleApi(app).handle("GET", path)
    assert status == 200
    assert descriptor["__file__"] == media
    assert "__raw__" not in descriptor

    server = ConsoleServer(app, host="127.0.0.1", port=0, quiet=True)
    server.serve_in_thread()
    host, port = server.server_address[:2]
    try:
        status, headers, body = _request(
            host, port, path,
            headers={"Range": "bytes=100-199", "X-Request-ID": "web-test-123"},
        )
        assert status == 206
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Range"] == f"bytes 100-199/{len(payload)}"
        assert int(headers["Content-Length"]) == 100
        assert body == payload[100:200]

        status, headers, body = _request(
            host, port, path, headers={"Range": f"bytes={len(payload)}-"})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{len(payload)}"
        assert body == b""
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "runtime_events.jsonl").read_text(
            encoding="utf-8").splitlines()
    ]
    finished = [record for record in records
                if record["event"] == "http.request.finished"]
    assert any(record["detail"].get("request_id") == "web-test-123"
               and record["detail"].get("status") == 206
               and record["detail"].get("bytes_sent") == 100
               for record in finished)


def test_api_errors_are_persisted_with_debug_id(tmp_path):
    app = ConsoleApp(ConsoleConfig(
        storage_root=tmp_path,
        inline_jobs=True,
        extra={"skip_startup_recovery": True},
    ))
    try:
        status, payload = ConsoleApi(app).handle("GET", "/api/not-a-route")
        assert status == 404
        debug_id = payload["error"]["debug_id"]
        records = [
            json.loads(line)
            for line in (tmp_path / "logs" / "runtime_events.jsonl").read_text(
                encoding="utf-8").splitlines()
        ]
        errors = [record for record in records if record["event"] == "api.error"]
        assert errors[-1]["detail"]["debug_id"] == debug_id
        assert errors[-1]["detail"]["code"] == "unknown_route"
    finally:
        app.shutdown()
