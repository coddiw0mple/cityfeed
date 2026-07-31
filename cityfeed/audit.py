"""Automated data-quality checks over what the pipeline actually stored.

The measurement harness answers "what did we miss?". This answers the other
half: "is what we kept any good?" — and it is the half that is easy to skip,
because bad rows do not raise. A newspaper column stored as an event at midnight
with no venue looks, to every counter in the system, exactly like a real event.
The source-health line says success, the dashboard renders it, and the only
thing that notices is a person reading the output.

So the checks here are written to fire on *plausible* data rather than on
crashes. Each one encodes a specific way this pipeline has been, or could
quietly be, wrong.

Severity means something operational:

    ERROR  a defect. The data is wrong, not merely suspicious, and something
           downstream will show a user something false. Fails the exit code.
    WARN   a strong smell. Usually right to fix, occasionally the real world.
    INFO   distributions and rates. No judgement, just the numbers you need to
           notice a change.

Nothing here mutates the database. An audit that repairs what it finds is an
audit whose findings you can never reproduce.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .normalize import normalize_title

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
MAX_EXAMPLES = 5


@dataclass
class Finding:
    check: str
    severity: str
    summary: str
    explanation: str
    count: int
    examples: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check": self.check, "severity": self.severity, "summary": self.summary,
            "explanation": self.explanation, "count": self.count,
            "examples": self.examples, "detail": self.detail,
        }


@dataclass
class AuditReport:
    city: str
    events: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    def counts(self) -> dict[str, int]:
        return Counter(f.severity for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "city": self.city, "events": self.events,
            "counts": dict(self.counts()),
            "findings": [f.to_dict() for f in self.findings],
        }

    def render(self) -> str:
        ordered = sorted(self.findings, key=lambda f: (_ORDER[f.severity], -f.count))
        counts = self.counts()
        lines = [
            f"data quality audit — {self.city}",
            "=" * 72,
            f"  {self.events} canonical events examined",
            f"  {counts.get(ERROR, 0)} error   {counts.get(WARN, 0)} warn   "
            f"{counts.get(INFO, 0)} info",
            "",
            f"{'severity':<9} {'count':>6}  check",
            "-" * 72,
        ]
        for f in ordered:
            lines.append(f"{f.severity:<9} {f.count:>6}  {f.check}")
        if not ordered:
            lines.append("(no findings)")

        lines += ["", "detail", "=" * 72]
        for f in ordered:
            lines += ["", f"[{f.severity}] {f.check} — {f.summary}", f"  {f.explanation}"]
            for ex in f.examples[:MAX_EXAMPLES]:
                lines.append(f"    · {ex}")
            if f.count > len(f.examples):
                lines.append(f"    … and {f.count - len(f.examples)} more")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

@dataclass
class AuditContext:
    city: str
    events: list[dict]
    venues: list[dict]
    now: datetime
    bbox: Optional[tuple[float, float, float, float]] = None
    source_ids: set[str] = field(default_factory=set)
    # What the registry says should be contributing, which is the only thing a
    # silence check can be measured against.
    enabled_sources: set[str] = field(default_factory=set)

    def by_source(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = defaultdict(list)
        for e in self.events:
            for sid in e["source_ids"]:
                out[sid].append(e)
        return out


def load_context(
    conn: sqlite3.Connection,
    city: str,
    now: Optional[datetime] = None,
    registry: str = "sources",
) -> AuditContext:
    from .geocode import CITY_BBOX

    conn.row_factory = sqlite3.Row
    events = []
    for row in conn.execute("SELECT * FROM events WHERE LOWER(city) = LOWER(?)", (city,)):
        keys = row.keys()
        events.append({
            "id": row["id"], "title": row["title"] or "",
            "start": datetime.fromisoformat(row["start"]),
            "end": datetime.fromisoformat(row["end"]) if row["end"] else None,
            "venue_id": row["venue_id"], "venue_name": row["venue_name"],
            "venue_lat": row["venue_lat"], "venue_lon": row["venue_lon"],
            "url": row["url"] or "", "is_free": row["is_free"], "price": row["price"],
            "rrule": row["rrule"],
            "description": (row["description"] if "description" in keys else None) or "",
            "category": row["category"],
            "source_ids": [s for s in (row["source_ids"] or "").split(",") if s],
            "members": json.loads(row["members"]) if row["members"] else [],
        })
    venues = [dict(r) for r in conn.execute(
        "SELECT * FROM venues WHERE LOWER(COALESCE(city,'')) = LOWER(?)", (city,)
    )]
    enabled: set[str] = set()
    try:
        from .fetch import load_registries

        enabled = {
            s.id for s in load_registries(registry)
            if s.enabled and s.city.lower() == city.lower()
        }
    except Exception:  # noqa: BLE001 - auditing a database without its registry is fine
        enabled = set()

    return AuditContext(
        city=city, events=events, venues=venues,
        now=now or datetime.now(timezone.utc),
        bbox=CITY_BBOX.get(city.lower()),
        source_ids={s for e in events for s in e["source_ids"]},
        enabled_sources=enabled,
    )


def _label(e: dict) -> str:
    when = e["start"].strftime("%Y-%m-%d %H:%M")
    return f"{when}  {e['title'][:52]}  [{','.join(e['source_ids'])[:34]}]"


def _finding(check, severity, summary, explanation, hits, detail=None) -> Optional[Finding]:
    """Build a finding, or None when nothing matched."""
    hits = list(hits)
    if not hits:
        return None
    return Finding(
        check=check, severity=severity, summary=summary, explanation=explanation,
        count=len(hits),
        examples=[h if isinstance(h, str) else _label(h) for h in hits[:MAX_EXAMPLES]],
        detail=detail or {},
    )


# --------------------------------------------------------------------------
# non-events
# --------------------------------------------------------------------------

_EDITORIAL_PATH = re.compile(r"/(news|column|article|nieuws|opinie|blog|artikel)/", re.I)
_EDITORIAL_TITLE = re.compile(
    r"^(column|opinie|interview|video|podcast|update|reactie|ingezonden)\b", re.I
)


def check_editorial_shape(ctx: AuditContext) -> Iterable[Finding]:
    hits = [
        e for e in ctx.events
        if e["start"].hour == 0 and e["start"].minute == 0
        and not e["venue_name"]
        and _EDITORIAL_PATH.search(e["url"] or "")
    ]
    yield _finding(
        "non_event.editorial_url", ERROR,
        "articles stored as events",
        "Midnight start, no venue, and an editorial URL path. This is a news "
        "item that was promoted to an event because it had a date on it.",
        hits,
    )

    titled = [e for e in ctx.events
              if _EDITORIAL_TITLE.match(e["title"]) or e["title"].rstrip().endswith("?")]
    yield _finding(
        "non_event.editorial_title", WARN,
        "titles that read like articles, not events",
        'Starts with Column/Opinie/Interview/Video/Podcast/Update/Reactie, or '
        'ends in a question mark. Events are rarely phrased as questions.',
        titled,
    )

    longform = [e for e in ctx.events
                if len(e["description"]) > 1500 and not e["venue_name"]]
    yield _finding(
        "non_event.longform_no_venue", WARN,
        "long-form text with no venue",
        "A description over 1500 characters with nowhere to attend it is the "
        "shape of an article body, not an event blurb.",
        longform,
    )


# --------------------------------------------------------------------------
# temporal
# --------------------------------------------------------------------------

def check_temporal(ctx: AuditContext) -> Iterable[Finding]:
    yield _finding(
        "temporal.past", INFO,
        "events already in the past",
        "Not wrong in itself — feeds carry history — but these should not be "
        "surfaced as upcoming, and a large share means the window is unbounded.",
        [e for e in ctx.events if e["start"] < ctx.now],
    )

    horizon = ctx.now + timedelta(days=456)  # ~15 months
    yield _finding(
        "temporal.far_future", WARN,
        "events more than 15 months out",
        "Beyond the horizon anyone programmes for. Usually a year parsed wrong "
        "(a compact date read as a year, or a two-digit year mis-expanded).",
        [e for e in ctx.events if e["start"] > horizon],
    )

    yield _finding(
        "temporal.small_hours", WARN,
        "events starting between 02:00 and 06:00",
        "Almost always a parse failure rather than a real 4am event: a time "
        "zone applied twice, or a 12/24-hour confusion.",
        [e for e in ctx.events if 2 <= e["start"].hour < 6],
    )

    yield _finding(
        "temporal.end_before_start", ERROR,
        "events that end before they begin",
        "Impossible. Usually an end time on the following day that never had "
        "the date rolled over.",
        [e for e in ctx.events if e["end"] and e["end"] < e["start"]],
    )

    yield _finding(
        "temporal.long_duration", WARN,
        "events longer than 14 hours",
        "Festivals and exhibitions legitimately run long; a 14-hour concert "
        "is a missing end date or a date-range flattened into one event.",
        [e for e in ctx.events
         if e["end"] and (e["end"] - e["start"]) > timedelta(hours=14)],
    )

    # Midnight starts, counted per source: the ratio is the signal, not the count.
    per_source = ctx.by_source()
    bad_sources = []
    for sid, evs in sorted(per_source.items()):
        midnight = sum(1 for e in evs if e["start"].hour == 0 and e["start"].minute == 0)
        if evs and midnight / len(evs) > 0.60:
            bad_sources.append(f"{sid}: {midnight}/{len(evs)} start at 00:00")
    yield _finding(
        "temporal.midnight_source", ERROR,
        "sources losing the time component",
        "Over 60% of a source's events start at exactly midnight. A venue does "
        "not programme at midnight; the time is being dropped in extraction.",
        bad_sources,
    )

    # A date that defaulted usually lands on the 1st.
    days = Counter(e["start"].day for e in ctx.events)
    if ctx.events:
        first_share = days.get(1, 0) / len(ctx.events)
        if first_share > 0.15 and days.get(1, 0) > 5:
            yield Finding(
                "temporal.first_of_month_cluster", WARN,
                "events clustered on the 1st of the month",
                f"{days[1]} of {len(ctx.events)} events ({first_share:.0%}) fall on "
                "a 1st. A date that failed to parse commonly defaults to the "
                "first of the month.",
                days[1],
                [_label(e) for e in ctx.events if e["start"].day == 1][:MAX_EXAMPLES],
            )


# --------------------------------------------------------------------------
# text quality
# --------------------------------------------------------------------------

_MOJIBAKE = re.compile(r"Ã[©¨«¯´¶¼½]|â€|Â[ ©«°»]|ï¿½|Ãƒ")
_ENTITIES = re.compile(r"&(amp|lt|gt|quot|nbsp|#\d+|#x[0-9a-fA-F]+);")
_TAGS = re.compile(r"<\s*/?\s*(p|br|div|span|a|strong|em|img|ul|li|h[1-6])\b[^>]*>", re.I)
_ONLY_DATE = re.compile(
    r"^[\s\d/.\-]+$|^(ma|di|wo|do|vr|za|zo|mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+\d+", re.I
)


def check_text(ctx: AuditContext) -> Iterable[Finding]:
    yield _finding(
        "text.mojibake", ERROR,
        "encoding corruption in text",
        "UTF-8 bytes decoded as Latin-1: 'CafÃ©' instead of 'Café'. Extremely "
        "common on Dutch venue sites, crashes nothing, and is visible to every "
        "user. Fix at fetch time, not by patching strings later.",
        [e for e in ctx.events
         if _MOJIBAKE.search(e["title"]) or _MOJIBAKE.search(e["description"])],
    )

    yield _finding(
        "text.html_entities", ERROR,
        "unescaped HTML entities in titles",
        "'Jazz &amp; Blues' means the payload was decoded once too few times. "
        "It reaches the user literally.",
        [e for e in ctx.events if _ENTITIES.search(e["title"])],
    )

    yield _finding(
        "text.raw_html", ERROR,
        "raw HTML tags in text fields",
        "Markup that should have been stripped during extraction.",
        [e for e in ctx.events
         if _TAGS.search(e["title"]) or _TAGS.search(e["description"])],
    )

    shape = []
    for e in ctx.events:
        t = e["title"]
        letters = [c for c in t if c.isalpha()]
        if len(t) < 4:
            shape.append(f"too short ({len(t)}): {t!r}")
        elif len(t) > 180:
            shape.append(f"too long ({len(t)}): {t[:60]!r}…")
        elif len(letters) >= 8 and all(c.isupper() for c in letters):
            shape.append(f"all caps: {t[:60]!r}")
    yield _finding(
        "text.title_shape", WARN,
        "titles of implausible shape",
        "Under 4 characters, over 180, or entirely uppercase. Each usually "
        "means the wrong element was selected.",
        shape,
    )

    yield _finding(
        "text.title_truncated", WARN,
        "titles that look truncated",
        "Ending in an ellipsis is a listing page's preview text rather than the "
        "event's actual name.",
        [e for e in ctx.events if e["title"].rstrip().endswith(("...", "…"))],
    )

    yield _finding(
        "text.title_is_date_or_venue", WARN,
        "titles that are a date or the venue name",
        "The title selector matched the wrong element: what is stored is when "
        "or where, not what.",
        [e for e in ctx.events
         if _ONLY_DATE.match(e["title"])
         or (e["venue_name"] and e["title"].strip().lower() == e["venue_name"].strip().lower())],
    )

    yield _finding(
        "text.whitespace_punct", INFO,
        "titles with stray whitespace or punctuation",
        "Cosmetic, but it shows the text was concatenated from fragments.",
        [e for e in ctx.events
         if re.search(r"\s{2,}", e["title"])
         or e["title"] != e["title"].strip()
         or e["title"].strip().startswith((",", ".", "-", "|", "·"))],
    )


# --------------------------------------------------------------------------
# venue
# --------------------------------------------------------------------------

def check_venue(ctx: AuditContext) -> Iterable[Finding]:
    total = len(ctx.venues)
    ungeocoded = [v for v in ctx.venues if v["lat"] is None]
    if total:
        yield Finding(
            "venue.ungeocoded_rate", INFO,
            "venues without coordinates",
            f"{len(ungeocoded)}/{total} venues ({len(ungeocoded)/total:.0%}) have no "
            "lat/lon, covering "
            f"{sum(1 for e in ctx.events if e['venue_lat'] is None)} events. The "
            "per-source split below says whether this is a geocoder problem or a "
            "source publishing unresolvable names.",
            len(ungeocoded),
            [v["name"][:60] for v in ungeocoded[:MAX_EXAMPLES]],
        )

    per_source_rows = []
    for sid, evs in sorted(ctx.by_source().items()):
        missing = sum(1 for e in evs if e["venue_lat"] is None)
        if missing:
            per_source_rows.append(f"{sid}: {missing}/{len(evs)} events ungeocoded")
    yield _finding(
        "venue.ungeocoded_per_source", INFO,
        "ungeocoded events by source",
        "A source concentrated here is publishing room names or generic "
        "labels; a flat spread points at the geocoder.",
        per_source_rows,
    )

    bad = []
    for v in ctx.venues:
        name = (v["name"] or "").strip()
        if not name:
            bad.append(f"empty venue name (id {v['id']})")
        elif name.lower() == ctx.city.lower():
            bad.append(f"venue named after the city: {name!r}")
        elif re.search(r"https?://|www\.", name):
            bad.append(f"URL in venue name: {name[:60]!r}")
    yield _finding(
        "venue.bad_name", WARN,
        "venue names that are not venue names",
        "Empty, identical to the city, or containing a URL. None of these can "
        "be geocoded and none mean anything to a reader.",
        bad,
    )

    if ctx.bbox:
        min_lat, max_lat, min_lon, max_lon = ctx.bbox
        outside = [
            f"{v['name'][:44]} at {v['lat']:.4f},{v['lon']:.4f}"
            for v in ctx.venues
            if v["lat"] is not None and v["lon"] is not None
            and not (min_lat <= v["lat"] <= max_lat and min_lon <= v["lon"] <= max_lon)
        ]
        yield _finding(
            "venue.outside_bbox", ERROR,
            "venue coordinates outside the city",
            "A geocoder returned a same-named place elsewhere and it was "
            "stored. The bounding-box check should have rejected it.",
            outside,
        )

    # Venues that should have resolved to one entity.
    from rapidfuzz import fuzz

    near = []
    named = [(v, normalize_title(v["name"] or "")) for v in ctx.venues]
    for i, (a, na) in enumerate(named):
        for b, nb in named[i + 1:]:
            if not na or not nb or na == nb:
                continue
            if fuzz.ratio(na, nb) > 90:
                near.append(f"{a['name'][:34]!r} ≈ {b['name'][:34]!r}")
    yield _finding(
        "venue.near_duplicates", WARN,
        "distinct venues with near-identical names",
        "These are probably one building. Each extra row is a duplicate map "
        "pin and a wasted geocode.",
        near,
    )


# --------------------------------------------------------------------------
# dedup health
# --------------------------------------------------------------------------

def check_dedup(ctx: AuditContext) -> Iterable[Finding]:
    from rapidfuzz import fuzz

    by_day: dict[Any, list[dict]] = defaultdict(list)
    for e in ctx.events:
        by_day[e["start"].date()].append(e)

    missed = []
    for _day, evs in sorted(by_day.items()):
        if len(evs) > 120:  # pathological day, skip the O(n^2)
            continue
        for i, a in enumerate(evs):
            for b in evs[i + 1:]:
                # Same-source pairs are excluded by design: dedup deliberately
                # never merges them, so counting them here would measure the
                # policy rather than the matcher's recall.
                if set(a["source_ids"]) & set(b["source_ids"]):
                    continue
                na, nb = normalize_title(a["title"]), normalize_title(b["title"])
                if not na or not nb:
                    continue
                if fuzz.token_set_ratio(na, nb) / 100.0 > 0.85:
                    missed.append(
                        f"{a['start']:%Y-%m-%d %H:%M} {a['title'][:34]!r} "
                        f"[{','.join(a['source_ids'])}] vs "
                        f"{b['start']:%H:%M} {b['title'][:34]!r} "
                        f"[{','.join(b['source_ids'])}]"
                    )
    yield _finding(
        "dedup.missed_merges", WARN,
        "same-day near-identical events from different sources that did not merge",
        "This is dedup recall. Each pair is two listings of one event shown to "
        "the user twice. Look for a systematic pattern before tuning anything.",
        missed,
    )

    time_disagree, title_disagree, same_source = [], [], []
    for e in ctx.events:
        members = e["members"]
        if len(members) < 2:
            continue
        starts = [datetime.fromisoformat(m["start"]) for m in members if m.get("start")]
        # Exact midnight means "this source published no time", not "starts at
        # 00:00" -- the same reading dedup uses. Counting it as a 19-hour
        # disagreement would flag every correct merge with a date-only source
        # on one side, which is noise, not signal.
        starts = [s for s in starts if (s.hour, s.minute, s.second) != (0, 0, 0)] or starts
        if len(starts) > 1 and (max(starts) - min(starts)) > timedelta(hours=3):
            time_disagree.append(
                f"{e['title'][:40]!r} members span "
                f"{(max(starts) - min(starts)).total_seconds() / 3600:.1f}h"
            )
        titles = [m.get("title", "") for m in members]
        for i, ta in enumerate(titles):
            for tb in titles[i + 1:]:
                if fuzz.token_set_ratio(normalize_title(ta), normalize_title(tb)) / 100.0 < 0.5:
                    title_disagree.append(f"{ta[:34]!r} merged with {tb[:34]!r}")
        sids = [m["source_id"] for m in members]
        if len(sids) != len(set(sids)):
            same_source.append(f"{e['title'][:44]!r} has {len(sids)} members from {len(set(sids))} sources")

    yield _finding(
        "dedup.over_merge_time", WARN,
        "merged events whose sources disagree by more than 3 hours",
        "Either two different events were collapsed, or one source has the "
        "time badly wrong. Both are worth looking at.",
        time_disagree,
    )
    yield _finding(
        "dedup.over_merge_title", WARN,
        "merged events with barely-related member titles",
        "Pairwise title similarity below 0.5 inside one cluster. The blocking "
        "keys brought them together and the score let them through.",
        title_disagree,
    )
    yield _finding(
        "dedup.same_source_cluster", ERROR,
        "clusters containing two records from one source",
        "pair_score() returns 0 for same-source pairs, so this should be "
        "unreachable. Any hit is a real bug in clustering or persistence.",
        same_source,
    )


# --------------------------------------------------------------------------
# volume and distribution
# --------------------------------------------------------------------------

def check_volume(ctx: AuditContext) -> Iterable[Finding]:
    total = len(ctx.events)
    if not total:
        return

    dominant = []
    for sid, evs in sorted(ctx.by_source().items(), key=lambda kv: -len(kv[1])):
        share = len(evs) / total
        if share > 0.40:
            dominant.append(f"{sid}: {len(evs)}/{total} events ({share:.0%})")
    yield _finding(
        "volume.source_dominance", WARN,
        "one source contributing over 40% of all events",
        "A cinema with several screenings a day swamps every other source and "
        "distorts every rate computed over the corpus. Collapse repeats into a "
        "series rather than storing each showing as its own event.",
        dominant,
    )

    titles = Counter(normalize_title(e["title"]) for e in ctx.events if e["title"])
    repeated = [f"{t!r} × {n}" for t, n in titles.most_common() if n > 8]
    yield _finding(
        "volume.repeated_title", WARN,
        "the same title appearing more than 8 times",
        "A recurring series that was never collapsed. It belongs in the "
        "occurrences table as one event with many dates.",
        repeated,
    )

    # Compared against the registry, not against the events: deriving the
    # source list from the events themselves can only ever produce sources that
    # have events, so the check would be structurally unable to fire.
    produced = {s for s, evs in ctx.by_source().items() if evs}
    silent = sorted(sid for sid in ctx.enabled_sources if sid not in produced)
    yield _finding(
        "volume.silent_source", WARN,
        "enabled sources that produced no events",
        "A source that quietly stops yielding looks exactly like a quiet week. "
        "The registry says it should be contributing; the database says it is not.",
        silent,
    )

    cats = Counter(e["category"] or "(uncategorised)" for e in ctx.events)
    unc = cats.get("(uncategorised)", 0)
    yield Finding(
        "distribution.category", INFO,
        "category distribution",
        f"{unc}/{total} uncategorised ({unc/total:.0%}). The categoriser returns "
        "None rather than guessing, so this is a coverage figure, not an error.",
        total,
        [f"{c}: {n}" for c, n in cats.most_common(8)],
    )

    free = Counter(
        "free" if e["is_free"] == 1 else "paid" if e["is_free"] == 0 else "unknown"
        for e in ctx.events
    )
    yield Finding(
        "distribution.is_free", INFO,
        "free/paid distribution",
        f"{free.get('unknown', 0)}/{total} unknown "
        f"({free.get('unknown', 0)/total:.0%}). Silence is recorded as unknown "
        "rather than guessed as paid.",
        total,
        [f"{k}: {v}" for k, v in free.most_common()],
    )


# --------------------------------------------------------------------------
# consistency
# --------------------------------------------------------------------------

def check_consistency(ctx: AuditContext, conn: sqlite3.Connection) -> Iterable[Finding]:
    yield _finding(
        "consistency.free_with_price", WARN,
        "events marked free that also carry a price",
        "One of the two fields is wrong and the UI has to pick one.",
        [e for e in ctx.events if e["is_free"] == 1 and e["price"]],
    )

    dupes = [
        f"{row[0]} × {row[1]}"
        for row in conn.execute(
            "SELECT id, count(*) c FROM events GROUP BY id HAVING c > 1"
        )
    ]
    yield _finding(
        "consistency.duplicate_ids", ERROR,
        "duplicate canonical ids",
        "The primary key should make this impossible; if it fires, persistence "
        "is writing through a path that bypasses it.",
        dupes,
    )

    known = {v["id"] for v in ctx.venues}
    orphans = [
        _label(e) for e in ctx.events
        if e["venue_id"] and e["venue_id"] not in known
    ]
    yield _finding(
        "consistency.orphan_venue_ref", ERROR,
        "events referencing a venue that does not exist",
        "A dangling foreign key: the event claims a venue the venues table has "
        "never heard of, so the map and the venue endpoint disagree.",
        orphans,
    )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

CHECKS = [
    check_editorial_shape,
    check_temporal,
    check_text,
    check_venue,
    check_dedup,
    check_volume,
]


def audit(
    conn: sqlite3.Connection,
    city: str,
    now: Optional[datetime] = None,
    registry: str = "sources",
) -> AuditReport:
    ctx = load_context(conn, city, now, registry)
    report = AuditReport(city=city, events=len(ctx.events))
    for check in CHECKS:
        for finding in check(ctx):
            if finding is not None:
                report.findings.append(finding)
    for finding in check_consistency(ctx, conn):
        if finding is not None:
            report.findings.append(finding)
    return report
