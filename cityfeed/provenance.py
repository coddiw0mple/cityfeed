"""Field-level provenance, derived confidence, and revision history.

Three things that are really one thing: knowing *where each field came from*.

`members[]` has always stored every source's full claim, so the raw material was
there — but `_merge_cluster` returned the winning value and threw away which
record produced it. That loses the only question a reviewer actually asks about
a merged record: not "why did these merge" but "why does it say 20:00 when the
newspaper says 19:30".

Once the winner is known per field, two more things follow for free:

**Confidence becomes derivable rather than invented.** A self-reported score
from an extractor is an opinion — it cannot know whether it was right. A score
computed from how the value was obtained can be checked: a `startDate` out of
JSON-LD is not the same evidence as a date scraped off a permalink with a
regex, and two independent sources agreeing is not the same as one asserting.

**Change becomes visible.** If the winning value for `start` moves from 19:00 to
20:00, that is a fact about the world worth keeping. Overwriting it is the
default and it is wrong: for a live-events product "doors moved an hour" is
precisely the thing a user needs and precisely what an UPDATE destroys.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .models import CanonicalEvent, RawRecord, SourceType, TrustTier

# How much the extraction tier itself vouches for a value.
#
# Not a guess about parser quality -- a statement about how far the value is
# from something the publisher explicitly asserted. A schema.org startDate is a
# machine-readable claim by the venue. A date recovered by regex from a
# permalink is an inference we made about their URL scheme, and it is right
# until they change the scheme without telling anyone.
TIER_EVIDENCE: dict[str, float] = {
    "ics": 0.95,          # RFC 5545, explicit DTSTART, unambiguous
    "jsonld": 0.95,       # publisher-asserted, typed, usually with an offset
    "jsonld_index": 0.95,
    "api": 0.92,          # typed, but field naming is the publisher's invention
    "wp_rest": 0.88,      # typed, and the ACF field names are per-site config
    "wrapper": 0.70,      # induced CSS selectors; correct until markup drifts
    "rss": 0.60,          # a feed is a list of posts that happens to have dates
    "prose": 0.40,
}

# Trust tier is about precedence when sources disagree, not about correctness.
# It moves confidence a little; it should not dominate it.
TRUST_EVIDENCE: dict[int, float] = {1: 1.00, 2: 0.98, 3: 0.95, 4: 0.90, 5: 0.85}

# Fields worth tracking individually. Deliberately not every field: provenance
# on `city` would be noise, and history on `confidence` would be recursive.
TRACKED_FIELDS = ("title", "start", "end", "venue", "url", "is_free", "price", "description")

SCHEMA = """
-- One row per (event, field): which source won it, and how sure we are.
CREATE TABLE IF NOT EXISTS field_provenance (
    event_id    TEXT NOT NULL,
    field       TEXT NOT NULL,
    source_id   TEXT,
    trust       INTEGER,
    tier        TEXT,
    confidence  REAL,
    agreeing    INTEGER,   -- how many sources asserted a matching value
    dissenting  INTEGER,   -- how many asserted something different
    updated_at  TEXT,
    PRIMARY KEY (event_id, field)
);

-- Append-only. An event's history is a fact about the world; overwriting it
-- means the answer to "did this move?" is permanently unavailable.
CREATE TABLE IF NOT EXISTS event_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    source_id   TEXT,
    changed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_event ON event_revisions (event_id, changed_at);
CREATE INDEX IF NOT EXISTS revisions_when ON event_revisions (changed_at);
"""


@dataclass
class FieldOrigin:
    """Where one field's value came from, and how much that is worth."""

    field: str
    value: Any
    source_id: Optional[str]
    trust: Optional[int]
    tier: Optional[str]
    agreeing: int = 1
    dissenting: int = 0

    @property
    def confidence(self) -> float:
        """Derived, never asserted.

        Three independent signals, multiplied rather than averaged so that a
        weak link cannot be hidden by strong ones:

        * how directly the publisher stated it (tier)
        * how much precedence the source has (trust)
        * whether anybody else independently said the same thing

        Corroboration is capped: a second source agreeing is strong evidence, a
        fifth adds almost nothing, and it can never certify a value the
        extractor had to guess at.
        """
        if self.value is None or self.value == "":
            return 0.0
        evidence = TIER_EVIDENCE.get(self.tier or "", 0.5)
        trust = TRUST_EVIDENCE.get(self.trust or 3, 0.9)

        # Independent agreement raises confidence with diminishing returns;
        # disagreement is a real signal that somebody is wrong.
        corroboration = 1.0 + min(0.25, 0.12 * (self.agreeing - 1))
        if self.dissenting:
            corroboration *= max(0.55, 1.0 - 0.18 * self.dissenting)

        return round(min(0.99, evidence * trust * corroboration), 3)

    def as_row(self, event_id: str, now: str) -> tuple:
        return (event_id, self.field, self.source_id, self.trust, self.tier,
                self.confidence, self.agreeing, self.dissenting, now)


