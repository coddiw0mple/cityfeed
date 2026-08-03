"""Command line interface.

    cityfeed sources                       list the registry
    cityfeed venues --city Delft           venues, event counts, geocoding state
    cityfeed geocode --city Delft          resolve venue coordinates (cached)
    cityfeed probe --file urls.txt         find the extraction tier of candidates
    cityfeed run --city Delft              fetch, extract, dedup, store
    cityfeed run --offline --fixtures DIR  same, from saved payloads
    cityfeed recall --city Delft           measure recall against holdouts
    cityfeed audit --city Delft            data-quality findings by severity
    cityfeed metrics --city Delft          freshness, breakage, yield regressions
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
from .extract import ExtractContext, ExtractionError, extract
from .fetch import (
    Fetcher, SnapshotStore, assert_holdouts_are_held_out, load_registries,
)
from .geocode import geocode_records
from .models import CanonicalEvent, SourceSpec, is_zero_token
from .provenance import record_provenance

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
    description TEXT,
    category    TEXT,
    confidence  REAL,
    source_ids  TEXT NOT NULL,
    -- When a crawl last saw this event, and when every source that listed it
    -- stopped. Without these an event lives forever: a cancelled show, or one
    -- pulled from its source, is indistinguishable from a current one.
    last_seen   TEXT,
    withdrawn_at TEXT,
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

-- Every run, not just the last one. A source that quietly halves its output is
-- invisible against a single previous value but obvious against its own
-- median, and this is what lets the yield check calibrate itself instead of
-- being hand-tuned per source.
CREATE TABLE IF NOT EXISTS source_run_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    ran_at    TEXT NOT NULL,
    records   INTEGER NOT NULL,
    ok        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS run_history_source ON source_run_history (source_id, id);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so an already-deployed database
# needs them applied explicitly or every read of the new field fails.
_MIGRATIONS = [
    ("events", "description", "TEXT"),
    ("events", "last_seen", "TEXT"),
    ("events", "withdrawn_at", "TEXT"),
]


def connect(path: str = "data/cityfeed.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for table, column, coltype in _MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()
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


def withdraw_unseen(
    conn: sqlite3.Connection,
    city: str,
    seen_at: str,
    succeeded: set[str],
    retired_sources: set[str] | None = None,
) -> int:
    """Retire events that their sources stopped listing.

    Two different silences, and conflating them is a bug in either direction:

    * **A source fetched and did not mention the event.** Withdraw it. This is
      evidence.
    * **A source failed to fetch.** Withdraw nothing. Absence of evidence is not
      evidence -- the same reason venue similarity returns a neutral score
      rather than zero when coordinates are missing. Without this rule the first
      aggregator rate-limit empties the database.

    And a third case that the first two miss: a source **disabled or removed
    from the registry**. It will never appear in `succeeded` again, so its
    events become permanently un-withdrawable and live forever. That is not
    caution, it is a leak -- it stranded four events from a source disabled for
    403ing. Deliberate removal by an operator *is* evidence, so those retire.

    Soft delete, so the row survives for audit and comes back if the source
    starts listing it again.
    """
    retired_sources = retired_sources or set()
    if not succeeded and not retired_sources:
        return 0
    withdrawn = 0
    now = datetime.now(dt_timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT id, source_ids FROM events "
        "WHERE city = ? AND withdrawn_at IS NULL AND (last_seen IS NULL OR last_seen < ?)",
        (city, seen_at),
    ).fetchall()
    for event_id, source_ids in rows:
        listing = {s for s in (source_ids or "").split(",") if s}
        # Every source either reported in, or has been taken out of service.
        if listing and listing <= (succeeded | retired_sources):
            conn.execute("UPDATE events SET withdrawn_at = ? WHERE id = ?", (now, event_id))
            # Occurrences are derived and regenerable, so they are deleted
            # rather than soft-deleted -- otherwise the table grows forever
            # behind withdrawn events. Human overrides survive: someone
            # cancelled or repriced that date deliberately, and if the event
            # returns, that edit should still be there.
            conn.execute(
                "DELETE FROM occurrences WHERE event_id = ? AND is_override = 0",
                (event_id,),
            )
            withdrawn += 1
    # Self-healing rather than only correct going forward: occurrences orphaned
    # by any earlier withdrawal, or by an event that no longer exists at all,
    # are swept every run. A fix that only applies to future rows leaves the
    # store permanently wrong about how much data it holds.
    conn.execute(
        """
        DELETE FROM occurrences WHERE is_override = 0 AND event_id IN (
            SELECT o.event_id FROM occurrences o
            LEFT JOIN events e ON e.id = o.event_id
            WHERE e.id IS NULL OR e.withdrawn_at IS NOT NULL
        )
        """
    )
    conn.commit()
    return withdrawn


def persist(conn: sqlite3.Connection, events: list[CanonicalEvent], seen_at: str | None = None) -> None:
    persist_venues(conn, events)
    seen_at = seen_at or datetime.now(dt_timezone.utc).isoformat()
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
            e.description,
            e.category,
            e.confidence,
            ",".join(e.source_ids),
            seen_at,
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
        """
        INSERT INTO events (id, city, title, start, end, venue_id, venue_name,
                            venue_lat, venue_lon, url, is_free, price, rrule,
                            description, category, confidence, source_ids,
                            last_seen, members)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET
            title=excluded.title, start=excluded.start, end=excluded.end,
            venue_id=excluded.venue_id, venue_name=excluded.venue_name,
            venue_lat=excluded.venue_lat, venue_lon=excluded.venue_lon,
            url=excluded.url, is_free=excluded.is_free, price=excluded.price,
            rrule=excluded.rrule, description=excluded.description,
            category=excluded.category, confidence=excluded.confidence,
            source_ids=excluded.source_ids, last_seen=excluded.last_seen,
            members=excluded.members, withdrawn_at=NULL
        """, rows
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
        conn.execute(
            "INSERT INTO source_run_history (source_id, ran_at, records, ok) VALUES (?,?,?,?)",
            (source_id, now, records, int(ok)),
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


async def _gather(
    specs: list[SourceSpec],
    offline: bool,
    fixtures: Path | None,
    context: ExtractContext | None = None,
):
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
            found = extract(payload, spec, context=context)
            records.extend(found)
            stats[spec.id] = f"{len(found)} records" + ("" if changed else " (unchanged)")
        except ExtractionError as exc:
            # Extraction failure is a source-health signal, not a crash. One
            # drifted venue page must not take the whole crawl down.
            stats[spec.id] = f"FAILED: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface transport errors the same way
            stats[spec.id] = f"ERROR: {type(exc).__name__}: {exc}"
    return records, stats


def _known_venue_names(db: str, city: str | None) -> set[str]:
    """Lower-cased venue names from the store, for RSS event detection."""
    try:
        conn = connect(db)
        rows = conn.execute(
            "SELECT name FROM venues WHERE ? IS NULL OR LOWER(city) = LOWER(?)",
            (city, city),
        ).fetchall()
        return {r[0].lower() for r in rows if r[0]}
    except sqlite3.Error:
        return set()


def cmd_run(args: argparse.Namespace) -> int:
    # Before anything is fetched. A holdout that has leaked into the ingested
    # registry does not break the crawl -- it silently turns the recall figure
    # into a self-assessment -- so it has to fail here, loudly, rather than be
    # noticed later.
    assert_holdouts_are_held_out(args.registry)

    specs = load_registries(args.registry)
    city_hint = args.city
    if args.city:
        specs = [s for s in specs if s.city.lower() == args.city.lower()]
    if not specs:
        print(f"no sources for city {args.city!r}")
        return 1

    fixtures = Path(args.fixtures) if args.fixtures else None

    # Venue names the store already knows, so an RSS item that mentions one can
    # be recognised as an event even when its URL and time say nothing.
    context = ExtractContext(known_venues=_known_venue_names(args.db, city_hint))
    records, stats = asyncio.run(_gather(specs, args.offline, fixtures, context))

    print("source health")
    print("-" * 52)
    for source_id, status in stats.items():
        flag = "!" if status.startswith(("FAILED", "ERROR", "no payload")) else " "
        print(f" {flag} {source_id:<32} {status}")
        for reason, n in sorted(context.for_source(source_id).items()):
            print(f"   {'':<32} dropped {n}: {reason}")

    # A source dropping most of what it publishes is not an event feed. Saying
    # so here is the difference between a registry row that looks healthy and
    # one you know to go and disable.
    for spec in specs:
        dropped = sum(context.for_source(spec.id).values())
        kept = sum(1 for r in records if r.source_id == spec.id)
        if dropped and dropped / (dropped + kept) > 0.70:
            print(
                f"\n  ! {spec.id}: dropped {dropped} of {dropped + kept} items "
                f"({dropped / (dropped + kept):.0%}). This is probably not an "
                f"event feed - consider disabling it with a note."
            )

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
        seen_at = datetime.now(dt_timezone.utc).isoformat()
        persist(conn, events, seen_at)
        persist_source_runs(conn, stats)
        # Only sources that actually fetched may retire anything.
        succeeded = {
            sid for sid, status in stats.items()
            if not status.startswith(("FAILED", "ERROR", "no payload"))
        }
        # Sources that used to feed this city and no longer will: disabled in
        # the registry, or deleted from it. Their events cannot come back.
        enabled_now = {s.id for s in specs if s.enabled}
        retired_sources = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT source_id FROM source_runs"
            ) if row[0] not in enabled_now
        }
        retired = withdraw_unseen(conn, city, seen_at, succeeded, retired_sources)
        if retired:
            print(f"withdrew {retired} events their sources no longer list")

        # Which source won each field, and what moved since last time. The tier
        # map is passed in because a record knows its source but not how that
        # source is parsed, and the difference is most of what confidence means.
        tiers = {s.id: s.type.value for s in specs}
        fields, changed = record_provenance(conn, events, tiers, seen_at)
        print(f"provenance: {fields} field origins recorded", end="")
        print(f", {changed} values changed since the last crawl" if changed else "")
        timezone = specs[0].timezone if specs else "Europe/Amsterdam"
        materialised = persist_occurrences(conn, events, timezone)
        venue_count = conn.execute(
            "SELECT count(*) FROM venues WHERE city = ?", (city,)
        ).fetchone()[0]
        recurring = sum(1 for e in events if e.rrule)
        # Two different numbers, and reporting one as the other is how a
        # README ends up with figures that do not reconcile: `materialised` is
        # what this run generated forward from today, `stored` includes dates
        # that have since passed and are kept as history.
        stored = conn.execute("SELECT count(*) FROM occurrences").fetchone()[0]
        print(
            f"persisted to {args.db}: {len(events)} events, {venue_count} venues, "
            f"{materialised} occurrences materialised ({recurring} recurring series), "
            f"{stored} in store including past dates"
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


def cmd_audit(args: argparse.Namespace) -> int:
    """Report data-quality findings. Non-zero exit if any ERROR fired."""
    from .audit import ERROR, audit

    conn = connect(args.db)
    report = audit(conn, args.city)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(report.render())
    # Non-zero so this can gate a crawl in CI: an ERROR means the database
    # holds something a user would see and believe, and that is false.
    return 1 if report.errors else 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Operational health: freshness, breakage, and yield regressions."""
    from .metrics import render, snapshot

    conn = connect(args.db)
    snap = snapshot(conn, args.city)
    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print(render(snap))
    # A collapsed source is a broken source; exit non-zero so CI can gate on it.
    collapsed = [r for r in snap["yield_regressions"] if r["severity"] == "collapsed"]
    return 1 if collapsed else 0


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

    p_audit = sub.add_parser("audit", help="data-quality checks over stored events")
    p_audit.add_argument("--city", required=True)
    p_audit.add_argument("--json", action="store_true", help="machine-readable output")
    p_audit.add_argument("--db", default="data/cityfeed.db")
    p_audit.set_defaults(func=cmd_audit)

    p_metrics = sub.add_parser("metrics", help="freshness, breakage, yield regressions")
    p_metrics.add_argument("--city")
    p_metrics.add_argument("--json", action="store_true")
    p_metrics.add_argument("--db", default="data/cityfeed.db")
    p_metrics.set_defaults(func=cmd_metrics)

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
