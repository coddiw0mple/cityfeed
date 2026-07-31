"""Measurement.

The argument for building this first: "94% coverage" is not a number until
somebody writes down the denominator. Until a labelled set exists, every
decision about which sources to keep, where to spend model calls, and whether
a change helped is being made on vibes.

Fields are scored separately on purpose. A pipeline that finds every event but
gets a third of the start times wrong is worse than useless — it is confidently
wrong — and a single blended accuracy figure hides exactly that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .models import CanonicalEvent
from .normalize import normalize_title, parse_datetime

# A start time within this tolerance counts as correct. Listings disagree about
# doors vs showtime and that is not the pipeline's fault.
TIME_TOLERANCE = timedelta(minutes=30)
TITLE_MATCH_THRESHOLD = 0.85


@dataclass
class GoldEvent:
    """One hand-labelled real event. The unit of ground truth."""

    title: str
    start: datetime
    venue: Optional[str] = None
    is_free: Optional[bool] = None
    city: str = "Delft"
    source_hint: Optional[str] = None  # where a human found it, for recall-by-source

    @classmethod
    def from_dict(cls, raw: dict, timezone: str = "Europe/Amsterdam") -> "GoldEvent":
        start = parse_datetime(raw["start"], timezone)
        if start is None:
            raise ValueError(f"gold event {raw.get('title')!r} has an unparseable start")
        return cls(
            title=raw["title"],
            start=start,
            venue=raw.get("venue"),
            is_free=raw.get("is_free"),
            city=raw.get("city", "Delft"),
            source_hint=raw.get("source_hint"),
        )


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class Report:
    matched: int = 0
    missed: int = 0          # in gold, absent from output  -> recall failure
    spurious: int = 0        # in output, absent from gold  -> precision failure
    fields: dict[str, FieldScore] = field(default_factory=dict)
    per_source_recall: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        denom = self.matched + self.missed
        return self.matched / denom if denom else 0.0

    @property
    def precision(self) -> float:
        denom = self.matched + self.spurious
        return self.matched / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def render(self) -> str:
        lines = [
            "ingestion quality report",
            "=" * 46,
            f"  matched   {self.matched}",
            f"  missed    {self.missed}   (in gold, not produced)",
            f"  spurious  {self.spurious}   (produced, not in gold)",
            "",
            f"  precision {self.precision:.3f}",
            f"  recall    {self.recall:.3f}",
            f"  f1        {self.f1:.3f}",
        ]
        if self.fields:
            lines += ["", "field accuracy (over matched events only)", "-" * 46]
            for name, score in sorted(self.fields.items()):
                lines.append(f"  {name:<12} {score.accuracy:.3f}  ({score.correct}/{score.total})")
        if self.per_source_recall:
            lines += ["", "recall by source", "-" * 46]
            for source, (found, total) in sorted(self.per_source_recall.items()):
                rate = found / total if total else 0.0
                lines.append(f"  {source:<24} {rate:.3f}  ({found}/{total})")
        return "\n".join(lines)


def _same_event(gold: GoldEvent, produced: CanonicalEvent, locale: str = "nl") -> bool:
    """Matching rule for pairing output against ground truth.

    Intentionally stricter than the dedup threshold. Using the same similarity
    function to both merge and grade would let a sloppy merge rule mark its own
    homework.
    """
    from rapidfuzz import fuzz

    if abs(gold.start - produced.start) > timedelta(hours=6):
        return False
    a = normalize_title(gold.title, locale)
    b = normalize_title(produced.title, locale)
    if not a or not b:
        return False
    return fuzz.token_set_ratio(a, b) / 100.0 >= TITLE_MATCH_THRESHOLD


def evaluate(
    produced: list[CanonicalEvent],
    gold: list[GoldEvent],
    locale: str = "nl",
) -> Report:
    """Greedy one-to-one matching between output and ground truth."""
    report = Report()
    report.fields = {
        "start_time": FieldScore(),
        "venue": FieldScore(),
        "is_free": FieldScore(),
    }

    unmatched = list(produced)
    source_totals: dict[str, list[int]] = {}

    for gold_event in gold:
        if gold_event.source_hint:
            source_totals.setdefault(gold_event.source_hint, [0, 0])[1] += 1

        match = next((p for p in unmatched if _same_event(gold_event, p, locale)), None)
        if match is None:
            report.missed += 1
            continue

        unmatched.remove(match)
        report.matched += 1
        if gold_event.source_hint:
            source_totals[gold_event.source_hint][0] += 1

        score = report.fields["start_time"]
        score.total += 1
        if abs(match.start - gold_event.start) <= TIME_TOLERANCE:
            score.correct += 1

        if gold_event.venue is not None:
            score = report.fields["venue"]
            score.total += 1
            produced_venue = match.venue.name if match.venue else ""
            from rapidfuzz import fuzz

            if fuzz.token_set_ratio(gold_event.venue.lower(), produced_venue.lower()) >= 80:
                score.correct += 1

        if gold_event.is_free is not None:
            score = report.fields["is_free"]
            score.total += 1
            if match.is_free == gold_event.is_free:
                score.correct += 1

    report.spurious = len(unmatched)
    report.per_source_recall = {k: (v[0], v[1]) for k, v in source_totals.items()}
    return report


def load_gold(path: str | Path, timezone: str = "Europe/Amsterdam") -> list[GoldEvent]:
    raw = json.loads(Path(path).read_text())
    return [GoldEvent.from_dict(item, timezone) for item in raw]
