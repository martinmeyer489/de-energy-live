"""Regional view: Germany's four TSO control areas via SMARD.

SMARD (Bundesnetzagentur) publishes the same realised-generation and
net-load series per control area as for Germany as a whole, so each zone
gets live signals: net load, generation mix, renewable share, an
estimated CO2 intensity and self-sufficiency (generation / load).

Zone -> federal-state mapping is approximate: control-area borders do
not follow state borders exactly (e.g. Rhineland-Palatinate and Lower
Saxony are each split between two zones). States are assigned to the
zone covering most of their area.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .model import TimeSeries
from .sources import smard

# Approximate Bundesland (ISO suffix) -> control area assignment
ZONES: dict[str, dict] = {
    "50Hertz": {
        "tso": "50Hertz Transmission",
        "states": ["BB", "BE", "HH", "MV", "SN", "ST", "TH"],
    },
    "Amprion": {
        "tso": "Amprion",
        "states": ["NW", "RP", "SL"],
    },
    "TenneT": {
        "tso": "TenneT TSO",
        "states": ["BY", "HB", "HE", "NI", "SH"],
    },
    "TransnetBW": {
        "tso": "TransnetBW",
        "states": ["BW"],
    },
}

RENEWABLE_LABELS = {
    "Wasserkraft (hydro)",
    "Sonstige Erneuerbare (other renewables)",
    "Biomasse (biomass)",
    "Wind Offshore",
    "Wind Onshore",
    "Photovoltaik (solar)",
}

# Lifecycle CO2 intensity per source, g CO2eq/kWh (IPCC-style estimates)
EMISSION_FACTORS = {
    "Braunkohle (lignite)": 1100,
    "Steinkohle (hard coal)": 900,
    "Sonstige Konventionelle (other conventional)": 700,
    "Erdgas (natural gas)": 490,
    "Sonstige Erneuerbare (other renewables)": 230,
    "Biomasse (biomass)": 230,
    "Photovoltaik (solar)": 45,
    "Wasserkraft (hydro)": 24,
    "Kernenergie (nuclear)": 12,
    "Wind Offshore": 11,
    "Wind Onshore": 11,
    "Pumpspeicher (pumped storage)": 0,
}

DEFAULT_EMISSION_FACTOR = 400  # fallback for sources without a factor

# Same lifecycle basis as EMISSION_FACTORS, keyed by the energy-charts
# production-type names used in the national mix (cli._generation_snapshot).
EC_EMISSION_FACTORS = {
    "Fossil brown coal / lignite": EMISSION_FACTORS["Braunkohle (lignite)"],
    "Fossil hard coal": EMISSION_FACTORS["Steinkohle (hard coal)"],
    "Fossil coal-derived gas": EMISSION_FACTORS["Steinkohle (hard coal)"],
    "Fossil oil": 650,
    "Fossil gas": EMISSION_FACTORS["Erdgas (natural gas)"],
    "Others": EMISSION_FACTORS["Sonstige Konventionelle (other conventional)"],
    "Waste": EMISSION_FACTORS["Biomasse (biomass)"],
    "Biomass": EMISSION_FACTORS["Biomasse (biomass)"],
    "Geothermal": 38,
    "Solar": EMISSION_FACTORS["Photovoltaik (solar)"],
    "Hydro Run-of-River": EMISSION_FACTORS["Wasserkraft (hydro)"],
    "Hydro water reservoir": EMISSION_FACTORS["Wasserkraft (hydro)"],
    "Hydro pumped storage": EMISSION_FACTORS["Pumpspeicher (pumped storage)"],
    "Nuclear": EMISSION_FACTORS["Kernenergie (nuclear)"],
    "Wind offshore": EMISSION_FACTORS["Wind Offshore"],
    "Wind onshore": EMISSION_FACTORS["Wind Onshore"],
}


def mix_co2_intensity(
    gen, factors: dict | None = None
) -> float | None:
    """Generation-weighted CO2 intensity (g/kWh) of a (name, mw) mix."""
    factors = factors or EMISSION_FACTORS
    total = weighted = 0.0
    for name, mw in gen:
        total += mw
        weighted += mw * factors.get(name, DEFAULT_EMISSION_FACTOR)
    return weighted / total if total else None


def _latest_before(ts: TimeSeries, before) -> float | None:
    hit = ts.latest(before)
    return hit[1] if hit else None


def zone_snapshot(zone: str, resolution: str = "quarterhour") -> dict:
    """Latest supply/demand picture for one control area."""
    load_ts = smard.fetch_net_load(resolution, zone)
    gen = smard.fetch_generation(resolution, zone)

    hits = [ts.latest() for ts in gen.values()]
    hits = [h for h in hits if h]
    as_of = max((t for t, _ in hits), default=None)
    load_hit = load_ts.latest(as_of)

    rows = []
    for name, ts in gen.items():
        v = _latest_before(ts, as_of)
        if v is not None:
            rows.append(
                {
                    "name": name,
                    "mw": round(v, 1),
                    "renewable": name in RENEWABLE_LABELS,
                    "co2_gkwh": EMISSION_FACTORS.get(
                        name, DEFAULT_EMISSION_FACTOR
                    ),
                }
            )
    rows.sort(key=lambda r: -r["mw"])

    total = sum(r["mw"] for r in rows)
    ren = sum(r["mw"] for r in rows if r["renewable"])
    co2 = mix_co2_intensity((r["name"], r["mw"]) for r in rows)
    load_mw = load_hit[1] if load_hit else None

    return {
        "zone": zone,
        "tso": ZONES[zone]["tso"],
        "states": ZONES[zone]["states"],
        "as_of": as_of.isoformat() if as_of else None,
        "load_mw": round(load_mw, 1) if load_mw is not None else None,
        "load_as_of": load_hit[0].isoformat() if load_hit else None,
        "total_generation_mw": round(total, 1) if rows else None,
        "renewable_share_pct": round(100 * ren / total, 1) if total else None,
        "co2_intensity_gkwh": round(co2, 0) if co2 else None,
        "self_sufficiency_pct": (
            round(100 * total / load_mw, 1) if load_mw else None
        ),
        "generation": rows,
    }


def all_zones(resolution: str = "quarterhour") -> dict:
    """Snapshots for all four control areas, fetched in parallel."""
    zones: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            z: ex.submit(zone_snapshot, z, resolution) for z in ZONES
        }
        errors = {}
        for z, f in futures.items():
            try:
                zones[z] = f.result()
            except Exception as e:  # keep partial map usable
                errors[z] = str(e)
    return {"zones": zones, "errors": errors}
