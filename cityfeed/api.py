"""Read-only HTTP over what the pipeline wrote.

One rule shapes this module: the API does not think. Every filter here is a
WHERE clause against tables the pipeline already populated, and there is no
extraction, no merging and no scoring in the request path. Re-implementing any
of that here would give two answers to the same question — the crawl's and the
API's — and the moment they disagree, neither is trustworthy.

The filter worth pointing at is `min_sources`. Anyone can serve a list of
events scraped from one aggregator. Serving only the events that two
independent sources agree on is a claim about corroboration, and it is only
possible because dedup kept the provenance rather than collapsing it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from .models import is_zero_token

DB_PATH = os.environ.get("CITYFEED_DB", "data/cityfeed.db")
# Read endpoints are public unless this is set, so a demo can be handed to
# someone without provisioning a key. /v1/admin/* ignores this entirely.
REQUIRE_KEY_FOR_READS = os.environ.get("CITYFEED_REQUIRE_KEY", "").lower() in {"1", "true", "yes"}
CACHE_CONTROL = "public, max-age=300"

app = FastAPI(
    title="cityfeed",
    version="1.0",
    description=(
        "Public event data for one city, assembled from independent sources.\n\n"
        "The distinguishing filter is **`min_sources`**: `min_sources=2` returns "
        "only events that two or more independent sources listed. That is a "
        "corroboration guarantee rather than a volume claim, and it is the "
        "reason provenance is kept through deduplication instead of being "
        "collapsed into a single winning record."
    ),
)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _valid_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("CITYFEED_API_KEYS", "").split(",") if k.strip()]


def _check_key(request: Request) -> None:
    """Constant-time comparison against every configured key.

    `==` on a secret leaks its length and prefix through timing. It almost
    never matters and costs nothing to avoid, so there is no reason to be the
    service that finds out it mattered.
    """
    supplied = request.headers.get("X-API-Key", "")
    keys = _valid_keys()
    if not keys:
        raise HTTPException(503, "no API keys configured: set CITYFEED_API_KEYS")
    if not any(secrets.compare_digest(supplied, key) for key in keys):
        raise HTTPException(401, "invalid or missing X-API-Key")


def reject_if_readonly() -> None:
    """Refuse writes on a host that cannot perform them, before checking auth.

    Ordered ahead of the key check on purpose. On a read-only deployment the
    endpoint is non-functional for everybody, so "this deployment does not
    crawl" is both the true answer and a more useful one than "your key is
    wrong" — and it reveals nothing that the README does not already say.
    """
    if os.environ.get("CITYFEED_READONLY", "").lower() in {"1", "true", "yes"}:
        raise HTTPException(
            501,
            "this deployment is read-only: it serves a database built elsewhere. "
            "Crawls run on a schedule in CI; see .github/workflows/crawl.yml.",
        )


def require_admin(request: Request) -> None:
    _check_key(request)


def require_read(request: Request) -> None:
    if REQUIRE_KEY_FOR_READS:
        _check_key(request)


def _encode_cursor(start: str, event_id: str) -> str:
    return base64.urlsafe_b64encode(f"{start}|{event_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """Opaque base64 of (start, id).

    Keyed on the sort key rather than an offset, so inserting an event during
    pagination cannot make the client skip or repeat one. An OFFSET-based
    cursor silently does both, and the client has no way to detect it.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        start, _, event_id = base64.urlsafe_b64decode(padded).decode().partition("|")
        if not start or not event_id:
            raise ValueError
        return start, event_id
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "malformed cursor") from exc


def _etag(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, default=str).encode()
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _conditional(payload: dict, request: Request) -> Response:
    """Attach an ETag and honour If-None-Match with a 304.

    The hash is over the result set, not the query, so a client polling an
    unchanged city gets a 304 and a few bytes rather than the whole page.
    """
    etag = _etag(payload)
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


