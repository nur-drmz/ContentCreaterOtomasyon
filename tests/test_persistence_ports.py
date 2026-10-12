"""Conformance tests for the persistence ports and their local adapters.

The point of these tests is dependency inversion, not class hierarchy: the
existing local implementations must satisfy the port protocols WITHOUT inheriting
from them. If a future PostgreSQL adapter is added, it must pass the same checks.
"""
from __future__ import annotations

import pytest

from application.ports import (ArtifactRepository, AuditSink, JobRepository,
                               ObjectStore, ProjectNamespaceStore)
from creator_console.audit import AuditLog
from creator_console.ids import Ownership
from creator_console.jobs import JobStore
from creator_console.storage import LocalFilesystemStorage, StorageViolation
from infrastructure.persistence.local import (LocalArtifactRepository,
                                              LocalAuditSink,
                                              LocalJobRepository,
                                              LocalProjectNamespaceStore)
from infrastructure.storage.local import LocalObjectStore
from infrastructure.storage.local.object_store import ObjectNotFound


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "console_root")


@pytest.fixture
def ownership():
    return Ownership("usr_test", "wsp_test", "prj_test")


@pytest.fixture
def object_store(storage):
    return LocalObjectStore(storage)


# ---------------------------------------------------------------------------
# Structural conformance
# ---------------------------------------------------------------------------

@pytest.fixture
def artifact_repository(storage, ownership):
    return LocalArtifactRepository(storage, ownership, AuditLog(storage, ownership))


# ---------------------------------------------------------------------------
# Structural conformance
# ---------------------------------------------------------------------------

class TestPortConformance:
    def test_local_storage_satisfies_project_namespace_store(self, storage):
        assert isinstance(storage, ProjectNamespaceStore)

    def test_job_store_satisfies_job_repository(self, storage, ownership):
        assert isinstance(JobStore(storage, ownership), JobRepository)

    def test_audit_log_satisfies_audit_sink(self, storage, ownership):
        assert isinstance(AuditLog(storage, ownership), AuditSink)

    def test_local_object_store_satisfies_object_store(self, object_store):
        assert isinstance(object_store, ObjectStore)

    def test_local_artifact_repository_satisfies_artifact_repository(
            self, artifact_repository):
        # The REAL adapter: `EditorService` depends on this, not on the facade.
        assert isinstance(artifact_repository, ArtifactRepository)

    def test_pipeline_service_still_satisfies_artifact_repository(
            self, storage, ownership):
        # Structural compatibility is preserved -- the facade keeps its public
        # artifact methods as delegates to the injected repository.
        from creator_console.pipeline_service import PipelineService

        service = PipelineService(storage, ownership, None)
        assert isinstance(service, ArtifactRepository)

    def test_adapter_aliases_point_at_the_current_implementations(self):
        assert LocalProjectNamespaceStore is LocalFilesystemStorage
        assert LocalJobRepository is JobStore
        assert LocalAuditSink is AuditLog

    def test_ports_do_not_force_inheritance(self):
        # Structural typing is what keeps this milestone behaviour-free: the
        # existing classes conform without an inheritance edge being added.
        assert JobRepository not in JobStore.__mro__
        assert AuditSink not in AuditLog.__mro__
        assert ProjectNamespaceStore not in LocalFilesystemStorage.__mro__
        assert ObjectStore not in LocalObjectStore.__mro__
        assert ArtifactRepository not in LocalArtifactRepository.__mro__


# ---------------------------------------------------------------------------
# LocalObjectStore behaviour
# ---------------------------------------------------------------------------

class TestLocalObjectStoreWrites:
    def test_put_bytes_then_stat_reports_size_and_digest(self, object_store, ownership):
        import hashlib

        payload = b"video-bytes" * 100
        stat = object_store.put_bytes(ownership, "media/source.mp4", payload)
        assert stat["exists"] is True
        assert stat["size_bytes"] == len(payload)
        assert stat["sha256"] == hashlib.sha256(payload).hexdigest()

    def test_put_bytes_is_readable_back(self, object_store, ownership):
        object_store.put_bytes(ownership, "media/a.bin", b"\x00\x01\x02")
        with object_store.open(ownership, "media/a.bin") as handle:
            assert handle.read() == b"\x00\x01\x02"

    def test_put_file_publishes_a_local_render(self, object_store, ownership, tmp_path):
        rendered = tmp_path / "render_out.mp4"
        rendered.write_bytes(b"rendered" * 50)
        stat = object_store.put_file(ownership, "render/final.mp4", rendered)
        assert stat["exists"] is True
        assert stat["size_bytes"] == rendered.stat().st_size
        assert object_store.exists(ownership, "render/final.mp4")

    def test_put_file_missing_source_raises(self, object_store, ownership, tmp_path):
        with pytest.raises(ObjectNotFound):
            object_store.put_file(ownership, "render/x.mp4", tmp_path / "nope.mp4")

    def test_put_file_onto_itself_does_not_truncate(self, object_store, ownership):
        stored = object_store.put_bytes(ownership, "media/self.bin", b"keep-me" * 10)
        path = object_store.storage.resolve(ownership, "media/self.bin")
        again = object_store.put_file(ownership, "media/self.bin", path)
        assert again["size_bytes"] == stored["size_bytes"]
        assert path.read_bytes() == b"keep-me" * 10


