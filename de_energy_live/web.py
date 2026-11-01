"""JSON API for German electricity supply, demand & carbon.

Pure stdlib (http.server). Serves:
  /api/data    current snapshot as JSON (incl. national CO2 estimate)
  /api/regions per-control-area snapshot as JSON
  /api/mastr   largest registered units per group (MaStR cache)
  /api/mastr/capacity   installed capacity per source (In Betrieb)
  /api/mastr/batteries  paginated battery-storage units
  /api/mastr/map?type=X per-Bundesland aggregation + geolocated
               markers for one unit layer (batteries, solar, wind, ...)
  /geo/states  Bundesländer GeoJSON used by the map
  /chart.png   stacked generation vs load chart (matplotlib, if installed)
  /healthz     liveness probe
"""
from __future__ import annotations

import io
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from . import regional
from .cli import MASTR_TOP_GROUPS, _generation_snapshot, _latest, _today
from .sources import energy_charts, mastr

BERLIN = ZoneInfo("Europe/Berlin")
CACHE_TTL = 55  # seconds; upstream resolves at 15 min anyway
REGIONS_TTL = 300  # SMARD control-area data resolves at 15 min
MASTR_TTL = 3600  # register data changes daily at most
GEOJSON = Path(__file__).with_name("bundeslaender.geo.json")

_cache: dict = {"snapshot": None, "expires": 0.0, "error": None}
_regions_cache: dict = {"snapshot": None, "expires": 0.0}
_mastr_cache: dict = {"snapshot": None, "expires": 0.0}
_lock = threading.Lock()
_regions_lock = threading.Lock()
_mastr_lock = threading.Lock()

# short-TTL cache for parametrised MaStR queries: key -> (expires, payload)
_mastr_qcache: dict = {}
_mastr_qlock = threading.Lock()


def _qcached(key, fn):
    with _mastr_qlock:
        hit = _mastr_qcache.get(key)
        if hit and time.time() < hit[0]:
            return hit[1]
    data = fn()
    with _mastr_qlock:
        if len(_mastr_qcache) > 500:
            _mastr_qcache.clear()
        _mastr_qcache[key] = (time.time() + MASTR_TTL, data)
    return data


def snapshot() -> dict:
    """Current grid picture as a JSON-serialisable dict (cached ~55 s)."""
    with _lock:
        if _cache["snapshot"] and time.time() < _cache["expires"]:
            return _cache["snapshot"]

    mix = energy_charts.fetch_power_mix(_today(), _today())
    as_of, gen = _generation_snapshot(mix)
    co2 = regional.mix_co2_intensity(gen, regional.EC_EMISSION_FACTORS)

    data = {
        "as_of": as_of.astimezone(BERLIN).isoformat() if as_of else None,
        "fetched_at": datetime.now(BERLIN).isoformat(timespec="seconds"),
        "load_mw": _latest(mix, "Load (incl. self-consumption)", "Load", before=as_of),
        "residual_load_mw": _latest(mix, "Residual load", before=as_of),
        "renewable_share_pct": _latest(mix, "Renewable share of load", before=as_of),
        "trading_mw": _latest(mix, "Cross border electricity trading", before=as_of),
        "total_generation_mw": round(sum(v for _, v in gen), 1) if gen else None,
        "co2_intensity_gkwh": round(co2, 0) if co2 is not None else None,
        "generation": [
            {
                "name": n,
                "mw": v,
                "renewable": n in energy_charts.RENEWABLE_TYPES,
                "co2_gkwh": regional.EC_EMISSION_FACTORS.get(
                    n, regional.DEFAULT_EMISSION_FACTOR
                ),
            }
            for n, v in gen
        ],
        "price_eur_mwh": None,
        "price_time": None,
        "forecast_peak_renewables_pct": None,
        "error": None,
    }

    try:
        hit = energy_charts.fetch_price(_today(), _today()).latest()
        if hit:
            pt, pv = hit
            data["price_eur_mwh"] = round(pv, 2)
            data["price_time"] = pt.astimezone(BERLIN).strftime("%H:%M")
    except Exception:
        pass

    try:
        shares = energy_charts.fetch_signal().get("share", [])
        if shares:
            data["forecast_peak_renewables_pct"] = round(max(shares), 1)
    except Exception:
        pass

    with _lock:
        _cache["snapshot"] = data
        _cache["expires"] = time.time() + CACHE_TTL
    return data