def _row_to_event(row: sqlite3.Row) -> dict:
    keys = row.keys()
    # A compact provenance summary on every list item, not just on detail. The
    # dashboard draws marker border weight and the per-source strip from this,
    # and making it fetch each event separately to render a list would be a
    # request per card. The full member records stay on the detail endpoint.
    sources: list[dict] = []
    if "members" in keys and row["members"]:
        try:
            sources = [
                {"source_id": m["source_id"], "trust": m["trust"], "title": m["title"]}
                for m in json.loads(row["members"])
            ]
        except (ValueError, KeyError, TypeError):
            sources = []
    return {
        "sources": sources,
        "id": row["id"],
        "title": row["title"],
        "start": row["start"],
        "end": row["end"],
        "city": row["city"],
        "url": row["url"],
        "is_free": None if row["is_free"] is None else bool(row["is_free"]),
        "price": row["price"] if "price" in keys else None,
        "rrule": row["rrule"] if "rrule" in keys else None,
        "category": row["category"],
        "confidence": row["confidence"],
        "venue": {
            "id": row["venue_id"] if "venue_id" in keys else None,
            "name": row["venue_name"],
            "lat": row["venue_lat"],
            "lon": row["venue_lon"],
        },
        "source_ids": row["source_ids"].split(",") if row["source_ids"] else [],
        "source_count": len(set(row["source_ids"].split(","))) if row["source_ids"] else 0,
    }


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

@app.get("/v1/events", dependencies=[Depends(require_read)])
def list_events(
    request: Request,
    city: Optional[str] = None,
    category: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    free: Optional[bool] = None,
    min_sources: int = Query(
        1,
        ge=1,
        description=(
            "Only return events listed by at least this many independent sources. "
            "min_sources=2 is a corroboration filter: it trades recall for the "
            "guarantee that no single source's mistake reaches the caller alone."
        ),
    ),
    bbox: Optional[str] = Query(None, description="minLon,minLat,maxLon,maxLat"),
    q: Optional[str] = None,
    expand: Optional[str] = Query(
        None,
        description=(
            "Set to 'occurrences' to return dated instances instead of series. "
            "A weekly event is one event with an rrule; expanded, it is one row "
            "per date, which is what a calendar view needs."
        ),
    ),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    # Withdrawn events are retired, not deleted; they stay for audit.
    where, params = ["e.withdrawn_at IS NULL"], []
    if city:
        where.append("LOWER(e.city) = LOWER(?)")
        params.append(city)
    if category:
        where.append("e.category = ?")
        params.append(category)
    if free is not None:
        where.append("e.is_free = ?")
        params.append(int(free))
    if q:
        where.append("(e.title LIKE ? OR e.venue_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        except ValueError as exc:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat") from exc
        where.append(
            "e.venue_lat BETWEEN ? AND ? AND e.venue_lon BETWEEN ? AND ?"
        )
        params += [min_lat, max_lat, min_lon, max_lon]
    if min_sources > 1:
        # source_ids is a comma-joined set of distinct ids, so counting commas
        # counts sources without a join.
        where.append("(LENGTH(e.source_ids) - LENGTH(REPLACE(e.source_ids, ',', '')) + 1) >= ?")
        params.append(min_sources)

    expanded = expand == "occurrences"
    sort_time = "o.start" if expanded else "e.start"
    if from_:
        where.append(f"{sort_time} >= ?")
        params.append(from_)
    if to:
        where.append(f"{sort_time} <= ?")
        params.append(to)

    if cursor:
        cursor_start, cursor_id = _decode_cursor(cursor)
        where.append(f"({sort_time}, e.id) > (?, ?)")
        params += [cursor_start, cursor_id]

    if expanded:
        sql = (
            "SELECT e.*, o.id AS occurrence_id, o.start AS occ_start, o.end AS occ_end, "
            "o.is_free AS occ_free, o.price AS occ_price, o.cancelled "
            "FROM occurrences o JOIN events e ON e.id = o.event_id "
            f"WHERE {' AND '.join(where)} AND o.cancelled = 0 "
            f"ORDER BY o.start, e.id LIMIT {limit + 1}"
        )
    else:
        sql = (
            f"SELECT e.* FROM events e WHERE {' AND '.join(where)} "
            f"ORDER BY e.start, e.id LIMIT {limit + 1}"
        )

    rows = db.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = []
    for row in rows:
        event = _row_to_event(row)
        if expanded:
            event["occurrence"] = {
                "id": row["occurrence_id"],
                "start": row["occ_start"],
                "end": row["occ_end"],
                "is_free": None if row["occ_free"] is None else bool(row["occ_free"]),
                "price": row["occ_price"],
            }
            event["start"], event["end"] = row["occ_start"], row["occ_end"]
        items.append(event)

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(last["occ_start" if expanded else "start"], last["id"])

    return _conditional(
        {"items": items, "next_cursor": next_cursor, "count": len(items)}, request
    )


@app.get("/v1/events/{event_id}", dependencies=[Depends(require_read)])
def get_event(event_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM events WHERE id = ? AND withdrawn_at IS NULL", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such event")

    event = _row_to_event(row)
    # The whole provenance chain, not a summary of it. A reviewer asking "why
    # did these two listings merge?" needs to see what each source actually
    # said, including where they disagreed about the title.
    event["members"] = json.loads(row["members"]) if row["members"] else []
    event["occurrences"] = [
        {
            "id": o["id"], "start": o["start"], "end": o["end"],
            "is_free": None if o["is_free"] is None else bool(o["is_free"]),
            "price": o["price"], "cancelled": bool(o["cancelled"]),
            "is_override": bool(o["is_override"]),
        }
        for o in db.execute(
            "SELECT * FROM occurrences WHERE event_id = ? ORDER BY start", (event_id,)
        )
    ]
    return _conditional(event, request)


# --------------------------------------------------------------------------
# venues
# --------------------------------------------------------------------------

@app.get("/v1/venues", dependencies=[Depends(require_read)])
def list_venues(
    request: Request,
    city: Optional[str] = None,
    bbox: Optional[str] = Query(None, description="minLon,minLat,maxLon,maxLat"),
    has_coords: Optional[bool] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: sqlite3.Connection = Depends(get_db),
):
    where, params = ["1=1"], []
    if city:
        where.append("LOWER(v.city) = LOWER(?)")
        params.append(city)
    if has_coords is not None:
        where.append("v.lat IS NOT NULL" if has_coords else "v.lat IS NULL")
    if bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(x) for x in bbox.split(","))
        except ValueError as exc:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat") from exc
        where.append("v.lat BETWEEN ? AND ? AND v.lon BETWEEN ? AND ?")
        params += [min_lat, max_lat, min_lon, max_lon]

    rows = db.execute(
        f"""
        SELECT v.*, count(e.id) AS event_count
        FROM venues v LEFT JOIN events e ON e.venue_id = v.id AND e.withdrawn_at IS NULL
        WHERE {' AND '.join(where)}
        GROUP BY v.id ORDER BY event_count DESC, v.name LIMIT {limit}
        """,
        params,
    ).fetchall()
    items = [
        {
            "id": r["id"], "name": r["name"], "city": r["city"], "address": r["address"],
            "lat": r["lat"], "lon": r["lon"], "geocode_source": r["geocode_source"],
            "default_price": r["default_price"], "event_count": r["event_count"],
        }
        for r in rows
    ]
    return _conditional({"items": items, "count": len(items)}, request)


