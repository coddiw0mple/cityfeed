"""Audit tests: one deliberately corrupted record per check.

A check that cannot fire is worse than no check, because it reads as a passing
guarantee. So every check here gets a row built specifically to trip it, and the
assertion is that it does.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cityfeed.audit import ERROR, INFO, WARN, audit
from cityfeed.cli import connect

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return connect(str(tmp_path / "audit.db"))


def add_venue(conn, vid, name, *, lat=52.01, lon=4.36, city="Delft"):
    conn.execute(
        "INSERT OR REPLACE INTO venues (id,name,city,address,lat,lon) VALUES (?,?,?,?,?,?)",
        (vid, name, city, None, lat, lon),
    )
    conn.commit()


def add_event(conn, **kw):
    """Insert one canonical event row, defaulting everything unspecified."""
    row = {
        "id": kw.get("id", f"e{abs(hash(str(kw))) % 10**8}"),
        "city": "Delft",
        "title": kw.get("title", "A Concert"),
        "start": kw.get("start", NOW + timedelta(days=3)),
        "end": kw.get("end"),
        "venue_id": kw.get("venue_id"),
        "venue_name": kw.get("venue_name", "Theater de Veste"),
        "venue_lat": kw.get("venue_lat", 52.01),
        "venue_lon": kw.get("venue_lon", 4.36),
        "url": kw.get("url", "https://example.test/programma/x"),
        "is_free": kw.get("is_free"),
        "price": kw.get("price"),
        "rrule": kw.get("rrule"),
        "description": kw.get("description", ""),
        "category": kw.get("category", "music"),
        "confidence": 1.0,
        "source_ids": ",".join(kw.get("sources", ["venue_site"])),
        "members": json.dumps(kw.get("members", [])),
    }
    # Columns named explicitly rather than positionally: a positional insert
    # here breaks every test in the file the moment a column is added, which is
    # exactly what happened when last_seen and withdrawn_at arrived.
    conn.execute(
        """
        INSERT OR REPLACE INTO events
            (id, city, title, start, end, venue_id, venue_name, venue_lat,
             venue_lon, url, is_free, price, rrule, description, category,
             confidence, source_ids, members)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row["id"], row["city"], row["title"], row["start"].isoformat(),
            row["end"].isoformat() if row["end"] else None,
            row["venue_id"], row["venue_name"], row["venue_lat"], row["venue_lon"],
            row["url"], row["is_free"], row["price"], row["rrule"],
            row["description"], row["category"], row["confidence"],
            row["source_ids"], row["members"],
        ),
    )
    conn.commit()
    return row["id"]


def fired(conn, check, registry="sources"):
    report = audit(conn, "Delft", now=NOW, registry=registry)
    return next((f for f in report.findings if f.check == check), None)


def member(source_id, title, start, trust=3):
    return {"source_id": source_id, "trust": trust, "title": title,
            "start": start.isoformat(), "url": None, "venue": None, "price": None}


# ------------------------------------------------------------------ non-events

def test_editorial_url_shape_fires(db):
    add_event(db, title="Raadsvergadering", venue_name=None,
              start=datetime(2026, 9, 1, 0, 0, tzinfo=AMS),
              url="https://delftopzondag.nl/nieuws/raad-stemt-in")
    f = fired(db, "non_event.editorial_url")
    assert f and f.severity == ERROR and f.count == 1


def test_editorial_title_fires(db):
    add_event(db, title="Column: fietsen in de binnenstad")
    add_event(db, id="q", title="Waarom sluit het zwembad?")
    f = fired(db, "non_event.editorial_title")
    assert f and f.count == 2


def test_longform_without_venue_fires(db):
    add_event(db, venue_name=None, description="x" * 1600)
    f = fired(db, "non_event.longform_no_venue")
    assert f and f.count == 1


# -------------------------------------------------------------------- temporal

def test_past_events_reported(db):
    add_event(db, start=NOW - timedelta(days=5))
    f = fired(db, "temporal.past")
    assert f and f.severity == INFO and f.count == 1


def test_far_future_fires(db):
    add_event(db, start=NOW + timedelta(days=600))
    f = fired(db, "temporal.far_future")
    assert f and f.severity == WARN


def test_small_hours_fires(db):
    add_event(db, start=datetime(2026, 9, 1, 4, 30, tzinfo=AMS))
    f = fired(db, "temporal.small_hours")
    assert f and f.count == 1


def test_end_before_start_is_an_error(db):
    start = datetime(2026, 9, 1, 20, tzinfo=AMS)
    add_event(db, start=start, end=start - timedelta(hours=2))
    f = fired(db, "temporal.end_before_start")
    assert f and f.severity == ERROR


