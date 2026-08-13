from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from studio import worker
from studio.models import (
    AuditEvent,
    Job,
    NarrationScript,
    NarrationSentence,
    Project,
    SourceAsset,
    VoiceOver,
    VoiceSegment,
)
from studio.voice.providers import (
    PROVIDERS,
    VoiceProviderError,
    default_provider_key,
    get_provider,
    provider_choices,
)
from studio.voice.providers.base import SynthesisResult
from studio.voice.providers.local_disabled import LocalDisabledProvider


def create_project(title: str = "Voice project") -> Project:
    project = Project.objects.create(title=title, rights_accepted_at=timezone.now())
    SourceAsset.objects.create(
        project=project, relative_path="s.mp4", filename="s.mp4", size_bytes=1, container="mp4"
    )
    return project


# --- Provider interface / registry -----------------------------------------------------


def test_local_disabled_provider_never_calls_network_and_estimates_zero_cost():
    provider = LocalDisabledProvider()
    assert provider.estimate_cost("hello", {}) == Decimal("0")
    with pytest.raises(VoiceProviderError) as exc_info:
        provider.synthesize("hello", {})
    assert exc_info.value.code == "VOICE_PROVIDER_NOT_CONFIGURED"


def test_default_provider_key_is_local_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert default_provider_key() == "local_disabled"


def test_default_provider_key_is_elevenlabs_with_api_key(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    assert default_provider_key() == "elevenlabs"


def test_get_provider_raises_for_unknown_key():
    with pytest.raises(VoiceProviderError):
        get_provider("not_a_real_provider")


def test_provider_choices_lists_all_registered_providers():
    keys = {key for key, _label in provider_choices()}
    assert keys == set(PROVIDERS.keys())


def test_elevenlabs_provider_requires_api_key(monkeypatch):
    from studio.voice.providers.elevenlabs import ElevenLabsProvider

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    provider = ElevenLabsProvider()
    with pytest.raises(VoiceProviderError) as exc_info:
        provider.synthesize("hello", {})
    assert exc_info.value.code == "ELEVENLABS_API_KEY_MISSING"


def test_elevenlabs_provider_never_reaches_network_without_key(monkeypatch):
    """No API key means synthesize must fail before any urlopen call."""
    from studio.voice.providers import elevenlabs as elevenlabs_module

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with patch.object(elevenlabs_module.urllib.request, "urlopen") as mock_urlopen:
        with pytest.raises(VoiceProviderError):
            elevenlabs_module.ElevenLabsProvider().synthesize("hello", {})
        mock_urlopen.assert_not_called()


def test_elevenlabs_estimate_cost_scales_with_text_length(settings):
    from studio.voice.providers.elevenlabs import ElevenLabsProvider

    settings.ELEVENLABS_COST_PER_CHARACTER = "0.0001"
    provider = ElevenLabsProvider()
    assert provider.estimate_cost("a" * 100, {}) == Decimal("0.0100")
    assert provider.estimate_cost("a" * 10, {}) == Decimal("0.0010")


# --- worker.run_voiceover ---------------------------------------------------------------


def _voiceover_job(project: Project) -> Job:
    attempt = project.jobs.filter(job_type=Job.Type.GENERATE_VOICEOVER).count() + 1
    return Job.objects.create(
        project=project,
        job_type=Job.Type.GENERATE_VOICEOVER,
        status=Job.Status.RUNNING,
        idempotency_key=f"voiceover:{project.id}:{attempt}",
    )


class FakeProvider:
    key = "fake"
    display_name = "Fake"

    def __init__(self, cost: Decimal = Decimal("0.01")):
        self.cost = cost
        self.calls: list[str] = []

    def estimate_cost(self, text, voice_settings):
        return self.cost

    def synthesize(self, text, voice_settings):
        self.calls.append(text)
        return SynthesisResult(audio_bytes=b"fake-audio", duration_seconds=1.5, cost=self.cost)


@pytest.mark.django_db
def test_run_voiceover_fails_without_narration_script():
    project = create_project()
    job = _voiceover_job(project)
    result = worker.run_voiceover(job)
    assert result.status == Job.Status.FAILED
    assert result.failure_code == "NO_NARRATION_SCRIPT"


@pytest.mark.django_db
def test_run_voiceover_fails_without_voice_over_config():
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="Hello there.")
    NarrationSentence.objects.create(script=script, order_index=0, text="Hello there.")
    job = _voiceover_job(project)
    result = worker.run_voiceover(job)
    assert result.status == Job.Status.FAILED
    assert result.failure_code == "NO_VOICE_OVER_CONFIG"