def regions_snapshot() -> dict:
    """Per-control-area snapshot for the map (cached ~5 min)."""
    with _regions_lock:
        if _regions_cache["snapshot"] and time.time() < _regions_cache["expires"]:
            return _regions_cache["snapshot"]

    data = regional.all_zones()
    data["fetched_at"] = datetime.now(BERLIN).isoformat(timespec="seconds")

    with _regions_lock:
        _regions_cache["snapshot"] = data
        _regions_cache["expires"] = time.time() + REGIONS_TTL
    return data


# groups shown on the dashboard (slug subset of cli.MASTR_TOP_GROUPS)
MASTR_WEB_SLUGS = ("coal", "lignite", "gas", "offshore", "solar", "battery")


def mastr_snapshot() -> dict:
    """Top registered units from the MaStR cache (cached ~1 h)."""
    with _mastr_lock:
        if _mastr_cache["snapshot"] and time.time() < _mastr_cache["expires"]:
            return _mastr_cache["snapshot"]

    try:
        conn = mastr.open_db()
    except RuntimeError:
        return {"available": False, "groups": []}
    meta = mastr._load_meta(mastr.DATA_DIR)
    groups = []
    for label, slug, where in MASTR_TOP_GROUPS:
        if slug not in MASTR_WEB_SLUGS:
            continue
        rows = conn.execute(
            f"SELECT * FROM units WHERE {where} AND status='In Betrieb' "
            "ORDER BY netto_mw DESC LIMIT 5"
        ).fetchall()
        if not rows:
            continue
        ops = mastr.operator_names(conn, (r["operator_abr"] for r in rows))
        groups.append({
            "label": label,
            "units": [{
                "name": r["name"] or r["plant"] or r["mastr_nr"],
                "plant": r["plant"],
                "mw": r["netto_mw"],
                "operator": ops.get(r["operator_abr"], ""),
                "location": f"{r['plz']} {r['ort']}".strip() or r["landkreis"],
            } for r in rows],
        })
    conn.close()
    data = {
        "available": True,
        "export": (meta.get("export_url") or "").rsplit("/", 1)[-1],
        "groups": groups,
    }
    with _mastr_lock:
        _mastr_cache["snapshot"] = data
        _mastr_cache["expires"] = time.time() + MASTR_TTL
    return data


