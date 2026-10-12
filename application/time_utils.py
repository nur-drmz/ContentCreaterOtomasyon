"""Timestamp formatting shared by the application layer.

One implementation so a project record, a source descriptor and an audit detail
can never disagree about what "now" looks like. UTC and ISO-8601 with offset,
matching every timestamp already persisted in this repository.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
