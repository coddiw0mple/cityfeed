# Architecture

The pipeline in depth. [The README](../README.md) has the one-screen version;
this is the reasoning behind each stage, and the failures that shaped it.

```
registry (YAML)  →  fetch  →  extract  →  geocode  →  dedup  →  store  →  API
   one row per       snapshot   by tier    venues     blocking  events    read
   source, no        store      not by     once,      scoring   venues    only
   Python                       source     cached     merging   occurrences
```

## 2. Tiered extraction

| Tier | Cost | Delft sources |
|---|---|---|
| `ics` | 0 tokens | 1 |
| `jsonld` | 0 tokens | 2 |
| `jsonld_index` | 0 tokens | 1 |
| `wp_rest` | 0 tokens | 1 |
| `wrapper` | 1 model call per *domain*, cached | 2 |
| `prose` | 1 model call per *page* | 0 |

**5 of 7 enabled Delft sources parse with zero model calls, covering 218 of 239
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

239 raw listings → **234 canonical events. 5 records absorbed, a 2.1% duplication rate.**

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

**Is the 0.72 threshold derived?** No — it arrived with the first commit under a
docstring describing an intention ("tuned to be deliberately permissive") rather
than an experiment. `scripts/threshold_sweep.py` is the experiment, replayed
over the pinned snapshot corpus:

```
 thresh  canonical  merged  multi  time!  title!
   0.50        233       6      5      0       1     over-merge appears
   0.55 … 0.72 234       5      4      0       0     identical across the band
   0.75 … 0.90 235       4      4      0       0     a real merge is lost
```

So 0.72 sits in a wide flat plateau and is defensible — but only now that it has
been measured, and only at *this* source mix. The plateau is wide precisely
because there are four multi-source events. With genuine overlap the threshold
would bind and would need re-deriving; the honest claim is "not currently the
binding constraint", not "correct".

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

**70 of 75 Delft venues geocoded (93%)**, covering 209 of 234 events. PDOK first
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

That rule then turned out to have a hole, found by a reviewer noticing the
README's figures did not reconcile. It is right about *transient* failure and
wrong about *deliberate removal*: a source disabled in the registry never
appears in the succeeded set again, so its events were permanently
un-withdrawable — four theater.nl events were stranded live forever. An operator
taking a source out of service **is** evidence, so those retire too. Two
different silences that look identical from inside a single crawl.

Occurrences had the same shape of leak: derived rows left behind withdrawn
events, growing without bound. They are now swept every run rather than only on
new withdrawals, because a fix that applies only going forward leaves the store
permanently wrong about what it holds. Human overrides survive the sweep —
someone cancelled or repriced that specific date on purpose, and that is not
derived data.

### Provenance per field, and what it caught immediately

`members[]` always stored every source's claim, but the merge returned the
winning *value* and discarded which record supplied it. That loses the question
people actually ask about a merged record — not "why did these merge" but "why
does it say 20:00 when the newspaper says 19:30".

`GET /v1/events/{id}` now answers it per field: winning source, extraction tier,
how many sources agreed, how many dissented, and a **derived** confidence.
Derived matters. A self-reported score from an extractor is an opinion; it
cannot know whether it was right. This one multiplies three checkable signals —
how directly the publisher stated the value, the source's trust tier, and
whether anyone independently agreed. Corroboration has diminishing returns and
can never certify a value the extractor had to guess at.

The first query against real data found a bug:

```
title    popdelft_agenda   tier=wrapper   conf=0.426  agree=1 dissent=2
```

`Delft Jazz` — a title recovered by regex from a permalink — had beaten
`Lindy Hop Swing — Delft Jazz - The Royal Croquettes` from a publisher-asserted
schema.org Event, while two other sources agreed on the longer form. Both are
`trust: 3`, and the tie-break was alphabetical on source id: `p` sorts before
`t`. Merges were being decided by the alphabet.

Ordering now breaks ties on evidence quality before id:

```
title    theaterdeveste_programma   tier=jsonld_index   conf=0.578  agree=1 dissent=2
start    theaterdeveste_programma   tier=jsonld_index   conf=0.829  agree=2 dissent=1
```

Title confidence stays low, and correctly so — three sources really do have
three different strings, so low confidence in the exact wording is the true
answer rather than a failure.

### Events change, and the change is the interesting part

Withdrawal records that an event vanished. It said nothing about an event that
*moved*, and "doors moved 19:00 → 20:00" is exactly what a live-events user
needs and exactly what an `UPDATE` destroys. `event_revisions` is append-only,
and re-crawling an unchanged event writes nothing — the table logs real changes,
not crawls.

## 6. API

`uvicorn cityfeed.api:app` — read-only over what the pipeline wrote. No
extraction, merging or scoring in the request path: two answers to one question
is worse than a slow answer.

| Endpoint | Notes |
|---|---|
| `GET /v1/events` | `city, category, from, to, free, min_sources, bbox, q, expand, limit, cursor` |
| `GET /v1/events/{id}` | full record, `members[]`, per-field provenance and change history |
| `GET /v1/venues` | `city, bbox, has_coords` |
| `GET /v1/venues/{id}` | venue plus upcoming occurrences with per-date pricing |
| `GET /v1/sources` | registry + per-source health, last success, record count |
| `GET /v1/categories` | category counts |
| `GET /v1/metrics` | freshness, breakage rates, yield regressions |
| `GET /v1/health` | 200 only if every enabled source succeeded within 2× its cadence |
| `POST /v1/admin/refresh` | API-key auth, always |

**`min_sources` is a corroboration filter**, and worth understanding for what it
is. `min_sources=2` returns only events that two independent sources listed —
possible only because dedup keeps provenance instead of collapsing it into a
single winning record.

At Delft's source mix it returns **4 events out of 234**, so it is a
demonstrable property rather than a useful product feature today. Calling it "the
filter that matters" would oversell it: it matters in a city whose sources
overlap, and Delft's do not. What it demonstrates is that the provenance needed
to answer "who else said this?" survives the pipeline, which is the part that
would be expensive to retrofit.

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
