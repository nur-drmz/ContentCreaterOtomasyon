import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=160)
    season = models.CharField(max_length=40, blank=True)
    episode = models.CharField(max_length=40, blank=True)
    known_characters = models.JSONField(default=list, blank=True)
    focus_prompt = models.TextField(blank=True)
    target_duration_seconds = models.PositiveSmallIntegerField(
        default=45,
        validators=[MinValueValidator(30), MaxValueValidator(60)],
    )
    rights_accepted_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title


class SourceAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name="source",
    )
    relative_path = models.CharField(max_length=1024)
    filename = models.CharField(max_length=255)
    size_bytes = models.PositiveBigIntegerField()
    duration_seconds = models.FloatField(null=True, blank=True)
    container = models.CharField(max_length=24, blank=True)
    probe_metadata = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.filename


class Job(models.Model):
    class Type(models.TextChoices):
        ANALYZE_SOURCE = "ANALYZE_SOURCE", "Analyze source"
        RERANK_MOMENTS = "RERANK_MOMENTS", "Re-rank moments"
        PROPOSE_TIMELINE = "PROPOSE_TIMELINE", "Propose timeline"
        RENDER_ROUGH_CUT = "RENDER_ROUGH_CUT", "Render rough cut"
        GENERATE_VOICEOVER = "GENERATE_VOICEOVER", "Generate voice-over"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        PAUSED = "PAUSED", "Paused"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    job_type = models.CharField(max_length=32, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    current_stage = models.CharField(max_length=80, blank=True)
    progress = models.PositiveSmallIntegerField(default=0)
    idempotency_key = models.CharField(max_length=160, unique=True)
    failure_code = models.CharField(max_length=80, blank=True)
    failure_detail = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)
    attempt_count = models.PositiveIntegerField(default=0)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-queued_at"]

    def __str__(self) -> str:
        return f"{self.get_job_type_display()} · {self.get_status_display()}"


class JobCheckpoint(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="checkpoints")
    stage = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=Status.choices)
    result = models.JSONField(default=dict, blank=True)
    stage_config_hash = models.CharField(max_length=64)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=80, blank=True)
    failure_detail = models.TextField(blank=True)

    class Meta:
        ordering = ["started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "stage", "stage_config_hash"],
                name="unique_job_stage_config_checkpoint",
            )
        ]

    def __str__(self) -> str:
        return f"{self.stage} · {self.get_status_display()}"


class AnalysisRun(models.Model):
    """A persisted pass of the pipeline in docs/pre-alpha-local/03-media-ai-pipeline.md.

    This model only stores the *result* of an analysis run (or a clearly
    marked demo run seeded for interface testing). No transcript, scene,
    VLM or worker processing is implemented here.
    """

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="analysis_runs"
    )
    source_asset = models.ForeignKey(
        SourceAsset, on_delete=models.CASCADE, related_name="analysis_runs"
    )
    pipeline_version = models.CharField(max_length=40)
    config_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    current_stage = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AnalysisRun({self.pipeline_version}) · {self.get_status_display()}"

    def clean(self) -> None:
        super().clean()
        if self.source_asset_id and self.project_id:
            if self.source_asset.project_id != self.project_id:
                raise ValidationError(
                    {
                        "source_asset": (
                            "This source video belongs to a different project "
                            "than the analysis run being created."
                        )
                    }
                )


