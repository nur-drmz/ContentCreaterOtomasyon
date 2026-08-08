"""HTTP surface. Transport only -- no pipeline logic, no secret values.

WHY THIS SHAPE
--------------
Routes are plain functions returning `(status, payload)`; the server adapter
turns them into HTTP. That keeps every endpoint callable directly from the test
suite without binding a socket, while the real server exercises the same code.

Three invariants this layer enforces:
  1. Every project id from the wire goes through `ConsoleApp.assert_owned`
     before it can reach storage.
  2. Every error body comes from the CLOSED catalog in `errors.py`. Raw exception
     text never leaves the process -- it goes to a scrubbed server-side log line
     correlated by `debug_id`. That is what stops a secret which never went
     through `SecretResolver` from riding an exception into a response.
  3. Nothing here reads a secret value; it only ever sees `configured`/`missing`.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .errors import ApiError
from .progress import load_progress
from .ids import InvalidIdentifier
from .jobs import JobConflict, JobError, JobNotFound
from .pipeline_service import (
    ArtifactProblem,
    PipelineServiceError,
    is_supported_source_video_filename,
)
from .storage import StorageViolation

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Only these media types are ever served from a project namespace.
SERVABLE_MEDIA_SUFFIXES = {".mp4", ".wav", ".mp3", ".m4a", ".srt", ".ass", ".json", ".png", ".jpg"}


class ConsoleApi:
    """Routing table over a `ConsoleApp`."""

    def __init__(self, app):
        self.app = app

    # ------------------------------------------------------------- dispatch
    def handle(self, method: str, raw_path: str, *, body: bytes = b"",
               headers=None, body_stream=None, content_length=None) -> tuple:
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            return self._route(method.upper(), path, body, query, headers or {},
                               body_stream=body_stream, content_length=content_length)
        except ApiError as exc:
            return self._fail(exc)
        except InvalidIdentifier as exc:
            return self._fail(ApiError("invalid_identifier", detail=str(exc)))
        except StorageViolation as exc:
            return self._fail(ApiError("storage_violation", detail=str(exc)))
        except JobNotFound as exc:
            return self._fail(ApiError("job_not_found", detail=str(exc)))
        except JobConflict as exc:
            return self._fail(ApiError("job_conflict", detail=str(exc)))
        except LookupError as exc:
            return self._fail(ApiError("not_found", detail=str(exc)))
        except ArtifactProblem as exc:
            return self._fail(ApiError("artifact_problem", detail=str(exc)))
        except (PipelineServiceError, JobError, ValueError) as exc:
            return self._fail(ApiError(_classify(str(exc)), detail=str(exc)))
        except OSError as exc:
            # Disk full / permission denied / device error: actionable, and the
            # request can be retried once the operator fixes storage.
            return self._fail(ApiError("storage_unavailable",
                                        detail=f"{type(exc).__name__}: {exc}"))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            return self._fail(ApiError("internal_error",
                                        detail=f"{type(exc).__name__}: {exc}"))

    def _fail(self, error: ApiError) -> tuple:
        """Log the scrubbed detail server-side; return ONLY the catalog body."""
        self.app.log_error(error)
        return error.status, error.to_payload()

    def _route(self, method, path, body, query, headers, *, body_stream=None,
               content_length=None):
        if path == "/" and method == "GET":
            return self._static("index.html")
        if path.startswith("/static/") and method == "GET":
            return self._static(path[len("/static/"):])
        if path == "/api/health" and method == "GET":
            return 200, {"ok": True, "single_user": True, "not_uploaded": True}
        if path == "/api/preflight" and method == "GET":
            return 200, {"preflight": self.app.preflight()}
        if path == "/api/diagnostics" and method == "GET":
            # Default is the CACHED report, because a full run exercises real
            # media and must never be triggered by merely opening a page.
            if _truthy(_first(query.get("run"))):
                return 200, {"diagnostics": self.app.run_diagnostics(
                    only=_csv(_first(query.get("only"))))}
            return 200, {"diagnostics": self.app.last_diagnostics()}
        if path == "/api/diagnostics" and method == "POST":
            payload = _json_body(body)
            return 200, {"diagnostics": self.app.run_diagnostics(
                only=_csv(payload.get("only")))}
        if path == "/api/workspace" and method == "GET":
            return 200, self.app.workspace_view()
        if path == "/api/settings/secrets" and method == "POST":
            payload = _json_body(body)
            return 200, self.app.save_local_secret(
                kind=_required(payload, "kind"),
                value=_required(payload, "secret_value", minimum=20),
            )
        if path == "/api/projects" and method == "POST":
            payload = _json_body(body)
            title = _required(payload, "title")
            operator = _required(payload, "operator_name")
            record = self.app.create_project(
                title=title,
                created_by=operator, operator_name=operator)
            return 201, {"project": record}

        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
            return self._project_routes(
                method, parts, body, query, headers,
                body_stream=body_stream, content_length=content_length)
        raise ApiError("unknown_route", detail=f"{method} {path}")

    # ------------------------------------------------------- project routes
    def _project_routes(self, method, parts, body, query, headers, *,
                        body_stream=None, content_length=None):
        ownership = self.app.assert_owned(parts[2])
        service = self.app.service(ownership)
        rest = parts[3:]

        if not rest and method == "GET":
            return 200, self.app.project_view(ownership)

        if not rest and method == "PATCH":
            payload = _json_body(body)
            record = service.update_project(
                title=payload.get("title", ""),
                notes=payload.get("notes", ""),
                updated_by=_header(headers, "x-actor") or service.project_actor(),
                operator_name=(payload.get("operator_name")
                               if "operator_name" in payload else None),
            )
            return 200, {"project": record}

        if not rest and method == "DELETE":
            payload = _json_body(body)
            return 200, self.app.delete_project(
                ownership,
                confirmation=_required(payload, "confirmation"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )

        if rest == ["lifecycle"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action")
            actor = (_header(headers, "x-actor") or payload.get("actor")
                     or service.project_actor())
            if action == "rewind":
                return 200, self.app.rewind_project(
                    ownership, target=payload.get("target") or "", actor=actor)
            if action == "reset":
                return 200, self.app.reset_project(
                    ownership,
                    keep_source=_truthy(payload.get("keep_source", True)),
                    confirmation=_required(payload, "confirmation"),
                    actor=actor)
            raise ApiError("validation_failed", detail=f"yasam dongusu eylemi: {action!r}")

        if rest == ["script"] and method == "PATCH":
            payload = _json_body(body)
            artifact = self.app.update_script_text(
                ownership,
                script_text=_required(payload, "script_text"),
                updated_by=payload.get("updated_by") or service.project_actor(),
                notes=payload.get("notes", ""),
            )
            return 200, {"script": artifact,
                         "approval": service.approval_state("script")}

        if rest == ["source"] and method == "POST":
            filename = _header(headers, "x-filename") or _first(query.get("filename")) or "source.mp4"
            filename = unquote(filename)
            if not is_supported_source_video_filename(filename):
                raise ApiError("unsupported_video_type")
            if body_stream is not None:
                try:
                    length = int(content_length)
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    raise ApiError("empty_upload")
                if length > self.app.config.max_upload_bytes:
                    raise ApiError("upload_too_large")
                record = service.register_source_video_stream(
                    filename=filename, stream=body_stream,
                    content_length=length,
                    registered_by=_header(headers, "x-actor") or service.project_actor(),
                    track=True)
                return 201, {"project": record}
            if not body:
                raise ApiError("empty_upload")
            if len(body) > self.app.config.max_upload_bytes:
                raise ApiError("upload_too_large")
            record = service.register_source_video(
                filename=filename, payload=body,
                registered_by=_header(headers, "x-actor") or service.project_actor(),
                track=True)
            return 201, {"project": record}

        if rest == ["plan"] and method == "POST":
            payload = _json_body(body)
            mode = payload.get("mode")
            if not mode:
                raise ApiError("mode_required")
            # New projects require one named operator. Reuse that persisted
            # identity here instead of asking for the same name at every gate.
            plan = service.create_processing_plan(
                mode=mode, selected_by=payload.get("selected_by") or service.project_actor(),
                output_count=payload.get("output_count"),
                target_duration_sec=payload.get("target_duration_sec"),
                moment_ranks=payload.get("moment_ranks"),
                theory_angle=payload.get("theory_angle", ""),
                content_language=payload.get("content_language"),
                narration_preset=payload.get("narration_preset"),
                render_config=payload.get("render_config"),
                notes=payload.get("notes", ""))
            return 201, {"plan": plan, "review": service.describe_plan_review()}

        if rest == ["plan"] and method == "PATCH":
            payload = _json_body(body)
            plan = service.update_processing_plan(
                updated_by=_header(headers, "x-actor")
                           or payload.get("updated_by") or service.project_actor(),
                mode=payload.get("mode"),
                selected_by=payload.get("selected_by"),
                output_count=payload.get("output_count"),
                target_duration_sec=payload.get("target_duration_sec"),
                moment_ranks=(payload["moment_ranks"]
                              if "moment_ranks" in payload else None),
                theory_angle=(payload["theory_angle"]
                              if "theory_angle" in payload else None),
                content_language=(payload["content_language"]
                                  if "content_language" in payload else None),
                narration_preset=(payload["narration_preset"]
                                  if "narration_preset" in payload else None),
                render_config=(payload["render_config"]
                               if "render_config" in payload else None),
                notes=(payload["notes"] if "notes" in payload else None),
            )
            return 200, {"plan": plan, "review": service.describe_plan_review()}

        if rest == ["plan", "review"] and method == "GET":
            return 200, service.describe_plan_review()

        if rest == ["moment-selection"] and method == "POST":
            payload = _json_body(body)
            slot = _slot(payload)
            selection = service.record_moment_selection(
                selected_rank=payload.get("selected_rank"),
                reviewed_by=payload.get("reviewed_by") or service.project_actor(),
                rejected_ranks=payload.get("rejected_ranks"),
                notes=payload.get("notes", ""),
                require_preview=payload.get("require_preview", True),
                slot=slot)
            return 201, {"moment_selection": selection, "slot": slot}

        if rest == ["transcript"] and method == "GET":
            return 200, {"transcript": service.transcript_view(_slot(query))}

        if rest == ["transcript", "corrections"] and method == "POST":
            payload = _json_body(body)
            slot = _slot(payload)
            artifact = service.set_transcript_corrections(
                corrections=payload.get("corrections") or [],
                edited_by=payload.get("edited_by") or service.project_actor(),
                slot=slot)
            return 200, {"transcript": service.transcript_view(slot),
                         "corrections": artifact.get("corrections") or []}

        if rest == ["outputs"] and method == "GET":
            return 200, {"outputs": service.outputs(), "batch": service.batch_state()}

        if rest == ["batch"] and method == "GET":
            return 200, {"batch": service.batch_state()}

        if rest == ["batch"] and method == "POST":
            payload = _json_body(body)
            result = self.app.submit_batch_render(
                ownership,
                submitted_by=payload.get("submitted_by") or service.project_actor(),
                run_request_id=payload.get("run_request_id"),
                slots=payload.get("slots"))
            return (202 if result["submitted_count"] else 200), result

        if len(rest) == 2 and rest[0] == "approvals" and method == "POST":
            payload = _json_body(body)
            slot = _slot(payload)
            approved = service.approve(
                rest[1], reviewed_sha256=payload.get("reviewed_sha256", ""),
                approved_by=payload.get("approved_by") or service.project_actor(),
                notes=payload.get("notes", ""), slot=slot)
            return 200, {"artifact": approved,
                         "approval": service.approval_state(rest[1], slot)}

        if rest == ["jobs"] and method == "GET":
            store = self.app.job_store(ownership)
            store.recover_abandoned()
            return 200, {"jobs": store.list(limit=100)}

        if rest == ["jobs"] and method == "POST":
            payload = _json_body(body)
            record, created = self.app.submit_job(
                ownership, payload.get("job_type"), params=payload.get("params") or {},
                submitted_by=payload.get("submitted_by", "console"),
                run_request_id=payload.get("run_request_id"))
            # 200 (not 201) on a deduplicated submit: nothing new was created, so
            # a double-clicked button is visibly a no-op rather than a second run.
            return (201 if created else 200), {"job": record, "created": created,
                                                "deduplicated": not created}

        if rest == ["render-review"] and method == "GET":
            return 200, {"render_review": service.render_review_state(_slot(query))}

        if rest == ["render-review"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "set_item"
            slot = _slot(payload)
            if action == "set_item":
                return 200, {"render_review": service.set_render_review_item(
                    payload.get("key", ""), checked=bool(payload.get("checked")),
                    reviewed_by=payload.get("reviewed_by") or service.project_actor(),
                    note=payload.get("note", ""), slot=slot)}
            if action == "request_revision":
                return 200, {"render_review": service.request_render_revision(
                    requested_by=payload.get("requested_by") or service.project_actor(),
                    notes=payload.get("notes", ""),
                    target_tab=payload.get("target_tab", "moments"), slot=slot)}
            if action == "clear_revision":
                return 200, {"render_review": service.clear_render_revision(
                    resolved_by=payload.get("resolved_by") or service.project_actor(),
                    slot=slot)}
            raise ApiError("validation_failed", detail=f"eylem: {action!r}")

        if rest == ["voice-review"] and method == "GET":
            return 200, {"voice_review": service.voice_review_state()}

        if rest == ["voice-review"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "set_item"
            if action == "set_item":
                return 200, {"voice_review": service.set_voice_review_item(
                    payload.get("key", ""), checked=bool(payload.get("checked")),
                    reviewed_by=payload.get("reviewed_by") or service.project_actor(),
                    note=payload.get("note", ""))}
            if action == "request_revision":
                return 200, {"voice_review": service.request_voice_revision(
                    requested_by=payload.get("requested_by") or service.project_actor(),
                    notes=_required({"note": payload.get("notes")}, "note"),
                    target_tab=payload.get("target_tab", "voice"))}
            if action == "clear_revision":
                return 200, {"voice_review": service.clear_voice_revision(
                    resolved_by=payload.get("resolved_by") or service.project_actor())}
            raise ApiError("validation_failed", detail=f"eylem: {action!r}")

        if rest == ["progress"] and method == "GET":
            return 200, {"progress": load_progress(self.app.storage, ownership)}

        if rest == ["preflight"] and method == "GET":
            return 200, {"preflight": self.app.preflight(ownership)}

        if rest == ["automation"] and method == "GET":
            return 200, {"automation": self.app.automation(ownership).view()}

        if rest == ["automation"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "tick"
            if action not in ("start", "pause", "resume", "cancel", "tick"):
                raise ApiError("validation_failed", detail=f"eylem: {action!r}")
            return 202, {"automation": self.app.automation_action(
                ownership, action, actor=payload.get("actor") or service.project_actor())}

        if rest == ["auto-run"] and method == "POST":
            # Backwards-compatible alias for the persisted engine's tick.
            return 202, {"automation": self.app.automation_action(
                ownership, "tick", actor=service.project_actor())}

        if len(rest) == 3 and rest[0] == "jobs" and rest[2] == "retry" and method == "POST":
            record = self.app.retry_job(ownership, rest[1],
                                        requested_by=_header(headers, "x-actor")
                                                     or service.project_actor())
            return 202, {"job": record}

        if len(rest) == 3 and rest[0] == "jobs" and rest[2] == "cancel" and method == "POST":
            record = self.app.cancel_job(
                ownership, rest[1],
                requested_by=_header(headers, "x-actor") or service.project_actor())
            return 202, {"job": record}

        if len(rest) == 2 and rest[0] == "jobs" and method == "GET":
            return 200, {"job": self.app.job_store(ownership).get(rest[1])}

        if rest and rest[0] == "media" and method == "GET":
            return self._media(service, ownership, rest[1:])

        if rest == ["audit"] and method == "GET":
            from .audit import AuditLog
            return 200, {"events": AuditLog(self.app.storage, ownership).events(limit=200)}

        raise ApiError("unknown_route", detail=f"{method} /{'/'.join(parts)}")

    # -------------------------------------------------------------- helpers
    def _media(self, service, ownership, segments):
        if not segments:
            raise ApiError("validation_failed", detail="medya yolu verilmedi")
        target = self.app.storage.resolve(ownership, *segments)
        if target.suffix.lower() not in SERVABLE_MEDIA_SUFFIXES:
            raise ApiError("media_type_blocked", detail=str(target.suffix))
        if not target.is_file():
            raise ApiError("media_not_found")
        if target.stat().st_size <= 0:
            raise ApiError("media_empty")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return 200, {"__raw__": target.read_bytes(), "__content_type__": content_type}

    def _static(self, relative):
        safe = Path(relative.replace("\\", "/"))
        if safe.is_absolute() or ".." in safe.parts:
            raise ApiError("static_path_blocked")
        target = (STATIC_DIR / safe).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            raise ApiError("static_not_found", detail=str(relative))
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        return 200, {"__raw__": target.read_bytes(), "__content_type__": content_type}



def _required(payload, name: str, *, minimum: int = 1) -> str:
    """A required string field, or a field-named 400.

    Central so no route can accidentally invent its own default: the whole point
    of marking a field required in the UI is that the server refuses without it.
    """
    value = str(payload.get(name) or "").strip()
    if len(value) < minimum:
        raise ApiError("missing_required_field", fields=[name],
                       detail=f"{name} bos veya cok kisa")
    return value


def _slot(source) -> int:
    """Output slot from a JSON payload or a query string, defaulting to 1.

    Accepts both shapes so one helper serves POST bodies and GET query params.
    Whether the slot actually exists is decided by `PipelineService.require_slot`
    against the approved plan -- not here, where the plan is not in scope.
    """
    if not source:
        return 1
    raw = source.get("slot")
    if isinstance(raw, list):  # parse_qs gives lists
        raw = _first(raw)
    if raw in (None, ""):
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError("validation_failed", fields=["slot"],
                       detail="slot bir tam sayi olmali") from None
    if value < 1:
        raise ApiError("validation_failed", fields=["slot"],
                       detail="slot 1'den kucuk olamaz")
    return value


def _json_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("invalid_json", detail=str(exc)) from None
    if not isinstance(parsed, dict):
        raise ApiError("invalid_json", detail="govde JSON nesnesi degil")
    return parsed


def _header(headers, name: str):
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    return lowered.get(name.lower())


def _first(values):
    return values[0] if values else None


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _csv(value):
    """`?only=render,review` -> ('render', 'review'); anything empty -> None."""
    if isinstance(value, (list, tuple)):
        keys = [str(item).strip() for item in value]
    else:
        keys = [part.strip() for part in str(value or "").split(",")]
    keys = [key for key in keys if key]
    return tuple(keys) or None


def _classify(message: str) -> str:
    """Map a pipeline refusal to a catalog code.

    Only the CODE is derived from the message; the message itself is never
    returned. Approval refusals get their own code because the user genuinely
    needs to know the hash did not match -- the catalog says that statically.
    """
    lowered = message.lower()
    if "onay reddedildi" in lowered:
        return "approval_rejected"
    if "faz 0" in lowered or "isleme plani" in lowered or "plan" in lowered:
        return "phase_blocked"
    return "validation_failed"
