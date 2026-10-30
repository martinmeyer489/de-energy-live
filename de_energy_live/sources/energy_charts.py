"""Fraunhofer ISE energy-charts.info public API (no key required).

Endpoint used: /total_power — German public power generation by source,
plus load, residual load and renewable shares. 15-minute resolution.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, timezone

from ..model import TimeSeries

API = "https://api.energy-charts.info"
BIDDING_ZONE = "DE-LU"

# Production types that count as generation (MW)
GENERATION_TYPES = {
    "Nuclear",
    "Hydro Run-of-River",
    "Biomass",
    "Fossil brown coal / lignite",
    "Fossil hard coal",
    "Fossil oil",
    "Fossil coal-derived gas",
    "Fossil gas",
    "Geothermal",
    "Hydro water reservoir",
    "Hydro pumped storage",
    "Others",
    "Waste",
    "Wind offshore",
    "Wind onshore",
    "Solar",
}

RENEWABLE_TYPES = {
    "Hydro Run-of-River",
    "Biomass",
    "Geothermal",
    "Hydro water reservoir",
    "Wind offshore",
    "Wind onshore",
    "Solar",
    "Waste",  # ~50% biogenic; energy-charts counts it as renewable
}


def _get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "de-energy-live/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_power_mix(start: date, end: date) -> dict[str, TimeSeries]:
    """Fetch the full German power picture between two dates.

    Returns a dict keyed by production type name -> TimeSeries, including
    'Load (incl. self-consumption)', 'Residual load' and the two
    'Renewable share of ...' percentage series.
    """
    url = (
        f"{API}/total_power?bzn={BIDDING_ZONE}"
        f"&start={start.isoformat()}&end={end.isoformat()}"
    )
    payload = _get(url)
    return parse_power_mix(payload)


def parse_power_mix(payload: dict) -> dict[str, TimeSeries]:
    stamps = [datetime.fromtimestamp(s, tz=timezone.utc) for s in payload["unix_seconds"]]
    out: dict[str, TimeSeries] = {}
    for pt in payload.get("production_types", []):
        unit = "%" if pt["name"].startswith("Renewable share") else "MW"
        out[pt["name"]] = TimeSeries(
            name=pt["name"],
            unit=unit,
            timestamps=list(stamps),
            values=[None if v is None else float(v) for v in pt["data"]],
        )
    return out


def fetch_price(start: date, end: date) -> TimeSeries:
    """Day-ahead spot price for the DE-LU bidding zone (EUR/MWh)."""
    url = f"{API}/price?bzn={BIDDING_ZONE}&start={start.isoformat()}&end={end.isoformat()}"
    payload = _get(url)
    return TimeSeries(
        name="Day-ahead price",
        unit=payload.get("unit", "EUR/MWh"),
        timestamps=[
            datetime.fromtimestamp(s, tz=timezone.utc)
            for s in payload["unix_seconds"]
        ],
        values=[None if v is None else float(v) for v in payload["price"]],
    )


def fetch_signal() -> dict:
    """Day forecast: renewable share of load and green/red signal per 15 min."""
    return _get(f"{API}/signal?country=de")


def total_generation(mix: dict[str, TimeSeries], before: datetime | None = None) -> float | None:
    """Sum the latest values of all generation series (excludes consumption lines)."""
    total = 0.0
    found = False
    for name, ts in mix.items():
        if name not in GENERATION_TYPES:
            continue
        latest = ts.latest(before)
        if latest:
            total += latest[1]
            found = True
    return total if found else None
