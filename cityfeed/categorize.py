"""Event categorisation.

Deliberately keyword-driven rather than model-driven. Category is the field
users filter on hardest and it is also the easiest field to get right without
spending anything: event titles and venue names are short, formulaic, and
densely signposted in every language a listing appears in.

Rules are ordered and scored rather than first-match. "Jazz in de kerk" hits
both `music` and a venue word for `community`; scoring lets the stronger signal
win instead of whichever rule happened to be written first.

Returns None on no signal. A wrong category is worse than an absent one,
because a user filtering by "music" will never see an event miscategorised as
"community", and will never know it was there.
"""

from __future__ import annotations

from typing import Optional

CATEGORIES = (
    "music",
    "nightlife",
    "theatre",
    "art",
    "film",
    "food",
    "sport",
    "market",
    "academic",
    "community",
)

# Weight 2 terms are near-decisive; weight 1 terms are supporting evidence.
_RULES: dict[str, dict[int, tuple[str, ...]]] = {
    "music": {
        2: ("concert", "concierto", "jazz", "koor", "orkest", "orquesta", "symfonie",
            "sinfon", "recital", "gig", "band", "live music", "livemuziek",
            "kamermuziek", "chamber music", "muziekfestival", "dj set", "acoustic"),
        1: ("muziek", "musica", "música", "song", "zang", "canto", "festival"),
    },
    "nightlife": {
        2: ("club night", "clubnacht", "afterparty", "discoteca", "nachtclub",
            "borrel", "pubquiz", "pub quiz", "karaoke", "fiesta"),
        1: ("bar", "cafe", "café", "party", "feest", "night", "noche"),
    },
    "theatre": {
        2: ("theater", "theatre", "teatro", "toneel", "cabaret", "opera",
            "musical", "ballet", "dansvoorstelling", "stand-up", "improv"),
        1: ("voorstelling", "performance", "espectáculo", "podium"),
    },
    "art": {
        2: ("expositie", "exposición", "exposicion", "tentoonstelling", "exhibition",
            "vernissage", "galerie", "gallery", "museum", "sculptuur", "escultura"),
        1: ("kunst", "arte", "art", "fotografie", "fotografía", "design"),
    },
    "film": {
        2: ("filmhuis", "cinema", "filmvertoning", "screening", "documentaire",
            "documental", "cortometraje", "kortfilm", "filmfestival"),
        1: ("film", "pelicula", "película", "movie"),
    },
    "food": {
        2: ("proeverij", "wijnproeverij", "cata de vinos", "food truck", "streetfood",
            "kookworkshop", "cooking class", "degustación", "brunch", "diner"),
        1: ("food", "eten", "comida", "restaurant", "wijn", "vino", "bier", "cerveza"),
    },
    "sport": {
        2: ("wedstrijd", "toernooi", "torneo", "marathon", "hardloop", "carrera",
            "roeien", "regatta", "voetbal", "fútbol", "futbol", "basketbal",
            "yoga", "bootcamp"),
        1: ("sport", "deporte", "run", "training", "match"),
    },
    "market": {
        2: ("markt", "mercado", "rommelmarkt", "vlooienmarkt", "boekenmarkt",
            "kerstmarkt", "farmers market", "mercadillo", "braderie"),
        1: ("market", "fair", "beurs", "feria"),
    },
    "academic": {
        2: ("lezing", "conferencia", "symposium", "colloquium", "seminar",
            "masterclass", "promotie", "phd defence", "open day", "opendag",
            "jornada de puertas abiertas", "hackathon", "workshop"),
        1: ("lecture", "talk", "charla", "college", "universiteit", "universidad"),
    },
    "community": {
        2: ("buurtfeest", "vrijwilligers", "voluntariado", "inloop", "meetup",
            "debat", "debate", "debatavond", "taalcafé", "language cafe",
            "repair cafe", "asamblea"),
        1: ("community", "buurt", "vecinos", "gemeente", "ayuntamiento", "social"),
    },
}


def categorize(*texts: Optional[str], venue: Optional[str] = None) -> Optional[str]:
    """Score every category against the available text and return the winner.

    Venue text is weighted at half, since a venue name describes where an event
    is, not what it is: a lecture in a theatre is still a lecture.
    """
    blob = " ".join(t.lower() for t in texts if t)
    venue_blob = (venue or "").lower()
    if not blob and not venue_blob:
        return None

    scores: dict[str, float] = {}
    for category, tiers in _RULES.items():
        score = 0.0
        for weight, terms in tiers.items():
            for term in terms:
                if term in blob:
                    score += weight
                elif term in venue_blob:
                    score += weight * 0.5
        if score:
            scores[category] = score

    if not scores:
        return None

    best = max(scores.values())
    winners = sorted(k for k, v in scores.items() if v == best)
    # A genuine tie means the signal is ambiguous, and guessing between two
    # equally-supported categories is how a filter quietly loses events.
    if len(winners) > 1 and best < 2:
        return None
    return winners[0]
