"""Operational metrics: is the pipeline still working, and how do we know?

`recall` answers "what did we miss", `audit` answers "is what we kept any good".
Neither answers "did a source break last Tuesday", which is the failure that
actually happens and the one that is hardest to notice, because a source that
stops returning events looks exactly like a quiet week.

Two things live here.

**Yield regression.** A source's expected output is not configured, it is
learned: the median of its own recent runs. That matters because hand-set
thresholds are wrong the day a venue's programme genuinely changes size, and
nobody updates them. Comparing a source against its own history means a cinema
that lists 50 films and a chapel that lists 2 are both held to a sensible
standard without anyone writing either number down.

**Freshness and breakage.** Both derivable from data already stored; neither
was being reported.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# A drop past this fraction of the historical median is a real regression, not
# a slow week. Set from the shape of the failure being caught: a broken
# selector or a changed API returns zero or near-zero, not 60%.
COLLAPSE_RATIO = 0.30
DEGRADE_RATIO = 0.60
MIN_HISTORY = 3   # below this there is no median worth comparing against


@dataclass
class YieldFinding:
    source_id: str
    latest: int
    median: float
    runs: int
    severity: str     # "collapsed" | "degraded"

    @property
    def ratio(self) -> float:
        return self.latest / self.median if self.median else 1.0

    def render(self) -> str:
        return (f"{self.source_id:<28} {self.latest:>4} vs median {self.median:>6.1f} "
                f"over {self.runs} runs  ({self.ratio:.0%})  {self.severity.upper()}")


def yield_regressions(conn: sqlite3.Connection, window: int = 10) -> list[YieldFinding]:
    """Sources producing far less than they historically do.

    Only successful runs count toward the median: a 403 already surfaces as a
    failure, and folding its zero into the baseline would teach the check that
    zero is normal for that source -- which is exactly backwards.
    """
    try:
        sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT source_id FROM source_run_history"
        )]
    except sqlite3.OperationalError:
        return []

    findings: list[YieldFinding] = []
    for source_id in sources:
        rows = [r[0] for r in conn.execute(
            "SELECT records FROM source_run_history WHERE source_id = ? AND ok = 1 "
            "ORDER BY id DESC LIMIT ?", (source_id, window + 1),
        )]
        if len(rows) < MIN_HISTORY + 1:
            continue
        latest, history = rows[0], rows[1:]
        median = statistics.median(history)
        if median <= 0:
            continue
        ratio = latest / median
        if ratio <= COLLAPSE_RATIO:
            findings.append(YieldFinding(source_id, latest, median, len(history), "collapsed"))
        elif ratio <= DEGRADE_RATIO:
            findings.append(YieldFinding(source_id, latest, median, len(history), "degraded"))
    return sorted(findings, key=lambda f: f.ratio)


def freshness(conn: sqlite3.Connection) -> dict:
    """How old is the data, per source and overall.

    Reported as age of the last *success*, not the last attempt: a source that
    has been failing for a week is a week stale regardless of how often the
    crawler tried.
    """
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT source_id, last_success, records FROM source_runs"
    ).fetchall()

    ages: list[float] = []
    per_source = {}
    for source_id, last_success, records in rows:
        if not last_success:
            per_source[source_id] = {"age_hours": None, "records": records}
            continue
        age = (now - datetime.fromisoformat(last_success)).total_seconds() / 3600
        ages.append(age)
        per_source[source_id] = {"age_hours": round(age, 1), "records": records}

    return {
        "sources": per_source,
        "median_age_hours": round(statistics.median(ages), 1) if ages else None,
        "worst_age_hours": round(max(ages), 1) if ages else None,
        "never_succeeded": [s for s, v in per_source.items() if v["age_hours"] is None],
    }


def breakage(conn: sqlite3.Connection, window: int = 50) -> dict:
    """How often sources fail, over their recent history.

    A source that fails one run in twenty is flaky transport; one that fails
    half its runs is a source that no longer works and has not been switched
    off. The distinction is invisible from a single run and obvious here.
    """
    try:
        rows = conn.execute(
            "SELECT source_id, ok FROM source_run_history "
            "WHERE id > (SELECT MAX(id) - ? FROM source_run_history)", (window * 20,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {"sources": {}, "overall_failure_rate": None}

    tally: dict[str, list[int]] = {}
    for source_id, ok in rows:
        tally.setdefault(source_id, []).append(ok)

    per_source = {
        s: {"runs": len(v), "failures": len(v) - sum(v),
            "failure_rate": round(1 - sum(v) / len(v), 3)}
        for s, v in tally.items()
    }
    total = sum(len(v) for v in tally.values())
    failed = sum(len(v) - sum(v) for v in tally.values())
    return {
        "sources": dict(sorted(per_source.items(), key=lambda kv: -kv[1]["failure_rate"])),
        "overall_failure_rate": round(failed / total, 3) if total else None,
    }


def snapshot(conn: sqlite3.Connection, city: Optional[str] = None) -> dict:
    """Everything measurable about pipeline health in one object."""
    where, params = ("WHERE city = ? AND withdrawn_at IS NULL", [city]) if city \
        else ("WHERE withdrawn_at IS NULL", [])
    q = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731

    events = q(f"SELECT count(*) FROM events {where}", params)
    corroborated = q(
        f"SELECT count(*) FROM events {where} AND source_ids LIKE '%,%'", params
    )
    regressions = yield_regressions(conn)

    try:
        revisions = q("SELECT count(*) FROM event_revisions WHERE old_value IS NOT NULL")
    except sqlite3.OperationalError:
        revisions = 0

    return {
        "events": events,
        "withdrawn": q("SELECT count(*) FROM events WHERE withdrawn_at IS NOT NULL"),
        "occurrences": q("SELECT count(*) FROM occurrences"),
        "corroborated": corroborated,
        # The duplication rate is a property of the source mix as much as of the
        # matcher, so it is reported next to the mix rather than alone.
        "corroboration_rate": round(corroborated / events, 3) if events else None,
        "value_changes_recorded": revisions,
        "freshness": freshness(conn),
        "breakage": breakage(conn),
        "yield_regressions": [
            {"source_id": f.source_id, "latest": f.latest, "median": f.median,
             "ratio": round(f.ratio, 3), "severity": f.severity}
            for f in regressions
        ],
    }


def render(snap: dict) -> str:
    lines = [
        "pipeline metrics",
        "=" * 58,
        f"  events           {snap['events']}  ({snap['withdrawn']} withdrawn)",
        f"  occurrences      {snap['occurrences']}",
        f"  corroborated     {snap['corroborated']}"
        + (f"  ({snap['corroboration_rate']:.1%})" if snap["corroboration_rate"] is not None else ""),
        f"  value changes    {snap['value_changes_recorded']} recorded across all crawls",
    ]
    fresh = snap["freshness"]
    lines += [
        "",
        "freshness",
        "-" * 58,
        f"  median age       {fresh['median_age_hours']}h",
        f"  worst age        {fresh['worst_age_hours']}h",
    ]
    if fresh["never_succeeded"]:
        lines.append(f"  never succeeded  {', '.join(fresh['never_succeeded'])}")

    brk = snap["breakage"]
    if brk["overall_failure_rate"] is not None:
        lines += ["", "breakage", "-" * 58,
                  f"  overall failure rate  {brk['overall_failure_rate']:.1%}"]
        for source_id, v in list(brk["sources"].items())[:6]:
            if v["failures"]:
                lines.append(
                    f"  {source_id:<30} {v['failures']}/{v['runs']} runs failed"
                )

    lines += ["", "yield regressions", "-" * 58]
    if not snap["yield_regressions"]:
        lines.append("  none — every source is producing what it usually does")
    for r in snap["yield_regressions"]:
        lines.append(
            f"  {r['source_id']:<28} {r['latest']:>4} vs median {r['median']:>6.1f}"
            f"  ({r['ratio']:.0%})  {r['severity'].upper()}"
        )
    return "\n".join(lines)
