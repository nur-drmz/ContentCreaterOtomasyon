"""Separate-process job worker.

    python -m creator_console.worker --storage-root ./.console_data

WHY THIS EXISTS
---------------
The pre-alpha console ran jobs in threads inside the HTTP process, and the
startup recovery pass assumed exactly that ("any job still `running` was claimed
by a process that no longer exists"). That assumption is fine for one process and
WRONG the moment a second one exists -- a restarting server would kill a healthy
external worker's job.

So this module makes the multi-process case real and testable:

  * a worker is an independent OS process with its own `worker_id`;
  * it claims jobs through the same compare-and-set + lease used by the thread
    runner, so two workers can never run one job;
  * it heartbeats while working, so a long analysis/render does not look
    abandoned;
  * `--claim-only` and `--exit-after` let a test kill it at a controlled point
    and then assert the recovery behaviour.

Startup recovery is now lease-aware rather than process-count-aware: a server
only reclaims jobs whose lease has actually expired, plus jobs it can prove were
its own (`owner_process` matches a pid that is gone). See
`JobStore.recover_abandoned`.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from .app import ConsoleApp, ConsoleConfig, _Heartbeat
from .ids import Ownership
from .jobs import ACTIVE_STATUSES, STATUS_QUEUED, JobConflict, JobError, JobStore

POLL_INTERVAL_SEC = 0.25


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m creator_console.worker",
        description="Creator Console icin ayri surecli is worker'i.")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--user-id", default="usr_local")
    parser.add_argument("--workspace-id", default="wsp_local")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SEC)
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="0 = sinirsiz; test icin N is sonrasi cik")
    parser.add_argument("--idle-timeout", type=float, default=0.0,
                        help="0 = sonsuza kadar bekle; test icin bos gecen sn sonrasi cik")
    parser.add_argument("--claim-only", action="store_true",
                        help="isi claim et ve CALISTIRMADAN bekle (kontrollu oldurme testi)")
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument("--ready-file", default=None,
                        help="claim sonrasi bu dosyayi yaz (test senkronizasyonu)")
    parser.add_argument("--demo-clients", action="store_true",
                        help="sahte saglayici istemcileri kullan (gercek API cagrisi YOK)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    worker_id = args.worker_id or f"proc-{os.getpid()}"

    client_factories = {}
    secret_getter = None
    if args.demo_clients:
        from . import fixtures
        client_factories = fixtures.fixture_client_factories()
        secret_getter = fixtures.fake_secret_getter()

    app = ConsoleApp(ConsoleConfig(
        storage_root=Path(args.storage_root),
        user_id=args.user_id, workspace_id=args.workspace_id,
        inline_jobs=True,          # this process IS the worker; no nested threads
        secret_getter=secret_getter,
        # A worker must never run the server's startup recovery: it would
        # reclaim jobs belonging to other live workers.
        extra={"skip_startup_recovery": True},
        **client_factories))

    ownership = Ownership(args.user_id, args.workspace_id, args.project_id)
    store = JobStore(app.storage, ownership)

    print(f"[worker {worker_id}] hazir | proje={args.project_id} pid={os.getpid()}",
          file=sys.stderr, flush=True)

    processed = 0
    idle_since = time.time()
    while True:
        record = _next_claimable(store)
        if record is None:
            if args.idle_timeout and (time.time() - idle_since) > args.idle_timeout:
                print(f"[worker {worker_id}] bosta zaman asimi, cikiliyor",
                      file=sys.stderr, flush=True)
                return 0
            time.sleep(args.poll_interval)
            continue

        idle_since = time.time()
        try:
            claimed = store.claim(record["job_id"], worker_id)
        except (JobConflict, JobError):
            continue  # another worker won the race; entirely normal

        print(f"[worker {worker_id}] claim edildi: {claimed['job_id']} "
              f"({claimed['job_type']})", file=sys.stderr, flush=True)
        if args.ready_file:
            Path(args.ready_file).write_text(
                f"{worker_id}\n{claimed['job_id']}\n{os.getpid()}\n", encoding="utf-8")

        if args.claim_only:
            # Hold the claim, heartbeat, and wait to be killed. This is how the
            # test suite produces a "worker died mid-job" state on demand.
            heartbeat = _Heartbeat(store, claimed["job_id"], worker_id,
                                   interval=args.heartbeat_interval).start()
            try:
                while True:
                    time.sleep(0.2)
            finally:
                heartbeat.stop()

        app._execute_job(store, claimed["job_id"], worker_id)
        processed += 1
        print(f"[worker {worker_id}] bitti: {claimed['job_id']} -> "
              f"{store.get(claimed['job_id'])['status']}", file=sys.stderr, flush=True)
        if args.max_jobs and processed >= args.max_jobs:
            return 0


def _next_claimable(store: JobStore):
    for record in sorted(store.list(statuses=(STATUS_QUEUED,)),
                         key=lambda r: r.get("created_at") or ""):
        return record
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
