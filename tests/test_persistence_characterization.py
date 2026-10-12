"""Characterization tests for the CURRENT local persistence behavior.

These tests exist to freeze behavior BEFORE the persistence ports are introduced.
They assert what the code does today, not what it should ideally do. If a later
refactor changes one of these, that is a behavior change and must be justified.

No provider is contacted, no network call is made, no credential is read.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from creator_console import jobs as jobs_module
from creator_console.audit import EVENT_TYPES, AuditLog
from creator_console.ids import Ownership
from creator_console.jobs import (ACTIVE_STATUSES, JOB_TYPES, MAX_ATTEMPTS,
                                  STATUS_CANCELLED, STATUS_COMPLETED,
                                  STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING,
                                  JobConflict, JobError, JobNotFound, JobStore)
from creator_console.storage import (LocalFilesystemStorage, StorageViolation,
                                     atomic_write_json, build_ownership)


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


# ---------------------------------------------------------------------------
# Storage: namespace isolation and path validation
# ---------------------------------------------------------------------------

class TestStorageNamespace:
    def test_namespace_layout_is_workspaces_projects(self, storage, ownership):
        root = storage.namespace_root(ownership)
        parts = root.parts
        assert "workspaces" in parts
        assert "wsp_test" in parts
        assert "projects" in parts
        assert "prj_test" in parts

    def test_workspace_root_excludes_project_segment(self, storage, ownership):
        ws = storage.workspace_root(ownership)
        assert ws.name == "wsp_test"
        assert "projects" not in ws.parts[len(ws.parts) - 1:]

    def test_two_workspaces_never_share_a_namespace(self, storage):
        a = storage.namespace_root(Ownership("usr_a", "wsp_a", "prj_1"))
        b = storage.namespace_root(Ownership("usr_b", "wsp_b", "prj_1"))
        assert a != b
        assert not str(a).startswith(str(b))

    def test_relative_segments_resolve_inside_namespace(self, storage, ownership):
        target = storage.resolve(ownership, "results/edl/edl.json")
        base = storage.namespace_root(ownership)
        assert str(target).startswith(str(base))

    def test_traversal_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "../../escape.json")

    def test_traversal_inside_a_longer_path_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "results/../../escape.json")

    def test_posix_absolute_path_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "/etc/passwd")

    def test_windows_drive_letter_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "C:/Windows/System32/config")

    def test_unc_path_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "//server/share/file.json")

    def test_backslash_traversal_is_rejected(self, storage, ownership):
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, r"..\..\escape.json")

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
    def test_symlink_escape_is_rejected(self, storage, ownership, tmp_path):
        base = storage.ensure_namespace(ownership)
        outside = tmp_path / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (base / "leak").symlink_to(outside, target_is_directory=True)
        with pytest.raises(StorageViolation):
            storage.resolve(ownership, "leak/secret.json")

    def test_empty_parts_return_the_namespace_root(self, storage, ownership):
        assert storage.resolve(ownership) == storage.namespace_root(ownership).resolve()

    def test_exists_returns_false_for_a_violation_instead_of_raising(self, storage, ownership):
        assert storage.exists(ownership, "../../escape.json") is False

    def test_relative_to_namespace_rejects_a_foreign_path(self, storage, ownership, tmp_path):
        with pytest.raises(StorageViolation):
            storage.relative_to_namespace(ownership, tmp_path / "elsewhere.json")

    def test_relative_to_namespace_returns_posix_form(self, storage, ownership):
        path = storage.resolve(ownership, "results/edl/edl.json", create_parent=True)
        assert storage.relative_to_namespace(ownership, path) == "results/edl/edl.json"


class TestStorageAtomicWrite:
    def test_write_json_then_read_json_roundtrips(self, storage, ownership):
        storage.write_json(ownership, "results/x.json", {"a": 1, "b": "tr-ıçğ"})
        assert storage.read_json(ownership, "results/x.json") == {"a": 1, "b": "tr-ıçğ"}

    def test_read_json_missing_returns_default(self, storage, ownership):
        assert storage.read_json(ownership, "results/none.json", default=None) is None

    def test_read_json_corrupt_raises_rather_than_defaulting(self, storage, ownership):
        path = storage.resolve(ownership, "results/bad.json", create_parent=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            storage.read_json(ownership, "results/bad.json")

    def test_atomic_write_json_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "nested" / "doc.json"
        atomic_write_json(target, {"ok": True})
        assert target.is_file()
        leftovers = [p.name for p in target.parent.iterdir() if p.name.startswith(".")]
        assert leftovers == []

    def test_atomic_write_json_serialization_shape(self, tmp_path):
        target = tmp_path / "doc.json"
        atomic_write_json(target, {"z": 1, "a": 2})
        text = target.read_text(encoding="utf-8")
        # Indent 2, non-ASCII preserved, trailing newline: the on-disk contract.
        assert text.endswith("\n")
        assert '"z": 1' in text

    def test_write_bytes_roundtrips(self, storage, ownership):
        storage.write_bytes(ownership, "media/a.bin", b"\x00\x01\x02")
        path = storage.resolve(ownership, "media/a.bin")
        assert path.read_bytes() == b"\x00\x01\x02"


class TestStorageTrashAndDelete:
    def test_trash_moves_namespace_and_returns_recovery_token(self, storage, ownership):
        storage.write_json(ownership, "project.json", {"title": "t"})
        token = storage.trash_namespace(ownership)
        assert token.startswith("prj_test__")
        assert not storage.namespace_root(ownership).exists()
        trashed = storage.workspace_root(ownership) / ".trash" / token
        assert (trashed / "project.json").is_file()

    def test_trash_requires_a_project_id(self, storage):
        with pytest.raises(StorageViolation):
            storage.trash_namespace(Ownership("usr_test", "wsp_test", None))

    def test_trash_missing_namespace_raises_filenotfound(self, storage, ownership):
        with pytest.raises(FileNotFoundError):
            storage.trash_namespace(ownership)

    def test_delete_namespace_requires_a_project_id(self, storage):
        with pytest.raises(StorageViolation):
            storage.delete_namespace(Ownership("usr_test", "wsp_test", None))


class TestBuildOwnership:
    def test_valid_ids_are_accepted(self):
        own = build_ownership("usr_1", "wsp_1", "prj_1")
        assert (own.user_id, own.workspace_id, own.project_id) == ("usr_1", "wsp_1", "prj_1")

    def test_project_id_is_optional(self):
        assert build_ownership("usr_1", "wsp_1").project_id is None

    def test_hostile_id_is_rejected(self):
        with pytest.raises(Exception):
            build_ownership("../../root", "wsp_1", "prj_1")


# ---------------------------------------------------------------------------
# Jobs: idempotency, claim, lease, heartbeat, retry, cancel
# ---------------------------------------------------------------------------

@pytest.fixture
def store(storage, ownership):
    return JobStore(storage, ownership)


class TestJobStoreConstruction:
    def test_project_id_is_required(self, storage):
        with pytest.raises(JobError):
            JobStore(storage, Ownership("usr_test", "wsp_test", None))

    def test_unknown_job_type_is_rejected(self, store):
        with pytest.raises(JobError):
            store.submit("not_a_job_type", idempotency_key="k1")

    def test_all_declared_job_types_are_submittable(self, store):
        for index, job_type in enumerate(JOB_TYPES):
            record, created = store.submit(job_type, idempotency_key=f"k{index}")
            assert created is True
            assert record["job_type"] == job_type


class TestJobIdempotency:
    def test_duplicate_key_while_queued_returns_the_same_job(self, store):
        first, created_first = store.submit("render", idempotency_key="same")
        second, created_second = store.submit("render", idempotency_key="same")
        assert created_first is True
        assert created_second is False
        assert first["job_id"] == second["job_id"]

    def test_duplicate_key_after_completion_returns_the_finished_job(self, store):
        first, _ = store.submit("render", idempotency_key="done")
        store.claim(first["job_id"], "worker-a")
        store.finish(first["job_id"], STATUS_COMPLETED)
        second, created = store.submit("render", idempotency_key="done")
        assert created is False
        assert second["job_id"] == first["job_id"]
        assert second["status"] == STATUS_COMPLETED

    def test_duplicate_key_after_failure_creates_a_new_job(self, store):
        first, _ = store.submit("render", idempotency_key="failed-key")
        store.claim(first["job_id"], "worker-a")
        store.finish(first["job_id"], STATUS_FAILED, error="boom", recoverable=True)
        second, created = store.submit("render", idempotency_key="failed-key")
        assert created is True
        assert second["job_id"] != first["job_id"]

    def test_different_keys_create_different_jobs(self, store):
        a, _ = store.submit("render", idempotency_key="a")
        b, _ = store.submit("render", idempotency_key="b")
        assert a["job_id"] != b["job_id"]


class TestJobClaim:
    def test_claim_moves_queued_to_running(self, store):
        record, _ = store.submit("render", idempotency_key="c1")
        claimed = store.claim(record["job_id"], "worker-a")
        assert claimed["status"] == STATUS_RUNNING
        assert claimed["claimed_by"] == "worker-a"
        assert claimed["lease_expires_at"] is not None
        assert claimed["attempts"] == 1

    def test_second_claimant_loses_while_the_lease_is_live(self, store):
        record, _ = store.submit("render", idempotency_key="c2")
        store.claim(record["job_id"], "worker-a")
        with pytest.raises(JobConflict):
            store.claim(record["job_id"], "worker-b")

    def test_claim_records_owner_process_and_host(self, store):
        record, _ = store.submit("render", idempotency_key="c3")
        claimed = store.claim(record["job_id"], "worker-a")
        assert claimed["owner_pid"] == os.getpid()
        assert claimed["owner_host"]

    def test_terminal_job_cannot_be_claimed(self, store):
        record, _ = store.submit("render", idempotency_key="c4")
        store.claim(record["job_id"], "worker-a")
        store.finish(record["job_id"], STATUS_COMPLETED)
        with pytest.raises(JobConflict):
            store.claim(record["job_id"], "worker-b")

    def test_claiming_an_unknown_job_raises_not_found(self, store):
        with pytest.raises(JobNotFound):
            store.claim("job_doesnotexist", "worker-a")


class TestJobHeartbeat:
    def test_heartbeat_extends_the_lease(self, store):
        record, _ = store.submit("render", idempotency_key="h1")
        claimed = store.claim(record["job_id"], "worker-a")
        before = claimed["lease_expires_at"]
        time.sleep(0.01)
        beat = store.heartbeat(record["job_id"], "worker-a")
        assert beat["lease_expires_at"] >= before

    def test_a_foreign_worker_cannot_heartbeat(self, store):
        record, _ = store.submit("render", idempotency_key="h2")
        store.claim(record["job_id"], "worker-a")
        with pytest.raises(JobConflict):
            store.heartbeat(record["job_id"], "worker-b")


class TestJobLeaseRecovery:
    def test_expired_lease_with_dead_owner_is_recovered_as_failed(self, storage, ownership):
        short = JobStore(storage, ownership, lease_seconds=0)
        record, _ = short.submit("render", idempotency_key="r1")
        claimed = short.claim(record["job_id"], "worker-gone")
        # Simulate a worker process that no longer exists.
        claimed["owner_pid"] = 2 ** 31 - 1
        short._save(claimed)
        recovered = short.recover_abandoned()
        assert [r["job_id"] for r in recovered] == [record["job_id"]]
        after = short.get(record["job_id"])
        assert after["status"] == STATUS_FAILED
        assert after["error_kind"] == "worker_lost"
        assert after["recoverable"] is True

    def test_a_live_owner_is_never_recovered(self, store):
        record, _ = store.submit("render", idempotency_key="r2")
        store.claim(record["job_id"], "worker-alive")  # owner_pid = this process
        assert store.recover_abandoned() == []
        assert store.get(record["job_id"])["status"] == STATUS_RUNNING

    def test_ignore_lease_reclaims_even_a_live_owner(self, store):
        record, _ = store.submit("render", idempotency_key="r3")
        store.claim(record["job_id"], "worker-alive")
        recovered = store.recover_abandoned(ignore_lease=True)
        assert [r["job_id"] for r in recovered] == [record["job_id"]]


class TestJobRetry:
    def test_failed_job_can_be_requeued(self, store):
        record, _ = store.submit("render", idempotency_key="rt1")
        store.claim(record["job_id"], "worker-a")
        store.finish(record["job_id"], STATUS_FAILED, error="boom", recoverable=True)
        again = store.request_retry(record["job_id"], requested_by="nur")
        assert again["status"] == STATUS_QUEUED
        assert again["retry_of"] == record["job_id"]
        assert again["error"] is None

    def test_running_job_cannot_be_retried(self, store):
        record, _ = store.submit("render", idempotency_key="rt2")
        store.claim(record["job_id"], "worker-a")
        with pytest.raises(JobConflict):
            store.request_retry(record["job_id"])

    def test_completed_job_cannot_be_retried(self, store):
        record, _ = store.submit("render", idempotency_key="rt3")
        store.claim(record["job_id"], "worker-a")
        store.finish(record["job_id"], STATUS_COMPLETED)
        with pytest.raises(JobConflict):
            store.request_retry(record["job_id"])

    def test_attempt_limit_blocks_further_retries(self, store):
        record, _ = store.submit("render", idempotency_key="rt4")
        for _ in range(MAX_ATTEMPTS):
            store.claim(record["job_id"], "worker-a")
            store.finish(record["job_id"], STATUS_FAILED, error="boom", recoverable=True)
            try:
                store.request_retry(record["job_id"])
            except JobConflict:
                break
        with pytest.raises(JobConflict):
            store.request_retry(record["job_id"])


class TestJobCancel:
    def test_queued_job_cancels_immediately(self, store):
        record, _ = store.submit("render", idempotency_key="x1")
        cancelled = store.request_cancel(record["job_id"], requested_by="nur")
        assert cancelled["status"] == STATUS_CANCELLED
        assert cancelled["error_kind"] == "cancelled_by_user"

    def test_running_job_stays_running_until_the_worker_acknowledges(self, store):
        record, _ = store.submit("render", idempotency_key="x2")
        store.claim(record["job_id"], "worker-a")
        asked = store.request_cancel(record["job_id"], requested_by="nur")
        assert asked["status"] == STATUS_RUNNING
        assert asked["cancel_requested_at"] is not None
        assert store.cancel_requested(record["job_id"]) is True

    def test_terminal_job_cannot_be_cancelled(self, store):
        record, _ = store.submit("render", idempotency_key="x3")
        store.claim(record["job_id"], "worker-a")
        store.finish(record["job_id"], STATUS_COMPLETED)
        with pytest.raises(JobConflict):
            store.request_cancel(record["job_id"])

    def test_cancel_requested_is_false_for_unknown_job(self, store):
        assert store.cancel_requested("job_unknown") is False


class TestJobPersistence:
    def test_status_survives_store_recreation(self, storage, ownership):
        first = JobStore(storage, ownership)
        record, _ = first.submit("render", idempotency_key="p1")
        first.claim(record["job_id"], "worker-a")

        second = JobStore(storage, ownership)
        assert second.get(record["job_id"])["status"] == STATUS_RUNNING

    def test_execution_status_is_distinct_from_artifact_status(self, store):
        record, _ = store.submit("render", idempotency_key="p2")
        store.claim(record["job_id"], "worker-a")
        done = store.finish(record["job_id"], STATUS_COMPLETED,
                            artifact_status="review_required")
        assert done["status"] == STATUS_COMPLETED
        assert done["artifact_status"] == "review_required"

    def test_job_record_is_stored_as_jobs_job_id_json(self, storage, ownership, store):
        record, _ = store.submit("render", idempotency_key="p3")
        path = storage.resolve(ownership, "jobs", f"{record['job_id']}.json")
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["job_id"] == record["job_id"]

    def test_corrupt_job_file_is_surfaced_not_skipped(self, storage, ownership, store):
        store.submit("render", idempotency_key="p4")
        bad = storage.resolve(ownership, "jobs", "job_corrupt.json", create_parent=True)
        bad.write_text("{not json", encoding="utf-8")
        listed = store.list()
        corrupt = [r for r in listed if r.get("corrupt")]
        assert len(corrupt) == 1
        assert corrupt[0]["status"] == STATUS_FAILED
        assert corrupt[0]["recoverable"] is True

    def test_finish_rejects_a_non_terminal_status(self, store):
        record, _ = store.submit("render", idempotency_key="p5")
        store.claim(record["job_id"], "worker-a")
        with pytest.raises(JobError):
            store.finish(record["job_id"], STATUS_RUNNING)

    def test_new_job_record_shape_is_stable(self, store):
        record, _ = store.submit("render", idempotency_key="p6")
        for field in ("schema_version", "artifact_type", "job_id", "user_id",
                      "workspace_id", "project_id", "job_type", "status",
                      "artifact_status", "idempotency_key", "params", "attempts",
                      "max_attempts", "submitted_by", "created_at", "updated_at",
                      "started_at", "finished_at", "claimed_by", "owner_pid",
                      "owner_host", "lease_expires_at", "error", "error_kind",
                      "recoverable", "cancel_requested_at", "cancel_requested_by",
                      "retry_of", "result", "artifacts", "not_uploaded"):
            assert field in record, field
        assert record["artifact_type"] == "console_job"
        assert record["schema_version"] == 1


# ---------------------------------------------------------------------------
# Audit: closed vocabulary, append-only, redaction, ordering
# ---------------------------------------------------------------------------

@pytest.fixture
def audit(storage, ownership):
    return AuditLog(storage, ownership)


class TestAuditVocabulary:
    def test_known_event_is_accepted(self, audit):
        event = audit.record("project.created", actor="nur")
        assert event["event_type"] == "project.created"

    def test_unknown_event_is_rejected(self, audit):
        with pytest.raises(ValueError):
            audit.record("project.definitely_not_an_event", actor="nur")

    def test_vocabulary_is_a_closed_set(self):
        assert isinstance(EVENT_TYPES, (set, frozenset))
        assert "project.created" in EVENT_TYPES
        assert "edl.revision_requested" in EVENT_TYPES


class TestAuditRecordShape:
    def test_event_carries_actor_and_ownership(self, audit, ownership):
        event = audit.record("project.created", actor="nur")
        assert event["actor"] == "nur"
        assert event["user_id"] == ownership.user_id
        assert event["workspace_id"] == ownership.workspace_id
        assert event["project_id"] == ownership.project_id

    def test_event_has_id_and_timestamp(self, audit):
        event = audit.record("project.created")
        assert event["event_id"].startswith("evt_")
        assert event["recorded_at"]

    def test_default_actor_is_system(self, audit):
        assert audit.record("project.created")["actor"] == "system"

    def test_job_id_and_artifact_are_carried(self, audit):
        event = audit.record("artifact.written", job_id="job_1",
                             artifact="results/edl/edl.json")
        assert event["job_id"] == "job_1"
        assert event["artifact"] == "results/edl/edl.json"


class TestAuditAppendOnly:
    def test_events_are_stored_as_jsonl(self, storage, ownership, audit):
        audit.record("project.created")
        audit.record("project.updated")
        path = storage.resolve(ownership, "audit_events.jsonl")
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "project.created"

    def test_events_are_returned_newest_first(self, audit):
        audit.record("project.created")
        audit.record("project.updated")
        listed = audit.events()
        assert listed[0]["event_type"] == "project.updated"
        assert listed[1]["event_type"] == "project.created"

    def test_writes_append_rather_than_replace(self, storage, ownership, audit):
        audit.record("project.created")
        AuditLog(storage, ownership).record("project.updated")
        assert len(audit.events()) == 2

    def test_limit_truncates_the_newest_window(self, audit):
        audit.record("project.created")
        audit.record("project.updated")
        assert len(audit.events(limit=1)) == 1

    def test_job_id_filter_applies(self, audit):
        audit.record("job.submitted", job_id="job_a")
        audit.record("job.submitted", job_id="job_b")
        assert [e["job_id"] for e in audit.events(job_id="job_a")] == ["job_a"]

    def test_missing_file_returns_empty_list(self, audit):
        assert audit.events() == []

    def test_truncated_line_does_not_hide_the_rest(self, storage, ownership, audit):
        audit.record("project.created")
        path = storage.resolve(ownership, "audit_events.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"event_type": "trunc')
        listed = audit.events()
        assert any(e.get("event_type") == "audit.unreadable_line" for e in listed)
        assert any(e.get("event_type") == "project.created" for e in listed)


class TestAuditRedaction:
    def test_registered_secret_value_is_scrubbed(self, audit):
        from creator_console.redaction import register_secret

        secret = "super-secret-token-value-1234567890"
        register_secret(secret)
        event = audit.record("settings.changed", detail={"token": secret})
        assert secret not in json.dumps(event, default=str)

    def test_key_shaped_value_is_scrubbed(self, audit):
        event = audit.record("settings.changed",
                             detail={"api_key": "AIzaSyDUMMYKEYVALUE1234567890abcdefgh"})
        assert "AIzaSyDUMMYKEYVALUE1234567890abcdefgh" not in json.dumps(event, default=str)
