"""Build a demo dataset for the dashboard.

The listings here are synthetic, but they are not hand-written output: every
record is pushed through the real `deduplicate()` and `categorize()` path, so
the categories, merges, source counts and confidence values in the dashboard
are produced by the pipeline rather than typed into a JSON file.

Multi-source events are generated with the title drift you actually see in the
wild: the newspaper pads the headline, the venue abbreviates, the municipal
feed is formal. That drift is what the dedup logic has to survive.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cityfeed.dedup import deduplicate
from cityfeed.models import RawRecord, TrustTier, Venue

random.seed(20260729)

AMS = ZoneInfo("Europe/Amsterdam")
MAD = ZoneInfo("Europe/Madrid")
BASE = datetime(2026, 9, 7, tzinfo=AMS)

DELFT_VENUES = {
    "Theater de Veste": (52.0098, 4.3573, "Vesteplein 1"),
    "Aula Congrescentrum TU Delft": (51.9989, 4.3736, "Mekelweg 5"),
    "Café de Wijnhaven": (52.0116, 4.3571, "Wijnhaven 3"),
    "Nieuwe Kerk": (52.0116, 4.3596, "Markt 80"),
    "Museum Prinsenhof": (52.0127, 4.3552, "Sint Agathaplein 1"),
    "Filmhuis Lumen": (52.0089, 4.3585, "Doelenplein 5"),
    "Speakers Delft": (52.0104, 4.3563, "Burgwal 45"),
    "Markt Delft": (52.0117, 4.3592, "Markt"),
    "Sportcentrum TU Delft": (51.9995, 4.3706, "Mekelweg 8"),
    "Bebop Jazzcafé": (52.0110, 4.3565, "Kromstraat 33"),
    "X TU Delft": (51.9997, 4.3720, "Mekelweg 10"),
    "Doerak": (52.0122, 4.3583, "Voldersgracht 20"),
}

MADRID_VENUES = {
    "Parque del Retiro": (40.4153, -3.6844, "Plaza de la Independencia 7"),
    "CentroCentro": (40.4190, -3.6934, "Plaza de Cibeles 1"),
    "Teatro Real": (40.4180, -3.7108, "Plaza de Isabel II"),
    "Matadero Madrid": (40.3925, -3.6975, "Paseo de la Chopera 14"),
    "Museo Reina Sofía": (40.4079, -3.6946, "Calle de Santa Isabel 52"),
    "Círculo de Bellas Artes": (40.4185, -3.6960, "Calle de Alcalá 42"),
    "Mercado de San Miguel": (40.4153, -3.7090, "Plaza de San Miguel"),
    "Sala El Sol": (40.4194, -3.7003, "Calle de los Jardines 3"),
    "Cine Doré": (40.4118, -3.6994, "Calle de Santa Isabel 3"),
    "Conde Duque": (40.4272, -3.7106, "Calle del Conde Duque 11"),
}

# (title, venue, day offset, hour, free, description)
DELFT = [
    ("Jazzavond met het Delft Trio", "Bebop Jazzcafé", 0, 20, True, "Maandelijkse livemuziek met lokale musici. Gratis toegang."),
    ("Delft Chamber Music Festival: Openingsconcert", "Theater de Veste", 1, 19, False, "Kamermuziek van het jaarlijkse festival. Tickets EUR 24,50."),
    ("TU Delft Open Day", "Aula Congrescentrum TU Delft", 2, 10, True, "Meet the faculties. Free admission for all visitors."),
    ("Pubquiz in de Wijnhaven", "Café de Wijnhaven", 2, 21, False, "Wekelijkse pubquiz. Deelname EUR 2,50 per team."),
    ("Expositie: Delfts Blauw Herzien", "Museum Prinsenhof", 3, 11, False, "Tentoonstelling over hedendaags keramiek. Entree 14 euro."),
    ("Filmvertoning: Kortfilm Nacht", "Filmhuis Lumen", 3, 20, False, "Een avond met Nederlandse kortfilms. Tickets 9 euro."),
    ("Debatavond: De toekomst van de binnenstad", "Speakers Delft", 4, 19, True, "Debat met bewoners en de gemeente. Gratis toegang."),
    ("Zaterdagmarkt op de Markt", "Markt Delft", 5, 9, True, "Wekelijkse warenmarkt met streekproducten."),
    ("Orgelconcert in de Nieuwe Kerk", "Nieuwe Kerk", 5, 15, False, "Klassiek orgelconcert. Entree 12 euro."),
    ("Bootcamp op de campus", "Sportcentrum TU Delft", 6, 8, True, "Wekelijkse training, gratis voor studenten."),
    ("Wijnproeverij: Spaanse wijnen", "Doerak", 6, 19, False, "Proeverij met zes wijnen. EUR 32,50 per persoon."),
    ("Lezing: Stadsplanning na 2030", "Aula Congrescentrum TU Delft", 7, 16, True, "Lezing door de faculteit Bouwkunde. Gratis toegang."),
    ("Clubnacht: X Sessions", "X TU Delft", 7, 23, False, "Studentenclubnacht met dj set. Entree 7 euro."),
    ("Toneelvoorstelling: De Vreemdeling", "Theater de Veste", 8, 20, False, "Nederlandse toneelbewerking. Tickets EUR 19,00."),
    ("Rommelmarkt Voldersgracht", "Markt Delft", 9, 10, True, "Vlooienmarkt in de binnenstad. Gratis entree."),
    ("Repair Café Delft", "Doerak", 9, 13, True, "Breng kapotte spullen mee. Vrijwilligers helpen gratis."),
    ("Kamermuziek: Strijkkwartet", "Nieuwe Kerk", 10, 19, False, "Strijkkwartet speelt Sjostakovitsj. Tickets 21 euro."),
    ("Hardloopclinic langs de Schie", "Sportcentrum TU Delft", 11, 18, True, "Gratis hardlooptraining voor beginners."),
    ("Documentaire: Water en Land", "Filmhuis Lumen", 11, 20, False, "Documentaire over waterbeheer. Entree 9 euro."),
    ("Taalcafé Nederlands", "Doerak", 12, 19, True, "Oefen je Nederlands met vrijwilligers. Gratis."),
    ("Streetfood Festival Delft", "Markt Delft", 13, 12, True, "Food trucks op de Markt. Gratis toegang."),
    ("Masterclass: Machine Learning in de praktijk", "X TU Delft", 14, 14, True, "Masterclass voor studenten. Gratis deelname."),
    ("Jazzavond met het Delft Trio", "Bebop Jazzcafé", 14, 20, True, "Maandelijkse livemuziek. Gratis toegang."),
    ("Fotografie-expositie: Delft bij nacht", "Museum Prinsenhof", 15, 11, False, "Fototentoonstelling. Entree 14 euro."),
]

MADRID = [
    ("Concierto de verano en el Retiro", "Parque del Retiro", 0, 21, True, "Concierto al aire libre. Entrada libre."),
    ("Exposición: Madrid Moderno", "CentroCentro", 1, 10, False, "Exposición de arquitectura. Entrada 6 euros."),
    ("Ópera: La Traviata", "Teatro Real", 2, 20, False, "Ópera en tres actos. Entradas desde 45 euros."),
    ("Mercadillo de diseño", "Matadero Madrid", 3, 11, True, "Mercadillo de creadores locales. Acceso libre."),
    ("Cine de verano: cortometrajes", "Cine Doré", 3, 22, False, "Sesión de cortometrajes españoles. Entrada 3 euros."),
    ("Conferencia: arte y ciudad", "Círculo de Bellas Artes", 4, 19, True, "Charla abierta al público. Entrada gratuita."),
    ("Concierto: indie nacional", "Sala El Sol", 5, 22, False, "Concierto en directo. Entradas 18 euros."),
    ("Cata de vinos de Rioja", "Mercado de San Miguel", 5, 19, False, "Degustación guiada. 25 euros por persona."),
    ("Exposición permanente: Guernica", "Museo Reina Sofía", 6, 10, False, "Colección permanente. Entrada 12 euros."),
    ("Taller de fotografía urbana", "Conde Duque", 7, 17, True, "Taller gratuito para vecinos."),
    ("Teatro: improvisación en directo", "Matadero Madrid", 8, 20, False, "Espectáculo de improvisación. Entradas 14 euros."),
    ("Carrera popular del Retiro", "Parque del Retiro", 9, 9, True, "Carrera de 10km. Inscripción libre."),
    ("Documental: memoria de Madrid", "Cine Doré", 10, 20, False, "Documental histórico. Entrada 3 euros."),
    ("Concierto de piano", "Teatro Real", 11, 20, False, "Recital de piano. Entradas desde 30 euros."),
    ("Mercado de productores", "Mercado de San Miguel", 12, 10, True, "Mercado semanal. Acceso libre."),
    ("Asamblea vecinal de Chamberí", "Conde Duque", 13, 18, True, "Asamblea abierta a los vecinos."),
]

SOURCES = {
    "Delft": [
        ("delft_gemeente_agenda", TrustTier.MUNICIPAL),
        ("theater_de_veste", TrustTier.VENUE),
        ("cafe_wijnhaven_wrapper", TrustTier.VENUE),
        ("tudelft_events", TrustTier.VENUE),
        ("delft_op_zondag_rss", TrustTier.EDITORIAL),
    ],
    "Madrid": [
        ("madrid_ayuntamiento_api", TrustTier.MUNICIPAL),
        ("madrid_destino_jsonld", TrustTier.VENUE),
        ("madrid_cultura_rss", TrustTier.EDITORIAL),
    ],
}

# How each tier mangles a title, so dedup has something real to survive.
EDITORIAL_SUFFIX = {
    "Delft": [" trekt volle zaal", ": wat u moet weten", " opnieuw uitverkocht"],
    "Madrid": [" llena el aforo", ": todo lo que hay que saber", " vuelve este mes"],
}


def build(city: str, venues: dict, rows: list, tz: ZoneInfo) -> list:
    records: list[RawRecord] = []
    sources = SOURCES[city]

    for title, venue_name, day, hour, is_free, description in rows:
        lat, lon, address = venues[venue_name]
        start = (BASE + timedelta(days=day)).replace(
            hour=hour, minute=0, tzinfo=tz
        )
        # 1-3 independent sources per event, weighted toward fewer: most events
        # in a real corpus are listed once, which is what makes the multi-source
        # ones worth flagging in the UI.
        n_sources = random.choices([1, 2, 3], weights=[5, 3, 2])[0]
        chosen = random.sample(sources, min(n_sources, len(sources)))

        for source_id, trust in chosen:
            variant_title = title
            variant_start = start
            variant_venue = Venue(
                name=venue_name, address=address, city=city, lat=lat, lon=lon
            )
            if trust == TrustTier.EDITORIAL:
                variant_title = title + random.choice(EDITORIAL_SUFFIX[city])
                # papers report doors, not showtime
                variant_start = start + timedelta(minutes=random.choice([0, 15, 30]))
                # and rarely geocode anything
                variant_venue = Venue(name=venue_name, city=city)
            elif trust == TrustTier.VENUE and random.random() < 0.4:
                variant_title = title.split(":")[0].strip()

            records.append(
                RawRecord(
                    source_id=source_id,
                    source_url=f"https://example.invalid/{source_id}",
                    trust=trust,
                    title=variant_title,
                    start=variant_start,
                    end=variant_start + timedelta(hours=2),
                    description=description,
                    venue=variant_venue,
                    url=f"https://example.invalid/{source_id}/event",
                    is_free=is_free if trust != TrustTier.EDITORIAL else None,
                )
            )
    return records


def main() -> None:
    delft = deduplicate(build("Delft", DELFT_VENUES, DELFT, AMS), city="Delft", locale="nl")
    madrid = deduplicate(build("Madrid", MADRID_VENUES, MADRID, MAD), city="Madrid", locale="es")
    events = delft + madrid

    payload = []
    for e in events:
        payload.append({
            "id": e.id,
            "title": e.title,
            "start": e.start.isoformat(),
            "end": e.end.isoformat() if e.end else None,
            "city": e.city,
            "category": e.category,
            "is_free": e.is_free,
            "confidence": round(e.confidence, 3),
            "venue": {
                "name": e.venue.name if e.venue else None,
                "address": e.venue.address if e.venue else None,
                "lat": e.venue.lat if e.venue else None,
                "lon": e.venue.lon if e.venue else None,
            },
            "sources": [
                {"id": m.source_id, "trust": int(m.trust), "title": m.title}
                for m in e.members
            ],
        })

    out = Path(__file__).resolve().parent.parent / "data" / "demo_events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))

    uncategorised = sum(1 for e in events if e.category is None)
    multi = sum(1 for e in events if len(e.members) > 1)
    print(f"{len(events)} canonical events  ({len(delft)} Delft, {len(madrid)} Madrid)")
    print(f"{multi} corroborated by 2+ sources, {uncategorised} uncategorised")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
