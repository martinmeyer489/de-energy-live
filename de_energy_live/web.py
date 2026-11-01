"""Live web dashboard for German electricity supply, demand & carbon.

Pure stdlib (http.server). Serves:
  /            single-page dashboard: national snapshot + regional
               control-area map (auto-refreshes every ~60 s)
  /map         redirects to /#regions (the map lives on the main page)
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


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>de-energy-live — German grid</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0e1116;
         color: #e6e6e6; }
  header { padding: 18px 24px; border-bottom: 1px solid #232a33; }
  h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .sub { color: #8b98a5; font-size: 13px; margin-top: 4px; }
  main { padding: 20px 24px; max-width: 1100px; margin: auto; }
  .cards { display: grid; gap: 14px;
           grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  .card { background: #161b22; border: 1px solid #232a33; border-radius: 10px;
          padding: 14px 16px; }
  .card .label { font-size: 12px; color: #8b98a5; text-transform: uppercase;
                 letter-spacing: .05em; }
  .card .value { font-size: 26px; font-weight: 650; margin-top: 6px;
                 font-variant-numeric: tabular-nums; }
  .card .unit { font-size: 13px; color: #8b98a5; }
  section { margin-top: 26px; }
  h2 { font-size: 15px; color: #8b98a5; text-transform: uppercase;
       letter-spacing: .05em; margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  td { padding: 6px 8px; border-bottom: 1px solid #1d242d; }
  td.mw { text-align: right; font-variant-numeric: tabular-nums; width: 110px; }
  .bar { height: 14px; border-radius: 3px; background: #388bfd;
         display: inline-block; min-width: 2px; }
  .bar.ren { background: #3fb950; }
  td.barcell { width: 45%; }
  .tag { font-size: 10px; border-radius: 4px; padding: 1px 5px;
         margin-left: 6px; vertical-align: middle; }
  .tag.ren { background: #1b3a24; color: #3fb950; }
  img.chart { width: 100%; border-radius: 10px; border: 1px solid #232a33;
              background: #fff; }
  .err { color: #f85149; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
  .chip { border: 1px solid #2a333f; background: #161b22; color: #c8d0d9;
          border-radius: 999px; padding: 5px 12px; font-size: 12px;
          cursor: pointer; }
  .chip.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  .frow { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
          margin-bottom: 12px; }
  .frow .lbl { font-size: 11px; color: #8b98a5; text-transform: uppercase;
               letter-spacing: .04em; }
  .finput, .fselect { background: #161b22; border: 1px solid #2a333f;
          color: #c8d0d9; border-radius: 6px; padding: 5px 9px;
          font-size: 12px; }
  .pager { margin-top: 10px; display: flex; gap: 10px; align-items: center;
           font-size: 12px; color: #8b98a5; }
  .pager button { border: 1px solid #2a333f; background: #161b22;
           color: #c8d0d9; border-radius: 6px; padding: 5px 12px;
           font-size: 12px; cursor: pointer; }
  .pager button:disabled { opacity: .4; cursor: default; }
  #battmapwrap, #mmapwrap { height: 340px; border: 1px solid #232a33;
           border-radius: 10px; overflow: hidden; background: #0b0e13;
           margin-bottom: 14px; position: relative; }
  #battmap, #mmap { width: 100%; height: 100%; display: block; }
  .bstate { fill: #161b22; stroke: #2a333f; stroke-width: 1;
            vector-effect: non-scaling-stroke; }
  .bpt { fill: #f2c40f; fill-opacity: .55; stroke: #f2c40f;
         vector-effect: non-scaling-stroke; stroke-width: 1; }
  .mstate { stroke: #0b0e13; stroke-width: 1.2;
            vector-effect: non-scaling-stroke; cursor: default; }
  .mstate:hover { stroke: #fff; stroke-width: 1.6; }
  .mpt { fill-opacity: .55; stroke: #0e1116; stroke-width: 1.2;
         vector-effect: non-scaling-stroke; }
  #mmaplegend { position: absolute; left: 14px; bottom: 14px; z-index: 4;
               background: rgba(20,25,33,.92); border: 1px solid #2a333f;
               border-radius: 10px; padding: 10px 14px; font-size: 11px;
               color: #aeb8c4; }
  #mmaplegend .grad { height: 10px; width: 200px; border-radius: 5px;
                     margin: 6px 0 2px; }
  #mmaplegend .lbl { display: flex; justify-content: space-between; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; width: 90px; }
  #mapwrap { display: flex; height: 480px; border: 1px solid #232a33;
             border-radius: 10px; overflow: hidden; background: #0b0e13; }
  #mapbox { flex: 1; position: relative; min-width: 0; }
  #regmap { width: 100%; height: 100%; display: block; }
  .state { stroke: #0b0e13; stroke-width: 1.2; cursor: pointer;
           fill: #232a33; transition: fill .4s, opacity .3s;
           vector-effect: non-scaling-stroke; }
  .state:hover { stroke: #fff; stroke-width: 1.6; }
  .state.sel { stroke: #fff; stroke-width: 2; }
  .state.dim { opacity: .25; }
  #maptip { position: fixed; pointer-events: none; z-index: 9; display: none;
            background: #1c2230; border: 1px solid #2f3a4a; border-radius: 8px;
            padding: 8px 10px; font-size: 12px; max-width: 240px;
            box-shadow: 0 6px 18px rgba(0,0,0,.5); }
  #maptip b { display: block; font-size: 13px; margin-bottom: 2px; }
  #maptip .z { color: #8b98a5; }
  #maplegend { position: absolute; left: 14px; bottom: 14px; z-index: 4;
               background: rgba(20,25,33,.92); border: 1px solid #2a333f;
               border-radius: 10px; padding: 10px 14px; font-size: 11px;
               color: #aeb8c4; }
  #maplegend .grad { height: 10px; width: 200px; border-radius: 5px;
                     margin: 6px 0 2px; }
  #maplegend .lbl { display: flex; justify-content: space-between; }
  #mappanel { width: 320px; flex: none; overflow-y: auto; padding: 16px;
              border-left: 1px solid #1f2630; background: #10141b; }
  #mappanel h3.zone { font-size: 18px; margin: 0 0 2px; }
  #mappanel .tso { color: #8b98a5; font-size: 12px; margin-bottom: 4px; }
  #mappanel .states { color: #57606a; font-size: 11px; margin-bottom: 14px; }
  #mappanel .hint { color: #8b98a5; font-size: 13px; margin-top: 40px;
                    text-align: center; }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { background: #161b22; border: 1px solid #232a33;
          border-radius: 8px; padding: 10px 12px; }
  .stat .l { font-size: 10px; color: #8b98a5; text-transform: uppercase;
             letter-spacing: .04em; }
  .stat .v { font-size: 19px; font-weight: 650; margin-top: 3px;
             font-variant-numeric: tabular-nums; }
  .stat .u { font-size: 11px; color: #8b98a5; font-weight: 400; }
  .mapmix { margin-top: 16px; }
  .mapmix h4 { font-size: 12px; color: #8b98a5; text-transform: uppercase;
               letter-spacing: .05em; margin: 0 0 8px; }
  .mrow { display: grid; grid-template-columns: 1fr 62px; gap: 8px;
          align-items: center; font-size: 12px; padding: 3px 0; }
  .mrow .mw { text-align: right; font-variant-numeric: tabular-nums;
              color: #c8d0d9; }
  .mtrack { grid-column: 1 / -1; height: 5px; background: #1a2029;
            border-radius: 3px; margin: -2px 0 4px; }
  .mbar { height: 5px; border-radius: 3px; background: #388bfd; }
  .mbar.ren { background: #3fb950; }
  .asof { color: #57606a; font-size: 11px; margin-top: 14px; }
  @media (max-width: 900px) {
    #mapwrap { flex-direction: column; height: auto; }
    #mapbox { height: 360px; }
    #mappanel { width: auto; border-left: 0;
                border-top: 1px solid #1f2630; }
  }
  footer { color: #57606a; font-size: 12px; padding: 20px 24px; }
</style>
</head>
<body>
<header>
  <h1>German power grid — live</h1>
  <div class="sub" id="stamp">loading…</div>
  <div class="sub"><a href="#regions" style="color:#58a6ff">Regional map ↓</a>
    · <a href="#mmapsec" style="color:#58a6ff">MaStR unit map ↓</a></div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">Load</div>
      <div class="value"><span id="load">–</span> <span class="unit">MW</span></div></div>
    <div class="card"><div class="label">Generation</div>
      <div class="value"><span id="gen">–</span> <span class="unit">MW</span></div></div>
    <div class="card"><div class="label">Renewable share</div>
      <div class="value"><span id="ren">–</span> <span class="unit">%</span></div></div>
    <div class="card"><div class="label">CO₂ intensity</div>
      <div class="value"><span id="co2">–</span> <span class="unit">g/kWh</span></div></div>
    <div class="card"><div class="label">Spot price <span id="ptime"></span></div>
      <div class="value"><span id="price">–</span> <span class="unit">€/MWh</span></div></div>
    <div class="card"><div class="label">Residual load</div>
      <div class="value"><span id="res">–</span> <span class="unit">MW</span></div></div>
    <div class="card"><div class="label">Cross-border <span id="tdir"></span></div>
      <div class="value"><span id="trade">–</span> <span class="unit">MW</span></div></div>
  </div>

  <section>
    <h2>Generation mix</h2>
    <table><tbody id="mix"></tbody></table>
  </section>

  <section>
    <h2>Today — supply vs demand</h2>
    <img class="chart" id="chart" src="/chart.png" alt="generation vs load chart"
         onerror="this.style.display='none'">
  </section>

  <section id="regions">
    <h2>Regions — TSO control areas <span id="mapstamp"
        style="text-transform:none;font-weight:400"></span></h2>
    <div class="chips" id="mapchips"></div>
    <div id="mapwrap">
      <div id="mapbox">
        <svg id="regmap" preserveAspectRatio="xMidYMid meet"></svg>
        <div id="maplegend">
          <span id="lgname"></span>
          <div class="grad" id="lggrad"></div>
          <div class="lbl"><span id="lgmin"></span><span id="lgmax"></span></div>
        </div>
      </div>
      <div id="mappanel"><div class="hint">Click a region for details</div></div>
    </div>
  </section>

  <section id="mmapsec" style="display:none">
    <h2>MaStR unit map <span id="mmaptotal"
        style="text-transform:none;font-weight:400"></span></h2>
    <div class="chips" id="mmapchips"></div>
    <div class="frow">
      <span class="lbl">colour states by</span>
      <select class="fselect" id="mmapmetric">
        <option value="units">unit count</option>
        <option value="mw">capacity (MW)</option>
      </select>
      <label style="font-size:12px;color:#8b98a5">
        <input type="checkbox" id="mmappts" checked> largest located units</label>
      <span id="mmapnote" style="font-size:11px;color:#57606a"></span>
    </div>
    <div id="mmapwrap">
      <svg id="mmap" preserveAspectRatio="xMidYMid meet"></svg>
      <div id="mmaplegend">
        <span id="mmlgname"></span>
        <div class="grad" id="mmlggrad"></div>
        <div class="lbl"><span id="mmlgmin"></span><span id="mmlgmax"></span></div>
      </div>
    </div>
    <div id="mmapother" style="font-size:12px;color:#8b98a5;margin:-6px 0 14px"></div>
  </section>

  <section id="mastrsec" style="display:none">
    <h2>Largest registered units — MaStR <span id="mastrexport"
        style="text-transform:none;font-weight:400"></span></h2>
    <table><tbody id="mastr"></tbody></table>
  </section>

  <section id="capsec" style="display:none">
    <h2>Installed capacity — MaStR <span id="capstamp"
        style="text-transform:none;font-weight:400"></span></h2>
    <div class="frow">
      <span class="lbl">Bundesland</span>
      <select class="fselect" id="capstate">
        <option value="">Deutschland</option>
      </select>
      <span style="font-size:11px;color:#57606a">registered units
        (status: In Betrieb) · Nettonennleistung</span>
    </div>
    <table><tbody id="cap"></tbody></table>
  </section>

  <section id="battsec" style="display:none">
    <h2>Battery storage — MaStR <span id="batttotal"
        style="text-transform:none;font-weight:400"></span></h2>
    <div class="frow">
      <span class="lbl">PLZ</span>
      <input class="finput" id="battplz" size="6" maxlength="5"
             placeholder="prefix, e.g. 50">
      <span class="lbl">Bundesland</span>
      <select class="fselect" id="battstate"><option value="">all</option></select>
      <span class="lbl">Sort</span>
      <select class="fselect" id="battsort">
        <option value="mw">largest</option>
        <option value="mw_asc">smallest</option>
        <option value="newest">newest</option>
        <option value="plz">postcode</option>
      </select>
      <label style="font-size:12px;color:#8b98a5">
        <input type="checkbox" id="battall"> all storage types</label>
    </div>
    <div id="battmapwrap">
      <svg id="battmap" preserveAspectRatio="xMidYMid meet"></svg>
    </div>
    <table><tbody id="batt"></tbody></table>
    <div class="pager">
      <button id="bprev">← prev</button>
      <span id="bpage"></span>
      <button id="bnext">next →</button>
    </div>
  </section>
</main>
<div id="maptip"></div>
<footer>
  Data: energy-charts.info (Fraunhofer ISE) · SMARD (Bundesnetzagentur) ·
  auto-refresh every 60 s · <span id="fetched"></span>
</footer>
<script>
const fmt = v => v == null ? "–" : Math.round(v).toLocaleString("en-US");
const $ = id => document.getElementById(id);

// colour scales shared by the CO2 card and the regional map below
const SIGNALS = {
  co2:    {label: "CO₂ intensity", unit: "g/kWh", stops: [[0,"#2bb673"],[150,"#8fce3f"],[300,"#f2c40f"],[500,"#e67e22"],[700,"#d64541"],[900,"#7d3c98"]], lo:0, hi:900},
  ren:    {label: "Renewable share", unit: "%", stops: [[0,"#d64541"],[35,"#f2c40f"],[65,"#8fce3f"],[100,"#2bb673"]], lo:0, hi:100},
  load:   {label: "Net load", unit: "MW", stops: [[0,"#123a5c"],[6000,"#1f6feb"],[20000,"#79c0ff"]], lo:0, hi:22000},
  self:   {label: "Self-sufficiency", unit: "%", stops: [[0,"#d64541"],[60,"#e67e22"],[90,"#f2c40f"],[130,"#8fce3f"],[200,"#2bb673"]], lo:0, hi:200},
};
const BL = {"BW":"Baden-Württemberg","BY":"Bayern","BE":"Berlin",
  "BB":"Brandenburg","HB":"Bremen","HH":"Hamburg","HE":"Hessen",
  "MV":"Mecklenburg-Vorpommern","NI":"Niedersachsen","NW":"Nordrhein-Westfalen",
  "RP":"Rheinland-Pfalz","SL":"Saarland","SN":"Sachsen","ST":"Sachsen-Anhalt",
  "SH":"Schleswig-Holstein","TH":"Thüringen"};
const fmtMW = v => v == null ? "–" :
  (v >= 1000 ? (v/1000).toLocaleString("en-US",{maximumFractionDigits:1})+" GW"
             : (Math.round(v*10)/10).toLocaleString("en-US")+" MW");
function hex2rgb(h){return [1,3,5].map(i=>parseInt(h.slice(i,i+2),16));}
function colorFor(sig, v){
  if (v == null) return "#232a33";
  const s = SIGNALS[sig], stops = s.stops;
  const x = Math.max(s.lo, Math.min(s.hi, v));
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [x0,c0] = stops[i-1], [x1,c1] = stops[i];
      const t = (x-x0)/(x1-x0), a=hex2rgb(c0), b=hex2rgb(c1);
      return "rgb(" + a.map((p,j)=>Math.round(p+t*(b[j]-p))).join(",") + ")";
    }
  }
  return stops.at(-1)[1];
}

async function refresh() {
  try {
    const d = await (await fetch("/api/data")).json();
    if (d.error) throw new Error(d.error);
    document.getElementById("stamp").textContent =
      "Data as of " + new Date(d.as_of).toLocaleString("de-DE",
        {timeZone: "Europe/Berlin"}) + " (Europe/Berlin) · renewables forecast peak today: " +
      (d.forecast_peak_renewables_pct ?? "–") + " %";
    document.getElementById("load").textContent = fmt(d.load_mw);
    document.getElementById("gen").textContent = fmt(d.total_generation_mw);
    document.getElementById("ren").textContent =
      d.renewable_share_pct == null ? "–" : d.renewable_share_pct.toFixed(1);
    const co2El = document.getElementById("co2");
    co2El.textContent =
      d.co2_intensity_gkwh == null ? "–" : Math.round(d.co2_intensity_gkwh);
    co2El.style.color =
      d.co2_intensity_gkwh == null ? "" : colorFor("co2", d.co2_intensity_gkwh);
    document.getElementById("price").textContent =
      d.price_eur_mwh == null ? "–" : d.price_eur_mwh.toFixed(2);
    document.getElementById("ptime").textContent =
      d.price_time ? "@ " + d.price_time : "";
    document.getElementById("res").textContent = fmt(d.residual_load_mw);
    document.getElementById("trade").textContent = fmt(Math.abs(d.trading_mw ?? 0));
    document.getElementById("tdir").textContent =
      d.trading_mw == null ? "" : (d.trading_mw >= 0 ? "(export)" : "(import)");

    const vmax = Math.max(...d.generation.map(g => g.mw), 1);
    document.getElementById("mix").innerHTML = d.generation.map(g =>
      `<tr><td>${g.name}${g.renewable ? '<span class="tag ren">renewable</span>' : ""}</td>` +
      `<td class="mw">${fmt(g.mw)} MW</td>` +
      `<td class="barcell"><span class="bar${g.renewable ? " ren" : ""}" ` +
      `style="width:${(100 * g.mw / vmax).toFixed(1)}%"></span></td></tr>`
    ).join("");

    document.getElementById("chart").src = "/chart.png?t=" + Date.now();
    document.getElementById("chart").style.display = "";
    document.getElementById("fetched").textContent =
      "page updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("stamp").innerHTML =
      '<span class="err">update failed: ' + e.message + "</span>";
  }
}
refresh();
setInterval(refresh, 60000);

async function loadMastr() {
  try {
    const d = await (await fetch("/api/mastr")).json();
    if (!d.available) return;
    document.getElementById("mastrsec").style.display = "";
    document.getElementById("mastrexport").textContent =
      " (" + (d.export || "Gesamtdatenexport") + ")";
    document.getElementById("mastr").innerHTML = d.groups.map(g =>
      `<tr><td colspan="3" style="padding-top:10px;color:#8b98a5">` +
      `${g.label}</td></tr>` +
      g.units.map(u =>
        `<tr><td>${(u.plant && u.plant !== u.name ? u.plant + " · " : "") + u.name}` +
        `<div style="color:#57606a;font-size:12px">${u.operator} · ${u.location}</div></td>` +
        `<td class="mw">${fmt(u.mw)} MW</td>` +
        `<td class="barcell"><span class="bar" ` +
        `style="width:${(100 * u.mw / g.units[0].mw).toFixed(1)}%"></span></td></tr>`
      ).join("")
    ).join("");
  } catch (e) { /* register data optional */ }
}
loadMastr();

// --- MaStR: installed capacity + battery browser ---
function fillStates(sel){
  Object.values(BL).sort().forEach(n=>{
    const o=document.createElement("option");
    o.value=n; o.textContent=n; sel.appendChild(o);
  });
}

async function loadCapacity(){
  try{
    const st=$("capstate").value;
    const d=await (await fetch("/api/mastr/capacity"+
      (st?"?bundesland="+encodeURIComponent(st):""))).json();
    if(!d.available) return;
    $("capsec").style.display="";
    const vmax=Math.max(...d.groups.map(g=>g.mw),1);
    $("cap").innerHTML=d.groups.map(g=>
      `<tr><td>${g.label}</td><td class="mw">${fmtMW(g.mw)}</td>`+
      `<td class="n">${g.units.toLocaleString("en-US")}</td>`+
      `<td class="barcell"><span class="bar" `+
      `style="width:${(100*g.mw/vmax).toFixed(1)}%"></span></td></tr>`
    ).join("")+
    `<tr><td style="padding-top:8px;color:#8b98a5">Total (In Betrieb)</td>`+
    `<td class="mw" style="padding-top:8px;color:#8b98a5">${fmtMW(d.total_mw)}</td>`+
    `<td class="n" style="padding-top:8px;color:#8b98a5">`+
    `${d.total_units.toLocaleString("en-US")}</td><td></td></tr>`;
    $("capstamp").textContent=" — "+d.total_units.toLocaleString("en-US")+
      " units"+(st?" in "+st:"");
  }catch(e){ /* register data optional */ }
}
$("capstate").onchange=loadCapacity;
fillStates($("capstate"));
loadCapacity();

const batt={plz:"",state:"",sort:"mw",all:false,offset:0,limit:50};
let battGeo=null;

async function drawBattMap(units){
  try{
    battGeo=battGeo||mapGeo||await (await fetch("/geo/states")).json();
    const svg=$("battmap"), K=Math.cos(51*Math.PI/180);
    let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
    const walk=c=>{if(typeof c[0]==="number"){
      const p=c[0]*K,q=-c[1];
      x0=Math.min(x0,p);x1=Math.max(x1,p);y0=Math.min(y0,q);y1=Math.max(y1,q);
    }else c.forEach(walk);};
    battGeo.features.forEach(f=>walk(f.geometry.coordinates));
    const pad=(x1-x0)*0.05;
    svg.setAttribute("viewBox",[x0-pad,y0-pad,x1-x0+2*pad,y1-y0+2*pad].join(" "));
    const vmax=Math.max(...units.map(u=>u.netto_mw||0),0.001);
    svg.innerHTML=battGeo.features.map(f=>
      `<path class="bstate" d="${pathFor(f.geometry)}"/>`).join("")+
      units.filter(u=>u.lon&&u.lat).map(u=>
        `<circle class="bpt" cx="${(u.lon*K).toFixed(3)}" cy="${(-u.lat).toFixed(3)}" `+
        `r="${(0.015+0.06*Math.sqrt((u.netto_mw||0)/vmax)).toFixed(3)}">`+
        `<title>${u.name} — ${fmtMW(u.netto_mw)} (${u.plz} ${u.ort||""})</title></circle>`
      ).join("");
  }catch(e){}
}

async function loadBatteries(){
  try{
    const q=new URLSearchParams({limit:batt.limit,offset:batt.offset,sort:batt.sort});
    if(batt.plz)q.set("plz",batt.plz);
    if(batt.state)q.set("bundesland",batt.state);
    if(batt.all)q.set("all_types","1");
    const d=await (await fetch("/api/mastr/batteries?"+q)).json();
    if(!d.available)return;
    $("battsec").style.display="";
    $("batttotal").textContent=" — "+d.total_units.toLocaleString("en-US")+
      " units · "+fmtMW(d.total_mw_netto)+" net";
    const vmax=Math.max(...d.units.map(u=>u.netto_mw||0),0.001);
    $("batt").innerHTML=d.units.map(u=>
      `<tr><td>${(u.plant&&u.plant!==u.name?u.plant+" · ":"")+u.name}`+
      `<div style="color:#57606a;font-size:12px">`+
      `${[u.operator,(u.plz+" "+(u.ort||"")).trim(),u.bundesland,
         u.inbetrieb?"since "+u.inbetrieb.slice(0,4):"",
         u.subtype!=="Batterie"?u.subtype:""].filter(Boolean).join(" · ")}</div></td>`+
      `<td class="mw">${fmtMW(u.netto_mw)}</td>`+
      `<td class="barcell"><span class="bar" `+
      `style="width:${(100*(u.netto_mw||0)/vmax).toFixed(1)}%"></span></td></tr>`
    ).join("")||'<tr><td style="color:#57606a">no units match</td></tr>';
    const pages=Math.max(1,Math.ceil(d.total_units/d.limit));
    $("bpage").textContent="page "+(Math.floor(d.offset/d.limit)+1)+
      " / "+pages.toLocaleString("en-US");
    $("bprev").disabled=d.offset<=0;
    $("bnext").disabled=d.offset+d.limit>=d.total_units;
    batt.offset=d.offset;
    drawBattMap(d.units);
  }catch(e){ /* register data optional */ }
}
$("bprev").onclick=()=>{batt.offset=Math.max(0,batt.offset-batt.limit);loadBatteries();};
$("bnext").onclick=()=>{batt.offset+=batt.limit;loadBatteries();};
const battReset=()=>{batt.offset=0;loadBatteries();};
$("battplz").oninput=e=>{batt.plz=e.target.value.trim();battReset();};
$("battstate").onchange=e=>{batt.state=e.target.value;battReset();};
$("battsort").onchange=e=>{batt.sort=e.target.value;battReset();};
$("battall").onchange=e=>{batt.all=e.target.checked;battReset();};
fillStates($("battstate"));
loadBatteries();

// --- MaStR unit map: Bundesland choropleth + located-unit markers ---
const ML_COLOR = {battery:"#f2c40f",storage:"#f2c40f",solar:"#e8912d",
  "solar-park":"#e8912d",onshore:"#79c0ff",offshore:"#a5d6ff",
  coal:"#9aa7b4",gas:"#d2a8ff",biomass:"#3fb950",hydro:"#39c5cf",
  nuclear:"#f85149"};
const ML_ICON = {battery:"🔋",storage:"📦",solar:"☀️","solar-park":"🏞️",
  onshore:"🌬️",offshore:"🌊",coal:"🏭",gas:"🔥",biomass:"🌿",
  hydro:"💧",nuclear:"☢️"};
const MMAP_STOPS = ["#141d30","#1f6feb","#3fb950","#f2c40f"]; // log scale
let mmapType = "battery", mmapMetric = "units",
    mmapData = null, mmapGeo = null;
const esc = s => String(s).replace(/[<>&"]/g, "");
const mmapVal = s => mmapMetric === "mw" ? s.mw : s.units;
const mmapFmt = v => mmapMetric === "mw" ? fmtMW(v)
  : Math.round(v).toLocaleString("en-US");

function heatColor(t){
  const n = MMAP_STOPS.length - 1, x = Math.max(0, Math.min(1, t)) * n;
  const i = Math.min(Math.floor(x), n - 1), f = x - i;
  const a = hex2rgb(MMAP_STOPS[i]), b = hex2rgb(MMAP_STOPS[i + 1]);
  return "rgb(" + a.map((p, j) => Math.round(p + f * (b[j] - p))).join(",") + ")";
}

async function drawMmap(){
  const d = mmapData; if (!d) return;
  mmapGeo = mmapGeo || mapGeo || await (await fetch("/geo/states")).json();
  const svg = $("mmap"), K = Math.cos(51 * Math.PI / 180);
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  const walk = c => { if (typeof c[0] === "number") {
      const p = c[0] * K, q = -c[1];
      x0 = Math.min(x0, p); x1 = Math.max(x1, p);
      y0 = Math.min(y0, q); y1 = Math.max(y1, q);
    } else c.forEach(walk); };
  mmapGeo.features.forEach(f => walk(f.geometry.coordinates));
  const pad = (x1 - x0) * 0.05;
  svg.setAttribute("viewBox",
    [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad].join(" "));

  const byName = {}; d.states.forEach(s => byName[s.name] = s);
  const vals = d.states.map(mmapVal).filter(v => v > 0);
  const lo = Math.log10(Math.min(...vals, 1)),
        hi = Math.log10(Math.max(...vals, 1));
  const tOf = v => v > 0 && hi > lo ? (Math.log10(v) - lo) / (hi - lo) : 0;
  const ptColor = ML_COLOR[mmapType] || "#f2c40f";
  const vmax = Math.max(...d.markers.map(m => m.mw || 0), 0.001);

  svg.innerHTML = mmapGeo.features.map(f => {
    const s = byName[f.properties.name];
    return `<path class="mstate" data-st="${f.properties.name}" ` +
      `fill="${s ? heatColor(tOf(mmapVal(s))) : "#10141b"}" ` +
      `d="${pathFor(f.geometry)}"/>`;
  }).join("") + ($("mmappts").checked ? d.markers.map(m =>
    `<circle class="mpt" fill="${ptColor}" ` +
    `cx="${(m.lon * K).toFixed(3)}" cy="${(-m.lat).toFixed(3)}" ` +
    `r="${(0.02 + 0.07 * Math.sqrt((m.mw || 0) / vmax)).toFixed(3)}">` +
    `<title>${esc(m.name)} — ${fmtMW(m.mw)} ` +
    `(${esc((m.plz + " " + (m.ort || "")).trim() || m.subtype || "")})</title>` +
    `</circle>`).join("") : "");

  svg.querySelectorAll(".mstate").forEach(el => {
    el.addEventListener("mousemove", e => {
      const tip = $("maptip"), s = byName[el.dataset.st];
      tip.style.display = "block";
      tip.innerHTML = `<b>${el.dataset.st}</b>` + (s ?
        `<span class="z">${d.label} · ` +
        `${d.any_status ? "all statuses" : "In Betrieb"}</span><br>` +
        `${s.units.toLocaleString("en-US")} units · ${fmtMW(s.mw)}` +
        (s.units > s.with_coords ?
          `<br><span class="z">${(s.units - s.with_coords)
            .toLocaleString("en-US")} w/o usable coordinates</span>` : "")
        : `<span class="z">no ${d.label} units</span>`);
      tip.style.left = (e.clientX + 14) + "px";
      tip.style.top = (e.clientY + 14) + "px";
    });
    el.addEventListener("mouseleave",
      () => $("maptip").style.display = "none");
  });

  $("mmlgname").textContent = (ML_ICON[mmapType] || "") + " " + d.label +
    (d.any_status ? " (all statuses)" : " (In Betrieb)") +
    (mmapMetric === "mw" ? " — MW" : " — units");
  $("mmlggrad").style.background =
    "linear-gradient(90deg," + MMAP_STOPS.join(",") + ")";
  $("mmlgmin").textContent = vals.length ? mmapFmt(Math.pow(10, lo)) : "0";
  $("mmlgmax").textContent = vals.length ? mmapFmt(Math.pow(10, hi)) : "0";

  $("mmaptotal").textContent = " — " +
    d.total_units.toLocaleString("en-US") + " units · " + fmtMW(d.total_mw);
  const missing = d.total_units - d.with_coords;
  $("mmapnote").textContent =
    (d.with_coords > d.markers.length ?
      "markers: largest " + d.markers.length.toLocaleString("en-US") +
      " of " + d.with_coords.toLocaleString("en-US") + " located units · " :
      d.with_coords.toLocaleString("en-US") + " located units · ") +
    (missing > 0 ? missing.toLocaleString("en-US") +
      " without usable coordinates (in state totals)" :
      "all units located");
  $("mmapother").innerHTML = d.other.map(o =>
    "+ " + o.name + ": " + o.units.toLocaleString("en-US") +
    " units · " + fmtMW(o.mw)).join(" · ");
}

function renderMmapChips(layers){
  $("mmapchips").innerHTML = layers.map(l =>
    `<button class="chip${l.slug === mmapType ? " active" : ""}" ` +
    `data-t="${l.slug}">${ML_ICON[l.slug] || "⚡"} ${l.label}</button>`).join("");
  $("mmapchips").querySelectorAll(".chip").forEach(c => c.onclick = () => {
    mmapType = c.dataset.t; loadMastrMap(); });
}

async function loadMastrMap(){
  try {
    const d = await (await fetch("/api/mastr/map?type=" + mmapType)).json();
    if (!d.available || d.error) return;
    $("mmapsec").style.display = "";
    mmapData = d;
    renderMmapChips(d.layers);
    drawMmap();
  } catch (e) { /* register data optional */ }
}
$("mmapmetric").onchange = e => { mmapMetric = e.target.value; drawMmap(); };
$("mmappts").onchange = drawMmap;
loadMastrMap();

// --- regional map (control areas, SMARD data via /api/regions) ---
let mapSignal = "co2", regionsData = null, mapGeo = null, selZone = null;
const zoneVal = (z, sig) => z ? ({co2:z.co2_intensity_gkwh,
  ren:z.renewable_share_pct, load:z.load_mw,
  self:z.self_sufficiency_pct})[sig] : null;

function project(coords){
  const K = Math.cos(51*Math.PI/180);
  return coords.map(([lon,lat]) => [lon*K, -lat]);
}
function pathFor(geom){
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
  return polys.map(rings => rings.map(ring =>
    project(ring).map((p,i)=>(i?"L":"M")+p[0].toFixed(3)+","+p[1].toFixed(3)
  ).join("")+"Z").join("")).join("");
}
function drawMap(){
  // viewBox from projected bounds
  let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
  const walk=c=>{if(typeof c[0]==="number"){const[p,q]=[c[0]*Math.cos(51*Math.PI/180),-c[1]];
    x0=Math.min(x0,p);x1=Math.max(x1,p);y0=Math.min(y0,q);y1=Math.max(y1,q);}
    else c.forEach(walk);};
  mapGeo.features.forEach(f=>walk(f.geometry.coordinates));
  const pad=(x1-x0)*0.05;
  $("regmap").setAttribute("viewBox",[x0-pad,y0-pad,x1-x0+2*pad,y1-y0+2*pad].join(" "));
  $("regmap").innerHTML = mapGeo.features.map(f=>{
    const code=f.properties.id.split("-")[1];
    return `<path class="state" data-state="${code}" data-name="${f.properties.name}" d="${pathFor(f.geometry)}"/>`;
  }).join("");
  document.querySelectorAll(".state").forEach(el=>{
    el.addEventListener("mousemove", e=>{
      const tip=$("maptip"), z=zoneOf(el.dataset.state);
      const v=zoneVal(z,mapSignal);
      tip.style.display="block";
      tip.innerHTML=`<b>${el.dataset.name}</b><span class="z">${z?z.zone:"—"} · ${z?z.tso:""}</span><br>`+
        (v!=null?`${SIGNALS[mapSignal].label}: <b>${fmtVal(mapSignal,v)}</b>`:"no data");
      tip.style.left=(e.clientX+14)+"px"; tip.style.top=(e.clientY+14)+"px";
    });
    el.addEventListener("mouseleave",()=>$("maptip").style.display="none");
    el.addEventListener("click",()=>{selZone=zoneOf(el.dataset.state)?.zone??null;paint();renderPanel();});
  });
}
function zoneOf(stateCode){
  if(!regionsData) return null;
  for(const zn in regionsData.zones)
    if(regionsData.zones[zn].states.includes(stateCode)) return regionsData.zones[zn];
  return null;
}
function fmtVal(sig,v){
  if(sig==="load") return Math.round(v).toLocaleString("en-US")+" MW";
  return (sig==="co2"?Math.round(v):v.toFixed(1))+" "+SIGNALS[sig].unit;
}
function paint(){
  document.querySelectorAll(".state").forEach(el=>{
    const z=zoneOf(el.dataset.state);
    el.style.fill=colorFor(mapSignal,zoneVal(z,mapSignal));
    el.classList.toggle("sel", z&&z.zone===selZone);
    el.classList.toggle("dim", selZone&&z&&z.zone!==selZone);
  });
}
function renderChips(){
  $("mapchips").innerHTML=Object.entries(SIGNALS).map(([k,s])=>
    `<button class="chip${k===mapSignal?" active":""}" data-sig="${k}">${s.label}</button>`).join("");
  document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{
    mapSignal=c.dataset.sig;selZone=null;renderChips();paint();renderLegend();renderPanel();});
}
function renderLegend(){
  const s=SIGNALS[mapSignal];
  $("lgname").textContent=s.label;
  const cols=s.stops.map(st=>st[1]).join(",");
  $("lggrad").style.background=`linear-gradient(90deg,${cols})`;
  $("lgmin").textContent=s.lo+" "+s.unit;
  $("lgmax").textContent=(mapSignal==="co2"||mapSignal==="load"||mapSignal==="self"?s.hi+"+ ":s.hi+" ")+s.unit;
}
function renderPanel(){
  const p=$("mappanel");
  if(!selZone||!regionsData.zones[selZone]){p.innerHTML='<div class="hint">Click a region for details</div>';return;}
  const z=regionsData.zones[selZone];
  const vmax=Math.max(...z.generation.map(g=>g.mw),1);
  const names=BL;
  p.innerHTML=`
    <h3 class="zone">${z.zone}</h3>
    <div class="tso">${z.tso}</div>
    <div class="states">${z.states.map(s=>names[s]||s).join(" · ")}</div>
    <div class="stats">
      <div class="stat"><div class="l">Net load</div><div class="v">${z.load_mw==null?"–":Math.round(z.load_mw).toLocaleString("en-US")} <span class="u">MW</span></div></div>
      <div class="stat"><div class="l">Generation</div><div class="v">${z.total_generation_mw==null?"–":Math.round(z.total_generation_mw).toLocaleString("en-US")} <span class="u">MW</span></div></div>
      <div class="stat"><div class="l">CO₂ intensity</div><div class="v" style="color:${colorFor("co2",z.co2_intensity_gkwh)}">${z.co2_intensity_gkwh??"–"} <span class="u">g/kWh</span></div></div>
      <div class="stat"><div class="l">Renewable share</div><div class="v" style="color:${colorFor("ren",z.renewable_share_pct)}">${z.renewable_share_pct??"–"} <span class="u">%</span></div></div>
      <div class="stat"><div class="l">Self-sufficiency</div><div class="v">${z.self_sufficiency_pct??"–"} <span class="u">%</span></div></div>
      <div class="stat"><div class="l">Imports/exports</div><div class="v">${z.self_sufficiency_pct==null?"–":(z.self_sufficiency_pct>=100?"exporter":"importer")}</div></div>
    </div>
    <div class="mapmix"><h4>Generation mix</h4>
      ${z.generation.map(g=>`<div class="mrow"><span>${g.name}</span><span class="mw">${Math.round(g.mw).toLocaleString("en-US")} MW</span><div class="mtrack"><div class="mbar${g.renewable?" ren":""}" style="width:${(100*g.mw/vmax).toFixed(1)}%"></div></div></div>`).join("")}
    </div>
    <div class="asof">Data as of ${z.as_of?new Date(z.as_of).toLocaleString("de-DE",{timeZone:"Europe/Berlin"}):"–"} (Europe/Berlin)</div>`;
}
async function refreshRegions(){
  try{
    const r=await (await fetch("/api/regions")).json();
    if(r.error) throw new Error(r.error);
    regionsData=r;
    const times=Object.values(r.zones).map(z=>z.as_of).filter(Boolean).sort();
    $("mapstamp").textContent=" — data as of "+
      (times.length?new Date(times.at(-1)).toLocaleString("de-DE",{timeZone:"Europe/Berlin"}):"–");
    paint();
    renderPanel();
  }catch(e){
    $("mapstamp").innerHTML='<span class="err"> — update failed: '+e.message+"</span>";
  }
}
(async()=>{
  mapGeo=await (await fetch("/geo/states")).json();
  renderChips();renderLegend();drawMap();
  await refreshRegions();
  setInterval(refreshRegions,60000);
})();
</script>
</body>
</html>
"""


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
            if path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/map":
                self.send_response(302)
                self.send_header("Location", "/#regions")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/data":
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
    print(f"Serving dashboard on http://{host}:{port} (Ctrl+C to stop)", flush=True)
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
