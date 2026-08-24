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
from . import render_profile
from .pipeline_service import (
    REWIND_LABELS,
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

        if rest == ["archive"] and method == "POST":
            payload = _json_body(body)
            return 200, self.app.archive_project(
                ownership,
                confirmation=_required(payload, "confirmation"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )

        if rest == ["long-form-plan"] and method == "PATCH":
            payload = _json_body(body)
            return 200, {"long_form": service.save_long_form_plan(
                topic=_required(payload, "topic"),
                content_type=_required(payload, "content_type"),
                target_duration_min=_required(payload, "target_duration_min"),
                chapter_count=_required(payload, "chapter_count"),
                language=_required(payload, "language"),
                tone=_required(payload, "tone"),
                audience=payload.get("audience") or "Genel izleyici",
                angle=payload.get("angle") or "",
                narration_mode=payload.get("narration_mode") or "undecided",
                include_subtitles=_truthy(payload.get("include_subtitles", True)),
                notes=payload.get("notes") or "",
                updated_by=_header(headers, "x-actor") or payload.get("updated_by")
                or service.project_actor(),
            )}

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

        # Guided revision. GET only DESCRIBES what a revision would archive, so
        # the confirmation can name the files instead of asking a blind
        # "are you sure".
        if rest == ["revision"] and method == "GET":
            target = _first(query.get("target"))
            suggestion = service.suggested_revision_stage(_slot(query))
            return 200, {"revision": {
                "suggested": suggestion,
                "plan": service.revision_plan(
                    target=target or suggestion["stage"], slot=_slot(query)),
                "stages": [{"key": key, "label": label}
                           for key, label in REWIND_LABELS.items()],
                "archive": service.archive_view(),
            }}

        if rest == ["revision"] and method == "POST":
            payload = _json_body(body)
            actor = (_header(headers, "x-actor") or payload.get("actor")
                     or service.project_actor())
            return 200, {"revision": service.start_revision(
                target=_required(payload, "target"),
                notes=payload.get("notes") or "",
                actor=actor, slot=_slot(payload)),
                "archive": service.archive_view()}

        if rest == ["revision", "purge"] and method == "POST":
            payload = _json_body(body)
            actor = (_header(headers, "x-actor") or payload.get("actor")
                     or service.project_actor())
            return 200, {"archive": service.purge_revision(
                revision_id=_required(payload, "revision_id"),
                confirmation=_required(payload, "confirmation"), actor=actor)}

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

        if rest == ["subtitles"] and method == "PATCH":
            payload = _json_body(body)
            cues = payload.get("cues")
            if not isinstance(cues, list) or not cues:
                raise ApiError("missing_required_field", fields=["cues"],
                               detail="cues bos veya liste degil")
            view = self.app.update_subtitle_texts(
                ownership,
                cues=cues,
                updated_by=payload.get("updated_by") or service.project_actor(),
                notes=payload.get("notes", ""),
                slot=_slot(payload),
            )
            return 200, {"subtitles": view}

        if rest == ["source-library"] and method == "POST":
            payload = _json_body(body)
            record = self.app.import_source_from_library(
                ownership, filename=_required(payload, "filename"),
                actor=_header(headers, "x-actor") or payload.get("actor")
                or service.project_actor())
            return 201, {"project": record}

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
                theory_angle_auto=bool(payload.get("theory_angle_auto")),
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
                theory_angle_auto=(bool(payload["theory_angle_auto"])
                                   if "theory_angle_auto" in payload else None),
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

        # Segment-wise and whole-transcript edits are one server operation with
        # two entry shapes, so the textarea cannot bypass a validation the row
        # editor enforces.
        if rest == ["transcript", "segments"] and method == "POST":
            payload = _json_body(body)
            return 200, {"transcript": service.save_transcript_edits(
                segments=payload.get("segments") or [],
                edited_by=payload.get("edited_by") or service.project_actor(),
                slot=_slot(payload))}

        if rest == ["transcript", "full-text"] and method == "POST":
            payload = _json_body(body)
            return 200, {"transcript": service.save_transcript_edits(
                full_text=_required(payload, "full_text"),
                edited_by=payload.get("edited_by") or service.project_actor(),
                slot=_slot(payload))}

        if rest == ["transcript", "exclusions"] and method == "POST":
            payload = _json_body(body)
            return 200, {"transcript": service.set_transcript_exclusions(
                segment_ids=payload.get("segment_ids") or [],
                excluded=_truthy(payload.get("excluded", True)),
                actor=payload.get("actor") or service.project_actor(),
                slot=_slot(payload))}

        if rest == ["transcript", "restore-asr"] and method == "POST":
            payload = _json_body(body)
            return 200, {"transcript": service.restore_transcript_from_asr(
                actor=payload.get("actor") or service.project_actor(),
                slot=_slot(payload))}

        if rest == ["glossary"] and method == "GET":
            return 200, {"glossary": service.glossary_view()}

        if rest == ["glossary"] and method == "POST":
            payload = _json_body(body)
            actor = payload.get("actor") or service.project_actor()
            if payload.get("accept_term"):
                return 200, {"glossary": service.accept_glossary_suggestion(
                    str(payload["accept_term"]), actor=actor)}
            return 200, {"glossary": service.set_glossary_terms(
                payload.get("terms") or [], actor=actor)}

        # ---------------------------------------------- Long-form Video Studio
        # GET only DESCRIBES the workflow; it never starts a stage. Opening the
        # screen must not spend a provider request.
        if rest == ["long-form"] and method == "GET":
            return 200, {"long_form_studio": service.long_form_studio().view()}

        if rest == ["long-form", "control"] and method == "POST":
            payload = _json_body(body)
            studio = service.long_form_studio()
            actor = payload.get("actor") or service.project_actor()
            action = str(payload.get("action") or "")
            if action == "start":
                studio.start(actor=actor)
            elif action == "pause":
                studio.pause(actor=actor)
            elif action == "resume":
                studio.resume(actor=actor)
            elif action == "cancel":
                studio.cancel(actor=actor)
            elif action == "approve_stage":
                studio.approve_stage(_required(payload, "stage"), actor=actor)
            elif action == "retry_billing":
                studio.retry_billing(actor=actor)
            else:
                raise ApiError("validation_failed",
                               detail=f"uzun video eylemi: {action!r}")
            return 200, {"long_form_studio": studio.view()}

        if rest == ["long-form", "stage"] and method == "POST":
            payload = _json_body(body)
            studio = service.long_form_studio()
            actor = payload.get("actor") or service.project_actor()
            outcome = studio.run_next_stage(actor=actor)
            return 200, {"ran": outcome, "long_form_studio": studio.view()}

        if rest == ["long-form", "chapter"] and method == "POST":
            payload = _json_body(body)
            studio = service.long_form_studio()
            studio.save_chapter_text(
                int(_required(payload, "index")), _required(payload, "text"),
                actor=payload.get("actor") or service.project_actor())
            return 200, {"long_form_studio": studio.view()}

        if rest == ["long-form", "tts"] and method == "POST":
            payload = _json_body(body)
            studio = service.long_form_studio()
            actor = payload.get("actor") or service.project_actor()
            action = str(payload.get("action") or "run")
            if action == "plan":
                studio.plan_tts(actor=actor)
            elif action in ("retry", "regenerate"):
                studio.run_tts(actor=actor,
                               only_chunk=_required(payload, "chunk_id"))
            elif action == "concat":
                studio.concat_audio(actor=actor)
            else:
                studio.run_tts(actor=actor,
                               max_chunks=int(payload.get("max_chunks") or 0))
            return 200, {"long_form_studio": studio.view()}

        if rest == ["long-form", "broll"] and method == "POST":
            payload = _json_body(body)
            studio = service.long_form_studio()
            actor = payload.get("actor") or service.project_actor()
            action = str(payload.get("action") or "")
            if action == "plan":
                studio.create_broll_plan(
                    clip_duration_sec=int(payload.get("clip_duration_sec") or 5),
                    actor=actor)
            elif action == "search":
                studio.search_broll(
                    _required(payload, "slot_id"),
                    provider=_required(payload, "provider"),
                    query=_required(payload, "query"),
                    per_page=int(payload.get("per_page") or 6), actor=actor)
            elif action == "select":
                studio.select_broll(
                    _required(payload, "slot_id"),
                    _required(payload, "candidate_id"), actor=actor)
            elif action == "download":
                studio.download_broll(_required(payload, "slot_id"), actor=actor)
            else:
                raise ApiError("validation_failed",
                               detail=f"B-roll eylemi: {action!r}")
            return 200, {"long_form_studio": studio.view()}

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
                target_tab = payload.get("target_tab", "moments")
                review = service.request_render_revision(
                    requested_by=payload.get("requested_by") or service.project_actor(),
                    notes=payload.get("notes", ""),
                    target_tab=target_tab, slot=slot)
                # Recording the note and STARTING the fix are separate steps on
                # purpose: the note must survive even if the job is refused,
                # and a quota-spending job only starts once the operator has
                # confirmed it.
                started = self._start_revision_job(
                    service, ownership, target_tab,
                    confirmed=bool(payload.get("start_confirmed")),
                    actor=payload.get("requested_by") or service.project_actor())
                return 200, {"render_review": service.render_review_state(slot),
                             "revision_run": started}
            if action == "clear_revision":
                return 200, {"render_review": service.clear_render_revision(
                    resolved_by=payload.get("resolved_by") or service.project_actor(),
                    slot=slot)}
            raise ApiError("validation_failed", detail=f"eylem: {action!r}")

        if rest == ["clip-bounds"] and method == "GET":
            slot = _slot(query)
            return 200, {"clip_bounds": service.clip_bounds_view(slot)}

        if rest == ["clip-bounds"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "save"
            slot = _slot(payload)
            actor = payload.get("actor") or service.project_actor()
            if action == "save":
                for key in ("start", "end"):
                    if payload.get(key) is None:
                        raise ApiError("validation_failed",
                                       detail=f"{key} zorunlu")
                try:
                    start = float(payload["start"])
                    end = float(payload["end"])
                except (TypeError, ValueError):
                    raise ApiError("validation_failed",
                                   detail="start ve end sayisal olmali")
                result = service.save_clip_bounds(start=start, end=end,
                                                  actor=actor, slot=slot)
                return 200, {"clip_bounds_saved": result,
                             "clip_bounds": service.clip_bounds_view(slot)}
            if action == "reset":
                result = service.reset_clip_bounds(actor=actor, slot=slot)
                return 200, {"clip_bounds_saved": result,
                             "clip_bounds": service.clip_bounds_view(slot)}
            raise ApiError("validation_failed", detail=f"eylem: {action!r}")

        if rest == ["render-slot"] and method == "GET":
            # EVERYTHING the Render Studio screen needs for ONE output slot, in
            # one request.
            #
            # The screen used to be slot 1 only: the batch board's "Short 3"
            # button navigated here and silently showed Short 1's video,
            # checklist and approval. Splitting this into three fetches would
            # let those three arrive separately, so the page could render a
            # slot-3 video above a slot-1 checklist for a frame -- which is the
            # same lie, briefly.
            slot = _slot(query)
            try:
                slot = service.require_slot(slot)
            except PipelineServiceError as exc:
                raise ApiError("validation_failed", detail=str(exc)) from None
            return 200, {"render_slot": {
                "slot": slot,
                "render_profile": service.render_profile_view(slot),
                "clip_bounds": service.clip_bounds_view(slot),
                "render_review": service.render_review_state(slot),
                "approvals": {"render": service.approval_state("render", slot)},
            }}

        if rest == ["render-profile"] and method == "GET":
            slot = _slot(query)
            profile = service.render_profile(slot)
            return 200, {"render_profile": {
                **profile,
                "slot": slot,
                "effective": service.effective_render_config(slot),
                "impact": service.render_profile_impact(slot),
                "options": {
                    "quality_presets": sorted(render_profile.QUALITY_PRESETS),
                    "resolution_presets": sorted(render_profile.RESOLUTION_PRESETS),
                    "render_modes": list(render_profile.RENDER_MODES),
                    "caption_themes": ["auto"] + list(render_profile.allowed_themes()),
                    "active_word_colors": sorted(render_profile.ACTIVE_COLORS),
                    "delivery_quality": render_profile.DELIVERY_QUALITY,
                },
            }}

        if rest == ["render-profile"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "save"
            slot = _slot(payload)
            actor = payload.get("actor") or service.project_actor()
            if action == "save":
                settings = payload.get("settings")
                if not isinstance(settings, dict):
                    raise ApiError("validation_failed", detail="settings bir nesne olmali")
                # `expected_hash` is passed through UNCHANGED, including when it is
                # absent: the service decides what a missing echo means, so a
                # client cannot skip the stale-write guard by omitting the field
                # and having the API invent a matching value.
                result = service.save_render_profile(
                    settings, actor=actor, slot=slot,
                    expected_hash=payload.get("expected_hash"),
                    reason=payload.get("reason", ""))
                return 200, {"render_profile_saved": result,
                             "render_profile": service.render_profile(slot),
                             "effective": service.effective_render_config(slot)}
            if action == "reset":
                result = service.reset_render_profile(actor=actor, slot=slot)
                return 200, {"render_profile_saved": result,
                             "render_profile": service.render_profile(slot),
                             "effective": service.effective_render_config(slot)}
            raise ApiError("validation_failed", detail=f"eylem: {action!r}")

        if rest == ["copyright-review"] and method == "GET":
            return 200, {"copyright_review": service.copyright_review_state()}

        if rest == ["copyright-review"] and method == "POST":
            payload = _json_body(body)
            action = payload.get("action") or "set_item"
            if action == "set_item":
                return 200, {"copyright_review": service.set_copyright_review_item(
                    payload.get("key", ""), checked=bool(payload.get("checked")),
                    reviewed_by=payload.get("reviewed_by") or service.project_actor(),
                    note=payload.get("note", ""))}
            if action == "request_revision":
                return 200, {"copyright_review": service.request_copyright_revision(
                    requested_by=payload.get("requested_by") or service.project_actor(),
                    notes=_required({"note": payload.get("notes")}, "note"),
                    target_tab=payload.get("target_tab", "phase0"))}
            if action == "clear_revision":
                return 200, {"copyright_review": service.clear_copyright_revision(
                    cleared_by=payload.get("resolved_by") or service.project_actor())}
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
                target_tab = payload.get("target_tab", "voice")
                service.request_voice_revision(
                    requested_by=payload.get("requested_by") or service.project_actor(),
                    notes=_required({"note": payload.get("notes")}, "note"),
                    target_tab=target_tab)
                started = self._start_revision_job(
                    service, ownership, target_tab,
                    confirmed=bool(payload.get("start_confirmed")),
                    actor=payload.get("requested_by") or service.project_actor())
                return 200, {"voice_review": service.voice_review_state(),
                             "revision_run": started}
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

        # ---------------------------------------------------- faz 2 scene editor
        if rest == ["edl"] and method == "POST":
            payload = _json_body(body)
            return 201, {"edl": service.build_edl(
                source_path=payload.get("source_path"),
                source_duration=float(payload.get("source_duration", 0)),
                title=payload.get("title", ""),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["edl"] and method == "GET":
            return 200, {"edl": service.load_artifact("edl")["data"]}

        if rest == ["edl", "clips"] and method == "POST":
            payload = _json_body(body)
            return 201, {"edl": service.add_edl_clip(
                source_path=_required(payload, "source_path"),
                source_start_sec=float(payload.get("source_start_sec", 0)),
                source_end_sec=float(_required(payload, "source_end_sec")),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if len(rest) == 3 and rest[0] == "edl" and rest[1] == "clips" and method == "PATCH":
            payload = _json_body(body)
            return 200, {"edl": service.update_edl_clip(
                clip_index=int(rest[2]),
                start=payload.get("start"),
                end=payload.get("end"),
                crop=payload.get("crop"),
                zoom=payload.get("zoom"),
                caption_style=payload.get("caption_style"),
                add_b_roll=payload.get("add_b_roll"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["edl", "music"] and method == "POST":
            payload = _json_body(body)
            return 200, {"edl": service.set_edl_music(
                music_path=_required(payload, "music_path"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["edl", "preview"] and method == "POST":
            payload = _json_body(body)
            return 200, {"preview_command": service.build_edl_preview(
                output_name=payload.get("output_name", "preview.mp4"),
            )}

        if rest == ["edl", "render"] and method == "POST":
            payload = _json_body(body)
            return 200, service.render_edl_preview(
                output_name=payload.get("output_name", "preview.mp4"),
            )

        # ------------------------------------------------- trend ingest routes
        if rest == ["source-candidates"] and method == "POST":
            payload = _json_body(body)
            return 201, {"source_candidates": service.discover_source_candidates(
                terms=payload.get("terms"),
                manual_urls=payload.get("manual_urls"),
                max_search_calls=int(payload.get("max_search_calls", 4)),
                lookback_days=int(payload.get("lookback_days", 14)),
                shorts_only=_truthy(payload.get("shorts_only")),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["source-candidates", "probe"] and method == "POST":
            payload = _json_body(body)
            return 200, {"probe": service.probe_source_candidate(
                candidate_url=_required(payload, "candidate_url"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["rights"] and method == "POST":
            payload = _json_body(body)
            return 201, {"rights_decision": service.record_rights_decision(
                candidate_url=_required(payload, "candidate_url"),
                license_type=_required(payload, "license_type"),
                decided_by=_required(payload, "decided_by"),
                attribution=payload.get("attribution", ""),
                transformation_note=payload.get("transformation_note", ""),
                risk_acknowledged=_truthy(payload.get("risk_acknowledged")),
                source_title=payload.get("source_title", ""),
                source_channel=payload.get("source_channel", ""),
                notes=payload.get("notes", ""),
            )}

        if rest == ["ingest"] and method == "POST":
            payload = _json_body(body)
            record, created = self.app.submit_job(
                ownership, "source_ingest", params={"candidate_url": _required(payload, "candidate_url")},
                submitted_by=_header(headers, "x-actor") or payload.get("actor") or "console",
                run_request_id=payload.get("run_request_id"))
            return (201 if created else 200), {"job": record, "created": created}

        if rest == ["ingest", "state"] and method == "GET":
            return 200, {"ingest_state": service.ingest_state()}

        # --------------------------------------------- publication routes
        if rest == ["metadata"] and method == "GET":
            return 200, {"publication_metadata": service.load_artifact("publication_metadata")}

        if rest == ["metadata", "generate"] and method == "POST":
            record, created = self.app.submit_job(
                ownership, "metadata_generation", params={},
                submitted_by=_header(headers, "x-actor") or "console",
                run_request_id=_json_body(body).get("run_request_id"))
            return (201 if created else 200), {"job": record, "created": created}

        if rest == ["metadata", "manual"] and method == "POST":
            payload = _json_body(body)
            return 201, {"publication_metadata": service.write_manual_metadata(
                title=_required(payload, "title"),
                description=payload.get("description", ""),
                tags=payload.get("tags"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["metadata", "select"] and method == "POST":
            payload = _json_body(body)
            return 200, {"publication_metadata": service.select_publication_metadata(
                title_index=int(_required(payload, "title_index")),
                description=payload.get("description"),
                tags=payload.get("tags"),
                category_id=payload.get("category_id"),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["metadata", "approve"] and method == "POST":
            payload = _json_body(body)
            return 200, {"publication_metadata": service.approve_publication_metadata(
                reviewed_sha256=_required(payload, "reviewed_sha256"),
                approved_by=_required(payload, "approved_by"),
                notes=payload.get("notes", ""),
            )}

        if rest == ["publish", "state"] and method == "GET":
            return 200, {"publish_state": service.publish_state()}

        if rest == ["publish", "intent"] and method == "POST":
            payload = _json_body(body)
            return 201, {"publish_intent": service.create_publish_intent(
                platform=payload.get("platform", "youtube"),
                visibility=payload.get("visibility", "private"),
                publish_at=payload.get("publish_at"),
                made_for_kids=_truthy(payload.get("made_for_kids")),
                account_ref=payload.get("account_ref", ""),
                actor=_header(headers, "x-actor") or payload.get("actor") or "console",
            )}

        if rest == ["publish", "intent", "approve"] and method == "POST":
            payload = _json_body(body)
            return 200, {"publish_intent": service.approve_publish_intent(
                reviewed_sha256=_required(payload, "reviewed_sha256"),
                approved_by=_required(payload, "approved_by"),
                notes=payload.get("notes", ""),
            )}

        if rest == ["publish", "upload"] and method == "POST":
            payload = _json_body(body)
            record, created = self.app.submit_job(
                ownership, "publish_upload", params={},
                submitted_by=_header(headers, "x-actor") or payload.get("actor") or "console",
                run_request_id=payload.get("run_request_id"))
            return (201 if created else 200), {"job": record, "created": created}

        if rest == ["publish", "visibility", "approve"] and method == "POST":
            payload = _json_body(body)
            return 200, {"publish_manifest": service.approve_publish_visibility(
                target_visibility=_required(payload, "target_visibility"),
                reviewed_remote_id=_required(payload, "reviewed_remote_id"),
                approved_by=_required(payload, "approved_by"),
                publish_at=payload.get("publish_at"),
                notes=payload.get("notes", ""),
            )}

        if rest == ["publish", "visibility", "apply"] and method == "POST":
            record, created = self.app.submit_job(
                ownership, "visibility_change", params={},
                submitted_by=_header(headers, "x-actor") or _json_body(body).get("actor") or "console",
                run_request_id=_json_body(body).get("run_request_id"))
            return (201 if created else 200), {"job": record, "created": created}

        # GET only DESCRIBES the Turbo workflow. Opening the tab must not spend
        # an LLM, TTS, stock-provider or render request -- this is a pure read.
        if rest == ["turbo"] and method == "GET":
            return 200, {"turbo_studio": service.turbo_studio().view()}

        if rest == ["turbo", "brief"] and method == "POST":
            payload = _json_body(body)
            # Presence only. The length/range rules belong to
            # `turbo.validate_brief` -- duplicating them here would give the
            # project two sources of truth that silently drift apart, and
            # would skip the rejection event the studio records.
            _required(payload, "topic")
            studio = service.turbo_studio()
            actor = payload.get("actor") or service.project_actor()
            result = studio.save_brief(payload, actor=actor)
            return 200, {"saved": result["invalidated"],
                         "turbo_studio": studio.view()}

        if rest == ["turbo", "approve"] and method == "POST":
            payload = _json_body(body)
            _required(payload, "stage")
            studio = service.turbo_studio()
            studio.approve_stage(
                str(payload.get("stage") or ""),
                actor=payload.get("actor") or service.project_actor(),
                note=str(payload.get("note") or ""))
            return 200, {"turbo_studio": studio.view()}

        # Runs whatever is runnable NOW. It never skips a human gate: if the
        # next stage is one, this returns the reason and runs nothing.
        if rest == ["turbo", "stage"] and method == "POST":
            payload = _json_body(body)
            studio = service.turbo_studio()
            outcome = studio.run_next_stage(
                actor=payload.get("actor") or service.project_actor(),
                max_chunks=int(payload.get("max_chunks") or 0))
            return 200, {"ran": outcome["ran"], "decision": outcome["decision"],
                         "turbo_studio": studio.view()}

        if rest == ["turbo", "media"] and method == "POST":
            payload = _json_body(body)
            studio = service.turbo_studio()
            actor = payload.get("actor") or service.project_actor()
            action = str(payload.get("action") or "")
            # Search, select and download stay three separate operator actions.
            # Collapsing them would make opening a slot spend a provider request.
            if action == "plan":
                studio.run_media_plan(actor=actor)
            elif action == "search":
                _required(payload, "slot_id")
                studio.search_slot(
                    payload["slot_id"], provider=str(payload.get("provider") or ""),
                    query=str(payload.get("query") or ""),
                    per_page=int(payload.get("per_page") or 6), actor=actor)
            elif action == "select":
                _required(payload, "slot_id")
                _required(payload, "candidate_id")
                studio.select_clip(payload["slot_id"], payload["candidate_id"],
                                   actor=actor)
            elif action == "download":
                _required(payload, "slot_id")
                studio.download_clip(payload["slot_id"], actor=actor)
            else:
                raise ApiError("validation_failed",
                               detail="materyal eylemi plan, search, select veya "
                                      "download olmali")
            return 200, {"turbo_studio": studio.view()}

        if rest == ["turbo", "music"] and method == "POST":
            payload = _json_body(body)
            studio = service.turbo_studio()
            studio.choose_music(
                origin=str(payload.get("origin") or "none"),
                filename=str(payload.get("filename") or ""),
                volume=float(payload.get("volume") or 0.18),
                ducking=bool(payload.get("ducking", True)),
                actor=payload.get("actor") or service.project_actor())
            return 200, {"turbo_studio": studio.view()}

        if rest == ["turbo", "music", "upload"] and method == "POST":
            # Streamed like the source-video upload: the extension is checked
            # before a byte is written and the size cap is enforced twice.
            filename = unquote(_header(headers, "x-filename")
                               or _first(query.get("filename")) or "")
            studio = service.turbo_studio()
            actor = _header(headers, "x-actor") or service.project_actor()
            if body_stream is not None:
                try:
                    length = int(content_length)
                except (TypeError, ValueError):
                    length = 0
                if length <= 0:
                    raise ApiError("empty_upload")
                track = studio.upload_music(
                    filename=filename, stream=body_stream,
                    content_length=length, actor=actor)
            else:
                if not body:
                    raise ApiError("empty_upload")
                track = studio.upload_music(
                    filename=filename, payload=body,
                    content_length=len(body), actor=actor)
            return 201, {"track": track, "turbo_studio": studio.view()}

        if rest == ["turbo", "render"] and method == "POST":
            payload = _json_body(body)
            studio = service.turbo_studio()
            preset = str(payload.get("preset") or "preview")
            record = studio.render(
                preset=preset,
                actor=payload.get("actor") or service.project_actor())
            return 200, {"render": record, "turbo_studio": studio.view()}

        if rest == ["turbo", "revision"] and method == "POST":
            payload = _json_body(body)
            _required(payload, "kind")
            studio = service.turbo_studio()
            result = studio.apply_revision(
                str(payload["kind"]), target=str(payload.get("target") or ""),
                values=payload.get("values") or {},
                actor=payload.get("actor") or service.project_actor())
            return 200, {"revision": result, "turbo_studio": studio.view()}

        if rest == ["turbo", "revision-preview"] and method == "GET":
            kind = _first(query.get("kind")) or ""
            return 200, {"effect": service.turbo_studio().revision_preview(kind)}

        raise ApiError("unknown_route", detail=f"{method} /{'/'.join(parts)}")

    # -------------------------------------------------------------- helpers
    def _start_revision_job(self, service, ownership, target_tab, *,
                            confirmed: bool, actor: str) -> dict:
        """Start the phase that owns this fix, when starting it is meaningful.

        Three outcomes, and each one is reported rather than guessed at:
          - not runnable: choosing a moment or changing the plan is a human
            decision, so the console navigates instead of pretending to work.
          - needs confirmation: script and TTS spend provider quota, and a
            mis-click used to cost nothing. The caller re-sends with
            `start_confirmed` once the operator has agreed.
          - started: the job is queued exactly like any other run, so it gets
            the same progress, retry and audit treatment.
        """
        plan = service.revision_action(target_tab)
        if not plan["runnable"]:
            return {**plan, "started": False, "job": None}
        if plan["needs_confirmation"] and not confirmed:
            return {**plan, "started": False, "job": None,
                    "awaiting_confirmation": True}
        record, created = self.app.submit_job(
            ownership, plan["job_type"], params={}, submitted_by=actor,
            run_request_id=f"revision-{target_tab}")
        from .app import _job_view

        return {**plan, "started": True, "created": bool(created),
                "job": _job_view(record)}

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
        # Transport adapters stream this validated path. Returning bytes here
        # would load every preview into RAM and prevents HTTP Range handling.
        return 200, {"__file__": target, "__content_type__": content_type}

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
