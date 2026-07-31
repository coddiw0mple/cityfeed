"""Venue geocoding.

The design point that makes this affordable: **geocode venues, not events.**
Delft has on the order of a hundred venues and thousands of events a year. A
hundred lookups, cached permanently, cover every event that will ever happen at
those venues. Geocoding per event would multiply the same hundred answers by the
number of listings and turn a one-off cost into a per-crawl one.

The cache is the `venues` table, not a second store beside it. There is exactly
one row per venue and exactly one answer to "where is this", so a coordinate
resolved here is immediately visible to the API, the dashboard and dedup.

Providers are tried in order, cheapest and most authoritative first:

    1. PDOK Locatieserver  - the Dutch national address service. No key, no
                             quota worth worrying about, and authoritative for
                             NL addresses in a way no global geocoder is.
    2. Nominatim           - global fallback. Requires a real User-Agent and is
                             capped at one request per second, which is a
                             licence condition and not a suggestion.

The bounding box is the part that earns its keep. Geocoders are built never to
say "I don't know": ask for "Bacchus, Delft" and you will get a Bacchus
somewhere, possibly in Gelderland, with no indication that it is the wrong one.
Rejecting anything outside the city's box converts a confidently wrong
coordinate into an honest null, and an honest null is visible in
`cityfeed venues` where a wrong pin is not.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol

import httpx

from .models import Venue

USER_AGENT = "cityfeed/1.0 (public event aggregation; +https://github.com/coddiw0mple/cityfeed)"

# Per-city acceptance boxes: (min_lat, max_lat, min_lon, max_lon).
# Adding a city means adding a row here — one of the small, real per-city costs
# that the "onboarding is just config" claim has to be honest about.
CITY_BBOX: dict[str, tuple[float, float, float, float]] = {
    "delft": (51.97, 52.04, 4.32, 4.40),
    "madrid": (40.31, 40.56, -3.89, -3.52),
}

NOMINATIM_MIN_INTERVAL = 1.0


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    source: str
    query: str
    confidence: float = 1.0


class Provider(Protocol):
    name: str

    async def lookup(self, client: httpx.AsyncClient, query: str) -> Optional[GeocodeResult]:
        ...


class PDOKProvider:
    """Dutch national address service. Open, no registration, no key."""

    name = "pdok"
    URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
    _POINT = re.compile(r"POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")

    async def lookup(self, client: httpx.AsyncClient, query: str) -> Optional[GeocodeResult]:
        try:
            response = await client.get(
                self.URL,
                params={"q": query, "fq": "type:adres", "rows": 1},
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            docs = response.json().get("response", {}).get("docs", [])
        except Exception:  # noqa: BLE001 - a provider outage is not a crash
            return None
        if not docs:
            return None

        # centroide_ll is WKT in lon/lat order. Reading it as lat/lon puts every
        # Dutch venue in the Indian Ocean, which at least fails visibly; the
        # dangerous version of this bug is a box that happens to contain both.
        match = self._POINT.search(docs[0].get("centroide_ll", "") or "")
        if not match:
            return None
        lon, lat = float(match.group(1)), float(match.group(2))
        return GeocodeResult(lat, lon, self.name, query, float(docs[0].get("score", 1.0) or 1.0))


class NominatimProvider:
    """OpenStreetMap fallback. One request per second, hard."""

    name = "nominatim"
    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self) -> None:
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def lookup(self, client: httpx.AsyncClient, query: str) -> Optional[GeocodeResult]:
        async with self._lock:
            wait = NOMINATIM_MIN_INTERVAL - (asyncio.get_event_loop().time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await client.get(
                    self.URL,
                    params={"q": query, "format": "json", "limit": 1},
                    headers={"User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                results = response.json()
            except Exception:  # noqa: BLE001
                return None
            finally:
                self._last = asyncio.get_event_loop().time()

        if not results:
            return None
        try:
            return GeocodeResult(
                float(results[0]["lat"]), float(results[0]["lon"]), self.name, query,
                float(results[0].get("importance", 0.5) or 0.5),
            )
        except (KeyError, TypeError, ValueError):
            return None


def in_bbox(lat: float, lon: float, city: str) -> bool:
    """Is this coordinate plausibly in the city we asked about?

    A geocoder will always answer. "Bacchus, Delft" resolves to a Bacchus, and
    nothing in the response says whether it is the one on the Markt or one in
    another province. Without this check those answers land in the database
    indistinguishable from correct ones and put pins in the wrong city.
    """
    box = CITY_BBOX.get(city.lower())
    if box is None:
        return True  # no box configured for this city: accept rather than invent one
    min_lat, max_lat, min_lon, max_lon = box
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# Cruft that venue names carry on real sites. Stripped before querying because
# a geocoder matching "Café X | Delft — Officiële site" against its address
# index will find nothing, and the failure is indistinguishable from a venue
# that genuinely does not exist.
_NAME_NOISE = re.compile(
    r"\s*(\||—|–|-|·|:)\s*(offici[eë]le\s+site|official\s+site|home|homepage"
    r"|tickets?|agenda|programma|welkom|website)\s*$",
    re.I,
)
_TRAILING_CITY = re.compile(r"\s*[\|\-–—,(]\s*(?:in\s+)?%s\s*\)?\s*$", re.I)
# Names too generic to resolve anywhere: "the church", "the centre".
_GENERIC = re.compile(
    r"^(de|het|the)?\s*(kerk|church|centrum|centre|center|zaal|hall|hok|room|foyer"
    r"|pub|bar|caf[eé]|theater|theatre|museum|bibliotheek|library)\s*$",
    re.I,
)


def clean_venue_name(name: str, city: str) -> str:
    """Strip the decoration a site puts around its own name.

    Site titles get reused as venue names and arrive carrying separators, the
    city, and marketing ("Officiële site"). Each of those turns a resolvable
    query into an unresolvable one, and the resulting null looks exactly like a
    venue nobody can find.
    """
    cleaned = (name or "").strip()
    for _ in range(3):  # suffixes stack: "X | Delft | Officiële site"
        before = cleaned
        cleaned = _NAME_NOISE.sub("", cleaned)
        cleaned = re.sub(_TRAILING_CITY.pattern % re.escape(city), "", cleaned, flags=re.I)
        cleaned = cleaned.strip(" -|–—·:,")
        if cleaned == before:
            break
    return cleaned or (name or "").strip()


def is_generic_name(name: str) -> bool:
    """True for names no geocoder could resolve, so we do not spend a call."""
    return bool(_GENERIC.match((name or "").strip()))


def query_ladder(name: str, address: Optional[str], city: str) -> list[str]:
    """Queries from most specific to least, deduplicated.

    Name plus address first because it pins a specific building; name alone next
    because venue names are what geocoders index well; address alone last because
    it always resolves to *something* and so is the weakest evidence that we
    found the right place.
    """
    name = clean_venue_name(name, city)
    candidates = []
    if name and not is_generic_name(name):
        if address:
            candidates.append(f"{name}, {address}, {city}")
        candidates.append(f"{name}, {city}")
    if address:
        candidates.append(f"{address}, {city}")
    return list(dict.fromkeys(q for q in candidates if q.strip(" ,")))


def load_overrides(directory: str | Path = "sources") -> dict[str, tuple[float, float]]:
    """Hand-written coordinates for venues no geocoder will resolve.

    Ten hand-written points is a completely reasonable thing to own, and much
    more honest than a heuristic that guesses. Keyed on the normalised venue
    name so it survives the spelling differences between sources.
    """
    import yaml

    from .normalize import normalize_title

    path = Path(directory) / "venue_overrides.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    out: dict[str, tuple[float, float]] = {}
    for entry in raw.get("venues", []):
        try:
            out[normalize_title(entry["name"])] = (float(entry["lat"]), float(entry["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class Geocoder:
    """Cache-first venue resolution."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        providers: Optional[list[Provider]] = None,
        client: Optional[httpx.AsyncClient] = None,
        overrides_dir: Optional[str | Path] = "sources",
    ) -> None:
        self.conn = conn
        self.providers = providers if providers is not None else [PDOKProvider(), NominatimProvider()]
        self._client = client
        self.calls = 0  # network lookups this run; the second-run-is-free assertion
        self.overrides = load_overrides(overrides_dir) if overrides_dir else {}
        # Every query tried and what came back, so an unresolved venue is a
        # diagnosis rather than a shrug.
        self.attempts: dict[str, list[str]] = {}

    def cached(self, key: str) -> Optional[tuple[float, float, str]]:
        row = self.conn.execute(
            "SELECT lat, lon, geocode_source FROM venues WHERE id = ? AND lat IS NOT NULL",
            (key,),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def store(self, key: str, venue: Venue, result: Optional[GeocodeResult], city: str) -> None:
        """Write the answer back, including a failure.

        A miss is recorded (resolved_at set, lat left null) so the next run does
        not retry every unresolvable room name forever. `cityfeed venues` shows
        them as ungeocoded, which is where the next unit of work is visible.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO venues (id, name, city, address, lat, lon, resolved_at, geocode_source, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (id) DO UPDATE SET
                lat            = COALESCE(excluded.lat, venues.lat),
                lon            = COALESCE(excluded.lon, venues.lon),
                resolved_at    = excluded.resolved_at,
                geocode_source = COALESCE(excluded.geocode_source, venues.geocode_source),
                notes          = COALESCE(excluded.notes, venues.notes)
            """,
            (
                key, venue.name, venue.city or city, venue.address,
                result.lat if result else None,
                result.lon if result else None,
                now,
                result.source if result else None,
                f"resolved via: {result.query}" if result else None,
            ),
        )
        self.conn.commit()

    async def resolve(self, venue: Venue, city: str) -> Optional[GeocodeResult]:
        """Try every query against every provider, in order, until one lands."""
        from .normalize import normalize_title

        log: list[str] = []
        self.attempts[venue.name] = log

        if hit := self.overrides.get(normalize_title(clean_venue_name(venue.name, city))):
            log.append("manual override")
            return GeocodeResult(hit[0], hit[1], "override", "manual", 1.0)

        if is_generic_name(clean_venue_name(venue.name, city)):
            log.append("name too generic to query")
            return None

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        try:
            for query in query_ladder(venue.name, venue.address, city):
                for provider in self.providers:
                    self.calls += 1
                    result = await provider.lookup(client, query)
                    if result is None:
                        log.append(f"{provider.name}: no match for {query!r}")
                        continue
                    if not in_bbox(result.lat, result.lon, city):
                        # Rejected, not accepted-with-low-confidence. A pin in
                        # the wrong province is worse than no pin at all.
                        log.append(
                            f"{provider.name}: {query!r} -> "
                            f"{result.lat:.4f},{result.lon:.4f} outside {city}"
                        )
                        continue
                    log.append(f"{provider.name}: matched {query!r}")
                    return result
        finally:
            if owns_client:
                await client.aclose()
        return None

    async def resolve_all(
        self, venues: Iterable[Venue], city: str, refresh: bool = False
    ) -> dict[str, Optional[GeocodeResult]]:
        out: dict[str, Optional[GeocodeResult]] = {}
        seen: set[str] = set()
        for venue in venues:
            key = venue.key
            if key in seen:
                continue
            seen.add(key)

            if not refresh:
                if hit := self.cached(key):
                    out[key] = GeocodeResult(hit[0], hit[1], hit[2] or "cache", "cache")
                    continue
                already = self.conn.execute(
                    "SELECT resolved_at FROM venues WHERE id = ?", (key,)
                ).fetchone()
                if already and already[0]:
                    out[key] = None  # known-unresolvable, don't ask again
                    continue

            result = await self.resolve(venue, city)
            self.store(key, venue, result, city)
            out[key] = result
        return out


