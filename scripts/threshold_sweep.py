"""Is MERGE_THRESHOLD = 0.72 derived, or did someone type it?

It was typed. It arrived with the first commit and no measurement was ever
attached to it, and the docstring saying "tuned to be deliberately permissive"
describes an intention rather than an experiment.

This is the experiment. Replay the pinned snapshot corpus at a range of
thresholds and report, for each: how many records merge, and how many of those
merges look wrong. "Wrong" is approximated two ways, because there is no
labelled set:

  * **time conflict** -- members whose start times differ by more than three
    hours, excluding the midnight-means-unknown case
  * **title conflict** -- members whose pairwise normalised title similarity
    falls below 0.5

Both are the same checks `cityfeed audit` uses for over-merges, reused rather
than reinvented so the sweep is graded by the same rule the pipeline is.

    python scripts/threshold_sweep.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rapidfuzz import fuzz  # noqa: E402

from cityfeed.dedup import deduplicate  # noqa: E402
from cityfeed.extract import extract  # noqa: E402
from cityfeed.fetch import SnapshotStore, load_registries  # noqa: E402
from cityfeed.normalize import normalize_title  # noqa: E402


def load_records(city: str = "Delft"):
    """Replay the newest snapshot for every enabled source. No network."""
    store = SnapshotStore()
    records = []
    for spec in load_registries(ROOT / "sources"):
        if spec.city.lower() != city.lower() or not spec.enabled:
            continue
        digest = store.latest_for(spec.id)
        if not digest:
            continue
        try:
            records.extend(extract(store.get(digest), spec))
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipped {spec.id}: {type(exc).__name__})", file=sys.stderr)
    return records


def suspicious(event) -> tuple[bool, bool]:
    """(time_conflict, title_conflict) for a merged cluster."""
    members = event.members
    if len(members) < 2:
        return False, False

    time_conflict = False
    for a in members:
        for b in members:
            if a is b:
                continue
            # Midnight means "time unknown", not "starts at 00:00" -- the same
            # rule dedup scores on. Reading it literally here would flag every
            # date-only listing as an over-merge.
            if (a.start.hour, a.start.minute) == (0, 0) or (b.start.hour, b.start.minute) == (0, 0):
                continue
            if abs(a.start - b.start) > timedelta(hours=3):
                time_conflict = True

    title_conflict = False
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            na, nb = normalize_title(a.title), normalize_title(b.title)
            if na and nb and fuzz.token_set_ratio(na, nb) / 100.0 < 0.5:
                title_conflict = True
    return time_conflict, title_conflict


def main() -> int:
    records = load_records()
    if not records:
        print("no snapshots to replay - run `cityfeed run --city Delft` first")
        return 1

    print(f"replaying {len(records)} raw records from the snapshot store\n")
    print(f"{'thresh':>7} {'canonical':>10} {'merged':>7} {'multi':>6} "
          f"{'time!':>6} {'title!':>7}   note")
    print("-" * 72)

    baseline_merged = None
    for threshold in [0.50, 0.55, 0.60, 0.65, 0.70, 0.72, 0.75, 0.80, 0.85, 0.90]:
        events = deduplicate(records, city="Delft", locale="nl", threshold=threshold)
        merged = len(records) - len(events)
        multi = [e for e in events if len({m.source_id for m in e.members}) > 1]
        flags = [suspicious(e) for e in multi]
        time_bad = sum(1 for t, _ in flags if t)
        title_bad = sum(1 for _, t in flags if t)

        note = ""
        if threshold == 0.72:
            note = "<- current"
            baseline_merged = merged
        elif baseline_merged is not None and merged > baseline_merged:
            note = f"+{merged - baseline_merged} merges"

        print(f"{threshold:>7.2f} {len(events):>10} {merged:>7} {len(multi):>6} "
              f"{time_bad:>6} {title_bad:>7}   {note}")

    print()
    print("time!  = merged clusters whose members disagree by >3h (midnight excluded)")
    print("title! = merged clusters with pairwise title similarity <0.5")
    print()
    print("Read it as a stability check, not an optimisation: if the numbers do not")
    print("move across a wide band, the threshold is not what is limiting the result,")
    print("and tuning it would be theatre.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
