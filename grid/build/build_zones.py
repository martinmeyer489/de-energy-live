#!/usr/bin/env python3
"""Build data/zones.geojson + data/data.json for the electricity map.

Inputs (fetched beforehand, see README):
  /tmp/countries.geo.json   world countries, id = ISO3
  /tmp/us-states.geojson    US states
  /tmp/ne_admin1.geojson    Natural Earth admin-1 (provinces/counties/regions)

Output zones: every country is a zone keyed by ISO3, except:
  - USA -> ISO/bidding zones built from state polygons
  - DEU -> 16 Bundesländer, each tagged with its TSO control area
  - ITA -> 6 market zones (NORD/CNOR/CSUD/SUD/SICI/SARD)
  - NOR -> NO1..NO5, SWE -> SE1..SE4, DNK -> DK1/DK2
  - GBR -> GB, except Northern Ireland admin-1 units -> SEM (with IRL)
"""
import json, math, random

COUNTRIES = "/tmp/countries.geo.json"
US_STATES = "/tmp/us-states.geojson"
ADMIN1 = "/tmp/ne_admin1.geojson"
OUT_GEO = "/paperclip/workspaces/devin/electricity-map/data/zones.geojson"
OUT_DATA = "/paperclip/workspaces/devin/electricity-map/data/data.json"

# ---------------- zone membership maps ----------------

US_ZONE = {
    "California": "US-CAISO",
    "Texas": "US-ERCOT",
    "New York": "US-NYISO",
    "Connecticut": "US-ISONE", "Massachusetts": "US-ISONE", "Maine": "US-ISONE",
    "New Hampshire": "US-ISONE", "Rhode Island": "US-ISONE", "Vermont": "US-ISONE",
    "Pennsylvania": "US-PJM", "New Jersey": "US-PJM", "Delaware": "US-PJM",
    "Maryland": "US-PJM", "District of Columbia": "US-PJM", "Virginia": "US-PJM",
    "West Virginia": "US-PJM", "Ohio": "US-PJM", "Kentucky": "US-PJM",
    "Indiana": "US-PJM", "Illinois": "US-PJM",
    "Minnesota": "US-MISO", "Wisconsin": "US-MISO", "Michigan": "US-MISO",
    "Iowa": "US-MISO", "Missouri": "US-MISO", "Arkansas": "US-MISO",
    "Louisiana": "US-MISO", "Mississippi": "US-MISO",
    "Kansas": "US-SPP", "Nebraska": "US-SPP", "Oklahoma": "US-SPP",
    "North Dakota": "US-SPP", "South Dakota": "US-SPP",
    "Florida": "US-SE", "Georgia": "US-SE", "Alabama": "US-SE",
    "Tennessee": "US-SE", "North Carolina": "US-SE", "South Carolina": "US-SE",
    "Washington": "US-NW", "Oregon": "US-NW", "Idaho": "US-NW",
    "Montana": "US-NW", "Wyoming": "US-NW", "Nevada": "US-NW", "Utah": "US-NW",
    "Colorado": "US-NW", "Arizona": "US-NW", "New Mexico": "US-NW",
    "Alaska": "US-AK", "Hawaii": "US-HI",
    }

IT_ZONE = {
    "Piemonte": "IT-NORD", "Valle d'Aosta": "IT-NORD", "Liguria": "IT-NORD",
    "Lombardia": "IT-NORD", "Trentino-Alto Adige": "IT-NORD", "Veneto": "IT-NORD",
    "Friuli-Venezia Giulia": "IT-NORD", "Emilia-Romagna": "IT-NORD",
    "Toscana": "IT-CNOR", "Umbria": "IT-CNOR", "Marche": "IT-CNOR",
    "Lazio": "IT-CSUD", "Abruzzo": "IT-CSUD", "Molise": "IT-CSUD",
    "Campania": "IT-SUD", "Apulia": "IT-SUD", "Basilicata": "IT-SUD",
    "Calabria": "IT-SUD",
    "Sicily": "IT-SICI",
    "Sardegna": "IT-SARD",
}

NO_ZONE = {
    "Oslo": "NO1", "Akershus": "NO1", "Østfold": "NO1", "Vestfold": "NO1",
    "Buskerud": "NO1", "Hedmark": "NO1", "Oppland": "NO1", "Telemark": "NO1",
    "Aust-Agder": "NO2", "Vest-Agder": "NO2", "Rogaland": "NO2",
    "Hordaland": "NO5", "Sogn og Fjordane": "NO5",
    "Sør-Trøndelag": "NO3", "Nord-Trøndelag": "NO3", "Møre og Romsdal": "NO3",
    "Nordland": "NO4", "Troms": "NO4", "Finnmark": "NO4", "Svalbard": "NO4",
}

SE_ZONE = {
    "Norrbotten": "SE1", "Västerbotten": "SE1",
    "Västernorrland": "SE2", "Jämtland": "SE2",
    "Gävleborg": "SE3", "Dalarna": "SE3", "Värmland": "SE3", "Orebro": "SE3",
    "Västmanland": "SE3", "Uppsala": "SE3", "Stockholm": "SE3",
    "Södermanland": "SE3", "Östergötland": "SE3", "Västra Götaland": "SE3",
    "Skåne": "SE4", "Blekinge": "SE4", "Kronoberg": "SE4", "Halland": "SE4",
    "Kalmar": "SE4", "Jönköping": "SE4", "Gotland": "SE4",
}

DK_ZONE = {
    "Nordjylland": "DK1", "Midtjylland": "DK1", "Syddanmark": "DK1",
    "Hovedstaden": "DK2", "Sjaælland": "DK2",
}

