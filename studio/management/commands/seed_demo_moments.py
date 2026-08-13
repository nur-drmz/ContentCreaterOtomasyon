from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from studio import services
from studio.demo_data import DEMO_MOMENTS, DEMO_PIPELINE_VERSION, parse_timestamp
from studio.models import AnalysisRun, HotMoment, Project


class Command(BaseCommand):
    help = (
        "Seed a project with demo Hot Moments for interface testing only. "
        "This never analyzes, opens or modifies the source video file — it "
        "only writes illustrative HotMoment rows tagged is_demo=True. "
        "Running this command twice for the same project is a no-op."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "project_id",
            help="UUID of an existing project (must already have a registered source video).",
        )

    def handle(self, *args, **options):
        project_id = options["project_id"]
        try:
            project = Project.objects.select_related("source").get(id=project_id)
        except Project.DoesNotExist as exc:
            raise CommandError(f"No project found with id {project_id!r}.") from exc
        except ValueError as exc:
            raise CommandError(f"{project_id!r} is not a valid project id.") from exc

        if not hasattr(project, "source"):
            raise CommandError(
                "This project has no registered source video yet. "
                "Create the project through the New Project screen first."
            )

        existing_run = AnalysisRun.objects.filter(
            project=project, pipeline_version=DEMO_PIPELINE_VERSION
        ).first()
        if existing_run is not None:
            moment_count = existing_run.hot_moments.count()
            self.stdout.write(
                self.style.WARNING(
                    f"Demo moments already exist for project {project.title!r} "
                    f"({moment_count} moment(s)). Skipping — seeding is idempotent."
                )
            )
            return

        now = timezone.now()
        created = 0
        # Everything below is one atomic unit: if any HotMoment fails to be
        # created, the AnalysisRun (and any moments already written in this
        # call) is rolled back rather than left as a partial, orphaned run.
        with transaction.atomic():
            analysis_run = services.create_analysis_run(
                project=project,
                source_asset=project.source,
                pipeline_version=DEMO_PIPELINE_VERSION,
                config_hash="demo",
                status=AnalysisRun.Status.SUCCEEDED,
                current_stage="Demo seed (no real analysis performed)",
                started_at=now,
                completed_at=now,
            )

            for moment in DEMO_MOMENTS:
                start_ts = parse_timestamp(moment["start"])
                end_ts = parse_timestamp(moment["end"])
                trim = min(2.0, (end_ts - start_ts) * 0.2)
                HotMoment.objects.create(
                    analysis_run=analysis_run,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    core_start_ts=start_ts + trim,
                    core_end_ts=end_ts - trim,
                    base_score=moment["score"],
                    score_breakdown=moment["score_components"],
                    summary=moment["summary"],
                    characters=[moment["character"]],
                    spoiler_level=moment["spoiler"],
                    reasons=[moment["reason"], *moment["signals"]],
                    transcript_evidence=moment["transcript"],
                    is_demo=True,
                )
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {created} demo moment(s) for project {project.title!r}. "
                "These are clearly marked is_demo=True and did not touch the source video."
            )
        )
