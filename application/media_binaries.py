"""Lazy, cached ffmpeg/ffprobe binary resolution.

WHY THIS IS A SHARED OBJECT
---------------------------
`PipelineService.binaries()` used to own this: two attributes plus a lazy call
to `render_utils.resolve_ffmpeg_binaries()`. Application services that probe
media need the same pair, and injecting the façade's bound method would create
exactly the back-reference this refactor removes. So the resolver became a
small injectable object that the composition root builds once and both the
façade and the services share.

Resolution stays LAZY and CACHED, matching the previous behaviour: merely
constructing a service must not shell out looking for ffmpeg, and a request
that needs it twice must not pay twice. Explicitly configured binaries (from
`ConsoleConfig.ffmpeg_bin` / `ffprobe_bin`) are used as-is and never probed for.
"""
from __future__ import annotations

from local_pipeline import render_utils


class MediaBinaries:
    """The ffmpeg/ffprobe pair for one process, resolved at most once."""

    def __init__(self, ffmpeg_bin: str | None = None,
                 ffprobe_bin: str | None = None):
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin

    def resolve(self) -> tuple[str | None, str | None]:
        """Return `(ffmpeg_bin, ffprobe_bin)`, discovering them if needed.

        A missing ffprobe is NOT an error: `render_utils.probe_media` falls back
        to parsing `ffmpeg -i`, so an empty ffprobe path is a supported state.
        """
        if self.ffmpeg_bin is None or self.ffprobe_bin is None:
            self.ffmpeg_bin, self.ffprobe_bin = render_utils.resolve_ffmpeg_binaries()
        return self.ffmpeg_bin, self.ffprobe_bin