def test_long_duration_fires(db):
    start = datetime(2026, 9, 1, 9, tzinfo=AMS)
    add_event(db, start=start, end=start + timedelta(hours=20))
    f = fired(db, "temporal.long_duration")
    assert f and f.count == 1


def test_source_losing_its_time_component_is_an_error(db):
    """>60% midnight starts means the clock time is being dropped."""
    for i in range(8):
        add_event(db, id=f"m{i}", sources=["broken_feed"],
                  start=datetime(2026, 9, 1 + i, 0, 0, tzinfo=AMS))
    add_event(db, id="ok", sources=["broken_feed"],
              start=datetime(2026, 9, 20, 20, 0, tzinfo=AMS))
    f = fired(db, "temporal.midnight_source")
    assert f and f.severity == ERROR
    assert "broken_feed" in f.examples[0]


def test_first_of_month_clustering_fires(db):
    for i in range(8):
        add_event(db, id=f"f{i}", start=datetime(2026, 9 + (i % 3), 1, 20, tzinfo=AMS))
    add_event(db, id="other", start=datetime(2026, 9, 14, 20, tzinfo=AMS))
    f = fired(db, "temporal.first_of_month_cluster")
    assert f and f.count == 8


# ------------------------------------------------------------------------ text

def test_mojibake_is_an_error(db):
    add_event(db, title="CafÃ© de Wijnhaven")
    f = fired(db, "text.mojibake")
    assert f and f.severity == ERROR


def test_html_entities_in_title_is_an_error(db):
    add_event(db, title="Jazz &amp; Blues")
    f = fired(db, "text.html_entities")
    assert f and f.severity == ERROR


def test_raw_html_is_an_error(db):
    add_event(db, title="Concert <br> tonight")
    f = fired(db, "text.raw_html")
    assert f and f.severity == ERROR


def test_title_shape_fires_on_short_long_and_shouting(db):
    add_event(db, id="s", title="Hi")
    add_event(db, id="l", title="x" * 200)
    add_event(db, id="c", title="TOTAAL UITVERKOCHT")
    f = fired(db, "text.title_shape")
    assert f and f.count == 3


def test_truncated_title_fires(db):
    add_event(db, title="Een avond vol muziek en…")
    f = fired(db, "text.title_truncated")
    assert f and f.count == 1


def test_title_that_is_a_date_or_the_venue_fires(db):
    add_event(db, id="d", title="12-09-2026")
    add_event(db, id="v", title="Theater de Veste", venue_name="Theater de Veste")
    f = fired(db, "text.title_is_date_or_venue")
    assert f and f.count == 2


def test_whitespace_and_punctuation_noise_fires(db):
    add_event(db, id="w", title="Jazz   Night")
    add_event(db, id="p", title="- Pubquiz")
    f = fired(db, "text.whitespace_punct")
    assert f and f.severity == INFO and f.count == 2


# ----------------------------------------------------------------------- venue

def test_ungeocoded_rate_reported(db):
    add_venue(db, "v1", "Theater de Veste")
    add_venue(db, "v2", "Lecture hall Pi", lat=None, lon=None)
    f = fired(db, "venue.ungeocoded_rate")
    assert f and f.severity == INFO and f.count == 1


def test_ungeocoded_per_source_reported(db):
    add_event(db, sources=["roomy_feed"], venue_lat=None, venue_lon=None)
    f = fired(db, "venue.ungeocoded_per_source")
    assert f and "roomy_feed" in f.examples[0]


def test_bad_venue_names_fire(db):
    add_venue(db, "v1", "Delft")
    add_venue(db, "v2", "https://example.test")
    add_venue(db, "v3", "")
    f = fired(db, "venue.bad_name")
    assert f and f.count == 3


def test_venue_outside_the_bounding_box_is_an_error(db):
    add_venue(db, "v1", "Bacchus", lat=36.5092, lon=-83.6038)  # Tennessee
    f = fired(db, "venue.outside_bbox")
    assert f and f.severity == ERROR


def test_near_duplicate_venues_fire(db):
    add_venue(db, "v1", "Theater de Veste")
    add_venue(db, "v2", "Theater de Vestes")
    f = fired(db, "venue.near_duplicates")
    assert f and f.count == 1


# ----------------------------------------------------------------------- dedup

def test_missed_merge_between_sources_fires(db):
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    add_event(db, id="a", title="Jazzavond in de Wijnhaven", start=start,
              sources=["venue_site"])
    add_event(db, id="b", title="Jazzavond Wijnhaven", start=start + timedelta(hours=1),
              sources=["aggregator"])
    f = fired(db, "dedup.missed_merges")
    assert f and f.count == 1


