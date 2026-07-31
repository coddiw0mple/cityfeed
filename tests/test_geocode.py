"""Geocoding tests against recorded provider responses.

No network. The provider payloads are the real shapes PDOK and Nominatim
return, so the parsing is genuinely exercised while the assertions stay stable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from cityfeed.cli import connect
from cityfeed.geocode import (
    Geocoder,
    NominatimProvider,
    PDOKProvider,
    apply_to_records,
    in_bbox,
    query_ladder,
)
from cityfeed.models import RawRecord, TrustTier, Venue

FIXTURES = Path(__file__).parent / "fixtures"
AMS = ZoneInfo("Europe/Amsterdam")


def _responses() -> dict:
    return json.loads((FIXTURES / "geocode_responses.json").read_text())


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- units

def test_bbox_rejects_a_same_named_street_in_another_province():
    """The check that turns a confidently wrong pin into an honest null."""
    assert in_bbox(52.0116, 4.3571, "Delft") is True      # Markt, Delft
    assert in_bbox(52.0907, 5.1214, "Delft") is False     # Utrecht
    assert in_bbox(40.4153, -3.7025, "Delft") is False    # Madrid
    assert in_bbox(40.4153, -3.7025, "Madrid") is True


def test_bbox_accepts_when_no_box_is_configured():
    """An unknown city must not silently reject every result."""
    assert in_bbox(1.0, 1.0, "Timbuktu") is True


def test_query_ladder_runs_specific_to_general():
    assert query_ladder("Theater de Veste", "Vesteplein 1", "Delft") == [
        "Theater de Veste, Vesteplein 1, Delft",
        "Theater de Veste, Delft",
        "Vesteplein 1, Delft",
    ]
    assert query_ladder("Bebop", None, "Delft") == ["Bebop, Delft"]
    assert query_ladder("", None, "Delft") == []


def test_pdok_reads_wkt_in_lon_lat_order():
    """centroide_ll is POINT(lon lat). Swapping them moves NL into the ocean."""
    recorded = _responses()["pdok_veste"]

    def handler(request):
        return httpx.Response(200, json=recorded)

    result = _run(PDOKProvider().lookup(_client(handler), "Theater de Veste, Delft"))
    assert result is not None
    assert result.lat == pytest.approx(52.00853, abs=1e-4)
    assert result.lon == pytest.approx(4.36283, abs=1e-4)
    assert result.source == "pdok"
    # The order check that matters: read lat/lon the wrong way round and this
    # Delft address lands at 4.3N 52E, in the Gulf of Guinea.
    assert in_bbox(result.lat, result.lon, "Delft")
    assert not in_bbox(result.lon, result.lat, "Delft")


def test_pdok_returns_none_on_empty_docs():
    def handler(request):
        return httpx.Response(200, json={"response": {"docs": []}})

    assert _run(PDOKProvider().lookup(_client(handler), "nowhere")) is None


def test_provider_failure_is_not_an_exception():
    """A provider outage must degrade to 'unresolved', not take the crawl down."""
    def handler(request):
        return httpx.Response(500, text="upstream is down")

    assert _run(PDOKProvider().lookup(_client(handler), "x")) is None
    assert _run(NominatimProvider().lookup(_client(handler), "x")) is None


def test_nominatim_parses_its_string_coordinates():
    recorded = _responses()["nominatim_lumen"]

    def handler(request):
        return httpx.Response(200, json=recorded)

    result = _run(NominatimProvider().lookup(_client(handler), "Filmhuis Lumen, Delft"))
    assert result is not None
    assert result.lat == pytest.approx(52.0075, abs=1e-2)
    assert result.source == "nominatim"


# ------------------------------------------------------------------ the chain

def test_falls_back_to_nominatim_when_pdok_has_nothing(tmp_path):
    recorded = _responses()
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.host)
        if "pdok" in request.url.host:
            return httpx.Response(200, json={"response": {"docs": []}})
        return httpx.Response(200, json=recorded["nominatim_lumen"])

    conn = connect(str(tmp_path / "g.db"))
    geocoder = Geocoder(conn, providers=[PDOKProvider(), NominatimProvider()],
                        client=_client(handler))
    result = _run(geocoder.resolve(Venue(name="Filmhuis Lumen", city="Delft"), "Delft"))

    assert result is not None and result.source == "nominatim"
    assert "api.pdok.nl" in seen, "PDOK must be tried first"


def test_out_of_box_result_is_rejected_and_the_chain_continues(tmp_path):
    """PDOK answers with the wrong province; Nominatim's in-city answer wins."""
    recorded = _responses()

    def handler(request):
        if "pdok" in request.url.host:
            return httpx.Response(200, json=recorded["pdok_wrong_province"])
        return httpx.Response(200, json=recorded["nominatim_lumen"])

    conn = connect(str(tmp_path / "g.db"))
    geocoder = Geocoder(conn, providers=[PDOKProvider(), NominatimProvider()],
                        client=_client(handler))
    result = _run(geocoder.resolve(Venue(name="Bacchus", city="Delft"), "Delft"))

    assert result is not None
    assert result.source == "nominatim"
    assert in_bbox(result.lat, result.lon, "Delft")


