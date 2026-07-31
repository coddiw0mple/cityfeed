"""Registry loading and fetching.

Two decisions here carry most of the operational weight.

First, sources live in YAML, not Python. Onboarding city N+1 should be an
afternoon of writing rows, not a sprint of writing scrapers. If a new city
needs new code, the extraction layer isn't actually city-agnostic and the
cost of growth is linear.

Second, every fetch is written to a content-addressed snapshot store. Crawl
once, re-parse for free. Without it, improving an extractor means re-crawling
the world and there is no way to check whether a change helped or hurt,
because the input has moved underneath you.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import yaml

from .models import SourceSpec, SourceType


def load_registry(path: str | Path) -> list[SourceSpec]:
    """Load and validate a source registry file."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    specs = [SourceSpec(**entry) for entry in raw.get("sources", [])]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"duplicate source id in registry: {spec.id}")
        seen.add(spec.id)
    return specs


HOLDOUT_PREFIX = "holdout_"


def load_registries(directory: str | Path) -> list[SourceSpec]:
    """Load every registry file in a directory, e.g. one YAML per city.

    Files named `holdout_*.yaml` are skipped, and the skip is structural rather
    than a convention someone has to remember. Those sources are the only
    independent estimate of what the pipeline missed; the moment one is ingested
    the recall figure is measuring the pipeline against itself and is worth
    nothing. `cityfeed recall` loads them explicitly, and nothing else does.
    """
    specs: list[SourceSpec] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        if path.name.startswith(HOLDOUT_PREFIX):
            continue
        specs.extend(load_registry(path))
    return specs


def load_holdouts(directory: str | Path, city: str | None = None) -> list[SourceSpec]:
    """Load only the held-out registries. Used by `recall`, by nothing else."""
    specs: list[SourceSpec] = []
    for path in sorted(Path(directory).glob(f"{HOLDOUT_PREFIX}*.yaml")):
        specs.extend(load_registry(path))
    if city:
        specs = [s for s in specs if s.city.lower() == city.lower()]
    return specs


def assert_holdouts_are_held_out(directory: str | Path) -> None:
    """Fail loudly if a holdout id or URL has leaked into the main registry.

    A silent leak does not break anything visible: the crawl succeeds, the recall
    number goes up, and the measurement quietly becomes self-graded. Loud is the
    only safe failure mode here.
    """
    main = load_registries(directory)
    holdouts = load_holdouts(directory)
    if not holdouts:
        return
    held_ids = {s.id for s in holdouts}
    held_urls = {s.url.rstrip("/") for s in holdouts}
    leaked = [
        s.id for s in main if s.id in held_ids or s.url.rstrip("/") in held_urls
    ]
    if leaked:
        raise ValueError(
            "holdout sources leaked into the ingested registry: "
            + ", ".join(sorted(leaked))
            + " -- recall measured against these is self-graded. Remove them "
            "from the main registry, or promote a fresh holdout to replace them."
        )


