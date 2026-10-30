"""SMARD (smard.de) — official Bundesnetzagentur market data API.

Two-step protocol: fetch the index file for available block timestamps,
then fetch the data file for the newest block. Resolution 'hour' covers
the last week of hourly data; 'quarterhour' the last ~3 days.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from ..model import TimeSeries

BASE = "https://www.smard.de/app/chart_data"
REGION = "DE"

# SMARD filter IDs — realised generation / consumption
FILTER_NET_LOAD = 410  # Gesamte Netzlast (total grid load), demand
GENERATION_FILTERS = {
    1223: "Braunkohle (lignite)",
    1224: "Kernenergie (nuclear)",
    1225: "Wind Offshore",
    1226: "Wasserkraft (hydro)",
    1227: "Sonstige Konventionelle (other conventional)",
    1228: "Sonstige Erneuerbare (other renewables)",
    4066: "Biomasse (biomass)",
    4067: "Wind Onshore",
    4068: "Photovoltaik (solar)",
    4069: "Steinkohle (hard coal)",
    4070: "Pumpspeicher (pumped storage)",
    4071: "Erdgas (natural gas)",
}


def _get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "de-energy-live/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# SMARD reports energy per interval in MWh, not average power.
# Divide by the interval length to get MW.
INTERVAL_HOURS = {"quarterhour": 0.25, "hour": 1.0, "day": 24.0}


def _fetch_series(filter_id: int, resolution: str) -> TimeSeries:
    index = _get(f"{BASE}/{filter_id}/{REGION}/index_{resolution}.json")
    latest_block = index["timestamps"][-1]
    data = _get(
        f"{BASE}/{filter_id}/{REGION}/{filter_id}_{REGION}_{resolution}_{latest_block}.json"
    )
    return parse_series(
        data["series"],
        name=f"SMARD filter {filter_id}",
        interval_hours=INTERVAL_HOURS.get(resolution, 1.0),
    )


def parse_series(
    series: list[list], name: str = "", interval_hours: float = 1.0
) -> TimeSeries:
    """Parse SMARD [epoch_ms, MWh] pairs into average MW per interval."""
    return TimeSeries(
        name=name,
        unit="MW",
        timestamps=[datetime.fromtimestamp(ms / 1000, tz=timezone.utc) for ms, _ in series],
        values=[
            None if v is None else float(v) / interval_hours for _, v in series
        ],
    )


def fetch_net_load(resolution: str = "quarterhour") -> TimeSeries:
    """Total German grid load (demand) in MW — SMARD filter 410."""
    ts = _fetch_series(FILTER_NET_LOAD, resolution)
    ts.name = "Netzlast (grid load)"
    return ts


def fetch_generation(resolution: str = "hour") -> dict[str, TimeSeries]:
    """Realised generation by source — 12 SMARD filter series."""
    out: dict[str, TimeSeries] = {}
    for fid, label in GENERATION_FILTERS.items():
        try:
            ts = _fetch_series(fid, resolution)
        except Exception:
            continue  # individual filters can lag; skip failures
        ts.name = label
        out[label] = ts
    return out
