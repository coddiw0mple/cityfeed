"""Recall against held-out sources.

This is the module that stops the project grading its own homework.

Precision is easy to check: look at what was produced and see whether it is
real. Recall is not, because the events the pipeline missed are by definition
not in its output — there is nothing to inspect. The usual workaround is
hand-labelling a day of listings, which is expensive, has to be redone every
time the city changes, and quietly measures the labeller's diligence as much as
the crawler's coverage.

The alternative here: pick two or three public sources, never ingest them, and
count how many of their events the pipeline found anyway. The denominator is
explicit — it is exactly the events those sources published — and the miss list
is the actionable output. Every missed event names a source worth adding.

The matching rule is `evaluate._same_event`, reused rather than reimplemented.
A separate, looser matcher here would let the pipeline pass its own exam by
counting near-misses as hits.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .evaluate import GoldEvent, _same_event
from .extract import extract
from .fetch import Fetcher, SnapshotStore, load_holdouts
from .models import CanonicalEvent, RawRecord, Venue


@dataclass
class Miss:
    title: str
    start: datetime
    venue: Optional[str]
    source_id: str
    url: Optional[str]


@dataclass
class RecallReport:
    matched: int = 0
    total: int = 0
    misses: list[Miss] = field(default_factory=list)
    per_source: dict[str, tuple[int, int]] = field(default_factory=dict)
    out_of_scope: dict[str, int] = field(default_factory=dict)
    source_errors: dict[str, str] = field(default_factory=dict)
    corpus_size: int = 0
    window_days: int = 0
    estimate: Optional[dict] = None

    @property
    def recall(self) -> float:
        return self.matched / self.total if self.total else 0.0

    def render(self) -> str:
        lines = [
            "recall against held-out sources",
            "=" * 62,
            "",
            f"  {self.matched}/{self.total} held-out events were found by the pipeline",
        ]
        if self.total:
            lines.append(f"  recall = {self.recall:.3f}")
        lines += [
            "",
            f"  denominator: {self.total} events published by "
            f"{len(self.per_source)} holdout source(s) within {self.window_days} days",
            f"  compared against: {self.corpus_size} canonical events in the database",
        ]
        dropped = sum(self.out_of_scope.values())
        if dropped:
            lines.append(
                f"  excluded from the denominator: {dropped} held-out events whose "
                f"listing names a different city"
            )
            lines.append(
                "  (holdouts search by radius, not by municipality; only events "
                "explicitly\n   placed elsewhere are dropped, ambiguous ones are "
                "kept and counted against us)"
            )

        if self.source_errors:
            lines += ["", "holdout sources that could not be read", "-" * 62]
            for source_id, error in sorted(self.source_errors.items()):
                lines.append(f"  ! {source_id:<28} {error}")
            lines.append("  (these contribute nothing to the denominator)")

        if self.per_source:
            lines += ["", "recall by holdout source", "-" * 62]
            for source_id, (found, total) in sorted(self.per_source.items()):
                rate = found / total if total else 0.0
                lines.append(f"  {source_id:<32} {rate:.3f}  ({found}/{total})")

        lines += ["", f"missed events ({len(self.misses)}) — this is the deliverable", "-" * 62]
        if not self.misses:
            lines.append("  none")
        for miss in sorted(self.misses, key=lambda m: m.start):
            venue = f" @ {miss.venue}" if miss.venue else ""
            lines.append(f"  {miss.start.strftime('%Y-%m-%d %H:%M')}  {miss.title[:46]}{venue[:26]}")
            lines.append(f"      via {miss.source_id}{'  ' + miss.url if miss.url else ''}")

        if self.estimate:
            est = self.estimate
            lines += [
                "",
                "capture-recapture population estimate",
                "-" * 62,
                f"  source A ({est['a_id']}):  {est['a']} events",
                f"  source B ({est['b_id']}):  {est['b']} events",
                f"  in both:                   {est['overlap']}",
            ]
            if est.get("estimate") is None:
                lines.append(
                    "  estimate: undefined — the two holdouts share no events, so "
                    "there is nothing to estimate from. Widen the window or pick "
                    "holdouts with more overlap."
                )
            else:
                lines += [
                    f"  estimated total population: {est['estimate']:.0f} events",
                    f"  pipeline holds {self.corpus_size}, "
                    f"implying coverage of {est['coverage']:.1%}",
                ]
            lines += [
                "",
                "  This assumes the two sources list events independently. They do",
                "  not: aggregators copy from the same venue pages, so an event",
                "  listed by one is more likely to be listed by the other than",
                "  chance would predict. That inflates the overlap, shrinks the",
                "  population estimate, and makes coverage look better than it is.",
                "  Read the figure as a ceiling, never as a measurement.",
            ]
        return "\n".join(lines)


def load_corpus(conn: sqlite3.Connection, city: str) -> list[CanonicalEvent]:
    """Read canonical events back out of the store for matching."""
    rows = conn.execute(
        "SELECT id, title, start, end, venue_name, venue_lat, venue_lon, city, "
        "url, is_free, category FROM events WHERE LOWER(city) = LOWER(?)",
        (city,),
    ).fetchall()
    events = []
    for row in rows:
        venue = (
            Venue(name=row[4], city=row[7], lat=row[5], lon=row[6]) if row[4] else None
        )
        events.append(
            CanonicalEvent(
                id=row[0], title=row[1],
                start=datetime.fromisoformat(row[2]),
                end=datetime.fromisoformat(row[3]) if row[3] else None,
                venue=venue, city=row[7], url=row[8],
                is_free=None if row[9] is None else bool(row[9]),
                category=row[10],
            )
        )
    return events


async def fetch_holdouts(
    specs, offline: bool = False
) -> tuple[dict[str, list[RawRecord]], dict[str, str]]:
    """Fetch and extract the holdout sources. Never touches the main registry."""
    store = SnapshotStore()
    fetcher = Fetcher(store)
    records: dict[str, list[RawRecord]] = {}
    errors: dict[str, str] = {}
    for spec in specs:
        try:
            if offline:
                digest = store.latest_for(spec.id)
                payload = store.get(digest) if digest else None
            else:
                payload, _ = await fetcher.fetch(spec)
            if payload is None:
                errors[spec.id] = "no payload"
                continue
            records[spec.id] = extract(payload, spec)
        except Exception as exc:  # noqa: BLE001
            errors[spec.id] = f"{type(exc).__name__}: {exc}"
    return records, errors


def in_target_city(record: RawRecord, city: str) -> bool:
    """Is this held-out event actually in the city under test?

    Necessary because holdout sources do not share the pipeline's definition of
    a city. Meetup's "near Delft" is a radius search, and more than half of what
    it returns is in Rotterdam or Den Haag. Scoring those as misses does not
    measure coverage of Delft — it measures the radius, and it drives recall to
    zero for a reason that has nothing to do with the crawler.

    The rule is deliberately asymmetric, because this filter shrinks the
    denominator and a filter that shrinks its own denominator is exactly how an
    honest metric becomes a flattering one. An event is excluded only when the
    source explicitly names a *different* city. Anything unstated or ambiguous
    stays in and counts against us.
    """
    venue = record.venue
    if venue is None:
        return True

    target = city.strip().lower()
    stated = (venue.city or "").strip().lower()
    if stated:
        return target in stated or stated in target

    address = (venue.address or "").lower()
    if address:
        # An address that names some city, but not ours, is out of scope.
        # An address with no recognisable city stays in.
        if target in address:
            return True
        return not any(other in address for other in _KNOWN_NEARBY if other != target)
    return True


# Cities close enough to Delft that a radius-based holdout will return them.
# This list exists only to recognise an explicitly stated other city; it is not
# used to guess, and an unrecognised place name always counts as in scope.
_KNOWN_NEARBY = {
    "rotterdam", "den haag", "the hague", "'s-gravenhage", "leiden", "zoetermeer",
    "rijswijk", "schiedam", "vlaardingen", "amsterdam", "utrecht", "brussels",
    "pijnacker", "naaldwijk", "westland", "capelle", "gouda", "dordrecht",
}


def _as_gold(record: RawRecord, city: str) -> GoldEvent:
    return GoldEvent(
        title=record.title,
        start=record.start,
        venue=record.venue.name if record.venue else None,
        is_free=record.is_free,
        city=city,
        source_hint=record.source_id,
    )


def measure(
    holdout_records: dict[str, list[RawRecord]],
    corpus: list[CanonicalEvent],
    city: str,
    locale: str = "nl",
    window_days: int = 90,
    now: Optional[datetime] = None,
) -> RecallReport:
    """Match every holdout event against the corpus and report what is missing."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=window_days)

    report = RecallReport(corpus_size=len(corpus), window_days=window_days)
    for source_id, records in holdout_records.items():
        # Only score the window the pipeline actually covers. Counting a
        # holdout's listing for next spring as a miss measures the horizon,
        # not the coverage, and quietly makes recall look worse than it is.
        dated = [r for r in records if now <= r.start <= horizon]
        in_window = [r for r in dated if in_target_city(r, city)]
        report.out_of_scope[source_id] = len(dated) - len(in_window)
        found = 0
        for record in in_window:
            gold = _as_gold(record, city)
            if any(_same_event(gold, produced, locale) for produced in corpus):
                found += 1
            else:
                report.misses.append(
                    Miss(record.title, record.start,
                         record.venue.name if record.venue else None,
                         source_id, record.url)
                )
        report.per_source[source_id] = (found, len(in_window))
        report.matched += found
        report.total += len(in_window)
    return report


