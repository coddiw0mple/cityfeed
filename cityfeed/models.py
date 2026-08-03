"""Core domain model.

One rule drives the whole design: an extracted record is *evidence from a source*,
never truth. Truth is the canonical event that dedup builds from several pieces of
evidence. Keeping those two things in separate types is what makes provenance,
precedence and evaluation tractable later on.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class SourceType(str, Enum):
    """How a source is parsed. Ordered roughly by cost per event."""

    JSONLD = "jsonld"      # schema.org/Event embedded in HTML  -> 0 tokens
    JSONLD_INDEX = "jsonld_index"  # listing page -> detail pages with JSON-LD
    ICS = "ics"            # RFC 5545 calendar feed             -> 0 tokens
    RSS = "rss"            # feed with dates in the payload     -> 0 tokens
    API = "api"            # municipal / partner JSON endpoint  -> 0 tokens
    WP_REST = "wp_rest"    # WordPress custom post type over /wp-json -> 0 tokens
    WRAPPER = "wrapper"    # cached CSS template, LLM-induced once per domain
    PROSE = "prose"        # genuine free text, per-page model call (last resort)


# Tiers that cost no model tokens at read time. `wrapper` is excluded because
# its selectors were induced by a model once per domain; `jsonld_index` and
# `wp_rest` belong here because they are pure parsing however many pages they
# touch. Getting this set wrong misstates the entire cost argument, so it is
# defined once, beside the enum it describes.
ZERO_TOKEN_TYPES = frozenset({
    SourceType.JSONLD, SourceType.JSONLD_INDEX, SourceType.ICS,
    SourceType.RSS, SourceType.API, SourceType.WP_REST,
})


def is_zero_token(source_type: "SourceType | str") -> bool:
    value = source_type.value if isinstance(source_type, SourceType) else str(source_type)
    return value in {t.value for t in ZERO_TOKEN_TYPES}


class TrustTier(int, Enum):
    """Precedence when two sources disagree about the same event.

    Lower wins. Organiser-submitted data beats a journalist's summary of it.
    """

    ORGANISER = 1
    MUNICIPAL = 2
    VENUE = 3
    AGGREGATOR = 4
    EDITORIAL = 5


class SourceSpec(BaseModel):
    """A row in the source registry.

    The entire point of this type: adding city N+1 means adding rows, not code.
    If a new city needs a new Python module, the architecture has failed.
    """

    id: str
    city: str
    country: str = "NL"
    type: SourceType
    url: str
    trust: TrustTier = TrustTier.VENUE
    cadence_minutes: int = 360
    timezone: str = "Europe/Amsterdam"
    locale: str = "nl"
    enabled: bool = True
    # How to locate each field inside this source's payload. The syntax depends
    # on the tier -- CSS selectors for WRAPPER and JSONLD_INDEX, dotted JSON
    # paths for WP_REST -- but the role is the same in all three: per-source
    # configuration that keeps a new site out of the Python.
    selectors: Optional[dict[str, str]] = None
    # JSONLD_INDEX only: cap on detail pages fetched per crawl, so a listing
    # that grows to 400 shows doesn't quietly turn one source into a crawler.
    max_detail_pages: int = 40
    # For sources that are a single venue. A venue page never repeats its own
    # name in its listings -- a cinema's programme says "Zaal 3", a pub's says
    # nothing at all -- so the one fact every record from it shares is the one
    # fact missing from all of them. Naming it here fixes geocoding (a room
    # number has no coordinates), dedup (nothing matches "Zaal 3") and
    # categorisation (a screening at a *filmhuis* is a film) in one line.
    venue_name: Optional[str] = None
    venue_address: Optional[str] = None
    # For sources that publish one row per showing rather than one per title.
    # A cinema with five screenings a day produces five "events" that are the
    # same film, and they swamp every rate computed over the corpus. Opt-in per
    # source rather than global, because for a theatre two performances of one
    # play on one day are genuinely two things you can buy a ticket to, and
    # collapsing them there would lose information.
    collapse_repeats: bool = False
    # RSS only. A feed's <pubDate> is when the article was published, not when
    # the event happens, and treating the two as one silently fills the database
    # with confidently-wrong start times. Set to "published" only for a feed
    # verified to date its items by event, never as a way to raise the count.
    date_from: str = "event"
    notes: str = ""


class Venue(BaseModel):
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    # Where the coordinates came from, once a geocoder has run. Kept because a
    # PDOK address hit and a Nominatim name guess are not equally trustworthy
    # and the difference has to survive into the store.
    geocode_source: Optional[str] = None
    default_price: Optional[str] = None
    notes: Optional[str] = None

    @property
    def key(self) -> str:
        """Stable identity for this venue: normalised name plus city.

        Reuses the same normalisation as title matching, so "Café de Wijnhaven"
        and "Cafe de Wijnhaven" are one venue rather than two. Sources disagree
        about accents, punctuation and whether the city is part of the name, and
        every one of those disagreements would otherwise become a duplicate pin
        on the map.

        City tokens are stripped from the name before hashing, because whether
        the city is part of the venue's name is a formatting choice each source
        makes independently: one aggregator says "Theater De Veste", another
        "Theater de Veste - Delft", a third "Theater de Veste (Delft)". Those are
        one building. Since the city is already part of the key, dropping it from
        the name loses nothing and collapses all three.
        """
        from .normalize import normalize_title

        city = (self.city or "").strip()
        name_tokens = normalize_title(self.name).split()
        city_tokens = set(normalize_title(city).split())
        stripped = [t for t in name_tokens if t not in city_tokens]
        # A venue genuinely called after its city ("Delft Blue") must not key on
        # an empty string, so only use the stripped form when it survives.
        name_key = " ".join(stripped or name_tokens)
        return hashlib.sha1(f"{name_key}|{city.lower()}".encode()).hexdigest()[:16]

    def geokey(self, precision: int = 3) -> str:
        """Coarse spatial key for dedup blocking.

        Deliberately coarse: this is a candidate filter, not a decision. Venues
        get rounded to a few hundred metres so that a slightly-off geocode still
        lands in the same block as its twin.
        """
        if self.lat is None or self.lon is None:
            return "nogeo"
        return f"{round(self.lat, precision)},{round(self.lon, precision)}"


class RawRecord(BaseModel):
    """One event as claimed by one source. Evidence, not truth."""

    source_id: str
    source_url: str
    trust: TrustTier
    # How this record was extracted. Part of the evidence, not bookkeeping: a
    # schema.org startDate is a claim the publisher made, while the same date
    # recovered by regex from a permalink is an inference about their URL
    # scheme. When two equally-trusted sources disagree, that difference is
    # the only thing left to decide between them.
    source_type: Optional[str] = None
    title: str
    start: datetime
    end: Optional[datetime] = None
    rrule: Optional[str] = None          # RFC 5545, for recurring events
    description: Optional[str] = None
    venue: Optional[Venue] = None
    price: Optional[str] = None
    is_free: Optional[bool] = None
    url: Optional[str] = None
    fetched_at: Optional[datetime] = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must not be blank")
        return v.strip()

    @property
    def fingerprint(self) -> str:
        """Stable id for this claim, so re-crawling doesn't create duplicates."""
        basis = f"{self.source_id}|{self.title.lower()}|{self.start.isoformat()}"
        return hashlib.sha1(basis.encode()).hexdigest()[:16]