class TestLocalObjectStoreReads:
    def test_stat_of_missing_object_reports_absent(self, object_store, ownership):
        stat = object_store.stat(ownership, "media/missing.mp4")
        assert stat["exists"] is False
        assert stat["size_bytes"] == 0
        assert stat["sha256"] is None

    def test_open_missing_object_raises_rather_than_returning_empty(self, object_store, ownership):
        with pytest.raises(ObjectNotFound):
            with object_store.open(ownership, "media/missing.mp4"):
                pass

    def test_exists_is_false_for_missing(self, object_store, ownership):
        assert object_store.exists(ownership, "media/missing.mp4") is False

    def test_delete_reports_whether_it_existed(self, object_store, ownership):
        object_store.put_bytes(ownership, "media/gone.bin", b"x")
        assert object_store.delete(ownership, "media/gone.bin") is True
        assert object_store.delete(ownership, "media/gone.bin") is False


class TestLocalObjectStoreIsolation:
    def test_traversal_key_is_rejected(self, object_store, ownership):
        with pytest.raises(StorageViolation):
            object_store.put_bytes(ownership, "../../escape.bin", b"x")

    def test_absolute_key_is_rejected(self, object_store, ownership):
        with pytest.raises(StorageViolation):
            object_store.put_bytes(ownership, "/etc/passwd", b"x")

    def test_stat_of_a_violating_key_reports_absent_instead_of_raising(self, object_store, ownership):
        assert object_store.stat(ownership, "../../escape.bin")["exists"] is False

    def test_exists_of_a_violating_key_is_false(self, object_store, ownership):
        assert object_store.exists(ownership, "../../escape.bin") is False

    def test_two_projects_do_not_share_objects(self, object_store):
        a = Ownership("usr_test", "wsp_test", "prj_a")
        b = Ownership("usr_test", "wsp_test", "prj_b")
        object_store.put_bytes(a, "media/x.bin", b"a-bytes")
        assert object_store.exists(b, "media/x.bin") is False

    def test_two_workspaces_do_not_share_objects(self, object_store):
        a = Ownership("usr_test", "wsp_a", "prj_1")
        b = Ownership("usr_test", "wsp_b", "prj_1")
        object_store.put_bytes(a, "media/x.bin", b"a-bytes")
        assert object_store.exists(b, "media/x.bin") is False


class TestMaterializeLifecycle:
    def test_materialize_yields_a_real_local_path_for_ffmpeg(self, object_store, ownership):
        object_store.put_bytes(ownership, "media/source.mp4", b"video" * 10)
        with object_store.materialize(ownership, "media/source.mp4") as path:
            assert path.is_file()
            assert path.read_bytes() == b"video" * 10

    def test_local_materialize_does_not_delete_the_stored_object(self, object_store, ownership):
        object_store.put_bytes(ownership, "media/source.mp4", b"video")
        with object_store.materialize(ownership, "media/source.mp4") as path:
            kept = path
        # The local adapter's working copy IS the stored file; it must survive.
        assert kept.is_file()
        assert object_store.exists(ownership, "media/source.mp4") is True

    def test_materialize_missing_object_raises(self, object_store, ownership):
        with pytest.raises(ObjectNotFound):
            with object_store.materialize(ownership, "media/missing.mp4"):
                pass

    def test_round_trip_materialize_then_put_file(self, object_store, ownership, tmp_path):
        """The full production lifecycle: fetch, process locally, publish back."""
        object_store.put_bytes(ownership, "media/source.mp4", b"source-bytes")
        with object_store.materialize(ownership, "media/source.mp4") as local_in:
            produced = tmp_path / "out.mp4"
            produced.write_bytes(local_in.read_bytes() + b"-rendered")
        stat = object_store.put_file(ownership, "render/out.mp4", produced)
        assert stat["exists"] is True
        with object_store.open(ownership, "render/out.mp4") as handle:
            assert handle.read() == b"source-bytes-rendered"