@app.get("/v1/venues/{venue_id}", dependencies=[Depends(require_read)])
def get_venue(
    venue_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
):
    """A venue and what is on there, dated and priced.

    This is the endpoint behind "tap a church, see communion Sunday free and a
    concert Wednesday for €17". Pricing is read per occurrence and falls back
    to the series, because a run of shows usually has one price and sometimes
    has a cheap preview.
    """
    row = db.execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such venue")

    now = datetime.now(timezone.utc).isoformat()
    upcoming = db.execute(
        """
        SELECT o.id, o.start, o.end, o.price AS occ_price, o.is_free AS occ_free,
               o.cancelled, e.id AS event_id, e.title, e.category, e.url,
               e.price AS series_price, e.is_free AS series_free, e.source_ids
        FROM occurrences o JOIN events e ON e.id = o.event_id
        WHERE e.venue_id = ? AND o.start >= ? AND o.cancelled = 0
              AND e.withdrawn_at IS NULL
        ORDER BY o.start LIMIT ?
        """,
        (venue_id, now, limit),
    ).fetchall()

    payload = {
        "id": row["id"], "name": row["name"], "city": row["city"],
        "address": row["address"], "lat": row["lat"], "lon": row["lon"],
        "geocode_source": row["geocode_source"], "default_price": row["default_price"],
        "upcoming": [
            {
                "occurrence_id": o["id"],
                "event_id": o["event_id"],
                "title": o["title"],
                "start": o["start"],
                "end": o["end"],
                "category": o["category"],
                "url": o["url"],
                "price": o["occ_price"] or o["series_price"] or row["default_price"],
                "is_free": (
                    bool(o["occ_free"]) if o["occ_free"] is not None
                    else (bool(o["series_free"]) if o["series_free"] is not None else None)
                ),
                "source_count": len(set((o["source_ids"] or "").split(","))),
            }
            for o in upcoming
        ],
    }
    return _conditional(payload, request)


