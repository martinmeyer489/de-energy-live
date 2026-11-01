#!/usr/bin/env python3
"""
Fetch real electricity-market data for the interactive price map.

Sources (all public, no API key):
  - api.energy-charts.info (Fraunhofer ISE, CC BY 4.0 / SMARD):
      /price         day-ahead spot price per bidding zone (15-min or hourly)
      /public_power  generation mix per country (15-min)
      /cbpf          cross-border physical flows per country (15-min)
  - electricitymaps-contrib geo/world.geojson for zone polygons (already
    downloaded to geo_raw.geojson; re-download if missing).

Output:
  geo/zones.geojson   European zone polygons (simplified)
  data/data.json      prices + mix + flows + estimated carbon intensity

To swap in a live backend later, replace data/data.json with an equivalent
payload served by your API; the frontend only reads that one file.
"""

import json
import math
import os
import time
import urllib.request
import urllib.error

BASE = "https://api.energy-charts.info"
SMARD = "https://www.smard.de/app/chart_data"
HERE = os.path.dirname(os.path.abspath(__file__))
GEO_RAW = os.path.join(HERE, "geo_raw.geojson")
GEO_URL = ("https://raw.githubusercontent.com/electricitymaps/"
           "electricitymaps-contrib/master/geo/world.geojson")

