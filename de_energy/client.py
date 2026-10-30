"""Client for the public energy-charts.info API (Fraunhofer ISE).

Data source: https://energy-charts.info — near-real-time German power
generation, load, prices and cross-border flows. No API key required.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.energy-charts.info"
BERLIN = ZoneInfo("Europe/Berlin")

# Non-generator series returned by /total_power alongside the real sources.
# /public_power calls the demand series "Load", /total_power calls it
# "Load (incl. self-consumption)".
LOAD_NAMES = ("Load", "Load (incl. self-consumption)")
META_SERIES = {
    *LOAD_NAMES,
    "Residual load",
    "Renewable share of load",
    "Renewable share of generation",
    "Cross border electricity trading",
    "Hydro pumped storage consumption",
}

RENEWABLE_SOURCES = {
    "Solar",
    "Wind onshore",
    "Wind offshore",
    "Hydro Run-of-River",
    "Hydro water reservoir",
    "Biomass",
    "Geothermal",
    "Waste",
}


@dataclass
class PowerData:
    unix_seconds: list[float]
    series: dict[str, list[float | None]]

    def times(self) -> list[datetime]:
        return [datetime.fromtimestamp(t, tz=BERLIN) for t in self.unix_seconds]

    def latest_index(self) -> int:
        """Index of the newest sample at or before now."""
        now = time.time()
        idx = 0
        for i, t in enumerate(self.unix_seconds):
            if t <= now:
                idx = i
        return idx

    def latest_data_index(self) -> int:
        """Newest index with at least one non-null generation value."""
        for i in range(len(self.unix_seconds) - 1, -1, -1):
            if any(
                vals[i] is not None
                for name, vals in self.series.items()
                if name not in META_SERIES and i < len(vals)
            ):
                return i
        return 0

    def generation_sources(self) -> dict[str, list[float | None]]:
        return {k: v for k, v in self.series.items() if k not in META_SERIES}

    def daily_totals_gwh(self) -> dict[str, float]:
        """Integrate each series over the returned day (15-min steps)."""
        out: dict[str, float] = {}
        for name, values in self.series.items():
            if name in META_SERIES:
                continue
            mwh = sum(v for v in values if v is not None) * 0.25
            out[name] = mwh / 1000.0
        return out


@dataclass
class PriceData:
    unix_seconds: list[float]
    price: list[float | None]
    unit: str

    def latest(self) -> tuple[datetime, float] | None:
        now = time.time()
        idx = 0
        for i, t in enumerate(self.unix_seconds):
            if t <= now:
                idx = i
        for i in range(idx, -1, -1):
            if self.price[i] is not None:
                return datetime.fromtimestamp(self.unix_seconds[i], tz=BERLIN), self.price[i]
        return None


def _get(path: str, **params: Any) -> dict:
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _today() -> str:
    return datetime.now(BERLIN).date().isoformat()


def fetch_total_power(
    start: str | None = None, end: str | None = None, country: str = "de"
) -> PowerData:
    """Total generation incl. non-public plants, plus load / residual load."""
    payload = _get(
        "/total_power", country=country, start=start or _today(), end=end or _today()
    )
    return PowerData(
        unix_seconds=payload.get("unix_seconds", []),
        series={p["name"]: p["data"] for p in payload.get("production_types", [])},
    )


def fetch_public_power(
    start: str | None = None, end: str | None = None, country: str = "de"
) -> PowerData:
    """Generation feeding the public grid (excl. industrial self-supply)."""
    payload = _get(
        "/public_power", country=country, start=start or _today(), end=end or _today()
    )
    return PowerData(
        unix_seconds=payload.get("unix_seconds", []),
        series={p["name"]: p["data"] for p in payload.get("production_types", [])},
    )


def fetch_signal(country: str = "de") -> dict:
    """Day forecast: renewable share of load and green/red signal per 15 min."""
    return _get("/signal", country=country)


def fetch_price(
    start: str | None = None, end: str | None = None, bzn: str = "DE-LU"
) -> PriceData:
    """Day-ahead spot price for a bidding zone."""
    payload = _get("/price", bzn=bzn, start=start or _today(), end=end or _today())
    return PriceData(
        unix_seconds=payload.get("unix_seconds", []),
        price=payload.get("price", []),
        unit=payload.get("unit", "EUR/MWh"),
    )
