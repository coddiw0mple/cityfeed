"""Operational metrics.

The failure these exist to catch is the quiet one: a source that stops
returning events looks exactly like a quiet week, and nothing in the pipeline
raises.
"""

from __future__ import annotations

import pytest

from cityfeed.cli import connect
from cityfeed.metrics import breakage, freshness, snapshot, yield_regressions


def _history(conn, source_id, counts, ok=True):
    for i, n in enumerate(counts):
        conn.execute(
            "INSERT INTO source_run_history (source_id, ran_at, records, ok) VALUES (?,?,?,?)",
            (source_id, f"2026-08-{i + 1:02d}T00:00:00+00:00", n, int(ok)),
        )
    conn.commit()


def test_a_source_that_collapses_to_zero_is_caught(tmp_path):
    """The 'expected 20, actual 0' case. Nothing else in the pipeline sees it.

    A wrapper raises when its selector stops matching, but a JSON-LD source
    that simply returns an empty page is indistinguishable from a venue with
    nothing on -- unless you compare it against its own past.
    """
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "venue_site", [20, 19, 21, 20, 22])
    assert yield_regressions(conn) == []

    _history(conn, "venue_site", [0])
    found = yield_regressions(conn)
    assert len(found) == 1
    assert found[0].source_id == "venue_site"
    assert found[0].severity == "collapsed"


def test_a_halved_source_is_degraded_not_collapsed(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "venue_site", [20, 20, 20, 20])
    _history(conn, "venue_site", [10])
    found = yield_regressions(conn)
    assert len(found) == 1 and found[0].severity == "degraded"


def test_normal_variation_is_not_a_regression(tmp_path):
    """A quiet week must not page anyone."""
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "venue_site", [20, 24, 18, 22, 19, 21])
    _history(conn, "venue_site", [17])
    assert yield_regressions(conn) == []


def test_the_baseline_is_learned_not_configured(tmp_path):
    """A cinema listing 50 and a chapel listing 2 are both held to their own
    standard, without anyone writing either number into config."""
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "cinema", [50, 48, 52, 49])
    _history(conn, "chapel", [2, 2, 3, 2])
    _history(conn, "cinema", [49])
    _history(conn, "chapel", [2])
    assert yield_regressions(conn) == []

    _history(conn, "chapel", [0])
    assert [f.source_id for f in yield_regressions(conn)] == ["chapel"]


def test_failed_runs_do_not_become_the_baseline(tmp_path):
    """A 403 already surfaces as a failure. Folding its zero into the median
    would teach the check that zero is normal for that source."""
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "flaky", [30, 30, 30, 30])
    _history(conn, "flaky", [0, 0, 0], ok=False)   # three failed fetches
    _history(conn, "flaky", [0])                    # then a successful empty one
    found = yield_regressions(conn)
    assert len(found) == 1, "the successful zero must still be caught"
    assert found[0].median == 30


def test_a_new_source_is_not_judged_before_it_has_a_history(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "brand_new", [0])
    assert yield_regressions(conn) == []


def test_breakage_separates_flaky_transport_from_a_dead_source(tmp_path):
    conn = connect(str(tmp_path / "m.db"))
    _history(conn, "flaky", [10, 10, 10, 10, 10, 10, 10, 10, 10])
    _history(conn, "flaky", [0], ok=False)
    _history(conn, "dead", [0, 0, 0, 0, 0], ok=False)

    rates = breakage(conn)["sources"]
    assert rates["dead"]["failure_rate"] == 1.0
    assert 0 < rates["flaky"]["failure_rate"] < 0.2


def test_freshness_measures_last_success_not_last_attempt(tmp_path):
    """A source failing for a week is a week stale however often it retried."""
    conn = connect(str(tmp_path / "m.db"))
    conn.execute(
        "INSERT INTO source_runs (source_id, last_attempt, last_success, records, status) "
        "VALUES (?,?,?,?,?)",
        ("stale", "2026-08-10T00:00:00+00:00", "2026-08-01T00:00:00+00:00", 5, "ok"),
    )
    conn.execute(
        "INSERT INTO source_runs (source_id, last_attempt, last_success, records, status) "
        "VALUES (?,?,?,?,?)",
        ("never", "2026-08-10T00:00:00+00:00", None, 0, "ERROR"),
    )
    conn.commit()

    f = freshness(conn)
    assert f["never_succeeded"] == ["never"]
    assert f["sources"]["stale"]["age_hours"] > 24


def test_snapshot_survives_a_database_without_the_new_tables(tmp_path):
    """Metrics must not be the reason a fresh install crashes."""
    conn = connect(str(tmp_path / "m.db"))
    snap = snapshot(conn, "Delft")
    assert snap["events"] == 0
    assert snap["yield_regressions"] == []
    assert snap["value_changes_recorded"] == 0