class TranscriptSegment(models.Model):
    class Source(models.TextChoices):
        EMBEDDED = "embedded", "Embedded subtitle"
        SIDECAR = "sidecar", "Sidecar SRT"
        WHISPER = "whisper", "Whisper transcription"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="transcript_segments"
    )
    start_ts = models.FloatField()
    end_ts = models.FloatField()
    text = models.TextField(blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    confidence = models.FloatField(null=True, blank=True)
    language = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["start_ts"]
        constraints = [
            models.CheckConstraint(
                condition=Q(start_ts__lt=F("end_ts")),
                name="transcript_segment_start_before_end",
            ),
        ]

    def __str__(self) -> str:
        return f"TranscriptSegment({self.start_ts:.1f}-{self.end_ts:.1f})"


class Scene(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="scenes"
    )
    start_ts = models.FloatField()
    end_ts = models.FloatField()
    deterministic_features = models.JSONField(default=dict, blank=True)
    vlm_features = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["start_ts"]
        constraints = [
            models.CheckConstraint(
                condition=Q(start_ts__lt=F("end_ts")),
                name="scene_start_before_end",
            ),
        ]

    def __str__(self) -> str:
        return f"Scene({self.start_ts:.1f}-{self.end_ts:.1f})"


class HotMoment(models.Model):
    class SpoilerLevel(models.TextChoices):
        LOW = "Low", "Low"
        MEDIUM = "Medium", "Medium"
        HIGH = "High", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="hot_moments"
    )
    start_ts = models.FloatField()
    end_ts = models.FloatField()
    core_start_ts = models.FloatField()
    core_end_ts = models.FloatField()
    base_score = models.PositiveSmallIntegerField(validators=[MaxValueValidator(100)])
    score_breakdown = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True)
    characters = models.JSONField(default=list, blank=True)
    spoiler_level = models.CharField(max_length=10, choices=SpoilerLevel.choices)
    reasons = models.JSONField(default=list, blank=True)
    transcript_evidence = models.TextField(blank=True)
    is_demo = models.BooleanField(
        default=False,
        help_text="True for moments created by the demo seeder rather than a real analysis run.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-base_score"]
        constraints = [
            models.CheckConstraint(
                condition=Q(start_ts__lt=F("end_ts")),
                name="hot_moment_start_before_end",
            ),
            models.CheckConstraint(
                condition=Q(core_start_ts__lt=F("core_end_ts")),
                name="hot_moment_core_start_before_core_end",
            ),
            models.CheckConstraint(
                condition=Q(core_start_ts__gte=F("start_ts")) & Q(core_end_ts__lte=F("end_ts")),
                name="hot_moment_core_within_range",
            ),
            models.CheckConstraint(
                condition=Q(base_score__gte=0) & Q(base_score__lte=100),
                name="hot_moment_score_between_0_and_100",
            ),
            models.CheckConstraint(
                condition=Q(spoiler_level__in=["Low", "Medium", "High"]),
                name="hot_moment_spoiler_level_enum",
            ),
        ]

    def __str__(self) -> str:
        return f"HotMoment({self.start_ts:.1f}-{self.end_ts:.1f}, score={self.base_score})"


class MomentSelection(models.Model):
    class Decision(models.TextChoices):
        UNDECIDED = "UNDECIDED", "Undecided"
        SELECTED = "SELECTED", "Selected"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="moment_selections"
    )
    hot_moment = models.ForeignKey(
        HotMoment, on_delete=models.CASCADE, related_name="selections"
    )
    decision = models.CharField(
        max_length=10, choices=Decision.choices, default=Decision.UNDECIDED
    )
    order_index = models.PositiveIntegerField(default=0)
    trim_in = models.FloatField(null=True, blank=True)
    trim_out = models.FloatField(null=True, blank=True)
    locked = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order_index", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "hot_moment"],
                name="unique_selection_per_project_and_moment",
            ),
            models.CheckConstraint(
                condition=Q(trim_in__isnull=True)
                | Q(trim_out__isnull=True)
                | Q(trim_in__lt=F("trim_out")),
                name="moment_selection_trim_in_before_trim_out",
            ),
        ]

    def __str__(self) -> str:
        return f"MomentSelection({self.hot_moment_id}) · {self.get_decision_display()}"

    def clean(self) -> None:
        super().clean()
        if self.hot_moment_id and self.project_id:
            moment_project_id = self.hot_moment.analysis_run.project_id
            if moment_project_id != self.project_id:
                raise ValidationError(
                    "This moment does not belong to the selected project."
                )