def test_everything_out_of_box_resolves_to_nothing(tmp_path):
    recorded = _responses()

    def handler(request):
        if "pdok" in request.url.host:
            return httpx.Response(200, json=recorded["pdok_wrong_province"])
        return httpx.Response(200, json=recorded["nominatim_wrong_country"])

    conn = connect(str(tmp_path / "g.db"))
    geocoder = Geocoder(conn, providers=[PDOKProvider(), NominatimProvider()],
                        client=_client(handler))
    assert _run(geocoder.resolve(Venue(name="Bacchus", city="Delft"), "Delft")) is None


# ----------------------------------------------------------------- the cache

def test_second_run_makes_zero_network_calls(tmp_path):
    """The acceptance criterion: a venue is geocoded once, ever."""
    recorded = _responses()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=recorded["pdok_veste"])

    conn = connect(str(tmp_path / "g.db"))
    venues = [Venue(name="Theater de Veste", city="Delft", address="Vesteplein 1")]

    first = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler))
    assert _run(first.resolve_all(venues, "Delft"))
    assert calls["n"] > 0
    after_first = calls["n"]

    second = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler))
    resolved = _run(second.resolve_all(venues, "Delft"))
    assert calls["n"] == after_first, "the second run hit the network"
    assert second.calls == 0
    assert resolved[venues[0].key] is not None


