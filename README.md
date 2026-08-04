# cityfeed

**We expected extraction and deduplication to be the hard part. After measuring
all 238 venue websites in a city, they weren't.**

Deduplication turned out to be nearly a no-op — 2.1% duplication, 4 events with
more than one source. Meanwhile **9 of 238 Delft venues publish machine-readable
event data at all**, and 56% of the rest have real programming that no crawler
can reach, because the date was never written down anywhere public.

Supply is the hard problem. Dedup was cheap because Delft's sources barely
overlap, not because the matcher is good. Everything here is the measurement
that produced that conclusion, including the three passes where the measurement
corrected me.

**Live:** [cityfeed-delft.vercel.app](https://cityfeed-delft.vercel.app) ·
[API docs](https://cityfeed-delft.vercel.app/docs) ·
[dashboard served from the API](https://cityfeed-delft.vercel.app/live.html)

---

## The system

An event-ingestion pipeline that runs unattended, costs zero model tokens per
crawl, and measures its own coverage against sources it deliberately never reads.

```
                    ┌──────────────────────────────────────────┐
   sources/*.yaml   │  one row per source. Adding a city means  │
   (the registry)   │  adding rows, never Python.               │
                    └────────────────┬─────────────────────────┘
                                     ▼
   ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐
   │  FETCH  │──▶│ EXTRACT  │──▶│ GEOCODE  │──▶│  DEDUP   │──▶│  STORE  │
   └─────────┘   └──────────┘   └──────────┘   └──────────┘   └─────────┘
    conditional    by *tier*,    venues once,   blocking →     events
    GET + ETag,    not by        cached         scoring →      venues
    content-       source        forever;       union-find →   occurrences
    addressed      ─────────     bbox rejects   per-field      provenance
    snapshots      ics jsonld    wrong-city     merge by       revisions
    (replayable)   rss api       answers        trust          source_runs
                   wp_rest                                          │
                   jsonld_index                                     ▼
                   wrapper ──── 1 model call per domain,      ┌───────────┐
                                cached & replayed free        │ READ-ONLY │
                                                              │    API    │
   ┌────────────────────────────────────────────────┐         └───────────┘
   │ MEASUREMENT — runs beside the pipeline, not in it        cursor pages
   │  recall   vs holdout sources never ingested              ETag / 304
   │  audit    34 data-quality checks, gates CI               min_sources
   │  census   probes every venue in the city                 expand=occurrences
   │  metrics  freshness, breakage, yield regression
   └────────────────────────────────────────────────┘
```

| Component | What it does | Where |
|---|---|---|
| **Source registry** | YAML rows, one per source. New city = new rows, no code. | `sources/` |
| **Fetcher** | Conditional GETs, ETag memory, content-addressed snapshot store so any crawl replays offline. | `fetch.py` |
| **Tiered extractors** | Six zero-token parsers (`ics`, `jsonld`, `jsonld_index`, `rss`, `api`, `wp_rest`) plus a cached CSS `wrapper`. Dispatch is by *tier*, so 7 sources share 6 extractors. | `extract.py` |
| **Probe** | Given a URL, reports which tier yields events — by running the real extractor, never by detecting markup. | `probe.py` |
| **Geocoder** | PDOK → Nominatim, query ladder, per-city bounding box. Venues resolved once and cached forever. | `geocode.py` |
| **Entity resolution** | Blocking → pairwise scoring → union-find → per-field merge by trust and evidence quality. | `dedup.py` |
| **Provenance** | Which source won each field, with derived confidence and append-only revision history. | `provenance.py` |
| **Recurrence** | RRULE kept on the series; dates materialised as occurrences. DST- and month-end-correct. | `occurrence.py` |
| **Recall harness** | Holdout sources structurally excluded from ingestion; recall with a stated denominator. | `recall.py` |
| **Audit** | 34 checks over stored events, ERROR/WARN/INFO, non-zero exit gates CI. | `audit.py` |
| **Metrics** | Freshness, breakage rate, and yield regression against each source's own median. | `metrics.py` |
| **API** | Read-only FastAPI: cursor pagination, ETags, `min_sources` corroboration filter. | `api.py` |
| **CLI** | `probe · run · recall · audit · metrics · venues · geocode · sources` | `cli.py` |

**Delft, 2026-08-03:** 7 sources · 239 raw listings · 234 canonical events ·
285 dated occurrences · 75 venues (70 geocoded) · **165 tests, no network** ·
zero model calls in the crawl path.

One number belongs in the headline rather than a footnote: **116 of those 239
listings (49%) come from a single student association**, another 55 from one
cinema. Small corpus, concentrated in two sources; read every rate that way.

## Three findings

**1. About one venue in ten is machine-readable.** 238 Delft venue sites: 9
publish event data, 20 were unreachable, 209 were readable but published nothing
parseable. Of those 209, 35% point at Instagram with no on-site programme and
21% mention programming but never publish a date. **No model extracts a date
nobody wrote down.**
→ [venue census](docs/venue-census.md)

**2. Recall against an independent channel is 0/15.** Two sources are never
ingested; the pipeline finds none of their Delft events. The misses are specific
— seven weekly Bebop jam sessions (free, unticketed, absent from Bebop's own
ticket shop) and four at Cultuurlab, a venue nothing in the registry lists. A
recall number you cannot fail is not a measurement.
→ [measurement](docs/measurement.md)

**3. Enumerating venues beats searching for sources.** Hand-searching found 8
sources across an entire build. One automated pass over OpenStreetMap found 9
more with **zero overlap** — a brewery, AEGEE-Delft, a microtheatre, a dance
school, all with clean WordPress event APIs. Search ranks sites with good
markup, which is exactly the bias you cannot have when measuring coverage.
→ [coverage strategy](docs/coverage-strategy.md)

## Quickstart

```bash
uv venv && uv pip install -e ".[dev,serve]"
pytest -q

cityfeed run --city Delft         # fetch → extract → geocode → dedup → store
cityfeed audit --city Delft       # is what we kept any good?
cityfeed recall --city Delft      # what did we miss?
cityfeed metrics --city Delft     # is any source quietly breaking?
uvicorn cityfeed.api:app          # serve it

python scripts/venue_census.py --city Delft   # how much of the city is readable?
python scripts/threshold_sweep.py             # does the dedup threshold matter?
```

## Deeper

| | |
|---|---|
| [**Findings**](docs/findings.md) | What building it taught us, including where manual effort pays and every silent failure we hit |
| [**Architecture**](docs/architecture.md) | The pipeline stage by stage, and the failures that shaped each one |
| [**Measurement**](docs/measurement.md) | Recall, capture–recapture, the audit, and the discipline of changing one thing at a time |
| [**Venue census**](docs/venue-census.md) | How much of a city is machine-readable, and how to find out |
| [**Coverage strategy**](docs/coverage-strategy.md) | What to build next, by token cost |
| [**Adding a city**](docs/adding-a-city.md) | The honest per-city cost, with Madrid as the worked example |

## Known limits

- **Recall is 0/15.** Free, recurring, café-scale events are a structural blind
  spot. Named, quantified, unfixed.
- **2.1% duplication** reflects a source mix that barely overlaps, not a strong
  matcher — and one source is 49% of the corpus. This is the central finding,
  not a caveat.
- **Four sources disabled for client-side rendering**, including the municipal
  calendar, the highest-value source in the city. One more for a broken TLS
  chain, one for bot protection (`theater.nl` 403s from every IP we tried).
- **54 of 234 events are uncategorised.** The categoriser is keyword-based and
  returns `None` rather than guessing.
- **Delft only.** `sources/madrid.yaml` is the worked example for the onboarding
  doc and has never been crawled.

## Deployment

Static dashboard plus one read-only serverless function with the SQLite file
bundled beside it — no hosted database at this size. Crawling cannot run there
(read-only filesystem, seconds of budget, and a polite crawl takes minutes), so
CI does the writing twice a day and commits the refreshed database. The push
triggers a redeploy, and `POST /v1/admin/refresh` answers 501 there with the
reason rather than hanging.

## Licence

MIT — see [LICENSE](LICENSE). An independent open-source project: nobody's
deliverable, dependent on nobody's availability.
