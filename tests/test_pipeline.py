"""Tests run entirely off fixtures, no network.

That is the point of the snapshot store: the corpus is pinned, so a change in
the numbers is caused by a change in the code rather than by the web moving.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cityfeed.dedup import deduplicate, pair_score, title_similarity, venue_similarity
from cityfeed.evaluate import evaluate, load_gold
from cityfeed.extract import ExtractionError, extract
from cityfeed.fetch import load_registries
from cityfeed.models import CanonicalEvent, RawRecord, TrustTier, Venue
from cityfeed.normalize import detect_free, normalize_title, parse_datetime

FIXTURES = Path(__file__).parent / "fixtures"
REGISTRY = Path(__file__).parent.parent / "sources"
AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(scope="module")
def specs():
    """Synthetic specs for the mechanics tests, real specs for the rest.

    The synthetic registry is deliberately not in `sources/`. Its fixtures were
    written to exercise the pipeline, which means any accuracy figure derived
    from them is self-graded; keeping them in the test tree is what stops one
    leaking into a README.
    """
    from cityfeed.fetch import load_registry

    combined = load_registries(REGISTRY) + load_registry(FIXTURES / "sources_synthetic.yaml")
    return {s.id: s for s in combined}


@pytest.fixture(scope="module")
def synthetic():
    from cityfeed.fetch import load_registry

    return {s.id: s for s in load_registry(FIXTURES / "sources_synthetic.yaml")}


def payload(source_id: str) -> str:
    for suffix in (".html", ".ics", ".xml", ".json"):
        path = FIXTURES / f"{source_id}{suffix}"
        if path.exists():
            return path.read_text()
    raise FileNotFoundError(source_id)


# --------------------------------------------------------------------- normalise

def test_normalize_strips_stopwords_and_accents():
    assert normalize_title("De Jazzavond in de Wijnhaven", "nl") == "jazzavond wijnhaven"
    assert normalize_title("Exposición: Madrid Moderno", "es") == "exposicion madrid moderno"


def test_naive_datetimes_localise_to_source_timezone():
    """A Delft venue writing 20:00 means 20:00 in Amsterdam, not UTC."""
    dt = parse_datetime("2026-09-12T20:00:00", "Europe/Amsterdam")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=2)  # CEST


def test_free_detection_returns_none_without_signal():
    """Silence must not be read as 'paid'."""
    assert detect_free("Gratis toegang", locale="nl") is True
    assert detect_free("Entrada libre", locale="es") is True
    assert detect_free("Tickets EUR 12,50", locale="nl") is False
    assert detect_free("Een concert", locale="nl") is None


# ----------------------------------------------------------------------- extract

def test_jsonld_extraction(specs):
    records = extract(payload("delft_gemeente_agenda"), specs["delft_gemeente_agenda"])
    assert len(records) == 2
    jazz = next(r for r in records if "Jazz" in r.title)
    assert jazz.venue.name == "Café de Wijnhaven"
    assert jazz.venue.lat == pytest.approx(52.0116)
    assert jazz.is_free is True
    assert jazz.start.hour == 20


def test_jsonld_handles_graph_wrapper(specs):
    """@graph is common and silently yields zero events if unhandled."""
    records = extract(payload("tudelft_events"), specs["tudelft_events"])
    assert len(records) == 1
    assert records[0].title == "TU Delft Open Day"


def test_ics_preserves_rrule_instead_of_expanding(specs):
    records = extract(payload("speakers_delft_ics"), specs["speakers_delft_ics"])
    assert len(records) == 1
    assert "FREQ=WEEKLY" in records[0].rrule
    assert records[0].is_free is True


def test_rss_uses_publication_dates_only_when_the_registry_opts_in(specs):
    """A feed's pubDate is a publication time, not an event time.

    Regression for the worst bug this pipeline has had. Every Delft venue runs
    a WordPress blog whose feed advertises twenty items; reading their pubDates
    as start times produced twenty events that all "happened" at the moment the
    webmaster hit publish. The failure is invisible from the source-health line
    -- it reports twenty records either way -- so it has to be closed off in the
    parser rather than caught by review.
    """
    spec = specs["delft_op_zondag_rss"]
    assert spec.date_from == "published"
    assert any("Jazz" in r.title for r in extract(payload("delft_op_zondag_rss"), spec))

    strict = spec.model_copy(update={"date_from": "event"})
    assert extract(payload("delft_op_zondag_rss"), strict) == []


def test_wordpress_news_feed_yields_no_events(specs):
    """Real captured payload: a venue's blog feed, which is not a programme."""
    from cityfeed.models import SourceType

    spec = specs["filmhuis_lumen_shows"].model_copy(
        update={"type": SourceType.RSS, "id": "news", "selectors": None}
    )
    assert extract(payload("wordpress_news_feed"), spec) == []


def _feed(items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>Test feed</title>" + items + "</channel></rss>"
    )


def _item(title, link, pubdate):
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>iets</description><pubDate>{pubdate}</pubDate></item>"
    )


