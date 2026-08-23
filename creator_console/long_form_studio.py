"""Long-form Video Studio: the service that actually runs the twelve stages.

WHY A SEPARATE MODULE
---------------------
`PipelineService` already carries the Shorts pipeline. Long-form adds twelve
stages, a chunk ledger and a resumable TTS loop; putting that inline would make
one class own two workflows. This wraps the service instead, so it reuses the
same storage, approval, audit, quota and provider-routing abstractions without
enlarging the class that owns short-form behaviour.

WHAT IT GUARANTEES
------------------
  * Every stage is persisted before and after it runs, so a restart resumes at
    the first stage that is not `completed` instead of starting over.
  * A TTS chunk that succeeded is never spoken twice. The chunk ledger is keyed
    by the hash of (text, voice, model, provider, direction, language).
  * Every real provider request goes through its own routed call, so the quota
    ledger counts requests, not jobs.
  * A provider failure is recorded as a failure. Nothing here invents text,
    audio, or a "partial success" that later stages would treat as real.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_pipeline import google_ai, long_form, long_form_script, stock_media, tts_chunks

from . import quota_signals, tts_runner

EVENT_LOG = "results/long_form/events.jsonl"
AUDIO_DIRNAME = "results/long_form/tts"
COMBINED_AUDIO = "results/long_form/narration.wav"
QUOTA_LEDGER = "results/long_form/quota_ledger.json"

#: The event names the operator-facing diagnostics contract promises.
EVENTS = (
    "long_form.started", "long_form.paused", "long_form.resumed",
    "long_form.cancelled", "long_form.completed",
    "research.started", "research.completed", "research.failed",
    "outline.started", "outline.completed", "outline.failed",
    "chapter.started", "chapter.completed", "chapter.failed",
    "consistency_review.started", "consistency_review.completed",
    "consistency_review.failed",
    "tts_chunk.started", "tts_chunk.completed", "tts_chunk.failed",
    "tts_chunk.retried", "tts_chunk.waiting", "tts_chunk.blocked_billing",
    "long_form.waiting_rate_limit", "long_form.waiting_daily_quota",
    "long_form.blocked_billing", "long_form.billing_retry_requested",
    "audio_concat.started", "audio_concat.completed", "audio_concat.failed",
    "timeline.started", "timeline.completed", "timeline.failed",
    "render.started", "render.completed", "render.failed",
    "broll.plan_created", "broll.search_completed", "broll.selected",
    "broll.download_completed",
)

#: Fields that must never reach the event log even if a caller passes them.
_FORBIDDEN_EVENT_FIELDS = frozenset({
    "prompt", "prompts", "text", "script", "api_key", "credential", "token",
    "authorization", "secret", "path", "output_path", "source_path",
})


class LongFormError(ValueError):
    """A rejected long-form request, phrased for the operator."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LongFormStudio:
    """One project's long-form workflow."""

    def __init__(self, service):
        self.service = service
        self.storage = service.storage
        self.ownership = service.ownership

    # -------------------------------------------------------------- storage
    def _path(self, relative: str) -> Path:
        return Path(self.storage.resolve(self.ownership, relative, create_parent=True))

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
        """Append one sanitized, structured event.

        Sanitized by construction rather than by reviewing call sites: the
        forbidden keys are dropped here, so a future caller cannot leak a prompt
        or a credential into the log by passing it along with the metrics.
        """
        if name not in EVENTS:
            raise LongFormError(f"bilinmeyen olay: {name!r}")
        payload = {"event": name, "at": _now(),
                   "project_id": self.ownership.project_id}
        for key, value in (fields or {}).items():
            if key in _FORBIDDEN_EVENT_FIELDS:
                continue
            if isinstance(value, str) and len(value) > 300:
                value = value[:300]
            payload[key] = value
        path = self._path(EVENT_LOG)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def events(self, *, limit: int = 100) -> list:
        path = self._path(EVENT_LOG)
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-int(limit):]
        parsed = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return parsed

    # -------------------------------------------------------------- records
    def plan(self) -> dict:
        return self.service.load_artifact("long_form_plan").get("data") or {}

    def workflow(self) -> dict:
        record = self._read(long_form.WORKFLOW_ARTIFACT)
        if not isinstance(record, dict):
            record = long_form.new_workflow(ownership=self.ownership)
        return record

    def save_workflow(self, record: dict, *, actor: str = "console") -> dict:
        self._write(long_form.WORKFLOW_ARTIFACT, record, actor=actor)
        return record

    def research(self):
        return self._read(long_form_script.RESEARCH_ARTIFACT)

    def outline(self):
        return self._read(long_form_script.OUTLINE_ARTIFACT)

    def chapters(self):
        return self._read(long_form_script.CHAPTERS_ARTIFACT)

    def consistency(self):
        return self._read(long_form_script.CONSISTENCY_ARTIFACT)

    def broll_plan(self):
        return self._read(stock_media.BROLL_ARTIFACT)

    def chunk_manifest(self) -> dict:
        record = self._read(tts_chunks.MANIFEST_ARTIFACT)
        if not isinstance(record, dict):
            record = tts_chunks.new_manifest(ownership=self.ownership)
        return record

    def ledger(self):
        """One PERSISTENT quota ledger for the whole workflow.

        Without a path a `QuotaLedger` lives and dies inside a single call, so
        twenty TTS requests would each start from zero and the project would
        appear to have spent nothing. Binding it to a file is what makes the
        count reflect the requests actually issued.
        """
        return google_ai.QuotaLedger(path=self._path(QUOTA_LEDGER))

    def quota_summary(self) -> dict:
        data = self.ledger().data or {}
        models = data.get("models") or {}
        return {
            "estimated": True,
            # Key names taken from what QuotaLedger actually writes. Guessing
            # them produced a summary that cheerfully reported zero requests
            # while the router was already refusing calls for quota.
            "request_total": sum(int((entry or {}).get("requests_current_day") or 0)
                                 for entry in models.values()),
            "models": {name: {
                "requests_today": (entry or {}).get("requests_current_day"),
                "requests_this_minute": (entry or {}).get("requests_current_minute"),
                "tokens_this_minute": (entry or {}).get(
                    "estimated_input_tokens_current_minute"),
                "successful_calls": (entry or {}).get("successful_calls"),
                "failed_calls": (entry or {}).get("failed_calls"),
            } for name, entry in models.items()},
            "note": ("Yerel TAHMIN; saglayicinin resmi kotasi degildir. Her "
                     "gercek istek ayri sayilir."),
        }

    # ------------------------------------------------------------ estimates
    def estimates(self) -> dict:
        """What a full run would cost, BEFORE it is started."""
        plan = self.plan()
        if not plan:
            return {"available": False,
                    "reason": "once uzun video planini kaydet"}
        duration = plan.get("target_duration_min") or long_form.DEFAULT_DURATION_MIN
        chapters = plan.get("chapter_count") or long_form.CHAPTER_MIN
        words = long_form.word_target(duration)
        return {
            "available": True,
            "word_range": words,
            "chapter_targets": long_form.chapter_targets(duration, chapters),
            "tts_chunk_estimate": long_form.estimate_tts_chunks(words["target"]),
            "api_calls": long_form.estimate_api_calls(chapters, words["target"]),
            "duration_estimate": long_form.duration_estimate_sec(words["target"]),
            "aspect_ratio": long_form.DEFAULT_ASPECT_RATIO,
        }

    def provider_readiness(self) -> dict:
        """Whether the run can start at all, and if not, exactly what is missing."""
        credential = self.service.secrets.credential("google_api")
        blockers = []
        if not credential.configured and not callable(
                getattr(self.service, "_google_client_factory", None)):
            blockers.append("Google API anahtari tanimli degil; arastirma, script "
                            "ve TTS asamalari calistirilamaz.")
        warnings = []
        capacity = self.tts_capacity()
        if capacity["needed"] > capacity["daily_request_capacity"]:
            warnings.append(
                f"Bu anlatim icin ~{capacity['needed']} TTS istegi gerekiyor ama "
                f"yapilandirilmis gunluk TTS kapasitesi {capacity['daily_request_capacity']} "
                f"istek. Uretim tek gecise sigmaz; is akisi kaldigi parcadan "
                f"devam edecek sekilde tasarlandi.")
        if capacity["needed"] > capacity["per_minute_request_capacity"]:
            warnings.append(
                f"Dakika basina sinir ({capacity['binding_limit']}) nedeniyle tek "
                f"gecişte ancak ~{capacity['per_minute_request_capacity']} parca "
                f"uretilebilir; bu anlatim ~{capacity['needed']} parca gerektiriyor. "
                f"Kalanlar sonraki gecislerde tamamlanir -- parca defteri bu yuzden "
                f"yeniden baslatilabilir.")
        return {
            "google_configured": bool(credential.configured),
            "blockers": blockers,
            "warnings": warnings,
            "tts_capacity": capacity,
            "ready": not blockers,
            "note": ("Yedek saglayicilar (ElevenLabs/OpenAI/Piper) acikca "
                     "secilmedikce KULLANILMAZ; ucretli bir yedege sessizce "
                     "gecilmez."),
        }

    def tts_capacity(self) -> dict:
        """What the CONFIGURED limits allow, against what this video needs.

        The selected preview TTS model is currently configured at rpm=3 and
        rpd=10, so an 18 minute narration (~12 requests) needs minute-window
        pauses and may cross the configured daily boundary. The scheduler must
        surface those waits instead of calling them failures or silently moving
        the narration to a different model.
        """
        plan = self.plan()
        words = long_form.word_target(
            plan.get("target_duration_min") or long_form.DEFAULT_DURATION_MIN)
        needed = long_form.estimate_tts_chunks(words["target"])
        # One narration stays on one explicitly selected model. Summing the
        # capacity of fallback models would promise throughput that can only be
        # obtained by changing the voice engine halfway through the video.
        model = plan.get("tts_model") or google_ai.TASK_ROUTES["tts"][0]
        spec = google_ai.MODEL_REGISTRY.get(model) or {}
        rpd = int(spec.get("configured_rpd") or 0)
        rpm = int(spec.get("configured_rpm") or 0)
        tpm = int(spec.get("configured_tpm") or 0)
        words_per_chunk = long_form.TTS_CHUNK_TARGET_SEC / 60 * (
            (long_form.WPM_MIN + long_form.WPM_MAX) / 2)
        sample = "kelime " * int(words_per_chunk)
        tokens_per_chunk = google_ai.estimate_input_tokens(sample)
        # Whichever ceiling bites first for the selected model.
        by_tokens = (tpm // tokens_per_chunk) if tokens_per_chunk else 0
        per_minute = min(x for x in (rpm, by_tokens) if x) if (rpm or by_tokens) else 0
        return {
            "needed": needed,
            "model": model,
            "daily_request_capacity": rpd,
            "per_minute_request_capacity": per_minute,
            "requests_per_minute_limit": rpm,
            "tpm_limit": tpm,
            "estimated_tokens_per_chunk": tokens_per_chunk,
            "binding_limit": ("requests_per_minute" if rpm and rpm <= by_tokens
                              else "tokens_per_minute"),
            "estimated": True,
        }

    # ------------------------------------------------------------- controls
    def start(self, *, actor: str = "console") -> dict:
        plan = self.plan()
        if not plan:
            raise LongFormError("once uzun video planini kaydet")
        long_form.validate_brief(
            duration_min=plan.get("target_duration_min"),
            chapter_count=plan.get("chapter_count"),
            content_type=_content_type(plan))
        record = self.workflow()
        record["paused"] = False
        record["cancelled"] = False
        record["plan_sha256"] = long_form.sha256_text(json.dumps(plan, sort_keys=True,
                                                                 ensure_ascii=False))
        record["status"] = long_form.overall_status(record)
        self.save_workflow(record, actor=actor)
        self.log_event("long_form.started", actor=actor,
                       chapter_count=plan.get("chapter_count"),
                       target_duration_min=plan.get("target_duration_min"))
        return record

    def pause(self, *, actor: str = "console") -> dict:
        record = dict(self.workflow(), paused=True, updated_at=_now())
        self.save_workflow(record, actor=actor)
        self.log_event("long_form.paused", actor=actor)
        return record

    def resume(self, *, actor: str = "console") -> dict:
        record = dict(self.workflow(), paused=False, updated_at=_now())
        self.save_workflow(record, actor=actor)
        self.log_event("long_form.resumed", actor=actor)
        return record

    def cancel(self, *, actor: str = "console") -> dict:
        """Stop between units of work. Nothing already produced is destroyed."""
        record = dict(self.workflow(), cancelled=True, paused=False,
                      updated_at=_now())
        record["status"] = long_form.CANCELLED
        self.save_workflow(record, actor=actor)
        self.log_event("long_form.cancelled", actor=actor)
        return record

    def approve_stage(self, stage: str, *, actor: str = "console") -> dict:
        """Close a human gate. Only the manual stages accept this."""
        if stage not in long_form.STAGE_KEYS:
            raise LongFormError(f"bilinmeyen asama: {stage!r}")
        if stage in long_form.AUTOMATIC_STAGES:
            raise LongFormError(
                f"{long_form.STAGE_LABELS[stage]} otomatik bir asama; elle "
                "tamamlanmaz")
        record = long_form.set_stage(self.workflow(), stage, long_form.COMPLETED,
                                     detail=f"{actor} onayladi")
        self.save_workflow(record, actor=actor)
        return record

    # ------------------------------------------------------ stock B-roll
    def create_broll_plan(self, *, clip_duration_sec=5,
                          actor: str = "console") -> dict:
        """Create editable script-order slots; this performs no API request."""
        record = stock_media.new_plan(
            self.chapters(), clip_duration_sec=clip_duration_sec,
            ownership=self.ownership)
        self._write(stock_media.BROLL_ARTIFACT, record, actor=actor)
        self.log_event("broll.plan_created", actor=actor,
                       slot_count=len(record.get("slots") or []),
                       clip_duration_sec=record.get("clip_duration_sec"))
        return record

    def search_broll(self, slot_id: str, *, provider: str, query: str,
                     per_page=6, actor: str = "console") -> dict:
        """One explicit stock API call. Credentials remain server-side."""
        plan = self.broll_plan()
        if not plan:
            raise LongFormError("once B-roll arama plani olustur")
        if stock_media.is_stale(plan, self.chapters()):
            raise LongFormError("bolum scripti degisti; once B-roll planini yenile")
        kind = {"pexels": "pexels", "pixabay": "pixabay"}.get(
            str(provider or "").strip().lower())
        if not kind:
            raise LongFormError("stok video saglayicisi Pexels veya Pixabay olmali")
        credential = self.service.secrets.credential(kind)
        if not credential.configured:
            raise LongFormError(
                f"{kind} API anahtari tanimli degil; .env.local dosyasini guncelle")
        result = stock_media.search_videos(
            kind, query, credential.value, per_page=per_page)
        plan = stock_media.store_search(plan, slot_id, result)
        self._write(stock_media.BROLL_ARTIFACT, plan, actor=actor)
        self.log_event("broll.search_completed", actor=actor, slot_id=slot_id,
                       provider=kind, candidate_count=result.get("count"))
        return plan

    def select_broll(self, slot_id: str, candidate_id: str, *,
                     actor: str = "console") -> dict:
        plan = self.broll_plan()
        if not plan:
            raise LongFormError("once B-roll arama plani olustur")
        if stock_media.is_stale(plan, self.chapters()):
            raise LongFormError("bolum scripti degisti; once B-roll planini yenile")
        plan = stock_media.select_candidate(plan, slot_id, candidate_id)
        self._write(stock_media.BROLL_ARTIFACT, plan, actor=actor)
        selected = next(item for item in plan["slots"] if item["id"] == slot_id)
        self.log_event("broll.selected", actor=actor, slot_id=slot_id,
                       provider=(selected.get("selected") or {}).get("provider"),
                       asset_id=(selected.get("selected") or {}).get("asset_id"))
        return plan

    def download_broll(self, slot_id: str, *, actor: str = "console") -> dict:
        plan = self.broll_plan()
        if not plan:
            raise LongFormError("once B-roll arama plani olustur")
        if stock_media.is_stale(plan, self.chapters()):
            raise LongFormError("bolum scripti degisti; once B-roll planini yenile")
        slot = next((item for item in plan.get("slots") or []
                     if item.get("id") == slot_id), None)
        if not slot or not slot.get("selected"):
            raise LongFormError("once bu slot icin bir B-roll adayi sec")
        candidate = slot["selected"]
        filename = stock_media.safe_filename(candidate)
        destination = self._path(f"{stock_media.BROLL_DIRNAME}/{filename}")
        downloaded = stock_media.download_candidate(candidate, destination)
        plan = stock_media.record_download(plan, slot_id, downloaded)
        self._write(stock_media.BROLL_ARTIFACT, plan, actor=actor)
        self.log_event("broll.download_completed", actor=actor, slot_id=slot_id,
                       provider=candidate.get("provider"),
                       asset_id=candidate.get("asset_id"),
                       bytes=downloaded.get("bytes"))
        return plan

    def retry_billing(self, *, actor: str = "console") -> dict:
        """Explicitly requeue a billing-blocked stage after operator action.

        Merely opening or refreshing the page cannot call this. That distinction
        prevents a permanent plan/configuration problem from burning more calls.
        """
        record = self.workflow()
        stage = long_form.next_stage(record)
        if not stage or long_form.stage_state(record, stage) != long_form.BLOCKED_BILLING:
            raise LongFormError("yeniden acilacak billing bloklu bir asama yok")
        if stage == "tts":
            manifest = self._read(tts_chunks.MANIFEST_ARTIFACT)
            if manifest:
                self._write(tts_chunks.MANIFEST_ARTIFACT,
                            tts_chunks.reset_billing_blocks(manifest), actor=actor)
        record = long_form.set_stage(
            record, stage, long_form.QUEUED,
            detail="operator billing/kota ayarlarini duzelttigini bildirdi")
        self.save_workflow(record, actor=actor)
        self.log_event("long_form.billing_retry_requested", actor=actor, stage=stage)
        return record

    # ------------------------------------------------------- human editing
    def save_chapter_text(self, index: int, text: str, *, actor: str = "console") -> dict:
        """Persist a human chapter edit and invalidate ONLY what it affects.

        Editing chapter 3 must not re-run the research or re-speak chapters 1
        and 2. The chunk ledger sorts the audio out by hash; the workflow is
        rolled back to the consistency review because the assembled script
        genuinely changed.
        """
        record = self.chapters()
        if not record:
            raise LongFormError("once bolum scriptlerini uret")
        chapters = [dict(item) for item in (record.get("chapters") or [])]
        target = None
        for chapter in chapters:
            if int(chapter.get("index") or 0) == int(index):
                target = chapter
                break
        if target is None:
            raise LongFormError(f"bolum bulunamadi: {index}")
        body = str(text or "").strip()
        if not body:
            raise LongFormError("bolum metni bos olamaz")
        target["text"] = body
        target["word_count"] = long_form.count_words(body)
        target["text_sha256"] = long_form.sha256_text(body)
        target["duration_estimate"] = long_form.duration_estimate_sec(target["word_count"])
        goal = int(target.get("target_words") or 0)
        target["within_target"] = (bool(goal) and
                                   int(goal * 0.85) <= target["word_count"] <= int(goal * 1.15))
        target["edited_by_human"] = True
        target["updated_at"] = _now()
        record["chapters"] = chapters
        record["total_word_count"] = sum(int(item.get("word_count") or 0)
                                         for item in chapters)
        record["duration_estimate"] = long_form.duration_estimate_sec(
            record["total_word_count"])
        record["updated_at"] = _now()
        self._write(long_form_script.CHAPTERS_ARTIFACT, record, actor=actor)

        workflow = long_form.invalidate_from(
            self.workflow(), "consistency",
            reason=f"bolum {index} elle duzenlendi")
        workflow = long_form.set_stage(workflow, "chapters", long_form.COMPLETED,
                                       detail=f"bolum {index} elle duzenlendi")
        self.save_workflow(workflow, actor=actor)
        return record

    def save_research(self, payload: dict, *, actor: str = "console") -> dict:
        record = self.research() or {}
        record.update({k: v for k, v in (payload or {}).items()
                       if k in {"thesis", "facts", "interpretations", "glossary",
                                "open_questions"}})
        record.setdefault("artifact_type", "long_form_research_pack")
        record["fact_count"] = len(record.get("facts") or [])
        record["interpretation_count"] = len(record.get("interpretations") or [])
        record["updated_at"] = _now()
        self._write(long_form_script.RESEARCH_ARTIFACT, record, actor=actor)
        workflow = long_form.invalidate_from(self.workflow(), "outline",
                                             reason="arastirma paketi duzenlendi")
        self.save_workflow(workflow, actor=actor)
        return record

    # ------------------------------------------------------------- TTS loop
    def _tts_quota_wait(self, ledger, model: str, estimated_tokens: int) -> dict:
        """Turn the quota ledger into a persistent scheduling decision.

        The rules live in `quota_signals` so Turbo asks the same question of the
        same code -- a second copy of "what does this refusal mean" would drift,
        and the drift would only surface against a real provider.
        """
        return quota_signals.tts_quota_wait(ledger, model, estimated_tokens)

    @staticmethod
    def _quota_kind_from_result(result: dict):
        return quota_signals.quota_kind_from_result(result)

    def _record_stage_failure(self, stage: str, exc, result, *, actor: str,
                              started: float, event: str) -> None:
        """Persist a stage failure, telling a quota WAIT apart from a real error.

        A 429 recorded as `failed` invites the operator to press retry, which
        spends more of the quota that just ran out. A wait carries `resume_at`
        and says "come back", which is the only useful thing to say. The TTS
        loop already made this distinction; the text stages did not, and the
        first live pilot ran straight into it.
        """
        quota_kind = self._quota_kind_from_result(result or {})
        if quota_kind is None:
            quota_kind = self._quota_kind_from_exception(exc)
        latency = round(time.time() - started, 3)
        if quota_kind:
            resume_at = (None if quota_kind == long_form.BLOCKED_BILLING
                         else self._quota_resume_at(quota_kind))
            if quota_kind == long_form.BLOCKED_BILLING:
                detail = ("Google AI Studio plan/billing veya kota yapilandirmasi "
                          "istegi engelliyor; ayarlari duzeltip acikca yeniden deneyin")
                event_name = "long_form.blocked_billing"
            elif quota_kind == long_form.WAITING_DAILY_QUOTA:
                detail = "gunluk kota doldu; yeniden denemek kotayi tuketir"
                event_name = "long_form.waiting_daily_quota"
            else:
                detail = "dakikalik hiz siniri; kisa sure sonra devam"
                event_name = "long_form.waiting_rate_limit"
            self.save_workflow(long_form.set_stage(
                self.workflow(), stage, quota_kind, resume_at=resume_at,
                detail=detail),
                actor=actor)
            self.log_event(
                event_name,
                stage=stage, resume_at=resume_at, latency_sec=latency)
            return
        self.save_workflow(long_form.set_stage(
            self.workflow(), stage, long_form.FAILED, error=str(exc)), actor=actor)
        self.log_event(event, error_kind=type(exc).__name__, latency_sec=latency)

    @staticmethod
    def _quota_kind_from_exception(exc):
        """Classify a raised provider error the route did not wrap."""
        return quota_signals.quota_kind_from_exception(exc)

    def _quota_resume_at(self, quota_kind: str) -> str:
        return quota_signals.quota_resume_at(quota_kind)

    def plan_tts(self, *, actor: str = "console") -> dict:
        """Rebuild the chunk plan from the CURRENT chapters, keeping valid audio."""
        plan = self.plan()
        chapters_record = self.chapters()
        if not chapters_record:
            raise LongFormError("once bolum scriptlerini uret")
        voice = plan.get("voice") or google_ai.DEFAULT_TTS_VOICE
        language = plan.get("language") or google_ai.DEFAULT_TTS_LANGUAGE
        model = plan.get("tts_model") or google_ai.TASK_ROUTES["tts"][0]
        provider = plan.get("tts_provider") or "google"
        direction = plan.get("narration_direction") or ""
        words_per_chunk = int(long_form.TTS_CHUNK_TARGET_SEC / 60
                              * ((long_form.WPM_MIN + long_form.WPM_MAX) / 2))
        planned = tts_chunks.plan_chunks(
            chapters_record.get("chapters") or [], words_per_chunk=words_per_chunk,
            voice=voice, model=model, provider=provider,
            narration_direction=direction, language_code=language)
        manifest = tts_chunks.merge_plan(self.chunk_manifest(), planned)
        self._write(tts_chunks.MANIFEST_ARTIFACT, manifest, actor=actor)
        return manifest

    def run_tts(self, *, actor: str = "console", only_chunk: str = "",
                tracker=None, cancel_check=None, max_chunks: int = 0) -> dict:
        """Generate the MISSING chunks only.

        `only_chunk` retries or regenerates exactly one. `max_chunks` bounds a
        single pass so a job can yield; the next pass resumes where this stopped
        because the ledger is on disk, not in memory.

        The pass itself lives in `tts_runner` -- speaking a list of chunks is
        identical work in every workflow, and only the stage bookkeeping below
        is long-form's own.
        """
        manifest = self.plan_tts(actor=actor) if not only_chunk else self.chunk_manifest()
        pending = tts_chunks.pending_chunks(manifest)
        if only_chunk:
            _index, chunk = tts_chunks.find_chunk(manifest, only_chunk)
            pending = [chunk]
        if max_chunks:
            pending = pending[:int(max_chunks)]

        outcome = tts_runner.run_chunk_pass(
            self, manifest=manifest,
            manifest_artifact=tts_chunks.MANIFEST_ARTIFACT,
            audio_dir=self._path(AUDIO_DIRNAME), pending=pending,
            actor=actor, tracker=tracker, cancel_check=cancel_check)

        manifest = outcome["manifest"]
        produced, failed = outcome["produced"], outcome["failed"]
        waiting, readiness = outcome["waiting"], outcome["readiness"]

        workflow = self.workflow()
        if readiness["ready"]:
            workflow = long_form.set_stage(workflow, "tts", long_form.COMPLETED,
                                           detail=f"{readiness['chunk_count']} parca hazir")
        elif waiting:
            workflow = long_form.set_stage(
                workflow, "tts", waiting["state"], resume_at=waiting["resume_at"],
                detail=(f"{readiness['completed_count']}/{readiness['chunk_count']} "
                        f"parca; {waiting['reason']}"))
            self.log_event(
                ("long_form.blocked_billing"
                 if waiting["state"] == long_form.BLOCKED_BILLING
                 else "long_form.waiting_rate_limit"
                 if waiting["state"] == long_form.WAITING_RATE_LIMIT
                 else "long_form.waiting_daily_quota"),
                stage="tts", model=waiting.get("model"),
                resume_at=waiting.get("resume_at"),
                completed_count=readiness["completed_count"],
                chunk_count=readiness["chunk_count"])
        elif failed:
            workflow = long_form.set_stage(
                workflow, "tts", long_form.FAILED,
                error=f"{failed} parca basarisiz", attempt_delta=1)
        else:
            workflow = long_form.set_stage(workflow, "tts", long_form.RUNNING,
                                           detail=f"{readiness['completed_count']}/"
                                                  f"{readiness['chunk_count']} parca")
        self.save_workflow(workflow, actor=actor)
        return {"manifest": manifest, "produced": produced, "failed": failed,
                "waiting": waiting, "readiness": readiness}

    def concat_audio(self, *, actor: str = "console") -> dict:
        """Join validated chunks in sequence order and validate the whole.

        Refuses on a missing chunk rather than joining what exists: a narration
        with a hole in it still sounds finished, which is the worst possible
        failure mode for an eighteen minute video.
        """
        manifest = self.chunk_manifest()
        ordered = tts_chunks.ordered_ready(manifest)
        if not ordered:
            readiness = tts_chunks.concat_readiness(manifest)
            self.log_event("audio_concat.failed",
                           missing_count=len(readiness["missing"]))
            raise LongFormError(
                f"{len(readiness['missing'])} parca eksik; birlestirme yapilmadi")
        self.log_event("audio_concat.started", chunk_count=len(ordered))
        import wave
        target = self._path(COMBINED_AUDIO)
        params = None
        frames = []
        for chunk in ordered:
            with wave.open(str(chunk["output_path"]), "rb") as handle:
                if params is None:
                    params = handle.getparams()
                elif (handle.getnchannels(), handle.getsampwidth(),
                      handle.getframerate()) != (params.nchannels, params.sampwidth,
                                                 params.framerate):
                    self.log_event("audio_concat.failed", reason="format_mismatch")
                    raise LongFormError(
                        f"{chunk['id']} farkli ses formatinda; birlestirilemez")
                frames.append(handle.readframes(handle.getnframes()))
        payload = b"".join(frames)
        temp = target.with_suffix(".part")
        with wave.open(str(temp), "wb") as handle:
            handle.setnchannels(params.nchannels)
            handle.setsampwidth(params.sampwidth)
            handle.setframerate(params.framerate)
            handle.writeframes(payload)
        import os as _os
        _os.replace(temp, target)

        validation = google_ai.validate_audio_file(target)
        duration = (validation.get("details") or {}).get("duration_sec") or 0.0
        combined = {
            "path": str(target),
            "relative_path": self.storage.relative_to_namespace(self.ownership, target),
            "sha256": tts_chunks.sha256_file(target),
            "duration_sec": round(float(duration), 2),
            "chunk_count": len(ordered),
            "chunk_order": [chunk["id"] for chunk in ordered],
            "validation": validation,
            "duration_check": long_form.validate_final_duration(duration),
            "created_at": _now(),
        }
        manifest["combined"] = combined
        manifest["status"] = "review_required"
        self._write(tts_chunks.MANIFEST_ARTIFACT, manifest, actor=actor)
        self.log_event("audio_concat.completed", chunk_count=len(ordered),
                       duration_sec=combined["duration_sec"],
                       within_target=combined["duration_check"]["within_target"])
        if not validation.get("passed"):
            raise LongFormError(f"birlesik ses dogrulamayi gecmedi: "
                                f"{validation.get('reason')}")
        return combined

    # ---------------------------------------------------- generation stages
    def _text_call(self, task: str, prompt: str, **kwargs) -> dict:
        """One routed text request. Every call is its own route entry.

        Routed per call rather than per stage so the quota ledger records what
        actually happened: six chapters are six requests, not one job.
        """
        client = self.service._build_google_client()
        kwargs.setdefault("ledger", self.ledger())
        if task == "grounded_research":
            return google_ai.run_google_grounded_research(prompt, client=client, **kwargs)
        return google_ai._text_task(task, prompt, client=client, **kwargs)

    def run_research(self, *, actor: str = "console", instructions: str = "") -> dict:
        plan = self.plan()
        if not plan:
            raise LongFormError("once uzun video planini kaydet")
        workflow = long_form.set_stage(self.workflow(), "research",
                                       long_form.RUNNING, attempt_delta=1)
        self.save_workflow(workflow, actor=actor)
        self.log_event("research.started", task="grounded_research")
        started = time.time()
        try:
            result = self._text_call(
                "grounded_research",
                long_form_script.build_research_prompt(
                    plan, instructions=instructions or plan.get("research_instructions") or ""))
            if result.get("status") != "ok":
                raise LongFormError(result.get("status_reason") or "arastirma basarisiz")
            record = long_form_script.parse_research(
                result.get("output"),
                grounding_sources=result.get("grounding_sources"))
        except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
            self._record_stage_failure("research", exc, locals().get("result"),
                                       actor=actor, started=started,
                                       event="research.failed")
            raise
        record["provider"] = result.get("provider")
        record["model"] = result.get("model_used")
        self._write(long_form_script.RESEARCH_ARTIFACT, record, actor=actor)
        self.save_workflow(long_form.set_stage(
            self.workflow(), "research", long_form.COMPLETED,
            detail=f"{record['fact_count']} olgu, {record['interpretation_count']} yorum"),
            actor=actor)
        self.log_event("research.completed", fact_count=record["fact_count"],
                       interpretation_count=record["interpretation_count"],
                       model=result.get("model_used"),
                       latency_sec=round(time.time() - started, 3))
        return record

    def run_outline(self, *, actor: str = "console") -> dict:
        plan = self.plan()
        research = self.research()
        if not research:
            raise LongFormError("once arastirma paketini uret")
        self.save_workflow(long_form.set_stage(self.workflow(), "outline",
                                               long_form.RUNNING, attempt_delta=1),
                           actor=actor)
        self.log_event("outline.started", chapter_count=plan.get("chapter_count"))
        started = time.time()
        try:
            result = self._text_call(
                "script_generation",
                long_form_script.build_outline_prompt(plan, research))
            if result.get("status") != "ok":
                raise LongFormError(result.get("status_reason") or "outline basarisiz")
            record = long_form_script.parse_outline(result.get("output"), plan)
        except Exception as exc:  # noqa: BLE001
            self._record_stage_failure("outline", exc, locals().get("result"),
                                       actor=actor, started=started,
                                       event="outline.failed")
            raise
        record["provider"] = result.get("provider")
        record["model"] = result.get("model_used")
        self._write(long_form_script.OUTLINE_ARTIFACT, record, actor=actor)
        self.save_workflow(long_form.set_stage(
            self.workflow(), "outline", long_form.COMPLETED,
            detail=f"{record['chapter_count']} bolum"), actor=actor)
        self.log_event("outline.completed", chapter_count=record["chapter_count"],
                       model=result.get("model_used"),
                       latency_sec=round(time.time() - started, 3))
        return record

    def run_chapters(self, *, actor: str = "console", cancel_check=None,
                     tracker=None) -> dict:
        """Generate every chapter in its OWN call, resuming where it stopped.

        Chapters already written are skipped, so a failure in chapter five costs
        one request to fix, not five.
        """
        plan = self.plan()
        research = self.research()
        outline = self.outline()
        if not outline:
            raise LongFormError("once outline uret")
        existing = {int(item.get("index") or 0): item
                    for item in ((self.chapters() or {}).get("chapters") or [])}
        self.save_workflow(long_form.set_stage(self.workflow(), "chapters",
                                               long_form.RUNNING, attempt_delta=1),
                           actor=actor)
        produced = list(existing.values())
        previous_summary = ""
        planned = sorted(outline.get("chapters") or [],
                         key=lambda item: int(item.get("index") or 0))
        for entry in planned:
            index = int(entry.get("index"))
            if index in existing:
                previous_summary = existing[index].get("summary") or previous_summary
                continue
            if cancel_check is not None and cancel_check():
                self.log_event("long_form.cancelled", stage="chapters",
                               chapter_index=index)
                break
            bible = long_form_script.build_story_bible(
                plan, research, outline, index=index,
                previous_summary=previous_summary)
            self.log_event("chapter.started", chapter_id=f"ch_{index:02d}",
                           chapter_index=index,
                           target_words=(bible.get("current_chapter") or {}).get("target_words"))
            if tracker is not None:
                tracker.count(len(produced), len(planned), unit="bolum",
                              message=f"Bolum {index}")
            started = time.time()
            try:
                result = self._text_call(
                    "script_generation", long_form_script.build_chapter_prompt(bible))
                if result.get("status") != "ok":
                    raise LongFormError(result.get("status_reason") or "bolum basarisiz")
                chapter = long_form_script.parse_chapter(result.get("output"), bible)
            except Exception as exc:  # noqa: BLE001
                self.save_workflow(long_form.set_stage(
                    self.workflow(), "chapters", long_form.FAILED,
                    error=f"bolum {index}: {exc}"), actor=actor)
                self.log_event("chapter.failed", chapter_index=index,
                               error_kind=type(exc).__name__,
                               latency_sec=round(time.time() - started, 3))
                if produced:
                    self._write(long_form_script.CHAPTERS_ARTIFACT,
                                long_form_script.chapters_artifact(produced, plan),
                                actor=actor)
                raise
            chapter["provider"] = result.get("provider")
            chapter["model"] = result.get("model_used")
            produced.append(chapter)
            previous_summary = chapter.get("summary") or previous_summary
            # Persisted after EVERY chapter: a crash costs the chapter in
            # flight, never the ones already paid for.
            self._write(long_form_script.CHAPTERS_ARTIFACT,
                        long_form_script.chapters_artifact(produced, plan), actor=actor)
            self.log_event("chapter.completed", chapter_id=chapter["id"],
                           chapter_index=index, word_count=chapter["word_count"],
                           within_target=chapter["within_target"],
                           model=result.get("model_used"),
                           latency_sec=round(time.time() - started, 3))

        record = long_form_script.chapters_artifact(produced, plan)
        self._write(long_form_script.CHAPTERS_ARTIFACT, record, actor=actor)
        complete = len(produced) == len(planned)
        self.save_workflow(long_form.set_stage(
            self.workflow(), "chapters",
            long_form.COMPLETED if complete else long_form.RUNNING,
            detail=f"{len(produced)}/{len(planned)} bolum"), actor=actor)
        return record

    def run_consistency(self, *, actor: str = "console") -> dict:
        plan = self.plan()
        chapters_record = self.chapters()
        if not chapters_record:
            raise LongFormError("once bolum scriptlerini uret")
        self.save_workflow(long_form.set_stage(self.workflow(), "consistency",
                                               long_form.RUNNING, attempt_delta=1),
                           actor=actor)
        self.log_event("consistency_review.started",
                       chapter_count=len(chapters_record.get("chapters") or []))
        started = time.time()
        try:
            result = self._text_call(
                "consistency_review",
                long_form_script.build_consistency_prompt(plan, chapters_record))
            if result.get("status") != "ok":
                raise LongFormError(result.get("status_reason") or "inceleme basarisiz")
            record = long_form_script.parse_consistency(result.get("output"),
                                                        chapters_record)
        except Exception as exc:  # noqa: BLE001
            self._record_stage_failure("consistency", exc, locals().get("result"),
                                       actor=actor, started=started,
                                       event="consistency_review.failed")
            raise
        record["provider"] = result.get("provider")
        record["model"] = result.get("model_used")
        self._write(long_form_script.CONSISTENCY_ARTIFACT, record, actor=actor)
        self.save_workflow(long_form.set_stage(
            self.workflow(), "consistency", long_form.COMPLETED,
            detail=f"{record['issue_count']} bulgu"), actor=actor)
        self.log_event("consistency_review.completed",
                       issue_count=record["issue_count"],
                       high_severity_count=record["high_severity_count"],
                       model=result.get("model_used"),
                       latency_sec=round(time.time() - started, 3))
        return record

    def run_next_stage(self, *, actor: str = "console", cancel_check=None,
                       tracker=None) -> dict:
        """Run the ONE stage that is currently runnable.

        A dispatcher rather than a loop: each call does one unit of work and
        returns, so a job can be interrupted, restarted and resumed without any
        of the stages needing to know about the others.
        """
        decision = long_form.runnable_stage(self.workflow())
        stage = decision.get("stage")
        if decision.get("blocked") or not stage:
            return {"ran": None, "blocked": True, "reason": decision.get("reason")}
        runners = {
            "research": lambda: self.run_research(actor=actor),
            "outline": lambda: self.run_outline(actor=actor),
            "chapters": lambda: self.run_chapters(actor=actor,
                                                  cancel_check=cancel_check,
                                                  tracker=tracker),
            "consistency": lambda: self.run_consistency(actor=actor),
            "tts": lambda: self.run_tts(actor=actor, cancel_check=cancel_check,
                                        tracker=tracker),
        }
        runner = runners.get(stage)
        if runner is None:
            return {"ran": None, "blocked": True,
                    "reason": f"{long_form.STAGE_LABELS[stage]} bu surumde "
                              "otomatik calistirilmiyor"}
        runner()
        return {"ran": stage, "blocked": False,
                "reason": f"{long_form.STAGE_LABELS[stage]} calisti"}

    # ------------------------------------------------------------- view model
    def view(self) -> dict:
        plan = self.plan()
        workflow = self.workflow()
        chapters_record = self.chapters() or {}
        manifest = self.chunk_manifest()
        broll = self.broll_plan()
        stock_providers = []
        for kind, label in (("pexels", "Pexels"), ("pixabay", "Pixabay")):
            reference = self.service.secrets.reference(kind)
            stock_providers.append({"id": kind, "label": label,
                                    "configured": reference.configured,
                                    "alias_used": reference.alias_used})
        return {
            "available": bool(plan),
            "plan": plan,
            "estimates": self.estimates(),
            "readiness": self.provider_readiness(),
            "progress": long_form.progress_summary(workflow),
            "research": self.research(),
            "outline": self.outline(),
            "chapters": chapters_record.get("chapters") or [],
            "chapters_summary": {
                "total_word_count": chapters_record.get("total_word_count"),
                "duration_estimate": chapters_record.get("duration_estimate"),
                "word_target": chapters_record.get("word_target"),
            },
            "full_script": long_form_script.full_script_text(chapters_record),
            "consistency": self.consistency(),
            "tts": tts_chunks.summarise(manifest),
            "broll": {**stock_media.summary(broll, chapters_record),
                      "providers": stock_providers,
                      "provider_calls_are_explicit": True,
                      "source_credit_note": ("Pexels/Pixabay kaynak sayfasi ve "
                                             "ureticisi manifestte korunur.")},
            "events": self.events(limit=40),
            "quota": self.quota_summary(),
            "stage_states": list(long_form.STAGE_STATES),
            "not_uploaded": True,
        }


def _content_type(plan: dict) -> str:
    """Map the planner's older vocabulary onto the workflow's content types.

    The planning screen shipped with `comparison`; the workflow speaks
    `commentary` and `general_knowledge`. Translating here keeps every saved
    plan loadable instead of failing validation on a value it was allowed to
    save.
    """
    value = str((plan or {}).get("content_type") or "").strip()
    if value in long_form.CONTENT_TYPES:
        return value
    return {"comparison": "commentary"}.get(value, "explainer")
