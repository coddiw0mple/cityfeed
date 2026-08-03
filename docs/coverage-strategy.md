# Maximising coverage, by token cost

Written after measuring Delft rather than before. The numbers come from
[the venue census](venue-census.md); the tiering comes from what the census
said was left.

## The baseline

238 Delft venue websites. **9 publish machine-readable events.** Of the 229 that
do not:

| Reason | Count | % |
|---|---|---|
| Points at social media, no on-site programme | 74 | 35% |
| Mentions programming but publishes no dates | 44 | 21% |
| No event content at all | 36 | 17% |
| Events in prose with dates | 29 | 14% |
| JS-rendered, nothing server-side | 19 | 9% |
| Programme on a ticketing host | 7 | 3% |

**74.6% link to Instagram or Facebook.**

## First, a correction

The bottom three rows were initially reported as "55 venues technically
reachable, ~27% ceiling". That was a hypothesis dressed as a measurement — the
classifier tagged a page parseable if it contained event-ish words *and*
date-ish text, which is presence rather than yield, the exact mistake
[`probe.py`](../cityfeed/probe.py) exists to prevent.

Checking it:

- **Of the 29 "prose with dates", 15 have three or more plausible event dates.**
  Nine have one or two, five are pure boilerplate — opening hours, a postcode,
  a copyright year. Even the good bucket has false positives: `'t Boterhuis`'s
  dates are a *terrace season* ("Van 1 april tot 1 oktober"), and Falie
  Begijnhoftheater's are a cancellation notice and a closure announcement.
  Theater de Veste appears too, because the census probed its homepage while its
  machine-readable programme is at `/programma` and is already ingested.
- **Of the 7 on ticketing hosts, 3 are Eventbrite — which is a holdout.**
  Ingesting them would make the recall figure self-graded. The winnable ones are
  Stager (2 venues) and Weeztix (1).
- **Of the 19 JS-rendered, most are restaurants.** Koffie & Zo, Kokam,
  Settebello, Sparerib line. Their XHR endpoint returns a menu. Perhaps two or
  three are real venues.

**Corrected ceiling: roughly 25–32 of 238 venues, about 12%.** Not 27%. The
correction matters in the direction that hurts: less is winnable by crawling
than the first pass suggested, so the supply-side argument gets *stronger*.

---

## Tier 0 — no tokens

Deterministic parsing. Free forever, and where nearly all remaining winnable
coverage actually is.

| Technique | Measured yield | Status |
|---|---|---|
| **OSM venue enumeration** | 9 venues, zero overlap with search | Built — `scripts/venue_census.py` |
| **WordPress CPT probe** (`/wp-json/wp/v2/types`) | Found **all 9** | Built |
| **Two-level `jsonld_index`** | Theater de Veste, every CultureSuite site | Built |
| **ICS / JSON-LD / RSS / sitemap** | 5 of 7 live sources | Built |
| **Ticketing-platform APIs** (Stager, Weeztix) | ~3 venues | **Not built** |
| **XHR endpoint discovery** for JS sites | ~2–3 venues | **Not built** |
| **Municipal open data / permit lists** | Unknown, likely the largest single source | **Not built** |

Two things earn their place here beyond their yield:

**Venue enumeration is the city-onboarding answer.** Search found 8 sources
across an entire build; one automated OSM pass found 9 more with zero overlap.
Search ranks sites with good markup, which is the bias under test. This is the
difference between onboarding a city in an afternoon of human work and running a
command.

**Platform templates amortise across sites, not just pages.** popdelft.nl and
shop.jazzcafebebop.nl run the same SilverStripe ticketing template, so one
induced selector block covers two unrelated venues. Stager and Weeztix are the
same argument at national scale: one integration, many venues.

## Tier 1 — low tokens

**One model call per *domain*, cached forever.** This is where model spend
belongs, and the only place this project permits it.

- **Wrapper induction.** A model reads the markup once, emits CSS selectors,
  they are validated against the live page and stored in the registry as data.
  Covers the ~15 genuinely parseable prose venues. Currently hand-written for
  two sites — automating it is the single biggest missing piece.
- **Schema mapping for new API shapes.** The ACF dotted paths for Filmhuis Lumen
  (`acf.dag`, `acf.start_time`) were derived by hand. A model proposes them once
  per site.
- **Re-induction on drift only** — triggered when the container selector stops
  matching, never on a schedule.

Cost model: **~1 call per domain, ever.** Two hundred venues is ~200 calls in
total, not per crawl. At tier 2 pricing the same coverage would be ~200 calls
per crawl cycle, forever.

## Tier 2 — high tokens

Per page, per item, indefinitely. Justified only where the data exists nowhere
cheaper.

- **Social post → event** (caption plus image). This is where the 74.6% lives.
  Also ToS-hostile, so it is a legal question before a technical one.
- **Poster OCR.** A vision model on an organizer-uploaded flyer is a genuinely
  good use of tier 2: the data exists in no other form, and the upload already
  signals consent.
- **Free-text prose extraction** for pages with no stable structure to induce a
  wrapper from.

The hard limit: **44 venues (21%) mention programming but publish no dates at
all.** No model extracts a date that was never written down. Those are not a
tier-2 problem, they are not a software problem.

## Tier 3 — the one that wins the tail

**Supply side.** For the 118 venues (56%) unreachable by any crawler, a
submission form, an ICS import and a "claim your venue" flow beat every scraper
permanently, at zero marginal cost per event.

Every aggregator that won this category won it here. Eventbrite, Meetup and
Resident Advisor did not scrape their way to coverage; they made publishing
easier than not publishing.

---

## The strategy in one line

> Exhaust tier 0 first (~12% of venues, zero marginal cost), spend models once
> per domain at tier 1, spend per-item tokens almost nowhere, and win the
> remaining 56% with a submission form.

## What the ordering implies

1. **Ingestion quality is not the binding constraint.** Getting from 4% to 12%
   is engineering; getting past 12% is not an ingestion problem at all.
2. **Per-page model calls buy the least coverage per euro** and the bill grows
   with every city. Tier 1 buys nearly the same coverage at a fixed cost.
3. **The denominator is the deliverable.** "We read 9 of 238 venues, here are
   the other 229 and why" is checkable. "94% coverage" is not, and the
   difference shows up the first time somebody spot-checks a city.