# --------------------------------------------------------------------------
# registry, categories, health
# --------------------------------------------------------------------------

@app.get("/v1/sources", dependencies=[Depends(require_read)])
def list_sources(request: Request, city: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    from .fetch import load_registries

    specs = load_registries(os.environ.get("CITYFEED_REGISTRY", "sources"))
    if city:
        specs = [s for s in specs if s.city.lower() == city.lower()]
    health = {
        r["source_id"]: r
        for r in db.execute("SELECT * FROM source_runs").fetchall()
    }
    items = []
    for spec in specs:
        run = health.get(spec.id)
        items.append({
            "id": spec.id, "city": spec.city, "type": spec.type.value,
            "url": spec.url, "trust": int(spec.trust), "enabled": spec.enabled,
            "cadence_minutes": spec.cadence_minutes,
            "zero_token": is_zero_token(spec.type),
            "last_attempt": run["last_attempt"] if run else None,
            "last_success": run["last_success"] if run else None,
            "records": run["records"] if run else None,
            "status": run["status"] if run else "never run",
            "notes": spec.notes,
        })
    return _conditional({"items": items, "count": len(items)}, request)


@app.get("/v1/categories", dependencies=[Depends(require_read)])
def list_categories(request: Request, city: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    where, params = (("WHERE withdrawn_at IS NULL AND LOWER(city) = LOWER(?)", [city])
                     if city else ("WHERE withdrawn_at IS NULL", []))
    rows = db.execute(
        f"SELECT category, count(*) AS n FROM events {where} GROUP BY category ORDER BY n DESC",
        params,
    ).fetchall()
    return _conditional(
        {"items": [{"category": r["category"], "count": r["n"]} for r in rows]}, request
    )


@app.get("/v1/health")
def health(city: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    """200 only if every enabled source succeeded within twice its cadence.

    Deliberately stricter than "the process is up". A crawler that is running
    fine while three feeds have quietly 404'd for a week is the failure mode
    that matters, and a health check that cannot see it is decorative.
    """
    from .fetch import load_registries

    specs = [s for s in load_registries(os.environ.get("CITYFEED_REGISTRY", "sources")) if s.enabled]
    if city:
        # Without this a multi-city registry reports the whole deployment as
        # degraded because some other city has not been crawled yet, which
        # makes the check useless exactly when you have more than one city.
        specs = [s for s in specs if s.city.lower() == city.lower()]
    runs = {r["source_id"]: r for r in db.execute("SELECT * FROM source_runs").fetchall()}
    now = datetime.now(timezone.utc)

    stale, never = [], []
    for spec in specs:
        run = runs.get(spec.id)
        if run is None or not run["last_success"]:
            never.append(spec.id)
            continue
        last = datetime.fromisoformat(run["last_success"])
        if now - last > timedelta(minutes=2 * spec.cadence_minutes):
            stale.append({"source": spec.id, "last_success": run["last_success"]})

    ok = not stale and not never
    payload = {
        "status": "ok" if ok else "degraded",
        "enabled_sources": len(specs),
        "stale": stale,
        "never_succeeded": never,
        "events": db.execute("SELECT count(*) FROM events").fetchone()[0],
        "occurrences": db.execute("SELECT count(*) FROM occurrences").fetchone()[0],
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app.post(
    "/v1/admin/refresh",
    dependencies=[Depends(reject_if_readonly), Depends(require_admin)],
)
def refresh(city: str = Query(..., description="city to crawl")):
    """Trigger a crawl. Always key-protected, regardless of the read setting."""
    result = subprocess.run(
        [sys.executable, "-m", "cityfeed.cli", "run", "--city", city],
        capture_output=True, text=True, timeout=900,
    )
    return {
        "city": city,
        "exit_code": result.returncode,
        "output": result.stdout[-4000:],
        "errors": result.stderr[-2000:] or None,
    }