# German Bundesländer -> zone id. Each Bundesland also belongs to a TSO
# control area (50Hertz / Amprion / TenneT / TransnetBW); the assignment to
# the dominant TSO is approximate where areas don't follow state borders.
DE_ZONE = {
    "Baden-Württemberg": "DE-BW",
    "Bayern": "DE-BY",
    "Berlin": "DE-BE",
    "Brandenburg": "DE-BB",
    "Bremen": "DE-HB",
    "Hamburg": "DE-HH",
    "Hessen": "DE-HE",
    "Mecklenburg-Vorpommern": "DE-MV",
    "Niedersachsen": "DE-NI",
    "Nordrhein-Westfalen": "DE-NW",
    "Rheinland-Pfalz": "DE-RP",
    "Saarland": "DE-SL",
    "Sachsen": "DE-SN",
    "Sachsen-Anhalt": "DE-ST",
    "Schleswig-Holstein": "DE-SH",
    "Thüringen": "DE-TH",
}

COUNTRY_ZONE_OVERRIDE = {
    "IRL": "SEM",
}
REPLACE_WITH_ADMIN1 = {"USA", "ITA", "NOR", "SWE", "DNK", "DEU"}

ADMIN1_ZONE = {"ITA": IT_ZONE, "NOR": NO_ZONE, "SWE": SE_ZONE, "DNK": DK_ZONE,
               "DEU": DE_ZONE}

