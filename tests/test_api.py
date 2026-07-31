"""API tests against a seeded temp database.

Everything here runs off a database this module builds, never the live one, so
the assertions are about behaviour rather than about what Delft happens to have
on this week.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cityfeed.cli import connect, persist, persist_occurrences, persist_source_runs
from cityfeed.models import CanonicalEvent, RawRecord, TrustTier, Venue

AMS = ZoneInfo("Europe/Amsterdam")
pytest.importorskip("fastapi")


def _event(n: int, *, venue: Venue, sources: list[str], **kwargs) -> CanonicalEvent:
    start = datetime(2026, 9, 1, 20, tzinfo=AMS) + timedelta(days=n)
    members = [
        RawRecord(source_id=s, source_url="https://x", trust=TrustTier.VENUE,
                  title=f"Event {n} as {s} saw it", start=start)
        for s in sources
    ]
    return CanonicalEvent(
        id=f"evt{n:04d}", title=f"Event {n}", start=start, city="Delft",
        venue=venue, members=members, **kwargs,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    veste = Venue(name="Theater de Veste", city="Delft", address="Vesteplein 1",
                  lat=52.0104, lon=4.3595)
    lumen = Venue(name="Filmhuis Lumen", city="Delft", lat=52.0075, lon=4.3560)

    events = [
        _event(n, venue=veste if n % 2 else lumen,
               sources=["venue_site", "aggregator"] if n % 3 == 0 else ["venue_site"],
               price="€17,50" if n % 3 == 0 else None,
               is_free=(n % 5 == 0),
               category="theatre" if n % 2 else "film")
        for n in range(23)
    ]
    # one weekly series, so expand=occurrences has something to expand
    events.append(
        _event(30, venue=veste, sources=["venue_site"], rrule="FREQ=WEEKLY", price="€5")
    )

    conn = connect(str(db))
    persist(conn, events)
    persist_occurrences(conn, events)
    persist_source_runs(conn, {"venue_site": "12 records", "aggregator": "5 records"})
    conn.close()

    monkeypatch.setenv("CITYFEED_DB", str(db))
    monkeypatch.setenv("CITYFEED_API_KEYS", "topsecret,second")
    monkeypatch.delenv("CITYFEED_REQUIRE_KEY", raising=False)

    from fastapi.testclient import TestClient
    import cityfeed.api as api

    importlib.reload(api)
    return TestClient(api.app), events


def test_openapi_and_docs_render(client):
    http, _ = client
    assert http.get("/openapi.json").status_code == 200
    spec = http.get("/openapi.json").json()
    described = spec["paths"]["/v1/events"]["get"]
    params = {p["name"]: p for p in described["parameters"]}
    # the differentiating filter has to be discoverable, not folklore
    assert "corroboration" in params["min_sources"]["description"].lower()


def test_pagination_yields_every_event_exactly_once(client):
    """The acceptance criterion: limit=5 through the whole city, no gaps or repeats."""
    http, events = client
    seen, cursor, pages = [], None, 0
    while True:
        params = {"city": "Delft", "limit": 5}
        if cursor:
            params["cursor"] = cursor
        body = http.get("/v1/events", params=params).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        pages += 1
        if not cursor:
            break
        assert pages < 50, "cursor did not terminate"

    assert len(seen) == len(set(seen)), "an event was returned twice"
    assert set(seen) == {e.id for e in events}, "an event was skipped"
    assert pages > 1


def test_cursor_is_stable_when_a_row_is_inserted_midway(client):
    """An offset cursor would skip a row here; a keyed cursor does not."""
    http, events = client
    first = http.get("/v1/events", params={"city": "Delft", "limit": 5}).json()
    cursor = first["next_cursor"]

    import sqlite3, os
    conn = connect(os.environ["CITYFEED_DB"])
    inserted = _event(99, venue=Venue(name="Filmhuis Lumen", city="Delft"),
                      sources=["venue_site"])
    inserted.start = events[0].start - timedelta(hours=1)  # sorts before page 1
    persist(conn, [inserted])
    conn.close()

    rest = http.get("/v1/events", params={"city": "Delft", "limit": 5, "cursor": cursor}).json()
    page1 = {i["id"] for i in first["items"]}
    assert not page1 & {i["id"] for i in rest["items"]}, "page 1 rows reappeared"


def test_etag_round_trip_returns_304(client):
    http, _ = client
    first = http.get("/v1/events", params={"city": "Delft"})
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "public, max-age=300"

    again = http.get("/v1/events", params={"city": "Delft"}, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_min_sources_filters_to_corroborated_events(client):
    http, events = client
    all_events = http.get("/v1/events", params={"city": "Delft", "limit": 200}).json()
    corroborated = http.get(
        "/v1/events", params={"city": "Delft", "limit": 200, "min_sources": 2}
    ).json()

    assert 0 < corroborated["count"] < all_events["count"]
    assert all(item["source_count"] >= 2 for item in corroborated["items"])


def test_filters_narrow_the_result_set(client):
    http, _ = client
    free = http.get("/v1/events", params={"city": "Delft", "free": True, "limit": 200}).json()
    assert free["count"] and all(i["is_free"] for i in free["items"])

    film = http.get("/v1/events", params={"category": "film", "limit": 200}).json()
    assert film["count"] and all(i["category"] == "film" for i in film["items"])

    bbox = http.get(
        "/v1/events", params={"bbox": "4.3590,52.0100,4.3600,52.0110", "limit": 200}
    ).json()
    assert bbox["count"] and all(i["venue"]["name"] == "Theater de Veste" for i in bbox["items"])

    assert http.get("/v1/events", params={"bbox": "nonsense"}).status_code == 400


def test_event_detail_exposes_per_source_provenance(client):
    http, _ = client
    body = http.get("/v1/events", params={"min_sources": 2, "limit": 1}).json()
    detail = http.get(f"/v1/events/{body['items'][0]['id']}").json()

    assert len(detail["members"]) >= 2
    assert {m["source_id"] for m in detail["members"]} == set(detail["source_ids"])
    # the per-source titles must survive, that is the point of keeping members
    assert len({m["title"] for m in detail["members"]}) >= 2
    assert detail["occurrences"]

    assert http.get("/v1/events/nope").status_code == 404


def test_expand_occurrences_returns_dated_instances(client):
    http, _ = client
    series = http.get("/v1/events", params={"limit": 200}).json()
    expanded = http.get("/v1/events", params={"limit": 200, "expand": "occurrences"}).json()

    assert expanded["count"] > series["count"], "the weekly series should expand"
    assert all("occurrence" in i for i in expanded["items"])
    # The horizon runs from now, not from the series start, so a series that
    # begins two months out contributes only the weeks that fall inside it.
    weekly = [i for i in expanded["items"] if i["id"] == "evt0030"]
    assert len(weekly) >= 4
    assert len({i["occurrence"]["start"] for i in weekly}) == len(weekly)
    assert all(i["occurrence"]["price"] == "€5" for i in weekly)


def test_venue_endpoints(client):
    http, _ = client
    listed = http.get("/v1/venues", params={"city": "Delft"}).json()
    assert listed["count"] == 2
    assert all(v["event_count"] > 0 for v in listed["items"])

    with_coords = http.get("/v1/venues", params={"has_coords": True}).json()
    assert with_coords["count"] == 2

    veste = next(v for v in listed["items"] if v["name"] == "Theater de Veste")
    detail = http.get(f"/v1/venues/{veste['id']}").json()
    assert detail["address"] == "Vesteplein 1"
    assert detail["upcoming"], "a venue with events must list them"
    # per-date pricing is the whole point: communion free, concert EUR 17
    assert any(u["price"] for u in detail["upcoming"])
    assert all("start" in u for u in detail["upcoming"])

    assert http.get("/v1/venues/nope").status_code == 404


def test_venue_detail_shows_per_occurrence_price_overrides(client):
    """Making one date free must not restate the series."""
    import os

    http, _ = client
    veste = next(
        v for v in http.get("/v1/venues").json()["items"] if v["name"] == "Theater de Veste"
    )
    conn = connect(os.environ["CITYFEED_DB"])
    target = conn.execute(
        "SELECT o.id FROM occurrences o JOIN events e ON e.id = o.event_id "
        "WHERE e.id = 'evt0030' ORDER BY o.start LIMIT 1 OFFSET 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE occurrences SET price = NULL, is_free = 1, is_override = 1 WHERE id = ?",
        (target,),
    )
    conn.commit()
    conn.close()

    upcoming = http.get(f"/v1/venues/{veste['id']}").json()["upcoming"]
    overridden = [u for u in upcoming if u["occurrence_id"] == target]
    others = [u for u in upcoming if u["event_id"] == "evt0030" and u["occurrence_id"] != target]
    assert overridden and overridden[0]["is_free"] is True
    assert others and all(o["price"] == "€5" for o in others)


def test_categories_and_sources(client):
    http, _ = client
    cats = {c["category"]: c["count"] for c in http.get("/v1/categories").json()["items"]}
    assert cats["film"] and cats["theatre"]

    sources = http.get("/v1/sources").json()
    assert sources["count"] > 0
    assert all("zero_token" in s for s in sources["items"])


def test_health_is_degraded_when_a_source_never_succeeded(client):
    """Health must track feed staleness, not process liveness."""
    http, _ = client
    body = http.get("/v1/health")
    # the seeded run table covers two invented sources, not the real registry,
    # so the real enabled sources register as never-succeeded
    assert body.status_code == 503
    assert body.json()["status"] == "degraded"
    assert body.json()["never_succeeded"]
    assert body.json()["events"] > 0


def test_admin_refresh_requires_a_key(client):
    http, _ = client
    assert http.post("/v1/admin/refresh", params={"city": "Delft"}).status_code == 401
    assert http.post(
        "/v1/admin/refresh", params={"city": "Delft"}, headers={"X-API-Key": "wrong"}
    ).status_code == 401


def test_reads_are_public_unless_configured_otherwise(client, monkeypatch):
    http, _ = client
    assert http.get("/v1/events").status_code == 200

    monkeypatch.setenv("CITYFEED_REQUIRE_KEY", "true")
    import cityfeed.api as api
    from fastapi.testclient import TestClient

    importlib.reload(api)
    locked = TestClient(api.app)
    assert locked.get("/v1/events").status_code == 401
    assert locked.get("/v1/events", headers={"X-API-Key": "topsecret"}).status_code == 200
    assert locked.get("/v1/events", headers={"X-API-Key": "second"}).status_code == 200
