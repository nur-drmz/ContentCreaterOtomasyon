"""PublishService: publication orchestration behind the private-first gates.

WHAT THIS OWNS
--------------
The publication use cases that used to live on the façade:

  - publication metadata: generation (through the injected Google client),
    manual writing, selection and hash-bound approval,
  - the YouTube provider's lazy construction and credential-state surface,
  - publish-intent creation and approval,
  - the one authorized PRIVATE upload an intent permits, with progress,
  - publish-manifest persistence after the external call,
  - visibility approval (bound to the reviewed remote id) and application,
  - the publish-state view model.

WHAT IT DELIBERATELY DOES NOT OWN
----------------------------------
- **Publication policy**: private-first, one-use intents, digest binding,
  visibility rules and preconditions stay canonical in
  `local_pipeline.publish`. This service calls them; it never re-implements a
  gate, a hash or a status transition.
- **YouTube wire protocol**: OAuth, resumable upload, chunk recovery, token
  redaction and quota accounting stay in `local_pipeline.publish_youtube`,
  reached only through the injected `publish.PublishProvider`.
- **Content approval**: granting and chain validity belong to
  `ApprovalService`, consulted READ-ONLY through `ports.ApprovalQuery`.
  Content approval is necessary but never sufficient for publication; the
  three publication approvals (metadata, intent, visibility) are separate
  state machines inside the publication artifacts, not `approval.approve`.
- **Job execution**: idempotency keys, leases, attempts, retries and
  cancellation stay in `creator_console.jobs` / `ConsoleApp`. This service
  reports progress; it does not lease work.

THE INVARIANTS THIS CLASS EXISTS TO PRESERVE
--------------------------------------------
A. Private-first. The first upload is ALWAYS `private`; the intent's
   `initial_visibility` is set by `local_pipeline.publish`, not by the caller.
B. One intent, one upload. `upload_performed` is spent exactly once per
   approved intent, and a persisted `remote_id` blocks a second attempt.
C. Approval chains stay separated. Metadata approval authorizes nothing by
   itself; intent approval authorizes one private upload; visibility approval
   authorizes one visibility change, bound to the reviewed remote id.
D. No token leaves the boundary. Credential state is a capability report; the
   provider's own sanitization stays the only way an error reaches the trail.
E. Fail-closed crash window. A durable `upload_attempt` marker is written
   BEFORE the external side effect; if the remote upload then succeeds but a
   local write fails (or the outcome is ambiguous), the marker blocks any
   automatic second upload of the same intent and preserves the observed
   remote id as reconciliation evidence. At-least-once, never silent
   duplicates (see PUBLISH_SERVICE.md).
"""
from __future__ import annotations

from pathlib import Path

from application.artifact_catalog import RUNTIME_DIRNAME
from application.errors import ArtifactProblem, PipelineServiceError
from application.time_utils import now_iso
from local_pipeline import google_ai, metadata_studio
from local_pipeline import publish as publish_module
from local_pipeline import publish_youtube


