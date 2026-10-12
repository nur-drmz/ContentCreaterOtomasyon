"""Local `.revisions/` snapshot adapter -- `ports.RevisionArchiver`.

WHY THIS LIVES HERE
-------------------
Before Milestone 11 these two helpers were `PipelineService._archive_revision`
and `._render_slot_archive_paths` -- private façade methods shared by rewind,
render-profile edits, review flows and `save_clip_bounds`. Milestone 11 moved
the clip-bounds mutation into `MomentService`, which is forbidden from
receiving façade-owned callables. The logic itself is pure namespace
mechanics (move files into a snapshot, write a manifest), so it is an
infrastructure adapter behind `ports.RevisionArchiver`, shared by the façade
and the service with exactly one implementation.

The module imports `creator_console.storage.atomic_write_json` and
`creator_console.ids.new_id` -- the same documented transitional
infrastructure→creator_console edge `LocalArtifactRepository` and
`LocalReviewGate` already carry. Because it binds `atomic_write_json` at
module level, it is registered in `creator_console.faults._ATOMIC_WRITE_HOLDERS`
so injected disk-full/partial-write faults reach it.
"""
from __future__ import annotations

import os

from application import artifact_catalog
from application.time_utils import now_iso
from creator_console import render_review as render_review_module
from creator_console.ids import new_id
from creator_console.storage import atomic_write_json

RESULTS_DIRNAME = artifact_catalog.RESULTS_DIRNAME
ARTIFACTS = artifact_catalog.ARTIFACTS

#: Slot 1 keeps these at the historical top-level paths, outside the render
#: directory, so they were safe there by accident. Slots 2+ keep them INSIDE
#: `results/render/output_NN/`, where archiving the directory wholesale
#: silently destroyed the moment selection and the approved caption text --
#: turning "change the caption theme" into "lose your moment".
RENDER_ARCHIVE_PRESERVED = ("moment_selection.json", "moment_transcript.json")


class LocalRevisionArchive:
    """`ports.RevisionArchiver` backed by `.revisions/` inside the namespace."""

    RENDER_ARCHIVE_PRESERVED = RENDER_ARCHIVE_PRESERVED

    def __init__(self, *, namespace, ownership):
        #: `ports.ProjectNamespaceStore` -- path safety is its job.
        self._namespace = namespace
        self._ownership = ownership

    def archive_revision(self, paths, *, action: str, actor: str,
                         target: str | None = None) -> dict:
        """Move exact namespace-relative paths into a recoverable snapshot."""
        revision_id = new_id("revision")
        moved = []
        # Parents replace their children; accepting both would make the second
        # move look like missing data.  Shortest paths first makes this explicit.
        selected = []
        for relative in sorted(set(paths), key=lambda value: (value.count("/"), value)):
            if any(relative == parent or relative.startswith(parent + "/")
                   for parent in selected):
                continue
            selected.append(relative)
        for relative in selected:
            source = self._namespace.resolve(self._ownership, relative)
            if not source.exists():
                continue
            destination = self._namespace.resolve(
                self._ownership, ".revisions", revision_id, relative,
                create_parent=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append(relative)
        manifest = {
            "schema_version": 1,
            "artifact_type": "project_revision",
            **self._ownership.to_dict(),
            "revision_id": revision_id,
            "action": action,
            "target": target,
            "created_by": str(actor or "console")[:120],
            "created_at": now_iso(),
            "moved_paths": moved,
            "recoverable": True,
            "not_uploaded": True,
        }
        manifest_path = self._namespace.resolve(
            self._ownership, ".revisions", revision_id, "manifest.json",
            create_parent=True)
        atomic_write_json(manifest_path, manifest)
        return manifest

    def render_slot_archive_paths(self, slot: int = 1, *, preserve=None) -> list:
        """Exactly this slot's render OUTPUT -- nothing another Short owns, and
        nothing this Short needs in order to render again.

        Slot 1's render directory CONTAINS slot 2's (`results/render/output_02`),
        so archiving `results/render` wholesale would take every other Short with
        it. That is the slot leak `PER_SLOT_ARTIFACTS` exists to prevent, and it
        would be silent: the operator changes slot 1's caption theme and slot 3's
        approved video quietly moves into the archive.

        `preserve` names files to keep even though they sit in the slot's
        directory; pass an empty tuple to archive the transcript too, which is
        what a direct-mode clip-range edit has to do.
        """
        index = int(slot or 1)
        keep = RENDER_ARCHIVE_PRESERVED if preserve is None else tuple(preserve)
        paths = []
        if index != 1:
            root = self._namespace.resolve(
                self._ownership, RESULTS_DIRNAME, "render",
                f"output_{index:02d}")
            if root.is_dir():
                for child in sorted(root.iterdir()):
                    if child.name in keep:
                        continue
                    paths.append(
                        f"{RESULTS_DIRNAME}/render/output_{index:02d}/{child.name}")
            return paths
        root = self._namespace.resolve(
            self._ownership, RESULTS_DIRNAME, "render")
        if root.is_dir():
            for child in sorted(root.iterdir()):
                name = child.name
                if (child.is_dir() and name.startswith("output_")
                        and name[len("output_"):].isdigit()):
                    continue                      # another slot's Short
                if name in keep:
                    continue
                paths.append(f"{RESULTS_DIRNAME}/render/{name}")
        paths.append(render_review_module.RENDER_REVIEW_ARTIFACT)
        # A publish intent authorises ONE upload of ONE exact video hash, and
        # `create_publish_intent` reads slot 1's manifest -- so the intent belongs
        # to slot 1 and must not outlive slot 1's render.
        paths.append(ARTIFACTS["publish_intent"])
        return paths