# ---------------- per-zone data ----------------
# price_eur: day-ahead-ish price normalized to EUR/MWh; disp: native display
# carbon: gCO2eq/kWh; renew: renewable %; lowcarbon: % low-carbon; demand GW
Z = {
 # --- Europe ---
 "AUT": dict(name="Austria", flag="🇦🇹", cur="€", price=105, carbon=185, renew=70, lowcarbon=74, demand=7,
     mix=dict(nuclear=0, hydro=52, wind=10, solar=9, coal=2, gas=16, biomass=6, other=5)),
 "LUX": dict(name="Luxembourg", flag="🇱🇺", cur="€", price=104, carbon=300, renew=32, lowcarbon=32, demand=0.7,
     mix=dict(nuclear=0, hydro=8, wind=6, solar=8, coal=0, gas=42, biomass=8, other=28)),
 # --- Germany: 16 Bundesländer, grouped into the 4 TSO control areas.
 #     Fallback snapshot values; build/fetch_live.py overwrites these with
 #     live SMARD (generation/load per control area) + energy-charts (price).
 # 50Hertz area (east + Hamburg): heavy wind onshore/offshore + lignite
 "DE-BE": dict(name="Berlin", flag="🇩🇪", cur="€", price=104, carbon=430, renew=38, lowcarbon=42, demand=1.8,
     mix=dict(nuclear=0, hydro=1, wind=20, solar=8, coal=38, gas=15, biomass=6, other=12), tso="50Hertz"),
 "DE-BB": dict(name="Brandenburg", flag="🇩🇪", cur="€", price=104, carbon=455, renew=42, lowcarbon=46, demand=2.0,
     mix=dict(nuclear=0, hydro=0, wind=30, solar=9, coal=36, gas=8, biomass=5, other=12), tso="50Hertz"),
 "DE-HH": dict(name="Hamburg", flag="🇩🇪", cur="€", price=104, carbon=420, renew=32, lowcarbon=36, demand=2.2,
     mix=dict(nuclear=0, hydro=1, wind=18, solar=5, coal=32, gas=26, biomass=6, other=12), tso="50Hertz"),
 "DE-MV": dict(name="Mecklenburg-Vorpommern", flag="🇩🇪", cur="€", price=104, carbon=390, renew=58, lowcarbon=60, demand=1.3,
     mix=dict(nuclear=0, hydro=0, wind=48, solar=10, coal=18, gas=8, biomass=8, other=8), tso="50Hertz"),
 "DE-SN": dict(name="Sachsen", flag="🇩🇪", cur="€", price=104, carbon=440, renew=36, lowcarbon=40, demand=3.2,
     mix=dict(nuclear=0, hydro=2, wind=16, solar=10, coal=40, gas=14, biomass=6, other=12), tso="50Hertz"),
 "DE-ST": dict(name="Sachsen-Anhalt", flag="🇩🇪", cur="€", price=104, carbon=450, renew=40, lowcarbon=44, demand=1.6,
     mix=dict(nuclear=0, hydro=1, wind=28, solar=9, coal=36, gas=10, biomass=6, other=10), tso="50Hertz"),
 "DE-TH": dict(name="Thüringen", flag="🇩🇪", cur="€", price=104, carbon=435, renew=38, lowcarbon=42, demand=1.6,
     mix=dict(nuclear=0, hydro=2, wind=22, solar=9, coal=34, gas=12, biomass=8, other=13), tso="50Hertz"),
 # Amprion area (west: NRW/RP/SL): industrial demand, coal + gas
 "DE-NW": dict(name="Nordrhein-Westfalen", flag="🇩🇪", cur="€", price=104, carbon=520, renew=30, lowcarbon=34, demand=15,
     mix=dict(nuclear=0, hydro=1, wind=14, solar=6, coal=38, gas=22, biomass=8, other=11), tso="Amprion"),
 "DE-RP": dict(name="Rheinland-Pfalz", flag="🇩🇪", cur="€", price=104, carbon=430, renew=40, lowcarbon=44, demand=3.6,
     mix=dict(nuclear=0, hydro=6, wind=22, solar=8, coal=24, gas=20, biomass=10, other=10), tso="Amprion"),
 "DE-SL": dict(name="Saarland", flag="🇩🇪", cur="€", price=104, carbon=500, renew=28, lowcarbon=32, demand=1.0,
     mix=dict(nuclear=0, hydro=1, wind=10, solar=6, coal=34, gas=34, biomass=4, other=11), tso="Amprion"),
 # TenneT area (north + south long corridor): wind north, solar south
 "DE-SH": dict(name="Schleswig-Holstein", flag="🇩🇪", cur="€", price=104, carbon=340, renew=72, lowcarbon=74, demand=3.0,
     mix=dict(nuclear=0, hydro=0, wind=62, solar=6, coal=10, gas=8, biomass=8, other=6), tso="TenneT"),
 "DE-HB": dict(name="Bremen", flag="🇩🇪", cur="€", price=104, carbon=430, renew=34, lowcarbon=38, demand=1.2,
     mix=dict(nuclear=0, hydro=0, wind=24, solar=5, coal=26, gas=28, biomass=6, other=11), tso="TenneT"),
 "DE-NI": dict(name="Niedersachsen", flag="🇩🇪", cur="€", price=104, carbon=410, renew=52, lowcarbon=56, demand=7.5,
     mix=dict(nuclear=0, hydro=1, wind=40, solar=7, coal=18, gas=18, biomass=10, other=6), tso="TenneT"),
 "DE-HE": dict(name="Hessen", flag="🇩🇪", cur="€", price=104, carbon=420, renew=36, lowcarbon=40, demand=6.0,
     mix=dict(nuclear=0, hydro=2, wind=16, solar=9, coal=30, gas=22, biomass=10, other=11), tso="TenneT"),
 "DE-BY": dict(name="Bayern", flag="🇩🇪", cur="€", price=104, carbon=380, renew=48, lowcarbon=52, demand=11,
     mix=dict(nuclear=0, hydro=8, wind=12, solar=24, coal=16, gas=22, biomass=10, other=8), tso="TenneT"),
 # TransnetBW area
 "DE-BW": dict(name="Baden-Württemberg", flag="🇩🇪", cur="€", price=104, carbon=390, renew=42, lowcarbon=48, demand=10,
     mix=dict(nuclear=0, hydro=8, wind=8, solar=18, coal=22, gas=26, biomass=10, other=8), tso="TransnetBW"),
 "FRA": dict(name="France", flag="🇫🇷", cur="€", price=52, carbon=42, renew=24, lowcarbon=92, demand=55,
     mix=dict(nuclear=63, hydro=11, wind=9, solar=5, coal=1, gas=6, biomass=2, other=3)),
 "ESP": dict(name="Spain", flag="🇪🇸", cur="€", price=71, carbon=158, renew=64, lowcarbon=78, demand=29,
     mix=dict(nuclear=20, hydro=8, wind=27, solar=22, coal=2, gas=14, biomass=3, other=4)),
 "PRT": dict(name="Portugal", flag="🇵🇹", cur="€", price=70, carbon=148, renew=72, lowcarbon=76, demand=6,
     mix=dict(nuclear=0, hydro=22, wind=31, solar=14, coal=0, gas=24, biomass=5, other=4)),
 "GBR": dict(name="Great Britain", flag="🇬🇧", cur="£", price=97, carbon=208, renew=57, lowcarbon=66, demand=33,
     mix=dict(nuclear=12, hydro=2, wind=38, solar=9, coal=0, gas=30, biomass=6, other=3)),
 "SEM": dict(name="Ireland & N. Ireland (SEM)", flag="🇮🇪", cur="€", price=112, carbon=285, renew=49, lowcarbon=49, demand=6.5,
     mix=dict(nuclear=0, hydro=3, wind=44, solar=2, coal=4, gas=44, biomass=1, other=2)),
 "IT-NORD": dict(name="Italy North", flag="🇮🇹", cur="€", price=121, carbon=335, renew=39, lowcarbon=39, demand=28,
     mix=dict(nuclear=0, hydro=18, wind=6, solar=15, coal=3, gas=50, biomass=4, other=4)),
 "IT-CNOR": dict(name="Italy Centre-North", flag="🇮🇹", cur="€", price=119, carbon=345, renew=30, lowcarbon=30, demand=9,
     mix=dict(nuclear=0, hydro=4, wind=4, solar=16, coal=2, gas=66, biomass=3, other=5)),
 "IT-CSUD": dict(name="Italy Centre-South", flag="🇮🇹", cur="€", price=123, carbon=360, renew=33, lowcarbon=33, demand=10,
     mix=dict(nuclear=0, hydro=6, wind=9, solar=18, coal=4, gas=55, biomass=3, other=5)),
 "IT-SUD": dict(name="Italy South", flag="🇮🇹", cur="€", price=126, carbon=378, renew=44, lowcarbon=44, demand=8,
     mix=dict(nuclear=0, hydro=3, wind=18, solar=23, coal=5, gas=45, biomass=2, other=4)),
 "IT-SICI": dict(name="Sicily", flag="🇮🇹", cur="€", price=131, carbon=480, renew=30, lowcarbon=30, demand=4,
     mix=dict(nuclear=0, hydro=1, wind=10, solar=19, coal=0, gas=62, biomass=1, other=7)),
 "IT-SARD": dict(name="Sardinia", flag="🇮🇹", cur="€", price=129, carbon=430, renew=36, lowcarbon=36, demand=1.5,
     mix=dict(nuclear=0, hydro=2, wind=14, solar=20, coal=18, gas=38, biomass=2, other=6)),
 "NLD": dict(name="Netherlands", flag="🇳🇱", cur="€", price=101, carbon=330, renew=44, lowcarbon=46, demand=15,
     mix=dict(nuclear=3, hydro=0, wind=27, solar=17, coal=8, gas=38, biomass=4, other=3)),
 "BEL": dict(name="Belgium", flag="🇧🇪", cur="€", price=103, carbon=185, renew=38, lowcarbon=72, demand=10,
     mix=dict(nuclear=42, hydro=1, wind=22, solar=14, coal=0, gas=16, biomass=3, other=2)),
 "POL": dict(name="Poland", flag="🇵🇱", cur="€", price=98, carbon=660, renew=24, lowcarbon=24, demand=23,
     mix=dict(nuclear=0, hydro=2, wind=15, solar=7, coal=60, gas=8, biomass=4, other=4)),
 "CZE": dict(name="Czechia", flag="🇨🇿", cur="€", price=96, carbon=470, renew=16, lowcarbon=56, demand=8,
     mix=dict(nuclear=40, hydro=3, wind=1, solar=8, coal=33, gas=6, biomass=5, other=4)),
 "SVK": dict(name="Slovakia", flag="🇸🇰", cur="€", price=97, carbon=190, renew=21, lowcarbon=80, demand=4,
     mix=dict(nuclear=59, hydro=16, wind=0, solar=5, coal=6, gas=8, biomass=3, other=3)),
 "HUN": dict(name="Hungary", flag="🇭🇺", cur="€", price=106, carbon=275, renew=22, lowcarbon=66, demand=6,
     mix=dict(nuclear=44, hydro=1, wind=2, solar=18, coal=8, gas=22, biomass=4, other=1)),
 "ROU": dict(name="Romania", flag="🇷🇴", cur="€", price=100, carbon=330, renew=42, lowcarbon=62, demand=7,
     mix=dict(nuclear=20, hydro=25, wind=12, solar=8, coal=14, gas=16, biomass=1, other=4)),
 "BGR": dict(name="Bulgaria", flag="🇧🇬", cur="€", price=102, carbon=505, renew=24, lowcarbon=55, demand=4,
     mix=dict(nuclear=31, hydro=8, wind=4, solar=11, coal=35, gas=5, biomass=2, other=4)),
 "GRC": dict(name="Greece", flag="🇬🇷", cur="€", price=108, carbon=350, renew=52, lowcarbon=52, demand=7,
     mix=dict(nuclear=0, hydro=7, wind=22, solar=23, coal=8, gas=33, biomass=1, other=6)),
 "HRV": dict(name="Croatia", flag="🇭🇷", cur="€", price=104, carbon=250, renew=48, lowcarbon=48, demand=2.5,
     mix=dict(nuclear=0, hydro=34, wind=13, solar=3, coal=6, gas=27, biomass=3, other=14)),
 "SVN": dict(name="Slovenia", flag="🇸🇮", cur="€", price=105, carbon=265, renew=36, lowcarbon=70, demand=2,
     mix=dict(nuclear=34, hydro=30, wind=0, solar=5, coal=18, gas=4, biomass=3, other=6)),
 "NO1": dict(name="Norway — Oslo/SE", flag="🇳🇴", cur="€", price=38, carbon=24, renew=98, lowcarbon=98, demand=9,
     mix=dict(nuclear=0, hydro=88, wind=10, solar=0, coal=0, gas=1, biomass=1, other=0)),
 "NO2": dict(name="Norway — South", flag="🇳🇴", cur="€", price=41, carbon=22, renew=99, lowcarbon=99, demand=4,
     mix=dict(nuclear=0, hydro=84, wind=15, solar=0, coal=0, gas=1, biomass=0, other=0)),
 "NO3": dict(name="Norway — Middle", flag="🇳🇴", cur="€", price=26, carbon=19, renew=99, lowcarbon=99, demand=4,
     mix=dict(nuclear=0, hydro=93, wind=6, solar=0, coal=0, gas=1, biomass=0, other=0)),
 "NO4": dict(name="Norway — North", flag="🇳🇴", cur="€", price=17, carbon=15, renew=99, lowcarbon=99, demand=3,
     mix=dict(nuclear=0, hydro=82, wind=17, solar=0, coal=0, gas=1, biomass=0, other=0)),
 "NO5": dict(name="Norway — West", flag="🇳🇴", cur="€", price=39, carbon=21, renew=98, lowcarbon=98, demand=5,
     mix=dict(nuclear=0, hydro=96, wind=2, solar=0, coal=0, gas=1, biomass=1, other=0)),
 "SE1": dict(name="Sweden — North (SE1)", flag="🇸🇪", cur="€", price=21, carbon=17, renew=98, lowcarbon=98, demand=3,
     mix=dict(nuclear=0, hydro=80, wind=18, solar=0, coal=0, gas=0, biomass=2, other=0)),
 "SE2": dict(name="Sweden — North Central (SE2)", flag="🇸🇪", cur="€", price=23, carbon=18, renew=96, lowcarbon=96, demand=4,
     mix=dict(nuclear=0, hydro=76, wind=20, solar=0, coal=0, gas=1, biomass=3, other=0)),
 "SE3": dict(name="Sweden — Stockholm (SE3)", flag="🇸🇪", cur="€", price=47, carbon=27, renew=72, lowcarbon=97, demand=15,
     mix=dict(nuclear=39, hydro=45, wind=11, solar=1, coal=0, gas=1, biomass=3, other=0)),
 "SE4": dict(name="Sweden — Malmö (SE4)", flag="🇸🇪", cur="€", price=79, carbon=58, renew=79, lowcarbon=97, demand=7,
     mix=dict(nuclear=18, hydro=1, wind=58, solar=3, coal=0, gas=2, biomass=3, other=15)),
 "DK1": dict(name="Denmark — West", flag="🇩🇰", cur="€", price=88, carbon=145, renew=81, lowcarbon=81, demand=3,
     mix=dict(nuclear=0, hydro=0, wind=62, solar=13, coal=8, gas=6, biomass=8, other=3)),
 "DK2": dict(name="Denmark — East", flag="🇩🇰", cur="€", price=91, carbon=135, renew=76, lowcarbon=76, demand=2.5,
     mix=dict(nuclear=0, hydro=0, wind=56, solar=10, coal=12, gas=8, biomass=11, other=3)),
 "FIN": dict(name="Finland", flag="🇫🇮", cur="€", price=44, carbon=88, renew=62, lowcarbon=93, demand=11,
     mix=dict(nuclear=35, hydro=20, wind=19, solar=1, coal=2, gas=2, biomass=17, other=4)),
 "EST": dict(name="Estonia", flag="🇪🇪", cur="€", price=74, carbon=380, renew=46, lowcarbon=46, demand=1.2,
     mix=dict(nuclear=0, hydro=0, wind=32, solar=9, coal=30, gas=6, biomass=12, other=11)),
 "LVA": dict(name="Latvia", flag="🇱🇻", cur="€", price=70, carbon=175, renew=62, lowcarbon=62, demand=1,
     mix=dict(nuclear=0, hydro=45, wind=13, solar=4, coal=0, gas=28, biomass=6, other=4)),
 "LTU": dict(name="Lithuania", flag="🇱🇹", cur="€", price=72, carbon=155, renew=66, lowcarbon=66, demand=1.8,
     mix=dict(nuclear=0, hydro=5, wind=40, solar=12, coal=0, gas=24, biomass=9, other=10)),
 "CHE": dict(name="Switzerland", flag="🇨🇭", cur="€", price=118, carbon=62, renew=68, lowcarbon=98, demand=7,
     mix=dict(nuclear=30, hydro=56, wind=0, solar=8, coal=0, gas=1, biomass=2, other=3)),
 "UKR": dict(name="Ukraine", flag="🇺🇦", cur="€", price=92, carbon=320, renew=14, lowcarbon=62, demand=13,
     mix=dict(nuclear=52, hydro=9, wind=3, solar=6, coal=24, gas=3, biomass=1, other=2)),
 "TUR": dict(name="Türkiye", flag="🇹🇷", cur="€", price=78, carbon=400, renew=42, lowcarbon=42, demand=48,
     mix=dict(nuclear=0, hydro=22, wind=11, solar=9, coal=28, gas=20, biomass=2, other=8)),
 "SRB": dict(name="Serbia", flag="🇷🇸", cur="€", price=99, carbon=640, renew=28, lowcarbon=28, demand=4,
     mix=dict(nuclear=0, hydro=26, wind=2, solar=1, coal=65, gas=2, biomass=1, other=3)),
 # --- North America ---
 "US-CAISO": dict(name="California (CAISO)", flag="🇺🇸", cur="$", price=46, carbon=255, renew=54, lowcarbon=68, demand=30,
     mix=dict(nuclear=9, hydro=7, wind=13, solar=31, coal=0, gas=32, biomass=2, other=6)),
 "US-ERCOT": dict(name="Texas (ERCOT)", flag="🇺🇸", cur="$", price=31, carbon=355, renew=40, lowcarbon=48, demand=62,
     mix=dict(nuclear=8, hydro=0, wind=28, solar=11, coal=11, gas=41, biomass=0, other=1)),
 "US-NYISO": dict(name="New York (NYISO)", flag="🇺🇸", cur="$", price=54, carbon=235, renew=31, lowcarbon=55, demand=22,
     mix=dict(nuclear=24, hydro=22, wind=4, solar=2, coal=0, gas=45, biomass=1, other=2)),
 "US-ISONE": dict(name="New England (ISO-NE)", flag="🇺🇸", cur="$", price=61, carbon=225, renew=18, lowcarbon=52, demand=15,
     mix=dict(nuclear=22, hydro=8, wind=4, solar=4, coal=0, gas=52, biomass=6, other=4)),
 "US-PJM": dict(name="PJM Interconnection", flag="🇺🇸", cur="$", price=44, carbon=385, renew=11, lowcarbon=45, demand=95,
     mix=dict(nuclear=33, hydro=1, wind=4, solar=2, coal=20, gas=38, biomass=1, other=1)),
 "US-MISO": dict(name="Midcontinent (MISO)", flag="🇺🇸", cur="$", price=37, carbon=455, renew=24, lowcarbon=40, demand=78,
     mix=dict(nuclear=12, hydro=1, wind=22, solar=2, coal=32, gas=28, biomass=1, other=2)),
 "US-SPP": dict(name="Southwest Power Pool", flag="🇺🇸", cur="$", price=29, carbon=380, renew=46, lowcarbon=51, demand=45,
     mix=dict(nuclear=5, hydro=3, wind=41, solar=2, coal=25, gas=22, biomass=0, other=2)),
 "US-NW": dict(name="US Northwest & Rockies", flag="🇺🇸", cur="$", price=42, carbon=245, renew=57, lowcarbon=66, demand=55,
     mix=dict(nuclear=4, hydro=35, wind=14, solar=6, coal=14, gas=24, biomass=1, other=2)),
 "US-SE": dict(name="US Southeast", flag="🇺🇸", cur="$", price=36, carbon=415, renew=15, lowcarbon=44, demand=110,
     mix=dict(nuclear=25, hydro=4, wind=0, solar=9, coal=18, gas=42, biomass=1, other=1)),
 "US-AK": dict(name="Alaska", flag="🇺🇸", cur="$", price=175, carbon=490, renew=28, lowcarbon=28, demand=0.7,
     mix=dict(nuclear=0, hydro=24, wind=3, solar=0, coal=12, gas=48, biomass=0, other=13)),
 "US-HI": dict(name="Hawaii", flag="🇺🇸", cur="$", price=275, carbon=615, renew=33, lowcarbon=33, demand=1.1,
     mix=dict(nuclear=0, hydro=1, wind=7, solar=25, coal=11, gas=0, biomass=3, other=53)),
 "PRI": dict(name="Puerto Rico", flag="🇵🇷", cur="$", price=195, carbon=640, renew=4, lowcarbon=4, demand=2.5,
     mix=dict(nuclear=0, hydro=1, wind=1, solar=3, coal=17, gas=42, biomass=0, other=36)),
 "CAN": dict(name="Canada", flag="🇨🇦", cur="$", price=44, carbon=125, renew=68, lowcarbon=84, demand=75,
     mix=dict(nuclear=15, hydro=58, wind=7, solar=1, coal=4, gas=11, biomass=2, other=2)),
 "MEX": dict(name="Mexico", flag="🇲🇽", cur="$", price=58, carbon=420, renew=23, lowcarbon=30, demand=42,
     mix=dict(nuclear=3, hydro=11, wind=7, solar=5, coal=8, gas=61, biomass=1, other=4)),
 # --- Rest of world ---
 "BRA": dict(name="Brazil", flag="🇧🇷", cur="$", price=52, carbon=95, renew=86, lowcarbon=91, demand=70,
     mix=dict(nuclear=2, hydro=62, wind=13, solar=9, coal=2, gas=6, biomass=8, other=2)),
 "ARG": dict(name="Argentina", flag="🇦🇷", cur="$", price=48, carbon=330, renew=32, lowcarbon=40, demand=15,
     mix=dict(nuclear=8, hydro=20, wind=10, solar=3, coal=1, gas=55, biomass=1, other=2)),
 "CHL": dict(name="Chile", flag="🇨🇱", cur="$", price=64, carbon=350, renew=44, lowcarbon=44, demand=9,
     mix=dict(nuclear=0, hydro=24, wind=9, solar=20, coal=17, gas=24, biomass=4, other=2)),
 "COL": dict(name="Colombia", flag="🇨🇴", cur="$", price=50, carbon=180, renew=70, lowcarbon=70, demand=10,
     mix=dict(nuclear=0, hydro=66, wind=0, solar=3, coal=7, gas=20, biomass=2, other=2)),
 "AUS": dict(name="Australia (NEM)", flag="🇦🇺", cur="$", price=82, carbon=545, renew=39, lowcarbon=39, demand=26,
     mix=dict(nuclear=0, hydro=7, wind=13, solar=19, coal=52, gas=7, biomass=1, other=1)),
 "NZL": dict(name="New Zealand", flag="🇳🇿", cur="$", price=78, carbon=92, renew=84, lowcarbon=84, demand=6,
     mix=dict(nuclear=0, hydro=58, wind=7, solar=3, coal=3, gas=10, biomass=1, other=18)),
 "JPN": dict(name="Japan", flag="🇯🇵", cur="$", price=92, carbon=475, renew=24, lowcarbon=34, demand=105,
     mix=dict(nuclear=10, hydro=8, wind=1, solar=11, coal=30, gas=34, biomass=4, other=2)),
 "KOR": dict(name="South Korea", flag="🇰🇷", cur="$", price=98, carbon=435, renew=9, lowcarbon=40, demand=80,
     mix=dict(nuclear=31, hydro=1, wind=2, solar=5, coal=32, gas=27, biomass=1, other=1)),
 "CHN": dict(name="China", flag="🇨🇳", cur="$", price=68, carbon=565, renew=32, lowcarbon=36, demand=1250,
     mix=dict(nuclear=5, hydro=15, wind=10, solar=7, coal=55, gas=3, biomass=2, other=3)),
 "IND": dict(name="India", flag="🇮🇳", cur="$", price=62, carbon=650, renew=22, lowcarbon=25, demand=240,
     mix=dict(nuclear=3, hydro=11, wind=5, solar=8, coal=68, gas=2, biomass=2, other=1)),
 "ZAF": dict(name="South Africa", flag="🇿🇦", cur="$", price=54, carbon=800, renew=12, lowcarbon=12, demand=28,
     mix=dict(nuclear=5, hydro=2, wind=4, solar=5, coal=82, gas=0, biomass=0, other=2)),
 "RUS": dict(name="Russia", flag="🇷🇺", cur="$", price=38, carbon=450, renew=19, lowcarbon=56, demand=140,
     mix=dict(nuclear=20, hydro=18, wind=0, solar=0, coal=17, gas=43, biomass=0, other=2)),
 "SAU": dict(name="Saudi Arabia", flag="🇸🇦", cur="$", price=42, carbon=620, renew=2, lowcarbon=2, demand=60,
     mix=dict(nuclear=0, hydro=0, wind=0, solar=2, coal=0, gas=62, biomass=0, other=36)),
 "ARE": dict(name="United Arab Emirates", flag="🇦🇪", cur="$", price=48, carbon=520, renew=7, lowcarbon=32, demand=18,
     mix=dict(nuclear=25, hydro=0, wind=0, solar=7, coal=4, gas=63, biomass=0, other=1)),
 "ISR": dict(name="Israel", flag="🇮🇱", cur="$", price=88, carbon=460, renew=14, lowcarbon=14, demand=12,
     mix=dict(nuclear=0, hydro=0, wind=0, solar=14, coal=22, gas=62, biomass=0, other=2)),
 "EGY": dict(name="Egypt", flag="🇪🇬", cur="$", price=45, carbon=450, renew=12, lowcarbon=12, demand=32,
     mix=dict(nuclear=0, hydro=8, wind=3, solar=2, coal=0, gas=84, biomass=0, other=3)),
 "MAR": dict(name="Morocco", flag="🇲🇦", cur="$", price=55, carbon=560, renew=22, lowcarbon=22, demand=7,
     mix=dict(nuclear=0, hydro=6, wind=13, solar=4, coal=60, gas=10, biomass=0, other=7)),
 "NGA": dict(name="Nigeria", flag="🇳🇬", cur="$", price=40, carbon=430, renew=24, lowcarbon=24, demand=5,
     mix=dict(nuclear=0, hydro=23, wind=0, solar=1, coal=0, gas=70, biomass=0, other=6)),
}

