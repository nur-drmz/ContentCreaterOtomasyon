"""Turbo Studio: the service that runs the fourteen stages.

WHY A SEPARATE MODULE
---------------------
Same reasoning as `long_form_studio.py`: `PipelineService` owns the Shorts
pipeline, and bolting a second full workflow onto it would make one class
responsible for three. This wraps the service, so Turbo reuses the same
storage, ownership, audit, approval, quota and provider-routing code without
enlarging the class that owns short-form behaviour.

WHAT IT GUARANTEES
------------------
  * Opening the page spends nothing. `view()` reads artifacts off disk and
    returns them; it starts no stage, contacts no provider and creates no file.
  * Every stage is persisted before and after it runs, so a restart resumes at
    the first stage that is not `completed`.
  * A revision invalidates by dependency, not by position: re-recording one
    narration chunk re-packs the timeline but keeps every downloaded clip.
  * Nothing here claims a music track is copyright-safe. A track without
    verified licence metadata is reported as unverified, and stays that way.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from local_pipeline import (google_ai, music_library, provider_adapters,
                            render_utils, stock_media, timeline, turbo,
                            turbo_script, tts_chunks, stage_machine)

from . import quota_signals, tts_runner

EVENT_LOG = "results/turbo/events.jsonl"
QUOTA_LEDGER = "results/turbo/quota_ledger.json"
AUDIO_DIRNAME = "results/turbo/tts"
MUSIC_DIRNAME = "results/turbo/music"
RENDER_DIRNAME = "results/turbo/renders"
GRAPH_DIRNAME = "results/turbo/graphs"

#: The complete event vocabulary. An unknown name raises rather than being
#: written, so a typo shows up as a failure instead of an unqueryable log line.
EVENTS = (
    "turbo.brief_saved", "turbo.brief_rejected",
    "turbo.stage_started", "turbo.stage_completed", "turbo.stage_failed",
    "turbo.stage_approved", "turbo.stage_waiting", "turbo.stage_blocked_billing",
    "turbo.revision_applied", "turbo.cancelled", "turbo.paused", "turbo.resumed",
    "script.started", "script.completed", "script.failed",
    "tts_chunk.started", "tts_chunk.completed", "tts_chunk.failed",
    "tts_chunk.retried", "tts_chunk.waiting",
    "audio_concat.started", "audio_concat.completed", "audio_concat.failed",
    "media_plan.created", "media.search_completed", "media.selected",
    "media.download_completed", "media.replaced",
    # The anchor clip is cut from the user's own source, not fetched from a
    # provider, so it gets its own event rather than pretending a download.
    "media.source_moment_extracted",
    "music.selected", "music.removed", "music.license_unverified",
    "subtitles.completed", "subtitles.degraded",
    "timeline.completed", "timeline.repacked",
    "render.started", "render.completed", "render.failed",
    "archive.completed",
)

#: Dropped from every event even if a caller passes them. Sanitising here
#: rather than at each call site means a future caller cannot leak a prompt or
#: a credential by passing it alongside the metrics.
_FORBIDDEN_EVENT_FIELDS = frozenset({
    "prompt", "prompts", "text", "script", "narration", "api_key", "credential",
    "token", "authorization", "secret", "path", "output_path", "source_path",
    "url", "request_url",
})

TurboError = turbo.TurboError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurboStudio:
    """One project's Turbo workflow."""

    def __init__(self, service):
        self.service = service
        self.storage = service.storage
        self.ownership = service.ownership

    # -------------------------------------------------------------- storage
    def _path(self, relative: str) -> Path:
        return Path(self.storage.resolve(self.ownership, relative,
                                         create_parent=True))

    def _read(self, relative: str):
        path = self._path(relative)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, relative: str, record: dict, *, actor: str) -> str:
        from .storage import atomic_write_json
        path = self._path(relative)
        atomic_write_json(path, record)
        rel = self.storage.relative_to_namespace(self.ownership, path)
        self.service.audit.record(
            "artifact.written", actor=actor, artifact=rel,
            detail={"status": record.get("status"),
                    "artifact_type": record.get("artifact_type")})
        return rel

    # --------------------------------------------------------------- events
    def log_event(self, name: str, **fields) -> dict:
        if name not in EVENTS:
            raise TurboError("bilinmeyen olay: {0!r}".format(name))
        payload = {"event": name, "at": _now(),
                   "project_id": self.ownership.project_id}
        for key, value in (fields or {}).items():
            if key in _FORBIDDEN_EVENT_FIELDS:
                continue
            if isinstance(value, str) and len(value) > 300:
                value = value[:300]
            payload[key] = value
        with open(self._path(EVENT_LOG), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def events(self, *, limit: int = 100) -> list:
        path = self._path(EVENT_LOG)
        if not path.is_file():
            return []
        parsed = []
        for line in path.read_text(encoding="utf-8").splitlines()[-int(limit):]:
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return parsed

    # -------------------------------------------------------------- records
    def brief(self):
        return self._read(turbo.BRIEF_ARTIFACT)

    def workflow(self):
        return self._read(turbo.WORKFLOW_ARTIFACT)

    def _save_workflow(self, record: dict, *, actor: str = "system") -> dict:
        self._write(turbo.WORKFLOW_ARTIFACT, record, actor=actor)
        return record

    def _require_workflow(self) -> dict:
        record = self.workflow()
        if record is None:
            raise TurboError("once brief kaydedin; is akisi henuz baslamadi")
        return record

    # ---------------------------------------------------------------- brief
    def save_brief(self, payload: dict, *, actor: str = "operator") -> dict:
        """Validate and persist the brief, then start or reset the workflow.

        Changing a brief field that affects the output invalidates everything
        from `script` onward -- this is the one genuinely positional case, since
        a different brief means a different video end to end. A cosmetic edit
        (notes) leaves `brief_sha256` unchanged and costs nothing.
        """
        try:
            validated = turbo.validate_brief(payload)
        except TurboError as exc:
            self.log_event("turbo.brief_rejected", reason=str(exc)[:200])
            raise
        previous = self.brief() or {}
        digest = validated["brief_sha256"]
        changed = bool(previous) and previous.get("brief_sha256") != digest

        self._write(turbo.BRIEF_ARTIFACT, validated, actor=actor)

        record = self.workflow()
        if record is None:
            record = turbo.new_workflow(brief_digest=digest,
                                        ownership=self.ownership)
        elif changed:
            record = turbo.invalidate_from(
                record, "script", reason="brief degisti")
            record["brief_sha256"] = digest
        else:
            record["brief_sha256"] = digest
        self._save_workflow(record, actor=actor)
        self.log_event("turbo.brief_saved", brief_sha256=digest,
                       invalidated=changed,
                       duration_sec=validated["duration_sec"],
                       aspect_ratio=validated["aspect_ratio"],
                       clip_count=validated["estimates"]["clip_count"])
        return {"brief": validated, "invalidated": changed,
                "progress": turbo.progress_summary(record)}

    # -------------------------------------------------------- human gates
    def approve_stage(self, key: str, *, actor: str = "operator",
                      note: str = "") -> dict:
        """Close a human gate. Only gates -- an automatic stage is completed by
        running it, and letting a person mark one `completed` by hand would
        let the workflow claim work that never happened."""
        if key not in turbo.STAGE_KEYS:
            raise TurboError("bilinmeyen asama: {0!r}".format(key))
        if key in turbo.AUTOMATIC_STAGES:
            raise TurboError(
                "{0} otomatik bir asama; onaylanarak degil calistirilarak "
                "tamamlanir".format(turbo.STAGE_LABELS[key]))
        record = self._require_workflow()
        blocking = [name for name in turbo.STAGE_KEYS[:turbo.STAGE_KEYS.index(key)]
                    if turbo.stage_state(record, name) != turbo.COMPLETED]
        if blocking:
            raise TurboError(
                "once su asamalar tamamlanmali: "
                + ", ".join(turbo.STAGE_LABELS[name] for name in blocking))
        record = turbo.set_stage(record, key, turbo.COMPLETED,
                                 detail=(note or "operator onayi"))
        self._save_workflow(record, actor=actor)
        self.log_event("turbo.stage_approved", stage=key)
        return turbo.progress_summary(record)

    # ------------------------------------------------------------ revisions
    def revision_preview(self, kind: str) -> dict:
        """What a revision would cost, computed before it is applied.

        The operator sees what goes stale AND what survives before deciding,
        because "regenerate this narration chunk" sounds cheap and would be
        ruinous if it silently discarded a hundred downloaded clips.
        """
        effect = turbo.revision_effect(kind)
        record = self.workflow()
        if record is not None:
            effect["currently_completed"] = [
                key for key in effect["stale_stages"]
                if turbo.stage_state(record, key) == turbo.COMPLETED]
        return effect

    # ------------------------------------------------------------- provider
    def ledger(self):
        """One PERSISTENT quota ledger for this project's Turbo workflow.

        Without a path a `QuotaLedger` lives and dies inside a single call, so
        every request would start the count from zero and the project would
        appear to have spent nothing while the router was already refusing it.
        """
        return google_ai.QuotaLedger(path=self._path(QUOTA_LEDGER))

    def quota_summary(self) -> dict:
        data = self.ledger().data or {}
        models = data.get("models") or {}
        return {
            "models": [
                {"model": name,
                 "requests_today": int(record.get("requests_current_day") or 0),
                 "requests_this_minute":
                     int(record.get("requests_current_minute") or 0),
                 "configured_rpd": int(record.get("configured_rpd") or 0),
                 "configured_rpm": int(record.get("configured_rpm") or 0),
                 "cooldown_until": record.get("cooldown_until")}
                for name, record in sorted(models.items())],
        }

    def _record_stage_failure(self, stage: str, exc, result, *, actor: str,
                              started: float, event: str) -> None:
        """Persist a failure, telling a quota WAIT apart from a real error.

        A 429 recorded as `failed` invites the operator to press retry, which
        spends more of the quota that just ran out. A wait carries `resume_at`
        and says "come back", which is the only useful thing to say.
        """
        quota_kind = quota_signals.quota_kind_from_result(result or {})
        if quota_kind is None:
            quota_kind = quota_signals.quota_kind_from_exception(exc)
        latency = round(time.time() - started, 3)
        record = self._require_workflow()
        if quota_kind:
            resume_at = (None if quota_kind == turbo.BLOCKED_BILLING
                         else quota_signals.quota_resume_at(quota_kind))
            if quota_kind == turbo.BLOCKED_BILLING:
                detail = ("Google AI Studio plan/billing veya kota yapilandirmasi "
                          "istegi engelliyor; ayarlari duzeltip acikca yeniden deneyin")
                name = "turbo.stage_blocked_billing"
            elif quota_kind == turbo.WAITING_DAILY_QUOTA:
                detail = "gunluk kota doldu; yeniden denemek kotayi tuketir"
                name = "turbo.stage_waiting"
            else:
                detail = "dakikalik hiz siniri; kisa sure sonra devam"
                name = "turbo.stage_waiting"
            self._save_workflow(turbo.set_stage(
                record, stage, quota_kind, resume_at=resume_at, detail=detail),
                actor=actor)
            self.log_event(name, stage=stage, wait_state=quota_kind,
                           resume_at=resume_at, latency_sec=latency)
            return
        self._save_workflow(
            turbo.set_stage(record, stage, turbo.FAILED, error=str(exc)),
            actor=actor)
        self.log_event(event, stage=stage, error_kind=type(exc).__name__,
                       latency_sec=latency)

    # -------------------------------------------------------------- script
    def script(self):
        return self._read(turbo.SCRIPT_ARTIFACT)

    def run_script(self, *, actor: str = "console", instructions: str = "") -> dict:
        """One routed call returning narration AND its scene beats.

        One call rather than one per beat: the two halves have to agree with
        each other, and generating them separately would spend a second request
        re-deriving alignment the first call already had.
        """
        brief = self.brief()
        if not brief:
            raise TurboError("once brief kaydedin")
        record = self._require_workflow()
        self._save_workflow(
            turbo.set_stage(record, "script", turbo.RUNNING, attempt_delta=1),
            actor=actor)
        self.log_event("script.started", task="script_generation")
        started = time.time()
        result = None
        try:
            client = self.service._build_google_client()
            prompt = turbo_script.build_script_prompt(
                brief, instructions=instructions)
            result = google_ai._text_task(
                "script_generation", prompt,
                client=client, ledger=self.ledger())
            # Google first, always: it owns the quota ledger and the circuit
            # breaker. Only a route that is genuinely exhausted continues onto
            # Groq/Cloudflare, and the artifact records which model answered.
            result = provider_adapters.continue_text_route(
                result, prompt, capability="high_quality_text",
                secret_getter=self.service.secrets.getter,
                fallback_error_kinds=google_ai.FALLBACK_ERROR_KINDS)
            if result.get("status") != "ok":
                raise TurboError(result.get("status_reason") or "script uretimi basarisiz")
            script = turbo_script.parse_script(result.get("output"), brief)
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            self._record_stage_failure("script", exc, result, actor=actor,
                                       started=started, event="script.failed")
            raise
        script["provider"] = result.get("provider")
        script["model"] = result.get("model_used") or result.get("model")
        script["fallback_used"] = bool(result.get("fallback_used"))
        script["fallback_reason"] = result.get("fallback_reason")
        script["provider_attempts"] = list(result.get("provider_attempts") or [])
        self._write(turbo.SCRIPT_ARTIFACT, script, actor=actor)
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "script", turbo.COMPLETED,
            detail=f"{script['beat_count']} sahne, {script['word_count']} kelime"),
            actor=actor)
        self.log_event("script.completed", beat_count=script["beat_count"],
                       word_count=script["word_count"],
                       beat_coverage=script["beat_coverage"],
                       model=result.get("model_used"),
                       latency_sec=round(time.time() - started, 3))
        return script

    # ----------------------------------------------------------------- TTS
    def chunk_manifest(self):
        return self._read(turbo.CHUNK_ARTIFACT) or tts_chunks.new_manifest(
            ownership=self.ownership)

    def plan_tts(self, *, actor: str = "console") -> dict:
        """Rebuild the chunk plan from the CURRENT script, keeping valid audio.

        `merge_plan` matches by the identity hash of the text and voice, so a
        script edit that left most sentences untouched re-speaks only what
        actually changed.
        """
        script = self.script()
        if not script:
            raise TurboError("once scripti uretin")
        brief = self.brief() or {}
        model = google_ai.TASK_ROUTES["tts"][0]
        # The WHOLE narration as one unit, not one unit per scene beat.
        # `plan_chunks` never lets a chunk straddle a "chapter" boundary --
        # correct for long-form, where a chapter is ~420 words and editing
        # chapter 3 should only re-speak chapter 3. Turbo's beats are ~12 words
        # each (one 5-second clip), so feeding them in as chapters produced ONE
        # TTS REQUEST PER FIVE SECONDS OF VIDEO: a 60-second brief that
        # estimated 1 chunk planned 12, burning a 10-request daily quota on a
        # one-minute video and splicing the narration from 12 separately
        # synthesised fragments. Found on the first real run.
        planned = tts_chunks.plan_chunks(
            [{"index": 1, "id": "turbo_narration",
              "title": brief.get("topic") or "",
              "text": script.get("narration") or ""}],
            words_per_chunk=turbo.TTS_WORDS_PER_CHUNK,
            voice=brief.get("voice") or google_ai.DEFAULT_TTS_VOICE,
            model=model, provider="google", narration_direction="",
            language_code=brief.get("language") or google_ai.DEFAULT_TTS_LANGUAGE)
        manifest = tts_chunks.merge_plan(self.chunk_manifest(), planned)
        self._write(turbo.CHUNK_ARTIFACT, manifest, actor=actor)
        return manifest

    def run_tts(self, *, actor: str = "console", only_chunk: str = "",
                tracker=None, cancel_check=None, max_chunks: int = 0) -> dict:
        """Speak the MISSING chunks only. The pass itself is `tts_runner`'s."""
        manifest = (self.plan_tts(actor=actor) if not only_chunk
                    else self.chunk_manifest())
        pending = tts_chunks.pending_chunks(manifest)
        if only_chunk:
            _index, chunk = tts_chunks.find_chunk(manifest, only_chunk)
            if chunk is None:
                raise TurboError(f"bilinmeyen parca: {only_chunk!r}")
            pending = [chunk]
        if max_chunks:
            pending = pending[:int(max_chunks)]

        outcome = tts_runner.run_chunk_pass(
            self, manifest=manifest, manifest_artifact=turbo.CHUNK_ARTIFACT,
            audio_dir=self._path(AUDIO_DIRNAME), pending=pending, actor=actor,
            tracker=tracker, cancel_check=cancel_check)
        readiness, waiting = outcome["readiness"], outcome["waiting"]

        record = self._require_workflow()
        if readiness["ready"]:
            record = turbo.set_stage(
                record, "tts", turbo.COMPLETED,
                detail=f"{readiness['chunk_count']} parca hazir")
        elif waiting:
            record = turbo.set_stage(
                record, "tts", waiting["state"], resume_at=waiting["resume_at"],
                detail=(f"{readiness['completed_count']}/{readiness['chunk_count']}"
                        f" parca; {waiting['reason']}"))
            self.log_event("turbo.stage_waiting", stage="tts",
                           wait_state=waiting["state"],
                           resume_at=waiting.get("resume_at"),
                           completed_count=readiness["completed_count"],
                           chunk_count=readiness["chunk_count"])
        elif outcome["failed"]:
            record = turbo.set_stage(
                record, "tts", turbo.FAILED,
                error=f"{outcome['failed']} parca basarisiz", attempt_delta=1)
        else:
            record = turbo.set_stage(
                record, "tts", turbo.RUNNING,
                detail=(f"{readiness['completed_count']}/"
                        f"{readiness['chunk_count']} parca"))
        self._save_workflow(record, actor=actor)
        return outcome

    def concat_audio(self, *, actor: str = "console") -> dict:
        """Join the chunks in order. Refuses on a gap rather than hiding one."""
        manifest = self.chunk_manifest()
        ordered = tts_chunks.ordered_ready(manifest)
        readiness = tts_chunks.concat_readiness(manifest)
        if not ordered or not readiness["ready"]:
            self.log_event("audio_concat.failed",
                           missing_count=len(readiness["missing"]))
            raise TurboError(
                f"{len(readiness['missing'])} parca eksik; birlestirme yapilmadi")
        self.log_event("audio_concat.started", chunk_count=len(ordered))
        started = time.time()
        try:
            combined = tts_runner.concat_wav_chunks(
                ordered, self._path(turbo.NARRATION_PATH))
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            self._record_stage_failure("audio_concat", exc, None, actor=actor,
                                       started=started, event="audio_concat.failed")
            raise
        combined["relative_path"] = self.storage.relative_to_namespace(
            self.ownership, Path(combined["path"]))
        # The measured duration, not the brief's estimate, is what the timeline
        # will be packed against.
        target = float((self.brief() or {}).get("duration_sec") or 0)
        combined["target_duration_sec"] = target
        combined["drift_sec"] = round(combined["duration_sec"] - target, 2)
        manifest["combined"] = combined
        manifest["status"] = "review_required"
        self._write(turbo.CHUNK_ARTIFACT, manifest, actor=actor)
        self.log_event("audio_concat.completed", chunk_count=len(ordered),
                       duration_sec=combined["duration_sec"],
                       drift_sec=combined["drift_sec"])
        if not combined["validation"].get("passed"):
            self._save_workflow(turbo.set_stage(
                self._require_workflow(), "audio_concat", turbo.FAILED,
                error="birlesik ses dogrulamayi gecmedi"), actor=actor)
            raise TurboError("birlesik ses dogrulamayi gecmedi: "
                             + str(combined["validation"].get("reason")))
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "audio_concat", turbo.COMPLETED,
            detail=f"{combined['duration_sec']} sn anlatim"), actor=actor)
        return combined

    def run_next_stage(self, *, actor: str = "console", tracker=None,
                       cancel_check=None, max_chunks: int = 0) -> dict:
        """Run whatever is runnable now, or explain why nothing is."""
        record = self._require_workflow()
        decision = turbo.runnable_stage(record)
        if decision.get("blocked") or not decision.get("stage"):
            return {"ran": None, "decision": decision}
        stage = decision["stage"]
        runners = {
            "script": lambda: self.run_script(actor=actor),
            "tts": lambda: self.run_tts(actor=actor, tracker=tracker,
                                        cancel_check=cancel_check,
                                        max_chunks=max_chunks),
            "audio_concat": lambda: self.concat_audio(actor=actor),
            "media_plan": lambda: self.run_media_plan(actor=actor),
            "subtitles": lambda: self.run_subtitles(actor=actor),
            "timeline": lambda: self.run_timeline(actor=actor),
            "preview_render": lambda: self.render(preset="preview",
                                                  actor=actor,
                                                  tracker=tracker),
        }
        runner = runners.get(stage)
        if runner is None:
            return {"ran": None, "decision": {
                "stage": stage, "blocked": True,
                "reason": (f"{turbo.STAGE_LABELS[stage]} calistiricisi henuz "
                           "baglanmadi")}}
        runner()
        return {"ran": stage,
                "decision": turbo.runnable_stage(self._require_workflow())}

    # ------------------------------------------------------- stock material
    #: Turbo keeps its plan and clips under `results/turbo/`, long-form under
    #: `results/long_form/`. One project can run both without either touching
    #: the other's files.
    MEDIA_KIND = "turbo"

    def media_plan(self):
        return self._read(stock_media.plan_artifact(self.MEDIA_KIND))

    def _orientation(self) -> str:
        """What shape of source footage actually suits the target frame.

        Asking Pexels for landscape and then centre-cropping it into 1080x1920
        discards 44% of every frame. Asking for portrait when the output is
        portrait is the difference between a usable clip and a salvaged one.
        """
        return {"9:16": "portrait", "1:1": "square"}.get(
            str((self.brief() or {}).get("aspect_ratio") or "9:16"), "landscape")

    def _script_digest(self) -> str:
        script = self.script()
        if not script:
            raise TurboError("once scripti uretin")
        return script["script_sha256"]

    def _fresh_plan(self):
        """The plan, or a refusal naming what changed under it."""
        plan = self.media_plan()
        if not plan:
            raise TurboError("once klip slotlarini olusturun")
        if stock_media.is_stale_against(plan, self._script_digest()):
            raise TurboError("script degisti; once klip slotlarini yenileyin")
        return plan

    def run_media_plan(self, *, actor: str = "console") -> dict:
        """One search slot per scene beat. Creating it calls no provider.

        Reuses whatever the previous plan already found: a slot whose beat text
        and query are unchanged keeps its candidates, its selection and its
        downloaded file, because re-fetching footage that is still correct
        spends bandwidth and provider quota for nothing.
        """
        script = self.script()
        if not script:
            raise TurboError("once scripti uretin")
        brief = self.brief() or {}
        previous = {slot.get("id"): slot
                    for slot in ((self.media_plan() or {}).get("slots") or [])}

        slots, reused = [], 0
        anchor_slot_id = None
        if brief.get("use_source_moment"):
            # The project's own approved moment leads. It is not a stock search
            # result, so it never enters the candidate/selection dance -- it is
            # extracted from the source the operator already chose and reviewed,
            # and arrives already "downloaded".
            anchor = self._source_moment_slot(actor=actor)
            slots.append(anchor)
            anchor_slot_id = anchor["id"]

        for beat in script["scene_beats"]:
            if anchor_slot_id is not None and beat["index"] == 1:
                # Beat 1 is spoken over the source moment itself, so it needs no
                # stock footage of its own.
                continue
            slot_id = beat["slot_id"]
            fresh = {
                "id": slot_id, "sequence": beat["index"],
                "chapter_index": beat["index"],
                "chapter_title": beat["search_query"],
                "script_excerpt": beat["text"][:260],
                "query": beat["search_query"],
                "status": "planned", "provider": None,
                "candidates": [], "selected": None, "download": None,
                "searched_at": None,
            }
            old = previous.get(slot_id)
            if (old and old.get("query") == fresh["query"]
                    and old.get("script_excerpt") == fresh["script_excerpt"]):
                fresh.update({
                    "status": old.get("status") or "planned",
                    "provider": old.get("provider"),
                    "candidates": old.get("candidates") or [],
                    "selected": old.get("selected"),
                    "download": old.get("download"),
                    "searched_at": old.get("searched_at"),
                })
                if old.get("download"):
                    reused += 1
            slots.append(fresh)

        record = stock_media.new_plan_from_slots(
            slots, clip_duration_sec=brief.get("clip_duration_sec") or 5,
            source_sha256=script["script_sha256"],
            artifact_type="turbo_media_plan",
            aspect_ratio=brief.get("aspect_ratio") or "9:16",
            ownership=self.ownership)
        record["orientation"] = self._orientation()
        self._write(stock_media.plan_artifact(self.MEDIA_KIND), record,
                    actor=actor)
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "media_plan", turbo.COMPLETED,
            detail=(f"{len(slots)} slot"
                    + (f", {reused} indirilmis klip korundu" if reused else ""))),
            actor=actor)
        self.log_event("media_plan.created", slot_count=len(slots),
                       reused_downloads=reused,
                       orientation=record["orientation"])
        return record

    def search_slot(self, slot_id: str, *, provider: str, query: str = "",
                    per_page: int = 6, actor: str = "console") -> dict:
        """One explicit stock API call. Credentials never leave the server."""
        plan = self._fresh_plan()
        kind = str(provider or "").strip().lower()
        if kind not in stock_media.PROVIDERS:
            raise TurboError("stok video saglayicisi Pexels veya Pixabay olmali")
        credential = self.service.secrets.credential(kind)
        if not credential.configured:
            raise TurboError(
                f"{kind} API anahtari tanimli degil; .env.local dosyasini guncelle")
        slots = {slot["id"]: slot for slot in plan.get("slots") or []}
        if slot_id not in slots:
            raise TurboError(f"bilinmeyen slot: {slot_id!r}")
        result = stock_media.search_videos(
            kind, query or slots[slot_id].get("query") or "",
            credential.value, per_page=per_page,
            orientation=plan.get("orientation") or self._orientation())
        plan = stock_media.store_search(plan, slot_id, result)
        self._write(stock_media.plan_artifact(self.MEDIA_KIND), plan, actor=actor)
        self.log_event("media.search_completed", slot_id=slot_id, provider=kind,
                       candidate_count=result.get("count"),
                       orientation=plan.get("orientation"))
        return plan

    def select_clip(self, slot_id: str, candidate_id: str, *,
                    actor: str = "console") -> dict:
        """Choosing is its own action: nothing downloads until it is asked for."""
        plan = self._fresh_plan()
        plan = stock_media.select_candidate(plan, slot_id, candidate_id)
        self._write(stock_media.plan_artifact(self.MEDIA_KIND), plan, actor=actor)
        self.log_event("media.selected", slot_id=slot_id,
                       candidate_id=candidate_id)
        return plan

    SOURCE_MOMENT_SLOT_ID = "src_moment"

    def _source_moment_slot(self, *, actor: str = "console") -> dict:
        """Cut this project's approved moment into Turbo's clip directory.

        Cut from the ORIGINAL source at the moment's own timestamps, not from
        the low-resolution preview: the preview exists so a human can watch
        candidates quickly, and burning it into a delivered video would ship
        that compromise.

        Returned already `downloaded`, with provenance saying it came from the
        user's own footage. `record_download` refuses a slot with no selected
        candidate, so this builds the slot directly rather than pretending a
        stock provider returned it.
        """
        selection = self.service.load_artifact("moment_selection")
        if selection["problem"] or not selection["data"]:
            raise TurboError(
                "Once Moment Explorer'da bir moment sec; kaynak momenti "
                "kullanmak icin onaylanmis bir secim gerekir.")
        hot = self.service.load_artifact("hot_moments", required=True)
        if hot["problem"]:
            raise TurboError(hot["problem"])

        moment = (selection["data"] or {}).get("selected_moment") or {}
        start = float(moment.get("source_start_sec")
                      or moment.get("aligned_start_sec") or 0.0)
        end = float(moment.get("source_end_sec")
                    or moment.get("aligned_end_sec") or 0.0)
        span = round(max(0.5, end - start), 3)

        destination = self._path(
            f"{stock_media.clip_dirname(self.MEDIA_KIND)}/source_moment.mp4")
        destination.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_bin, _ffprobe = render_utils.resolve_ffmpeg_binaries()
        if not ffmpeg_bin:
            raise TurboError("ffmpeg bulunamadi")
        render_utils.run_ffmpeg(ffmpeg_bin, [
            "-ss", str(start), "-i", str(self.service.source_path()),
            "-t", str(span), "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-y", str(destination)])
        info, _probe_note = render_utils.probe_media(
            *render_utils.resolve_ffmpeg_binaries(), destination)

        relative = self.storage.relative_to_namespace(self.ownership, destination)
        self.log_event("media.source_moment_extracted",
                       slot_id=self.SOURCE_MOMENT_SLOT_ID,
                       moment_rank=moment.get("rank"), duration_sec=span)
        return {
            "id": self.SOURCE_MOMENT_SLOT_ID, "sequence": 0,
            "chapter_index": 0,
            "chapter_title": "Kaynak moment",
            "script_excerpt": str(moment.get("reason") or "")[:260],
            "query": "", "status": "downloaded",
            "provider": "source_moment", "candidates": [],
            "selected": {
                "id": self.SOURCE_MOMENT_SLOT_ID,
                "provider": "source_moment",
                "asset_id": str(moment.get("rank")),
                "query": "", "source_page": None,
                "creator": {"name": None, "profile_page": None},
                "duration_sec": span,
                "width": info.get("width"), "height": info.get("height"),
                "bytes": destination.stat().st_size, "quality": "source",
                "preview_url": None, "download_url": None,
                # No attribution is invented for the operator's own footage; the
                # rights question for it was answered at the Phase 0 gate.
                "attribution": "Kullanicinin kendi kaynak videosu",
                "license_note": "kaynak video: proje sahibi tarafindan saglandi",
            },
            "download": {
                "relative_path": relative,
                "sha256": render_utils.sha256_file(destination),
                "bytes": destination.stat().st_size,
                "downloaded_at": _now(),
                "provider": "source_moment",
            },
            "searched_at": None,
            "source_moment_rank": moment.get("rank"),
        }

    def download_clip(self, slot_id: str, *, actor: str = "console") -> dict:
        """Fetch the chosen clip, atomically, with provenance preserved."""
        plan = self._fresh_plan()
        slots = {slot["id"]: slot for slot in plan.get("slots") or []}
        slot = slots.get(slot_id)
        if slot is None:
            raise TurboError(f"bilinmeyen slot: {slot_id!r}")
        candidate = slot.get("selected")
        if not candidate:
            raise TurboError("once bu slot icin bir aday secin")
        filename = stock_media.safe_filename(candidate)
        destination = self._path(
            f"{stock_media.clip_dirname(self.MEDIA_KIND)}/{filename}")
        downloaded = stock_media.download_candidate(candidate, destination)
        plan = stock_media.record_download(plan, slot_id, downloaded)
        self._write(stock_media.plan_artifact(self.MEDIA_KIND), plan, actor=actor)
        self.log_event("media.download_completed", slot_id=slot_id,
                       provider=candidate.get("provider"),
                       bytes=downloaded.get("bytes"))
        return plan

    def media_readiness(self) -> dict:
        """How much footage is actually on disk, and what is still missing."""
        plan = self.media_plan()
        slots = (plan or {}).get("slots") or []
        downloaded = [slot for slot in slots if slot.get("download")]
        return {
            "slot_count": len(slots),
            "downloaded_count": len(downloaded),
            "missing": [slot["id"] for slot in slots if not slot.get("download")],
            "ready": bool(slots) and len(downloaded) == len(slots),
            "stale": bool(plan) and bool(self.script())
                     and stock_media.is_stale_against(plan, self.script()["script_sha256"]),
            "attribution_lines": [
                (slot.get("selected") or {}).get("attribution")
                for slot in downloaded
                if (slot.get("selected") or {}).get("attribution")],
        }

    # ------------------------------------------------------------ subtitles
    def run_subtitles(self, *, actor: str = "console") -> dict:
        """Align cues against the MEASURED narration, not the script.

        `transcribe_for_subtitles` never raises: on a missing dependency or bad
        audio it silently degrades to splitting the text proportionally. That is
        the right behaviour -- captions are better than none -- but the manifest
        and the UI must SAY which happened, otherwise an operator sees badly
        timed captions and blames the timeline.
        """
        brief = self.brief() or {}
        combined = (self.chunk_manifest() or {}).get("combined") or {}
        if not combined.get("path"):
            raise TurboError("once anlatimi birlestirin")
        if not brief.get("subtitles_enabled", True):
            self._save_workflow(turbo.set_stage(
                self._require_workflow(), "subtitles", turbo.COMPLETED,
                detail="altyazi kapali (acik karar)"), actor=actor)
            return {"enabled": False,
                    "reason": "brief altyaziyi kapatiyor"}

        script = self.script() or {}
        # Word timestamps come from the SAME canonical helper the main render
        # path uses, so Turbo gets the active-word highlight instead of a second,
        # plainer caption implementation. It degrades to cues on its own.
        words, cues, source = render_utils.transcribe_for_dynamic_captions(
            combined["path"], fallback_text=script.get("narration") or "",
            max_chars_per_cue=42, whisper_model_size="small",
            language=(brief.get("language") or "tr"))
        degraded = "fallback" in str(source).lower()

        srt_path = self._path(turbo.SUBTITLE_PATH)
        ass_path = self._path(turbo.SUBTITLE_ASS_PATH)
        render_utils.write_srt(cues, srt_path)

        theme = str(brief.get("caption_theme") or "default")
        profile = render_utils.CAPTION_THEME_PROFILES.get(
            theme, render_utils.CAPTION_THEME_PROFILES["default"])
        caption_meta = None
        if words:
            caption_meta = render_utils.write_active_word_ass(
                words, ass_path,
                play_res_x=int(brief.get("width") or 1080),
                play_res_y=int(brief.get("height") or 1920),
                font_name=profile["font_name"], font_size=profile["font_size"],
                margin_v=profile["margin_v"], max_words=profile["max_words"],
                max_chars=profile["max_chars"],
                active_color_ass=profile["active_color_ass"],
                base_color_ass=profile["base_color_ass"],
                uppercase=bool(profile.get("uppercase", False)),
                letter_spacing=float(profile.get("letter_spacing", 0.0)))
            path = ass_path
        else:
            # No word alignment: burn the SRT and delete any ASS from a previous
            # successful run, so the renderer cannot pick up stale captions.
            ass_path.unlink(missing_ok=True)
            path = srt_path

        record = {
            "enabled": True,
            "relative_path": self.storage.relative_to_namespace(
                self.ownership, path),
            "format": "ass" if words else "srt",
            "sha256": tts_chunks.sha256_file(path),
            "cue_count": len(cues),
            "word_count": len(words),
            "dynamic_word_highlight": bool(words),
            "source": source,
            "degraded": degraded,
            "caption_theme": theme,
            "caption_meta": caption_meta,
            # ASS carries its own [V4+ Styles]; force_style would override the
            # theme, so it is only meaningful for the SRT path.
            "style": ({} if words else {
                "font_name": profile["font_name"],
                "font_size": int(profile["font_size"] * 0.6),
                "margin_v": profile["margin_v"],
                "border_style": 3, "outline": 2}),
        }
        if degraded:
            self.log_event("subtitles.degraded", cue_count=len(cues),
                           source=str(source))
        else:
            self.log_event("subtitles.completed", cue_count=len(cues),
                           word_count=len(words), source=str(source))
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "subtitles", turbo.COMPLETED,
            detail=(f"{len(cues)} cue"
                    + (" -- ORANSAL TAHMIN, hizalama yapilamadi" if degraded
                       else (" (aktif kelime vurgulu)" if words
                             else " (ses hizalamali)")))), actor=actor)
        return record

    def subtitles(self):
        """The file that must actually be burned.

        ASS wins when it exists, because that is the run that achieved word
        alignment; the SRT is kept only as the degraded-path artifact.
        """
        ass_path = self._path(turbo.SUBTITLE_ASS_PATH)
        if ass_path.is_file():
            return ass_path
        path = self._path(turbo.SUBTITLE_PATH)
        return path if path.is_file() else None

    # ---------------------------------------------------------------- music
    def music_choices(self) -> dict:
        """What is available to pick, with each track's licence status.

        Unverified tracks are listed rather than hidden -- the operator may own
        music whose paperwork lives elsewhere -- but never as "safe".
        """
        library = music_library.scan_library(
            getattr(self.service, "music_root", None))
        uploaded = []
        folder = self._path(MUSIC_DIRNAME)
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.suffix.lower() in music_library.AUDIO_SUFFIXES:
                    try:
                        uploaded.append(music_library.describe_track(
                            path, origin="project_upload"))
                    except music_library.MusicLibraryError:
                        continue
        return {"library": library, "uploaded": uploaded,
                "policy": ("Bu proje hicbir parcayi telifsiz olarak nitelemez. "
                           "Lisans kaydi eksik olan parcalar isaretlenir.")}

    def choose_music(self, *, origin: str, filename: str = "",
                     volume: float = 0.18, ducking: bool = True,
                     actor: str = "operator") -> dict:
        """Record the music decision, including the decision to have none."""
        record = self._require_workflow()
        if origin == "none":
            self._write_music(music_library.absent(), actor=actor)
            self._save_workflow(turbo.set_stage(
                record, "music", turbo.COMPLETED,
                detail="muzik yok (acik karar)"), actor=actor)
            self.log_event("music.removed")
            return music_library.absent()

        if origin == "project_upload":
            path = self._path(f"{MUSIC_DIRNAME}/{filename}")
        elif origin == "global_library":
            root = getattr(self.service, "music_root", None)
            if not root:
                raise TurboError(
                    "muzik kutuphanesi tanimli degil; CREATOR_CONSOLE_MUSIC_ROOT "
                    "ayarlayin veya projeye dosya yukleyin")
            path = Path(root) / filename
        else:
            raise TurboError("muzik kaynagi 'global_library', 'project_upload' "
                             "veya 'none' olmali")

        track = music_library.describe_track(path, origin=origin,
                                             display_name=filename)
        probe = google_ai.validate_audio_file(path)
        entry = music_library.manifest_entry(
            track,
            relative_path=(self.storage.relative_to_namespace(self.ownership, path)
                           if origin == "project_upload" else f"library:{filename}"),
            sha256=tts_chunks.sha256_file(path),
            duration_sec=(probe.get("details") or {}).get("duration_sec") or 0.0,
            volume=volume, ducking=ducking)
        self._write_music(entry, actor=actor)
        detail = track["display_name"] + (
            " -- LISANS DOGRULANMADI" if track["needs_review"] else "")
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "music", turbo.COMPLETED, detail=detail),
            actor=actor)
        self.log_event("music.selected", origin=origin,
                       license_status=track["license"]["status"],
                       needs_review=track["needs_review"])
        if track["needs_review"]:
            self.log_event("music.license_unverified",
                           missing=track["license"].get("missing"))
        return entry

    def _write_music(self, entry: dict, *, actor: str) -> None:
        self._write("results/turbo/music.json",
                    {"artifact_type": "turbo_music", "status": "recorded",
                     "music": entry, "not_uploaded": True}, actor=actor)

    def music(self):
        return (self._read("results/turbo/music.json") or {}).get("music")

    #: A music upload is small by nature; this cap exists so a mistaken video
    #: file cannot fill the project namespace.
    MAX_MUSIC_BYTES = 64 * 1024 * 1024

    def upload_music(self, *, filename: str, stream=None, payload=None,
                     content_length: int = 0, actor: str = "operator") -> dict:
        """Store one operator-supplied track inside the project.

        Streamed to a `.part` file and renamed, so an interrupted upload never
        leaves something that looks like a usable track. The extension is
        checked BEFORE any byte is written.
        """
        name = music_library.Path(str(filename or "")).name
        suffix = music_library.Path(name).suffix.lower()
        if suffix not in music_library.AUDIO_SUFFIXES:
            raise TurboError(
                "desteklenmeyen ses bicimi: {0}; kabul edilenler: {1}".format(
                    suffix or "(uzantisiz)",
                    ", ".join(sorted(music_library.AUDIO_SUFFIXES))))
        if content_length and int(content_length) > self.MAX_MUSIC_BYTES:
            raise TurboError(
                "muzik dosyasi {0} MB sinirini asiyor".format(
                    self.MAX_MUSIC_BYTES // (1024 * 1024)))

        target = self._path(f"{MUSIC_DIRNAME}/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            with open(temp, "wb") as handle:
                if stream is not None:
                    remaining = int(content_length or 0)
                    while remaining > 0:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            break
                        written += len(block)
                        if written > self.MAX_MUSIC_BYTES:
                            raise TurboError("muzik dosyasi boyut sinirini asti")
                        handle.write(block)
                        remaining -= len(block)
                else:
                    data = payload or b""
                    if len(data) > self.MAX_MUSIC_BYTES:
                        raise TurboError("muzik dosyasi boyut sinirini asti")
                    written = len(data)
                    handle.write(data)
            if written <= 0:
                raise TurboError("bos dosya yuklendi")
            temp.replace(target)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        track = music_library.describe_track(target, origin="project_upload",
                                             display_name=name)
        self.log_event("music.selected", origin="project_upload",
                       license_status=track["license"]["status"],
                       needs_review=track["needs_review"], bytes=written)
        if track["needs_review"]:
            self.log_event("music.license_unverified",
                           missing=track["license"].get("missing"))
        return track

    # ------------------------------------------------------------- timeline
    def timeline_manifest(self):
        return self._read(turbo.TIMELINE_ARTIFACT)

    def run_timeline(self, *, actor: str = "console") -> dict:
        """Pack the downloaded clips against the MEASURED narration duration."""
        brief = self.brief() or {}
        script = self.script() or {}
        combined = (self.chunk_manifest() or {}).get("combined") or {}
        if not combined.get("duration_sec"):
            raise TurboError("once anlatimi birlestirin; sure olculmeden "
                             "zaman cizelgesi paketlenemez")
        readiness = self.media_readiness()
        if not readiness["ready"]:
            raise TurboError(
                f"{len(readiness['missing'])} slot icin klip indirilmedi")

        plan = self._fresh_plan()
        sources = {}
        for slot in plan["slots"]:
            download = slot.get("download") or {}
            selected = slot.get("selected") or {}
            path = self._path(download["relative_path"]) if download.get(
                "relative_path") else Path(download.get("path") or "")
            info, _backend = render_utils.probe_media(
                *render_utils.resolve_ffmpeg_binaries(), path)
            sources[slot["id"]] = {
                "relative_path": download.get("relative_path")
                                 or str(Path(download.get("path") or "").name),
                "sha256": download.get("sha256"),
                "provider": selected.get("provider"),
                "asset_id": selected.get("asset_id"),
                "source_page": selected.get("source_page"),
                "creator": selected.get("creator"),
                "attribution": selected.get("attribution"),
                "license_note": selected.get("license_note"),
                "probed": {"duration_sec": info.get("duration_sec"),
                           "width": info.get("width"),
                           "height": info.get("height")},
            }

        mode = brief.get("ordering") or "sequential"
        seed = (timeline.resolve_seed(
            project_id=self.ownership.project_id,
            script_sha256=script.get("script_sha256") or "",
            seed_input=brief.get("seed_input") or "")
            if mode == "seeded_random" else None)
        order = timeline.order_clip_ids(sources, mode=mode, seed=seed)
        clips = timeline.pack_clips(
            sources, total_duration_sec=combined["duration_sec"],
            clip_duration_sec=brief.get("clip_duration_sec") or 5.0,
            fps=brief.get("fps") or 30, order=order,
            frame_defaults={"mode": brief.get("fit_mode") or "crop",
                            "zoom": 1.0, "focus_x": 0.5, "focus_y": 0.5,
                            "blur_sigma": 20},
            transition=brief.get("transition") or "none",
            transition_sec=brief.get("transition_sec") or 0.0)

        subtitle_path = self.subtitles()
        subtitles = {"enabled": False}
        if subtitle_path is not None and brief.get("subtitles_enabled", True):
            subtitles = {
                "enabled": True,
                "relative_path": self.storage.relative_to_namespace(
                    self.ownership, subtitle_path),
                "format": "srt",
                "sha256": tts_chunks.sha256_file(subtitle_path),
                "style": {"font_size": 42, "margin_v": 120, "border_style": 3,
                          "outline": 2},
            }

        manifest = timeline.new_manifest(
            clips=clips,
            narration={"relative_path": combined.get("relative_path"),
                       "sha256": combined.get("sha256"),
                       "duration_sec": combined["duration_sec"]},
            video={"width": brief.get("width") or 1080,
                   "height": brief.get("height") or 1920,
                   "fps": brief.get("fps") or 30,
                   "aspect_ratio": brief.get("aspect_ratio") or "9:16"},
            audio={"narration_volume": 1.0,
                   "music": self.music() or {"present": False},
                   "ducking": {"enabled": True, "duck_ratio": 8.0}},
            ordering={"mode": mode, "seed": seed,
                      "seed_input": brief.get("seed_input") or "",
                      "clip_id_order": order},
            subtitles=subtitles,
            settings_snapshot={"brief_sha256": brief.get("brief_sha256"),
                               "script_sha256": script.get("script_sha256"),
                               "encode": {}})
        manifest.update(self.ownership.to_dict())
        self._write(turbo.TIMELINE_ARTIFACT, manifest, actor=actor)
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), "timeline", turbo.COMPLETED,
            detail=(f"{len(clips)} klip, {manifest['duration_sec']} sn"
                    + (f", {manifest['reuse_count']} tekrar"
                       if manifest["reuse_count"] else ""))), actor=actor)
        self.log_event("timeline.completed", clip_count=len(clips),
                       duration_sec=manifest["duration_sec"],
                       reuse_count=manifest["reuse_count"],
                       ordering=mode, seed=seed,
                       render_fingerprint=manifest["render_fingerprint"])
        return manifest

    # -------------------------------------------------------------- renders
    def render(self, *, preset: str = "preview", actor: str = "console",
               tracker=None) -> dict:
        """Render the timeline. `preview` is cheap and small; `final` is neither.

        They are separate outputs with separate approvals: a preview exists to
        be judged, and nothing here treats approving one as approving the other.
        """
        if preset not in ("preview", "final"):
            raise TurboError("render profili 'preview' veya 'final' olmali")
        stage = f"{preset}_render"
        manifest = self.timeline_manifest()
        if not manifest:
            raise TurboError("once zaman cizelgesini olusturun")
        if preset == "final" and turbo.stage_state(
                self._require_workflow(), "preview_render") != turbo.COMPLETED:
            raise TurboError(
                "final render icin once onizlemeyi uretip izleyin")

        ffmpeg_bin, ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
        if not ffmpeg_bin:
            raise TurboError("ffmpeg bulunamadi")
        subtitle_path = self.subtitles() if (
            manifest.get("subtitles") or {}).get("enabled") else None
        graph = timeline.build_filter_complex(manifest,
                                              subtitle_path=subtitle_path)
        graph_path = timeline.write_graph(
            graph, self._path(f"{GRAPH_DIRNAME}/{preset}.txt"))
        output = self._path(f"{RENDER_DIRNAME}/{preset}.mp4")

        music = manifest.get("audio", {}).get("music") or {}
        music_path = None
        if music.get("present") and music.get("relative_path", "").startswith(
                "results/"):
            music_path = self._path(music["relative_path"])

        args = timeline.build_ffmpeg_args(
            manifest, resolve=lambda clip: self._path(
                f"{stock_media.clip_dirname(self.MEDIA_KIND)}/"
                f"{Path(clip['source']['relative_path']).name}"),
            narration_path=self._path(turbo.NARRATION_PATH),
            output_path=output, graph_path=graph_path, music_path=music_path,
            preset=preset)

        self._save_workflow(turbo.set_stage(
            self._require_workflow(), stage, turbo.RUNNING, attempt_delta=1),
            actor=actor)
        self.log_event("render.started", preset=preset,
                       render_fingerprint=manifest.get("render_fingerprint"))
        started = time.time()
        try:
            render_utils.run_ffmpeg(
                ffmpeg_bin, args, progress=tracker,
                total_duration_sec=manifest.get("duration_sec"))
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            self._record_stage_failure(stage, exc, None, actor=actor,
                                       started=started, event="render.failed")
            raise
        scale = 0.5 if preset == "preview" else 1.0
        validation = render_utils.validate_render(
            ffmpeg_bin, ffprobe_bin, output,
            max(2, int(manifest["video"]["width"] * scale) // 2 * 2),
            max(2, int(manifest["video"]["height"] * scale) // 2 * 2))
        record = {
            "preset": preset,
            "relative_path": self.storage.relative_to_namespace(
                self.ownership, output),
            "sha256": tts_chunks.sha256_file(output),
            "render_fingerprint": manifest.get("render_fingerprint"),
            "validation": validation,
            "attribution_lines": manifest.get("attribution_lines") or [],
            "music_license": (music.get("license") or {}).get("status")
                             if music.get("present") else None,
            "not_uploaded": True,
            "latency_sec": round(time.time() - started, 2),
        }
        self._write(f"results/turbo/{preset}_render.json",
                    {"artifact_type": f"turbo_{preset}_render",
                     "status": "review_required", **record}, actor=actor)
        self._save_workflow(turbo.set_stage(
            self._require_workflow(), stage, turbo.COMPLETED,
            detail=f"{preset} hazir, izlenmeyi bekliyor"), actor=actor)
        self.log_event("render.completed", preset=preset,
                       latency_sec=record["latency_sec"],
                       passed=validation.get("passed"))
        return record

    # ------------------------------------------------------------ revisions
    def apply_revision(self, kind: str, *, target: str = "", values: dict | None = None,
                       actor: str = "operator") -> dict:
        """Apply a revision and invalidate exactly what it actually affects.

        Nothing is deleted. A superseded render is moved into the project's
        revision archive by the same mechanism the rest of the console uses, so
        an operator who changes their mind can still see what the previous
        version looked like.
        """
        effect = turbo.revision_effect(kind)
        values = dict(values or {})
        record = self._require_workflow()
        manifest = self.timeline_manifest()

        if kind == "subtitle_style":
            # Writes the BRIEF, not the manifest: the theme decides how the ASS
            # is generated, so the subtitles stage has to run again. Validated
            # here so an unknown theme is refused before anything is thrown away.
            theme = str(values.get("caption_theme") or "").strip()
            if theme not in render_utils.CAPTION_THEME_PROFILES:
                raise TurboError(
                    "altyazi temasi su seceneklerden biri olmali: "
                    + ", ".join(sorted(render_utils.CAPTION_THEME_PROFILES)))
            brief = dict(self.brief() or {})
            brief["caption_theme"] = theme
            self._write(turbo.BRIEF_ARTIFACT, turbo.validate_brief(brief),
                        actor=actor)

        if effect["manifest_only"]:
            if manifest is None:
                raise TurboError("once zaman cizelgesini olusturun")
            manifest = self._revise_manifest(kind, manifest, target, values)
            self._write(turbo.TIMELINE_ARTIFACT, manifest, actor=actor)

        # Supersede before resetting, so the artifacts still exist to move.
        archived = self._archive_stale_outputs(effect["stale_stages"], actor=actor)
        record = turbo.invalidate_stages(
            record, effect["stale_stages"],
            reason=f"revizyon: {effect['label']}")
        self._save_workflow(record, actor=actor)
        self.log_event("turbo.revision_applied", kind=kind, target=target,
                       manifest_only=effect["manifest_only"],
                       stale_stages=list(effect["stale_stages"]),
                       archived_count=len(archived))
        return {**effect, "archived": archived,
                "render_fingerprint": (manifest or {}).get("render_fingerprint")}

    def _revise_manifest(self, kind: str, manifest: dict, target: str,
                         values: dict) -> dict:
        if kind == "frame":
            return timeline.set_frame(
                manifest, target, mode=values.get("mode"),
                zoom=values.get("zoom"), focus_x=values.get("focus_x"),
                focus_y=values.get("focus_y"),
                blur_sigma=values.get("blur_sigma"))
        if kind == "retime":
            return timeline.retime_clip(
                manifest, target, float(values.get("seconds") or 0.0),
                policy=str(values.get("policy") or "spread_forward"))
        if kind in ("music_volume", "music_remove"):
            manifest = dict(manifest)
            audio = dict(manifest.get("audio") or {})
            if kind == "music_remove":
                # The `music` STAGE stays completed: removing music is an
                # answered question, and resetting it would re-block the
                # workflow on a decision the operator already made.
                audio["music"] = music_library.absent()
                self._write_music(music_library.absent(), actor="operator")
                self.log_event("music.removed")
            else:
                music = dict(audio.get("music") or {})
                if not music.get("present"):
                    raise TurboError("bu projede muzik yok")
                volume = float(values.get("volume", music.get("volume") or 0.18))
                if not (0.0 <= volume <= 1.0):
                    raise TurboError("muzik ses duzeyi 0.0 ile 1.0 arasinda olmali")
                music["volume"] = round(volume, 3)
                audio["music"] = music
            manifest["audio"] = audio
            manifest["render_fingerprint"] = timeline.render_fingerprint(manifest)
            return manifest
        raise TurboError(f"manifest ici uygulanamayan revizyon: {kind!r}")

    def _archive_stale_outputs(self, stale, *, actor: str) -> list:
        """Move superseded outputs aside instead of overwriting them."""
        paths = []
        for stage in stale:
            if stage in turbo.RENDER_STAGES:
                preset = stage.replace("_render", "")
                for relative in (f"{RENDER_DIRNAME}/{preset}.mp4",
                                 f"results/turbo/{preset}_render.json"):
                    if self._path(relative).is_file():
                        paths.append(relative)
        if not paths:
            return []
        # `_archive_revision` returns the revision MANIFEST, not a path list.
        # Reading it as a list counted the manifest's keys and reported "13
        # files" for four.
        manifest = self.service._archive_revision(
            paths, action="turbo_revision", actor=actor) or {}
        return list(manifest.get("moved_paths") or [])

    # -------------------------------------------------------------- archive
    def archive_readiness(self) -> dict:
        """What archiving would move, and whether it is safe to.

        The byte count is shown BEFORE confirming because a Turbo project
        carries every downloaded clip, and that is not obvious from the screen.
        """
        record = self.workflow()
        total = 0
        clip_bytes = 0
        root = self._path(turbo.ARTIFACT_ROOT)
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    size = path.stat().st_size
                    total += size
                    if stock_media.clip_dirname(self.MEDIA_KIND) in str(
                            path).replace("\\", "/"):
                        clip_bytes += size
        final_done = (record is not None
                      and turbo.stage_state(record, "final_render")
                      == turbo.COMPLETED)
        return {
            "total_bytes": total,
            "clip_bytes": clip_bytes,
            "final_render_completed": final_done,
            "ready": final_done,
            "reason": ("" if final_done else
                       "final render tamamlanmadan arsivlemek yarim is birakir"),
            # Turbo does NOT archive. `ConsoleApp.archive_project` already moves
            # a whole project namespace, records a manifest and requires a typed
            # confirmation -- and it has its own suite. Reimplementing a
            # destructive action's confirmation flow here is exactly where a
            # second copy goes wrong, so this only reports readiness and says
            # where the real control is.
            "control": ("Proje arsivleme kontrolu Projeler sekmesindeki "
                        "'Arsivle' dugmesidir; yazili onay ister."),
        }

    # ----------------------------------------------------------------- view
    def view(self) -> dict:
        """Everything the Turbo screen renders. A pure read.

        No stage is started, no provider is contacted and no file is created by
        calling this -- opening a tab must never spend money or quota.
        """
        brief = self.brief()
        record = self.workflow()
        progress = (turbo.progress_summary(record) if record is not None
                    else turbo.progress_summary(turbo.new_workflow()))
        return {
            "started": record is not None,
            "brief": brief,
            "brief_defaults": turbo.new_brief(),
            "options": {
                "aspect_ratios": [
                    {"value": key, "label": "{0} ({1}x{2})".format(
                        key, *turbo.ASPECT_RATIOS[key])}
                    for key in turbo.ASPECT_RATIOS],
                "content_types": list(turbo.CONTENT_TYPES),
                "fit_modes": list(turbo.FIT_MODES),
                "transitions": list(turbo.TRANSITIONS),
                "orderings": list(turbo.ORDERINGS),
                "fps_choices": [24, 25, 30, 60],
                # Straight from the shared theme dictionary, so the picker can
                # never offer a look `validate_brief` would refuse.
                "caption_themes": sorted(render_utils.CAPTION_THEME_PROFILES),
            },
            "limits": {
                "min_duration_sec": turbo.MIN_DURATION_SEC,
                "max_duration_sec": turbo.MAX_DURATION_SEC,
                "min_clip_sec": turbo.MIN_CLIP_SEC,
                "max_clip_sec": turbo.MAX_CLIP_SEC,
                "max_clips": turbo.MAX_CLIPS,
            },
            "progress": progress,
            "revision_kinds": [
                {"kind": kind, "label": spec["label"],
                 "manifest_only": spec["manifest_only"]}
                for kind, spec in turbo.REVISION_KINDS.items()],
            "events": self.events(limit=40),
            "script": self._script_view(),
            "media": self._media_view(),
            "music": self._music_view(),
            "timeline": self._timeline_view(),
            "renders": self._render_view(),
            "narration": self._narration_view(),
            "archive": self.archive_readiness() if record is not None else None,
            "quota": self.quota_summary(),
            "not_uploaded": True,
        }

    # Each of these answers ONE question the screen asks, and every one of them
    # only reads. Splitting them keeps `view` legible and keeps a slow or broken
    # section from taking the whole tab down with it.
    def _script_view(self):
        script = self.script()
        if not script:
            return None
        return {
            "hook": script.get("hook"),
            "narration": script.get("narration"),
            "beat_count": script.get("beat_count"),
            "word_count": script.get("word_count"),
            "beat_coverage": script.get("beat_coverage"),
            "coverage_note": script.get("coverage_note"),
            "model": script.get("model"),
            "beats": [{"index": b["index"], "slot_id": b["slot_id"],
                       "text": b["text"], "search_query": b["search_query"]}
                      for b in script.get("scene_beats") or []],
        }

    def _narration_view(self):
        manifest = self._read(turbo.CHUNK_ARTIFACT)
        if not manifest:
            return None
        summary = tts_chunks.summarise(manifest)
        combined = manifest.get("combined") or {}
        return {
            "summary": summary,
            "combined": {k: combined.get(k) for k in (
                "relative_path", "duration_sec", "chunk_count", "drift_sec",
                "target_duration_sec")} if combined else None,
            "chunks": [{"id": c.get("id"), "sequence": c.get("sequence"),
                        "status": c.get("status"), "word_count": c.get("word_count"),
                        "duration_sec": c.get("duration_sec"),
                        "error": c.get("error"), "resume_at": c.get("resume_at")}
                       for c in manifest.get("chunks") or []],
        }

    def _media_view(self):
        plan = self.media_plan()
        if not plan:
            return None
        readiness = self.media_readiness()
        return {
            "orientation": plan.get("orientation"),
            "aspect_ratio": plan.get("aspect_ratio"),
            "clip_duration_sec": plan.get("clip_duration_sec"),
            "policy": plan.get("policy"),
            "readiness": readiness,
            "providers": list(stock_media.PROVIDERS),
            "slots": [{
                "id": slot.get("id"),
                "sequence": slot.get("sequence"),
                "query": slot.get("query"),
                "excerpt": slot.get("script_excerpt"),
                "status": slot.get("status"),
                "provider": slot.get("provider"),
                "searched_at": slot.get("searched_at"),
                "candidates": [{
                    "id": c.get("id"), "provider": c.get("provider"),
                    "width": c.get("width"), "height": c.get("height"),
                    "duration_sec": c.get("duration_sec"),
                    "preview_image": c.get("preview_image"),
                    "source_page": c.get("source_page"),
                    "attribution": c.get("attribution"),
                } for c in slot.get("candidates") or []],
                "selected_id": (slot.get("selected") or {}).get("id"),
                "downloaded": bool(slot.get("download")),
                "download_bytes": (slot.get("download") or {}).get("bytes"),
            } for slot in plan.get("slots") or []],
        }

    def _music_view(self):
        chosen = self.music()
        try:
            choices = self.music_choices()
        except Exception:  # noqa: BLE001 - a bad library must not break the tab
            choices = {"library": {"configured": False, "tracks": []},
                       "uploaded": [], "policy": ""}
        return {"chosen": chosen, **choices}

    def _timeline_view(self):
        manifest = self.timeline_manifest()
        if not manifest:
            return None
        return timeline.summarise(manifest)

    def _render_view(self):
        out = {}
        for preset in ("preview", "final"):
            record = self._read(f"results/turbo/{preset}_render.json")
            if not record:
                out[preset] = None
                continue
            out[preset] = {
                "relative_path": record.get("relative_path"),
                "sha256": record.get("sha256"),
                "render_fingerprint": record.get("render_fingerprint"),
                "passed": (record.get("validation") or {}).get("passed"),
                "latency_sec": record.get("latency_sec"),
                "attribution_lines": record.get("attribution_lines") or [],
                "music_license": record.get("music_license"),
                # A render is stale the moment the manifest it came from
                # changes, and saying so is the difference between watching the
                # current cut and watching a cut that no longer exists.
                "stale": bool(
                    (self.timeline_manifest() or {}).get("render_fingerprint")
                    and record.get("render_fingerprint")
                    != (self.timeline_manifest() or {}).get("render_fingerprint")),
            }
        return out
