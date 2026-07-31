"""Bake the dashboard from whatever the last crawl wrote.

Produces `public/index.html`: the template with event data inlined and the
window anchored to build time. Self-contained, so it works from a file:// URL,
from a static host, and from Vercel with no backend at all.

Passing --api additionally emits `public/live.html`, the same page in runtime
mode. Both are built from one template on purpose: two copies of the rendering
logic would drift, and the provenance UI is the part worth not breaking.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_events(db: Path, city: str | None) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    where, params = ("WHERE city = ?", [city]) if city else ("", [])
    rows = conn.execute(
        f"SELECT * FROM events {where} ORDER BY start", params
    ).fetchall()
    out = []
    for row in rows:
        members = json.loads(row["members"]) if row["members"] else []
        out.append({
            "id": row["id"],
            "title": row["title"],
            "start": row["start"],
            "end": row["end"],
            "city": row["city"],
            "category": row["category"],
            "is_free": None if row["is_free"] is None else bool(row["is_free"]),
            "price": row["price"],
            "url": row["url"],
            "confidence": row["confidence"],
            "venue": {
                "id": row["venue_id"], "name": row["venue_name"],
                "address": None, "lat": row["venue_lat"], "lon": row["venue_lon"],
            },
            # The dashboard's provenance strip and marker weights read this.
            "sources": [
                {"id": m["source_id"], "trust": m["trust"], "title": m["title"]}
                for m in members
            ],
        })
    conn.close()
    return out


def render(template: str, events: list[dict], start: datetime, api: str | None) -> str:
    html = template.replace("__DATA__", json.dumps(events, ensure_ascii=False))
    # The template ships a fixed demo date; a real build anchors the window to
    # the moment it ran, so "next 30 days" means the next 30 days.
    html = html.replace(
        'const START = new Date("2026-09-07T00:00:00+02:00");',
        f'const START = new Date("{start.isoformat()}");',
    )
    if api:
        html = html.replace(
            "<script>\n/* Two data modes.",
            f'<script>window.CITYFEED_API={json.dumps(api)};</script>\n<script>\n/* Two data modes.',
        )
    return html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "cityfeed.db"))
    parser.add_argument("--city", default="Delft")
    parser.add_argument("--out", default=str(ROOT / "public"))
    parser.add_argument("--api", help="also emit live.html pointed at this API origin")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db} - run `cityfeed run --city {args.city}` first")
        return 1

    template = (ROOT / "dashboard.template.html").read_text()
    events = load_events(db, args.city)
    start = datetime.now(timezone.utc).astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(template, events, start, None))
    print(f"wrote {out / 'index.html'}  ({len(events)} events inlined)")

    if args.api:
        # Empty inline data on purpose: if the fetch is broken, the page is
        # visibly empty rather than quietly serving a stale bake.
        (out / "live.html").write_text(render(template, [], start, args.api))
        print(f"wrote {out / 'live.html'}   (runtime mode -> {args.api})")

    upcoming = sum(1 for e in events if e["start"] >= start.isoformat())
    print(f"{upcoming} upcoming of {len(events)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
