"""Vercel entry point.

Vercel discovers `app` here and runs it as a serverless ASGI function. The only
thing this module adds to `cityfeed.api` is the two facts that are true on
Vercel and nowhere else: the database ships as a bundled read-only asset, and
nothing in the request path may write to disk.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The bundled database. Vercel's filesystem is read-only apart from /tmp, which
# is exactly what a read-only API needs -- there is no hosted database here
# because at 316K there is nothing to host.
os.environ.setdefault("CITYFEED_DB", str(ROOT / "data" / "cityfeed.db"))
os.environ.setdefault("CITYFEED_REGISTRY", str(ROOT / "sources"))
# Tells the app that crawling is somebody else's job here; see the 501 below.
os.environ.setdefault("CITYFEED_READONLY", "1")

from cityfeed.api import app  # noqa: E402

# Browsers on other origins need this; the data is public and read-only.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)