def capture_recapture(
    holdout_records: dict[str, list[RawRecord]],
    corpus_size: int,
    city: str = "Delft",
    locale: str = "nl",
    window_days: int = 90,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Lincoln-Petersen estimate of the total event population.

    N ≈ |A| × |B| / |A ∩ B|. Two independent observers of the same population;
    how much they overlap says how big the population is. The independence
    assumption is the whole ballgame and it is not satisfied here — see the
    caveat printed alongside the number.
    """
    if len(holdout_records) < 2:
        return None
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=window_days)

    (a_id, a_records), (b_id, b_records) = list(holdout_records.items())[:2]
    a = [r for r in a_records if now <= r.start <= horizon and in_target_city(r, city)]
    b = [r for r in b_records if now <= r.start <= horizon and in_target_city(r, city)]

    overlap = 0
    for record in a:
        gold = _as_gold(record, city)
        # Reuse the same matcher, comparing a holdout record against another
        # holdout record by wrapping it in the canonical shape.
        if any(
            _same_event(
                gold,
                CanonicalEvent(id="x", title=other.title, start=other.start,
                               city=city,
                               venue=other.venue),
                locale,
            )
            for other in b
        ):
            overlap += 1

    estimate = (len(a) * len(b) / overlap) if overlap else None
    return {
        "a_id": a_id, "b_id": b_id,
        "a": len(a), "b": len(b), "overlap": overlap,
        "estimate": estimate,
        "coverage": (corpus_size / estimate) if estimate else None,
    }


def run_recall(
    conn: sqlite3.Connection,
    registry: str | Path,
    city: str,
    locale: str = "nl",
    window_days: int = 90,
    offline: bool = False,
    with_capture_recapture: bool = False,
) -> RecallReport:
    specs = load_holdouts(registry, city)
    if not specs:
        raise ValueError(
            f"no holdout sources for {city}: expected {registry}/holdout_*.yaml"
        )

    records, errors = asyncio.run(fetch_holdouts(specs, offline))
    corpus = load_corpus(conn, city)
    report = measure(records, corpus, city, locale, window_days)
    report.source_errors = errors
    if with_capture_recapture:
        report.estimate = capture_recapture(records, len(corpus), city, locale, window_days)
    return report