class CanonicalEvent(BaseModel):
    """The merged view of one real-world event, assembled from RawRecords.

    `members` is kept deliberately: without it you cannot explain to a reviewer
    why two listings merged, and an unexplainable merge is one nobody will trust.
    """

    id: str
    title: str
    start: datetime
    end: Optional[datetime] = None
    venue: Optional[Venue] = None
    description: Optional[str] = None
    url: Optional[str] = None
    is_free: Optional[bool] = None
    price: Optional[str] = None
    # Carried through from the source rather than expanded. A weekly event is
    # one event; turning it into 52 canonical rows destroys that fact, and the
    # fact is what dedup and the UI both need. Dated instances are materialised
    # separately as occurrences.
    rrule: Optional[str] = None
    # Derived, not extracted: category is a property of the merged event,
    # inferred from the combined text of every source that described it.
    category: Optional[str] = None
    city: str
    members: list[RawRecord] = Field(default_factory=list)
    confidence: float = 1.0

    @property
    def source_ids(self) -> list[str]:
        return sorted({m.source_id for m in self.members})

    @property
    def venue_id(self) -> Optional[str]:
        return self.venue.key if self.venue else None


class Occurrence(BaseModel):
    """One dated instance of an event.

    The split exists because a series and its dates answer different questions.
    "Is this the same event the newspaper wrote about?" is about the series;
    "what is on next Wednesday, and what does it cost?" is about a date. Storing
    only the series makes the second question unanswerable without re-expanding
    an RRULE at query time; storing only the dates makes the first one
    unanswerable at all, because the series has been shredded.

    Overrides live here for the same reason: cancelling one night of a run, or
    making one screening free, must not restate the series.
    """

    id: str
    event_id: str
    start: datetime
    end: Optional[datetime] = None
    is_free: Optional[bool] = None
    price: Optional[str] = None
    cancelled: bool = False
    # True when a human or a source has edited this date away from what the
    # series says, which is what stops a re-crawl silently reverting it.
    is_override: bool = False

    @staticmethod
    def make_id(event_id: str, start: datetime) -> str:
        return hashlib.sha1(f"{event_id}|{start.isoformat()}".encode()).hexdigest()[:16]
