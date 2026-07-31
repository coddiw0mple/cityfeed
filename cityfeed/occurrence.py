"""Turning a series into dates.

The pipeline deliberately keeps a recurring event as one canonical record with
its RRULE intact: a weekly pub quiz is one event, and flattening it into 52
canonical rows would destroy the only fact that makes it dedupable — every one
of those rows would look like a separate event to be matched against every
other source's separate rows.

But "what is on next Wednesday, and what does it cost?" is a question about a
date, not a series, and answering it by re-expanding RRULEs at query time is
both slow and untestable. So dates are materialised here into their own table,
downstream of dedup and derived from it.

Two traps do most of the damage in this area, and both are silent.

**DST.** A weekly 20:00 concert must stay at 20:00 local across the October
change, not drift to 19:00. That means expanding in the venue's local wall
clock and attaching the offset afterwards — expanding in UTC gives you an event
that moves by an hour twice a year, which looks like a data-entry error and is
impossible to explain to a venue.

**Short months.** `FREQ=MONTHLY;BYMONTHDAY=31` simply has no occurrence in
February, and dateutil is right to skip it. The bug is in code that "fixes"
this by clamping to the 28th, inventing an event nobody scheduled.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .models import CanonicalEvent, Occurrence

# How far ahead to materialise. Long enough to answer "what's on this season",
# short enough that an unbounded RRULE (COUNT and UNTIL are both optional, and
# plenty of feeds omit both) cannot generate an unbounded table.
DEFAULT_HORIZON_DAYS = 90
MAX_OCCURRENCES = 400


def _split_rrule(rrule: str) -> tuple[list[str], list[str], list[str]]:
    """Separate RRULE/RDATE/EXDATE lines out of a serialised recurrence blob.

    ICS carries these as sibling properties, and extract_ics stores what it was
    given. A blob with an EXDATE that gets parsed as part of the RRULE silently
    produces the cancelled dates it was supposed to remove.
    """
    rules: list[str] = []
    rdates: list[str] = []
    exdates: list[str] = []
    for raw_line in rrule.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        name, _, value = line.partition(":")
        name = name.split(";")[0].upper()
        if name == "EXDATE":
            exdates.extend(v.strip() for v in value.split(",") if v.strip())
        elif name == "RDATE":
            rdates.extend(v.strip() for v in value.split(",") if v.strip())
        elif name == "RRULE":
            rules.append(value.strip())
        else:
            # A bare "FREQ=WEEKLY;BYDAY=TU" with no property name is what
            # icalendar's to_ical() hands back, and it is the common case.
            rules.append(line)
    return rules, rdates, exdates


# RFC 5545 basic format: 20260908T200000, optionally suffixed Z for UTC.
_ICS_COMPACT = re.compile(
    r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})"
    r"(?:T(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})?)?(?P<z>Z)?$"
)


def _parse_ics_datetime(value: str, tz: ZoneInfo) -> Optional[datetime]:
    """Parse one EXDATE/RDATE value.

    The compact form must be handled explicitly rather than handed to dateutil.
    With `dayfirst=True` — right for European free text, and what the shared
    parser defaults to — dateutil reads `20260908T200000` as the 9th of August
    rather than the 8th of September. On an EXDATE that means quietly deleting
    a date the venue never cancelled and keeping the one it did, with nothing
    anywhere reporting an error.
    """
    from .normalize import parse_datetime

    raw = value.strip()
    if ":" in raw:  # "TZID=Europe/Amsterdam:20260908T200000"
        raw = raw.rsplit(":", 1)[1]

    if match := _ICS_COMPACT.match(raw):
        parts = match.groupdict()
        naive = datetime(
            int(parts["y"]), int(parts["m"]), int(parts["d"]),
            int(parts["H"] or 0), int(parts["M"] or 0), int(parts["S"] or 0),
        )
        if parts["z"]:
            from datetime import timezone as _tz

            return naive.replace(tzinfo=_tz.utc).astimezone(tz)
        return naive.replace(tzinfo=tz)

    return parse_datetime(raw, str(tz))


def expand_rrule(
    start: datetime,
    rrule: Optional[str],
    timezone: str = "Europe/Amsterdam",
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    now: Optional[datetime] = None,
) -> list[datetime]:
    """Dates this series falls on, from `now` to the horizon.

    A non-recurring event yields exactly its own start, so callers never need a
    special case for the overwhelmingly common shape.
    """
    tz = ZoneInfo(timezone)
    now = now or datetime.now(tz)
    horizon = now + timedelta(days=horizon_days)

    if not rrule or not rrule.strip():
        return [start]

    from dateutil.rrule import rrulestr

    rules, rdates, exdates = _split_rrule(rrule)
    if not rules and not rdates:
        return [start]

    # Expand against a naive local wall clock, then re-attach the zone. This is
    # the DST fix: dateutil steps in absolute time, so a tz-aware 20:00 start
    # yields 20:00, 20:00, ... 19:00 once the offset changes. Stepping the wall
    # clock and localising afterwards keeps every occurrence at 20:00 local.
    naive_start = start.astimezone(tz).replace(tzinfo=None)
    try:
        if rules:
            rule = rrulestr("\n".join(rules), dtstart=naive_start, forceset=True)
        else:
            # RDATE with no RRULE is a legitimate shape -- it is what repeat
            # collapsing produces, an explicit list of dates with no pattern.
            # rrulestr needs a rule, so the set is built directly instead.
            from dateutil.rrule import rruleset

            rule = rruleset()
            rule.rdate(naive_start)
    except (ValueError, TypeError):
        # An unparseable rule is a source-quality problem, not a reason to lose
        # the event: fall back to the single date we were given.
        return [start]

    for value in exdates:
        if (parsed := _parse_ics_datetime(value, tz)) is not None:
            rule.exdate(parsed.astimezone(tz).replace(tzinfo=None))
    for value in rdates:
        if (parsed := _parse_ics_datetime(value, tz)) is not None:
            rule.rdate(parsed.astimezone(tz).replace(tzinfo=None))

    window_start = min(naive_start, now.astimezone(tz).replace(tzinfo=None))
    horizon_naive = horizon.astimezone(tz).replace(tzinfo=None)

    dates: list[datetime] = []
    for naive in rule:
        if naive < window_start:
            continue
        if naive > horizon_naive:
            break
        # fold=0 resolves the repeated hour of the autumn change to the first
        # pass, which is the one a venue means when it says "20:00".
        dates.append(naive.replace(tzinfo=tz, fold=0))
        if len(dates) >= MAX_OCCURRENCES:
            break
    return dates or [start]


def occurrences_for(
    event: CanonicalEvent,
    timezone: str = "Europe/Amsterdam",
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    now: Optional[datetime] = None,
) -> list[Occurrence]:
    """Materialise one event's dates, inheriting series-level price and free-ness."""
    duration = (event.end - event.start) if event.end else None
    rows: list[Occurrence] = []
    for start in expand_rrule(event.start, event.rrule, timezone, horizon_days, now):
        rows.append(
            Occurrence(
                id=Occurrence.make_id(event.id, start),
                event_id=event.id,
                start=start,
                end=start + duration if duration else None,
                is_free=event.is_free,
                price=event.price,
            )
        )
    return rows