class FocusPromptRun(models.Model):
    """A re-ranking pass over an existing analysis run's Hot Moments.

    Per docs/pre-alpha-local/03-media-ai-pipeline.md §6, creating a focus
    prompt run must never repeat stages 1-9 (transcription, scene/audio/
    motion detection, VLM enrichment) - it only re-ranks already-stored
    HotMoment data.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="focus_prompt_runs"
    )
    analysis_run = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="focus_prompt_runs"
    )
    prompt_text = models.TextField()
    formula_version = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"FocusPromptRun({self.formula_version})"

    def clean(self) -> None:
        super().clean()
        if self.analysis_run_id and self.project_id:
            if self.analysis_run.project_id != self.project_id:
                raise ValidationError(
                    {
                        "analysis_run": (
                            "This analysis run belongs to a different project "
                            "than the focus prompt run being created."
                        )
                    }
                )


class MomentRanking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    focus_prompt_run = models.ForeignKey(
        FocusPromptRun, on_delete=models.CASCADE, related_name="rankings"
    )
    hot_moment = models.ForeignKey(
        HotMoment, on_delete=models.CASCADE, related_name="rankings"
    )
    prompt_relevance = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)]
    )
    final_score = models.FloatField()
    rank = models.PositiveIntegerField()

    class Meta:
        ordering = ["rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["focus_prompt_run", "hot_moment"],
                name="unique_ranking_per_prompt_run_and_moment",
            ),
        ]

    def __str__(self) -> str:
        return f"MomentRanking(#{self.rank}, {self.final_score:.1f})"


class TimelineVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="timeline_versions"
    )
    version_number = models.PositiveIntegerField()
    parent_version = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_versions",
    )
    target_duration_seconds = models.PositiveSmallIntegerField()
    captions_enabled = models.BooleanField(default=False)
    clips = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version_number"],
                name="unique_timeline_version_per_project",
            ),
        ]

    def __str__(self) -> str:
        return f"TimelineVersion(v{self.version_number})"


class RenderArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timeline_version = models.ForeignKey(
        TimelineVersion, on_delete=models.CASCADE, related_name="render_artifacts"
    )
    relative_path = models.CharField(max_length=1024)
    width = models.PositiveSmallIntegerField()
    height = models.PositiveSmallIntegerField()
    codec = models.CharField(max_length=40)
    duration_seconds = models.FloatField()
    render_config_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"RenderArtifact({self.width}x{self.height}, {self.duration_seconds:.1f}s)"


class NarrationScript(models.Model):
    """User-authored narration text. No AI script generation - TTS input only.

    Per docs/pre-alpha-local/01-product-requirements.md, script generation is
    out of scope; this model exists solely so the user can type/paste text
    to be voiced by a TextToSpeechProvider (studio/voice/providers).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="narration_script"
    )
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"NarrationScript({self.project_id})"


class NarrationSentence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        NarrationScript, on_delete=models.CASCADE, related_name="sentences"
    )
    order_index = models.PositiveIntegerField()
    text = models.TextField()
    locked = models.BooleanField(default=False)

    class Meta:
        ordering = ["order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["script", "order_index"],
                name="unique_sentence_order_per_script",
            ),
        ]

    def __str__(self) -> str:
        return f"NarrationSentence(#{self.order_index})"


class VoiceOver(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        NarrationScript, on_delete=models.CASCADE, related_name="voice_overs"
    )
    provider_key = models.CharField(max_length=40)
    voice_settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VoiceOver({self.provider_key})"


class VoiceSegment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        GENERATING = "GENERATING", "Generating"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    voice_over = models.ForeignKey(VoiceOver, on_delete=models.CASCADE, related_name="segments")
    sentence = models.ForeignKey(
        NarrationSentence, on_delete=models.CASCADE, related_name="voice_segments"
    )
    audio_relative_path = models.CharField(max_length=1024, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    content_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text="Hash of sentence text + voice settings; unchanged hash skips regeneration.",
    )
    duration_seconds = models.FloatField(null=True, blank=True)
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    regenerated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sentence__order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["voice_over", "sentence"],
                name="unique_segment_per_voice_over_and_sentence",
            ),
        ]

    def __str__(self) -> str:
        return f"VoiceSegment({self.sentence_id}) · {self.get_status_display()}"


class AuditEvent(models.Model):
    """A local audit trail entry, primarily for external (network) calls.

    Per docs/pre-alpha-local/06-security-privacy.md §3, any optional cloud
    provider integration (the voice module is the first) must leave an audit
    record of what was sent externally. Logs the shape of the request, not
    full text bodies, per §6.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="audit_events")
    action = models.CharField(max_length=80)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AuditEvent({self.action})"
