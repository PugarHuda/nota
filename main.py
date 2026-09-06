"""Vercel entry point (framework-detected FastAPI): the same app as `arena serve`, over a read-only ledger snapshot.

Serverless filesystems cannot be written, so this sets ARENA_READONLY=1 and points ARENA_DB at
`data/demo.db`. Writes (backing) answer 503 with an explanation instead of faking success.
Local use is unchanged: `uv run arena serve`.
"""

import os

os.environ.setdefault("ARENA_READONLY", "1")
os.environ.setdefault("ARENA_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "demo.db"))

from arena.api import app  # noqa: E402,F401  (env must be set before the app reads it)