def test_a_failed_lookup_is_remembered_so_it_is_not_retried_forever(tmp_path):
    """Unresolvable room names must not cost a lookup on every single crawl."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"response": {"docs": []}})

    conn = connect(str(tmp_path / "g.db"))
    # A name with no override and no chance of resolving.
    venues = [Venue(name="Vergaderhok Q7", city="Delft")]

    _run(Geocoder(conn, providers=[PDOKProvider()], client=_client(handler),
                  overrides_dir=None).resolve_all(venues, "Delft"))
    after_first = calls["n"]
    assert after_first > 0

    second = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler),
                      overrides_dir=None)
    assert _run(second.resolve_all(venues, "Delft")) == {venues[0].key: None}
    assert calls["n"] == after_first, "a known-unresolvable venue was retried"


def test_refresh_forces_a_re_resolution(tmp_path):
    recorded = _responses()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=recorded["pdok_veste"])

    conn = connect(str(tmp_path / "g.db"))
    venues = [Venue(name="Theater de Veste", city="Delft")]
    _run(Geocoder(conn, providers=[PDOKProvider()], client=_client(handler)).resolve_all(venues, "Delft"))
    before = calls["n"]

    _run(Geocoder(conn, providers=[PDOKProvider()], client=_client(handler))
         .resolve_all(venues, "Delft", refresh=True))
    assert calls["n"] > before


def test_coordinates_reach_records_before_dedup(tmp_path):
    """Two spellings of one venue must merge on geometry, not on spelling."""
    from cityfeed.dedup import deduplicate

    recorded = _responses()

    def handler(request):
        return httpx.Response(200, json=recorded["pdok_veste"])

    start = datetime(2026, 9, 12, 20, tzinfo=AMS)
    records = [
        RawRecord(source_id="venue", source_url="u", trust=TrustTier.VENUE,
                  title="Lindy Hop Swing", start=start,
                  venue=Venue(name="Theater de Veste", city="Delft")),
        RawRecord(source_id="aggregator", source_url="u", trust=TrustTier.AGGREGATOR,
                  title="Lindy Hop Swing", start=start,
                  venue=Venue(name="Theater de Veste", city="Delft")),
    ]
    conn = connect(str(tmp_path / "g.db"))
    geocoder = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler))
    resolved = _run(geocoder.resolve_all([r.venue for r in records], "Delft"))
    stamped = apply_to_records(records, resolved)

    assert stamped == 2
    assert all(r.venue.lat is not None for r in records)
    assert all(r.venue.geocode_source == "pdok" for r in records)

    events = deduplicate(records, city="Delft", locale="nl")
    assert len(events) == 1
    assert events[0].venue.lat is not None


# --------------------------------------------- name cleaning and overrides

def test_venue_names_are_cleaned_before_querying():
    """Site decoration turns a resolvable query into an unresolvable one."""
    from cityfeed.geocode import clean_venue_name

    assert clean_venue_name("Café Bacchus | Delft", "Delft") == "Café Bacchus"
    assert clean_venue_name("Theater de Veste - Officiële site", "Delft") == "Theater de Veste"
    assert clean_venue_name("Filmhuis Lumen (Delft)", "Delft") == "Filmhuis Lumen"
    # A name with nothing to strip is returned untouched.
    assert clean_venue_name("Jazzcafé Bebop", "Delft") == "Jazzcafé Bebop"
    # Stripping must never empty the name.
    assert clean_venue_name("Delft", "Delft") == "Delft"


def test_generic_names_are_not_queried():
    """'de kerk' resolves to a church somewhere. Spending a call on it is worse
    than admitting we do not know which one."""
    from cityfeed.geocode import is_generic_name, query_ladder

    assert is_generic_name("de kerk")
    assert is_generic_name("het centrum")
    assert not is_generic_name("Oude Kerk Delft")
    assert query_ladder("de kerk", None, "Delft") == []
    # An address still gets tried even when the name is useless.
    assert query_ladder("de kerk", "Markt 1", "Delft") == ["Markt 1, Delft"]


def test_manual_override_wins_without_a_network_call(tmp_path):
    """Ten hand-written points is a reasonable thing to own."""
    from cityfeed.geocode import Geocoder

    overrides = tmp_path / "reg"
    overrides.mkdir()
    (overrides / "venue_overrides.yaml").write_text(
        "venues:\n  - name: Snijderszaal\n    lat: 51.99884\n    lon: 4.37366\n"
    )

    def handler(request):
        raise AssertionError("an override must not reach the network")

    conn = connect(str(tmp_path / "g.db"))
    g = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler),
                 overrides_dir=str(overrides))
    # Normalisation absorbs case and decoration, not a different spelling --
    # which is why the real overrides file lists both spellings explicitly.
    result = _run(g.resolve(Venue(name="SNIJDERSZAAL | Delft", city="Delft"), "Delft"))
    assert result is not None
    assert result.source == "override"
    assert g.calls == 0


def test_resolution_attempts_are_logged_for_diagnosis(tmp_path):
    """An unresolved venue should be a diagnosis, not a shrug."""
    from cityfeed.geocode import Geocoder

    recorded = _responses()

    def handler(request):
        return httpx.Response(200, json=recorded["pdok_wrong_province"])

    conn = connect(str(tmp_path / "g.db"))
    g = Geocoder(conn, providers=[PDOKProvider()], client=_client(handler),
                 overrides_dir=None)
    assert _run(g.resolve(Venue(name="Bacchus", city="Delft"), "Delft")) is None

    log = g.attempts["Bacchus"]
    assert any("outside Delft" in line for line in log), log