BL_NAMES = {
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin",
    "BB": "Brandenburg", "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland",
    "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}

BATTERY_SORTS = {
    "mw": "netto_mw DESC",
    "mw_asc": "netto_mw ASC",
    "newest": "inbetrieb DESC",
    "plz": "plz ASC, netto_mw DESC",
}


def _bundesland(v: str) -> str:
    """Accept an ISO code or a (possibly imperfect) state name."""
    v = (v or "").strip()
    if v.upper() in BL_NAMES:
        return BL_NAMES[v.upper()]
    for name in BL_NAMES.values():
        if v.lower() == name.lower():
            return name
    return v


def _one(params: dict, key: str, default: str = "") -> str:
    vals = params.get(key)
    return vals[0] if vals else default


def mastr_batteries(params: dict, conn=None) -> dict:
    """Paginated battery-storage units, filterable by PLZ prefix + state."""
    def run():
        try:
            limit = int(_one(params, "limit", "50"))
            offset = int(_one(params, "offset", "0"))
        except ValueError:
            limit, offset = 50, 0
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        plz = "".join(c for c in _one(params, "plz") if c.isdigit())[:5]
        state = _bundesland(_one(params, "bundesland"))
        all_types = _one(params, "all_types") in ("1", "true", "yes")
        sort = BATTERY_SORTS.get(_one(params, "sort"), BATTERY_SORTS["mw"])

        c = conn or mastr.open_db()
        where = ["category='speicher'", "status='In Betrieb'"]
        args: list = []
        if not all_types:
            where.append("subtype='Batterie'")
        if plz:
            where.append("plz GLOB ?")
            args.append(plz + "*")
        if state:
            where.append("bundesland=?")
            args.append(state)
        cond = " AND ".join(where)

        tot = c.execute(
            f"SELECT COUNT(*) c, SUM(netto_mw) n, SUM(brutto_mw) b "
            f"FROM units WHERE {cond}", args).fetchone()
        rows = c.execute(
            f"SELECT mastr_nr, subtype, name, plant, operator_abr, plz, ort,"
            f" bundesland, netto_mw, brutto_mw, inbetrieb, lon, lat"
            f" FROM units WHERE {cond} ORDER BY {sort} LIMIT ? OFFSET ?",
            (*args, limit, offset)).fetchall()
        ops = mastr.operator_names(conn=c, abrs=(r["operator_abr"] for r in rows))
        states = [r["bundesland"] for r in c.execute(
            "SELECT DISTINCT bundesland FROM units WHERE category='speicher'"
            " AND subtype='Batterie' AND status='In Betrieb' AND bundesland!=''"
            " ORDER BY bundesland")]
        if conn is None:
            c.close()
        return {
            "available": True,
            "limit": limit,
            "offset": offset,
            "total_units": tot["c"],
            "total_mw_netto": round(tot["n"] or 0, 1),
            "total_mw_brutto": round(tot["b"] or 0, 1),
            "filters": {"plz": plz, "bundesland": state, "all_types": all_types},
            "bundeslaender": states,
            "units": [{
                "mastr_nr": r["mastr_nr"],
                "subtype": r["subtype"],
                "name": r["name"] or r["plant"] or r["mastr_nr"],
                "plant": r["plant"],
                "operator": ops.get(r["operator_abr"], ""),
                "plz": r["plz"], "ort": r["ort"], "bundesland": r["bundesland"],
                "netto_mw": r["netto_mw"], "brutto_mw": r["brutto_mw"],
                "inbetrieb": r["inbetrieb"], "lon": r["lon"], "lat": r["lat"],
            } for r in rows],
        }

    try:
        return _qcached(("batt", json.dumps(params, sort_keys=True)), run)
    except RuntimeError:
        return {"available": False, "units": [], "total_units": 0}


def mastr_capacity(params: dict, conn=None) -> dict:
    """Installed registered capacity per generation group (In Betrieb)."""
    def run():
        state = _bundesland(_one(params, "bundesland"))
        c = conn or mastr.open_db()
        groups = []
        for label, slug, where in MASTR_TOP_GROUPS:
            q = (f"SELECT COUNT(*) c, SUM(netto_mw) s FROM units "
                 f"WHERE {where} AND status='In Betrieb'")
            args = (state,) if state else ()
            r = c.execute(q + (" AND bundesland=?" if state else ""),
                          args).fetchone()
            groups.append({"slug": slug, "label": label,
                           "units": r["c"], "mw": round(r["s"] or 0, 1)})
        groups.sort(key=lambda g: -g["mw"])
        if conn is None:
            c.close()
        return {
            "available": True,
            "bundesland": state,
            "total_units": sum(g["units"] for g in groups),
            "total_mw": round(sum(g["mw"] for g in groups), 1),
            "groups": groups,
        }

    try:
        return _qcached(("cap", json.dumps(params, sort_keys=True)), run)
    except RuntimeError:
        return {"available": False, "groups": []}


# unit-map layers: (slug, label, SQL predicate, include non-active units).
# Coordinates are trusted only inside a generous Germany bounding box —
# everything else still counts towards the per-state aggregation.
DE_LON = (5.5, 15.5)
DE_LAT = (47.0, 55.5)
XY_OK = (f"lon BETWEEN {DE_LON[0]} AND {DE_LON[1]}"
         f" AND lat BETWEEN {DE_LAT[0]} AND {DE_LAT[1]}")

MASTR_MAP_LAYERS = [
    ("battery", "Battery", "category='speicher' AND subtype='Batterie'", False),
    ("storage", "All storage", "category='speicher'", False),
    ("solar", "Solar", "category='solar'", False),
    ("solar-park", "Solar parks",
     "category='solar' AND subtype='Freiflächensolaranlage'", False),
    ("onshore", "Wind onshore",
     "category='wind' AND subtype='Windenergie an Land'", False),
    ("offshore", "Wind offshore",
     "category='wind' AND subtype='Windenergie auf See'", False),
    ("coal", "Coal",
     "category='verbrennung' AND subtype IN ('Steinkohle','Braunkohle')", False),
    ("gas", "Gas",
     "category='verbrennung' AND subtype IN ('Erdgas','andere Gase')", False),
    ("biomass", "Biomass", "category='biomasse'", False),
    ("hydro", "Hydro", "category='wasser'", False),
    ("nuclear", "Nuclear", "category='kernkraft'", True),
]
MARKER_CAP = 2000


def mastr_map(params: dict, conn=None) -> dict:
    """One map layer: per-Bundesland totals + located unit markers.

    Units whose coordinates are missing or fall outside Germany only
    appear in the state aggregates — the frontend choropleth is the
    fallback representation for them.
    """
    def run():
        slug = _one(params, "type", "battery")
        layers = {l[0]: l for l in MASTR_MAP_LAYERS}
        if slug not in layers:
            return {"available": True, "error": f"unknown type {slug!r}",
                    "layers": [{"slug": s, "label": l}
                               for s, l, _, _ in MASTR_MAP_LAYERS]}
        _slug, label, where, any_status = layers[slug]
        try:
            n_mark = int(_one(params, "markers", str(MARKER_CAP)))
        except ValueError:
            n_mark = MARKER_CAP
        n_mark = max(0, min(n_mark, 5000))
        cond = where + ("" if any_status else " AND status='In Betrieb'")

        c = conn or mastr.open_db()
        states, other = [], []
        for r in c.execute(
                f"SELECT bundesland bl, COUNT(*) c, SUM(netto_mw) s,"
                f" SUM({XY_OK}) ok FROM units WHERE {cond}"
                f" GROUP BY bundesland"):
            entry = {"name": r["bl"] or "(unknown)", "units": r["c"],
                     "mw": round(r["s"] or 0, 1), "with_coords": r["ok"] or 0}
            (states if r["bl"] in BL_NAMES.values() else other).append(entry)
        markers = [{
            "mastr_nr": r["mastr_nr"], "name": r["name"] or r["plant"]
                    or r["mastr_nr"],
            "plant": r["plant"], "subtype": r["subtype"],
            "mw": r["netto_mw"], "lon": r["lon"], "lat": r["lat"],
            "plz": r["plz"], "ort": r["ort"],
        } for r in c.execute(
            f"SELECT mastr_nr, name, plant, subtype, netto_mw, lon, lat,"
            f" plz, ort FROM units WHERE {cond} AND {XY_OK}"
            f" ORDER BY netto_mw DESC LIMIT ?", (n_mark,))]
        if conn is None:
            c.close()
        total = sum(s["units"] for s in states) + sum(o["units"] for o in other)
        located = sum(s["with_coords"] for s in states) \
            + sum(o["with_coords"] for o in other)
        return {
            "available": True,
            "type": slug,
            "label": label,
            "any_status": any_status,
            "total_units": total,
            "total_mw": round(sum(s["mw"] for s in states)
                              + sum(o["mw"] for o in other), 1),
            "with_coords": located,
            "states": sorted(states, key=lambda s: -s["mw"]),
            "other": sorted(other, key=lambda s: -s["mw"]),
            "markers": markers,
            "layers": [{"slug": s, "label": l}
                       for s, l, _, _ in MASTR_MAP_LAYERS],
        }

    try:
        return _qcached(("map", json.dumps(params, sort_keys=True)), run)
    except RuntimeError:
        return {"available": False, "states": [], "markers": []}


def chart_png() -> bytes:
    mix = energy_charts.fetch_power_mix(_today(), _today())
    from .cli import build_chart_figure

    fig = build_chart_figure(mix)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "de-energy-live/0.1"

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, status: int = 200) -> None:
        self._send(json.dumps(obj).encode(), "application/json", status)

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        path, params = url.path, parse_qs(url.query)
        try:
            if path == "/api/data":
                self._json(snapshot())
            elif path == "/api/regions":
                self._json(regions_snapshot())
            elif path == "/api/mastr":
                self._json(mastr_snapshot())
            elif path == "/api/mastr/capacity":
                self._json(mastr_capacity(params))
            elif path == "/api/mastr/batteries":
                self._json(mastr_batteries(params))
            elif path == "/api/mastr/map":
                self._json(mastr_map(params))
            elif path == "/geo/states":
                self._send(GEOJSON.read_bytes(), "application/geo+json")
            elif path == "/chart.png":
                self._send(chart_png(), "image/png")
            elif path == "/healthz":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            if path == "/api/data":
                self._json({"error": str(e), "generation": []}, 502)
            elif path == "/api/regions":
                self._json({"error": str(e), "zones": {}}, 502)
            else:
                self._send(f"error: {e}".encode(), "text/plain", 502)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} — {fmt % args}", flush=True)


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Serving API on http://{host}:{port} (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="de-energy web")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
