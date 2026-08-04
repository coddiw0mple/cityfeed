# Measurement

How this project knows what it knows. `recall` answers "what did we miss",
`audit` answers "is what we kept any good", and `metrics` answers "did a source
break last Tuesday" — three different questions that are easy to mistake for
one.

The census that establishes the denominator for all of it lives in
[venue-census.md](venue-census.md).

### Recall against held-out sources: the independent check

The census says how much is *publishable*. This says how much we actually
*got*, measured by a channel the pipeline cannot see — and it is a much smaller
sample, 15 events against the census's 238 venues, so it corroborates rather
than carries the argument.

Two public sources — [Meetup Delft](https://www.meetup.com/find/?location=nl--Delft)
and [uitagenda.nl](https://www.uitagenda.nl/delft) — are **never ingested**.
`load_registries()` structurally skips `holdout_*.yaml`, and `cityfeed run`
aborts if a holdout id or URL appears in the ingested registry. They are an
independent observation channel, and the only reason recall is measurable at all.

```
  0 / 15 held-out events were found by the pipeline
  recall = 0.000

  denominator: 15 events published by 2 holdout sources within 90 days
  excluded from the denominator: 10 events whose listing names a different city
```

**Recall is zero.** That is the real figure, and it is worth more than a
flattering one because the miss list says exactly why:

| Missed | Count | Why |
|---|---|---|
| Bebop jam sessions | 7 | Weekly, free, unticketed. Bebop's own site publishes only its ticket shop, and the jams are never ticketed. |
| Cultuurlab events | 4 | An entire Delft venue that no ingested source lists. Found only because a holdout listed it. |
| Other | 4 | Single listings at venues outside the registry. |

Note what this can and cannot tell you on its own: 0/15 does not distinguish
"the pipeline missed events its sources listed" from "those events were never
published anywhere it ingests". Only the census above separates them, which is
why it leads. The two together say the corpus is small *and* we are not losing
much of what is in it.

Both gaps are structural rather than bugs. The pipeline is good at *programmed,
ticketed* events — theatre, cinema, a university calendar — and blind to *free,
recurring, café-scale* ones. No amount of tuning fixes that; it needs different
sources. That is the finding, and producing findings like it is the entire
reason the holdout exists.

On the denominator: 10 of the 25 held-out events were dropped because the listing
explicitly named another city (Meetup's "near Delft" is a radius search that
reaches Rotterdam and Den Haag). That filter shrinks the denominator, so it is
deliberately asymmetric — an event is excluded only when a *different* city is
stated. Anything unstated or ambiguous stays in and counts against us.

### Capture–recapture, and why it is not trustworthy here

`cityfeed recall --capture-recapture` estimates the population as
`|A| × |B| / |A ∩ B|`. With these two holdouts the overlap is zero, so the
estimate is **undefined** and the tool says so rather than printing a number.

Even with overlap it would deserve little weight. Lincoln–Petersen assumes the
two observers are independent, and aggregators are not: they copy from the same
venue pages the registry reads. That inflates the overlap, shrinks the estimated
population and makes coverage look better than it is. It is a ceiling, never a
measurement — and the tool prints that caveat beside every estimate it produces.

### What is deliberately *not* measured

`tests/fixtures/sources_synthetic.yaml` holds six hand-written sources that test
pipeline mechanics. They sit outside `sources/` on purpose, because a fixture
written to exercise the pipeline will always flatter it. The end-to-end score in
the test suite grades the matcher and the merge; it is not a coverage claim about
Delft, and no number in this README comes from it.

### Data quality: what the audit found

`cityfeed recall` answers "what did we miss?". `cityfeed audit` answers the
other half — "is what we kept any good?" — and it is the half that is easy to
skip, because bad rows do not raise. A newspaper column stored as a midnight
event with no venue looks, to every counter in the system, exactly like a real
event. 34 checks, grouped ERROR / WARN / INFO; a non-zero exit on any ERROR so
it can gate a crawl.

Running it, then fixing what it found:

| check | before | after | what changed |
|---|---|---|---|
| `text.raw_html` | **115** | **0** | ICS `DESCRIPTION` carries `<br>` and `<a href>`. Stripped at extraction. |
| `venue.ungeocoded_rate` | 15 | **5** | Name cleaning + 13 hand-written room coordinates. |
| `volume.repeated_title` | 1 | **0** | 11 screenings of one film collapsed into one series. |
| `dedup.missed_merges` | 1 | **0** | Midnight now reads as "time unknown", not "starts at 00:00". |
| `venue.bad_name` | 1 | **0** | A venue literally named "Delft". |
| `temporal.long_duration` | 35 | 34 | Mostly real: month-long exhibitions. |
| canonical events | 286 | **232** | Repeats collapsed; the dates survive as occurrences. |

The `raw_html` finding is the instructive one. It was **not** a regression —
descriptions had never been persisted before, so 115 corrupted rows had been
sitting in every crawl, invisible, since the beginning. The check did not find a
new bug; it found an old one that nothing had been looking at.

**Zero ERROR-severity findings remain.** Five WARNs do, each understood:

- `temporal.long_duration` (34) — indelft.nl lists month-long exhibitions as
  single events. Genuinely long, not a parse failure.
- `venue.near_duplicates` (7) — address-shaped venue names differing by
  punctuation (`Schieweg 15B` / `Schieweg 15-B`) and TU Delft room-name
  variants. Would need fuzzy venue resolution, which risks merging real
  neighbours.
- `text.title_shape` (4) — popdelft.nl genuinely types its titles in caps.
  Source style, not corruption.
- `volume.source_dominance` (1) — the TU Delft association is 49% of the
  corpus. Its 115 events are distinct activities, not repeats, so there is
  nothing to collapse; the honest fix is more non-university sources.
- `temporal.small_hours` (1) — one indelft.nl event at 04:03, where the feed
  published a record-creation timestamp as `startDate`.

### Sources break quietly

A wrapper raises when its selector stops matching. A JSON-LD source that returns
an empty page is indistinguishable from a venue with nothing on.

`cityfeed metrics` compares each source against the median of **its own**
successful runs. Learned, not configured: a hand-set threshold is wrong the day
a venue's programme changes size and nobody updates it, while a self-calibrating
one holds a cinema listing 50 and a chapel listing 2 to sensible standards
without either number being written down. Failed runs are excluded from the
baseline — folding a 403's zero into the median teaches the check that zero is
normal, which is backwards. A collapse exits non-zero and gates CI.

It also reports freshness from *last success* rather than last attempt (a source
failing for a week is a week stale however often it retried) and breakage rates
that separate flaky transport from a source that no longer works and has not
been switched off.

### On not tuning blindly

The single missed merge was `Delft Jazz` listed by popdelft.nl at 00:00 against
the same festival at 19:00 from the theatre. The tempting fix is widening the
dedup time tolerance — but 19 hours of tolerance would merge unrelated events
all day long. The actual cause was that popdelft publishes *no time* for that
listing: the card reads "wo 19 augustus 2026" and nothing more.

So the change was to the semantics rather than the threshold: exact midnight now
means "time unknown" and scores neutral against any time on the same day, the
same treatment a missing venue already got. Re-running the audit confirmed it
closed the missed merge **and** created one over-merge warning — the same pair,
now merged, flagged because its members' times differ. That check was reading
midnight literally too, so it now uses the same rule. Both are zero. Changing
one thing and re-measuring is the only reason it is possible to say that.

---