@pytest.mark.django_db
def test_run_voiceover_generates_segments_and_logs_audit_events(tmp_path, settings):
    settings.GENERATED_MEDIA_ROOT = tmp_path
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="Hello there. Second line.")
    NarrationSentence.objects.create(script=script, order_index=0, text="Hello there.")
    NarrationSentence.objects.create(script=script, order_index=1, text="Second line.")
    voice_over = VoiceOver.objects.create(script=script, provider_key="fake", voice_settings={})
    job = _voiceover_job(project)

    fake_provider = FakeProvider()
    result = worker.run_voiceover(job, provider_factory=lambda _key: fake_provider)

    assert result.status == Job.Status.SUCCEEDED
    segments = list(VoiceSegment.objects.filter(voice_over=voice_over))
    assert len(segments) == 2
    assert all(segment.status == VoiceSegment.Status.READY for segment in segments)
    assert all(segment.audio_relative_path for segment in segments)
    assert fake_provider.calls == ["Hello there.", "Second line."]

    actions = list(AuditEvent.objects.filter(project=project).values_list("action", flat=True))
    assert "voice.generate.started" in actions
    assert actions.count("voice.synthesize") == 2


@pytest.mark.django_db
def test_run_voiceover_only_regenerates_changed_segment(tmp_path, settings):
    settings.GENERATED_MEDIA_ROOT = tmp_path
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="One. Two.")
    NarrationSentence.objects.create(script=script, order_index=0, text="One.")
    NarrationSentence.objects.create(script=script, order_index=1, text="Two.")
    voice_over = VoiceOver.objects.create(script=script, provider_key="fake", voice_settings={})

    first_provider = FakeProvider()
    worker.run_voiceover(_voiceover_job(project), provider_factory=lambda _key: first_provider)
    assert first_provider.calls == ["One.", "Two."]

    first_segment = VoiceSegment.objects.get(sentence__order_index=0, voice_over=voice_over)
    second_segment = VoiceSegment.objects.get(sentence__order_index=1, voice_over=voice_over)
    first_regenerated_at = first_segment.regenerated_at
    second_regenerated_at = second_segment.regenerated_at

    # Only the second sentence's text changes.
    sentence_two = NarrationSentence.objects.get(script=script, order_index=1)
    sentence_two.text = "Two changed."
    sentence_two.save(update_fields=["text"])

    second_provider = FakeProvider()
    worker.run_voiceover(_voiceover_job(project), provider_factory=lambda _key: second_provider)

    assert second_provider.calls == ["Two changed."]
    first_segment.refresh_from_db()
    second_segment.refresh_from_db()
    assert first_segment.regenerated_at == first_regenerated_at
    assert second_segment.regenerated_at != second_regenerated_at


@pytest.mark.django_db
def test_run_voiceover_second_run_with_no_changes_regenerates_nothing(tmp_path, settings):
    settings.GENERATED_MEDIA_ROOT = tmp_path
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="One.")
    NarrationSentence.objects.create(script=script, order_index=0, text="One.")
    VoiceOver.objects.create(script=script, provider_key="fake", voice_settings={})

    provider = FakeProvider()
    worker.run_voiceover(_voiceover_job(project), provider_factory=lambda _key: provider)
    assert provider.calls == ["One."]

    second_result = worker.run_voiceover(_voiceover_job(project), provider_factory=lambda _key: provider)
    assert second_result.status == Job.Status.SUCCEEDED
    assert provider.calls == ["One."]  # unchanged - not called again


