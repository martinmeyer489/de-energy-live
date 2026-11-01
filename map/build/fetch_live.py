#!/usr/bin/env python3
"""Overlay live data onto data/data.json.

Sources (all public, no API key):
  - api.energy-charts.info (Fraunhofer ISE / SMARD, CC BY 4.0):
      /price        day-ahead spot price per bidding zone
      /public_power generation mix + load per country
      /cbpf         cross-border physical flows per country
  - smard.de (Bundesnetzagentur) keyless chart API:
      per-TSO-control-area (50Hertz/Amprion/TenneT/TransnetBW) generation by
      source + realised consumption — the regional Germany layer.

Run build_zones.py first: it writes the fallback snapshot + zone meta
(names, centers, tso) that this script enriches. Zones without live data
keep their bundled snapshot values.

Series convention: for each of the 24 slider hours (UTC hour-of-day) we keep
the value at the most recent timestamp carrying that UTC hour — for prices
that includes day-ahead values, for generation/load the latest observation.
"""
import json, os, time, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data", "data.json")
EC = "https://api.energy-charts.info"
SMARD = "https://www.smard.de/app/chart_data"
UA = {"User-Agent": "electricity-map/1.0"}

# ---------------------------------------------------------------- zone maps
# energy-charts bidding-zone id per map zone (None -> no live price there)
PRICE_BZN = {
    "AUT": "AT", "LUX": "DE-LU", "FRA": "FR", "ESP": "ES", "PRT": "PT",
    "NLD": "NL", "BEL": "BE", "POL": "PL", "CZE": "CZ", "SVK": "SK",
    "HUN": "HU", "ROU": "RO", "BGR": "BG", "GRC": "GR", "HRV": "HR",
    "SVN": "SI", "FIN": "FI", "EST": "EE", "LVA": "LV", "LTU": "LT",
    "CHE": "CH", "UKR": "UA-BEI", "SRB": "RS",
    "IT-NORD": "IT-North", "IT-CNOR": "IT-Centre-North",
    "IT-CSUD": "IT-Centre-South", "IT-SUD": "IT-South",
    "IT-SICI": "IT-Sicily", "IT-SARD": "IT-Sardinia",
    "NO1": "NO1", "NO2": "NO2", "NO3": "NO3", "NO4": "NO4", "NO5": "NO5",
    "SE1": "SE1", "SE2": "SE2", "SE3": "SE3", "SE4": "SE4",
    "DK1": "DK1", "DK2": "DK2",
}
for z in ("DE-BW DE-BY DE-BE DE-BB DE-HB DE-HH DE-HE DE-MV DE-NI DE-NW "
          "DE-RP DE-SL DE-SN DE-ST DE-SH DE-TH").split():
    PRICE_BZN[z] = "DE-LU"

# energy-charts country code for /public_power + /cbpf
ZONE_COUNTRY = {
    "AUT": "at", "LUX": "lu", "FRA": "fr", "ESP": "es", "PRT": "pt",
    "GBR": "uk", "SEM": "ie", "NLD": "nl", "BEL": "be", "POL": "pl",
    "CZE": "cz", "SVK": "sk", "HUN": "hu", "ROU": "ro", "BGR": "bg",
    "GRC": "gr", "HRV": "hr", "SVN": "si", "FIN": "fi", "EST": "ee",
    "LVA": "lv", "LTU": "lt", "CHE": "ch", "UKR": "ua", "SRB": "rs",
    "IT-NORD": "it", "IT-CNOR": "it", "IT-CSUD": "it", "IT-SUD": "it",
    "IT-SICI": "it", "IT-SARD": "it",
    "NO1": "no", "NO2": "no", "NO3": "no", "NO4": "no", "NO5": "no",
    "SE1": "se", "SE2": "se", "SE3": "se", "SE4": "se",
    "DK1": "dk", "DK2": "dk",
}
COUNTRIES = sorted(set(ZONE_COUNTRY.values()) | {"de"})

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
TSO_REGION = {"50Hertz": "50Hertz", "Amprion": "Amprion",
              "TenneT": "TenneT", "TransnetBW": "TransnetBW"}

