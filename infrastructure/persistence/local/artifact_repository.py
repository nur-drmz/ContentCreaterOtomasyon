"""Local filesystem implementation of `application.ports.ArtifactRepository`.

WHY THIS EXISTS
---------------
In Milestone 1 the `ArtifactRepository` port was satisfied STRUCTURALLY by
`PipelineService`, because the artifact logic lived inside the façade. Once
application services depend on the port that binding is circular:

    PipelineService -> EditorService -> ArtifactRepository -> PipelineService

This adapter breaks the cycle: it implements the exact artifact semantics the
façade used to own -- honest reads, atomic writes, the shared catalog, and the
`artifact.written` audit record -- while depending only on the namespace store,
the audit sink and `application.artifact_catalog`. It must NEVER import
`creator_console.pipeline_service` (the architecture guards enforce this).

WHY THE FAçADE DELEGATES HERE
-----------------------------
`PipelineService.load_artifact`/`write_artifact` now delegate to this class so
the read/write rules exist in exactly ONE place. Duplicating them would let
the two copies drift; keeping them in the façade would keep the cycle alive.
"""
from __future__ import annotations

import json
from pathlib import Path

from application import artifact_catalog
from creator_console.storage import StorageViolation, atomic_write_json


class LocalArtifactRepository:
    """JSON artifact persistence for one project namespace.

    Scoped to a single `(storage, ownership)` pair, matching how the console
    builds one service object per project per request.
    """

    def __init__(self, storage, ownership, audit):
        #: `ports.ProjectNamespaceStore` -- path safety is its job.
        self.storage = storage
        self.ownership = ownership
        #: `ports.AuditSink` -- writes record `artifact.written` here.
        self.audit = audit

    # -------------------------------------------------------------- paths
    def artifact_relative(self, key: str, slot: int = 1) -> str:
        return artifact_catalog.artifact_relative(key, slot)

    def path(self, key: str, slot: int = 1) -> Path:
        return self.storage.resolve(self.ownership,
                                    self.artifact_relative(key, slot),
                                    create_parent=True)

    def relative(self, path) -> str:
        return self.storage.relative_to_namespace(self.ownership, path)

    # -------------------------------------------------------------- reads
    def load_artifact(self, key: str, *, required: bool = False,
                      slot: int = 1) -> dict:
        """Read one artifact and REPORT its trustworthiness.

        Returns {'present','data','problem','relative_path'}. A corrupt or
        incompatible artifact yields `data=None` and a human-actionable
        `problem` -- it is never repaired, guessed at, or replaced by a
        default.
        """
        relative = self.artifact_relative(key, slot)
        info = {"key": key, "slot": int(slot or 1), "present": False, "data": None,
                "problem": None, "relative_path": relative}
        try:
            path = self.storage.resolve(self.ownership, relative)
        except StorageViolation as exc:
            info["problem"] = f"gecersiz artefakt yolu: {exc}"
            return info
        if not path.is_file():
            if required:
                info["problem"] = f"{relative} bulunamadi"
            return info
        info["present"] = True

        raw = path.read_bytes()
        if not raw.strip():
            info["problem"] = f"{relative} BOS (0 bayt); artefakt kullanilamaz"
            return info
        try:
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            info["problem"] = f"{relative} UTF-8 olarak okunamadi (bozuk dosya)"
            return info
        except json.JSONDecodeError as exc:
            info["problem"] = (
                f"{relative} gecerli JSON degil (satir {exc.lineno}, sutun {exc.colno}); "
                "dosya yarim yazilmis veya bozulmus olabilir"
            )
            return info
        if not isinstance(data, dict):
            info["problem"] = f"{relative} beklenen JSON nesnesi degil ({type(data).__name__})"
            return info

        version = data.get("schema_version")
        if version is not None and version not in artifact_catalog.SUPPORTED_SCHEMA_VERSIONS:
            info["problem"] = (
                f"{relative} desteklenmeyen schema_version={version!r} "
                f"(desteklenen: {sorted(artifact_catalog.SUPPORTED_SCHEMA_VERSIONS)}); "
                "artefakt yeniden uretilmeli"
            )
            return info
        missing = [f for f in artifact_catalog.REQUIRED_FIELDS.get(key, ())
                   if f not in data]
        if missing:
            info["problem"] = f"{relative} zorunlu alanlari eksik: {missing}"
            return info
        if (data.get("status") == "production_ready"
                and key not in artifact_catalog.PRODUCTION_READY_ALLOWED):
            info["problem"] = (
                f"{relative} 'production_ready' iceriyor; bu proje hicbir artefakti "
                "otomatik olarak production_ready yapmaz, dosya elle degistirilmis olabilir"
            )
            return info

        info["data"] = data
        return info

    def load_artifact_path(self, relative: str):
        """Read one JSON artifact by RELATIVE path (not a registered key).

        Used by the automation engine for its own state file. Returns None when
        absent and None when unreadable -- a corrupt automation file must not
        wedge the console; the engine simply starts from a fresh idle state.
        """
        try:
            path = self.storage.resolve(self.ownership, relative)
        except StorageViolation:
            return None
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------- writes
    def write_artifact(self, key: str, data, *, job_id=None, actor="console",
                       slot: int = 1) -> str:
        path = self.path(key, slot)
        atomic_write_json(path, data)
        relative = self.relative(path)
        self.audit.record("artifact.written", actor=actor, job_id=job_id,
                          artifact=relative,
                          detail={"status": data.get("status")})
        return relative

    def write_artifact_path(self, relative: str, data) -> str:
        """Atomically write a JSON document at a namespace-relative path.

        For documents outside the artifact catalog (clip_bounds files live at
        `results/clip_bounds/output_NN.json`, not under a registered key).
        Deliberately emits NO `artifact.written` -- the caller records its own
        domain event, matching the behavior this path had when it wrote via
        `atomic_write_json` directly. Same containment + atomicity guarantees
        as the keyed write.
        """
        path = self.storage.resolve(self.ownership, relative,
                                    create_parent=True)
        atomic_write_json(path, data)
        return self.relative(path)
