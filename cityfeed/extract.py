"""Extraction tiers.

The cost ladder that the whole project rests on:

    tier 0  jsonld / ics / rss / api   ->  0 model tokens, deterministic
    tier 1  wrapper                    ->  1 model call per *domain*, cached forever
    tier 2  prose                      ->  1 model call per *page*, last resort

Most event data on the public web is tier 0 and people pay tier 2 prices for it
because nobody checked. Every source moved down a tier is permanent margin.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from lxml import html as lxml_html

from .models import RawRecord, SourceSpec, Venue
from .normalize import (
    clean_text,
    detect_free,
    parse_datetime,
    parse_price,
    strip_html,
)


@dataclass
class ExtractContext:
    """Side-channel for extraction: what the store knows, and what got dropped.

    Kept out of SourceSpec because none of it is configuration -- it is state
    the pipeline has accumulated (known venue names) and state extraction
    produces (drop reasons). Optional everywhere, so tests and the probe can
    call the extractors with nothing.
    """

    known_venues: set[str] = dataclass_field(default_factory=set)
    drops: Counter = dataclass_field(default_factory=Counter)

    def drop(self, source_id: str, reason: str) -> None:
        self.drops[(source_id, reason)] += 1

    def for_source(self, source_id: str) -> dict[str, int]:
        return {r: n for (s, r), n in self.drops.items() if s == source_id}


class ExtractionError(Exception):
    """Raised when a source's payload cannot be parsed at all.

    Deliberately loud. A source that silently yields zero events looks identical
    to a quiet week, and that is how coverage rots without anyone noticing.
    """


# --------------------------------------------------------------------------
# tier 0
# --------------------------------------------------------------------------

def _iter_jsonld_blocks(doc: str) -> Iterable[Any]:
    tree = lxml_html.fromstring(doc)
    for node in tree.xpath('//script[@type="application/ld+json"]'):
        text = node.text_content()
        if not text or not text.strip():
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            # Trailing commas and unescaped newlines are endemic in the wild.
            # A malformed block is not a reason to abandon the page.
            continue


# Keys whose values hold further schema.org nodes. `itemListElement` is the one
# that matters commercially: an "uitagenda" page publishes its entire programme
# as an ItemList of ListItems wrapping Events, and a flattener that only knows
# about @graph reports zero events on a page that is carrying fifty.
_JSONLD_CONTAINERS = ("@graph", "itemListElement", "item", "subEvent", "mainEntity")


def _flatten_jsonld(block: Any) -> Iterable[dict]:
    """Walk every node in a JSON-LD block.

    Yields containers as well as leaves and lets the caller filter. Doing it the
    other way round -- deciding what to descend into based on @type -- means
    every unanticipated wrapper type silently swallows its contents.
    """
    if isinstance(block, list):
        for item in block:
            yield from _flatten_jsonld(item)
    elif isinstance(block, dict):
        yield block
        for key in _JSONLD_CONTAINERS:
            value = block.get(key)
            if isinstance(value, (dict, list)):
                yield from _flatten_jsonld(value)


def _is_event(node: dict) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, list):
        return any("Event" in str(t) for t in node_type)
    return "Event" in str(node_type or "")


def _venue_from_jsonld(node: dict) -> Optional[Venue]:
    location = node.get("location")
    if isinstance(location, list):
        location = location[0] if location else None
    if isinstance(location, str):
        return Venue(name=clean_text(location) or location)
    if not isinstance(location, dict):
        return None

    address = location.get("address")
    address_str = None
    city = None
    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"),
            address.get("postalCode"),
            address.get("addressLocality"),
        ]
        address_str = clean_text(", ".join(p for p in parts if p))
        city = clean_text(address.get("addressLocality"))
    elif isinstance(address, str):
        address_str = clean_text(address)

    geo = location.get("geo") or {}
    lat = lon = None
    if isinstance(geo, dict):
        try:
            lat = float(geo["latitude"]) if geo.get("latitude") is not None else None
            lon = float(geo["longitude"]) if geo.get("longitude") is not None else None
        except (TypeError, ValueError):
            lat = lon = None

    name = clean_text(location.get("name")) or address_str or "Unknown venue"
    return Venue(name=name, address=address_str, city=city, lat=lat, lon=lon)


def _offers_blob(node: dict) -> str:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return ""
    return " ".join(
        str(offers.get(k, "")) for k in ("price", "priceCurrency", "description", "name")
    )


_DATE_ONLY = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")


def _start_with_door_time(raw_start: Any, node: dict) -> Any:
    """Recover a start time from `doorTime` when `startDate` is a bare date.

    A widespread and costly shape: the listing says `"startDate": "2026-08-04"`
    and puts the actual clock time in `doorTime`. Parsed literally that is an
    event at midnight — twenty hours away from when it happens, which is far
    enough that it will not match the same event from any other source, so the
    listing survives as a permanent duplicate and every time shown to a user is
    wrong.

    Only ever fills in a time that is missing; a startDate that already states
    one is left exactly as it is.
    """
    if not isinstance(raw_start, str) or not _DATE_ONLY.match(raw_start):
        return raw_start
    door = node.get("doorTime") or node.get("doortime")
    if not isinstance(door, str) or not door.strip():
        return raw_start
    # doorTime is sometimes a full datetime and sometimes a bare clock time.
    if match := re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", door):
        return f"{raw_start.strip()} {match.group(1)}"
    return raw_start


def extract_jsonld(doc: str, spec: SourceSpec) -> list[RawRecord]:
    """Parse schema.org/Event out of embedded JSON-LD.

    This single function covers most WordPress event plugins, Squarespace,
    Eventbrite pages and a good share of municipal CMSs.
    """
    records: list[RawRecord] = []
    # Walking every container yields the same event twice when a page lists it
    # in both @graph and an ItemList, and real pages reuse one @id across a
    # whole run of shows. Same-source records never merge in dedup by design,
    # so a duplicate here survives all the way to the user as a double listing.
    seen: set[tuple[str, str]] = set()
    for block in _iter_jsonld_blocks(doc):
        for node in _flatten_jsonld(block):
            if not _is_event(node):
                continue
            title = clean_text(node.get("name"))
            # Case in JSON-LD keys is not reliably schema.org's: real feeds ship
            # "URL" and "startdate" alongside the correct spelling.
            raw_start = node.get("startDate") or node.get("startdate")
            start = parse_datetime(
                _start_with_door_time(raw_start, node), spec.timezone
            )
            if not title or start is None:
                continue  # an event without a name or a time is not an event
            key = (title.lower(), start.isoformat())
            if key in seen:
                continue
            seen.add(key)
            offers = _offers_blob(node)
            description = clean_text(strip_html(node.get("description")))
            records.append(
                RawRecord(
                    source_id=spec.id,
                    source_url=spec.url,
                    trust=spec.trust,
                    title=title,
                    start=start,
                    end=parse_datetime(
                        node.get("endDate") or node.get("enddate"), spec.timezone
                    ),
                    description=description,
                    venue=_venue_from_jsonld(node),
                    url=clean_text(node.get("url") or node.get("URL")) or spec.url,
                    price=parse_price(offers),
                    is_free=detect_free(offers, description, title, locale=spec.locale),
                )
            )
    return records


def extract_ics(doc: str, spec: SourceSpec) -> list[RawRecord]:
    """Parse an RFC 5545 calendar feed.

    RRULE is carried through verbatim rather than expanded. Expanding a weekly
    event into 52 rows destroys the fact that it is one event, which is exactly
    the fact dedup and the UI both need.
    """
    from icalendar import Calendar

    try:
        cal = Calendar.from_ical(doc)
    except Exception as exc:  # icalendar raises bare ValueError subclasses
        raise ExtractionError(f"{spec.id}: unparseable ics feed") from exc

    records: list[RawRecord] = []
    for component in cal.walk("VEVENT"):
        title = clean_text(strip_html(str(component.get("SUMMARY", ""))))
        raw_start = component.get("DTSTART")
        if not title or raw_start is None:
            continue
        start = parse_datetime(raw_start.dt, spec.timezone)
        if start is None:
            continue
        raw_end = component.get("DTEND")
        location = clean_text(strip_html(str(component.get("LOCATION", "")) or ""))
        description = clean_text(strip_html(str(component.get("DESCRIPTION", "")) or ""))
        rrule = component.get("RRULE")
        records.append(
            RawRecord(
                source_id=spec.id,
                source_url=spec.url,
                trust=spec.trust,
                title=title,
                start=start,
                end=parse_datetime(raw_end.dt, spec.timezone) if raw_end is not None else None,
                rrule=rrule.to_ical().decode() if rrule is not None else None,
                description=description,
                venue=Venue(name=location) if location else None,
                url=clean_text(str(component.get("URL", "")) or "") or spec.url,
                is_free=detect_free(description, title, locale=spec.locale),
            )
        )
    return records


# Feed extensions that carry a genuine event start: the xCal and Event modules,
# and the ad-hoc spellings WordPress event plugins emit.
_RSS_EVENT_DATE_KEYS = (
    "start_time", "startdate", "start_date", "dtstart",
    "xcal_dtstart", "ev_startdate", "event_start", "eventdate",
)


# URL shapes that mean "this is a thing you attend" rather than "this is a
# thing you read". The editorial set is checked first because a path can match
# both (/nieuws/agenda-tips/).
_EDITORIAL_PATH = re.compile(
    r"/(news|nieuws|column|columns|article|artikel|opinie|blog|interview|podcast|video)(/|$)",
    re.I,
)
_EVENT_PATH = re.compile(
    r"/(events?|evenement(en)?|agenda|programma|voorstelling(en)?|activiteit(en)?"
    r"|concert(en)?|kalender|calendar|show|tickets?)(/|$)",
    re.I,
)


def _rss_event_signal(
    record_url: str,
    start: datetime,
    blob: str,
    known_venues: set[str],
) -> Optional[str]:
    """Does anything about this feed item say it is an event?

    A feed is a list of *posts*. Some of those posts are events and most are
    not, and the only thing they all share is a date -- which is why dating an
    item is not enough to promote it. Requiring a second, independent signal is
    what keeps a venue's blog from becoming twenty phantom events.

    Returns the name of the signal that fired, or None to drop the item.
    """
    path = urlparse(record_url or "").path
    if path and _EDITORIAL_PATH.search(path):
        return None  # explicitly an article; no other signal rescues it
    if path and _EVENT_PATH.search(path):
        return "event_url"
    if (start.hour, start.minute) != (0, 0):
        return "clock_time"
    lowered = blob.lower()
    for venue in known_venues:
        if len(venue) > 4 and venue in lowered:
            return "known_venue"
    return None


def extract_rss(
    doc: str, spec: SourceSpec, context: Optional["ExtractContext"] = None
) -> list[RawRecord]:
    """Parse an RSS/Atom feed.

    The trap this function exists to avoid: `<pubDate>` is when the article was
    published, not when the event happens. Almost every venue in Delft runs a
    WordPress blog whose feed advertises twenty items, and reading their pubDates
    as start times yields twenty events that all "happen" at the moment the
    webmaster hit publish — 16:52 on a Thursday, in the past, at no venue.

    That failure is worse than extracting nothing, because it is invisible: the
    source health line says twenty records and the dashboard fills with plausible
    titles at wrong times. So an event date must come from a field that actually
    means "event date". A feed whose items are dated by event can opt in with
    `date_from: published` in the registry, which is a claim the operator makes
    on the record rather than a default the parser assumes.
    """
    import feedparser

    parsed = feedparser.parse(doc)
    keys = _RSS_EVENT_DATE_KEYS
    if spec.date_from == "published":
        keys = keys + ("dc_date", "published", "updated")

    records: list[RawRecord] = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        if not title:
            continue
        summary = clean_text(strip_html(entry.get("summary")))
        start = None
        for key in keys:
            start = parse_datetime(entry.get(key), spec.timezone)
            if start is not None:
                break
        if start is None:
            if context is not None:
                context.drop(spec.id, "no parseable event date")
            continue

        url = clean_text(entry.get("link")) or spec.url
        signal = _rss_event_signal(
            url, start, f"{title} {summary or ''}",
            context.known_venues if context else set(),
        )
        if signal is None:
            # Dropped, and counted. A silent drop is indistinguishable from a
            # source that simply had nothing this week.
            if context is not None:
                context.drop(spec.id, "no event signal (article-shaped)")
            continue
        records.append(
            RawRecord(
                source_id=spec.id,
                source_url=spec.url,
                trust=spec.trust,
                title=title,
                start=start,
                description=summary,
                url=url,
                is_free=detect_free(summary, title, locale=spec.locale),
            )
        )
    return records


def extract_madrid_api(doc: str, spec: SourceSpec) -> list[RawRecord]:
    """Ayuntamiento de Madrid open-data events endpoint.

    Included as a worked example of the argument that matters commercially:
    this is a structured municipal feed, free, authoritative and updated daily.
    Any pipeline paying per-page model costs to scrape Madrid council events
    off HTML is buying something the council already gives away.
    """
    try:
        payload = json.loads(doc)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"{spec.id}: response was not json") from exc

    items = payload.get("@graph", payload if isinstance(payload, list) else [])
    records: list[RawRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        start = parse_datetime(item.get("dtstart"), spec.timezone)
        if not title or start is None:
            continue
        address = item.get("address") or {}
        area = address.get("area") or {}
        location = item.get("location") or {}
        venue = Venue(
            name=clean_text(area.get("organization-name")) or clean_text(item.get("event-location")) or "Unknown venue",
            address=clean_text(area.get("street-address")),
            city="Madrid",
            lat=location.get("latitude"),
            lon=location.get("longitude"),
        )
        description = clean_text(item.get("description"))
        free_flag = item.get("free")
        records.append(
            RawRecord(
                source_id=spec.id,
                source_url=spec.url,
                trust=spec.trust,
                title=title,
                start=start,
                end=parse_datetime(item.get("dtend"), spec.timezone),
                description=description,
                venue=venue,
                url=clean_text(item.get("link")) or spec.url,
                is_free=bool(free_flag) if free_flag is not None else detect_free(description, locale="es"),
            )
        )
    return records


def _dotted(node: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts and lists.

    `acf.prices.0.price` and `title.rendered` are both ordinary shapes in a
    WordPress payload, and both are configuration rather than code.
    """
    current = node
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def extract_wp_rest(doc: str, spec: SourceSpec) -> list[RawRecord]:
    """Read events out of a WordPress custom post type via /wp-json.

    Most Dutch venue sites are WordPress, and most of the ones with no ICS feed
    and no schema.org markup are nonetheless storing their programme in a custom
    post type that the REST API will hand over as clean JSON. The site looks
    like a tier-2 prose scrape from the outside and is tier 0 from the inside;
    that gap is the single biggest source of unnecessary model spend here.

    Field paths live in the registry because the ACF field names are the site
    author's invention -- `dag`, `datum`, `event_date` -- while the shape of the
    response never varies. Naming them is configuration; parsing them is not.
    """
    try:
        payload = json.loads(doc)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"{spec.id}: wp-json response was not json") from exc

    if isinstance(payload, dict):
        # An error body ({"code": "rest_no_route", ...}) is a drifted source,
        # not an empty programme, and must not read as a quiet week.
        if "code" in payload and "message" in payload:
            raise ExtractionError(f"{spec.id}: wp-json says {payload['code']}")
        payload = payload.get("items", [])
    if not isinstance(payload, list):
        raise ExtractionError(f"{spec.id}: wp-json response was not a list of posts")

    fields = spec.selectors or {}
    if "title" not in fields or "date" not in fields:
        raise ExtractionError(f"{spec.id}: wp_rest source needs title and date field paths")

    records: list[RawRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_title = _dotted(item, fields["title"])
        if raw_title is None:
            continue
        # title.rendered is HTML: entities and the odd <em> are routine.
        title = clean_text(lxml_html.fromstring(f"<x>{raw_title}</x>").text_content())
        day = _dotted(item, fields["date"])
        if not title or day is None:
            continue
        time_part = _dotted(item, fields["time"]) if "time" in fields else None
        start = parse_datetime(_join_day_time(day, time_part), spec.timezone)
        if start is None:
            continue

        end_time = _dotted(item, fields["end_time"]) if "end_time" in fields else None
        end = parse_datetime(_join_day_time(day, end_time), spec.timezone) if end_time else None
        # A show ending after midnight belongs to the next calendar day.
        if end is not None and end < start:
            end += timedelta(days=1)

        venue_name = _dotted(item, fields["venue"]) if "venue" in fields else None
        description = _dotted(item, fields["description"]) if "description" in fields else None
        if description:
            description = clean_text(
                lxml_html.fromstring(f"<x>{description}</x>").text_content()
            )
        # A field the registry explicitly points at as the price is a price even
        # when it arrives as a bare number. parse_price wants a currency symbol,
        # which is right for free text -- "12" in a description could be
        # anything -- and wrong here, where the source has already labelled it.
        raw_price = _dotted(item, fields["price"]) if "price" in fields else None
        if isinstance(raw_price, (int, float)):
            symbol = "€" if spec.country in {"NL", "ES", "DE", "FR", "BE"} else ""
            price_blob = f"{symbol}{raw_price:.2f}"
        else:
            price_blob = str(raw_price or "")

        records.append(
            RawRecord(
                source_id=spec.id,
                source_url=spec.url,
                trust=spec.trust,
                title=title,
                start=start,
                end=end,
                description=description,
                venue=Venue(name=clean_text(str(venue_name)) or spec.id, city=spec.city)
                if venue_name
                else None,
                url=clean_text(_dotted(item, fields.get("url", "link"))) or spec.url,
                price=parse_price(price_blob),
                is_free=detect_free(price_blob, description, locale=spec.locale),
            )
        )
    return records


_COMPACT_DAY = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _join_day_time(day: Any, time_part: Any) -> Optional[str]:
    """Combine a date and a time field into something parse_datetime accepts.

    ACF date pickers store `20260914`, which dateutil reads as a year far in the
    future rather than a date, so the compact form is expanded before parsing.
    """
    if day is None:
        return None
    day = str(day).strip()
    if not day:
        return None
    if match := _COMPACT_DAY.match(day):
        day = "-".join(match.groups())
    if time_part:
        return f"{day} {str(time_part).strip()}"
    return day


# --------------------------------------------------------------------------
# tier 1
# --------------------------------------------------------------------------

def extract_wrapper(doc: str, spec: SourceSpec) -> list[RawRecord]:
    """Apply a cached, per-domain CSS template.

    The important idea is in what this function does *not* do: it never calls a
    model. A model is used once, offline, to induce `spec.selectors` for a
    domain; that template is then stored in the registry and replayed for free
    across every page on that domain, for as long as the markup holds.

    When the markup drifts the template stops matching, `ExtractionError` fires,
    and re-induction is scheduled for that one domain. Failure is loud and
    scoped, which is the property that makes tier 1 safe to depend on.
    """
    if not spec.selectors:
        raise ExtractionError(f"{spec.id}: wrapper source has no cached selectors")

    tree = lxml_html.fromstring(doc)
    containers = tree.cssselect(spec.selectors["container"])
    if not containers:
        raise ExtractionError(
            f"{spec.id}: container selector matched nothing - markup likely drifted"
        )

    def pick(node, key: str) -> Optional[str]:
        selector = spec.selectors.get(key)
        if not selector:
            return None

        # "sel@attr" reads an attribute instead of text; a bare "@attr" reads it
        # from the container. Listing pages routinely carry the only unambiguous
        # date in a permalink -- /agenda/indorock/date/2026-08-02/ -- while the
        # rendered card shows "za 2 aug" with no year. Pulling the field out of
        # the attribute is still deterministic and still costs nothing.
        target, _, attr = selector.partition("@")
        if target.strip():
            found = node.cssselect(target.strip())
            if not found:
                return None
            el = found[0]
        else:
            el = node

        if attr:
            value = el.get(attr)
        else:
            # datetime attributes are more reliable than rendered text when present
            value = next((el.get(a) for a in ("datetime", "content") if el.get(a)), None)
            if value is None:
                value = clean_text(el.text_content())
        if value is None:
            return None

        if pattern := spec.selectors.get(f"{key}_regex"):
            match = re.search(pattern, value)
            if not match:
                return None
            groups = match.groupdict()
            value = groups.get("value") or (match.group(1) if match.groups() else match.group(0))
        return clean_text(value)

    records: list[RawRecord] = []
    for node in containers:
        title = pick(node, "title")
        # Date and clock time frequently live in different elements: the card
        # shows "zo 2 augustus 2026" (a Dutch month name dateutil will not read)
        # next to "15:00 uur", while the permalink carries a clean ISO date.
        # Taking the date from one and the time from the other keeps both
        # deterministic, and a start time that defaults to midnight is a wrong
        # answer rather than a missing one.
        start = parse_datetime(
            _join_day_time(pick(node, "start"), pick(node, "start_time")), spec.timezone
        )
        if not title or start is None:
            continue
        venue_name = pick(node, "venue")
        description = pick(node, "description")
        records.append(
            RawRecord(
                source_id=spec.id,
                source_url=spec.url,
                trust=spec.trust,
                title=title,
                start=start,
                description=description,
                venue=Venue(name=venue_name) if venue_name else None,
                url=spec.url,
                is_free=detect_free(description, pick(node, "price"), locale=spec.locale),
            )
        )
    return records


_DISPATCH = {
    "jsonld": extract_jsonld,
    # A jsonld_index payload is the detail pages' JSON-LD, stitched into one
    # document by the fetch layer. Assembling it there rather than here keeps
    # extraction a pure function of bytes, which is what makes offline replay
    # of a multi-page crawl possible at all.
    "jsonld_index": extract_jsonld,
    "ics": extract_ics,
    "rss": extract_rss,
    "api": extract_madrid_api,
    "wp_rest": extract_wp_rest,
    "wrapper": extract_wrapper,
}


def _apply_venue_default(record: RawRecord, spec: SourceSpec) -> None:
    """Fill in the venue a single-venue source never bothers to state.

    Anything the extractor did find is kept as the room/hall detail rather than
    discarded: "Zaal 3" is real information, it is just not a venue.
    """
    if not spec.venue_name:
        return
    room = record.venue.name if record.venue is not None else None
    record.venue = Venue(
        name=spec.venue_name,
        address=spec.venue_address,
        city=spec.city,
        lat=record.venue.lat if record.venue else None,
        lon=record.venue.lon if record.venue else None,
    )
    if room and room != spec.venue_name:
        record.description = f"{room}. {record.description}" if record.description else room


def extract(
    doc: str,
    spec: SourceSpec,
    fetched_at: Optional[datetime] = None,
    context: Optional[ExtractContext] = None,
) -> list[RawRecord]:
    """Route a payload to its extractor and stamp the fetch time."""
    handler = _DISPATCH.get(spec.type.value)
    if handler is None:
        raise ExtractionError(f"{spec.id}: no extractor for type {spec.type.value}")
    # Only the RSS path currently needs the context; passing it selectively
    # keeps the other extractors' signatures honest about what they use.
    if handler is extract_rss:
        records = handler(doc, spec, context)
    else:
        records = handler(doc, spec)
    for record in records:
        record.source_type = spec.type.value
        _apply_venue_default(record, spec)
        if fetched_at is not None:
            record.fetched_at = fetched_at

    if spec.collapse_repeats:
        from .occurrence import collapse_repeats as _collapse

        records = _collapse(records, spec)
    return records