@pytest.mark.django_db
def test_run_voiceover_aborts_before_calling_provider_when_over_cost_limit(settings):
    settings.VOICE_COST_LIMIT_USD = 0.01
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="Expensive sentence.")
    NarrationSentence.objects.create(script=script, order_index=0, text="Expensive sentence.")
    VoiceOver.objects.create(script=script, provider_key="fake", voice_settings={})

    expensive_provider = FakeProvider(cost=Decimal("5.00"))
    job = _voiceover_job(project)
    result = worker.run_voiceover(job, provider_factory=lambda _key: expensive_provider)

    assert result.status == Job.Status.FAILED
    assert result.failure_code == "VOICE_COST_LIMIT_EXCEEDED"
    assert expensive_provider.calls == []  # never actually called
    assert AuditEvent.objects.filter(project=project, action="voice.cost_limit_exceeded").exists()


@pytest.mark.django_db
def test_run_voiceover_marks_segment_failed_on_provider_error(tmp_path, settings):
    settings.GENERATED_MEDIA_ROOT = tmp_path
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="Bad sentence.")
    NarrationSentence.objects.create(script=script, order_index=0, text="Bad sentence.")
    voice_over = VoiceOver.objects.create(script=script, provider_key="fake", voice_settings={})

    class FailingProvider(FakeProvider):
        def synthesize(self, text, voice_settings):
            raise VoiceProviderError("PROVIDER_DOWN", "simulated failure")

    job = _voiceover_job(project)
    result = worker.run_voiceover(job, provider_factory=lambda _key: FailingProvider())

    assert result.status == Job.Status.FAILED
    assert result.failure_code == "PROVIDER_DOWN"
    segment = VoiceSegment.objects.get(voice_over=voice_over)
    assert segment.status == VoiceSegment.Status.FAILED
    assert AuditEvent.objects.filter(project=project, action="voice.synthesize.failed").exists()


# --- views -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_queue_voiceover_requires_server_side_approval():
    project = create_project()
    response = Client().post(
        reverse("studio:queue-voiceover", args=[project.id]),
        {"text": "Hello world.", "provider_key": "local_disabled"},  # no "approved"
    )
    assert response.status_code == 302
    assert Job.objects.filter(project=project, job_type=Job.Type.GENERATE_VOICEOVER).count() == 0
    assert VoiceOver.objects.count() == 0


@pytest.mark.django_db
def test_queue_voiceover_creates_job_and_audit_event_when_approved():
    project = create_project()
    response = Client().post(
        reverse("studio:queue-voiceover", args=[project.id]),
        {"text": "Hello world.", "provider_key": "local_disabled", "approved": "on"},
    )
    assert response.status_code == 302
    job = Job.objects.get(project=project, job_type=Job.Type.GENERATE_VOICEOVER)
    assert job.status == Job.Status.QUEUED
    assert AuditEvent.objects.filter(project=project, action="voice.generate.requested").exists()
    script = NarrationScript.objects.get(project=project)
    assert script.sentences.count() == 1


@pytest.mark.django_db
def test_queue_voiceover_preserves_unchanged_sentence_identity_across_edits():
    project = create_project()
    client = Client()
    client.post(
        reverse("studio:queue-voiceover", args=[project.id]),
        {"text": "One.\nTwo.", "provider_key": "local_disabled", "approved": "on"},
    )
    script = NarrationScript.objects.get(project=project)
    first_sentence_id = NarrationSentence.objects.get(script=script, order_index=0).id

    client.post(
        reverse("studio:queue-voiceover", args=[project.id]),
        {"text": "One.\nTwo changed.", "provider_key": "local_disabled", "approved": "on"},
    )
    script.refresh_from_db()
    assert NarrationSentence.objects.get(script=script, order_index=0).id == first_sentence_id
    assert NarrationSentence.objects.get(script=script, order_index=1).text == "Two changed."


@pytest.mark.django_db
def test_voice_segment_preview_404s_for_ungenerated_segment(tmp_path, settings):
    settings.GENERATED_MEDIA_ROOT = tmp_path
    project = create_project()
    script = NarrationScript.objects.create(project=project, text="One.")
    sentence = NarrationSentence.objects.create(script=script, order_index=0, text="One.")
    voice_over = VoiceOver.objects.create(script=script, provider_key="local_disabled", voice_settings={})
    segment = VoiceSegment.objects.create(voice_over=voice_over, sentence=sentence)

    response = Client().get(
        reverse("studio:voice-segment-preview", args=[project.id, segment.id])
    )
    assert response.status_code == 404
