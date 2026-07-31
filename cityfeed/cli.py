"""Command line interface.

    cityfeed sources                       list the registry
    cityfeed venues --city Delft           venues, event counts, geocoding state
    cityfeed geocode --city Delft          resolve venue coordinates (cached)
    cityfeed probe --file urls.txt         find the extraction tier of candidates
    cityfeed run --city Delft              fetch, extract, dedup, store
    cityfeed run --offline --fixtures DIR  same, from saved payloads
    cityfeed recall --city Delft           measure recall against holdouts
    cityfeed evaluate --gold gold.json     score output against ground truth

The --offline path exists so the pipeline can be developed and graded without
touching the network, replaying stored snapshots. That is also what makes a
change to dedup measurable: the input is pinned, so any movement in the numbers
came from the code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from .dedup import deduplicate
from .evaluate import evaluate, load_gold
from .extract import ExtractionError, extract
from .fetch import (
    Fetcher, SnapshotStore, assert_holdouts_are_held_out, load_registries,
)
from .geocode import geocode_records
from .models import CanonicalEvent, SourceSpec, is_zero_token

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    city           TEXT,
    address        TEXT,
    lat            REAL,
    lon            REAL,
    default_price  TEXT,
    notes          TEXT,
    resolved_at    TEXT,
    geocode_source TEXT
);
CREATE INDEX IF NOT EXISTS venues_city ON venues (city);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    city        TEXT NOT NULL,
    title       TEXT NOT NULL,
    start       TEXT NOT NULL,
    end         TEXT,
    venue_id    TEXT REFERENCES venues (id),
    venue_name  TEXT,
    venue_lat   REAL,
    venue_lon   REAL,
    url         TEXT,
    is_free     INTEGER,
    price       TEXT,
    rrule       TEXT,
    category    TEXT,
    confidence  REAL,
    source_ids  TEXT NOT NULL,
    -- The per-source evidence this event was merged from, as JSON. Kept
    -- because an unexplainable merge is one nobody will trust: the detail
    -- endpoint has to be able to show which source claimed which title.
    members     TEXT
);
CREATE INDEX IF NOT EXISTS events_city_start ON events (city, start);
CREATE INDEX IF NOT EXISTS events_category ON events (category);
CREATE INDEX IF NOT EXISTS events_venue ON events (venue_id);

CREATE TABLE IF NOT EXISTS occurrences (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL REFERENCES events (id),
    start       TEXT NOT NULL,
    end         TEXT,
    is_free     INTEGER,
    price       TEXT,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    is_override INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS occurrences_start ON occurrences (start);
CREATE INDEX IF NOT EXISTS occurrences_event ON occurrences (event_id);

-- Source health, so /v1/health can answer "is this feed stale?" rather than
-- "did the last process crash?". A source that quietly stopped returning
-- events is the failure this table exists to make visible.
CREATE TABLE IF NOT EXISTS source_runs (
    source_id    TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    records      INTEGER,
    status       TEXT
);
"""


