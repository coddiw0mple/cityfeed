"""Source discovery: what tier is this URL, and can we actually parse it?

The registry is the product. Everything else in the pipeline is downstream of
the question this module answers: for a given URL, what is the cheapest tier
that yields real events?

The design rule here is *verify, don't detect*. A page with a `<script
type="application/ld+json">` tag is not a JSON-LD source; a page from which
`extract_jsonld` returns records is. Most "it has schema.org markup!" pages
carry a `WebSite` or `BreadcrumbList` block and zero events, and a probe that
reports presence rather than yield sends you off to write a registry row for a
source that will silently produce nothing. So every finding this module reports
is the output of the real extractor, run against the real bytes.

The same standard applies to linked feeds: a `<link rel="alternate"
type="text/calendar">` is a lead, not a finding. The probe follows it, parses
it, and reports the record count — which is why the suggested `SourceSpec`
points at the feed URL rather than the page that mentioned it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from lxml import etree
from lxml import html as lxml_html

from .extract import ExtractionError, extract_ics, extract_jsonld, extract_rss
from .models import SourceSpec, SourceType, TrustTier

USER_AGENT = "cityfeed-probe/0.1 (+https://github.com/coddiw0mple/cityfeed)"

# Politeness. Concurrency is global, the delay is per host: four different
# venues get probed at once, one venue never gets two requests inside a second.
MAX_CONCURRENCY = 4
PER_HOST_DELAY = 1.0

# URL shapes that look like an event page in the languages this project covers.
_EVENT_URL = re.compile(
    r"/(events?|evenement|evenementen|agenda|programma|voorstelling(?:en)?"
    r"|kalender|calendar|activiteit(?:en)?|eventos|actividades)(?:/|$|\?)",
    re.IGNORECASE,
)

# Tiers in the order we'd rather have them. Ties are broken by verified yield,
# so this only decides between two tiers that both actually produced events.
_TIER_RANK = {"ics": 0, "jsonld": 1, "api": 2, "rss": 3, "microdata": 4, "sitemap": 5}


@dataclass
class Finding:
    """One extraction route detected on a URL.

    `events` is None when the route was detected but not verified (microdata,
    sitemap) and an integer when an extractor actually ran. Zero is a real and
    important answer: it is the difference between "there is markup here" and
    "there is data here".
    """

    tier: str
    detail: str
    url: Optional[str] = None      # the feed, when it differs from the page
    events: Optional[int] = None

    @property
    def verified(self) -> bool:
        return self.events is not None and self.events > 0

    def render(self) -> str:
        if self.events is None:
            return f"{self.tier} ({self.detail})"
        return f"{self.tier}, {self.events} events"


@dataclass
class ProbeResult:
    url: str
    final_url: Optional[str] = None
    status: Optional[int] = None
    content_type: Optional[str] = None
    error: Optional[str] = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def redirected(self) -> bool:
        return bool(self.final_url) and self.final_url != self.url

    @property
    def best(self) -> Optional[Finding]:
        """The finding a registry row should be written against.

        Verified beats unverified, more events beats fewer, and only then does
        tier preference break the tie. A wrapper is never "best" — it is what
        you are left with, which is why it is reported as an absence.
        """
        if not self.findings:
            return None
        return min(
            self.findings,
            key=lambda f: (not f.verified, -(f.events or 0), _TIER_RANK.get(f.tier, 9)),
        )

    @property
    def tier_label(self) -> str:
        if self.error:
            return "error"
        best = self.best
        if best is None:
            return "wrapper?"
        return best.tier

    @property
    def event_count(self) -> Optional[int]:
        best = self.best
        return best.events if best else None


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _parse_html(body: str):
    """Parse markup that may well be broken, without taking the probe down."""
    try:
        return lxml_html.fromstring(body)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None


def _probe_spec(url: str, source_type: SourceType, locale: str = "nl") -> SourceSpec:
    """A throwaway spec so the real extractors can be called unchanged."""
    return SourceSpec(id="probe", city="probe", type=source_type, url=url, locale=locale)


def sniff_kind(body: str, content_type: str) -> str:
    """What this payload actually is, ignoring what the server claimed.

    `Content-Type` on event feeds is wrong often enough that trusting it is a
    bug: ICS served as text/html, JSON served as text/plain, RSS served as
    application/octet-stream. The first hundred bytes never lie.
    """
    head = body[:2048].lstrip("﻿ \t\r\n")
    lowered = head.lower()
    if lowered.startswith("begin:vcalendar"):
        return "ics"
    if head.startswith(("{", "[")):
        return "json"
    if "<rss" in lowered or "<feed" in lowered or "<rdf:rdf" in lowered:
        return "feed"
    if "<urlset" in lowered or "<sitemapindex" in lowered:
        return "sitemap"
    if lowered.startswith("<?xml") or "<html" in lowered or "<!doctype html" in lowered:
        return "html" if "<html" in lowered or "<!doctype html" in lowered else "xml"
    if "html" in content_type:
        return "html"
    if "calendar" in content_type:
        return "ics"
    if "json" in content_type:
        return "json"
    if "xml" in content_type:
        return "xml"
    return "other"


def find_jsonld(body: str, url: str) -> Optional[Finding]:
    """Run the real JSON-LD extractor and report its yield, not its presence."""
    tree = _parse_html(body)
    if tree is None:
        return None
    blocks = tree.xpath('//script[@type="application/ld+json"]')
    if not blocks:
        return None

    types: set[str] = set()
    for node in blocks:
        text = node.text_content()
        if not text or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            types.add("unparseable")
            continue
        stack = [parsed]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if "@graph" in item:
                    stack.append(item["@graph"])
                node_type = item.get("@type")
                if isinstance(node_type, list):
                    types.update(str(t) for t in node_type)
                elif node_type:
                    types.add(str(node_type))

    try:
        records = extract_jsonld(body, _probe_spec(url, SourceType.JSONLD))
    except Exception as exc:  # noqa: BLE001 - a probe never crashes on one page
        return Finding("jsonld", f"present, extractor raised {type(exc).__name__}: {exc}", events=0)

    detail = "types: " + (", ".join(sorted(types)[:5]) or "none")
    return Finding("jsonld", detail, events=len(records))


def find_microdata(body: str) -> Optional[Finding]:
    """Detect microdata/RDFa Event markup. Detection only — no extractor yet.

    Reported rather than parsed on purpose: microdata is rare enough on event
    pages that writing a third structured extractor for it is not yet earned.
    Knowing it is there tells you the page has machine-readable event data and
    is worth a wrapper rather than a shrug.
    """
    tree = _parse_html(body)
    if tree is None:
        return None
    micro = tree.xpath(
        "//*[@itemtype[contains(., 'schema.org') and contains(., 'Event')]]"
    )
    rdfa = tree.xpath("//*[@typeof[contains(., 'Event')]]")
    total = len(micro) + len(rdfa)
    if not total:
        return None
    kind = "microdata" if micro else "rdfa"
    return Finding("microdata", f"{total} {kind} Event nodes, not extracted")


def _hrefs(tree, xpath: str, base: str) -> list[str]:
    out = []
    for node in tree.xpath(xpath):
        href = node.get("href")
        if href:
            out.append(urljoin(base, href))
    return out


def find_calendar_links(body: str, base: str) -> list[str]:
    tree = _parse_html(body)
    if tree is None:
        return []
    links = _hrefs(
        tree,
        "//link[@rel='alternate'][contains(translate(@type,'TEXCALNDR','texcalndr'),'calendar')]",
        base,
    )
    links += _hrefs(tree, "//a[contains(translate(@href,'ICS','ics'),'.ics')]", base)
    links += _hrefs(tree, "//link[contains(translate(@href,'ICS','ics'),'.ics')]", base)
    return list(dict.fromkeys(links))


def find_feed_links(body: str, base: str) -> list[str]:
    tree = _parse_html(body)
    if tree is None:
        return []
    links = _hrefs(
        tree,
        "//link[@rel='alternate'][contains(@type,'rss') or contains(@type,'atom')]",
        base,
    )
    links += _hrefs(tree, "//a[contains(@href,'/feed') or contains(@href,'/rss')]", base)
    return list(dict.fromkeys(links))[:3]


def find_sitemap_events(sitemap_body: str) -> Optional[Finding]:
    """Count event-looking URLs in a sitemap.

    A last-resort signal, and a weak one: it says a crawler could find event
    pages, not that anything on them is parseable. Reported so that "no tier"
    and "no tier but 300 event URLs to wrapper against" are distinguishable.
    """
    try:
        root = etree.fromstring(sitemap_body.encode("utf-8", "replace"))
    except etree.XMLSyntaxError:
        return None
    locs = [el.text for el in root.iter() if el.tag.endswith("}loc") or el.tag == "loc"]
    matches = [loc for loc in locs if loc and _EVENT_URL.search(loc)]
    if not matches:
        return None
    return Finding("sitemap", f"{len(matches)}/{len(locs)} event-shaped URLs", url=matches[0])


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

class _HostThrottle:
    """One request per host per second, regardless of global concurrency."""

    def __init__(self, delay: float = PER_HOST_DELAY) -> None:
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def __call__(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        await lock.acquire()
        wait = self.delay - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        return lock

    def done(self, host: str, lock: asyncio.Lock) -> None:
        self._last[host] = time.monotonic()
        lock.release()


async def _get(
    client: httpx.AsyncClient, url: str, throttle: _HostThrottle
) -> tuple[Optional[httpx.Response], Optional[str]]:
    host = urlparse(url).netloc
    lock = await throttle(host)
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        return response, None
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - bad TLS, bad encodings, bad everything
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        throttle.done(host, lock)


async def _verify_feed(
    client: httpx.AsyncClient, throttle: _HostThrottle, url: str, tier: str
) -> Optional[Finding]:
    """Fetch a linked feed and parse it, so the count in the table is real."""
    response, error = await _get(client, url, throttle)
    if response is None:
        return Finding(tier, f"linked but unfetchable: {error}", url=url, events=0)
    if response.status_code >= 400:
        return Finding(tier, f"linked but HTTP {response.status_code}", url=url, events=0)

    body = response.text
    kind = sniff_kind(body, response.headers.get("content-type", "").lower())
    spec_type = SourceType.ICS if tier == "ics" else SourceType.RSS
    try:
        if tier == "ics":
            if kind != "ics":
                return Finding(tier, f"link served {kind}, not a calendar", url=url, events=0)
            records = extract_ics(body, _probe_spec(url, spec_type))
        else:
            if kind not in {"feed", "xml"}:
                return Finding(tier, f"link served {kind}, not a feed", url=url, events=0)
            records = extract_rss(body, _probe_spec(url, spec_type))
    except ExtractionError as exc:
        return Finding(tier, f"unparseable: {exc}", url=url, events=0)
    except Exception as exc:  # noqa: BLE001
        return Finding(tier, f"{type(exc).__name__}: {exc}", url=url, events=0)
    return Finding(tier, "verified", url=url, events=len(records))


async def probe_url(
    client: httpx.AsyncClient,
    url: str,
    throttle: _HostThrottle,
    check_sitemap: bool = True,
) -> ProbeResult:
    """Probe one URL. Never raises: a failed probe is a result, not an error."""
    result = ProbeResult(url=url)
    response, error = await _get(client, url, throttle)
    if response is None:
        result.error = error
        return result

    result.status = response.status_code
    result.final_url = str(response.url)
    result.content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if response.status_code >= 400:
        result.error = f"HTTP {response.status_code}"
        return result

    try:
        body = response.text
    except Exception as exc:  # noqa: BLE001 - undecodable bytes
        result.error = f"undecodable body: {type(exc).__name__}"
        return result

    base = result.final_url
    kind = sniff_kind(body, (result.content_type or "").lower())

    # The URL may already *be* a feed. Content-Type is not consulted for this
    # decision; several real Delft feeds serve ICS as text/html.
    if kind == "ics":
        try:
            records = extract_ics(body, _probe_spec(base, SourceType.ICS))
            result.findings.append(Finding("ics", "url is a calendar", events=len(records)))
        except Exception as exc:  # noqa: BLE001
            result.findings.append(Finding("ics", f"unparseable calendar: {exc}", events=0))
        return result

    if kind == "feed":
        try:
            records = extract_rss(body, _probe_spec(base, SourceType.RSS))
            result.findings.append(Finding("rss", "url is a feed", events=len(records)))
        except Exception as exc:  # noqa: BLE001
            result.findings.append(Finding("rss", f"unparseable feed: {exc}", events=0))
        return result

    if kind == "json":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            result.error = "claims json, is not json"
            return result
        count = len(payload) if isinstance(payload, list) else len(payload.get("@graph", []))
        result.findings.append(
            Finding("api", "json endpoint, needs a per-API extractor", events=count or None)
        )
        return result

    if kind == "sitemap":
        found = find_sitemap_events(body)
        if found:
            result.findings.append(found)
        return result

    if kind not in {"html", "xml"}:
        result.error = f"non-HTML content: {result.content_type or kind}"
        return result

    # --- HTML: the interesting path -------------------------------------
    if (jsonld := find_jsonld(body, base)) is not None:
        result.findings.append(jsonld)
    if (micro := find_microdata(body)) is not None:
        result.findings.append(micro)

    for link in find_calendar_links(body, base)[:1]:
        if (found := await _verify_feed(client, throttle, link, "ics")) is not None:
            result.findings.append(found)
    for link in find_feed_links(body, base)[:1]:
        if (found := await _verify_feed(client, throttle, link, "rss")) is not None:
            result.findings.append(found)

    # Only spend a request on the sitemap when nothing better turned up.
    if check_sitemap and not any(f.verified for f in result.findings):
        parts = urlparse(base)
        sitemap_url = f"{parts.scheme}://{parts.netloc}/sitemap.xml"
        response, _ = await _get(client, sitemap_url, throttle)
        if response is not None and response.status_code < 400:
            if (found := find_sitemap_events(response.text)) is not None:
                result.findings.append(found)

    return result


async def probe_all(
    urls: list[str],
    concurrency: int = MAX_CONCURRENCY,
    timeout: float = 20.0,
    check_sitemap: bool = True,
) -> list[ProbeResult]:
    throttle = _HostThrottle()
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, max_redirects=5
    ) as client:
        async def one(url: str) -> ProbeResult:
            async with semaphore:
                return await probe_url(client, url, throttle, check_sitemap)

        return await asyncio.gather(*(one(u) for u in urls))


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

_TRUST_HINT = {
    "municipal": TrustTier.MUNICIPAL,
    "venue": TrustTier.VENUE,
    "aggregator": TrustTier.AGGREGATOR,
    "editorial": TrustTier.EDITORIAL,
}


def suggest_id(url: str) -> str:
    """A registry id from a URL: stable, readable, collision-resistant enough."""
    parts = urlparse(url)
    host = re.sub(r"^www\.", "", parts.netloc).split(":")[0]
    host_slug = re.sub(r"[^a-z0-9]+", "_", host.rsplit(".", 1)[0].lower()).strip("_")
    path_slug = re.sub(r"[^a-z0-9]+", "_", parts.path.lower()).strip("_")
    # Path segments that say nothing beyond "this is the events page".
    path_slug = re.sub(r"^(en|nl|es)_", "", path_slug)
    if not path_slug or path_slug in {"index", "home"}:
        return host_slug
    return f"{host_slug}_{path_slug}"[:48]


def suggest_yaml(result: ProbeResult, city: str, locale: str = "nl") -> Optional[str]:
    """A paste-ready registry block, or None when there is nothing to suggest."""
    best = result.best
    if best is None or best.tier in {"microdata", "sitemap"}:
        return None
    if best.tier == "api":
        # An API needs a bespoke extractor; emitting an enabled row would be a
        # promise the dispatch table cannot keep.
        return None

    target = best.url or result.final_url or result.url
    entry = {
        "id": suggest_id(target),
        "city": city,
        "country": "NL",
        "type": best.tier,
        "url": target,
        "trust": int(TrustTier.VENUE),
        "cadence_minutes": 360,
        "locale": locale,
    }
    if not best.verified:
        entry["enabled"] = False
        entry["notes"] = f"probe found {best.tier} but extracted 0 events: {best.detail}"
    else:
        entry["notes"] = f"probe: {best.events} events via {best.tier}"

    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=100)
    return "\n".join("  " + line if line.strip() else line for line in block.splitlines())


def render_table(results: list[ProbeResult]) -> str:
    rows = []
    header = f"{'url':<52} {'tier':<10} {'events':>6}  note"
    rows.append(header)
    rows.append("-" * len(header))
    for result in results:
        url = result.url if len(result.url) <= 52 else result.url[:49] + "..."
        count = "" if result.event_count is None else str(result.event_count)
        if result.error:
            note = result.error
        else:
            best = result.best
            note = best.detail if best else "no structured data - wrapper candidate"
            if result.redirected:
                note = f"-> {urlparse(result.final_url).netloc}; {note}"
        rows.append(f"{url:<52} {result.tier_label:<10} {count:>6}  {note[:60]}")

    verified = sum(1 for r in results if r.best and r.best.verified)
    total_events = sum(r.event_count or 0 for r in results if r.best and r.best.verified)
    rows.append("")
    rows.append(
        f"{verified}/{len(results)} URLs yield events with zero model calls "
        f"({total_events} records total)"
    )
    return "\n".join(rows)


def load_urls(path: str) -> list[str]:
    """One URL per line. `#` comments and blanks ignored."""
    from pathlib import Path

    urls = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not line.startswith(("http://", "https://")):
            line = "https://" + line
        urls.append(line)
    return list(dict.fromkeys(urls))