def test_rss_items_need_a_second_signal_before_becoming_events(specs):
    """A feed is a list of posts. Dating one does not make it an event.

    Every Delft venue runs a blog whose feed advertises twenty items. Requiring
    an independent signal -- an event-shaped URL, a real clock time, or a venue
    the store already knows -- is what stops that blog becoming twenty events.
    """
    from cityfeed.extract import ExtractContext
    from cityfeed.models import SourceType

    spec = specs["delft_op_zondag_rss"].model_copy(
        update={"type": SourceType.RSS, "date_from": "published"}
    )
    ctx = ExtractContext(known_venues={"café de wijnhaven"})

    doc = _feed(
        # article path: dropped even though it has a clock time
        _item("Raad stemt in", "https://x.test/nieuws/raad", "Mon, 07 Sep 2026 09:12:00 +0200")
        # event path: kept even though the time is midnight
        + _item("Jazzavond", "https://x.test/agenda/jazz", "Sat, 12 Sep 2026 00:00:00 +0200")
        # no path signal, but a real clock time
        + _item("Pubquiz", "https://x.test/p/123", "Sat, 12 Sep 2026 20:30:00 +0200")
        # no path signal, midnight, but names a venue the store knows
        + _item("Optreden in Café de Wijnhaven", "https://x.test/p/9",
                "Sun, 13 Sep 2026 00:00:00 +0200")
        # nothing at all: dropped
        + _item("Iets leuks", "https://x.test/p/7", "Mon, 14 Sep 2026 00:00:00 +0200")
    )
    titles = {r.title for r in extract(doc, spec, context=ctx)}
    assert titles == {"Jazzavond", "Pubquiz", "Optreden in Café de Wijnhaven"}

    # Drops are counted with a reason, never silent: a silent drop is
    # indistinguishable from a source that simply had nothing this week.
    drops = ctx.for_source(spec.id)
    assert drops == {"no event signal (article-shaped)": 2}


def test_rss_drop_accounting_is_per_source(specs):
    from cityfeed.extract import ExtractContext
    from cityfeed.models import SourceType

    ctx = ExtractContext()
    doc = _feed(_item("Column", "https://x.test/column/a", "Mon, 07 Sep 2026 09:00:00 +0200"))
    for source_id in ("feed_a", "feed_b"):
        spec = specs["delft_op_zondag_rss"].model_copy(
            update={"id": source_id, "type": SourceType.RSS, "date_from": "published"}
        )
        assert extract(doc, spec, context=ctx) == []
    assert ctx.for_source("feed_a") == {"no event signal (article-shaped)": 1}
    assert ctx.for_source("feed_b") == {"no event signal (article-shaped)": 1}


# ----------------------------------------------------- real-world regressions

def test_jsonld_events_nested_in_an_itemlist_are_found(specs):
    """indelft.nl publishes its whole programme inside a schema.org ItemList.

    Before the flattener descended into itemListElement this page reported
    "jsonld present, 0 events" -- the single most expensive kind of miss,
    because it looks exactly like a site that has no structured data at all.
    """
    records = extract(payload("indelft_uitagenda"), specs["indelft_uitagenda"])
    assert len(records) == 3
    assert all(r.start.tzinfo is not None for r in records)
    # the payload spells the key "URL", not "url"
    assert all(r.url and r.url.startswith("https://") for r in records)


def test_date_only_start_recovers_its_time_from_door_time(specs):
    """uitagenda.nl ships startDate="2026-08-04" with doorTime="20:30:00".

    Read literally that is an event at midnight, twenty hours from when it
    actually happens. It then fails to match the same event from any other
    source, so it survives dedup as a permanent duplicate and shows the user
    the wrong time. Found by `cityfeed recall`, which could not match a single
    held-out event until this was fixed.
    """
    doc = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Event","name":"Jamsession @ Bebop",
     "startDate":"2026-08-04","doorTime":"20:30:00",
     "endDate":"2026-08-04 02:00:00",
     "location":{"@type":"Place","address":"Delft","name":"Jazz Café de Bebop"}}
    </script></head><body></body></html>"""
    records = extract(doc, specs["indelft_uitagenda"])
    assert len(records) == 1
    assert (records[0].start.hour, records[0].start.minute) == (20, 30)

    # A startDate that already carries a time must be left alone.
    explicit = doc.replace('"startDate":"2026-08-04"', '"startDate":"2026-08-04T19:00:00"')
    assert extract(explicit, specs["indelft_uitagenda"])[0].start.hour == 19

    # No doorTime and no time: stays midnight rather than being invented.
    bare = doc.replace('"doorTime":"20:30:00",', "")
    assert extract(bare, specs["indelft_uitagenda"])[0].start.hour == 0


def test_jsonld_index_extracts_from_stitched_detail_pages(specs):
    """Theater de Veste: nothing on the listing, a full Event on each detail page."""
    listing = extract(payload("theaterdeveste_listing"), specs["theaterdeveste_programma"])
    assert listing == [], "the listing page alone carries no events"

    records = extract(payload("theaterdeveste_detail"), specs["theaterdeveste_programma"])
    assert len(records) == 1
    assert records[0].end > records[0].start
    assert records[0].start.hour == 19


def test_wp_rest_reads_a_wordpress_custom_post_type(specs):
    """Filmhuis Lumen's programme, from a site whose only feed is a blog feed."""
    records = extract(payload("filmhuis_lumen_shows"), specs["filmhuis_lumen_shows"])
    assert len(records) == 3
    for record in records:
        # acf.dag is "20260914": dateutil reads the compact form as a year
        # unless it is expanded, which silently dates everything to the year 20260.
        assert 2020 < record.start.year < 2100
        assert record.start.tzinfo is not None
        assert "&#" not in record.title  # title.rendered is HTML


