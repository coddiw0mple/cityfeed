# Adding a city

A checklist someone else could follow. Madrid is the worked example throughout,
because it is the case that stresses the "just add rows" claim hardest: different
country, different language, different geocoder, and a municipal open-data feed
where Delft has none.

Budget **one working day**, most of it in step 2. Delft took about that, and the
variance is almost entirely in how cooperative the sources are.

---

## 0. Before you start

You need, in this order:

- a timezone and locale for the city
- a bounding box (below)
- 40 minutes of somebody's local knowledge

That last one is not a formality. Delft's single best source — an ICS feed with
115 events at a student association — is linked from nowhere, exposed by no
`<link rel="alternate">`, and matched none of the nine path patterns the probe
guesses. It was found because someone said "the study associations run real
calendars". The same path returns 404 on nine other Delft associations. There is
no substitute for this step and no tooling that replaces it.

---

## 1. Compile candidate URLs (40 min, human)

Write one URL per line into `urls_<city>.txt`. Over-include: sorting the live
from the dead is the probe's job, and a wrong guess costs one HTTP request.

Cover these categories, which is roughly how a city's event data is distributed:

| Category | Madrid example |
|---|---|
| Municipal open data | `datos.madrid.es` event endpoint |
| Tourism board | `esmadrid.com/agenda` |
| City-owned venues | Madrid Destino (Teatro Español, CentroCentro, Matadero) |
| Independent venues | Sala El Sol, Café Berlín, Cines Golem |
| University | UCM / UPM faculty calendars |
| Student associations | ESN Madrid, Erasmus groups |
| Editorial | *El País* agenda, *Time Out Madrid* |
| Aggregators | Meetup, Eventbrite, local uitagenda-equivalents |

Guess paths freely: `/agenda`, `/eventos`, `/programacion`, `/events/feed/`,
`/wp-json/wp/v2/types`, `/feed/ical/`, `?ical=1`.

## 2. Probe them (10 min tool, 1–3 h acting on it)

```bash
cityfeed probe --file urls_madrid.txt --city Madrid --locale es --out probed.yaml
```

The probe **verifies rather than detects**: it runs the real extractors on the
real bytes and reports how many records came out. "jsonld present, 0 events" is a
different and much more useful answer than "has schema.org markup".

Then work the output. In practice you will hit:

- **`0 events` despite JSON-LD.** Check whether the events are on *detail* pages.
  If so it is a `jsonld_index` source: give it a `link` selector and it will crawl
  listing → detail and stitch the results.
- **WordPress with no visible programme.** Check `/wp-json/wp/v2/types` for a
  custom post type (`shows`, `agenda`, `eventos`, `tribe_events`). This is the
  single highest-yield trick in the whole process.
- **An RSS feed with a suspiciously round item count.** It is a blog feed. See
  step 5.
- **No structured data at all.** Read the markup once and write CSS selectors
  into the registry — that is a `wrapper`, and it is the only place a model is
  ever used.
- **JavaScript-rendered dates.** Open the page in a browser and check the network
  panel for the JSON endpoint it calls; register *that*. If there isn't one, set
  `enabled: false` and write down why. Do not reach for a headless browser: the
  zero-token property is the argument.

## 3. Write the registry (30 min)

`sources/madrid.yaml`. Trust by source type — municipal 2, venue 3, aggregator 4,
editorial 5, and 1 for an organiser publishing its own calendar.

```yaml
sources:
  - id: madrid_ayuntamiento_api
    city: Madrid
    country: ES
    type: api
    url: https://datos.madrid.es/egob/catalogo/206974-0-agenda-eventos-culturales-100.json
    trust: 2
    timezone: Europe/Madrid     # not the default
    locale: es                  # drives stopwords and free-text detection
```

Three fields carry most of the per-city weight:

- **`timezone`** — naive datetimes are localised to it. Getting it wrong shifts
  the whole corpus by hours, silently.
- **`locale`** — selects stopwords for title matching and the phrases that mean
  "free entry". `es` already exists; a new language is a dict entry in
  `normalize.py`.
- **`venue_name` / `venue_address`** — set these on any single-venue source. A
  venue page never repeats its own name: a cinema's listing says "Sala 3", a
  pub's says nothing. This one line fixes geocoding, dedup and categorisation
  together.

## 4. Add the bounding box (5 min, and do not skip it)