def connect(path: str = "data/cityfeed.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def persist_venues(conn: sqlite3.Connection, events: list[CanonicalEvent]) -> int:
    """Upsert the venues these events refer to, without clobbering geocodes.

    COALESCE on the incoming value rather than a blanket REPLACE: a crawl learns
    a venue's name and address, a geocoder learns its coordinates, and they run
    at different times. A plain upsert would have every crawl silently wipe the
    lat/lon that B3 spent a network round trip resolving.
    """
    seen: dict[str, tuple] = {}
    for event in events:
        if event.venue is None:
            continue
        venue = event.venue
        seen[venue.key] = (
            venue.key, venue.name, venue.city or event.city, venue.address,
            venue.lat, venue.lon, venue.default_price, venue.notes,
            venue.geocode_source,
        )
    conn.executemany(
        """
        INSERT INTO venues (id, name, city, address, lat, lon, default_price,
                            notes, geocode_source)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET
            name           = excluded.name,
            city           = COALESCE(excluded.city, venues.city),
            address        = COALESCE(excluded.address, venues.address),
            lat            = COALESCE(excluded.lat, venues.lat),
            lon            = COALESCE(excluded.lon, venues.lon),
            default_price  = COALESCE(excluded.default_price, venues.default_price),
            notes          = COALESCE(excluded.notes, venues.notes),
            geocode_source = COALESCE(excluded.geocode_source, venues.geocode_source)
        """,
        list(seen.values()),
    )
    conn.commit()
    return len(seen)


def persist(conn: sqlite3.Connection, events: list[CanonicalEvent]) -> None:
    persist_venues(conn, events)
    rows = [
        (
            e.id, e.city, e.title, e.start.isoformat(),
            e.end.isoformat() if e.end else None,
            e.venue_id,
            e.venue.name if e.venue else None,
            e.venue.lat if e.venue else None,
            e.venue.lon if e.venue else None,
            e.url,
            None if e.is_free is None else int(e.is_free),
            e.price,
            e.rrule,
            e.category,
            e.confidence,
            ",".join(e.source_ids),
            json.dumps(
                [
                    {
                        "source_id": m.source_id,
                        "trust": int(m.trust),
                        "title": m.title,
                        "start": m.start.isoformat(),
                        "url": m.url,
                        "venue": m.venue.name if m.venue else None,
                        "price": m.price,
                    }
                    for m in e.members
                ]
            ),
        )
        for e in events
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()


def persist_source_runs(conn: sqlite3.Connection, stats: dict[str, str]) -> None:
    """Record what each source did this cycle, so staleness is queryable."""
    now = datetime.now(dt_timezone.utc).isoformat()
    for source_id, status in stats.items():
        ok = not status.startswith(("FAILED", "ERROR", "no payload"))
        records = 0
        if ok and status.split(" ")[0].isdigit():
            records = int(status.split(" ")[0])
        conn.execute(
            """
            INSERT INTO source_runs (source_id, last_attempt, last_success, records, status)
            VALUES (?,?,?,?,?)
            ON CONFLICT (source_id) DO UPDATE SET
                last_attempt = excluded.last_attempt,
                last_success = COALESCE(excluded.last_success, source_runs.last_success),
                records      = excluded.records,
                status       = excluded.status
            """,
            (source_id, now, now if ok else None, records, status),
        )
    conn.commit()


def persist_occurrences(
    conn: sqlite3.Connection, events: list[CanonicalEvent], timezone: str = "Europe/Amsterdam"
) -> int:
    """Materialise dated instances, preserving any per-occurrence overrides.

    A re-crawl must not undo an edit. Rows flagged `is_override` are left
    exactly as they are, so cancelling one night of a run or making a single
    screening free survives the next crawl instead of being quietly reverted.
    """
    from .occurrence import expand_all

    rows = expand_all(events, timezone=timezone)
    overridden = {
        row[0]
        for row in conn.execute("SELECT id FROM occurrences WHERE is_override = 1")
    }
    payload = [
        (
            o.id, o.event_id, o.start.isoformat(),
            o.end.isoformat() if o.end else None,
            None if o.is_free is None else int(o.is_free),
            o.price, int(o.cancelled), int(o.is_override),
        )
        for o in rows
        if o.id not in overridden
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO occurrences VALUES (?,?,?,?,?,?,?,?)", payload
    )
    conn.commit()
    return len(rows)


def _payload_for(spec: SourceSpec, fixtures: Path | None, store: SnapshotStore):
    """Offline payload lookup: explicit fixture first, then latest snapshot."""
    if fixtures is not None:
        for suffix in (".html", ".ics", ".xml", ".json"):
            candidate = fixtures / f"{spec.id}{suffix}"
            if candidate.exists():
                return candidate.read_text()
    digest = store.latest_for(spec.id)
    return store.get(digest) if digest else None


async def _gather(specs: list[SourceSpec], offline: bool, fixtures: Path | None):
    store = SnapshotStore()
    fetcher = Fetcher(store)
    records, stats = [], {}
    for spec in specs:
        if not spec.enabled:
            continue
        try:
            if offline:
                payload = _payload_for(spec, fixtures, store)
                changed = payload is not None
            else:
                payload, changed = await fetcher.fetch(spec)
            if payload is None:
                stats[spec.id] = "no payload"
                continue
            found = extract(payload, spec)
            records.extend(found)
            stats[spec.id] = f"{len(found)} records" + ("" if changed else " (unchanged)")
        except ExtractionError as exc:
            # Extraction failure is a source-health signal, not a crash. One
            # drifted venue page must not take the whole crawl down.
            stats[spec.id] = f"FAILED: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface transport errors the same way
            stats[spec.id] = f"ERROR: {type(exc).__name__}: {exc}"
    return records, stats


def cmd_run(args: argparse.Namespace) -> int:
    # Before anything is fetched. A holdout that has leaked into the ingested
    # registry does not break the crawl -- it silently turns the recall figure
    # into a self-assessment -- so it has to fail here, loudly, rather than be
    # noticed later.
    assert_holdouts_are_held_out(args.registry)

    specs = load_registries(args.registry)
    if args.city:
        specs = [s for s in specs if s.city.lower() == args.city.lower()]
    if not specs:
        print(f"no sources for city {args.city!r}")
        return 1

    fixtures = Path(args.fixtures) if args.fixtures else None
    records, stats = asyncio.run(_gather(specs, args.offline, fixtures))

    print("source health")
    print("-" * 52)
    for source_id, status in stats.items():
        flag = "!" if status.startswith(("FAILED", "ERROR", "no payload")) else " "
        print(f" {flag} {source_id:<32} {status}")

    city = args.city or (specs[0].city if specs else "unknown")
    locale = specs[0].locale if specs else "nl"

    # Geocode before dedup, not after. Venue similarity is scored on distance
    # when both sides have coordinates and on fuzzy names when they do not, so
    # resolving first is what lets "Theater de Veste - Delft" and "Theater De
    # Veste" merge on geometry instead of on spelling.
    if not args.no_geocode and records:
        conn = connect(args.db)
        resolved, calls = asyncio.run(
            geocode_records(conn, records, city, refresh=args.refresh_geocode)
        )
        print(f"\ngeocoding: {resolved} venues resolved, {calls} network lookups")

    events = deduplicate(records, city=city, locale=locale)

    collapsed = len(records) - len(events)
    print()
    print(f"{len(records)} raw records -> {len(events)} canonical events "
          f"({collapsed} merged, {collapsed / len(records) * 100:.1f}% duplication)"
          if records else "no records extracted")

    if args.json:
        Path(args.json).write_text(
            json.dumps([json.loads(e.model_dump_json()) for e in events], indent=2)
        )
        print(f"wrote {args.json}")
    if not args.dry_run:
        conn = connect(args.db)
        persist(conn, events)
        persist_source_runs(conn, stats)
        timezone = specs[0].timezone if specs else "Europe/Amsterdam"
        materialised = persist_occurrences(conn, events, timezone)
        venue_count = conn.execute(
            "SELECT count(*) FROM venues WHERE city = ?", (city,)
        ).fetchone()[0]
        recurring = sum(1 for e in events if e.rrule)
        print(
            f"persisted to {args.db}: {len(events)} events, {materialised} occurrences "
            f"({recurring} recurring series), {venue_count} venues"
        )
    return 0


def cmd_geocode(args: argparse.Namespace) -> int:
    """Resolve the venue cache without re-crawling."""
    from .geocode import CITY_BBOX, Geocoder
    from .models import Venue

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT id, name, city, address, lat FROM venues WHERE city = ?"
        + ("" if args.refresh else " AND lat IS NULL AND resolved_at IS NULL"),
        (args.city,),
    ).fetchall()
    if not rows:
        print(f"nothing to resolve for {args.city} (use --refresh to re-resolve)")
        return 0

    if args.city.lower() not in CITY_BBOX:
        print(
            f"warning: no bounding box configured for {args.city!r}, so out-of-city "
            f"results cannot be rejected. Add one to geocode.CITY_BBOX."
        )

    venues = [Venue(name=r[1], city=r[2], address=r[3]) for r in rows]
    geocoder = Geocoder(conn)
    print(f"resolving {len(venues)} venues in {args.city}...")
    resolved = asyncio.run(geocoder.resolve_all(venues, args.city, refresh=args.refresh))

    hits = sum(1 for v in resolved.values() if v is not None)
    print(f"{hits}/{len(resolved)} resolved, {geocoder.calls} network lookups")
    if hits < len(resolved):
        print("\nunresolved (these are where the next unit of work is):")
        for row, venue in zip(rows, venues):
            if resolved.get(venue.key) is None:
                print(f"  {venue.name[:60]}")
    return 0


def cmd_venues(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    where, params = "", []
    if args.city:
        where, params = "WHERE v.city = ?", [args.city]
    rows = conn.execute(
        f"""
        SELECT v.name, v.city, v.lat, v.lon, v.geocode_source, count(e.id) AS n
        FROM venues v LEFT JOIN events e ON e.venue_id = v.id
        {where}
        GROUP BY v.id ORDER BY n DESC, v.name
        """,
        params,
    ).fetchall()
    if not rows:
        print("no venues yet - run `cityfeed run` first")
        return 1

    print(f"{'venue':<40} {'events':>6}  {'coords':<22} source")
    print("-" * 84)
    for name, _city, lat, lon, source, count in rows:
        coords = f"{lat:.5f},{lon:.5f}" if lat is not None and lon is not None else "-"
        print(f"{(name or '?')[:40]:<40} {count:>6}  {coords:<22} {source or ''}")

    resolved = sum(1 for r in rows if r[2] is not None)
    print()
    print(
        f"{resolved}/{len(rows)} venues geocoded "
        f"({resolved / len(rows) * 100:.0f}%), covering "
        f"{sum(r[5] for r in rows if r[2] is not None)}/{sum(r[5] for r in rows)} events"
    )
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    specs = load_registries(args.registry)
    by_city: dict[str, list[SourceSpec]] = {}
    for spec in specs:
        by_city.setdefault(spec.city, []).append(spec)
    for city, group in sorted(by_city.items()):
        print(f"\n{city}  ({len(group)} sources)")
        print("-" * 52)
        for spec in sorted(group, key=lambda s: (s.trust.value, s.id)):
            tier = "free" if is_zero_token(spec.type) else "cached"
            print(f"  {spec.id:<30} {spec.type.value:<8} trust={spec.trust.value} {tier}")
    zero = sum(1 for s in specs if is_zero_token(s.type))
    print(f"\n{zero}/{len(specs)} sources parse with zero model calls")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from .probe import load_urls, probe_all, render_table, suggest_yaml

    urls = load_urls(args.file) if args.file else list(args.url)
    if not urls:
        print("nothing to probe: pass URLs or --file")
        return 1

    results = asyncio.run(
        probe_all(urls, concurrency=args.concurrency, check_sitemap=not args.no_sitemap)
    )
    print(render_table(results))

    blocks = [b for r in results if (b := suggest_yaml(r, args.city, args.locale))]
    if blocks:
        print("\n\nsuggested registry rows (review trust before pasting)")
        print("-" * 52)
        print("sources:")
        print("\n".join(blocks))
    if args.out:
        Path(args.out).write_text("sources:\n" + "\n".join(blocks) + "\n")
        print(f"\nwrote {args.out}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    from .recall import run_recall

    conn = connect(args.db)
    try:
        report = run_recall(
            conn, args.registry, args.city, locale=args.locale,
            window_days=args.window, offline=args.offline,
            with_capture_recapture=args.capture_recapture,
        )
    except ValueError as exc:
        print(exc)
        return 1
    print(report.render())
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    produced_raw = json.loads(Path(args.produced).read_text())
    produced = [CanonicalEvent(**item) for item in produced_raw]
    gold = load_gold(args.gold)
    report = evaluate(produced, gold, locale=args.locale)
    print(report.render())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cityfeed", description=__doc__)
    parser.add_argument("--registry", default="sources", help="registry directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="fetch, extract, dedup, store")
    p_run.add_argument("--city")
    p_run.add_argument("--offline", action="store_true", help="replay snapshots/fixtures")
    p_run.add_argument("--fixtures", help="directory of saved payloads")
    p_run.add_argument("--db", default="data/cityfeed.db")
    p_run.add_argument("--json", help="also write canonical events to this path")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--no-geocode", action="store_true", help="skip venue resolution")
    p_run.add_argument("--refresh-geocode", action="store_true",
                       help="re-resolve venues even if already cached")
    p_run.set_defaults(func=cmd_run)

    p_sources = sub.add_parser("sources", help="list the registry")
    p_sources.set_defaults(func=cmd_sources)

    p_geo = sub.add_parser("geocode", help="resolve the venue cache")
    p_geo.add_argument("--city", required=True)
    p_geo.add_argument("--refresh", action="store_true", help="re-resolve already-cached venues")
    p_geo.add_argument("--db", default="data/cityfeed.db")
    p_geo.set_defaults(func=cmd_geocode)

    p_venues = sub.add_parser("venues", help="list venues with event counts and geocoding")
    p_venues.add_argument("--city")
    p_venues.add_argument("--db", default="data/cityfeed.db")
    p_venues.set_defaults(func=cmd_venues)

    p_probe = sub.add_parser("probe", help="discover the extraction tier of candidate URLs")
    p_probe.add_argument("url", nargs="*", help="URLs to probe")
    p_probe.add_argument("--file", help="file of candidate URLs, one per line")
    p_probe.add_argument("--city", default="Delft", help="city for suggested registry rows")
    p_probe.add_argument("--locale", default="nl")
    p_probe.add_argument("--concurrency", type=int, default=4)
    p_probe.add_argument("--no-sitemap", action="store_true", help="skip the sitemap fallback")
    p_probe.add_argument("--out", help="write the suggested YAML to this path")
    p_probe.set_defaults(func=cmd_probe)

    p_recall = sub.add_parser("recall", help="measure recall against held-out sources")
    p_recall.add_argument("--city", required=True)
    p_recall.add_argument("--locale", default="nl")
    p_recall.add_argument("--window", type=int, default=90,
                          help="days ahead to score, matching the crawl horizon")
    p_recall.add_argument("--offline", action="store_true")
    p_recall.add_argument("--capture-recapture", action="store_true",
                          help="estimate total population from two holdouts")
    p_recall.add_argument("--db", default="data/cityfeed.db")
    p_recall.set_defaults(func=cmd_recall)

    p_eval = sub.add_parser("evaluate", help="score output against a golden set")
    p_eval.add_argument("--produced", required=True)
    p_eval.add_argument("--gold", required=True)
    p_eval.add_argument("--locale", default="nl")
    p_eval.set_defaults(func=cmd_evaluate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