def test_duplicate_jsonld_nodes_collapse_within_one_source(specs):
    """Same-source records never merge in dedup, so a repeat here reaches the user."""
    doc = payload("theaterdeveste_detail")
    doubled = doc.replace("</head>", doc.split("<head>")[1].split("</head>")[0] + "</head>")
    assert len(extract(doubled, specs["theaterdeveste_programma"])) == 1


def test_wrapper_reads_a_date_from_a_permalink(specs):
    """popdelft.nl renders "zo 2 augustus 2026"; only the href has a parseable date."""
    spec = specs["popdelft_agenda"]
    doc = """<html><body>
      <a class="event-summary" href="/agenda/indorockfestival/date/323/2026-08-02/2026-08-02">
        <div class="event-summary__date">zo 2 augustus 2026
          <span>15:00- 19:00 uur</span></div>
        <div class="event-summary__title">Indo Rock festival</div>
      </a></body></html>"""
    records = extract(doc, spec)
    assert len(records) == 1
    assert records[0].start.date().isoformat() == "2026-08-02"
    # a date with no time defaults to midnight, which is a wrong answer, not a
    # missing one -- the clock time must come off the card
    assert (records[0].start.hour, records[0].start.minute) == (15, 0)


def test_wrapper_replays_cached_selectors_without_a_model(specs):
    records = extract(payload("cafe_wijnhaven_wrapper"), specs["cafe_wijnhaven_wrapper"])
    assert len(records) == 2
    titles = {r.title for r in records}
    assert titles == {"Jazzavond", "Pubquiz"}
    pubquiz = next(r for r in records if r.title == "Pubquiz")
    assert pubquiz.is_free is False


def test_wrapper_fails_loudly_on_markup_drift(specs):
    """A silent zero looks identical to a quiet week. It must raise."""
    with pytest.raises(ExtractionError, match="drifted"):
        extract("<html><body><p>redesigned</p></body></html>",
                specs["cafe_wijnhaven_wrapper"])


def test_madrid_municipal_api(specs):
    records = extract(payload("madrid_ayuntamiento_api"), specs["madrid_ayuntamiento_api"])
    assert len(records) == 2
    concert = next(r for r in records if "Concierto" in r.title)
    assert concert.is_free is True
    assert concert.venue.lat == pytest.approx(40.4153)
    assert concert.start.tzinfo is not None


# ------------------------------------------------------------------------- dedup

def test_title_similarity_tolerates_padding():
    assert title_similarity("Jazzavond", "Jazzavond in de Wijnhaven", "nl") > 0.85


def test_venue_similarity_is_neutral_when_ungeocoded():
    """Missing coordinates must not be punished into never merging."""
    assert venue_similarity(None, Venue(name="x")) == 0.5


def test_same_source_never_merges_with_itself():
    """Two listings on one page is a source bug; hiding it helps nobody."""
    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    a = RawRecord(source_id="s1", source_url="u", trust=TrustTier.VENUE,
                  title="Jazz Night", start=start)
    b = RawRecord(source_id="s1", source_url="u", trust=TrustTier.VENUE,
                  title="Jazz Night", start=start)
    assert pair_score(a, b) == 0.0


def test_three_listings_of_one_event_collapse_to_one(specs):
    records = []
    for source_id in ("delft_gemeente_agenda", "delft_op_zondag_rss",
                      "cafe_wijnhaven_wrapper", "theater_de_veste",
                      "tudelft_events", "speakers_delft_ics"):
        records.extend(extract(payload(source_id), specs[source_id]))

    events = deduplicate(records, city="Delft", locale="nl")
    jazz = [e for e in events if "azz" in e.title]
    assert len(jazz) == 1, f"jazz night did not collapse: {[e.title for e in jazz]}"
    assert len(jazz[0].members) == 3
    # municipal feed outranks the newspaper, so its title wins
    assert jazz[0].title == "Jazzavond in de Wijnhaven"
    # more independent sources agreeing raises confidence
    assert jazz[0].confidence > 1.0 - 1e-9


def test_merge_prefers_geocoded_venue_over_trust_order(specs):
    """Coordinates are objective; a trusted source with no geo shouldn't win."""
    records = []
    for source_id in ("delft_gemeente_agenda", "theater_de_veste"):
        records.extend(extract(payload(source_id), specs[source_id]))
    events = deduplicate(records, city="Delft", locale="nl")
    festival = next(e for e in events if "Chamber" in e.title)
    assert len(festival.members) == 2
    assert festival.venue.lat is not None


def test_distinct_events_are_not_over_merged(specs):
    records = extract(payload("cafe_wijnhaven_wrapper"), specs["cafe_wijnhaven_wrapper"])
    records += extract(payload("delft_gemeente_agenda"), specs["delft_gemeente_agenda"])
    events = deduplicate(records, city="Delft", locale="nl")
    assert any(e.title == "Pubquiz" for e in events)


