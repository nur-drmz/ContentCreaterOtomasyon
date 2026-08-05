"""Server-persisted automatic flow.

WHY THIS EXISTS
---------------
The first automatic flow lived in browser memory: a `state.autoRun` boolean plus
a polling loop. Refresh the tab and the flow silently stopped; restart the
backend and nothing knew a flow had been running. Worse, the advance function was
stateless, so "have we already tried this phase?" could not be answered -- the
only thing preventing a loop was that the caller stopped calling.

So the flow is now an ARTIFACT (`results/automation.json`) with an explicit state
machine, and every transition is written atomically before it is reported.

    idle ──start──▶ running ──(machine phase submitted)──▶ running
                      │
                      ├──(human gate reached)────────────▶ blocked
                      ├──pause───────────────────────────▶ paused ──resume──▶ running
                      ├──cancel──────────────────────────▶ cancelled
                      ├──(no progress / dedup / failure)─▶ blocked
                      └──(render approval valid)─────────▶ completed

WHAT AUTOMATION MAY NOT DO
--------------------------
It never chooses a processing mode, never selects a moment, never approves
anything, and never bypasses a gate. Those five points are hard stops that
produce `blocked` with an actionable message:

    plan_selection · moment_selection · script_approval ·
    voiceover_approval · render_approval

LOOP PREVENTION
---------------
Three independent brakes, because "it stopped calling" is not a guarantee:

  1. per-phase attempt cap -- automation submits each machine phase AT MOST
     `MAX_PHASE_ATTEMPTS` times; after that the phase is a human decision;
  2. no-progress counter -- a tick that neither submits a job nor changes the
     pipeline signature increments a counter and blocks at the threshold;
  3. terminal outcomes -- a deduplicated submit, a failed job, an artifact-less
     run or `insufficient_data` blocks immediately with the reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

from local_pipeline import source_strategy

from .audit import AuditLog
from .jobs import ACTIVE_STATUSES, STATUS_FAILED
from .storage import atomic_write_json

AUTOMATION_ARTIFACT = "results/automation.json"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_BLOCKED = "blocked"
STATE_COMPLETED = "completed"
STATE_CANCELLED = "cancelled"
AUTOMATION_STATES = (STATE_IDLE, STATE_RUNNING, STATE_PAUSED, STATE_BLOCKED,
                     STATE_COMPLETED, STATE_CANCELLED)

#: States from which a tick may do work.
ADVANCEABLE = (STATE_RUNNING,)

#: Human gates. Automation stops here, always, by design.
HUMAN_GATES = ("plan_selection", "moment_selection", "script_approval",
               "voiceover_approval", "render_approval")

#: Machine phases automation is allowed to submit on its own.
MACHINE_PHASES = ("source_assessment", "video_analysis", "script_generation",
                  "tts", "render")

MAX_PHASE_ATTEMPTS = 1
MAX_NO_PROGRESS_TICKS = 3
MAX_HISTORY = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_state(ownership) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "console_automation",
        **ownership.to_dict(),
        "state": STATE_IDLE,
        "blocker": None,
        "message": "Otomatik akis baslatilmadi.",
        "current_job_id": None,
        "last_phase": None,
        "phase_attempts": {},
        "no_progress_ticks": 0,
        "last_signature": None,
        "started_at": None,
        "started_by": None,
        "updated_at": _now(),
        "finished_at": None,
        "history": [],
        "not_uploaded": True,
    }


class AutomationEngine:
    """Owns `automation.json` and advances it one safe step at a time."""

    def __init__(self, app, ownership):
        self.app = app
        self.ownership = ownership
        self.service = app.service(ownership)
        self.store = app.job_store(ownership)
        self.audit = AuditLog(app.storage, ownership)

    # ------------------------------------------------------------- storage
    @property
    def path(self):
        return self.app.storage.resolve(self.ownership, AUTOMATION_ARTIFACT,
                                        create_parent=True)

    def load(self) -> dict:
        loaded = self.service.load_artifact_path(AUTOMATION_ARTIFACT)
        if loaded is None:
            return new_state(self.ownership)
        return loaded

    def save(self, state: dict) -> dict:
        state["updated_at"] = _now()
        atomic_write_json(self.path, state)
        self._report_progress(state)
        return state

    def _report_progress(self, state) -> None:
        """Mirror the flow into the shared progress panel.

        Countable by construction: the workflow map already knows how many steps
        this mode has and how many are done, so this bar is a real percentage --
        of STEPS, which is what it says it is.
        """
        from .progress import ProgressTracker
        from .status import build_workflow_map

        try:
            workflow = build_workflow_map(self.service, self.ownership)
            stages = [{"key": step["key"], "label": step["label"]}
                      for step in workflow["steps"] if step["state"] != "skipped"]
            tracker = ProgressTracker(self.app.storage, self.ownership, "automation",
                                       stages=stages)
            # Adopt the existing record so the elapsed clock survives ticks; the
            # stage list is rebuilt because the mode may have changed the flow.
            if not tracker.attach():
                tracker.start(state.get("message") or "")
            tracker._record["stages"] = [{**stage, "state": "waiting"} for stage in stages]
            current = workflow.get("current_step")
            if current and any(stage["key"] == current for stage in stages):
                tracker.stage(current, state.get("message") or workflow["current_label"])
            if state["state"] == STATE_COMPLETED:
                tracker.finish(message=state.get("message") or "Akis tamamlandi.")
            elif state["state"] in (STATE_BLOCKED, STATE_CANCELLED, STATE_PAUSED, STATE_IDLE):
                # NOT a failure: the flow is waiting for a human decision.
                # Painting that red would teach the user to ignore red.
                tracker.stop(state.get("blocker") or state["state"],
                             message=state.get("message") or "Otomatik akis bekliyor.")
        except Exception:  # noqa: BLE001 - progress must never break the flow
            pass

    def _record(self, state, event, detail=None):
        entry = {"at": _now(), "event": event, "state": state["state"],
                 "blocker": state.get("blocker"), "detail": detail}
        state["history"] = ([entry] + list(state.get("history") or []))[:MAX_HISTORY]
        return state

    # ------------------------------------------------------------ controls
    def start(self, *, actor="console") -> dict:
        state = self.load()
        if state["state"] == STATE_RUNNING:
            return state
        # Starting fresh clears the brakes: the user has looked at the console
        # and deliberately asked for another run.
        state.update({
            "state": STATE_RUNNING, "blocker": None,
            "message": "Otomatik akis basladi.",
            "phase_attempts": {}, "no_progress_ticks": 0, "last_signature": None,
            "started_at": _now(), "started_by": str(actor)[:120], "finished_at": None,
        })
        self._record(state, "start")
        self.audit.record("settings.changed", actor=actor,
                          detail={"automation": "start"})
        return self.save(state)

    def pause(self, *, actor="console") -> dict:
        state = self.load()
        if state["state"] not in (STATE_RUNNING, STATE_BLOCKED):
            return state
        state.update({"state": STATE_PAUSED,
                      "message": "Otomatik akis duraklatildi. Devam etmek icin 'Devam et'."})
        self._record(state, "pause")
        self.audit.record("settings.changed", actor=actor, detail={"automation": "pause"})
        return self.save(state)

    def resume(self, *, actor="console") -> dict:
        state = self.load()
        if state["state"] not in (STATE_PAUSED, STATE_BLOCKED):
            return state
        # Resuming after a block means the human dealt with the gate, so the
        # no-progress brake is released -- but per-phase attempts are NOT reset,
        # so a genuinely broken phase cannot be re-submitted in a loop.
        state.update({"state": STATE_RUNNING, "blocker": None,
                      "no_progress_ticks": 0,
                      "message": "Otomatik akis devam ediyor."})
        self._record(state, "resume")
        self.audit.record("settings.changed", actor=actor, detail={"automation": "resume"})
        return self.save(state)

    def cancel(self, *, actor="console") -> dict:
        state = self.load()
        state.update({"state": STATE_CANCELLED, "blocker": None,
                      "message": "Otomatik akis iptal edildi. Fazlari elle calistirabilirsin.",
                      "finished_at": _now()})
        self._record(state, "cancel")
        self.audit.record("settings.changed", actor=actor, detail={"automation": "cancel"})
        return self.save(state)

    # ---------------------------------------------------------------- tick
    def tick(self, *, actor="console:auto") -> dict:
        """Advance at most one step. Safe to call repeatedly and concurrently."""
        state = self.load()
        if state["state"] not in ADVANCEABLE:
            return state

        active = self.store.list(statuses=ACTIVE_STATUSES, limit=5)
        if active:
            state.update({"current_job_id": active[0]["job_id"],
                          "message": "Bir faz calisiyor; bitince otomasyon devam edecek."})
            return self.save(state)

        signature = self._signature()
        decision = self._next_step()

        if decision["kind"] == "gate":
            return self._block(state, decision["blocker"], decision["message"],
                               signature=signature)
        if decision["kind"] == "done":
            state.update({"state": STATE_COMPLETED, "blocker": None,
                          "message": decision["message"], "finished_at": _now(),
                          "last_signature": signature, "no_progress_ticks": 0})
            self._record(state, "completed")
            return self.save(state)

        phase = decision["phase"]
        attempts = int((state.get("phase_attempts") or {}).get(phase, 0))
        if attempts >= MAX_PHASE_ATTEMPTS:
            return self._block(
                state, "phase_attempt_limit",
                f"'{phase}' fazi otomatik olarak zaten denendi ve ilerlemedi. "
                "Ilgili sekmeden ayarlari gozden gecirip elle yeniden calistir.",
                signature=signature)

        record, created = self.app.submit_job(
            self.ownership, phase, params=decision["params"],
            submitted_by=actor, run_request_id=f"auto:{phase}:{attempts}")

        state.setdefault("phase_attempts", {})[phase] = attempts + 1
        state["last_phase"] = phase
        state["current_job_id"] = record["job_id"]

        if not created:
            return self._block(
                state, "deduplicated_job",
                f"'{phase}' fazi ayni girdilerle daha once calistirilmis. "
                "Yeni bir calistirma icin ilgili sekmedeki 'Baslat / yeniden calistir' "
                "dugmesini kullan.",
                signature=signature)

        # Genuine progress: reset the no-progress brake.
        state.update({"no_progress_ticks": 0, "last_signature": signature,
                      "message": decision["message"], "blocker": None})
        self._record(state, "submitted", {"phase": phase, "job_id": record["job_id"]})
        return self.save(state)

    def _block(self, state, blocker, message, *, signature=None) -> dict:
        if signature is not None and signature == state.get("last_signature"):
            state["no_progress_ticks"] = int(state.get("no_progress_ticks") or 0) + 1
        else:
            state["no_progress_ticks"] = 0
        state["last_signature"] = signature
        if state["no_progress_ticks"] >= MAX_NO_PROGRESS_TICKS:
            message = (f"{message} (Otomatik akis {MAX_NO_PROGRESS_TICKS} denemede "
                       "ilerleyemedi ve durduruldu.)")
        state.update({"state": STATE_BLOCKED, "blocker": blocker, "message": message})
        self._record(state, "blocked", {"blocker": blocker})
        return self.save(state)

    def _signature(self) -> str:
        """Cheap fingerprint of pipeline progress.

        Two ticks with the same signature and no new job mean nothing moved --
        which is exactly the condition a loop needs.
        """
        parts = []
        for key in ("source_assessment", "processing_plan", "hot_moments",
                    "moment_selection", "selected_script", "selected_voiceover",
                    "render_manifest"):
            loaded = self.service.load_artifact(key)
            data = loaded["data"] or {}
            parts.append(f"{key}={data.get('status') or ('problem' if loaded['problem'] else '-')}")
        parts.append(f"jobs={len(self.store.list(limit=200))}")
        return "|".join(parts)

    # ------------------------------------------------------------ decision
    def _next_step(self) -> dict:
        """Pure decision: what should happen next? No side effects."""
        service = self.service

        project = service.load_artifact("project")
        if not ((project.get("data") or {}).get("source")):
            return {"kind": "gate", "blocker": "source_video",
                    "message": "Once Faz 0 sekmesinden kaynak video yukle."}

        assessment = service.load_artifact("source_assessment")
        if assessment["problem"]:
            return {"kind": "gate", "blocker": "source_assessment",
                    "message": f"Kaynak degerlendirmesi okunamadi: {assessment['problem']}"}
        if not assessment["data"]:
            return {"kind": "job", "phase": "source_assessment", "params": {},
                    "message": "Kaynak degerlendirmesi baslatildi."}

        plan = service.load_artifact("processing_plan")
        if plan["problem"]:
            return {"kind": "gate", "blocker": "processing_plan", "message": plan["problem"]}
        if not plan["data"]:
            return {"kind": "gate", "blocker": "plan_selection",
                    "message": "Faz 0'da isleme modunu SEC, plani olustur ve hash ile onayla. "
                               "Otomatik akis mod secemez."}
        allowed, reason = service.plan_gate("video_analysis")
        if not allowed:
            return {"kind": "gate", "blocker": "plan_selection", "message": reason}

        hot = service.load_artifact("hot_moments")
        if hot["problem"]:
            return {"kind": "gate", "blocker": "hot_moments", "message": hot["problem"]}
        if not hot["data"]:
            return {"kind": "job", "phase": "video_analysis",
                    "params": {"whisper_model_size": "small", "clip_start_sec": 0,
                                "clip_duration_sec": 240, "scene_detector": "content",
                                "top_n": 5},
                    "message": "Video analizi baslatildi."}
        if not (hot["data"].get("hot_moments") or []):
            return {"kind": "gate", "blocker": "no_hot_moments",
                    "message": "Analiz aday moment uretmedi (insufficient_data). "
                               "Moment Explorer'da klip araligini/esikleri degistirip elle yeniden calistir."}

        selection = service.load_artifact("moment_selection")
        if selection["problem"] or not selection["data"]:
            return {"kind": "gate", "blocker": "moment_selection",
                    "message": "Moment Explorer'da adaylari izle ve bir moment SEC. "
                               "Otomatik akis moment secemez."}

        decision = plan["data"].get("decision") or {}
        approvals = service.approval_chain()

        if decision.get("mode") == source_strategy.MODE_NARRATED_THEORY:
            script = service.load_artifact("selected_script")
            script_state = approvals.get("script") or {}
            if script["problem"]:
                return {"kind": "gate", "blocker": "script_generation",
                        "message": script["problem"]}
            if not script["data"]:
                return {"kind": "job", "phase": "script_generation", "params": {},
                        "message": "Script uretimi baslatildi."}
            if script["data"].get("status") in ("failed", "insufficient_data"):
                return {"kind": "gate", "blocker": "script_generation",
                        "message": ("Script uretimi kullanilabilir bir artefakt uretmedi "
                                    f"(status={script['data'].get('status')}). "
                                    "Script Studio'dan elle yeniden calistir.")}
            if not script_state.get("effective_valid"):
                return {"kind": "gate", "blocker": "script_approval",
                        "message": "Script Studio'da scripti OKU ve hash ile onayla. "
                                   "Otomatik akis onay veremez."}

            voice = service.load_artifact("selected_voiceover")
            voice_state = approvals.get("voiceover") or {}
            if voice["problem"]:
                return {"kind": "gate", "blocker": "tts", "message": voice["problem"]}
            if not voice["data"]:
                return {"kind": "job", "phase": "tts",
                        "params": {"voice": "Kore", "language_code": "tr-TR"},
                        "message": "Seslendirme baslatildi."}
            if voice["data"].get("status") in ("failed", "insufficient_data"):
                return {"kind": "gate", "blocker": "tts",
                        "message": ("Seslendirme kullanilabilir bir ses uretmedi "
                                    f"(status={voice['data'].get('status')}). "
                                    "Voice Studio'dan elle yeniden calistir.")}
            if not voice_state.get("effective_valid"):
                return {"kind": "gate", "blocker": "voiceover_approval",
                        "message": "Voice Studio'da sesi DINLE ve hash ile onayla. "
                                   "Otomatik akis onay veremez."}

        render = service.load_artifact("render_manifest")
        render_state = approvals.get("render") or {}
        if render["problem"]:
            return {"kind": "gate", "blocker": "render", "message": render["problem"]}
        if not render["data"]:
            config = dict(decision.get("render") or {})
            return {"kind": "job", "phase": "render",
                    "params": {
                        "render_mode": config.get("render_mode", "blurred_background"),
                        "preview_mode": config.get("preview_mode", True),
                        "subtitle_whisper_model_size": config.get(
                            "subtitle_whisper_model_size", "small"),
                        "subtitle_language": config.get("subtitle_language", "auto"),
                        "caption_style": config.get("caption_style", "active_word_yellow"),
                        "caption_theme": config.get("caption_theme", "auto"),
                    },
                    "message": "Render baslatildi."}
        if render["data"].get("status") in ("failed", "insufficient_data"):
            return {"kind": "gate", "blocker": "render",
                    "message": ("Render teknik dogrulamayi gecemedi "
                                f"(status={render['data'].get('status')}). "
                                "Render Studio'dan ayarlari gozden gecirip elle yeniden calistir.")}
        if render_state.get("effective_valid"):
            return {"kind": "done", "message": "Akis tamamlandi; render onayi gecerli."}
        return {"kind": "gate", "blocker": "render_approval",
                "message": "Render Studio'da videoyu IZLE ve final onayi ver. "
                           "Otomatik akis onay veremez."}

    # ------------------------------------------------------------ view
    def view(self) -> dict:
        state = self.load()
        next_step = self._next_step() if state["state"] != STATE_CANCELLED else None
        return {
            "state": state["state"],
            "blocker": state.get("blocker"),
            "message": state.get("message"),
            "current_job_id": state.get("current_job_id"),
            "last_phase": state.get("last_phase"),
            "phase_attempts": dict(state.get("phase_attempts") or {}),
            "no_progress_ticks": state.get("no_progress_ticks"),
            "started_at": state.get("started_at"),
            "started_by": state.get("started_by"),
            "finished_at": state.get("finished_at"),
            "history": list(state.get("history") or [])[:10],
            "can_start": state["state"] in (STATE_IDLE, STATE_COMPLETED, STATE_CANCELLED,
                                             STATE_BLOCKED),
            "can_pause": state["state"] in (STATE_RUNNING, STATE_BLOCKED),
            "can_resume": state["state"] in (STATE_PAUSED, STATE_BLOCKED),
            "can_cancel": state["state"] in (STATE_RUNNING, STATE_PAUSED, STATE_BLOCKED),
            "should_poll": state["state"] == STATE_RUNNING,
            "human_gates": list(HUMAN_GATES),
            "next_hint": (next_step or {}).get("message"),
            "next_kind": (next_step or {}).get("kind"),
        }
