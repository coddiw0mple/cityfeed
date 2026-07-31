"""Recall harness tests.

The property under test is mostly negative: holdouts must never reach the
ingested registry, and the filters that shape the denominator must not be able
to shrink it in the pipeline's favour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cityfeed.fetch import (
    HOLDOUT_PREFIX,
    assert_holdouts_are_held_out,
    load_holdouts,
    load_registries,
)
from cityfeed.models import CanonicalEvent, RawRecord, TrustTier, Venue
from cityfeed.recall import capture_recapture, in_target_city, measure

REGISTRY = Path(__file__).parent.parent / "sources"
AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _rec(title, days, source="holdout_a", venue=None, city="Delft", address=None):
    return RawRecord(
        source_id=source, source_url="https://h", trust=TrustTier.AGGREGATOR,
        title=title, start=NOW + timedelta(days=days, hours=20),
        venue=Venue(name=venue, city=city, address=address) if venue else None,
    )


def _canon(title, days, venue=None):
    return CanonicalEvent(
        id=title, title=title, start=NOW + timedelta(days=days, hours=20),
        city="Delft", venue=Venue(name=venue, city="Delft") if venue else None,
    )


# ------------------------------------------------------- holdouts stay held out

def test_load_registries_excludes_holdout_files():
    """Structural, not a convention someone has to remember."""
    ingested = {s.id for s in load_registries(REGISTRY)}
    held = {s.id for s in load_holdouts(REGISTRY)}

    assert held, "the Delft holdout registry should exist"
    assert not (ingested & held), f"holdout leaked into the crawl: {ingested & held}"
    assert all(not s.id.startswith(HOLDOUT_PREFIX) for s in load_registries(REGISTRY))


def test_real_registry_passes_the_leak_check():
    assert_holdouts_are_held_out(REGISTRY)


def test_leak_check_fails_loudly_on_a_duplicated_id(tmp_path):
    (tmp_path / "city.yaml").write_text(
        "sources:\n  - id: shared_id\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/a\n"
    )
    (tmp_path / "holdout_city.yaml").write_text(
        "sources:\n  - id: shared_id\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/b\n"
    )
    with pytest.raises(ValueError, match="self-graded"):
        assert_holdouts_are_held_out(tmp_path)


def test_leak_check_catches_the_same_url_under_a_different_id(tmp_path):
    """Renaming the row does not make it a different source."""
    (tmp_path / "city.yaml").write_text(
        "sources:\n  - id: innocent_name\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/agenda\n"
    )
    (tmp_path / "holdout_city.yaml").write_text(
        "sources:\n  - id: holdout_agenda\n    city: Delft\n    type: jsonld\n"
        "    url: https://example.test/agenda/\n"
    )
    with pytest.raises(ValueError, match="leaked"):
        assert_holdouts_are_held_out(tmp_path)


# ------------------------------------------------------------------- the metric

def test_recall_counts_matches_and_lists_the_misses():
    holdout = {"holdout_a": [_rec("Jazz Night", 3), _rec("Pubquiz", 5)]}
    corpus = [_canon("Jazz Night", 3)]

    report = measure(holdout, corpus, "Delft", now=NOW)
    assert (report.matched, report.total) == (1, 2)
    assert report.recall == 0.5
    assert [m.title for m in report.misses] == ["Pubquiz"]
    assert report.per_source["holdout_a"] == (1, 2)


def test_events_beyond_the_horizon_are_not_counted_as_misses():
    """Otherwise the metric measures the crawl horizon, not the coverage."""
    holdout = {"holdout_a": [_rec("Next year", 300), _rec("Soon", 2)]}
    report = measure(holdout, [], "Delft", window_days=90, now=NOW)
    assert report.total == 1
    assert [m.title for m in report.misses] == ["Soon"]


def test_scope_filter_only_drops_explicitly_other_cities():
    """This filter shrinks the denominator, so it must be hard to abuse."""
    assert in_target_city(_rec("x", 1, venue="Bebop", city="Delft"), "Delft") is True
    assert in_target_city(_rec("x", 1, venue="Bar", city="Rotterdam"), "Delft") is False

    # unstated city: stays in the denominator and counts against us
    assert in_target_city(_rec("x", 1, venue="Somewhere", city=None), "Delft") is True
    assert in_target_city(_rec("x", 1), "Delft") is True

    # address naming another city is out; an unrecognisable address stays in
    veste = _rec("x", 1, venue="V", city=None, address="Vesteplein 1, Delft")
    assert in_target_city(veste, "Delft") is True
    elsewhere = _rec("x", 1, venue="V", city=None, address="Coolsingel 1, Rotterdam")
    assert in_target_city(elsewhere, "Delft") is False
    unknown = _rec("x", 1, venue="V", city=None, address="(see description)")
    assert in_target_city(unknown, "Delft") is True


def test_out_of_scope_events_are_reported_not_silently_dropped():
    holdout = {"holdout_a": [
        _rec("Delft thing", 2, venue="Bebop", city="Delft"),
        _rec("Rotterdam thing", 2, venue="Bar", city="Rotterdam"),
    ]}
    report = measure(holdout, [], "Delft", now=NOW)
    assert report.total == 1
    assert report.out_of_scope["holdout_a"] == 1
    assert "different city" in report.render()


def test_recall_reuses_the_evaluate_matcher_rather_than_a_looser_one():
    """A near-miss on time must not be scored as a hit."""
    holdout = {"holdout_a": [_rec("Jazz Night", 3)]}
    far_off = CanonicalEvent(
        id="x", title="Jazz Night",
        start=NOW + timedelta(days=3, hours=20) + timedelta(hours=9),
        city="Delft",
    )
    assert measure(holdout, [far_off], "Delft", now=NOW).matched == 0


def test_render_states_its_denominator():
    """Never report an accuracy number without one."""
    report = measure({"holdout_a": [_rec("a", 1), _rec("b", 2)]}, [], "Delft", now=NOW)
    text = report.render()
    assert "denominator" in text
    assert "0/2" in text
    assert "this is the deliverable" in text


# --------------------------------------------------------- capture-recapture

def test_capture_recapture_estimates_the_population():
    a = [_rec(f"Event {i}", i, source="A") for i in range(10)]
    b = [_rec(f"Event {i}", i, source="B") for i in range(5, 13)]  # 5 shared

    est = capture_recapture({"A": a, "B": b}, corpus_size=40, now=NOW)
    assert est["a"] == 10 and est["b"] == 8
    assert est["overlap"] == 5
    assert est["estimate"] == pytest.approx(10 * 8 / 5)
    assert est["coverage"] == pytest.approx(40 / 16)


def test_capture_recapture_is_undefined_without_overlap():
    """Zero overlap means no estimate, not a divide-by-zero or a fake number."""
    a = [_rec("Only in A", 1, source="A")]
    b = [_rec("Only in B", 2, source="B")]
    est = capture_recapture({"A": a, "B": b}, corpus_size=10, now=NOW)
    assert est["overlap"] == 0
    assert est["estimate"] is None
    assert est["coverage"] is None


def test_capture_recapture_needs_two_holdouts():
    assert capture_recapture({"A": [_rec("x", 1)]}, corpus_size=10, now=NOW) is None


def test_independence_caveat_is_printed_with_the_estimate():
    """The number is meaningless without it, so it is not optional."""
    a = [_rec(f"E{i}", i, source="A") for i in range(6)]
    b = [_rec(f"E{i}", i, source="B") for i in range(3, 9)]
    report = measure({"A": a, "B": b}, [], "Delft", now=NOW)
    report.estimate = capture_recapture({"A": a, "B": b}, 20, now=NOW)

    text = report.render()
    assert "independently" in text
    assert "ceiling" in text