# ------------------------------------------------- venues, price, occurrences

def test_price_and_rrule_survive_deduplication(synthetic):
    """Both were extracted and then silently dropped in _merge_cluster."""
    records = extract(payload("speakers_delft_ics"), synthetic["speakers_delft_ics"])
    assert records[0].rrule and "FREQ=WEEKLY" in records[0].rrule

    priced = RawRecord(
        source_id="venue_page", source_url="u", trust=TrustTier.VENUE,
        title=records[0].title, start=records[0].start, price="€17,50",
    )
    events = deduplicate(records + [priced], city="Delft", locale="nl")
    merged = next(e for e in events if e.title == records[0].title)
    assert merged.price == "€17,50"
    assert "FREQ=WEEKLY" in merged.rrule


def test_venue_key_collapses_spelling_differences():
    """Sources disagree about accents; one venue must not become two pins."""
    a = Venue(name="Café de Wijnhaven", city="Delft")
    b = Venue(name="Cafe de Wijnhaven", city="Delft")
    assert a.key == b.key
    assert Venue(name="Café de Wijnhaven", city="Madrid").key != a.key


def test_weekly_series_expands_to_dated_occurrences(synthetic):
    """One series row, many occurrence rows — not 13 canonical events."""
    from cityfeed.occurrence import occurrences_for

    records = extract(payload("speakers_delft_ics"), synthetic["speakers_delft_ics"])
    events = deduplicate(records, city="Delft", locale="nl")
    assert len(events) == 1, "a weekly event is one event, not one per week"

    series = events[0]
    now = series.start - timedelta(days=1)
    rows = occurrences_for(series, horizon_days=90, now=now)
    # The feed says COUNT=10, and a 90-day window would otherwise hold ~13.
    # Honouring COUNT rather than filling the window is the difference between
    # reporting a term's worth of sessions and inventing three extra ones.
    assert len(rows) == 10, f"COUNT=10 must bound the expansion, got {len(rows)}"
    assert len({o.id for o in rows}) == len(rows)
    assert all(o.event_id == series.id for o in rows)


def test_unbounded_weekly_rule_fills_the_horizon(synthetic):
    """Without COUNT or UNTIL, the 90-day horizon is what bounds the table."""
    from cityfeed.occurrence import occurrences_for

    event = CanonicalEvent(
        id="e1", title="Jam session", start=datetime(2026, 9, 1, 21, tzinfo=AMS),
        city="Delft", rrule="FREQ=WEEKLY",
    )
    rows = occurrences_for(event, horizon_days=90, now=event.start)
    assert 12 <= len(rows) <= 14, f"got {len(rows)}"


def test_non_recurring_event_gets_exactly_one_occurrence():
    from cityfeed.occurrence import occurrences_for

    event = CanonicalEvent(
        id="e1", title="Pubquiz", start=datetime(2026, 9, 12, 20, tzinfo=AMS),
        city="Delft",
    )
    assert len(occurrences_for(event)) == 1


def test_weekly_event_holds_its_local_time_across_the_dst_change():
    """A 20:00 weekly concert stays at 20:00 local when the clocks go back.

    Expanding in absolute time instead of wall-clock time shifts every date
    after the last Sunday in October by an hour. It looks like a data-entry
    error, it is impossible to explain to a venue, and nothing catches it
    except a test that spans the boundary.
    """
    from cityfeed.occurrence import expand_rrule

    start = datetime(2026, 10, 7, 20, 0, tzinfo=AMS)  # CEST, UTC+2
    dates = expand_rrule(start, "FREQ=WEEKLY", horizon_days=60, now=start)
    assert len(dates) > 4

    offsets = {d.utcoffset() for d in dates}
    assert len(offsets) == 2, "the window must actually cross the DST boundary"
    assert all(d.hour == 20 for d in dates), [d.isoformat() for d in dates]


def test_monthly_rule_skips_short_months_instead_of_inventing_a_date():
    """BYMONTHDAY=31 has no February occurrence. Clamping to the 28th invents one."""
    from cityfeed.occurrence import expand_rrule

    start = datetime(2026, 1, 31, 19, 0, tzinfo=AMS)
    dates = expand_rrule(
        start, "FREQ=MONTHLY;BYMONTHDAY=31", horizon_days=120, now=start
    )
    assert all(d.day == 31 for d in dates)
    assert 2 not in {d.month for d in dates}


def test_exdate_removes_a_cancelled_date():
    from cityfeed.occurrence import expand_rrule

    start = datetime(2026, 9, 1, 20, 0, tzinfo=AMS)
    rule = "RRULE:FREQ=WEEKLY\nEXDATE:20260908T200000"
    dates = expand_rrule(start, rule, horizon_days=30, now=start)
    assert datetime(2026, 9, 8, 20, 0, tzinfo=AMS) not in dates
    assert datetime(2026, 9, 15, 20, 0, tzinfo=AMS) in dates


