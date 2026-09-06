"""Vercel entry point: the same FastAPI app as `arena serve`, over a read-only ledger snapshot.

Serverless filesystems cannot be written, so this deployment sets ARENA_READONLY=1 and points
ARENA_DB at `data/demo.db`. Writes (backing) answer 503 with an explanation instead of faking success.
"""

import os

os.environ.setdefault("ARENA_READONLY", "1")
os.environ.setdefault("ARENA_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo.db"))

from arena.api import app  # noqa: E402  (env must be set before the app reads it)