class PublishService:
    """Publication use cases for ONE project namespace."""

    def __init__(self, *, artifacts, audit, namespace, ownership, progress,
                 approvals, secrets, provider=None, google_client_factory=None):
        #: `ports.ArtifactRepository` -- metadata, intent, manifest and every
        #: artifact the gates read (render, ingest, script, hot_moments, plan).
        self._artifacts = artifacts
        #: `ports.AuditSink`.
        self._audit = audit
        #: `ports.ProjectNamespaceStore` -- resolves the OAuth token path and
        #: the render directory that backs `source_video_path`.
        self._namespace = namespace
        self._ownership = ownership
        #: `ports.ProgressFactory`.
        self._progress = progress
        #: `ports.ApprovalQuery` -- read-only content-approval chain, consulted
        #: by `create_intent`. Implemented by `ApprovalService`; never imported.
        self._approvals = approvals
        #: Secret RESOLVER, not a secret: `credential("google_api").configured`
        #: is the only capability consulted, and only when no client factory is
        #: injected.
        self._secrets = secrets
        #: Injected `local_pipeline.publish.PublishProvider`. `None` builds the
        #: real YouTube provider LAZILY so constructing the service never reads
        #: a token file or touches the network.
        self._provider = provider
        #: Injected Google client factory for metadata generation; the secret
        #: resolver fallback is only used when no factory exists.
        self._google_client_factory = google_client_factory

    # ----------------------------------------------------- provider + oauth
    def provider(self):
        """The publication provider, built lazily.

        Constructing it reads no secret value and performs no network call --
        the token file is only opened when an operation actually needs it.
        """
        if self._provider is None:
            token_path = self._namespace.resolve(
                self._ownership, RUNTIME_DIRNAME, "youtube_oauth_token.json",
                create_parent=True)
            self._provider = publish_youtube.YouTubeProvider(
                token_store=publish_youtube.OAuthTokenStore(path=token_path))
        return self._provider

    def credential_state(self) -> dict:
        try:
            return self.provider().describe_credential_state()
        except Exception as exc:  # noqa: BLE001 - a settings panel must not crash
            return {"platform": publish_module.PLATFORM_YOUTUBE, "configured": False,
                    "authorized": False, "client_secrets_error": str(exc)}

    def authorization_url(self, *, redirect_uri: str, state: str = "") -> str:
        return self.provider().authorization_url(redirect_uri, state=state)

    def complete_authorization(self, *, code: str, redirect_uri: str,
                               actor="console") -> dict:
        state = self.provider().complete_authorization(code, redirect_uri)
        # `state` carries only channel identity and scopes -- never a token.
        self._audit.record("artifact.written", actor=actor,
                           detail={"youtube_authorized": bool(state.get("authorized")),
                                   "channel_id": state.get("channel_id")})
        return state

    # ------------------------------------------------- publication metadata
    def generate_metadata(self, *, job_id=None, actor="console") -> dict:
        tracker = self._progress.create("metadata_generation", job_id=job_id)
        tracker.start("Metadata uretimi basladi")
        try:
            return self._generate_metadata(tracker, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _generate_metadata(self, tracker, *, job_id, actor) -> dict:
        tracker.stage("preparation", "Kaynak metin toplaniyor")
        script = self._artifacts.load_artifact("selected_script")["data"] or {}
        hot = self._artifacts.load_artifact("hot_moments")["data"] or {}
        plan_decision = ((self._artifacts.load_artifact("processing_plan")["data"] or {})
                         .get("decision") or {})
        ingest = self._artifacts.load_artifact("ingest_manifest")["data"]

        script_text = script.get("script_text") or ""
        transcript_excerpt = ""
        if not script_text:
            # `direct_clip_series` produces no script, so the transcript of the
            # analyzed clip is the only honest input.
            segments = (hot.get("transcript") or {}).get("segments") or []
            transcript_excerpt = " ".join(
                str(segment.get("text") or "") for segment in segments[:80]).strip()
        if not script_text and not transcript_excerpt:
            raise PipelineServiceError(
                "metadata uretimi icin script veya transcript gerekli; once analiz "
                "veya script fazini calistir. Alternatif olarak metadatayi elle yazabilirsin.")

        client = self._build_google_client()
        if client is None:
            raise PipelineServiceError(
                "Gemini kimlik bilgisi yok; metadata uretilemez. Metadatayi elle "
                "yazabilirsin. " + google_ai.credential_help_text("google_api"))

        tracker.stage("request", "Gemini yaniti bekleniyor")
        artifact = metadata_studio.generate_metadata(
            script_text=script_text, transcript_excerpt=transcript_excerpt,
            source_title=(ingest or {}).get("title") or "",
            target_language=plan_decision.get("content_language") or "tr-TR",
            is_short=True, theory_angle=plan_decision.get("theory_angle") or "",
            client=client,
        )
        tracker.stage("attribution", "Kaynak atfi uygulaniyor")
        artifact = metadata_studio.apply_attribution(artifact, ingest)
        self._artifacts.write_artifact("publication_metadata", artifact,
                                       job_id=job_id, actor=actor)
        self._audit.record("provider.call", actor=actor, job_id=job_id,
                           detail={"phase": "metadata_generation",
                                   "provider": artifact.get("provider"),
                                   "model": artifact.get("model")})
        if artifact.get("status") in ("failed", "insufficient_data"):
            tracker.fail(artifact.get("status_reason") or "metadata uretilemedi")
        else:
            tracker.finish(message=f"{len(artifact['title_variants'])} baslik varyanti")
        return artifact

    def write_manual_metadata(self, *, title: str, description: str = "", tags=None,
                              actor="console") -> dict:
        plan_decision = ((self._artifacts.load_artifact("processing_plan")["data"] or {})
                         .get("decision") or {})
        artifact = metadata_studio.build_manual_metadata(
            title=title, description=description, tags=tags or [],
            language=plan_decision.get("content_language") or "tr-TR",
        )
        artifact = metadata_studio.apply_attribution(
            artifact, self._artifacts.load_artifact("ingest_manifest")["data"])
        self._artifacts.write_artifact("publication_metadata", artifact, actor=actor)
        return artifact

    def select_metadata(self, *, title_index: int, description=None,
                        tags=None, category_id=None, actor="console") -> dict:
        loaded = self._artifacts.load_artifact("publication_metadata", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        artifact = metadata_studio.select_metadata(
            loaded["data"], title_index, description_override=description,
            tags_override=tags, category_id=category_id)
        self._artifacts.write_artifact("publication_metadata", artifact, actor=actor)
        return artifact

    def approve_metadata(self, *, reviewed_sha256: str, approved_by: str,
                         notes: str = "") -> dict:
        loaded = self._artifacts.load_artifact("publication_metadata", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        try:
            approved = metadata_studio.approve_metadata(
                loaded["data"], reviewed_sha256, approved_by, notes)
        except ValueError as exc:
            self._audit.record(
                "approval.rejected", actor=approved_by,
                artifact=self._artifacts.artifact_relative("publication_metadata"),
                detail={"kind": "publication_metadata", "reason": str(exc)})
            raise PipelineServiceError(str(exc)) from None
        relative = self._artifacts.write_artifact(
            "publication_metadata", approved, actor=approved_by)
        self._audit.record("approval.granted", actor=approved_by, artifact=relative,
                           detail={"kind": "publication_metadata",
                                   "approved_content_sha256":
                                       approved["metadata_approval"]
                                       ["approved_selection_sha256"]})
        return approved

    # -------------------------------------------------------- publish intent
    def create_intent(self, *, platform=publish_module.PLATFORM_YOUTUBE,
                      visibility=publish_module.VISIBILITY_PRIVATE,
                      publish_at=None, made_for_kids=False,
                      account_ref="", actor="console") -> dict:
        render = self._artifacts.load_artifact("render_manifest", required=True)
        metadata = self._artifacts.load_artifact("publication_metadata", required=True)
        for loaded in (render, metadata):
            if loaded["problem"]:
                raise ArtifactProblem(loaded["problem"])

        # The render's own hash check is not enough: an upstream approval may have
        # been revoked since. The approval chain is the authority on that.
        chain = self._approvals.chain()
        render_state = chain.get("render") or {}
        if not render_state.get("effective_valid"):
            raise PipelineServiceError(
                "YAYIN ENGELLI: render onayi gecerli degil -- "
                + (render_state.get("reason") or "once Render Studio'da final onayi ver"))

        target = publish_module.PublishTarget(
            platform=platform, account_ref=account_ref, visibility=visibility,
            publish_at=publish_at, made_for_kids=made_for_kids)
        intent = publish_module.build_publish_intent(
            render["data"], metadata["data"], target,
            ingest_manifest=self._artifacts.load_artifact("ingest_manifest")["data"])
        relative = self._artifacts.write_artifact("publish_intent", intent, actor=actor)
        self._audit.record("artifact.written", actor=actor, artifact=relative,
                           detail={"platform": platform,
                                   "requested_visibility": visibility,
                                   "initial_visibility": publish_module.INITIAL_PRIVACY})
        return intent

    def approve_intent(self, *, reviewed_sha256: str, approved_by: str,
                       notes: str = "") -> dict:
        loaded = self._artifacts.load_artifact("publish_intent", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        try:
            approved = publish_module.approve_publish_intent(
                loaded["data"], reviewed_sha256, approved_by, notes)
        except ValueError as exc:
            self._audit.record(
                "approval.rejected", actor=approved_by,
                artifact=self._artifacts.artifact_relative("publish_intent"),
                detail={"kind": "publish_intent", "reason": str(exc)})
            raise PipelineServiceError(str(exc)) from None
        relative = self._artifacts.write_artifact(
            "publish_intent", approved, actor=approved_by)
        self._audit.record("approval.granted", actor=approved_by, artifact=relative,
                           detail={"kind": "publish_intent",
                                   "authorizes": "single private upload"})
        return approved

    # --------------------------------------------------------- private upload
    def run_upload(self, *, job_id=None, actor="console") -> dict:
        tracker = self._progress.create("publish_upload", job_id=job_id)
        tracker.start("Private yukleme basladi")
        try:
            return self._upload(tracker, job_id=job_id, actor=actor)
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    def _upload(self, tracker, *, job_id, actor) -> dict:
        tracker.stage("preparation", "Yayin onayi dogrulaniyor")
        loaded = self._artifacts.load_artifact("publish_intent", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        intent = loaded["data"]

        existing = self._artifacts.load_artifact("publish_manifest")["data"]
        if existing and (existing.get("upload") or {}).get("remote_id"):
            raise PipelineServiceError(
                "Bu proje icin zaten bir yukleme yapilmis "
                f"(id={existing['upload']['remote_id']}). Yeniden yayin icin yeni bir "
                "yayin niyeti olusturup onaylaman gerekir.")

        # Fail closed on any earlier attempt. An `upload_attempt` record means
        # an upload sequence was started and its remote outcome is unverified
        # (the manifest may never have been persisted); `upload_performed`
        # covers intents spent before the marker existed. The canonical
        # `execute_private_upload` refuses a spent intent too -- this check
        # runs first so the refusal can explain WHY a retry cannot re-upload.
        attempt = intent.get("upload_attempt") or {}
        if attempt or intent.get("upload_performed"):
            detail = ""
            if attempt.get("observed_remote_id"):
                detail = (
                    f" Saglayicidan gozlemlenen kimlik: "
                    f"{attempt['observed_remote_id']} -- YouTube Studio'da "
                    "dogrulayip gerekiyorsa sil, sonra yeni bir yayin niyeti olustur."
                )
            raise PipelineServiceError(
                "YUKLEME ENGELLI: bu yayin niyeti icin zaten bir yukleme denemesi "
                f"kaydedilmis (durum={attempt.get('status') or 'tamamlanmis'}, "
                f"tarih={attempt.get('attempted_at') or intent.get('upload_attempted_at')}). "
                "Uzaktaki sonuc dogrulanamadigi icin ayni niyetle otomatik ikinci "
                "yukleme yapilmaz; yeni bir yayin niyeti olusturup onaylaman gerekir."
                + detail)

        # The intent stores a namespace-relative path for display; the uploader
        # needs the real file, resolved through the storage guard.
        intent_for_upload = dict(intent)
        intent_for_upload["source_video_path"] = str(
            self._namespace.resolve(
                self._ownership,
                self._artifacts.artifact_relative("render_manifest")).parent
            / Path(intent["source_video_path"]).name
        ) if not Path(intent["source_video_path"]).is_absolute() else \
            intent["source_video_path"]

        def progress(sent, total):
            tracker.stage("upload", f"{sent}/{total} bayt gonderildi")

        # Resolve the provider BEFORE the marker: a lazy-build failure happens
        # before any remote side effect, so the intent stays unmarked and a
        # retry is allowed to try again.
        provider = self.provider()

        # Durable in-flight marker BEFORE the external side effect. If this
        # write fails, the upload never starts; after this point a crash can
        # never silently trigger a second upload of the same intent.
        intent["upload_attempt"] = {
            "attempted_at": now_iso(),
            "attempted_by": str(actor or "console")[:120],
            "job_id": job_id,
            "status": "in_flight",
        }
        self._artifacts.write_artifact("publish_intent", intent,
                                       job_id=job_id, actor=actor)

        ingest = self._artifacts.load_artifact("ingest_manifest")["data"]
        tracker.stage("upload", "YouTube resumable oturumu aciliyor")
        try:
            manifest = publish_module.execute_private_upload(
                intent_for_upload, provider=provider,
                ingest_manifest=ingest,
                uploaded_by=actor, progress_callback=progress,
            )
        except PermissionError:
            # Gate refusal happens BEFORE the provider call; annotate why the
            # attempt was refused so `publish_state` explains the spent intent.
            intent["upload_attempt"]["status"] = "refused"
            self._best_effort_attempt_update(intent, job_id=job_id, actor=actor)
            raise
        except Exception:
            # Any other exception here means the remote outcome is UNKNOWN:
            # bytes may already be on the platform. Record that honestly.
            intent["upload_attempt"]["status"] = "uncertain"
            self._best_effort_attempt_update(intent, job_id=job_id, actor=actor)
            raise
        tracker.stage("write", "Yayin manifesti yaziliyor")
        try:
            self._artifacts.write_artifact("publish_manifest", manifest,
                                           job_id=job_id, actor=actor)
        except Exception:
            # The remote side effect may already exist; preserve the observed
            # remote id as evidence for manual reconciliation. This annotates
            # what was observed -- it never fabricates a successful state.
            upload = manifest.get("upload") or {}
            intent["upload_attempt"]["status"] = "uncertain"
            if upload.get("remote_id"):
                intent["upload_attempt"]["observed_remote_id"] = upload["remote_id"]
            self._best_effort_attempt_update(intent, job_id=job_id, actor=actor)
            raise

        # One intent authorizes exactly one upload; mark it spent either way.
        intent["upload_performed"] = True
        intent["upload_attempted_at"] = now_iso()
        intent["upload_attempt"]["status"] = (
            "failed" if (manifest.get("upload") or {}).get("status") == "failed"
            else "completed")
        intent["upload_attempt"]["completed_at"] = now_iso()
        self._artifacts.write_artifact("publish_intent", intent,
                                       job_id=job_id, actor=actor)

        self._audit.record("provider.call", actor=actor, job_id=job_id,
                           detail={"phase": "publish_upload",
                                   "platform": manifest.get("platform"),
                                   "status": manifest["upload"].get("status"),
                                   "quota_units_estimated":
                                       manifest["upload"].get("quota_units_estimated")})
        if manifest["upload"].get("status") == "failed":
            tracker.fail(manifest["upload"].get("error")
                         or manifest["upload"].get("status_reason") or "yukleme basarisiz")
        else:
            tracker.finish(message="Video PRIVATE olarak yuklendi; gorunurluk onayi bekliyor")
        return manifest

    # -------------------------------------------------------------- visibility
    def approve_visibility(self, *, target_visibility: str, reviewed_remote_id: str,
                           approved_by: str, publish_at=None, notes: str = "") -> dict:
        loaded = self._artifacts.load_artifact("publish_manifest", required=True)
        if loaded["problem"]:
            raise ArtifactProblem(loaded["problem"])
        try:
            approved = publish_module.approve_visibility(
                loaded["data"], target_visibility, reviewed_remote_id, approved_by,
                publish_at=publish_at, notes=notes)
        except ValueError as exc:
            self._audit.record(
                "approval.rejected", actor=approved_by,
                artifact=self._artifacts.artifact_relative("publish_manifest"),
                detail={"kind": "visibility", "reason": str(exc)})
            raise PipelineServiceError(str(exc)) from None
        relative = self._artifacts.write_artifact(
            "publish_manifest", approved, actor=approved_by)
        self._audit.record("approval.granted", actor=approved_by, artifact=relative,
                           detail={"kind": "visibility",
                                   "target_visibility": target_visibility,
                                   "publish_at": publish_at})
        return approved

    def apply_visibility(self, *, job_id=None, actor="console") -> dict:
        tracker = self._progress.create("visibility_change", job_id=job_id)
        tracker.start("Gorunurluk degisikligi basladi")
        try:
            loaded = self._artifacts.load_artifact("publish_manifest", required=True)
            if loaded["problem"]:
                raise ArtifactProblem(loaded["problem"])
            tracker.stage("apply", "Saglayicida gorunurluk uygulaniyor")
            updated = publish_module.execute_visibility_change(
                loaded["data"], provider=self.provider())
            tracker.stage("write", "Manifest guncelleniyor")
            relative = self._artifacts.write_artifact(
                "publish_manifest", updated, job_id=job_id, actor=actor)
            self._audit.record("provider.call", actor=actor, job_id=job_id,
                               artifact=relative,
                               detail={"phase": "visibility_change",
                                       "visibility": updated.get("current_visibility")})
            if updated.get("status") == "failed":
                tracker.fail(updated.get("status_reason") or "gorunurluk uygulanamadi")
            else:
                tracker.finish(message="Gorunurluk uygulandi")
            return updated
        except Exception as exc:  # noqa: BLE001
            tracker.fail(f"{type(exc).__name__}: {exc}")
            raise

    # ----------------------------------------------------------------- state
    def publish_state(self) -> dict:
        """View model for the publish screen: what is ready, what is blocking."""
        render = self._artifacts.load_artifact("render_manifest")
        metadata = self._artifacts.load_artifact("publication_metadata")
        intent = self._artifacts.load_artifact("publish_intent")
        manifest = self._artifacts.load_artifact("publish_manifest")
        ingest = self._artifacts.load_artifact("ingest_manifest")

        preconditions = publish_module.validate_publish_preconditions(
            render["data"] or {}, metadata["data"], ingest["data"])
        intent_state = (publish_module.check_publish_intent_approval(intent["data"])
                        if intent["data"] else
                        {"approved": False, "valid": False,
                         "reason": "yayin niyeti olusturulmadi"})
        return {
            "credential": self.credential_state(),
            "preconditions": preconditions,
            "publishable": not preconditions,
            "metadata": metadata["data"],
            "metadata_problem": metadata["problem"],
            "intent": intent["data"],
            "intent_problem": intent["problem"],
            "intent_approval": intent_state,
            "manifest": manifest["data"],
            "manifest_problem": manifest["problem"],
            "initial_visibility": publish_module.INITIAL_PRIVACY,
            "visibility_values": list(publish_module.VISIBILITY_VALUES),
            "planned_platforms": list(publish_module.PLANNED_PLATFORMS),
            "registered_platforms": [publish_module.PLATFORM_YOUTUBE],
        }

    # ------------------------------------------------------------- helpers
    def _best_effort_attempt_update(self, intent, *, job_id, actor) -> None:
        """Crash-path evidence: if this annotation write also fails, the
        original exception is still the one that propagates -- the attempt
        marker already persisted, so the blocked state survives regardless."""
        try:
            self._artifacts.write_artifact("publish_intent", intent,
                                           job_id=job_id, actor=actor)
        except Exception:  # noqa: BLE001
            pass

    def _build_google_client(self):
        if callable(self._google_client_factory):
            return self._google_client_factory()
        credential = self._secrets.credential("google_api")
        if not credential.configured:
            return None
        return google_ai.build_google_client(credential)
