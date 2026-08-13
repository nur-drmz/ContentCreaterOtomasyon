import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=160)),
                ("season", models.CharField(blank=True, max_length=40)),
                ("episode", models.CharField(blank=True, max_length=40)),
                ("known_characters", models.JSONField(blank=True, default=list)),
                ("focus_prompt", models.TextField(blank=True)),
                (
                    "target_duration_seconds",
                    models.PositiveSmallIntegerField(
                        default=45,
                        validators=[
                            django.core.validators.MinValueValidator(30),
                            django.core.validators.MaxValueValidator(60),
                        ],
                    ),
                ),
                ("rights_accepted_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="Job",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "job_type",
                    models.CharField(
                        choices=[
                            ("ANALYZE_SOURCE", "Analyze source"),
                            ("RERANK_MOMENTS", "Re-rank moments"),
                            ("PROPOSE_TIMELINE", "Propose timeline"),
                            ("RENDER_ROUGH_CUT", "Render rough cut"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("RUNNING", "Running"),
                            ("PAUSED", "Paused"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="QUEUED",
                        max_length=20,
                    ),
                ),
                ("current_stage", models.CharField(blank=True, max_length=80)),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("idempotency_key", models.CharField(max_length=160, unique=True)),
                ("failure_code", models.CharField(blank=True, max_length=80)),
                ("failure_detail", models.TextField(blank=True)),
                ("queued_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="jobs",
                        to="studio.project",
                    ),
                ),
            ],
            options={"ordering": ["-queued_at"]},
        ),
        migrations.CreateModel(
            name="SourceAsset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("relative_path", models.CharField(max_length=1024)),
                ("filename", models.CharField(max_length=255)),
                ("size_bytes", models.PositiveBigIntegerField()),
                ("duration_seconds", models.FloatField(blank=True, null=True)),
                ("container", models.CharField(blank=True, max_length=24)),
                ("content_hash", models.CharField(blank=True, max_length=64)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                (
                    "project",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source",
                        to="studio.project",
                    ),
                ),
            ],
        ),
    ]