# ---------------------------------------------------------------------------
# Zone table: electricitymaps geo zoneName -> (energy-charts bzn, country, label)
# country is used for /public_power and /cbpf (country-level endpoints).
# ---------------------------------------------------------------------------
ZONE_MAP = {
    "DE":      ("DE-LU", "de", "Germany / Luxembourg"),
    "LU":      ("DE-LU", "lu", "Luxembourg (DE-LU zone)"),
    "AT":      ("AT", "at", "Austria"),
    "BE":      ("BE", "be", "Belgium"),
    "FR":      ("FR", "fr", "France"),
    "FR-COR":  ("FR", "fr", "France (Corsica)"),
    "NL":      ("NL", "nl", "Netherlands"),
    "PL":      ("PL", "pl", "Poland"),
    "CZ":      ("CZ", "cz", "Czechia"),
    "DK-DK1":  ("DK1", "dk", "Denmark West (DK1)"),
    "DK-DK2":  ("DK2", "dk", "Denmark East (DK2)"),
    "HU":      ("HU", "hu", "Hungary"),
    "CH":      ("CH", "ch", "Switzerland"),
    "NO-NO1":  ("NO1", "no", "Norway Southeast (NO1)"),
    "NO-NO2":  ("NO2", "no", "Norway Southwest (NO2)"),
    "NO-NO3":  ("NO3", "no", "Norway Central (NO3)"),
    "NO-NO4":  ("NO4", "no", "Norway North (NO4)"),
    "NO-NO5":  ("NO5", "no", "Norway West (NO5)"),
    "SE-SE1":  ("SE1", "se", "Sweden North (SE1)"),
    "SE-SE2":  ("SE2", "se", "Sweden Mid-North (SE2)"),
    "SE-SE3":  ("SE3", "se", "Sweden Mid-South (SE3)"),
    "SE-SE4":  ("SE4", "se", "Sweden South (SE4)"),
    "FI":      ("FI", "fi", "Finland"),
    "ES":      ("ES", "es", "Spain"),
    "PT":      ("PT", "pt", "Portugal"),
    "GB":      (None, "uk", "Great Britain"),
    "IE":      ("IE(SEM)", "ie", "Ireland (SEM)"),
    "GB-NIR":  ("IE(SEM)", "ie", "Northern Ireland (SEM)"),
    "SK":      ("SK", "sk", "Slovakia"),
    "SI":      ("SI", "si", "Slovenia"),
    "HR":      ("HR", "hr", "Croatia"),
    "RO":      ("RO", "ro", "Romania"),
    "BG":      ("BG", "bg", "Bulgaria"),
    "GR":      ("GR", "gr", "Greece"),
    "EE":      ("EE", "ee", "Estonia"),
    "LV":      ("LV", "lv", "Latvia"),
    "LT":      ("LT", "lt", "Lithuania"),
    "IT-NO":   ("IT-North", "it", "Italy North"),
    "IT-CNO":  ("IT-Centre-North", "it", "Italy Centre-North"),
    "IT-CSO":  ("IT-Centre-South", "it", "Italy Centre-South"),
    "IT-SO":   ("IT-South", "it", "Italy South"),
    "IT-CA":   ("IT-Calabria", "it", "Italy Calabria"),
    "IT-SIC":  ("IT-Sicily", "it", "Italy Sicily"),
    "IT-SAR":  ("IT-Sardinia", "it", "Italy Sardinia"),
    # Balkans / others: try them; zones without data render grey.
    "RS":      ("RS", "rs", "Serbia"),
    "BA":      (None, "ba", "Bosnia & Herzegovina"),
    "ME":      ("ME", "me", "Montenegro"),
    "AL":      (None, "al", "Albania"),
    "MK":      (None, "mk", "North Macedonia"),
    "XK":      (None, "xk", "Kosovo"),
    "UA":      ("UA-BEI", "ua", "Ukraine"),
    "MD":      (None, "md", "Moldova"),
    "MT":      (None, "mt", "Malta"),
    "CY":      (None, "cy", "Cyprus"),
    # Islands without a separate energy-charts zone stay grey.
    "ES-CN-FV": (None, "es", "Spain (Fuerteventura)"),
    "ES-CN-GC": (None, "es", "Spain (Gran Canaria)"),
    "ES-CN-HI": (None, "es", "Spain (El Hierro)"),
    "ES-CN-IG": (None, "es", "Spain (La Gomera)"),
    "ES-CN-LP": (None, "es", "Spain (La Palma)"),
    "ES-CN-LZ": (None, "es", "Spain (Lanzarote)"),
    "ES-CN-TE": (None, "es", "Spain (Tenerife)"),
    "ES-IB-FO": (None, "es", "Spain (Formentera)"),
    "ES-IB-IZ": (None, "es", "Spain (Ibiza)"),
    "ES-IB-MA": (None, "es", "Spain (Mallorca)"),
    "ES-IB-ME": (None, "es", "Spain (Menorca)"),
    "ES-CE":    (None, "es", "Spain (Ceuta)"),
    "ES-ML":    (None, "es", "Spain (Melilla)"),
    "PT-AC":    (None, "pt", "Portugal (Azores)"),
    "PT-MA":    (None, "pt", "Portugal (Madeira)"),
    "UA-CR":    (None, "ua", "Crimea"),
}

# Countries to query for generation mix + cross-border flows
COUNTRIES = ["de", "at", "be", "fr", "nl", "pl", "cz", "dk", "hu", "ch",
             "no", "se", "fi", "it", "es", "pt", "uk", "ie", "sk", "si",
             "hr", "ro", "bg", "gr", "ee", "lv", "lt", "lu",
             "rs", "ba", "me", "mk", "xk", "ua", "md", "cy", "al"]

# Lifecycle GHG intensity gCO2eq/kWh (IPCC AR5 medians, same approach EM uses)
EMISSION_FACTORS = {
    "Nuclear": 12, "Hydro Run-of-River": 24, "Hydro water reservoir": 24,
    "Hydro pumped storage": 24, "Wind onshore": 11, "Wind offshore": 12,
    "Solar": 45, "Geothermal": 38, "Biomass": 230, "Waste": 600,
    "Fossil gas": 490, "Fossil oil": 650, "Fossil hard coal": 900,
    "Fossil brown coal / lignite": 1050, "Fossil coal-derived gas": 580,
}
# production_types entries that are not generation sources
MIX_EXCLUDE = {"Cross border electricity trading", "Load", "Residual load",
               "Renewable share of load", "Renewable share of generation",
               "Hydro pumped storage consumption"}

