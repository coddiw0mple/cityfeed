"""The census numbers must add up, and a test must be what says so.

Three separate denominator errors reached a draft before a reader caught them:
an overlapping signal used inside an exclusive breakdown, percentages computed
against one denominator and labelled with another, and a breakdown of 209 sites
described as covering 229. Each was individually obvious and none was caught by
reading.

In a project whose entire argument is that a number without its denominator is
not a number, "be more careful next time" is not a fix. This is the fix.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CENSUS = ROOT / "data" / "venue_census_delft.json"
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


@pytest.fixture(scope="module")
def census() -> dict:
    return json.loads(CENSUS.read_text())


def _partition(census: dict) -> dict[str, dict]:
    return {
        "readable": {u: v for u, v in census.items() if v["events"]},
        "unreachable": {u: v for u, v in census.items()
                        if not v["events"] and v["how"] == "error"},
        "classified": {u: v for u, v in census.items()
                       if not v["events"] and v["how"] != "error" and v["reason"]},
        "unaccounted": {u: v for u, v in census.items()
                        if not v["events"] and v["how"] != "error" and not v["reason"]},
    }


def test_every_venue_falls_into_exactly_one_bucket(census):
    parts = _partition(census)
    assert sum(len(v) for v in parts.values()) == len(census)
    assert parts["unaccounted"] == {}, "a venue with no verdict is a hole in the denominator"


def test_the_published_partition_is_what_the_docs_claim(census):
    """238 = 9 readable + 20 unreachable + 209 classified."""
    parts = _partition(census)
    assert len(census) == 238
    assert len(parts["readable"]) == 9
    assert len(parts["unreachable"]) == 20
    assert len(parts["classified"]) == 209


def test_the_reason_breakdown_is_exclusive_and_complete(census):
    """Each classified site has exactly one reason, and they sum to the whole."""
    classified = _partition(census)["classified"]
    tally = collections.Counter(v["reason"] for v in classified.values())
    assert sum(tally.values()) == len(classified) == 209
    # Rounded to whole percents these sum to 99-100; anything outside that band
    # means a percentage in the docs is computed against the wrong denominator.
    percents = [round(n / 209 * 100) for n in tally.values()]
    assert 99 <= sum(percents) <= 101, f"percentages sum to {sum(percents)}"


def test_the_unreachable_bucket_is_not_silently_folded_in(census):
    """The 20 unreachable sites are not evidence of anything.

    They are the reason the breakdown's denominator is 209 and not 229: we
    could not fetch them, so we do not know what they publish. Counting them
    as "unreadable" would claim knowledge we do not have.
    """
    parts = _partition(census)
    assert len(parts["classified"]) + len(parts["unreachable"]) == 229
    assert all(v["reason"] is None for v in parts["unreachable"].values())


def test_no_document_describes_the_breakdown_as_covering_229(census):
    """The specific sentence a reader caught, kept from coming back."""
    bad = re.compile(r"229[^.\n]{0,80}(mutually exclusive|sums? to 100)", re.I)
    for doc in DOCS:
        assert not bad.search(doc.read_text()), f"{doc.name} attributes the breakdown to 229"


def test_docs_state_the_denominator_wherever_they_give_the_breakdown(census):
    """A percentage table without its denominator is the whole failure mode."""
    for doc in DOCS:
        text = doc.read_text()
        if "Points at social media" not in text and "points at social media" not in text:
            continue
        assert "209" in text, (
            f"{doc.name} gives the reason breakdown without naming its 209-site denominator"
        )
