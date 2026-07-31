"""Vercel entry point.

Vercel discovers `app` here and runs it as a serverless ASGI function. This
module adds the three things that are true on Vercel and nowhere else: the
database ships as a bundled read-only asset, nothing in the request path may
write to disk, and the URL the function receives is not the one the client
asked for.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent

# The bundled database. Vercel's filesystem is read-only apart from /tmp, which
# is exactly what a read-only API needs -- there is no hosted database here
# because at 316K there is nothing to host.
os.environ.setdefault("CITYFEED_DB", str(ROOT / "data" / "cityfeed.db"))
os.environ.setdefault("CITYFEED_REGISTRY", str(ROOT / "sources"))
# Crawling is CI's job here; /v1/admin/refresh answers 501 rather than hanging.
os.environ.setdefault("CITYFEED_READONLY", "1")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from cityfeed.api import app  # noqa: E402


class RestoreRewrittenPath:
    """Put back the path the client actually requested.

    A Vercel rewrite does not proxy transparently: the function is invoked with
    the *destination* path (`/api/index`), and any `:capture` from the source
    pattern is appended to the query string. So a request for `/v1/health`
    arrives as `/api/index?path=health`, and FastAPI — which knows only about
    `/v1/health` — answers 404 for every route in the application.

    Rather than reconstruct the path by guessing at which query parameter used
    to be a path segment, `vercel.json` states it outright as `__vpath`. This
    middleware reads it back and strips it, so every route below sees the
    original URL and the app stays identical to the one that runs locally.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            params = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
            original = next((v for k, v in params if k == "__vpath"), None)
            if original:
                scope = dict(scope)
                scope["path"] = original
                scope["raw_path"] = original.encode()
                scope["query_string"] = urlencode(
                    [(k, v) for k, v in params if k != "__vpath"]
                ).encode()
        await self.app(scope, receive, send)


# Browsers on other origins need this; the data is public and read-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)
# Added last, so it is outermost: the path has to be correct before anything
# else — CORS, routing, the endpoints — looks at it.
app.add_middleware(RestoreRewrittenPath)
