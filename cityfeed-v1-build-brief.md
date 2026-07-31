# cityfeed v1 — Delft, live

Scope for this build: **one city, real sources, measured recall, queryable API.**

Explicitly out of scope: Docker, scheduler, deployment, monitoring, multi-city
onboarding, tier-2 prose extraction.

The repo already exists. Nothing below rebuilds it — every task extends what's
there. Existing layout:

```
cityfeed/{models,normalize,extract,dedup,evaluate,categorize,fetch,cli}.py
sources/delft.yaml, sources/madrid.yaml
tests/test_pipeline.py + tests/fixtures/
dashboard.template.html, dashboard.html
scripts/make_demo_data.py
```

---

# Part A — the human steps

These are the parts Claude Code can't do for you. Roughly 2 hours total,
spread across the build.

### A1. Compile candidate Delft sources (~40 min)

You need a list of URLs to probe. Start here — these are the obvious ones, add
whatever else you find:

**Municipal / tourism**
- `delft.nl/agenda`
- `indelft.nl`
- `delft.com`
- VVV Delft

**University**
- `tudelft.nl/en/events`
- `x.tudelft.nl` (sports/culture centre)
- Faculty event pages (EEMCS, 3mE) — often separate calendars
- Study association sites (`ch.tudelft.nl`, `wisv.ch`, etc.)

**Venues**
- Theater de Veste
- Filmhuis Lumen
- Museum Prinsenhof
- Nieuwe Kerk / Oude Kerk
- Bebop Jazzcafé, Doerak, Bacchus, Kobus Kuch, Speakers
- Lijm & Cultuur

