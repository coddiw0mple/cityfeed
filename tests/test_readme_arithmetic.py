"""The README's own numbers must agree with each other and with the database.

Four numeric errors reached a reader before they reached a test: a raw-listing
count stale in two places, a source count that disagreed with itself between
the header and the tier table, a percentage table attributed to the wrong
denominator, and an overlapping signal used inside an exclusive breakdown.

Every one was found by someone reading carefully. None was found by me reading
carefully, including the pass where I "verified" a fix by grepping for the new
string instead of the absence of the old one.

So the README is now parsed and checked. If a crawl moves the numbers this goes
red, which is correct: the document claims to state what the pipeline actually
produced, and a stale figure is a false claim rather than an untidy one.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
DB = ROOT / "data" / "cityfeed.db"


def _one(pattern: str, text: str = README) -> tuple[str, ...]:
    found = re.findall(pattern, text)
    assert found, f"README no longer contains a figure matching {pattern!r}"
    return found[0] if isinstance(found[0], tuple) else (found[0],)


@pytest.fixture(scope="module")
def header() -> dict[str, int]:
    """The banner line: N sources, R raw, C canonical, O occurrences, V venues (G geocoded)."""
    m = _one(r"\*\*Delft, [\d-]+:\*\* (\d+) sources · (\d+) raw listings · (\d+) canonical\s+"
             r"events ·\s*(\d+) dated occurrences[^·]*· (\d+) venues \((\d+)\s*\n?geocoded\)")
    keys = ("sources", "raw", "canonical", "occurrences", "venues", "geocoded")
    return dict(zip(keys, map(int, m)))


def test_header_and_tier_section_agree_on_source_and_listing_counts(header):
    """The exact disagreement a reader caught: header said 7, tier table said 8."""
    zero_token, enabled, covered, total = map(
        int, _one(r"\*\*(\d+) of (\d+) enabled Delft sources parse with zero model calls, "
                  r"covering (\d+) of (\d+)\s*\n?listings")
    )
    assert enabled == header["sources"], (
        f"tier section says {enabled} enabled sources, header says {header['sources']}"
    )
    assert total == header["raw"], (
        f"tier section says {total} listings, header says {header['raw']}"
    )
    assert zero_token <= enabled and covered <= total


def test_dedup_section_agrees_with_the_header_and_with_itself(header):
    """raw − canonical must equal the number said to be absorbed."""
    raw, canonical, absorbed, rate = _one(
        r"(\d+) raw listings → \*\*(\d+) canonical events\. (\d+) records absorbed, "
        r"a ([\d.]+)% duplication rate"
    )
    raw, canonical, absorbed, rate = int(raw), int(canonical), int(absorbed), float(rate)

    assert raw == header["raw"], f"dedup says {raw} raw, header says {header['raw']}"
    assert canonical == header["canonical"]
    assert raw - canonical == absorbed, (
        f"{raw} - {canonical} = {raw - canonical}, but the text claims {absorbed} absorbed"
    )
    assert round(absorbed / raw * 100, 1) == rate


@pytest.mark.skipif(not DB.exists(), reason="no database to check against")
def test_every_header_number_matches_the_database(header):
    conn = sqlite3.connect(DB)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    live = "WHERE city='Delft' AND withdrawn_at IS NULL"

    from cityfeed.fetch import load_registries
    enabled = [s for s in load_registries(ROOT / "sources")
               if s.city == "Delft" and s.enabled]

    assert header["sources"] == len(enabled)
    assert header["canonical"] == q(f"SELECT count(*) FROM events {live}")
    assert header["occurrences"] == q("SELECT count(*) FROM occurrences")
    assert header["venues"] == q("SELECT count(*) FROM venues WHERE city='Delft'")
    assert header["geocoded"] == q("SELECT count(*) FROM venues WHERE lat IS NOT NULL")

    runs = dict(conn.execute("SELECT source_id, records FROM source_runs"))
    assert header["raw"] == sum(runs.get(s.id, 0) for s in enabled)


def test_no_stale_listing_counts_survive_anywhere(header):
    """A figure fixed in one place and left in another is the recurring failure."""
    for stale in ("233 raw", "237 raw", "215 of 237", "212 of 233", "6 of 8 enabled"):
        assert stale not in README, f"stale figure still present: {stale!r}"
