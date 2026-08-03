# Findings

What building a real event-ingestion pipeline for one city taught us. Written
after the fact, from measurements rather than intentions, and it contradicts the
assumption the project started with.

Numbers are from Delft on 2026-08-03: 7 live sources, 239 raw listings, 234
canonical events, 285 dated occurrences, 75 venues (70 geocoded), 165 tests.

---

## 1. The assumption we started with was wrong in both directions

The brief said "chaotic, unstructured organizer event data." After building it,
the picture looked like the opposite: most of what we ingested was clean
JSON-LD, ICS and municipal open data, and the expensive problems were
deduplication, measurement and per-city onboarding.

That reframe was also wrong, and the [venue census](venue-census.md) is what
showed it. Enumerating all 238 Delft venue websites from OpenStreetMap rather
than from a search engine:

**9 publish machine-readable events. About one venue in ten.**

Both statements are true at different scales, and conflating them is what
produces bad architecture:

- Among sources that publish machine-readably, tier 0 dominates and dedup is the
  hard part. **True, and it describes maybe 12% of the city.**
- The long tail is chaotic and unreachable. **Also true, and it is the other
  88%.**

The thing that made the original reframe wrong was **selection bias in our own
discovery method**. We found sources by searching. Search ranks sites with good
markup. We searched for the answer we then reported.

## 2. Recall against an independent channel was zero

Two sources — Meetup Delft and uitagenda.nl — are never ingested, structurally:
`load_registries()` skips `holdout_*.yaml` and `cityfeed run` aborts if one
leaks into the registry.

```
0 / 15 held-out events were found by the pipeline
denominator: 15 events published by 2 holdout sources within 90 days
```

