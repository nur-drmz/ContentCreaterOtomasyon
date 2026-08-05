"""Creator Console pre-alpha (M0.9).

A genuinely runnable, private, SINGLE-USER local web console over the canonical
`local_pipeline` modules. Launch it with:

    python -m creator_console

WHAT THIS IS
------------
A thin service + UI layer. Every pipeline decision, artifact contract, status
vocabulary and approval gate still lives in `local_pipeline`; this package adds
ownership, storage isolation, job persistence, audit events and an HTTP surface
around it. Notebook logic is NOT copied into the frontend -- the browser renders
server-built view models and never imports pipeline code.

WHAT THIS IS NOT
----------------
Not multi-tenant, not authenticated, not deployed, not billed, and it never
uploads or publishes anything. Those boundaries are DESIGNED FOR (ownership ids,
storage adapter, audit contract, pluggable job runner, server-side secret
references) but deliberately NOT faked: the UI reports unimplemented multi-user
capabilities as "planned", never as working.

STACK CHOICE
------------
Standard library only: `http.server.ThreadingHTTPServer` plus vanilla ES modules
and CSS custom properties. The project already ships a heavy scientific stack
(ffmpeg, whisper, scenedetect, librosa); adding a web framework and a Node build
toolchain to it would cost far more maintenance than a ~single-file router and a
build-step-free frontend. Threading is real, so concurrent-request and
duplicate-submission behaviour can be tested honestly.
"""
from __future__ import annotations

__all__ = ["__version__", "APP_NAME"]

__version__ = "0.1.0-pre-alpha"
APP_NAME = "Creator Console"