**Editorial**
- `delftopzondag.nl`
- Delta (TU Delft's paper)

**Aggregators**
- Eventbrite Delft
- Meetup Delft

Just collect URLs into a text file. Don't check anything by hand — that's what
task B1 builds.

### A2. Pick holdouts and keep them out (~10 min)

Choose **two or three** sources you deliberately never ingest. Good candidates:

- ESN Delft events page
- Eventbrite Delft
- Instagram pages of ~10 venues

These go in `sources/holdout_delft.yaml`, never in `delft.yaml`. They are your
independent observation channel — the only way to find out what the pipeline
missed. If you ever fold a holdout into the main registry, promote a new one to
replace it.

### A3. Get a PDOK key (0 min)

You don't need one. PDOK Locatieserver is open, no registration, no key.
Nominatim also needs no key but requires a real User-Agent and max 1 req/sec.

### A4. Sanity-read the output (~20 min)

After the first live run, open the dashboard and look at 15–20 events. You're
not grading, you're smell-testing: are titles mangled, are times plausible, did
anything obviously non-event get through. Note what's wrong and feed it back as
a bug list.

### A5. Optional: the poster walk (later, once you're in Delft)

One afternoon in the centre, note what's on café windows and notice boards that
never appeared online. Tells you the size of the gap no pipeline can close.
Do it once. Not per city, not repeatedly.

---

# Part B — the Claude Code brief

Paste these as separate tasks. Each has acceptance criteria; don't move on
until they pass.

---

## B1. Source probe tool

> Add `cityfeed/probe.py` and a `cityfeed probe` CLI subcommand.
>
> Given a URL, fetch it and report which extraction tiers are available:
> - `schema.org/Event` in JSON-LD (count events found)
> - `schema.org/Event` in microdata or RDFa
> - `<link rel="alternate" type="text/calendar">` or any `.ics` href
> - `<link rel="alternate" type="application/rss+xml">` or Atom
> - a sitemap with event-looking URLs
> - none of the above → candidate for a wrapper template
>
> Accept a file of URLs (`--file urls.txt`) and probe all of them concurrently,
> max 4 at a time, with a 1s delay per host. Output a table: URL, tier found,
> event count, suggested `SourceSpec` YAML block ready to paste.
>
> Reuse the existing extractors — if JSON-LD is detected, actually run
> `extract_jsonld` and report how many valid records came out, not just whether
> the script tag exists. A page with a `WebSite` JSON-LD block and no events
> should report "jsonld present, 0 events".
>
> **Acceptance:** `cityfeed probe --file urls.txt` on 20 mixed URLs produces a
> table plus paste-ready YAML, and doesn't crash on 404s, redirects, non-HTML
> content types, or malformed markup.

## B2. Wire live Delft sources

> Replace the placeholder entries in `sources/delft.yaml` with real sources
> from the probe output. Keep the existing schema. Set `trust` by type:
> municipal 2, venue 3, aggregator 4, editorial 5.
>
> Then run `cityfeed run --city Delft` live and fix every failure. Expect:
> encodings, redirect chains, `Content-Type` lies, dates rendered by JS,
> JSON-LD with `@type: "event"` lowercase, `startDate` as a date with no time,
> multiple events sharing one `@id`, and venues whose `address` is a bare string.
>
> For each real-world failure you fix, save the offending payload to
> `tests/fixtures/` and add a regression test. This is the point of the
> snapshot store — use it.
>
> Do not add model calls. Anything that can't be parsed deterministically gets
> `enabled: false` and a note explaining why.
>
> **Acceptance:** `cityfeed run --city Delft` completes with ≥8 enabled sources,
> zero unhandled exceptions, and `cityfeed sources` shows the health line for
> each. All existing tests still pass, plus new regression tests.

## B2.5 — venues as entities, price, and recurrence

Context: a user taps a church on the map and wants to see "communion Sunday,
free; concert Wednesday, €17". Three separate capabilities are needed and two
existing fields are being silently dropped.

1. FIX DROPPED FIELDS
   RawRecord has `price` and `rrule`. CanonicalEvent has neither, so both are
   discarded in _merge_cluster(). Add both to CanonicalEvent and merge them by
   trust precedence like the other fields. Add a regression test asserting a
   record with a price survives deduplication.

2. VENUES AS ENTITIES
   Venue is currently a value object embedded in each event. Promote it:
   - new `venues` table: id (sha1 of normalised name + city, 16 chars), name,
     city, address, lat, lon, default_price, notes, resolved_at, geocode_source
   - events reference venue_id
   - normalised name should reuse normalize_title() so "Café de Wijnhaven" and
     "Cafe de Wijnhaven" resolve to one venue
   - this table is also the geocoding cache for B3 — one table, not two
   - add a `cityfeed venues` CLI command listing venues with event counts and
     whether they resolved to coordinates

3. OCCURRENCE EXPANSION
   Keep the series as the canonical record with its rrule intact — do not
   flatten a weekly event into 52 canonical events, that destroys the fact that
   it is one event and breaks dedup.
   Instead materialise a separate `occurrences` table: id, event_id, start, end,
   is_free, price, cancelled, is_override.
   - expand rrule for the next 90 days using python-dateutil's rrulestr
   - honour EXDATE and RDATE
   - handle DST transitions correctly: expand in the event's local timezone,
     then convert, so a 20:00 weekly event stays at 20:00 local across the
     October change rather than shifting an hour
   - handle monthly rules landing on the 31st in short months
   - per-occurrence overrides live here: setting one occurrence free must not
     affect the others
   - non-recurring events get exactly one occurrence row
   - queries should hit occurrences; dedup continues to operate on series

4. API
   GET /v1/venues            list, with filters city, bbox, has_coords
   GET /v1/venues/{id}       venue detail plus upcoming occurrences with
                             per-date pricing
   Add an `expand=occurrences` option to GET /v1/events so a client can get
   dated instances rather than series.

5. DASHBOARD
   Cluster markers by venue_id rather than plotting one marker per event —
   currently multiple events at one venue stack into an unreadable pile at
   identical coordinates. Marker popup lists what is on at that venue with
   date and price per occurrence. Keep the existing provenance UI intact
   (border weight by source count, per-source strip, per-source titles in the
   detail pane).

ACCEPTANCE
- a weekly recurring ICS event yields 1 series row and ~13 occurrence rows
- overriding one occurrence's price leaves the others unchanged
- a 20:00 weekly event stays 20:00 local across the DST boundary
- GET /v1/venues/{id} returns upcoming occurrences with per-date pricing
- two events at the same venue render as one marker, both listed in the popup
- all existing tests still pass

If short on time, items 1, 2 and 4 deliver most of the value; item 3 is the
fiddly one and can ship separately.

## B3. Venue geocoding

> Add `cityfeed/geocode.py`.
>
> Key design point: **geocode venues, not events.** Delft has maybe 60–100
> venues. Each is geocoded once and cached forever; every future event at that
> venue is free. The cache is the `venues` table created in B2.5 — resolve into
> that table rather than standing up a second one, so there is exactly one
> answer to "where is this venue".
>
> Providers behind one interface, tried in order:
> 1. **PDOK Locatieserver** (NL) —
>    `https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?q={query}&fq=type:adres&rows=1`
>    Coordinates come back in `response.docs[0].centroide_ll` as `POINT(lon lat)`.
>    No key required.
> 2. **Nominatim** fallback —
>    `https://nominatim.openstreetmap.org/search?q={query}&format=json&limit=1`
>    Requires a descriptive `User-Agent`, hard limit 1 request/second.
>
> Query construction matters: try `"{venue name}, {address}, Delft"` first, fall
> back to `"{venue name}, Delft"`, then `"{address}, Delft"`. Record which
> query succeeded. Reject results whose coordinates fall outside a Delft bounding
> box (roughly lat 51.97–52.04, lon 4.32–4.40) — geocoders will happily return
> a same-named street in another province.
>
> Wire it into the pipeline between extraction and dedup: any `Venue` without
> coordinates gets resolved before records reach `deduplicate()`. Dedup quality
> improves substantially once venues have geometry.
>
> Add `cityfeed geocode --city Delft --refresh` to re-resolve the cache.
>
> **Acceptance:** ≥85% of distinct Delft venues resolve to coordinates inside
> the bounding box. Second run makes zero network calls. Tests cover the
> bounding-box rejection and the fallback chain, using recorded responses.

## B4. Holdout recall measurement

> This replaces hand-labelling. Add `cityfeed/recall.py` and a
> `cityfeed recall --city Delft` command.
>
> Load `sources/holdout_delft.yaml` — same `SourceSpec` schema as the main
> registry, but these sources are **never** ingested by `run`. Enforce that:
> `load_registries()` must exclude any file matching `holdout_*.yaml`, and
> `cityfeed run` should fail loudly if a holdout id appears in the main registry.
>
> `recall` fetches the holdout sources, extracts records from them, and matches
> each against the canonical events already in the database, reusing the
> matching rule in `evaluate.py` (`_same_event`) rather than a new one.
>
> Report:
> - recall against holdout: matched / total holdout events
> - the actual list of missed events, with title, start, venue, holdout source
> - recall broken down per holdout source
>
> The missed list is the deliverable. It tells you which real sources to add
> next, which is the whole point.
>
> Also implement `--capture-recapture`: using two holdouts A and B, estimate the
> total event population as `|A| × |B| / |A ∩ B|` and report estimated coverage.
> State clearly in the output that this assumes source independence, which is
> violated when one source copies another, so the estimate skews optimistic.
>
> **Acceptance:** `cityfeed recall --city Delft` prints a recall figure with a
> stated denominator and an actionable miss list. Holdout sources never appear
> in `cityfeed run` output.

## B5. API layer

> Add `cityfeed/api.py` — FastAPI over the existing SQLite store. Read-only
> except for the refresh endpoint. Do not reimplement query logic in the API;
> it reads what the pipeline wrote.
>
> ```
> GET  /v1/events
>        ?city= &category= &from= &to= &free=true
>        &min_sources=2 &bbox=minLon,minLat,maxLon,maxLat
>        &q= &limit=50 &cursor=
> GET  /v1/events/{id}      full record incl. members[] provenance
> GET  /v1/sources          registry + per-source health, last success, record count
> GET  /v1/categories       category list with counts
> GET  /v1/health           200 only if every enabled source succeeded within 2× cadence
> POST /v1/admin/refresh    triggers a crawl, API-key auth
> ```
>
> - **Auth**: API key in `X-API-Key`, compared with `secrets.compare_digest`.
>   Keys from an env var (comma-separated). Read endpoints optionally public via
>   a config flag; `/v1/admin/*` always requires a key.
> - **Pagination**: cursor-based, not offset. Cursor is an opaque base64 of
>   `(start_iso, id)`. Stable under inserts.
> - **ETag** on collection responses, hash of the result set; honour
>   `If-None-Match` with a 304.
> - `min_sources` is the differentiating filter — corroborated events only.
>   Make sure it's in the OpenAPI description.
> - Return `Cache-Control: public, max-age=300` on reads.
>
> **Acceptance:** `uvicorn cityfeed.api:app` serves, `/docs` renders,
> paginating through all Delft events with `limit=5` yields every event exactly
> once with no duplicates or gaps, a bad key gets 401, and a repeat request with
> the ETag gets 304. Tests use `TestClient` against a seeded temp database.

## B6. Point the dashboard at the API

> The dashboard currently has data inlined at build time. Add a runtime mode:
> if `window.CITYFEED_API` is set, fetch from `/v1/events` instead, with the
> filter controls mapping to query params so filtering happens server-side.
> Keep the inline mode working as an offline fallback.
>
> Preserve the provenance UI exactly — marker border weight by source count, the
> per-source strip on cards, per-source titles in the detail pane. That's the
> part worth keeping.
>
> **Acceptance:** dashboard works both ways. With the API set, changing a filter
> issues a request and re-renders; with it unset, behaviour is unchanged.

## B7. Write-up

> Rewrite `README.md` around what's now real. Replace every fixture-derived
> number with a measured one.
>
> Sections:
> 1. What it is, one paragraph, with real numbers
> 2. **Measurement** — lead with this. Recall against a held-out set of public
>    sources, with the denominator stated. Explain why "94% coverage" without a
>    denominator is not a number. Include capture-recapture and its independence
>    caveat.
> 3. **Tiered extraction** — real per-tier source counts and the cost argument
> 4. **Deduplication** — real duplication rate, blocking/scoring/merge design
> 5. **Onboarding a new city** — the honest cost. What's config (registry rows,
>    locale, timezone), what's per-city work (holdout selection, geocoder for a
>    new country, wrapper induction for HTML-only sources). Be specific: this is
>    the section a reader will judge the design by.
> 6. **API** — endpoint table, the `min_sources` filter and why it exists
> 7. **Known limits** — keep it honest and specific
>
> Also add `docs/adding-a-city.md`: a literal checklist someone else could
> follow, with the Madrid registry as the worked example.
>
> **Acceptance:** no number in the README traceable to synthetic fixtures.
> Every claim about accuracy has a stated denominator.

---

## Order and effort

| | task | est. |
|---|---|---|
| 1 | B1 probe tool | 1h |
| 2 | A1 compile URLs, run probe | 40m |
| 3 | B2 live sources + fix breakage | 2–4h |
| 4 | A4 sanity-read output | 20m |
| 4.5 | B2.5 venues, price, recurrence | 2–3h |
| 5 | B3 geocoding | 1–1.5h |
| 6 | A2 + B4 holdouts and recall | 1h |
| 7 | B5 API | 40m |
| 8 | B6 dashboard wiring | 30m |
| 9 | B7 write-up | 1–2h |

**~9–12 hours.** A weekend, if the live sources cooperate. B2 is the only task
with real variance — it's the one where reality pushes back.

## Rules for the whole build

- **No model calls in the pipeline.** If something can't be parsed
  deterministically, disable it and write down why. The zero-token property is
  the argument; don't quietly lose it.
- **Every real-world failure becomes a fixture and a regression test.**
- **Never report an accuracy number without its denominator.**
- **Holdouts stay held out.** The moment one leaks into the registry, the recall
  number becomes self-graded and worthless.