Zero. And the miss list explains it precisely: seven weekly Bebop jam sessions
(free, unticketed, so absent from Bebop's own ticket shop) and four events at
Cultuurlab, a venue no ingested source lists at all.

Neither is a bug. The pipeline is good at *programmed, ticketed* events and
structurally blind to *free, recurring, café-scale* ones. That is a finding
about the source mix, not about the parser, and no amount of extraction quality
moves it.

**A recall number you cannot fail is not a measurement.** This one we failed
completely, which is why it was worth having.

## 3. Technological limitations, in order of how much they cost

### The data does not exist (56% of the 209 reachable-but-unreadable sites)

35% of unreadable venue sites publish nothing on-site and point at social
media instead; 74.6% link to social media at all, on an overlapping denominator
that includes sites which do publish something. Another 21% mention programming
— "live muziek elke donderdag" — and publish no dates at all. **No model extracts a date nobody wrote down.** This is not a tier-2
problem, an OCR problem or a prompt problem. It is not a software problem.

### JavaScript rendering (9%)

The programme exists but only after a browser runs. Solvable — find the XHR
endpoint once, register *that*, and the crawl stays pure HTTP — but it needs a
headless browser in the discovery path. We did it by hand for Jazzcafé Bebop
and never automated it. Yield is lower than it looks: most JS-rendered venue
sites in Delft are restaurants whose endpoint returns a menu.

### Bot protection

`theater.nl` returns 403 to any programmatic request, from CI and from a
residential IP alike. Eventbrite returns 405. Both are deliberate, both are
respected, and `theater.nl` is disabled with the date and reason in the
registry. Losing it halved the corroborated set — `min_sources=2` went from 8
events to 4. That is what losing a source looks like, and it is worth showing
rather than hiding.

`podiuminfo.nl` is subtler: it serves CI a 403 and a residential IP a 200. Any
scheduled crawler will see systematically less than a developer testing locally.

### TLS and infrastructure

`lijmencultuur.nl` serves an incomplete certificate chain. Disabling
verification to gain one source is a bad trade, so it stays off.

### Two-level sources are invisible to one-page probes

Theater de Veste's programme page carries only a `WebSite` block; every detail
page behind it has a complete `schema.org/Event`. A homepage probe scores it
zero. Our own census scored it zero — while it was live in the registry
producing 20 events.

## 4. Where manual effort actually pays

The strongest lever we found, and it is not more scraping.

**Tag entities, not instances.** Delft has ~75 venues and thousands of events a
year. A hand-written coordinate for a venue is correct forever and serves every
future event there; a hand-corrected event serves one event once. The ratio is
1:many and it decides where human time should go.

Concretely, ranked by return per minute of human attention:

| Manual work | Amortises over | Cost |
|---|---|---|
| **Venue coordinates** | every future event at that venue | one-off, ~1 min each |
| **Wrapper selectors** | every page on that domain, sometimes the whole platform | ~15 min per domain |
| **Venue name → canonical entity** | all merges and map pins involving it | one-off |
| **Source discovery** (local knowledge) | a whole source, indefinitely | ~40 min per city |
| **Holdout selection** | the entire recall measurement | ~1 hr per city |
| Correcting one event's time | that event | ~1 min, never amortises |

We already do three of these. `sources/venue_overrides.yaml` carries 13
hand-written coordinates for TU Delft lecture halls that no geocoder can resolve
— rooms inside buildings are not addresses. Thirteen minutes of typing lifted
geocoding from 79% to 93%, and it is honest: the file is data, reviewable, and
each entry says why it exists.

The single highest-value manual input in the whole project was one sentence of
local knowledge: *"the study associations run real calendars."* That produced
`ch.tudelft.nl/feed/ical/` — 116 events, the only ICS feed in Delft, linked from
nowhere, matching no path pattern we guessed. The same path 404s on nine other
Delft associations. It is not a pattern; it is one good source that only a human
who lives there would name.

**What this means for an ambassador model:** point ambassadors at *venues and
sources*, not at events. "Which places programme things?" and "does this venue
publish a calendar?" are questions whose answers amortise. "Is this event
correct?" is a question whose answer expires.

## 5. Issues we hit while building

The useful part, because most were silent.

### Data corruption that raises nothing

- **RSS `<pubDate>` read as event time.** Every Delft venue runs a WordPress
  blog whose feed advertises ~20 items. Reading publication timestamps as start
  times produced twenty events that all "happened" at 16:52 on a Thursday, in
  the past, at no venue. The source-health line said "20 records" either way.
  Now an event date must come from a field that *means* event date, and a feed
  genuinely dated by event opts in explicitly via `date_from: published`.
- **115 rows of raw HTML in descriptions.** ICS `DESCRIPTION` carries `<br>` and
  `<a href>`. Not a regression — descriptions had never been persisted, so the
  corruption had been there since the first crawl, invisible, because nothing
  looked. The audit found it the day descriptions were first stored.
- **Events never withdrawn.** The store only ever grew. 27 events whose sources
  had stopped listing them were still being served, indistinguishable from
  current ones. For a *live events* product that is the worst failure mode:
  confidently listing something that is not happening.

### Date parsing, four separate ways

- `dayfirst=True` is right for European free text and catastrophic for ISO-8601:
  `2026-09-12` becomes 9 December. Detect per value, never configure per source.
- The same flag read the ICS compact form `20260908T200000` as **9 August**
  rather than 8 September — on an `EXDATE`, which silently deletes a date the
  venue never cancelled and keeps the one it did.
- ACF's `20260914` parses as a *year* unless expanded first.
- `startDate` as a bare date with the real time in `doorTime`. Read literally
  that is an event at midnight, twenty hours from when it happens, which then
  fails to match the same event from any other source and survives as a
  permanent duplicate.

### Identity

- **Venue keyed on name + city** meant a source omitting the city produced a
  different key for the same building. Three rows, three map pins, three cache
  entries for Theater de Veste.
- **Venue names carrying the city** — "Theater De Veste", "Theater de Veste -
  Delft", "Theater de Veste (Delft)" — same building, three identities, until
  city tokens were stripped from the name before hashing.
- **Rooms are not venues.** A cinema's feed says "Zaal 3"; a university calendar
  says "Lecture hall Pi". Unmappable, unmatchable, and it read as a room number
  in the UI. Naming the venue once in the registry fixed geocoding, dedup *and*
  categorisation together — 96 events went from uncategorised to `film` because
  the categoriser could finally see the word *filmhuis*.

### Measurement instruments measuring the wrong thing

Three times, and the third was inside the experiment built to catch the first
two:

1. The probe originally reported markup *presence*. A page with a `WebSite`
   JSON-LD block and no events looked identical to a real source.
2. The census marked a site "publishes events" for merely having a WordPress
   event post type, without fetching it to count records.
3. The census classifier tagged pages "parseable" from event-ish words plus
   date-ish text. Verifying halved the number: most "dates" were opening hours,
   postcodes and copyright years — one was a terrace season, one a cancellation
   notice.

The lesson is narrow and repeatable: **verify yield, never presence.** Every
time we skipped it, the number came out flattering.

### One over-merge that was really a semantics bug

The pipeline's only missed merge was `Delft Jazz` at 00:00 from popdelft.nl
against the same festival at 19:00 from the theatre. The tempting fix is
widening the dedup time tolerance — but 19 hours of tolerance merges unrelated
events all day.

The real cause: popdelft publishes *no time* for that listing. So the change was
to semantics, not thresholds — exact midnight now means "time unknown" and
scores neutral against any time on the same day, the same treatment a missing
venue already gets. Re-running the audit confirmed it closed the missed merge
**and** created an over-merge warning, because that check read midnight
literally too. Both are now zero.

Changing one thing and re-measuring is the only reason that sentence can be
written.

### Deployment

Four failures, none of them in the pipeline: Vercel installs from
`pyproject.toml` not `requirements.txt`; a Vercel rewrite is not a transparent
proxy and hands the function the *destination* path; Vercel auto-detected
FastAPI as the framework and routed `/` to the API, so the homepage served
`{"detail":"Not Found"}` while the dashboard built correctly every time; and
Deployment Protection is on by default, 302ing everything to SSO.

Also: a positional `INSERT INTO events VALUES (...)` in test helpers broke 32
tests at once the moment two columns were added. Name your columns.

## 6. What we would do next

In value order, and the ordering matters — each enables the next.

1. **Temporal versioning.** We record that an event vanished but not that its
   time changed. "Doors moved 19:00 → 20:00" is exactly what a live-events user
   needs and exactly what an overwrite destroys.
2. **Field-level provenance.** `members[]` already stores every source's claim,
   but the merge returns the winning *value* and discards which source won it.
   Recording that makes a merge explainable per field, not just per event.
3. **Derived per-field confidence.** Built on (2), because provenance is what
   makes confidence derivable rather than invented. A self-reported `0.97` from
   an adapter is an opinion; a number derived from extraction tier, source
   trust, inter-source agreement and whether a fallback fired is checkable.
4. **Expected-yield regression per source.** Wrappers already fail loudly when a
   selector stops matching. A JSON-LD source silently dropping to zero does not.
   A tolerance band per source, failing CI, closes that.
5. **Freshness and breakage metrics.** The data is in `source_runs`; nothing
   aggregates it.

And one that is not engineering: **a submission path for organizers.** For the
56% of venues no crawler can reach, a form and an ICS import beat every scraper
permanently, at zero marginal cost per event. Every aggregator that won this
category won it there.

## 7. The one-paragraph version

Most event data that *is* published is free to parse, and a pipeline with zero
model calls in the crawl path handles it comfortably. But most venues publish
nothing a crawler can read — about nine in ten in Delft — and the gap is not an
extraction-quality problem, because the data was never written down anywhere
public. Coverage past roughly 12% of venues is a supply-side problem wearing an
engineering costume. The useful thing a pipeline can do is cover the readable
part cheaply, and *measure the rest honestly enough that nobody spends a year
optimising the wrong end of it.*