def apply_to_records(records: list, resolved: dict[str, Optional[GeocodeResult]]) -> int:
    """Stamp coordinates onto raw records before they reach dedup.

    Order matters: dedup scores venue similarity geographically when both sides
    have coordinates and falls back to fuzzy name matching when they do not.
    Two sources spelling one venue differently only merge reliably once both
    carry the same point, so this has to happen before deduplicate(), not after.
    """
    stamped = 0
    for record in records:
        if record.venue is None or record.venue.lat is not None:
            continue
        result = resolved.get(record.venue.key)
        if result is None:
            continue
        record.venue = record.venue.model_copy(
            update={"lat": result.lat, "lon": result.lon, "geocode_source": result.source}
        )
        stamped += 1
    return stamped


async def geocode_records(
    conn: sqlite3.Connection, records: list, city: str, refresh: bool = False
) -> tuple[int, int]:
    """Resolve every venue mentioned by these records. Returns (resolved, calls)."""
    # Stamp the run's city onto every venue first. Venue identity is keyed on
    # name *and* city, and raw records from sources that never mention the city
    # would otherwise key differently from the same venue after dedup — writing
    # two rows for one building and geocoding it twice.
    for record in records:
        if record.venue is not None and not record.venue.city:
            record.venue = record.venue.model_copy(update={"city": city})

    venues = [r.venue for r in records if r.venue is not None]
    if not venues:
        return 0, 0
    geocoder = Geocoder(conn)
    resolved = await geocoder.resolve_all(venues, city, refresh=refresh)
    apply_to_records(records, resolved)
    return sum(1 for v in resolved.values() if v is not None), geocoder.calls
