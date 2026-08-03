# cityfeed

Public event ingestion for one city at a time. Delft is live: **7 sources, 233
raw listings, 232 canonical events, 355 dated occurrences, 75 venues (70
geocoded), zero model calls in the pipeline.** Every number in this file came
from running the thing against the real web on 2026-08-02. Where a number is
bad, it is written down as it is.

**Write-ups:** [findings](docs/findings.md) · [venue census](docs/venue-census.md) ·
[coverage strategy](docs/coverage-strategy.md) · [adding a city](docs/adding-a-city.md)

**Live:** [cityfeed-delft.vercel.app](https://cityfeed-delft.vercel.app) ·
[/live.html](https://cityfeed-delft.vercel.app/live.html) (same page, served from the API) ·
[/docs](https://cityfeed-delft.vercel.app/docs)

```bash
cityfeed probe --file urls_delft.txt   # what tier is this URL?
cityfeed run --city Delft              # fetch, extract, geocode, dedup, store
cityfeed recall --city Delft           # what did we miss?
cityfeed audit --city Delft            # is what we kept any good?
cityfeed venues --city Delft           # where are they, and which didn't resolve
uvicorn cityfeed.api:app               # serve it

python scripts/venue_census.py --city Delft   # how much of the city is readable at all?
```

---

## 1. Measurement first

Because "94% coverage" is not a number. It is a number *and a denominator*, and
without the denominator it cannot be checked, compared, or falsified. Most
coverage claims in this space omit it, because the honest denominator is
unknowable: nobody has a list of every event in a city.

### Recall against held-out sources

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

### The denominator nobody publishes

`recall` measures against two holdout sources. That is a real denominator but a
narrow one — it says nothing about how much of the city is machine-readable in
the first place. So: enumerate every venue in Delft from OpenStreetMap (not from
a search engine, which ranks the well-marked-up sites this is trying to measure)
and probe all of them.

**361 venue-like places. 238 with a website. 9 publish machine-readable
events — 3.8%.** Validated against venues already known to work, which exposed
the probe's false negatives (it cannot see two-level listing→detail sources), so
the honest bracket is **5–15%: roughly one venue in ten.**

Why the other 229 cannot be read — measured, because "unstructured" is a shrug
rather than a diagnosis:

| Reason | Count | % |
|---|---|---|
| Points at social media, no on-site programme | 74 | 35% |
| Mentions programming, publishes no dates | 44 | 21% |
| No event content at all | 36 | 17% |
| Events in prose **with** dates | 29 | 14% |
| JS-rendered, nothing server-side | 19 | 9% |
| Programme on a ticketing host | 7 | 3% |

**74.6% link to Instagram or Facebook.** Those three groups have entirely
different fixes: 17% *should* fail, ~11% is reachable with tier-0/tier-1 work,
and 56% is unreachable by any crawler because the data was never published. So
**3.8% readable today against ~12% technically readable** — 3× headroom, none of
it requiring per-page model calls, and a hard ceiling above it.

That 12% is itself a correction. The first pass said 26%, from a classifier that
tagged a page parseable if it held event-ish words *and* date-ish text — presence
rather than yield, the exact mistake `probe.py` exists to prevent. Verifying it
halved the number: most "dates" were opening hours, three of the ticketing hosts
are Eventbrite (a holdout, so ingesting them would invalidate recall), and most
JS-rendered sites are restaurants. The correction runs in the direction that
hurts, which is the useful direction to be wrong in.
[docs/coverage-strategy.md](docs/coverage-strategy.md) has the tiering that
follows from it.

All nine venues the census found were **new** — zero overlap with the eight
sources assembled by hand via search. Enumerating venues beats searching for
sources. Full write-up: [docs/venue-census.md](docs/venue-census.md).

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

### Events are withdrawn, not accumulated

A store that only ever grows will confidently list a cancelled show forever.
The first crawl after theater.nl started 403ing exposed 27 events whose sources
had stopped listing them, still queryable, indistinguishable from current ones.

So a crawl now retires what its sources no longer mention — with one rule that
makes it safe: an event is withdrawn only if **every** source that listed it
fetched *successfully* and still did not mention it. A source that 403s or times
out withdraws nothing, because absence of evidence is not evidence. It is the
same principle as venue similarity returning a neutral score rather than zero
when coordinates are missing.

Soft delete: the row survives for audit, and reappears the moment a source lists
it again. Without that rule the first aggregator rate-limit would have emptied
the database.

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

## 2. Tiered extraction

| Tier | Cost | Delft sources |
|---|---|---|
| `ics` | 0 tokens | 1 |
| `jsonld` | 0 tokens | 2 |
| `jsonld_index` | 0 tokens | 1 |
| `wp_rest` | 0 tokens | 1 |
| `wrapper` | 1 model call per *domain*, cached | 2 |
| `prose` | 1 model call per *page* | 0 |

**6 of 8 enabled Delft sources parse with zero model calls, covering 215 of 237
listings (91%).** The other two are cached CSS templates induced once and
replayed for free. Nothing in the crawl path calls a model, ever.

The commercially interesting part is how many sources *look* like tier 2 from
outside and are tier 0 inside:

- **Filmhuis Lumen** publishes an RSS feed of blog posts and no visible
  programme. Its screenings sit in a WordPress custom post type readable at
  `/wp-json/wp/v2/shows` — 49 dated, priced series, free (100 individual
  screenings, collapsed by title).
- **Theater de Veste**'s programme page carries only a `WebSite` block. Every
  detail page behind it has a complete `schema.org/Event` with `doorTime` and
  coordinates. Two levels, still zero tokens (`jsonld_index`).
- **indelft.nl**, the official city agenda, publishes its whole programme as a
  JSON-LD `ItemList`. The extractor reported *zero events* until it learned to
  descend into `itemListElement` — the most expensive kind of miss, because a
  page carrying fifty events looks exactly like a page carrying none.

Two Delft venues (popdelft.nl, shop.jazzcafebebop.nl) run the same SilverStripe
ticketing template, so one induced selector block covers both. That is the tier-1
argument in miniature: a template amortises across a *platform*, not just across
pages on one site.

### The failure that mattered most

Every Delft venue runs a WordPress blog whose RSS feed advertises ~20 items. The
extractor was reading `<pubDate>` — *when the article was published* — as the
event start. The result was twenty events that all "happened" at the moment the
webmaster hit publish: 16:52 on a Thursday, in the past, at no venue.

It is invisible from outside; the source-health line reads "20 records" either
way. It is now closed off in the parser. An event date must come from a field
that means "event date", and a feed whose items genuinely are dated by event opts
in via `date_from: published` — a claim the operator makes on the record, not a
default the parser assumes.

`sources/delft.yaml` keeps 7 disabled rows with the reason written out. A source
that cannot be parsed deterministically is a finding, not a gap to paper over
with a model call.

---

## 3. Deduplication

233 raw listings → **232 canonical events. 5 merged, a 2.1% duplication rate.**

That rate is low, and not because the matcher is weak: Delft's sources barely
overlap. 164 of 237 listings come from two sources — a student association and a
cinema — that nothing else lists. The only real overlap is Theater de Veste,
carried by its own site plus two aggregators, and all 5 merges are there. A
duplication rate is a property of the source mix as much as of the matcher, which
is why quoting one without the mix is close to meaningless.

Three stages: **blocking** (day+geo, day+title-trigram, normalised title — several
key families, because any single one has a blind spot), **scoring** (title 0.45 /
time 0.35 / venue 0.20, threshold 0.72), **clustering** (union-find, then a
per-field merge by trust precedence).

Two design points earn their keep:

- **Same-source records never merge.** One venue listing an event twice is a
  source-quality problem, and collapsing it hides the bug.
- **Merging is per-field, not per-record.** The municipal feed usually has the
  best time, the venue page the best description. Taking the best of each beats
  taking all of the least-bad one.

Geocoding runs *before* dedup, because venue similarity is scored on distance
when both sides have coordinates and on fuzzy names when they do not.

### Venues are entities

`Theater de Veste`, `Theater de Veste - Delft` and `Theater de Veste (Delft)` are
one building. Venue identity is keyed on normalised name plus city, with city
tokens stripped from the name — whether the city belongs in the venue's name is a
formatting choice each source makes independently. Before that fix, one venue was
three rows, three map pins and three cache entries.

**70 of 75 Delft venues geocoded (93%)**, covering 207 of 232 events. PDOK first
(Dutch national addresses, no key), Nominatim as fallback, three queries per venue
from most specific to least. A second run makes **zero** network calls.

Getting from 79% to 93% took three things, none of them a better geocoder.
Venue names arrive carrying site decoration ("Café X | Delft", "X — Officiële
site") which turns a resolvable query unresolvable, so it is stripped first.
Names too generic to mean anything ("de kerk") are not queried at all rather
than resolved to some church somewhere. And `sources/venue_overrides.yaml`
carries 13 hand-written coordinates for TU Delft lecture halls — each mapped to
the building it is in, with the building's position taken from PDOK rather than
from memory. Ten hand-written points is a reasonable thing to own.

The five remaining failures are rooms that cannot be attributed to a building
with confidence. They stay unresolved on purpose: assigning every event from
that feed a campus coordinate would cover the off-campus ones with an invisible
error, and a visible gap is worth more.

One earlier failure was a real Amsterdam address in a Delft feed, correctly
rejected by the bounding box. That check is not cosmetic — asked for "Bacchusstraat, Delft"
PDOK returns an address in **Almere**, and asked for "Bacchus" Nominatim returns a
hamlet in **Tennessee**. Both are saved as test fixtures. Geocoders never say "I
don't know", so the bounding box says it for them.

---

## 4. Series and occurrences

A weekly pub quiz is *one* event. Flattening it into 52 canonical rows destroys
the only fact that makes it dedupable. So the series keeps its `rrule`, and dates
are materialised separately into an `occurrences` table on a 90-day horizon.

Two traps, both silent, both tested:

- **DST.** A 20:00 weekly concert must stay at 20:00 local across the October
  change. Expanding in absolute time drifts it to 19:00, which looks like a
  data-entry error and is impossible to explain to a venue. Expansion runs on the
  local wall clock, with the offset attached afterwards.
- **Short months.** `FREQ=MONTHLY;BYMONTHDAY=31` has no February occurrence.
  dateutil is right to skip it; the bug is code that "fixes" this by clamping to
  the 28th and inventing an event nobody scheduled.

Per-occurrence overrides live in the table and survive re-crawls, so cancelling
one night of a run — or making a single screening free — is not silently reverted
by the next crawl.

One `EXDATE` bug is worth naming: `20260908T200000` parsed as **9 August** rather
than 8 September, because `dayfirst=True` — correct for European free text — reads
it as `YYYYDDMM`. An EXDATE that deletes the wrong date reports nothing at all.

---

## 5. Onboarding a new city

The honest cost, which is where a design like this should be judged. See
[docs/adding-a-city.md](docs/adding-a-city.md) for the literal checklist.

**Genuinely just config**: registry rows in `sources/<city>.yaml` — locale,
timezone, trust tiers, per-source field maps. No Python.

**Real per-city work, roughly a day:**

| Task | Cost | Why it cannot be config |
|---|---|---|
| Finding candidate URLs | 2–4 h | Local knowledge. Delft's best source — an ICS feed at a study association — is linked from nowhere and matched no path guess. Nine other associations 404 on the same path. |
| Wrapper induction | ~15 min/site | A model reads the markup once and writes selectors into the registry. Config afterwards, not before. |
| Holdout selection | 1 h | Must be independent of everything ingested *and* publish for that municipality. Meetup's radius search made it nearly useless for Delft. |
| Geocoder + bounding box | 1–2 h | PDOK is Dutch-only; a new country needs a provider. A new city needs a `CITY_BBOX` row, without which wrong-city results cannot be rejected. |
| Stopwords and free-text markers | ~30 min | `normalize.py` carries per-language stopwords and "free entry" phrasings. A new language is a dict entry; a new *script* is more. |

**What does not scale**: sites that render dates in JavaScript. Four of Delft's
14 rows are disabled for exactly this, including the municipal calendar — the
highest-value source in the city. A headless browser would fix it and would cost
the zero-token property, so it is not on the table. The gap is documented instead.

---

## 6. API

`uvicorn cityfeed.api:app` — read-only over what the pipeline wrote. No
extraction, merging or scoring in the request path: two answers to one question
is worse than a slow answer.

| Endpoint | Notes |
|---|---|
| `GET /v1/events` | `city, category, from, to, free, min_sources, bbox, q, expand, limit, cursor` |
| `GET /v1/events/{id}` | full record including `members[]` provenance |
| `GET /v1/venues` | `city, bbox, has_coords` |
| `GET /v1/venues/{id}` | venue plus upcoming occurrences with per-date pricing |
| `GET /v1/sources` | registry + per-source health, last success, record count |
| `GET /v1/categories` | category counts |
| `GET /v1/health` | 200 only if every enabled source succeeded within 2× its cadence |
| `POST /v1/admin/refresh` | API-key auth, always |

**`min_sources` is the filter that matters.** Anyone can serve events scraped
from one aggregator. `min_sources=2` returns only events that two independent
sources listed — a corroboration guarantee, and possible only because dedup kept
provenance instead of collapsing it. In Delft today it returns **4 events out of
232**, which is an honest statement about how little Delft's sources overlap.

Cursor pagination is keyed on `(start, id)` rather than an offset, so an insert
during pagination cannot make a client skip or repeat a row. Collections carry an
ETag over the result set and honour `If-None-Match` with a 304. Reads are public
unless `CITYFEED_REQUIRE_KEY` is set; `/v1/admin/*` always requires a key,
compared with `secrets.compare_digest`.

## 7. Dashboard

The dashboard works two ways. Inline, events are baked in at build time and it
runs from a `file://` URL with no server. Set `window.CITYFEED_API` and it fetches
`/v1/events` instead, mapping every filter control to a query parameter so
filtering happens server-side.

Markers cluster by `venue_id`: a cinema with 76 screenings was 76 pins stacked on
one pixel, of which exactly one was clickable. The provenance UI is preserved —
marker border weight by source count, the per-source strip on cards, per-source
titles in the detail pane.

Times render in the *city's* timezone, never the viewer's. A concert in Delft
starts at 20:15 in Delft; formatting in the browser's zone showed it as 23:45 to
a reader in India — a plausible-looking, confidently wrong answer.

---

## 8. Known limits

- **Recall against the holdout is 0/15.** Free, recurring, café-scale events are a
  structural blind spot. Named, quantified, unfixed.
- **Five WARN-severity audit findings remain**, listed and explained in the
  measurement section. None are ERROR; `cityfeed audit` exits 0.
- **2.1% duplication** reflects a source mix that barely overlaps, not a strong
  matcher. Only 4 events have two sources.
- **55 of 232 events are uncategorised.** The categoriser is keyword-based and
  returns `None` rather than guessing.
- **Four sources are disabled for client-side rendering**, including the municipal
  calendar. This is the single largest recoverable gap.
- **One source is disabled for a broken TLS chain** (lijmencultuur.nl). Disabling
  certificate verification to gain one source is a bad trade.
- **Eventbrite returns HTTP 405** and **theater.nl returns 403** to programmatic
  requests, from CI and from a residential IP alike. That is bot protection and
  is not worked around; theater.nl is disabled with the date and the reason.
  Losing it halved the corroborated set, which is why `min_sources=2` returns
  4 events — an honest consequence of losing a source, not a dedup regression.
- **podiuminfo.nl 403s from GitHub Actions but not from a residential IP.**
  It stays enabled, and `/v1/health` reports it stale when CI cannot reach it.
- **No Docker.** Out of scope for v1, deliberately. Deployment is a static build
  plus one read-only serverless function; crawling runs in CI, not on the host.
- **Delft only.** `sources/madrid.yaml` exists as the worked example for the
  onboarding doc and has never been crawled.

## Testing

**132 tests, no network.** Every real-world failure became a saved payload plus a
regression test: the ItemList descent, the news-feed pubDate, the WordPress CPT,
the compact ACF date, the permalink date, the doorTime recovery, the EXDATE
day/month swap, the bounding-box rejections, the geocode cache, cursor pagination, ETag 304s, and 34 data-quality checks each with a deliberately
corrupted record proving it fires.

```bash
uv venv && uv pip install -e ".[dev,serve]"
pytest -q
```

## Licence

MIT — see [LICENSE](LICENSE). Built as an independent open-source project; it is
nobody's deliverable and depends on nobody's availability.

## Deployment

The dashboard is a static file and the API is read-only, so the whole thing is a
static build plus one serverless function with the 316K SQLite bundled beside it.
No hosted database: at this size there is nothing to host.

Crawling cannot run there — a serverless function has a read-only filesystem and
seconds of budget, while a polite crawl takes minutes by design. So CI does the
writing and the host only ever reads: `.github/workflows/crawl.yml` runs the
crawl twice a day, commits the refreshed database, and the push triggers a
redeploy. `POST /v1/admin/refresh` answers 501 there rather than hanging, and
says why.