# ---------------------------------------------------------------------------
# Germany drill-down: SMARD realised generation per TSO control area
# (quarter-hour values are MWh per interval -> multiply by 4 for MW).
# ---------------------------------------------------------------------------
SMARD_REGIONS = ["50Hertz", "Amprion", "TenneT", "TransnetBW"]
SMARD_FILTERS = {
    1223: "lignite", 1224: "nuclear", 1225: "wind_offshore", 1226: "hydro",
    1227: "other", 1228: "other_renew", 4066: "biomass", 4067: "wind_onshore",
    4068: "solar", 4069: "hard_coal", 4070: "pumped", 4071: "gas",
}
SMARD_LOAD = 410  # realisierter Stromverbrauch (total net load)
# Bundesland -> (control area, share of control-area consumption)
DE_STATES = {
    "DE-BE": ("50Hertz", 0.203), "DE-BB": ("50Hertz", 0.143),
    "DE-HH": ("50Hertz", 0.104), "DE-MV": ("50Hertz", 0.088),
    "DE-SN": ("50Hertz", 0.225), "DE-ST": ("50Hertz", 0.121),
    "DE-TH": ("50Hertz", 0.115),
    "DE-NW": ("Amprion", 0.778), "DE-RP": ("Amprion", 0.178),
    "DE-SL": ("Amprion", 0.043),
    "DE-SH": ("TenneT", 0.095), "DE-HB": ("TenneT", 0.022),
    "DE-NI": ("TenneT", 0.254), "DE-HE": ("TenneT", 0.203),
    "DE-BY": ("TenneT", 0.425),
    "DE-BW": ("TransnetBW", 1.0),
}
DE_STATE_NAMES = {
    "DE-BW": "Baden-Württemberg", "DE-BY": "Bayern", "DE-BE": "Berlin",
    "DE-BB": "Brandenburg", "DE-HB": "Bremen", "DE-HH": "Hamburg",
    "DE-HE": "Hessen", "DE-MV": "Mecklenburg-Vorpommern",
    "DE-NI": "Niedersachsen", "DE-NW": "Nordrhein-Westfalen",
    "DE-RP": "Rheinland-Pfalz", "DE-SL": "Saarland",
    "DE-SN": "Sachsen", "DE-ST": "Sachsen-Anhalt",
    "DE-SH": "Schleswig-Holstein", "DE-TH": "Thüringen",
}

REQ_DELAY = 1.3  # seconds between API calls (server rate-limits bursts)


def get_json(path, retries=4, base=BASE):
    url = base + path
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "price-map/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 8 * (attempt + 1)
                print(f"    429, retry in {wait}s")
                time.sleep(wait)
                continue
            print(f"    HTTP {e.code} for {path}")
            return None
        except Exception as e:
            print(f"    {e} for {path}")
            time.sleep(3)
    return None


def daterange(days_back=4, days_fwd=2):
    import datetime
    end = datetime.date.today() + datetime.timedelta(days=days_fwd)
    start = end - datetime.timedelta(days=days_back + days_fwd)
    return start.isoformat(), end.isoformat()