def test_overriding_one_occurrence_leaves_the_others_alone(tmp_path):
    """Per-date edits are the point of the table; a re-crawl must not revert them."""
    from cityfeed.cli import connect, persist, persist_occurrences

    event = CanonicalEvent(
        id="e1", title="Weekly quiz", start=datetime(2026, 9, 1, 20, tzinfo=AMS),
        city="Delft", price="€5", rrule="FREQ=WEEKLY", venue=Venue(name="Doerak", city="Delft"),
    )
    conn = connect(str(tmp_path / "t.db"))
    persist(conn, [event])
    persist_occurrences(conn, [event])

    target = conn.execute(
        "SELECT id FROM occurrences ORDER BY start LIMIT 1 OFFSET 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE occurrences SET price = ?, is_free = 1, is_override = 1 WHERE id = ?",
        ("free", target),
    )
    conn.commit()

    persist_occurrences(conn, [event])  # re-crawl

    price, override = conn.execute(
        "SELECT price, is_override FROM occurrences WHERE id = ?", (target,)
    ).fetchone()
    assert (price, override) == ("free", 1), "override was reverted by a re-crawl"
    others = conn.execute(
        "SELECT DISTINCT price FROM occurrences WHERE id != ?", (target,)
    ).fetchall()
    assert others == [("€5",)]


def test_venues_table_is_populated_and_survives_a_geocode(tmp_path):
    """The venues table is also B3's cache: a crawl must not wipe coordinates."""
    from cityfeed.cli import connect, persist

    event = CanonicalEvent(
        id="e1", title="Concert", start=datetime(2026, 9, 1, 20, tzinfo=AMS),
        city="Delft", venue=Venue(name="Theater de Veste", city="Delft"),
    )
    conn = connect(str(tmp_path / "t.db"))
    persist(conn, [event])
    conn.execute(
        "UPDATE venues SET lat = 52.0104, lon = 4.3595, geocode_source = 'pdok'"
    )
    conn.commit()

    persist(conn, [event])  # re-crawl with an ungeocoded venue
    lat, source = conn.execute("SELECT lat, geocode_source FROM venues").fetchone()
    assert (lat, source) == (52.0104, "pdok")


# ---------------------------------------------------------------------- evaluate

def test_end_to_end_quality_report(synthetic):
    """Scored against the synthetic corpus, which grades mechanics only.

    These numbers say the matcher and the merge work. They are not a coverage
    claim about Delft and must never be quoted as one -- that is what
    `cityfeed recall` against the holdout registry is for.
    """
    records = []
    for spec in synthetic.values():
        if spec.city != "Delft":
            continue
        records.extend(extract(payload(spec.id), spec))

    events = deduplicate(records, city="Delft", locale="nl")
    gold = load_gold(FIXTURES / "gold_delft.json")
    report = evaluate(events, gold, locale="nl")

    assert report.recall >= 0.8, report.render()
    assert report.precision >= 0.5, report.render()
    assert report.fields["start_time"].accuracy >= 0.8, report.render()
    # per-source recall must be populated, that is the whole point of the harness
    assert report.per_source_recall


# ---------------------------------------------------------------------- category

def test_categorisation_is_locale_aware():
    from cityfeed.categorize import categorize

    assert categorize("Jazzavond in de Wijnhaven") == "music"
    assert categorize("Concierto de verano en el Retiro") == "music"
    assert categorize("Exposición: Madrid Moderno") == "art"
    assert categorize("Pubquiz") == "nightlife"
    assert categorize("TU Delft Open Day") == "academic"


def test_venue_words_weigh_less_than_title_words():
    """A lecture in a theatre is a lecture, not theatre."""
    from cityfeed.categorize import categorize

    assert categorize("Lezing over stadsplanning", venue="Theater de Veste") == "academic"


def test_categorisation_returns_none_rather_than_guessing():
    from cityfeed.categorize import categorize

    assert categorize("Iets leuks op donderdag") is None
    assert categorize(None) is None


def test_pipeline_assigns_categories(synthetic):
    records = []
    for spec in synthetic.values():
        if spec.city == "Delft":
            records.extend(extract(payload(spec.id), spec))
    events = deduplicate(records, city="Delft", locale="nl")
    by_title = {e.title: e.category for e in events}
    assert by_title["Jazzavond in de Wijnhaven"] == "music"
    assert by_title["TU Delft Open Day"] == "academic"


# ---------------------------------------------------------------- encoding

def test_mis_encoded_page_is_decoded_correctly_at_fetch_time(specs):
    """UTF-8 bytes with a server claiming iso-8859-1 — the Dutch venue classic.

    Nothing raises on the wrong decode: 'Café' becomes 'CafÃ©', parses fine,
    stores fine, and is wrong on every screen it reaches. It has to be caught
    where the bytes still exist, because after a bad decode the information
    needed to do it right is gone.
    """
    from cityfeed.fetch import decode_payload

    raw = (FIXTURES / "mis_encoded_venue_page.html.utf8bytes").read_bytes()

    # The naive read, which is what a mislabelled response gives you.
    assert "CafÃ©" in raw.decode("latin-1")

    text = decode_payload(raw, "text/html; charset=iso-8859-1")
    assert "Café de Wijnhaven" in text
    assert "CafÃ©" not in text
    assert "septémber" in text

    records = extract(text, specs["indelft_uitagenda"])
    assert len(records) == 1
    assert records[0].title == "Jazzavond in Café de Wijnhaven"
    assert records[0].venue.name == "Café de Wijnhaven"
    assert records[0].is_free is True


