"""Every document must agree with every other, and with reality.

`test_readme_arithmetic.py` checks the README against itself and the database.
It missed two things on the very next pass, both caught by a reader:

  * the test count appeared three times with three different values, none of
    them right, because the guard never asserted it
  * the first-pass census ceiling was 26% in two documents and 27% in a third,
    because the guard only ever opened the README

So the guard was too narrow in both dimensions. This one reads *every* markdown
file in the repository and checks the figures that appear in more than one of
them, which is exactly the class of error that keeps surviving: a number
corrected where it was noticed and left alone everywhere else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = sorted([ROOT / "README.md", *(ROOT / "docs").glob("*.md")])


def _docs() -> dict[str, str]:
    # The build brief is a historical task description, not a claim about the
    # current state, so it is excluded from consistency checks on purpose.
    return {p.name: p.read_text() for p in DOCS if p.name != "build-brief.md"}


def actual_test_count() -> int:
    """How many tests pytest actually collects.

    Asked of pytest rather than derived by counting `def test_`, because those
    two numbers diverge the moment anything is parametrised -- which happened
    on the first draft of this file, four definitions collecting as seven. A
    guard against stale numbers that computes its own reference number wrongly
    is worse than no guard.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, f"could not read a collected count from pytest:\n{result.stdout[-400:]}"
    return int(match.group(1))


def test_the_stated_test_count_is_right_everywhere_it_appears():
    """It was 149, 132 and 150 in three places while the truth was 160."""
    real = actual_test_count()
    claims: dict[str, set[int]] = {}
    for name, text in _docs().items():
        found = {int(n) for n in re.findall(r"\*{0,2}(\d+) tests\b", text)}
        if found:
            claims[name] = found

    assert claims, "no document states a test count; the guard has nothing to check"
    wrong = {n: sorted(v) for n, v in claims.items() if v != {real}}
    assert not wrong, f"real count is {real}, documents claim {wrong}"


# Figures that must carry exactly one value across the whole repository.
SHARED_FIGURES = {
    "venue sites probed": r"\b(238) venue",
    "venues publishing events": r"\*\*(9) publish",
    "classified-unreadable denominator": r"\b(209)\b",
    "corrected ceiling": r"about (12)%|~(12)% technically",
}


def test_shared_figures_agree_across_documents():
    """One loop rather than parametrised cases, so this file's contribution to
    the collected count stays equal to its number of definitions."""
    problems = {}
    for label, pattern in SHARED_FIGURES.items():
        values: dict[str, set[str]] = {}
        for name, text in _docs().items():
            found = {g for m in re.findall(pattern, text)
                     for g in (m if isinstance(m, tuple) else (m,)) if g}
            if found:
                values[name] = found
        distinct = set().union(*values.values()) if values else set()
        if len(distinct) > 1:
            problems[label] = values
    assert not problems, f"figures disagree across documents: {problems}"


def test_the_first_pass_ceiling_is_quoted_consistently():
    """55 of 209 is 26.3%, so the pre-correction figure is 26 everywhere.

    It read 27% in coverage-strategy.md and 26% in two documents that link to
    it — the same number with two values, one click apart.
    """
    seen: dict[str, set[str]] = {}
    for name, text in _docs().items():
        found = set(re.findall(r"\b(2[67])%", text))
        if found:
            seen[name] = found
    distinct = set().union(*seen.values()) if seen else set()
    assert distinct <= {"26"}, f"pre-correction ceiling quoted inconsistently: {seen}"


def test_no_document_contradicts_the_census_partition():
    """238 = 9 readable + 20 unreachable + 209 classified."""
    import json

    census = json.loads((ROOT / "data" / "venue_census_delft.json").read_text())
    readable = sum(1 for v in census.values() if v["events"])
    unreachable = sum(1 for v in census.values()
                      if not v["events"] and v["how"] == "error")
    classified = len(census) - readable - unreachable
    assert (len(census), readable, unreachable, classified) == (238, 9, 20, 209)

    for name, text in _docs().items():
        if "209" not in text:
            continue
        assert "238" in text, f"{name} uses the 209 denominator without establishing 238"


def test_no_document_claims_rounded_percentages_sum_to_100():
    """The census percentages round to 99, not 100.

    35 + 21 + 17 + 14 + 9 + 3 = 99. The *counts* sum to 209 exactly, so the
    partition is complete and only the display is lossy -- but a document that
    prints those six numbers and then asserts they total 100% has made a
    checkable claim that fails against the table directly above it. Say 99
    after rounding, or quote the counts.
    """
    for name, text in _docs().items():
        for claim in ("sums to 100%", "sum to 100%"):
            if claim in text:
                assert "after rounding" in text or "counts" in text, (
                    f"{name} claims percentages {claim} without noting they round to 99"
                )