# SMARD filter ids (realised generation, per region)
SMARD_FILTERS = {
    1223: "lignite", 1224: "nuclear", 1225: "wind_offshore", 1226: "hydro",
    1227: "other", 1228: "other_renew", 4066: "biomass", 4067: "wind_onshore",
    4068: "solar", 4069: "hard_coal", 4070: "pumped", 4071: "gas",
}
SMARD_LOAD = 410  # realisierter Stromverbrauch

# energy-charts production type -> canonical mix key
EC_MIX = {
    "Hydro Run-of-River": "hydro", "Hydro water reservoir": "hydro",
    "Hydro pumped storage": "pumped", "Wind onshore": "wind_onshore",
    "Wind offshore": "wind_offshore", "Solar": "solar", "Nuclear": "nuclear",
    "Fossil brown coal / lignite": "lignite", "Fossil hard coal": "hard_coal",
    "Fossil gas": "gas", "Fossil oil": "oil",
    "Fossil coal-derived gas": "gas", "Biomass": "biomass", "Waste": "waste",
    "Geothermal": "other", "Others": "other",
}
RENEWABLE = {"wind_onshore", "wind_offshore", "solar", "hydro", "biomass",
             "other_renew"}
EMISSION = {"nuclear": 12, "hydro": 24, "pumped": 24, "wind_onshore": 11,
            "wind_offshore": 12, "solar": 45, "other": 380,
            "other_renew": 200, "biomass": 230, "waste": 600, "gas": 490,
            "oil": 650, "hard_coal": 900, "lignite": 1050, "coal": 950}

# German side zone per neighbour country (cbpf neighbour display name)
DE_BORDER = {
    "Denmark": ("DE-SH", "DK1"), "Norway": ("DE-SH", "NO2"),
    "Sweden": ("DE-MV", "SE4"), "Poland": ("DE-BB", "POL"),
    "Czech Republic": ("DE-SN", "CZE"), "Austria": ("DE-BY", "AUT"),
    "Switzerland": ("DE-BW", "CHE"), "France": ("DE-SL", "FRA"),
    "Luxembourg": ("DE-RP", "LUX"), "Netherlands": ("DE-NW", "NLD"),
    "Belgium": ("DE-NW", "BEL"),
}
# our zone for the *queried* country when it has subzones (arrow placement)
SIDE_ZONE = {
    "it": {"France": "IT-NORD", "Switzerland": "IT-NORD",
           "Austria": "IT-NORD", "Slovenia": "IT-NORD"},
    "no": {"Sweden": "NO1", "Denmark": "NO2", "Germany": "NO2",
           "Netherlands": "NO2", "Great Britain": "NO2", "Finland": "NO4",
           "Russia": "NO4"},
    "se": {"Norway": "SE2", "Denmark": "SE4", "Germany": "SE4",
           "Poland": "SE4", "Lithuania": "SE4", "Finland": "SE3"},
    "dk": {"Germany": "DK1", "Sweden": "DK2", "Norway": "DK1",
           "Netherlands": "DK1", "Great Britain": "DK2", "Poland": "DK2"},
}
DEFAULT_SIDE = {"it": "IT-NORD", "no": "NO2", "se": "SE3", "dk": "DK1"}
# neighbour country display name -> our whole-country zone id
NAME2ZONE = {
    "France": "FRA", "Spain": "ESP", "Portugal": "PRT",
    "Netherlands": "NLD", "Belgium": "BEL", "Poland": "POL",
    "Czech Republic": "CZE", "Slovakia": "SVK", "Hungary": "HUN",
    "Romania": "ROU", "Bulgaria": "BGR", "Greece": "GRC",
    "Croatia": "HRV", "Slovenia": "SVN", "Finland": "FIN",
    "Estonia": "EST", "Latvia": "LVA", "Lithuania": "LTU",
    "Ireland": "SEM", "Great Britain": "GBR", "Northern Ireland": "SEM",
    "Austria": "AUT", "Switzerland": "CHE", "Luxembourg": "LUX",
    "Italy": "IT-NORD", "Serbia": "SRB", "Ukraine": "UKR",
    "Turkey": "TUR", "Morocco": "MAR", "Russia": None,
    "Germany": "DE-BW", "Denmark": "DK1", "Norway": "NO2",
    "Sweden": "SE4",
}
CBPF_COUNTRIES = ["de", "fr", "es", "pt", "it", "nl", "be", "at", "ch",
                  "pl", "cz", "sk", "hu", "ro", "bg", "gr", "dk", "no",
                  "se", "fi", "hr", "si", "ee", "lv", "lt", "ie", "uk"]