def test_same_source_pairs_are_not_counted_as_missed_merges(db):
    """Dedup never merges same-source records by design, so this is not a miss."""
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    add_event(db, id="a", title="Jazzavond", start=start, sources=["venue_site"])
    add_event(db, id="b", title="Jazzavond", start=start, sources=["venue_site"])
    assert fired(db, "dedup.missed_merges") is None


def test_over_merge_on_time_fires(db):
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    add_event(db, start=start, sources=["a", "b"], members=[
        member("a", "Concert", start),
        member("b", "Concert", start + timedelta(hours=6)),
    ])
    f = fired(db, "dedup.over_merge_time")
    assert f and f.count == 1


def test_over_merge_on_title_fires(db):
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    add_event(db, start=start, sources=["a", "b"], members=[
        member("a", "Jazzavond", start),
        member("b", "Kinderworkshop pottenbakken", start),
    ])
    f = fired(db, "dedup.over_merge_title")
    assert f and f.count >= 1


def test_two_records_from_one_source_in_a_cluster_is_an_error(db):
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    add_event(db, start=start, sources=["a"], members=[
        member("a", "Concert", start),
        member("a", "Concert", start),
    ])
    f = fired(db, "dedup.same_source_cluster")
    assert f and f.severity == ERROR


# ---------------------------------------------------------------------- volume

def test_source_dominance_fires(db):
    for i in range(9):
        add_event(db, id=f"d{i}", sources=["cinema"], title=f"Film {i}")
    add_event(db, id="other", sources=["theatre"], title="A Play")
    f = fired(db, "volume.source_dominance")
    assert f and "cinema" in f.examples[0]


def test_repeated_title_fires(db):
    for i in range(10):
        add_event(db, id=f"r{i}", title="Jamsession @ Bebop",
                  start=NOW + timedelta(days=i))
    f = fired(db, "volume.repeated_title")
    assert f and f.count == 1


def test_silent_source_fires(db, tmp_path):
    """Measured against the registry: an enabled source with no events."""
    registry = tmp_path / "reg"
    registry.mkdir()
    (registry / "delft.yaml").write_text(
        "sources:\n"
        "  - id: quiet_source\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/a\n"
        "  - id: loud_source\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/b\n"
    )
    add_event(db, sources=["loud_source"])
    f = fired(db, "volume.silent_source", registry=str(registry))
    assert f and f.examples == ["quiet_source"]


def test_distributions_are_reported(db):
    add_event(db, id="a", category=None, is_free=None)
    add_event(db, id="b", category="music", is_free=1)
    cat = fired(db, "distribution.category")
    free = fired(db, "distribution.is_free")
    assert cat and cat.severity == INFO and "1/2 uncategorised" in cat.explanation
    assert free and "1/2 unknown" in free.explanation


# ----------------------------------------------------------------- consistency

def test_free_event_with_a_price_fires(db):
    add_event(db, is_free=1, price="€17,50")
    f = fired(db, "consistency.free_with_price")
    assert f and f.count == 1


def test_orphan_venue_reference_is_an_error(db):
    add_venue(db, "known", "Theater de Veste")
    add_event(db, venue_id="ghost")
    f = fired(db, "consistency.orphan_venue_ref")
    assert f and f.severity == ERROR


def test_duplicate_ids_check_fires_when_the_store_allows_them(db):
    """The primary key normally prevents this, so the check is exercised
    against a store that has lost the constraint -- which is exactly the
    situation it exists to catch."""
    from cityfeed.audit import check_consistency, load_context

    add_event(db, id="dup")
    ctx = load_context(db, "Delft", now=NOW)

    class LostConstraint:
        def execute(self, sql, *a):
            if "GROUP BY id" in sql:
                return [("dup", 2)]
            return db.execute(sql, *a)

    findings = [f for f in check_consistency(ctx, LostConstraint()) if f]
    dupes = next(f for f in findings if f.check == "consistency.duplicate_ids")
    assert dupes.severity == ERROR and dupes.count == 1


# ------------------------------------------------------------------- reporting

def test_exit_code_gates_on_errors(db):
    add_event(db, title="CafÃ© de Wijnhaven")
    report = audit(db, "Delft", now=NOW)
    assert report.errors
    assert "ERROR" in report.render()
    assert json.loads(json.dumps(report.to_dict(), default=str))["counts"]["ERROR"] >= 1


def test_clean_database_produces_no_errors(db):
    add_venue(db, "v1", "Theater de Veste")
    add_event(db, venue_id="v1", title="Jazzavond", is_free=0, price="€12,50")
    report = audit(db, "Delft", now=NOW)
    assert report.errors == []