# Exchanges: [from_zone, to_zone, MW flowing from->to]
EXCHANGES = [
 ["FRA","DE-SL",2500],["DE-NW","NLD",1800],["DE-BB","POL",1400],["DE-SH","DK1",1200],
 ["DE-SN","CZE",1900],["DE-BW","CHE",3200],["DE-BY","AUT",1500],["DE-RP","LUX",300],
 ["DE-NW","BEL",1000],["DE-MV","SE4",600],["DE-SH","NO2",1400],["FRA","GBR",2100],["FRA","ESP",2300],
 ["FRA","IT-NORD",2900],["CHE","IT-NORD",3500],["ESP","PRT",1600],["MAR","ESP",600],
 ["IT-NORD","IT-CNOR",1500],["IT-CNOR","IT-CSUD",1100],["IT-CSUD","IT-SUD",1300],
 ["IT-SUD","IT-SICI",700],["IT-CSUD","IT-SARD",500],["IT-NORD","SVN",600],
 ["NO1","SE3",1100],["NO4","NO3",800],["NO3","NO1",500],["NO5","NO1",900],
 ["NO2","DK1",850],["SE1","SE2",1400],["SE2","SE3",1700],["SE3","SE4",750],
 ["SE3","FIN",1000],["FIN","EST",650],["LTU","LVA",500],["POL","LTU",450],
 ["DK1","NLD",700],["DK2","SE4",550],["DK1","DK2",600],["GBR","NLD",1000],
 ["GBR","SEM",350],["EST","FIN",-300],["POL","UKR",350],["SVK","UKR",900],
 ["HUN","UKR",500],["HUN","ROU",400],["BGR","GRC",800],["TUR","BGR",300],
 ["US-NW","US-CAISO",3400],["US-MISO","US-PJM",2100],["US-SPP","US-MISO",1200],
 ["US-PJM","US-NYISO",800],["US-NYISO","US-ISONE",900],["US-SE","US-PJM",500],
 ["US-MISO","US-SE",700],["US-SE","US-MISO",0],["CAN","US-NW",2200],
 ["CAN","US-ISONE",1300],["CAN","US-NYISO",1600],["MEX","US-CAISO",250],
 ["BRA","ARG",200],
]