`cityfeed/geocode.py`, `CITY_BBOX`:

```python
"madrid": (40.31, 40.56, -3.89, -3.52),
```

Without it, out-of-city results cannot be rejected — and geocoders never say "I
don't know". Asked for `"Bacchusstraat, Delft"`, PDOK returned an address in
**Almere**. Asked for `"Bacchus"`, Nominatim returned a hamlet in **Tennessee**.
Both would have been stored as fact.

Spain also needs a provider decision. PDOK is Dutch-only, so Madrid falls through
to Nominatim for everything — which works, is slower (1 req/sec, hard limit), and
is less accurate on street addresses. A national geocoder (CartoCiudad for ES) is
a `Provider` subclass of about thirty lines. That is real per-city work, and the
honest cost of a new country.

## 5. Run it and fix what breaks (1–3 h)

```bash
cityfeed run --city Madrid
```

Expect, roughly in order of likelihood: encodings, redirect chains, `Content-Type`
lies, `@type: "event"` in lowercase, `startDate` as a bare date, one `@id` reused
across a run of shows, `address` as a bare string, and dates rendered by JS.

**The one that will cost you real damage**: an RSS feed's `<pubDate>` is when the
*article* was published, not when the event happens. Every venue running a
WordPress blog will hand you twenty confidently-wrong events dated to the moment
someone hit publish. `extract_rss` refuses this by default; a feed genuinely dated
by event opts in with `date_from: published`. Never set that flag to raise a
count.

For each real failure: save the payload to `tests/fixtures/`, add a regression
test, then fix it. That loop is the point of the snapshot store.

```bash
cityfeed venues --city Madrid   # which venues resolved, which didn't
```

Unresolved venues are where the next unit of work is. If they are room names
("Sala 3", "Aula Magna"), the fix is `venue_name` on the source, not a better
geocoder.

## 6. Choose holdouts (1 h, human, and get this right)

`sources/holdout_madrid.yaml`. Two or three sources you **never** ingest.
`load_registries()` skips `holdout_*.yaml` structurally and `cityfeed run` aborts
if one leaks, so the guarantee is enforced rather than remembered.

Two properties matter, and the second is easy to get wrong:

1. **Independent of everything ingested.** An aggregator that copies your venue
   pages measures your sources against themselves.
2. **Publishes for *that municipality*.** Meetup's "near Delft" is a radius
   search: 10 of its 12 results were in Rotterdam, Den Haag or Brussels. Scoring
   those as misses measures the radius, not your coverage. `recall` filters them
   out and reports how many it dropped — but a holdout that contributes almost
   nothing to the denominator is a holdout you should replace.

```bash
cityfeed recall --city Madrid --capture-recapture
```

The miss list is the deliverable. In Delft it named a venue (Cultuurlab) that no
ingested source listed, and a whole category the pipeline is blind to (free,
unticketed café events). Both went straight into the registry as documented rows.

If you ever want to ingest a holdout, promote a fresh one first.

## 7. Serve and check (20 min, human)

```bash
CITYFEED_DB=data/cityfeed.db uvicorn cityfeed.api:app
```

Then read 15–20 events in the dashboard. You are not grading, you are
smell-testing: are titles mangled, are times plausible, did anything obviously
non-event get through. Feed what you find back as a bug list.

Things this step caught in Delft that no test would have:

- every event at a cinema had the venue "Zaal 3" — the room, not the venue
- 103 of 167 events were uncategorised, because of the same bug
- times displayed in the *reader's* timezone, showing a 20:15 Delft concert as
  23:45

---

## What is genuinely just config

Registry rows, locale, timezone, trust tiers, per-source field maps, `venue_name`,
`max_detail_pages`, `date_from`. No Python for any of it.

## What is genuinely per-city work

| Item | Where |
|---|---|
| Finding the sources | a human, 2–4 h |
| Wrapper selectors | a model reads the markup once, ~15 min per site |
| Bounding box | one row in `CITY_BBOX` |
| Geocoder for a new country | a `Provider` subclass, ~30 lines |
| Stopwords / free-entry phrasing for a new language | a dict entry in `normalize.py` |
| Holdout selection and validation | a human, ~1 h |

If a new city ever needs a new *extractor*, that is a signal the tier is missing
from the ladder rather than that the city is special — `wp_rest` and
`jsonld_index` were both added that way, and both then applied to sources nobody
had looked at yet.
