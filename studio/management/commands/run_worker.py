import json

from django.core.management.base import BaseCommand, CommandError

from studio.worker import run_once


class Command(BaseCommand):
    help = "Claim at most one queued analysis job and run source preflight."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one eligible job and exit (the only supported Pre-Alpha mode).",
        )

    def handle(self, *args, **options):
        if not options["once"]:
            raise CommandError("Only the bounded --once worker mode is implemented.")
        try:
            job = run_once()
        except Exception as exc:
            raise CommandError(f"Unexpected worker crash: {type(exc).__name__}") from exc

        if job is None:
            self.stdout.write(json.dumps({"claimed": False, "status": "idle"}))
            return
        self.stdout.write(
            json.dumps(
                {
                    "claimed": True,
                    "job_id": str(job.id),
                    "status": job.status,
                    "stage": job.current_stage,
                    "failure_code": job.failure_code or None,
                }
            )
        )