def expand_all(
    events: Iterable[CanonicalEvent],
    timezone: str = "Europe/Amsterdam",
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    now: Optional[datetime] = None,
) -> list[Occurrence]:
    rows: list[Occurrence] = []
    for event in events:
        rows.extend(occurrences_for(event, timezone, horizon_days, now))
    return rows


def collapse_repeats(records: list, spec) -> list:
    """Fold repeated showings of one title at one venue into a single record.

    A cinema publishes one row per screening: the same film five times a day,
    thirty times a week. Each is a real showing, but they are not thirty events
    — they are one film with thirty dates, and storing them as separate
    canonical events distorts every count in the system: the source contributes
    40% of the corpus, "same title × 11" fires, and the map shows one venue with
    seventy identical pins.

    The dates are not lost. The earliest record becomes the series and the rest
    become RDATEs on it, so the occurrence expansion materialises every showing
    exactly as before. What changes is that dedup, the category counts and the
    dashboard now see one event, which is what a reader means by one.

    Deliberately keyed on title *and* venue: the same film at two cinemas is two
    programmes, and a reader choosing where to go needs both.
    """
    from collections import defaultdict

    from .normalize import normalize_title

    if not getattr(spec, "collapse_repeats", False):
        return records

    groups: dict[tuple[str, str], list] = defaultdict(list)
    for record in records:
        venue = record.venue.name if record.venue else ""
        groups[(normalize_title(record.title), venue)].append(record)

    collapsed = []
    for members in groups.values():
        members.sort(key=lambda r: r.start)
        primary = members[0]
        if len(members) > 1:
            # RDATE carries the remaining showings, so nothing is dropped and
            # the existing expander needs no special case for this shape.
            extra = ",".join(
                r.start.strftime("%Y%m%dT%H%M%S") for r in members[1:]
            )
            rule = f"RDATE:{extra}"
            primary = primary.model_copy(
                update={"rrule": f"{primary.rrule}\n{rule}" if primary.rrule else rule}
            )
        collapsed.append(primary)
    return collapsed
