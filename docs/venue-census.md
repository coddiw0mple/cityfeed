# Venue census — Delft, 2026-07-31

**How much of a city is actually machine-readable?**

Every coverage claim in this space rests on a number nobody publishes. "We parse
92% of our sources" is 92% of the sources you already picked — close to a
tautology, because the sources got picked *for* being parseable. The question
that means something is what fraction of the **venues in the city** publish
anything a crawler can read.

Answering it needs a venue list assembled by something other than the process
being measured. This one comes from OpenStreetMap. Search engines rank sites
with good markup, which is precisely the bias under test.

```bash
python scripts/venue_census.py --city Delft --out data/venue_census_delft.json
```

## Result

**361 venue-like places in Delft. 238 with a website. 9 publish
machine-readable events — 3.8%.** 193 events across those nine.

That 3.8% is a floor, not the answer. Validating it against venues already known
to work exposed its false negatives:

| Venue | Census | Reality |
|---|---|---|
| Theater de Veste | 0 | 20 — needs a two-level listing→detail crawl the probe cannot do |
| Jazzcafé Bebop | 0 | 2 — programme is on a different host (`shop.`) |
| Filmhuis Lumen | absent from OSM | 100 |
| popdelft, Prinsenhof | absent from OSM | real |

So OSM's website tags are themselves incomplete, and the probe misses two-level
sources. **The honest bracket is 5–15% — roughly one venue in ten.**

## The finding that changed how sources get found

All nine venues the census turned up were **new**. Zero overlap with the eight
sources in the live registry, which were assembled by hand via search: a
brewery, a student association, a microtheatre, a neighbourhood workshop, a
dance school — small places running WordPress event plugins.

Search found 8 sources across a full build. One automated pass over the venue
list found 9 more, none of them the same. **Enumerating venues beats searching
for sources**, and it is the cheaper of the two by a wide margin.

## Why the reachable rest cannot be read

**238 venue sites with a website: 9 publish machine-readable events, 20 could
not be fetched at all (DNS, TLS, timeout, 404), and 209 were reachable but not
machine-readable.** The breakdown below is of those **209** — not of 229. The
twenty unreachable sites are not evidence of anything: we never saw what they
publish, and folding them in would claim knowledge we do not have.

"Unstructured" is a shrug, not a diagnosis. Each of these fails for a specific
reason, and the reasons have entirely different fixes:

| Reason | Count | % | Fix |
|---|---|---|---|
| Points at social media, no on-site programme | 74 | 35% | None — supply side |
| Mentions programming, publishes no dates | 44 | 21% | None — the data does not exist |
| No event content at all | 36 | 17% | None needed — genuinely hosts nothing |
| Events in prose **with** dates | 29 | 14% | Wrapper induction (one model call per domain) |
| JS-rendered, nothing server-side | 19 | 9% | Find the XHR endpoint behind it |
| Programme on a ticketing host | 7 | 3% | Follow to the other domain |

**35% publish nothing on-site and point at social media instead** — the largest single bucket above. Separately, and on a different denominator: **74.6% link to Instagram or Facebook at all**, counting sites that also publish dates or render in JS. The first figure composes with the others to 100%; the second overlaps them and must not be added to them.

Either way it is the most important number here — but only the 35% belongs in
the table above, and conflating the two is precisely the denominator error this
document exists to argue against.

## What it means

Three groups, and conflating them is what produces both bad architecture and
bad forecasts:

- **36 (17%) should fail.** A restaurant with no programming has nothing to
  extract. Excluding them is correct behaviour, not a miss.
- **~25 (11%) are technically reachable** — and this number was initially
  reported as 55/26%, which was a hypothesis dressed as a measurement. Checking
  it: only 15 of the 29 "prose with dates" have three or more plausible event
  dates (the rest are opening hours and copyright years, and one is a terrace
  season); 3 of the 7 ticketing hosts are Eventbrite, which is a *holdout* and
  cannot be ingested without invalidating recall; and most of the 19
  JS-rendered are restaurants whose XHR endpoint returns a menu. See
  [coverage-strategy.md](coverage-strategy.md).
- **118 (56%) are unreachable by any crawler.** The event is real but exists
  only on Instagram, or the site says "live muziek elke donderdag" and never
  publishes a date. No model fixes this, because the data was never published.

So: **3.8% readable today against ~12% technically readable** — roughly 3×
headroom, all of it engineering rather than model spend. And a hard ceiling
above that, because the majority of a city's small-venue programming is simply
not on the web in any form a crawler can reach.

The corollary is uncomfortable and worth stating plainly: past ~12%, coverage
stops being an ingestion problem and becomes a supply-side one. A submission
form and an ICS import will beat any scraper for those 118 venues, permanently.

## Caveats

- OSM is volunteer-maintained; its venue list and website tags are incomplete
  in both directions.
- The probe cannot detect two-level (listing→detail) sources, so the 3.8%
  undercounts. Known false negatives are listed above.
- `restaurant` venues were counted in the denominator but not path-probed, on
  the grounds that most host nothing and 500 extra requests to other people's
  servers is not free.
- One city, one day. Delft is a university town of ~100k and is not obviously
  representative of anywhere else.