def _comparable(field: str, value: Any) -> Any:
    """Normalise a value enough to ask 'did two sources say the same thing?'."""
    if value is None:
        return None
    if field == "venue":
        return getattr(value, "key", None) or str(getattr(value, "name", value)).lower()
    if field == "start" or field == "end":
        # Listings disagree about doors vs showtime; within an hour is agreement.
        return value.replace(minute=0, second=0, microsecond=0) if hasattr(value, "hour") else value
    if field == "title":
        from .normalize import normalize_title

        return normalize_title(str(value))
    if isinstance(value, str):
        return value.strip().lower()[:120]
    return value


def origins_for(
    event: CanonicalEvent, tiers: dict[str, str]
) -> dict[str, FieldOrigin]:
    """Work out, per field, which member supplied the winning value.

    `tiers` maps source_id -> extraction tier, which the event itself does not
    carry: a RawRecord knows which source it came from but not how that source
    is parsed, and the difference is most of what confidence is made of.
    """
    ordered: list[RawRecord] = sorted(event.members, key=lambda r: (r.trust.value, r.source_id))
    origins: dict[str, FieldOrigin] = {}

    for field in TRACKED_FIELDS:
        winner = getattr(event, field, None)
        if winner is None or winner == "":
            continue
        target = _comparable(field, winner)

        source_id = trust = None
        agreeing = dissenting = 0
        for record in ordered:
            claimed = getattr(record, field, None)
            if claimed is None or claimed == "":
                continue
            if _comparable(field, claimed) == target:
                agreeing += 1
                if source_id is None:            # first in trust order wins
                    source_id, trust = record.source_id, int(record.trust)
            else:
                dissenting += 1

        origins[field] = FieldOrigin(
            field=field, value=winner, source_id=source_id, trust=trust,
            tier=tiers.get(source_id or ""), agreeing=max(agreeing, 1),
            dissenting=dissenting,
        )
    return origins


def _serialise(field: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if field == "venue":
        return getattr(value, "name", None) or str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def record_provenance(
    conn: sqlite3.Connection,
    events: list[CanonicalEvent],
    tiers: dict[str, str],
    now: Optional[str] = None,
) -> tuple[int, int]:
    """Store field origins and append a revision for anything that changed.

    Returns (fields_written, revisions_appended). A revision is only appended
    when the *value* moved -- re-crawling an unchanged event writes nothing to
    the history, so the table stays a log of real changes rather than of crawls.
    """
    conn.executescript(SCHEMA)
    now = now or datetime.now(timezone.utc).isoformat()

    previous: dict[tuple[str, str], str] = {}
    ids = [e.id for e in events]
    if ids:
        marks = ",".join("?" * len(ids))
        for eid, field, val in conn.execute(
            f"SELECT event_id, field, new_value FROM event_revisions "
            f"WHERE event_id IN ({marks}) AND id IN "
            f"(SELECT MAX(id) FROM event_revisions WHERE event_id IN ({marks}) "
            f"GROUP BY event_id, field)",
            ids + ids,
        ):
            previous[(eid, field)] = val

    rows, revisions = [], []
    for event in events:
        for field, origin in origins_for(event, tiers).items():
            rows.append(origin.as_row(event.id, now))
            new_value = _serialise(field, origin.value)
            key = (event.id, field)
            if key in previous:
                if previous[key] != new_value:
                    revisions.append((event.id, field, previous[key], new_value,
                                      origin.source_id, now))
            else:
                # First sighting is the baseline, not a change.
                revisions.append((event.id, field, None, new_value, origin.source_id, now))

    conn.executemany(
        "INSERT OR REPLACE INTO field_provenance VALUES (?,?,?,?,?,?,?,?,?)", rows
    )
    conn.executemany(
        "INSERT INTO event_revisions (event_id, field, old_value, new_value, "
        "source_id, changed_at) VALUES (?,?,?,?,?,?)",
        revisions,
    )
    conn.commit()
    return len(rows), len([r for r in revisions if r[2] is not None])


def load_provenance(conn: sqlite3.Connection, event_id: str) -> dict[str, dict]:
    try:
        rows = conn.execute(
            "SELECT field, source_id, trust, tier, confidence, agreeing, dissenting "
            "FROM field_provenance WHERE event_id = ?", (event_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        r[0]: {"source_id": r[1], "trust": r[2], "tier": r[3],
               "confidence": r[4], "agreeing": r[5], "dissenting": r[6]}
        for r in rows
    }


def load_history(conn: sqlite3.Connection, event_id: str, limit: int = 50) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT field, old_value, new_value, source_id, changed_at "
            "FROM event_revisions WHERE event_id = ? AND old_value IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (event_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"field": r[0], "from": r[1], "to": r[2], "source_id": r[3], "changed_at": r[4]}
        for r in rows
    ]
