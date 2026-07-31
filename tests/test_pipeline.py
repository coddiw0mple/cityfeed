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