def test_decode_prefers_a_correct_read_over_a_merely_successful_one():
    """latin-1 never raises, so 'it decoded' is not evidence it decoded right."""
    from cityfeed.fetch import decode_payload

    utf8 = "Café".encode("utf-8")
    assert decode_payload(utf8, "text/html; charset=iso-8859-1") == "Café"
    # A genuine cp1252 page must still come back intact.
    assert decode_payload("Café".encode("cp1252"), "text/html; charset=windows-1252") == "Café"


def test_decode_normalises_to_nfc():
    """Two spellings of one venue must compare equal."""
    from cityfeed.fetch import decode_payload

    decomposed = "Café".encode("utf-8")   # e + combining acute
    assert decode_payload(decomposed, "") == "Café"


# ------------------------------------------------------- repeat collapsing

def test_repeated_screenings_collapse_into_one_series(specs):
    """A cinema publishes one row per showing; a reader means one film.

    100 rows from one cinema was 40% of the whole corpus and distorted every
    rate computed over it. Collapsing keeps every showing as a date while
    making the event count mean what a person means by it.
    """
    from cityfeed.occurrence import collapse_repeats, expand_rrule

    spec = specs["filmhuis_lumen_shows"]
    assert spec.collapse_repeats is True, "registry must opt this source in"

    start = datetime(2026, 9, 12, 14, 0, tzinfo=AMS)
    showings = [
        RawRecord(source_id="cinema", source_url="u", trust=TrustTier.VENUE,
                  title="The Odyssey", start=start + timedelta(hours=h),
                  venue=Venue(name="Filmhuis Lumen", city="Delft"))
        for h in (0, 3, 6)
    ]
    other = RawRecord(source_id="cinema", source_url="u", trust=TrustTier.VENUE,
                      title="Blow-Up", start=start,
                      venue=Venue(name="Filmhuis Lumen", city="Delft"))

    collapsed = collapse_repeats(showings + [other], spec)
    assert len(collapsed) == 2, "three showings of one film are one film"

    odyssey = next(r for r in collapsed if r.title == "The Odyssey")
    assert odyssey.start == start, "the series keeps the earliest showing"

    # Every showing survives as a date -- collapsing must not lose any.
    dates = expand_rrule(odyssey.start, odyssey.rrule, horizon_days=90, now=start)
    assert sorted(dates) == sorted(r.start for r in showings)


def test_collapsing_is_opt_in_per_source(specs):
    """Two performances of one play in a day are two things you can book."""
    from cityfeed.occurrence import collapse_repeats

    theatre = specs["theaterdeveste_programma"]
    assert theatre.collapse_repeats is False

    start = datetime(2026, 9, 12, 14, 0, tzinfo=AMS)
    matinee_and_evening = [
        RawRecord(source_id="theatre", source_url="u", trust=TrustTier.VENUE,
                  title="Hamlet", start=start + timedelta(hours=h),
                  venue=Venue(name="Theater de Veste", city="Delft"))
        for h in (0, 6)
    ]
    assert len(collapse_repeats(matinee_and_evening, theatre)) == 2


def test_same_title_at_different_venues_does_not_collapse(specs):
    """One film at two cinemas is two programmes a reader chooses between."""
    from cityfeed.occurrence import collapse_repeats

    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    records = [
        RawRecord(source_id="c", source_url="u", trust=TrustTier.VENUE,
                  title="The Odyssey", start=start, venue=Venue(name=v, city="Delft"))
        for v in ("Filmhuis Lumen", "Pathé Delft")
    ]
    assert len(collapse_repeats(records, specs["filmhuis_lumen_shows"])) == 2


def test_html_is_stripped_from_free_text_fields(specs):
    """ICS DESCRIPTION routinely carries markup, and it renders literally.

    Found by `cityfeed audit` only after descriptions began being persisted:
    115 of 233 events carried raw <br> and <a href> in their description. It
    crashed nothing and was visible on every card.
    """
    from cityfeed.normalize import strip_html

    assert strip_html("A<br><br>Category: Social.<br><a href='x'>View</a>") == \
        "A Category: Social. View"
    assert strip_html("Jazz &amp; Blues") == "Jazz & Blues"
    # Plain text must come back byte-identical, not merely equivalent.
    assert strip_html("plain text, untouched") == "plain text, untouched"
    assert strip_html(None) is None
    # Block tags become spaces so words do not fuse together.
    assert "onetwo" not in strip_html("one<br>two")


def test_ics_descriptions_arrive_without_markup(specs):
    doc = payload("speakers_delft_ics").replace(
        "DESCRIPTION:", "DESCRIPTION:Live jazz<br><a href=\"http://x\">tickets</a> "
    )
    records = extract(doc, specs["speakers_delft_ics"])
    assert records
    assert "<" not in (records[0].description or "")


