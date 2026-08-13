from django import forms
from django.conf import settings
from django.utils import timezone

from .models import NarrationScript, Project, SourceAsset
from .services import discover_library_videos, resolve_library_video
from .voice.providers import provider_choices


class ProjectCreateForm(forms.ModelForm):
    source_relative_path = forms.ChoiceField(label="Source video")
    known_characters_text = forms.CharField(
        label="Known characters",
        required=False,
        help_text="Separate names with commas.",
    )
    rights_accepted = forms.BooleanField(
        label="I confirm that I am responsible for the rights to use this source.",
    )

    class Meta:
        model = Project
        fields = [
            "title",
            "season",
            "episode",
            "focus_prompt",
            "target_duration_seconds",
        ]
        labels = {
            "focus_prompt": "Initial focus prompt",
            "target_duration_seconds": "Target duration",
        }
        help_texts = {
            "focus_prompt": "Optional. Example: Explain why Daemon chooses this moment.",
            "target_duration_seconds": "30–60 seconds; 45 seconds is recommended.",
        }
        widgets = {
            "focus_prompt": forms.Textarea(attrs={"rows": 4}),
            "target_duration_seconds": forms.NumberInput(attrs={"min": 30, "max": 60}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        videos = discover_library_videos()
        self.fields["source_relative_path"].choices = [
            (video.relative_path, f"{video.filename} · {video.size_label}")
            for video in videos
        ]
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field-control")

    def clean_source_relative_path(self) -> str:
        relative_path = self.cleaned_data["source_relative_path"]
        path = resolve_library_video(relative_path)
        if path.stat().st_size > settings.MAX_SOURCE_SIZE_BYTES:
            raise forms.ValidationError("This file exceeds the 20 GB Pre-Alpha limit.")
        return relative_path

    def save(self, commit=True):
        project = super().save(commit=False)
        project.known_characters = [
            name.strip()
            for name in self.cleaned_data["known_characters_text"].split(",")
            if name.strip()
        ]
        project.rights_accepted_at = timezone.now()
        if not commit:
            return project

        project.save()
        source_path = resolve_library_video(self.cleaned_data["source_relative_path"])
        SourceAsset.objects.create(
            project=project,
            relative_path=self.cleaned_data["source_relative_path"],
            filename=source_path.name,
            size_bytes=source_path.stat().st_size,
            container=source_path.suffix.removeprefix(".").lower(),
        )
        return project


class FocusPromptForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["focus_prompt"]
        labels = {"focus_prompt": "Focus prompt"}
        help_texts = {
            "focus_prompt": (
                "Re-ranks already-analyzed moments only; it never repeats "
                "transcription, detection or VLM enrichment."
            ),
        }
        widgets = {"focus_prompt": forms.Textarea(attrs={"rows": 3, "class": "field-control"})}


class NarrationScriptForm(forms.ModelForm):
    class Meta:
        model = NarrationScript
        fields = ["text"]
        labels = {"text": "Narration script"}
        help_texts = {
            "text": "One sentence per line or period-separated. You write this yourself — no AI script generation.",
        }
        widgets = {
            "text": forms.Textarea(attrs={"rows": 8, "class": "field-control"}),
        }


class VoiceGenerationForm(forms.Form):
    provider_key = forms.ChoiceField(label="Voice provider")
    approved = forms.BooleanField(
        required=True,
        label=(
            "I reviewed the text above, the provider/model, and the estimated "
            "cost, and I approve sending this text to the selected provider."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider_key"].choices = provider_choices()
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field-control")