class SnapshotStore:
    """Content-addressed store of raw fetched payloads.

    Keyed by hash of the content, so an unchanged page costs one directory
    entry rather than another copy. The manifest records which source produced
    which snapshot when, which is what makes a historical re-parse meaningful.
    """

    def __init__(self, root: str | Path = "data/snapshots") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"

    def put(self, source_id: str, content: str, fetched_at: Optional[datetime] = None) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
        blob = self.root / f"{digest}.gz"
        if not blob.exists():
            blob.write_bytes(gzip.compress(content.encode("utf-8")))
        entry = {
            "source_id": source_id,
            "digest": digest,
            "fetched_at": (fetched_at or datetime.now(dt_timezone.utc)).isoformat(),
            "bytes": len(content),
        }
        with self.manifest_path.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
        return digest

    def get(self, digest: str) -> str:
        return gzip.decompress((self.root / f"{digest}.gz").read_bytes()).decode("utf-8")

    def latest_for(self, source_id: str) -> Optional[str]:
        """Digest of the most recent snapshot for a source, if any."""
        if not self.manifest_path.exists():
            return None
        latest: Optional[dict] = None
        for line in self.manifest_path.read_text().splitlines():
            entry = json.loads(line)
            if entry["source_id"] != source_id:
                continue
            if latest is None or entry["fetched_at"] > latest["fetched_at"]:
                latest = entry
        return latest["digest"] if latest else None


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9_\-]+)""", re.I
)
# The signature of UTF-8 read as Latin-1: 'CafÃ©' where 'Café' was meant.
_MOJIBAKE = re.compile(r"Ã[©¨«¯´¶¼½\x80-\xbf]|â€|Â[ ©«°»]|ï¿½")


def decode_payload(content: bytes, content_type: str = "") -> str:
    """Turn bytes into text, in the order the standards say to try.

    Encoding is the single most common cosmetic defect on Dutch venue sites and
    it never raises: UTF-8 bytes decoded as Latin-1 give 'CafÃ© de Wijnhaven',
    which parses fine, stores fine, and is wrong on every screen it reaches.

    Fixed here rather than in extraction because this is the only place the
    bytes still exist. Once a payload has been decoded wrongly the information
    needed to decode it correctly is gone, and everything downstream is reduced
    to guessing which mangled sequences to substitute.

    Order: the HTTP header wins (the server is authoritative about what it
    sent), then the document's own meta charset, then UTF-8, then a Latin-1
    fallback that cannot fail. A decode that succeeds but leaves mojibake
    behind is treated as a failure and retried, because "no exception" is not
    the same as "correct".
    """
    import unicodedata

    candidates: list[str] = []
    if match := re.search(r"charset=([\w\-]+)", content_type or "", re.I):
        candidates.append(match.group(1))
    if meta := _META_CHARSET.search(content[:4096]):
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1252", "latin-1"]

    best: Optional[str] = None
    for encoding in candidates:
        try:
            text = content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if not _MOJIBAKE.search(text):
            best = text
            break
        best = best if best is not None else text

    if best is None:
        best = content.decode("utf-8", "replace")
    # NFC so that "é" written as e+combining-accent compares equal to the
    # precomposed form; otherwise two spellings of one venue never match.
    return unicodedata.normalize("NFC", best)


class Fetcher:
    """HTTP client with conditional GETs and per-source ETag memory.

    Most event pages change rarely. Sending If-None-Match and honouring a 304
    means the majority of a crawl cycle costs a header exchange, which is what
    makes a short crawl cadence affordable in the first place.
    """

    def __init__(
        self,
        store: Optional[SnapshotStore] = None,
        timeout: float = 20.0,
        user_agent: str = "cityfeed/0.1 (+https://github.com/coddiw0mple/cityfeed)",
    ) -> None:
        self.store = store or SnapshotStore()
        self.timeout = timeout
        self.user_agent = user_agent
        self._etags: dict[str, str] = {}
        self._last_modified: dict[str, str] = {}

    async def fetch(self, spec: SourceSpec) -> tuple[Optional[str], bool]:
        """Return (content, changed).

        A 304 yields the cached snapshot with changed=False, so downstream
        stages can skip re-parsing an unchanged source entirely.
        """
        import httpx

        headers = {"User-Agent": self.user_agent, "Accept-Language": f"{spec.locale},en;q=0.8"}
        if etag := self._etags.get(spec.id):
            headers["If-None-Match"] = etag
        if modified := self._last_modified.get(spec.id):
            headers["If-Modified-Since"] = modified

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(spec.url, headers=headers)

        if response.status_code == 304:
            digest = self.store.latest_for(spec.id)
            return (self.store.get(digest) if digest else None, False)

        response.raise_for_status()
        if etag := response.headers.get("etag"):
            self._etags[spec.id] = etag
        if modified := response.headers.get("last-modified"):
            self._last_modified[spec.id] = modified

        # response.text applies httpx's own charset guess; decoding the raw
        # bytes ourselves is what lets the meta charset and the mojibake retry
        # participate in the decision.
        content = decode_payload(response.content, response.headers.get("content-type", ""))
        if spec.type is SourceType.JSONLD_INDEX:
            content = await self._crawl_index(spec, content, str(response.url))
        self.store.put(spec.id, content)
        return content, True

    async def _crawl_index(self, spec: SourceSpec, listing: str, base: str) -> str:
        """Follow a listing page to its detail pages and stitch their JSON-LD.

        A very common shape: the programme page renders cards with no machine
        readable dates, while every detail page behind it carries a complete
        schema.org/Event. The events are tier 0 the whole time; only the index
        is unhelpful.

        The stitched document is what lands in the snapshot store, so a crawl of
        sixty pages replays offline as a single pinned payload and extraction
        stays a pure function of bytes. It also keeps the snapshot small: the
        chrome is discarded and only the ld+json survives.
        """
        import httpx
        from lxml import etree
        from lxml import html as lxml_html

        selector = (spec.selectors or {}).get("link")
        if not selector:
            raise ValueError(f"{spec.id}: jsonld_index source needs a 'link' selector")

        try:
            tree = lxml_html.fromstring(listing)
        except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
            raise ValueError(f"{spec.id}: listing page did not parse") from exc

        origin = urlparse(base)
        links: list[str] = []
        for node in tree.cssselect(selector):
            href = node.get("href")
            if not href:
                continue
            absolute = urljoin(base, href)
            # Same-origin only. A listing page links to ticket vendors, social
            # media and sponsors, and following those turns one registry row
            # into an open-ended crawl of the web.
            if urlparse(absolute).netloc != origin.netloc:
                continue
            if absolute.rstrip("/") == base.rstrip("/"):
                continue
            if absolute not in links:
                links.append(absolute)

        if not links:
            raise ValueError(
                f"{spec.id}: link selector matched no detail pages - markup likely drifted"
            )
        links = links[: spec.max_detail_pages]

        blocks: list[str] = []
        semaphore = asyncio.Semaphore(4)
        headers = {"User-Agent": self.user_agent}

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            async def one(url: str) -> list[str]:
                async with semaphore:
                    # One detail page per second per source: this is somebody's
                    # venue site, not a crawl target.
                    await asyncio.sleep(1.0)
                    try:
                        page = await client.get(url, headers=headers)
                        page.raise_for_status()
                    except Exception:  # noqa: BLE001 - one dead show, not a dead source
                        return []
                try:
                    detail = lxml_html.fromstring(page.text)
                except (etree.ParserError, etree.XMLSyntaxError, ValueError):
                    return []
                return [
                    node.text_content()
                    for node in detail.xpath('//script[@type="application/ld+json"]')
                    if node.text_content() and node.text_content().strip()
                ]

            for found in await asyncio.gather(*(one(u) for u in links)):
                blocks.extend(found)

        scripts = "\n".join(
            f'<script type="application/ld+json">{b}</script>' for b in blocks
        )
        return (
            f"<!doctype html><html><head>\n<!-- cityfeed jsonld_index: "
            f"{len(links)} detail pages, {len(blocks)} ld+json blocks -->\n"
            f"{scripts}\n</head><body></body></html>\n"
        )