def test_a_date_only_listing_still_merges_with_a_timed_one():
    """Midnight means "no time published", not "starts at 00:00".

    popdelft lists a festival as "wo 19 augustus 2026" with no clock time.
    Read literally that is 19 hours from the same festival at the theatre, far
    enough to veto every merge, and the user sees it twice. Scoring an unknown
    time as neutral is the same treatment a missing venue already gets.
    """
    from cityfeed.dedup import time_similarity

    day = datetime(2026, 8, 22, tzinfo=AMS)
    assert time_similarity(day, day.replace(hour=19)) == 0.5
    # Different days still score zero: unknown time is not unknown date.
    assert time_similarity(day, day.replace(day=23, hour=19)) == 0.0
    # Two real times are unaffected by the rule.
    assert time_similarity(day.replace(hour=20), day.replace(hour=20)) == 1.0
    assert time_similarity(day.replace(hour=20), day.replace(hour=23)) == 0.0

    records = [
        RawRecord(source_id="popdelft", source_url="u", trust=TrustTier.VENUE,
                  title="Delft Jazz", start=day),
        RawRecord(source_id="veste", source_url="u", trust=TrustTier.VENUE,
                  title="Delft Jazz", start=day.replace(hour=19)),
    ]
    assert len(deduplicate(records, city="Delft", locale="nl")) == 1


def test_neutral_time_cannot_carry_a_merge_on_its_own():
    """The unknown-time score must not become a licence to merge anything."""
    day = datetime(2026, 8, 22, tzinfo=AMS)
    records = [
        RawRecord(source_id="a", source_url="u", trust=TrustTier.VENUE,
                  title="Kinderworkshop pottenbakken", start=day),
        RawRecord(source_id="b", source_url="u", trust=TrustTier.VENUE,
                  title="Death metal avond", start=day.replace(hour=21)),
    ]
    assert len(deduplicate(records, city="Delft", locale="nl")) == 2


# ------------------------------------------------- withdrawal of stale events

def test_events_are_withdrawn_when_their_source_stops_listing_them(tmp_path):
    """A cancelled or pulled event must not live forever.

    Without this the store only ever grows: an event removed from its source
    stays queryable indefinitely, which for a *live events* product is the
    worst kind of wrong — confidently listing something that isn't happening.
    """
    from cityfeed.cli import connect, persist, withdraw_unseen

    def evt(eid, title):
        return CanonicalEvent(
            id=eid, title=title, start=datetime(2026, 9, 1, 20, tzinfo=AMS),
            city="Delft", venue=Venue(name="Bebop", city="Delft"),
            members=[RawRecord(source_id="venue_site", source_url="u",
                               trust=TrustTier.VENUE, title=title,
                               start=datetime(2026, 9, 1, 20, tzinfo=AMS))],
        )

    conn = connect(str(tmp_path / "w.db"))
    persist(conn, [evt("a", "Still on"), evt("b", "Cancelled")], seen_at="2026-08-01T00:00:00")

    # Next crawl: the source succeeds but only lists 'a'.
    persist(conn, [evt("a", "Still on")], seen_at="2026-08-02T00:00:00")
    assert withdraw_unseen(conn, "Delft", "2026-08-02T00:00:00", {"venue_site"}) == 1

    live = {r[0] for r in conn.execute("SELECT id FROM events WHERE withdrawn_at IS NULL")}
    assert live == {"a"}
    # Soft delete: the row survives for audit.
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_a_failed_source_withdraws_nothing(tmp_path):
    """Absence of evidence is not evidence.

    If a source 403s, its events are not gone — we just did not get to look.
    Withdrawing on a failed fetch would empty the database the first time an
    aggregator rate-limited the crawler.
    """
    from cityfeed.cli import connect, persist, withdraw_unseen

    event = CanonicalEvent(
        id="a", title="Concert", start=datetime(2026, 9, 1, 20, tzinfo=AMS), city="Delft",
        members=[RawRecord(source_id="flaky", source_url="u", trust=TrustTier.VENUE,
                           title="Concert", start=datetime(2026, 9, 1, 20, tzinfo=AMS))],
    )
    conn = connect(str(tmp_path / "w.db"))
    persist(conn, [event], seen_at="2026-08-01T00:00:00")

    # A later run where 'flaky' did not succeed: nothing may be retired.
    assert withdraw_unseen(conn, "Delft", "2026-08-02T00:00:00", succeeded=set()) == 0
    assert withdraw_unseen(conn, "Delft", "2026-08-02T00:00:00", succeeded={"other"}) == 0
    assert conn.execute(
        "SELECT count(*) FROM events WHERE withdrawn_at IS NULL"
    ).fetchone()[0] == 1


def test_a_returning_event_is_un_withdrawn(tmp_path):
    """Sources drop things temporarily; coming back must restore the listing."""
    from cityfeed.cli import connect, persist, withdraw_unseen

    event = CanonicalEvent(
        id="a", title="Concert", start=datetime(2026, 9, 1, 20, tzinfo=AMS), city="Delft",
        members=[RawRecord(source_id="venue_site", source_url="u", trust=TrustTier.VENUE,
                           title="Concert", start=datetime(2026, 9, 1, 20, tzinfo=AMS))],
    )
    conn = connect(str(tmp_path / "w.db"))
    persist(conn, [event], seen_at="2026-08-01T00:00:00")
    withdraw_unseen(conn, "Delft", "2026-08-02T00:00:00", {"venue_site"})
    assert conn.execute("SELECT withdrawn_at FROM events").fetchone()[0] is not None

    persist(conn, [event], seen_at="2026-08-03T00:00:00")
    assert conn.execute("SELECT withdrawn_at FROM events").fetchone()[0] is None