REQ_DELAY = 1.1


def get_json(url, retries=4, delay=REQ_DELAY):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < retries - 1:
                time.sleep(8 * (a + 1)); continue
            print("    HTTP", e.code, url[:100]); return None
        except Exception as e:
            print("    ", e, url[:100]); time.sleep(3)
    return None


def hour_of(sec):
    return datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc).hour


def latest_per_hour(secs, vals, ms=False):
    """dict utc-hour -> value at the most recent timestamp with that hour."""
    out = {}
    for t, v in zip(secs, vals):
        if v is None:
            continue
        s = t / 1000 if ms else t
        h = hour_of(s)
        if h not in out or s > out[h][0]:
            out[h] = (s, v)
    return {h: v for h, (s, v) in out.items()}


def to24(byhour, fallback=None):
    return [round(byhour.get(h, fallback), 1)
            if byhour.get(h, fallback) is not None else None
            for h in range(24)]


def main():
    data = json.load(open(DATA))
    zones = data["zones"]
    now = int(time.time())
    start = (datetime.datetime.fromtimestamp(now - 8 * 86400,
             tz=datetime.timezone.utc).date().isoformat())
    end = (datetime.datetime.fromtimestamp(now + 2 * 86400,
           tz=datetime.timezone.utc).date().isoformat())
    print("range", start, "->", end)

    # ------------------------------------------------ 1. prices per bzn
    bzn_needed = sorted(set(PRICE_BZN.values()))
    price24 = {}   # bzn -> {hour: EUR/MWh}
    for i, b in enumerate(bzn_needed):
        d = get_json(f"{EC}/price?bzn={b}&start={start}&end={end}")
        if d and d.get("unix_seconds"):
            price24[b] = latest_per_hour(d["unix_seconds"], d["price"])
        time.sleep(REQ_DELAY)
        print(f"[{i+1}/{len(bzn_needed)}] price {b}: "
              f"{len(price24.get(b) or {})} h")

    # ------------------------------------- 2. mix + load per country
    country_mix = {}   # country -> {key: {hour: MW}}
    country_load = {}
    for i, c in enumerate(COUNTRIES):
        d = get_json(f"{EC}/public_power?country={c}&start={start}&end={end}")
        if d and d.get("unix_seconds"):
            ts = d["unix_seconds"]
            mix = {}
            for pt in d["production_types"]:
                k = EC_MIX.get(pt["name"])
                if k:
                    byh = latest_per_hour(ts, pt["data"])
                    dst = mix.setdefault(k, {})
                    for h, v in byh.items():
                        dst[h] = dst.get(h, 0) + max(0.0, v)
                elif pt["name"] == "Load":
                    country_load[c] = latest_per_hour(ts, pt["data"])
            country_mix[c] = mix
        time.sleep(REQ_DELAY)
        print(f"[{i+1}/{len(COUNTRIES)}] public_power {c}: "
              f"{len(country_mix.get(c) or {})} src")

    # --------------------------------------- 3. SMARD per control area
    tso_mix, tso_load = {}, {}
    for region in TSO_REGION:
        idx = get_json(f"{SMARD}/4068/{region}/index_hour.json", delay=0)
        ts_file = idx["timestamps"][-1] if idx and idx.get("timestamps") else None
        mix, load = {}, {}
        for fid, key in SMARD_FILTERS.items():
            path = f"{fid}/{region}"
            ok = False
            if ts_file:
                d = get_json(f"{SMARD}/{path}/{fid}_{region}_hour_{ts_file}.json",
                             delay=0)
                if d and d.get("series"):
                    mix[key] = latest_per_hour(
                        [s[0] for s in d["series"]],
                        [s[1] for s in d["series"]], ms=True)
                    ok = True
            if not ok:
                i2 = get_json(f"{SMARD}/{path}/index_hour.json", delay=0)
                if i2 and i2.get("timestamps"):
                    t2 = i2["timestamps"][-1]
                    d = get_json(f"{SMARD}/{path}/{fid}_{region}_hour_{t2}.json",
                                 delay=0)
                    if d and d.get("series"):
                        mix[key] = latest_per_hour(
                            [s[0] for s in d["series"]],
                            [s[1] for s in d["series"]], ms=True)
        d = get_json(f"{SMARD}/{SMARD_LOAD}/{region}/"
                     f"{SMARD_LOAD}_{region}_hour_{ts_file}.json", delay=0)
        if d and d.get("series"):
            load = latest_per_hour([s[0] for s in d["series"]],
                                   [s[1] for s in d["series"]], ms=True)
        tso_mix[region], tso_load[region] = mix, load
        print(f"smard {region}: {len(mix)} src, load {len(load)} h")

    # ------------------------------------------------ 4. cross-border flows
    pair_dir = {}    # frozenset(zoneA,zoneB) -> (a, b, signed MW a->b)
    for i, c in enumerate(CBPF_COUNTRIES):
        d = get_json(f"{EC}/cbpf?country={c}&start={start}&end={end}")
        if d and d.get("unix_seconds"):
            ts = d["unix_seconds"]
            for nb in d.get("countries", []):
                n = nb["name"]
                if n == "sum":
                    continue
                # median of the last 8 non-null points: the newest samples
                # are often only partially published (trailing zeros)
                tail = [v for v in nb["data"] if v is not None][-8:]
                if not tail:
                    continue
                v = sorted(tail)[len(tail) // 2]
                if n == "Germany":
                    # German borders are already covered by the de query
                    continue
                if c == "de":
                    pair = DE_BORDER.get(n)
                    if not pair:
                        continue
                    a, b = pair
                else:
                    a = SIDE_ZONE.get(c, {}).get(n) or \
                        DEFAULT_SIDE.get(c) or \
                        next((z for z, cc in ZONE_COUNTRY.items()
                              if cc == c), None)
                    b = NAME2ZONE.get(n)
                    if not a or not b:
                        continue
                if a not in zones or b not in zones:
                    continue
                key = frozenset((a, b))
                # cbpf positive = import into queried country c
                mw_a_to_b = -v * 1000.0
                if key not in pair_dir:
                    pair_dir[key] = (a, b, mw_a_to_b)
        time.sleep(REQ_DELAY)
        print(f"[{i+1}/{len(CBPF_COUNTRIES)}] cbpf {c}")

    # ------------------------------------------------ 5. compose zones
    def signals(mix_by_h):
        """carbon/renewable/lowcarbon per hour from MW mix per source."""
        carbon, ren, lowc = {}, {}, {}
        hours = set()
        for dst in mix_by_h.values():
            hours |= set(dst)
        for h in hours:
            num = den = r = lc = 0.0
            for k, dst in mix_by_h.items():
                v = dst.get(h)
                if not v or v <= 0:
                    continue
                f = EMISSION.get(k, 400)
                num += v * f; den += v
                if k in RENEWABLE:
                    r += v
                if k in RENEWABLE or k == "nuclear":
                    lc += v
            if den > 0:
                carbon[h] = num / den
                ren[h] = min(100.0, r / den * 100)
                lowc[h] = min(100.0, lc / den * 100)
        return carbon, ren, lowc

    def mix_share(mix_by_h, h):
        tot = sum(d.get(h, 0) or 0 for d in mix_by_h.values())
        if tot <= 0:
            return None, None
        share = {k: round(d.get(h, 0) / tot * 100, 1)
                 for k, d in mix_by_h.items() if d.get(h, 0) > 0}
        mw = {k: round(d.get(h, 0)) for k, d in mix_by_h.items()
              if d.get(h, 0) > 0}
        return share, mw

    cur_h = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).hour
    live = 0
    for zid, z in zones.items():
        p24 = price24.get(PRICE_BZN.get(zid, ""), {})
        est = []
        if zid in DE_STATES:
            tso, share = DE_STATES[zid]
            mix = tso_mix.get(tso, {})
            carbon, ren, lowc = signals(mix)
            share_now, mw_now = mix_share(mix, cur_h)
            ca_load = tso_load.get(tso, {})
            load_now = ca_load.get(cur_h) or \
                (ca_load[max(ca_load)] if ca_load else None)
            if mix:
                z["mix"] = share_now or z["mix"]
                if mw_now:
                    z["mixMW"] = mw_now
                z["carbon"] = round(carbon.get(cur_h, z["carbon"]))
                z["renewable"] = round(ren.get(cur_h, z["renewable"]), 1)
                z["lowCarbon"] = round(lowc.get(cur_h, z["lowCarbon"]))
                z["series"]["carbon"] = to24(carbon, z["carbon"])
                z["series"]["renewable"] = to24(ren, z["renewable"])
                z["tsoGenMW"] = round(sum(mw_now.values())) if mw_now else None
            if load_now:
                z["demandGW"] = round(load_now * share / 1000, 1)
                z["tsoLoadGW"] = round(load_now / 1000, 1)
                est.append("demandGW")
            z["tso"] = tso
            z["sources"] = (f"SMARD live: {tso} control area (generation mix, "
                            "load) · price DE-LU")
        else:
            c = ZONE_COUNTRY.get(zid)
            mix = country_mix.get(c) if c else None
            if mix:
                carbon, ren, lowc = signals(mix)
                share_now, mw_now = mix_share(mix, cur_h)
                if share_now:
                    z["mix"] = share_now
                    z["mixMW"] = mw_now
                z["carbon"] = round(carbon.get(cur_h, z["carbon"]))
                z["renewable"] = round(ren.get(cur_h, z["renewable"]), 1)
                z["lowCarbon"] = round(lowc.get(cur_h, z["lowCarbon"]))
                z["series"]["carbon"] = to24(carbon, z["carbon"])
                z["series"]["renewable"] = to24(ren, z["renewable"])
                subz = sum(1 for zz, cc in ZONE_COUNTRY.items() if cc == c) > 1
                if c and c in country_load and country_load[c]:
                    ld = country_load[c].get(cur_h) or \
                        country_load[c][max(country_load[c])]
                    if ld:
                        if subz:
                            total = sum(zones[z2]["demandGW"] for z2, cc in
                                        ZONE_COUNTRY.items() if cc == c)
                            z["demandGW"] = round(
                                ld / 1000 * z["demandGW"] / (total or 1), 1)
                            est.append("demandGW")
                        else:
                            z["demandGW"] = round(ld / 1000, 1)
                z["sources"] = ("energy-charts live: /public_power (mix, "
                                "load) · /price")
        if p24:
            z["price"] = round(p24.get(cur_h, list(p24.values())[-1]), 1)
            z["series"]["price"] = to24(p24, z["price"])
        if p24 or zid in DE_STATES or ZONE_COUNTRY.get(zid) in country_mix:
            z["live"] = True
            live += 1
        if est:
            z["estimated"] = est
        if zid in DE_STATES:
            z.setdefault("estimated", [])
            if "demandGW" not in z["estimated"]:
                z["estimated"].append("demandGW")
            src = z.get("sources", "")
            z["sources"] = src + (" · state demand estimated by population"
                                  " share")
        elif est:
            z["sources"] = z.get("sources", "") + " · est: " + ",".join(est)

    # ------------------------------------------------ 6. exchanges
    new_exch = []
    for key, (a, b, mw) in pair_dir.items():
        if abs(mw) >= 30:
            new_exch.append({"f": a, "t": b, "mw": round(mw)})
    # pairs with real data (even ~0) suppress the static fallback
    covered = set(pair_dir)
    for e in data["exchanges"]:
        if frozenset((e["f"], e["t"])) not in covered:
            new_exch.append(e)
    data["exchanges"] = new_exch

    data["generatedAt"] = datetime.datetime.fromtimestamp(
        now, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["note"] = ("Live data: prices/mix/flows via api.energy-charts.info "
                    "(CC BY 4.0 / SMARD); German Bundesländer generation + "
                    "load via smard.de per TSO control area. Carbon intensity "
                    "computed from mix (IPCC AR5 factors). Non-European zones "
                    "are a bundled snapshot.")
    tmp = DATA + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, DATA)
    print(f"zones live: {live}/{len(zones)}  exchanges: {len(new_exch)}")
    for zid in sorted(DE_STATES):
        z = zones[zid]
        print(f"  {zid} {z['tso']:<10} CO2={z['carbon']:>4} "
              f"ren={z['renewable']:>5}% P={z['price']} "
              f"demand={z['demandGW']}GW est={z.get('estimated')}")


if __name__ == "__main__":
    main()
