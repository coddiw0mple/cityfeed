"""Entity resolution.

This is the part that decides whether the product feels trustworthy. Extraction
failures look like a missing event, which users forgive. Dedup failures look
like the app is broken, which they don't.

Three stages, in the order that keeps it tractable:

    1. blocking   - cheap keys cut the comparison space from O(n^2) to something
                    linear-ish. Recall matters here; precision does not.
    2. scoring    - expensive similarity, but only inside blocks.
    3. clustering - union-find over the surviving pairs, then merge by trust.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta
from typing import Iterable, Optional

from rapidfuzz import fuzz

from .categorize import categorize
from .models import CanonicalEvent, RawRecord, Venue
from .normalize import normalize_title, title_trigrams

# Tuned to be deliberately permissive. A missed merge shows the user two copies
# of one concert; an over-eager merge hides a real event behind an unrelated one.
# The second failure is worse, so the threshold sits high and the blocking wide.
MERGE_THRESHOLD = 0.72

WEIGHTS = {
    "title": 0.45,
    "time": 0.35,
    "venue": 0.20,
}


# --------------------------------------------------------------------------
# 1. blocking
# --------------------------------------------------------------------------

def blocking_keys(record: RawRecord, locale: str = "nl") -> set[str]:
    """Cheap keys under which a record might collide with its duplicate.

    A record is compared against another only if they share at least one key.
    Several key families are emitted because any single one has a blind spot:
    a wrong geocode breaks the venue key, a typo'd title breaks the trigram key,
    a timezone bug breaks the day key. Emitting all three means one broken
    field doesn't cost the match.
    """
    keys: set[str] = set()
    day = record.start.date().isoformat()

    # day + coarse location
    if record.venue is not None:
        keys.add(f"dayloc:{day}:{record.venue.geokey()}")

    # day + every title trigram. Sampling here would be a silent recall bug:
    # a padded title shares a prefix but not an alphabetical slice.
    for tri in title_trigrams(record.title, locale):
        keys.add(f"daytri:{day}:{tri}")

    # normalised title alone catches events whose listed date differs by a day
    norm = normalize_title(record.title, locale)
    if norm:
        keys.add(f"title:{norm}")

    return keys


def build_blocks(records: Iterable[RawRecord], locale: str = "nl") -> dict[str, list[int]]:
    blocks: dict[str, list[int]] = defaultdict(list)
    for idx, record in enumerate(records):
        for key in blocking_keys(record, locale):
            blocks[key].append(idx)
    return blocks


# --------------------------------------------------------------------------
# 2. scoring
# --------------------------------------------------------------------------

def title_similarity(a: str, b: str, locale: str = "nl") -> float:
    """token_set_ratio, because listings pad titles with extra words.

    "Jazz Night" and "Jazz Night @ Cafe de Wijnhaven (live)" are the same event;
    a plain ratio punishes the padding, token_set does not.
    """
    na, nb = normalize_title(a, locale), normalize_title(b, locale)
    if not na or not nb:
        return 0.0
    return fuzz.token_set_ratio(na, nb) / 100.0


def time_similarity(a, b, tolerance_minutes: int = 90) -> float:
    """Decays linearly from identical start to `tolerance_minutes` apart.

    Listings routinely disagree on whether an event starts at doors-open or
    at showtime, so a small disagreement should not veto a merge — but a
    four-hour gap on the same evening usually means two different events.

    Exact midnight is treated as "time unknown" rather than "starts at 00:00",
    and scores neutral against any other time on the same day. Plenty of sources
    publish a date with no clock time — a festival listing reading "wo 19
    augustus 2026" and nothing more — and taking that literally puts the event
    19 hours from the same event elsewhere, which is far enough to veto every
    merge. This mirrors how venue_similarity already treats a missing venue:
    absence of evidence is not evidence against.

    The neutral value cannot carry a merge on its own. At weight 0.35 it
    contributes 0.175 of the 0.72 threshold, so title and venue still have to
    agree strongly for the pair to merge.
    """
    a_unknown = (a.hour, a.minute, a.second) == (0, 0, 0)
    b_unknown = (b.hour, b.minute, b.second) == (0, 0, 0)
    if a_unknown != b_unknown:
        return 0.5 if a.date() == b.date() else 0.0

    delta = abs((a - b).total_seconds()) / 60.0
    if delta > tolerance_minutes:
        return 0.0
    return 1.0 - (delta / tolerance_minutes)


def venue_similarity(a: Optional[Venue], b: Optional[Venue]) -> float:
    """Geographic distance if both are geocoded, name fuzz otherwise.

    Returns a neutral 0.5 when either side is missing, so an ungeocoded record
    is not penalised into never merging. Absence of evidence is not evidence.
    """
    if a is None or b is None:
        return 0.5
    if None not in (a.lat, a.lon, b.lat, b.lon):
        # Rough metres-per-degree at Dutch/Spanish latitudes; exact enough for
        # a similarity term, and avoids a geodesy dependency.
        dlat = (a.lat - b.lat) * 111_000
        dlon = (a.lon - b.lon) * 74_000
        metres = (dlat**2 + dlon**2) ** 0.5
        if metres < 100:
            return 1.0
        if metres > 2_000:
            return 0.0
        return 1.0 - (metres - 100) / 1_900
    if a.name and b.name:
        return fuzz.token_set_ratio(a.name.lower(), b.name.lower()) / 100.0
    return 0.5


def pair_score(a: RawRecord, b: RawRecord, locale: str = "nl") -> float:
    """Weighted similarity in [0, 1].

    Same source never merges with itself: if one venue lists an event twice
    that is a source-quality problem, and silently collapsing it hides the bug.
    """
    if a.source_id == b.source_id:
        return 0.0
    if abs((a.start - b.start)) > timedelta(days=1):
        return 0.0
    return (
        WEIGHTS["title"] * title_similarity(a.title, b.title, locale)
        + WEIGHTS["time"] * time_similarity(a.start, b.start)
        + WEIGHTS["venue"] * venue_similarity(a.venue, b.venue)
    )


# --------------------------------------------------------------------------
# 3. clustering
# --------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _merge_cluster(members: list[RawRecord], city: str) -> CanonicalEvent:
    """Build the canonical record, field by field, by trust precedence.

    Merging per-field rather than picking a single winning record matters:
    the municipal feed usually has the most reliable time, while the venue
    page usually has the better description. Taking the best of each beats
    taking all of the least-bad one.
    """
    ordered = sorted(members, key=lambda r: (r.trust.value, r.source_id))
    primary = ordered[0]

    def first(attr: str):
        for record in ordered:
            value = getattr(record, attr)
            if value is not None and value != "":
                return value
        return None

    venue = first("venue")
    # Prefer a geocoded venue even if it comes from a lower-trust source;
    # coordinates are objective in a way that free-text names are not.
    for record in ordered:
        if record.venue is not None and record.venue.lat is not None:
            venue = record.venue
            break
    if venue is not None and not venue.city:
        # Venue identity is keyed on name *and* city, so a source that omits the
        # city produces a different key for the same place: "Theater de Veste -
        # Delft" and "Theater de Veste (Delft)" normalise to the same name and
        # still land as two venues, two map pins and two rows in the cache.
        # City is a property of the merged event, so fill it in here.
        venue = venue.model_copy(update={"city": city})

    basis = f"{normalize_title(primary.title)}|{primary.start.isoformat()}|{city}"
    event_id = hashlib.sha1(basis.encode()).hexdigest()[:16]

    # More independent sources agreeing is genuine evidence the event is real.
    distinct_sources = len({m.source_id for m in members})
    confidence = min(1.0, 0.6 + 0.2 * distinct_sources)

    # Categorise against the pooled text of every member: the newspaper's
    # phrasing often names the genre the venue's own listing assumes.
    category = categorize(
        *(m.title for m in ordered),
        *(m.description for m in ordered if m.description),
        venue=venue.name if venue else None,
    )

    return CanonicalEvent(
        id=event_id,
        title=primary.title,
        start=primary.start,
        end=first("end"),
        venue=venue,
        description=max(
            (r.description for r in ordered if r.description),
            key=len,
            default=None,
        ),
        url=first("url"),
        is_free=first("is_free"),
        # Both were being extracted and then dropped on the floor here. A price
        # the venue published and a recurrence rule the venue's own calendar
        # stated are among the most useful things a source gives us, and the
        # trust order is exactly right for them: the organiser's price beats an
        # aggregator's guess at it.
        price=first("price"),
        rrule=first("rrule"),
        category=category,
        city=city,
        members=ordered,
        confidence=confidence,
    )


def deduplicate(
    records: list[RawRecord],
    city: str,
    locale: str = "nl",
    threshold: float = MERGE_THRESHOLD,
) -> list[CanonicalEvent]:
    """Collapse raw records into canonical events."""
    if not records:
        return []

    uf = _UnionFind(len(records))
    blocks = build_blocks(records, locale)

    compared: set[tuple[int, int]] = set()
    for indices in blocks.values():
        # A pathologically large block (a common trigram) would blow up the
        # comparison budget; blocking is a filter, not a promise.
        if len(indices) > 60:
            continue
        for i, left in enumerate(indices):
            for right in indices[i + 1 :]:
                pair = (left, right) if left < right else (right, left)
                if pair in compared:
                    continue
                compared.add(pair)
                if pair_score(records[left], records[right], locale) >= threshold:
                    uf.union(left, right)

    clusters: dict[int, list[RawRecord]] = defaultdict(list)
    for idx, record in enumerate(records):
        clusters[uf.find(idx)].append(record)

    events = [_merge_cluster(members, city) for members in clusters.values()]
    return sorted(events, key=lambda e: e.start)
