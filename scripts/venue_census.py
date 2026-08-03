"""Venue census: what fraction of a city's venues publish machine-readable events?

The question every coverage claim rests on and almost nobody answers. It is
easy to say "we parse 92% of our sources" — that is 92% of the sources you
already chose, which is close to a tautology. The useful question is what
fraction of the *venues in the city* publish anything a crawler can read, and
answering it needs a venue list that was not assembled by the same process
being measured.

So the venue list comes from OpenStreetMap, not from a search engine. Search
ranks sites with good markup, which is exactly the bias under test.

Three passes, because a single pass reproduces the bias it is measuring:

  1. probe every venue homepage
  2. for anything empty, check /wp-json/wp/v2/types for an event post type and
     then *fetch it* — having a `tribe_events` type and having events in it are
     different claims
  3. try the handful of agenda paths a Dutch venue site actually uses, because
     the programme is rarely on the homepage

Then classify every failure by reason, since "unstructured" is a shrug rather
than a diagnosis and the six reasons have very different fixes — one is a
crawler feature, one is a model call, and one is not solvable in software.

    python scripts/venue_census.py --city Delft --out data/venue_census_delft.json

Results for Delft (2026-07-31) are in docs/venue-census.md.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cityfeed.probe import probe_all  # noqa: E402

UA = {"User-Agent": "cityfeed-census/1.0 (+https://github.com/coddiw0mple/cityfeed)"}

# Paths a Dutch venue site actually uses for its programme.
AGENDA_PATHS = ["/agenda", "/programma", "/events", "/activiteiten", "/evenementen"]

# Custom post types that WordPress event plugins create.
EVENT_CPT = ("event", "shows", "agenda", "voorstelling", "activiteit",
             "programma", "concert", "tribe_events", "eo_event", "mec-events")

# Venue kinds that plausibly programme anything. Restaurants are counted in the
# denominator but not path-probed: most genuinely host nothing, and probing
# five paths each is a lot of requests for other people's servers.
PROGRAMMING_KINDS = {"theatre", "arts_centre", "museum", "gallery", "events_venue",
                     "community_centre", "nightclub", "bar", "pub", "cafe", "dance",
                     "sports_centre"}

OVERPASS = """
[out:json][timeout:90];
area["name"="{city}"]["admin_level"="8"]->.a;
(
  node(area.a)["amenity"~"^(bar|cafe|pub|nightclub|theatre|community_centre|arts_centre|events_venue|restaurant)$"];
  way(area.a)["amenity"~"^(bar|cafe|pub|nightclub|theatre|community_centre|arts_centre|events_venue|restaurant)$"];
  node(area.a)["tourism"~"^(museum|gallery)$"];
  way(area.a)["tourism"~"^(museum|gallery)$"];
  node(area.a)["leisure"~"^(sports_centre|dance)$"];
  way(area.a)["leisure"~"^(sports_centre|dance)$"];
);
out tags;
"""

SOCIAL = re.compile(r"(instagram\.com|facebook\.com|fb\.me|linktr\.ee)", re.I)
TICKET = re.compile(r"(eventbrite|ticketmaster|paylogic|eventix|activetickets|"
                    r"ticketkantoor|yesplan|stager|weeztix|ticketswap)", re.I)
JSAPP = re.compile(r"(__NUXT__|__NEXT_DATA__|ng-version|data-reactroot|"
                   r"vue(?:\.min)?\.js|react-dom)", re.I)
PROSE_EVENT = re.compile(
    r"\b(elke|iedere)\s+(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\b"
    r"|\blive\s*muziek\b|\bagenda\b|\bprogramma\b|\boptreden\b|\bborrel\b|\bpubquiz\b"
    r"|\bworkshop\b|\bconcert\b|\bvoorstelling\b|\bexpositie\b|\bfestival\b", re.I)
DATEISH = re.compile(r"\b\d{1,2}\s+(jan|feb|mrt|maart|apr|mei|jun|jul|aug|sep|okt|nov|dec)"
                     r"|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", re.I)


def harvest(city: str) -> dict[str, dict]:
    """Venue list from OSM. Deliberately not from a search engine."""
    response = httpx.post("https://overpass-api.de/api/interpreter",
                          data=OVERPASS.format(city=city), timeout=120, headers=UA)
    response.raise_for_status()
    venues: dict[str, dict] = {}
    for element in response.json()["elements"]:
        tags = element.get("tags", {})
        site = tags.get("website") or tags.get("contact:website")
        name = tags.get("name")
        if not site or not name:
            continue
        if not site.startswith(("http://", "https://")):
            site = "https://" + site
        kind = tags.get("amenity") or tags.get("tourism") or tags.get("leisure")
        venues.setdefault(site.rstrip("/"), {"name": name, "kind": kind})
    return venues


async def _get_json(client, url):
    try:
        response = await client.get(url)
        return response.json() if response.status_code == 200 else None
    except Exception:  # noqa: BLE001 - a dead venue site is data, not an error
        return None


async def count_wp_events(urls: list[str]) -> dict[str, int]:
    """Find WordPress event post types and count what is actually in them."""
    found: dict[str, int] = {}
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as client:
        async def one(url: str) -> None:
            base = url.rstrip("/")
            async with semaphore:
                await asyncio.sleep(0.5)
                types = await _get_json(client, f"{base}/wp-json/wp/v2/types")
            if not isinstance(types, dict):
                return
            hits = [t for t in types if any(k in t.lower() for k in EVENT_CPT)]
            best = 0
            for cpt in hits:
                async with semaphore:
                    await asyncio.sleep(0.5)
                    data = await _get_json(client, f"{base}/wp-json/wp/v2/{cpt}?per_page=100")
                if isinstance(data, list):
                    best = max(best, len(data))
            if best:
                found[url] = best
        await asyncio.gather(*(one(u) for u in urls))
    return found


async def classify(urls: list[str]) -> dict[str, str]:
    """Why can't this one be read? One verdict each, most actionable first."""
    verdict: dict[str, str] = {}
    semaphore = asyncio.Semaphore(5)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=UA) as client:
        async def one(url: str) -> None:
            async with semaphore:
                await asyncio.sleep(0.3)
                try:
                    response = await client.get(url)
                    if response.status_code >= 400:
                        return
                    body = response.text
                except Exception:  # noqa: BLE001
                    return
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            if TICKET.search(body):
                verdict[url] = "programme on a ticketing host (other domain)"
            elif PROSE_EVENT.search(text) and DATEISH.search(text):
                verdict[url] = "events in prose WITH dates (wrapper candidate)"
            elif PROSE_EVENT.search(text):
                verdict[url] = "mentions programming but publishes no dates"
            elif JSAPP.search(body):
                verdict[url] = "JS-rendered, nothing server-side"
            elif SOCIAL.search(body):
                verdict[url] = "points at social media, no on-site programme"
            else:
                verdict[url] = "no event content on the site at all"
        await asyncio.gather(*(one(u) for u in urls))
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Delft")
    parser.add_argument("--out", default=None)
    parser.add_argument("--reasons-out", default=None)
    args = parser.parse_args()

    venues = harvest(args.city)
    urls = sorted(venues)
    print(f"{args.city}: {len(urls)} OSM venues with a website\n", flush=True)

    print("pass 1: probing homepages...", flush=True)
    results = asyncio.run(probe_all(urls, concurrency=4, timeout=20, check_sitemap=False))
    events = {r.url.rstrip("/"): (r.event_count or 0) if (r.best and r.best.verified) else 0
              for r in results}
    errors = {r.url.rstrip("/") for r in results if r.error}
    how = {u: "homepage" for u, n in events.items() if n}

    empty = [u for u in urls if not events.get(u) and u not in errors]
    print(f"pass 2: WordPress event post types on {len(empty)} sites...", flush=True)
    for url, count in asyncio.run(count_wp_events(empty)).items():
        events[url], how[url] = count, "wp_rest"

    targets = [u for u in urls if not events.get(u) and u not in errors
               and venues[u]["kind"] in PROGRAMMING_KINDS]
    paths = [u + p for u in targets for p in AGENDA_PATHS]
    print(f"pass 3: {len(paths)} agenda paths across {len(targets)} venues...", flush=True)
    for res in asyncio.run(probe_all(paths, concurrency=4, timeout=15, check_sitemap=False)):
        if not (res.best and res.best.verified):
            continue
        base = next((u for u in targets if res.url.startswith(u)), None)
        if base and res.best.events > events.get(base, 0):
            events[base], how[base] = res.best.events, "agenda path"

    still_empty = [u for u in urls if not events.get(u) and u not in errors]
    print(f"classifying {len(still_empty)} that yielded nothing...", flush=True)
    reasons = asyncio.run(classify(still_empty))

    census = {
        u: {"name": venues[u]["name"], "kind": venues[u]["kind"],
            "events": events.get(u, 0), "how": how.get(u, "error" if u in errors else "none"),
            "reason": reasons.get(u)}
        for u in urls
    }

    total, hits = len(census), {u: v for u, v in census.items() if v["events"]}
    print()
    print("=" * 70)
    print(f"  {len(hits)}/{total} venue sites publish machine-readable events "
          f"({len(hits) / total * 100:.1f}%)")
    print(f"  {sum(v['events'] for v in hits.values())} events across them")
    print("=" * 70)

    print("\nwhy the rest cannot be read:")
    for reason, n in collections.Counter(reasons.values()).most_common():
        print(f"  {n:>4}  {n / len(reasons) * 100:>5.1f}%   {reason}")

    print("\nby venue type:")
    by_kind: dict = collections.defaultdict(lambda: [0, 0])
    for v in census.values():
        by_kind[v["kind"]][1] += 1
        if v["events"]:
            by_kind[v["kind"]][0] += 1
    for kind, (a, b) in sorted(by_kind.items(), key=lambda kv: -kv[1][1]):
        print(f"  {kind:<18} {a:>3}/{b:<4} {a / b * 100:>5.1f}%")

    print("\nvenues that publish events:")
    for u, v in sorted(hits.items(), key=lambda kv: -kv[1]["events"]):
        print(f"  {v['events']:>4}  {v['how']:<12} {(v['name'] or '')[:30]:<30} {u[:44]}")

    if args.out:
        Path(args.out).write_text(json.dumps(census, indent=1, ensure_ascii=False))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