def fetch_smard(now):
    """Realised generation per TSO control area + consumption.

    SMARD week files: index_quarterhour.json lists file-start timestamps;
    each file covers one week. Values are MWh per quarter-hour -> *4 = MW.
    Week files can carry stale index entries for discontinued fuels, so the
    per-filter index is only trusted when its last file still covers recent
    data.
    """
    regions = {}
    cutoff = now - 4 * 86400
    for region in SMARD_REGIONS:
        idx = get_json(f"/4068/{region}/index_quarterhour.json", base=SMARD)
        ts_file = idx["timestamps"][-1] if idx and idx.get("timestamps") else None
        maps, load_map = {}, {}
        for fid, key in SMARD_FILTERS.items():
            series = None
            if ts_file:
                d = get_json(f"/{fid}/{region}/{fid}_{region}_quarterhour_{ts_file}.json",
                             base=SMARD)
                if d and d.get("series"):
                    series = d["series"]
            if series is None:
                i2 = get_json(f"/{fid}/{region}/index_quarterhour.json", base=SMARD)
                if i2 and i2.get("timestamps"):
                    t2 = i2["timestamps"][-1]
                    if t2 / 1000 >= cutoff:
                        d = get_json(
                            f"/{fid}/{region}/{fid}_{region}_quarterhour_{t2}.json",
                            base=SMARD)
                        if d and d.get("series"):
                            series = d["series"]
            if series:
                maps[key] = {p[0] // 1000: p[1] * 4 for p in series
                             if p[1] is not None}
        if ts_file:
            d = get_json(f"/{SMARD_LOAD}/{region}/"
                         f"{SMARD_LOAD}_{region}_quarterhour_{ts_file}.json",
                         base=SMARD)
            if d and d.get("series"):
                load_map = {p[0] // 1000: p[1] * 4 for p in d["series"]
                            if p[1] is not None}
        if maps:
            grid = sorted({t for m in maps.values() for t in m}
                          | set(load_map))
            grid = [t for t in grid if t >= cutoff]
            if grid:
                entry = {"ts": grid,
                         "mix": {k: [round(m.get(t), 1) if m.get(t) is not None
                                     else None for t in grid]
                                 for k, m in maps.items()}}
                if load_map:
                    entry["load"] = [round(load_map[t], 1)
                                     if load_map.get(t) is not None else None
                                     for t in grid]
                regions[region] = entry
        print(f"    smard {region}: {len(maps)} fuels, "
              f"{len(regions.get(region, {}).get('ts', []))} quarter-hours")
    return regions