# ---------------- helpers ----------------
def rnd_coords(coords, nd=3):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [rnd_coords(c, nd) for c in coords]

def ring_area(ring):
    a = 0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i+1][1] - ring[i+1][0] * ring[i][1]
    return a / 2

def geom_center(geom):
    """Centroid of the largest polygon in a (Multi)Polygon."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    best, ba = None, -1
    for p in polys:
        a = abs(ring_area(p[0]))
        if a > ba:
            ba, best = a, p[0]
    sx = sy = 0
    n = max(1, len(best) - 1)
    step = max(1, n // 200)
    cnt = 0
    for i in range(0, n, step):
        sx += best[i][0]; sy += best[i][1]; cnt += 1
    return [round(sx / cnt, 3), round(sy / cnt, 3)]

# ---------------- build zones.geojson ----------------
countries = json.load(open(COUNTRIES))
us_states = json.load(open(US_STATES))
admin1 = json.load(open(ADMIN1))

features = []
zone_polys = {}   # zone -> [largest-area center candidates (area, center)]

def add_feature(geom, zone, name, sub=None):
    geom = {"type": geom["type"], "coordinates": rnd_coords(geom["coordinates"])}
    features.append({"type": "Feature",
        "properties": {"zone": zone, "name": name, "sub": sub or name},
        "geometry": geom})
    # track representative center from largest ring
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for p in polys:
        a = abs(ring_area(p[0])) if p and p[0] else 0
        zone_polys.setdefault(zone, []).append((a, geom_center({"type":"Polygon","coordinates":p})))

for f in countries["features"]:
    iso = f["id"]
    if iso in REPLACE_WITH_ADMIN1 or iso == "ATA":
        continue
    zone = COUNTRY_ZONE_OVERRIDE.get(iso, iso)
    add_feature(f["geometry"], zone, f["properties"]["name"])

for f in us_states["features"]:
    st = f["properties"]["name"]
    zone = US_ZONE.get(st)
    if not zone:
        continue
    add_feature(f["geometry"], zone, st + (" (part of %s)" % zone if zone.startswith("US-") else ""), st)

for f in admin1["features"]:
    p = f["properties"]
    c = p.get("adm0_a3")
    if c in ADMIN1_ZONE:
        key = p["region"] if c == "ITA" else p["name"]
        zone = ADMIN1_ZONE[c].get(key)
        if not zone:
            continue
        add_feature(f["geometry"], zone, p["name"], p["name"])
    elif c == "GBR" and p.get("region") == "Northern Ireland":
        add_feature(f["geometry"], "SEM", p["name"], p["name"])

# label points = center of each zone's largest polygon
labels = {z: max(lst, key=lambda t: t[0])[1] for z, lst in zone_polys.items()}
# manual fixes where largest-polygon centroid is misleading
labels.update({
 "SEM": [-7.9, 54.5], "DK1": [9.0, 56.2], "DK2": [11.9, 55.4],
 "US-NW": [-117.0, 45.5], "US-SE": [-84.0, 33.5], "US-MISO": [-92.5, 41.0],
 "US-PJM": [-78.5, 38.5], "US-SPP": [-99.5, 39.5], "US-ISONE": [-71.8, 43.5],
 "US-NYISO": [-75.0, 42.8], "US-AK": [-152.0, 64.0], "US-HI": [-157.8, 21.0],
 "PRI": [-66.4, 18.2], "IT-NORD": [10.0, 45.4], "IT-CNOR": [11.6, 43.3],
 "IT-CSUD": [13.5, 41.7], "IT-SUD": [15.8, 40.4], "IT-SICI": [14.2, 37.6],
 "IT-SARD": [9.0, 40.1], "NO1": [10.5, 60.3], "NO2": [7.2, 58.5], "NO3": [9.5, 63.0],
 "NO4": [17.5, 67.5], "NO5": [6.3, 60.6], "SE1": [19.5, 66.5], "SE2": [16.5, 63.5],
 "SE3": [16.0, 60.0], "SE4": [13.5, 55.8], "GBR": [-2.5, 53.5], "FR": [2.4, 46.7],
 "ESP": [-3.7, 40.0], "PRT": [-8.1, 39.6], "NLD": [5.3, 52.2], "BEL": [4.6, 50.7],
 "CHE": [8.2, 46.8], "POL": [19.4, 52.1], "FIN": [26.0, 64.0], "EST": [25.5, 58.7],
 "LVA": [24.9, 56.9], "LTU": [23.9, 55.3], "US-CAISO": [-119.5, 37.0],
 "US-ERCOT": [-99.0, 31.0], "CAN": [-96.0, 56.0], "UKR": [31.0, 49.0],
 "MAR": [-6.0, 32.0],
})

json.dump({"type": "FeatureCollection", "features": features},
          open(OUT_GEO, "w"), separators=(",", ":"))

# ---------------- build data.json ----------------
random.seed(42)
zones_out = {}
for zid, z in Z.items():
    # 24h series: shape price/carbon/renewable with plausible daily curves
    price_s, carb_s, ren_s = [], [], []
    solar = z["mix"].get("solar", 0)
    for h in range(24):
        # duck curve: solar depresses midday price, wind adds noise
        sun = max(0.0, math.sin((h - 6) / 12 * math.pi))  # 6h..18h
        load = 0.8 + 0.2 * math.sin((h - 15) / 12 * math.pi)  # evening peak
        jitter = random.uniform(-0.08, 0.08)
        p = z["price"] * (load + 0.15 - 0.35 * sun * min(solar, 30) / 30 + jitter)
        c = z["carbon"] * (1 + 0.18 * (1 - sun) - 0.10 * sun * min(solar, 30) / 30 + jitter / 2)
        r = z["renew"] * (1 + 0.25 * sun * min(solar, 30) / 30 + jitter / 3)
        price_s.append(round(p, 1)); carb_s.append(round(c)); ren_s.append(round(max(1, min(99, r)), 1))
    zones_out[zid] = {
        "name": z["name"], "flag": z["flag"], "currency": z["cur"],
        "price": z["price"], "carbon": z["carbon"], "renewable": z["renew"],
        "lowCarbon": z["lowcarbon"], "demandGW": z["demand"], "mix": z["mix"],
        "center": labels.get(zid, [0, 0]),
        "series": {"price": price_s, "carbon": carb_s, "renewable": ren_s},
    }
    if z.get("tso"):
        zones_out[zid]["tso"] = z["tso"]

exch = [{"f": a, "t": b, "mw": mw} for a, b, mw in EXCHANGES
        if a in zones_out and b in zones_out and mw != 0]

json.dump({
    "generatedAt": "2026-10-30T12:00:00Z",
    "note": "Bundled realistic snapshot (ENTSO-E/EIA-style magnitudes). "
            "Plug a live feed by replacing fetch() in js/data-provider.js.",
    "signals": {
        "price": {"label": "Electricity price", "unit": "€/MWh"},
        "carbon": {"label": "Carbon intensity", "unit": "gCO₂eq/kWh"},
        "renewable": {"label": "Renewable share", "unit": "%"},
    },
    "zones": zones_out,
    "exchanges": exch,
}, open(OUT_DATA, "w"), separators=(",", ":"))

import os
print("features:", len(features), "zones:", len(zones_out), "exchanges:", len(exch))
print("zones.geojson KB:", os.path.getsize(OUT_GEO)//1024,
      "| data.json KB:", os.path.getsize(OUT_DATA)//1024)
missing = [z for z in zones_out if z not in labels]
print("zones missing geometry:", missing)
