"""Normalisation: the unglamorous layer where most real accuracy is won or lost.

Every extractor funnels through here so that a Dutch venue page and a Spanish
municipal API produce records that are actually comparable downstream.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

# Words that carry no discriminative signal when comparing two titles.
# Kept per-language because "de" in Dutch and Spanish are both noise but
# "live" in an English title sometimes isn't.
_STOPWORDS = {
    "nl": {"de", "het", "een", "en", "van", "in", "op", "te", "bij", "met", "voor"},
    "es": {"el", "la", "los", "las", "de", "del", "y", "en", "con", "para", "un", "una"},
    "en": {"the", "a", "an", "and", "of", "in", "on", "at", "with", "for"},
}

_FREE_MARKERS = {
    "nl": ["gratis", "vrij entree", "vrije toegang", "gratis toegang", "geen entree"],
    "es": ["gratis", "gratuito", "entrada libre", "acceso libre"],
    "en": ["free", "free entry", "no charge", "free admission"],
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def clean_text(value: Optional[str]) -> Optional[str]:
    """Collapse whitespace and strip control characters."""
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value)
    value = "".join(ch for ch in value if ch == "\n" or not unicodedata.category(ch).startswith("C"))
    value = _WS.sub(" ", value).strip()
    return value or None


_BLOCK_TAG = re.compile(r"<\s*/?\s*(br|p|div|li|tr|h[1-6])\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def strip_html(value: Optional[str]) -> Optional[str]:
    """Remove markup from a free-text field and unescape its entities.

    Needed because "free text" in a feed frequently is not: an ICS DESCRIPTION
    routinely carries `<br>` and `<a href>`, and schema.org descriptions carry
    whole paragraphs of markup. None of it crashes anything — it renders
    literally in the UI as `<br>` and reaches the user as garbage.

    Block-level tags become spaces rather than vanishing, so "line one<br>line
    two" does not become "line oneline two".
    """
    import html as html_module

    if value is None:
        return None
    if "<" not in value and "&" not in value:
        return value  # the overwhelmingly common case, untouched
    text = _BLOCK_TAG.sub(" ", value)
    text = _ANY_TAG.sub("", text)
    # After tags, entities: "&amp;" must not survive as literal text.
    text = html_module.unescape(text)
    return _WS.sub(" ", text).strip()


def normalize_title(title: str, locale: str = "nl") -> str:
    """Aggressive normalisation used only for comparison, never for display.

    Lowercases, strips punctuation and locale stopwords, sorts nothing. The
    result is a comparison key, so readability is irrelevant.
    """
    title = unicodedata.normalize("NFKD", title.lower())
    title = "".join(c for c in title if not unicodedata.combining(c))
    title = _PUNCT.sub(" ", title)
    tokens = [t for t in title.split() if t]
    stop = _STOPWORDS.get(locale, _STOPWORDS["en"])
    tokens = [t for t in tokens if t not in stop]
    return " ".join(tokens)


def title_trigrams(title: str, locale: str = "nl", n: int = 3) -> set[str]:
    """Character trigrams of the normalised title, for blocking.

    Every trigram is returned, not a sample. Truncating to an alphabetical
    slice looks harmless and quietly destroys recall: "Jazzavond Wijnhaven"
    and "Jazzavond Wijnhaven trekt volle zaal" produce different slices even
    though one contains the other, so the pair is never compared.
    """
    norm = normalize_title(title, locale).replace(" ", "")
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i : i + n] for i in range(len(norm) - n + 1)}


_ISO_LEADING = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_datetime(
    value: str | datetime | None,
    timezone: str = "Europe/Amsterdam",
    dayfirst: bool = True,
) -> Optional[datetime]:
    """Parse a date string into a timezone-aware datetime.

    Two traps, both of which corrupt data silently rather than crashing.

    `dayfirst` is right for European free text ("12-09-2026" is 12 September)
    and catastrophically wrong for ISO 8601, where it swaps month and day:
    "2026-09-12" becomes 9 December. Feeds mix both formats freely, so the
    format is detected per value rather than configured per source.

    Naive datetimes are localised to the source's timezone rather than UTC.
    A venue in Delft writing "20:00" means 20:00 in Amsterdam, and reading
    that as UTC shifts a third of the corpus by two hours.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        value = value.strip()
        if not value:
            return None
        if _ISO_LEADING.match(value):
            dayfirst = False
        try:
            dt = dateparser.parse(value, dayfirst=dayfirst)
        except (ValueError, OverflowError, TypeError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    return dt


def detect_free(*texts: Optional[str], locale: str = "nl") -> Optional[bool]:
    """Best-effort free/paid detection.

    Returns None rather than False when there's no signal. Guessing "paid"
    on silence would quietly poison a field people filter on.
    """
    markers = _FREE_MARKERS.get(locale, []) + _FREE_MARKERS["en"]
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return None
    for marker in markers:
        if marker in blob:
            return True
    # Currency can lead or trail: "EUR 12,50", "12,50 euro", "€12.50".
    if re.search(r"[€$£]\s?\d|\b(?:eur|euro|usd)\s?\d|\d+[.,]?\d*\s?(?:euro|eur)\b", blob):
        return False
    return None


def parse_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"([€$£]\s?\d+(?:[.,]\d{2})?)", text)
    return match.group(1).replace(" ", "") if match else None