def main():
    os.makedirs(os.path.join(HERE, "geo"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)

    # ---- geometry ----------------------------------------------------------
    if not os.path.exists(GEO_RAW):
        print("downloading world.geojson ...")
        urllib.request.urlretrieve(GEO_URL, GEO_RAW)
    geo = json.load(open(GEO_RAW))
    keep = {f["properties"]["zoneName"]: f for f in geo["features"]
            if f["properties"].get("zoneName") in ZONE_MAP}
    print(f"geo: kept {len(keep)} European features")
    feats = []
    for name, f in keep.items():
        feats.append({"type": "Feature",
                      "properties": {"zoneName": name},
                      "geometry": round_geom(f["geometry"], 3)})
    json.dump({"type": "FeatureCollection", "features": feats},
              open(os.path.join(HERE, "geo", "zones.geojson"), "w"))

    start, end = daterange()

    # ---- prices -------------------------------------------------------------
    zones = {}
    bzns = sorted({bzn for bzn, _, _ in ZONE_MAP.values() if bzn})
    for i, bzn in enumerate(bzns):
        print(f"[{i+1}/{len(bzns)}] price {bzn}")
        d = get_json(f"/price?bzn={bzn}&start={start}&end={end}")
        if d and d.get("unix_seconds"):
            zones[bzn] = {"ts": d["unix_seconds"], "price": d["price"],
                          "unit": d.get("unit", "EUR / MWh")}
        else:
            zones[bzn] = None
        time.sleep(REQ_DELAY)

    # ---- country data -------------------------------------------------------
    countries = {}
    for i, c in enumerate(COUNTRIES):
        print(f"[{i+1}/{len(COUNTRIES)}] country {c}")
        mix = get_json(f"/public_power?country={c}&start={start}&end={end}")
        time.sleep(REQ_DELAY)
        flows = get_json(f"/cbpf?country={c}&start={start}&end={end}")
        time.sleep(REQ_DELAY)
        entry = {}
        if mix and mix.get("unix_seconds"):
            pts = {}
            for pt in mix["production_types"]:
                name = pt["name"]
                if name in MIX_EXCLUDE:
                    continue
                pts[name] = [max(0.0, v) if v is not None else None
                             for v in pt["data"]]
            entry["mix_ts"] = mix["unix_seconds"]
            entry["mix"] = pts
            entry["co2"] = carbon_series(mix["unix_seconds"], pts)
        if flows and flows.get("unix_seconds"):
            entry["cbpf_ts"] = flows["unix_seconds"]
            entry["cbpf"] = {nb["name"]: nb["data"]
                             for nb in flows.get("countries", [])
                             if nb["name"] != "sum"}
        countries[c] = entry or None

    # ---- Germany: SMARD per TSO control area --------------------------------
    print("smard TSO control areas")
    de_tso = fetch_smard(now=int(time.time()))

    # ---- unified 15-min timeline -------------------------------------------
    all_ts = sorted({t for z in zones.values() if z for t in z["ts"]})
    if not all_ts:
        raise SystemExit("no price data at all")
    # end = median of per-zone last timestamps (robust to partial series)
    lasts = sorted(z["ts"][-1] for z in zones.values() if z)
    t_end = lasts[len(lasts) // 2]
    # cap at now+12h so slider doesn't run far into day-ahead territory
    now = int(time.time())
    t_end = min(t_end, now - (now % 900) + 900)
    step = 900
    n = 288  # 72 h at 15-min
    timeline = [t_end - step * (n - 1 - i) for i in range(n)]

    zone_out = {}
    for em_name, (bzn, country, label) in ZONE_MAP.items():
        entry = {"bzn": bzn, "country": country, "label": label,
                 "price": None, "res": None}
        if bzn and zones.get(bzn):
            z = zones[bzn]
            series = dict(zip(z["ts"], z["price"]))
            diffs = [b - a for a, b in zip(z["ts"], z["ts"][1:])]
            entry["res"] = min(diffs) // 60 if diffs else 60
            entry["unit"] = z["unit"]
            arr, last, filled = [], None, 0
            for t in timeline:
                v = series.get(t)
                if v is None and last is not None and filled < 4:
                    v, filled = last, filled + 1
                elif v is not None:
                    last, filled = v, 0
                arr.append(round(v, 2) if v is not None else None)
            entry["price"] = arr
        zone_out[em_name] = entry

    out = {
        "generated_at": int(time.time()),
        "resolution_min": 15,
        "timeline": timeline,
        "zones": zone_out,
        "countries": countries,
        "de_tso": de_tso,
        "de_states": {z: {"name": DE_STATE_NAMES[z], "tso": tso,
                          "share": share}
                      for z, (tso, share) in DE_STATES.items()},
        "sources": {
            "prices": "api.energy-charts.info /price (day-ahead, CC BY 4.0 SMARD)",
            "mix": "api.energy-charts.info /public_power",
            "flows": "api.energy-charts.info /cbpf",
            "carbon": "estimated from generation mix, IPCC AR5 factors",
            "geometry": "electricitymaps-contrib geo/world.geojson",
            "de_regions": "smard.de realised generation + consumption per "
                          "TSO control area (Bundesnetzagentur)",
        },
    }
    path = os.path.join(HERE, "data", "data.json")
    json.dump(out, open(path, "w"), separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path)//1024} KB)")
    have = sum(1 for z in zone_out.values() if z["price"])
    print(f"zones with price data: {have}/{len(zone_out)}")


def carbon_series(ts, mix):
    out = []
    for i in range(len(ts)):
        num = den = 0.0
        for name, vals in mix.items():
            v = vals[i] if i < len(vals) else None
            if not v or v <= 0:
                continue
            f = EMISSION_FACTORS.get(name, 400)
            num += v * f
            den += v
        out.append(round(num / den) if den > 0 else None)
    return out


def round_geom(g, nd):
    def rec(x):
        if isinstance(x, (list, tuple)):
            return [rec(v) for v in x]
        return round(x, nd)
    return {"type": g["type"], "coordinates": rec(g["coordinates"])}


if __name__ == "__main__":
    main()