# --------------------------------------- field provenance and revision history

def _rec(source, trust, **kw):
    base = dict(source_url="u", title="Jazz Night",
                start=datetime(2026, 9, 12, 20, tzinfo=AMS))
    base.update(kw)
    return RawRecord(source_id=source, trust=trust, **base)


def test_provenance_records_which_source_won_each_field(tmp_path):
    """`members` explains a merge; provenance explains a *field*.

    The merge picks per field by trust and then discards which record supplied
    the winning value, which loses the only question anyone asks about a merged
    record: not "why did these merge" but "why does it say this".
    """
    from cityfeed.cli import connect
    from cityfeed.provenance import load_provenance, record_provenance

    venue = _rec("venue_site", TrustTier.VENUE, price="€17,50")
    paper = _rec("paper", TrustTier.EDITORIAL, title="Jazz Night at the Wijnhaven",
                 description="a long write-up")
    event = deduplicate([venue, paper], city="Delft", locale="nl")[0]

    conn = connect(str(tmp_path / "p.db"))
    fields, changed = record_provenance(
        conn, [event], {"venue_site": "jsonld", "paper": "rss"}
    )
    assert fields > 0 and changed == 0, "first sighting is a baseline, not a change"

    prov = load_provenance(conn, event.id)
    # The venue's price wins because nobody else stated one.
    assert prov["price"]["source_id"] == "venue_site"
    assert prov["price"]["tier"] == "jsonld"
    # The description only the paper carried is attributed to the paper.
    assert prov["description"]["source_id"] == "paper"


def test_confidence_is_derived_from_evidence_not_asserted(tmp_path):
    """A self-reported score is an opinion; this one is checkable.

    A startDate out of JSON-LD is a machine-readable claim by the venue. The
    same date scraped off a permalink by regex is an inference about their URL
    scheme. Those must not score the same.
    """
    from cityfeed.provenance import FieldOrigin

    jsonld = FieldOrigin("start", "x", "a", trust=3, tier="jsonld")
    wrapper = FieldOrigin("start", "x", "b", trust=3, tier="wrapper")
    assert jsonld.confidence > wrapper.confidence

    # Independent agreement helps, with diminishing returns.
    alone = FieldOrigin("start", "x", "a", trust=3, tier="jsonld", agreeing=1)
    pair = FieldOrigin("start", "x", "a", trust=3, tier="jsonld", agreeing=2)
    crowd = FieldOrigin("start", "x", "a", trust=3, tier="jsonld", agreeing=6)
    assert alone.confidence < pair.confidence <= crowd.confidence
    assert crowd.confidence - pair.confidence < pair.confidence - alone.confidence

    # Disagreement is a real signal that somebody is wrong.
    disputed = FieldOrigin("start", "x", "a", trust=3, tier="jsonld", agreeing=2, dissenting=2)
    assert disputed.confidence < pair.confidence

    # Corroboration can never certify a value the extractor had to guess at.
    assert FieldOrigin("start", "x", "a", trust=1, tier="rss", agreeing=6).confidence < 0.8
    # And a missing value is not confident, it is absent.
    assert FieldOrigin("price", None, "a", trust=1, tier="ics").confidence == 0.0


def test_a_changed_start_time_is_kept_as_history_not_overwritten(tmp_path):
    """"Doors moved 19:00 to 20:00" is exactly what an UPDATE destroys."""
    from cityfeed.cli import connect
    from cityfeed.provenance import load_history, record_provenance

    def at(hour):
        rec = _rec("venue_site", TrustTier.VENUE,
                   start=datetime(2026, 9, 12, hour, tzinfo=AMS))
        return deduplicate([rec], city="Delft", locale="nl")[0]

    conn = connect(str(tmp_path / "p.db"))
    first = at(19)
    record_provenance(conn, [first], {"venue_site": "jsonld"}, now="2026-08-01T00:00:00")
    assert load_history(conn, first.id) == [], "a baseline is not a change"

    # Same event id: the venue moved the time, it did not announce a new show.
    moved = at(20)
    moved.id = first.id
    _, changed = record_provenance(
        conn, [moved], {"venue_site": "jsonld"}, now="2026-08-02T00:00:00"
    )
    assert changed == 1

    history = load_history(conn, first.id)
    assert len(history) == 1
    assert history[0]["field"] == "start"
    assert "19:00" in history[0]["from"] and "20:00" in history[0]["to"]


def test_recrawling_an_unchanged_event_writes_no_history(tmp_path):
    """The table is a log of real changes, not a log of crawls."""
    from cityfeed.cli import connect
    from cityfeed.provenance import load_history, record_provenance

    event = deduplicate([_rec("venue_site", TrustTier.VENUE)], city="Delft", locale="nl")[0]
    conn = connect(str(tmp_path / "p.db"))
    tiers = {"venue_site": "jsonld"}
    for day in ("01", "02", "03"):
        record_provenance(conn, [event], tiers, now=f"2026-08-{day}T00:00:00")
    assert load_history(conn, event.id) == []
