"""Live web dashboard for German electricity supply & demand.

Pure stdlib (http.server). Serves:
  /            single-page dashboard (auto-refreshes every ~60 s)
  /map         regional map of the four TSO control areas
  /api/data    current snapshot as JSON
  /api/regions per-control-area snapshot as JSON
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
from zoneinfo import ZoneInfo

from . import regional
from .cli import _generation_snapshot, _latest, _today
from .sources import energy_charts

BERLIN = ZoneInfo("Europe/Berlin")
CACHE_TTL = 55  # seconds; upstream resolves at 15 min anyway
REGIONS_TTL = 300  # SMARD control-area data resolves at 15 min
GEOJSON = Path(__file__).with_name("bundeslaender.geo.json")

_cache: dict = {"snapshot": None, "expires": 0.0, "error": None}
_regions_cache: dict = {"snapshot": None, "expires": 0.0}
_lock = threading.Lock()
_regions_lock = threading.Lock()


def snapshot() -> dict:
    """Current grid picture as a JSON-serialisable dict (cached ~55 s)."""
    with _lock:
        if _cache["snapshot"] and time.time() < _cache["expires"]:
            return _cache["snapshot"]

    mix = energy_charts.fetch_power_mix(_today(), _today())
    as_of, gen = _generation_snapshot(mix)

    data = {
        "as_of": as_of.astimezone(BERLIN).isoformat() if as_of else None,
        "fetched_at": datetime.now(BERLIN).isoformat(timespec="seconds"),
        "load_mw": _latest(mix, "Load (incl. self-consumption)", "Load", before=as_of),
        "residual_load_mw": _latest(mix, "Residual load", before=as_of),
        "renewable_share_pct": _latest(mix, "Renewable share of load", before=as_of),
        "trading_mw": _latest(mix, "Cross border electricity trading", before=as_of),
        "total_generation_mw": round(sum(v for _, v in gen), 1) if gen else None,
        "generation": [
            {"name": n, "mw": v, "renewable": n in energy_charts.RENEWABLE_TYPES}
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
  footer { color: #57606a; font-size: 12px; padding: 20px 24px; }
</style>
</head>
<body>
<header>
  <h1>German power grid — live</h1>
  <div class="sub" id="stamp">loading…</div>
  <div class="sub"><a href="/map" style="color:#58a6ff">Regional map →</a></div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">Load</div>
      <div class="value"><span id="load">–</span> <span class="unit">MW</span></div></div>
    <div class="card"><div class="label">Generation</div>
      <div class="value"><span id="gen">–</span> <span class="unit">MW</span></div></div>
    <div class="card"><div class="label">Renewable share</div>
      <div class="value"><span id="ren">–</span> <span class="unit">%</span></div></div>
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
</main>
<footer>
  Data: energy-charts.info (Fraunhofer ISE) · auto-refresh every 60 s ·
  <span id="fetched"></span>
</footer>
<script>
const fmt = v => v == null ? "–" : Math.round(v).toLocaleString("en-US");
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
</script>
</body>
</html>
"""


MAP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>de-energy-live — Germany by grid region</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0b0e13;
         color: #e6e6e6; overflow: hidden; }
  #topbar { position: fixed; top: 0; left: 0; right: 0; z-index: 5;
            display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
            padding: 12px 20px; background: rgba(11,14,19,.92);
            border-bottom: 1px solid #1f2630; }
  #topbar h1 { font-size: 16px; font-weight: 650; margin: 0; }
  #topbar .sub { color: #8b98a5; font-size: 12px; }
  #topbar a { color: #58a6ff; font-size: 12px; text-decoration: none; }
  #signals { display: flex; gap: 6px; margin-left: auto; }
  .chip { border: 1px solid #2a333f; background: #161b22; color: #c8d0d9;
          border-radius: 999px; padding: 5px 12px; font-size: 12px;
          cursor: pointer; }
  .chip.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  #wrap { display: flex; height: 100vh; padding-top: 52px; }
  #mapbox { flex: 1; position: relative; }
  svg { width: 100%; height: 100%; display: block; }
  .state { stroke: #0b0e13; stroke-width: 1.2; cursor: pointer;
           transition: fill .4s, opacity .3s; }
  .state:hover { stroke: #fff; stroke-width: 1.6; }
  .state.sel { stroke: #fff; stroke-width: 2; }
  .state.dim { opacity: .25; }
  #tip { position: fixed; pointer-events: none; z-index: 9; display: none;
         background: #1c2230; border: 1px solid #2f3a4a; border-radius: 8px;
         padding: 8px 10px; font-size: 12px; max-width: 240px;
         box-shadow: 0 6px 18px rgba(0,0,0,.5); }
  #tip b { display: block; font-size: 13px; margin-bottom: 2px; }
  #tip .z { color: #8b98a5; }
  #legend { position: absolute; left: 18px; bottom: 18px; z-index: 4;
            background: rgba(20,25,33,.92); border: 1px solid #2a333f;
            border-radius: 10px; padding: 10px 14px; font-size: 11px;
            color: #aeb8c4; }
  #legend .grad { height: 10px; width: 220px; border-radius: 5px;
                  margin: 6px 0 2px; }
  #legend .lbl { display: flex; justify-content: space-between; }
  #panel { width: 340px; flex: none; overflow-y: auto; padding: 18px;
           border-left: 1px solid #1f2630; background: #10141b; }
  #panel h2 { font-size: 18px; margin: 0 0 2px; }
  #panel .tso { color: #8b98a5; font-size: 12px; margin-bottom: 4px; }
  #panel .states { color: #57606a; font-size: 11px; margin-bottom: 14px; }
  #panel .hint { color: #8b98a5; font-size: 13px; margin-top: 40px;
                 text-align: center; }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { background: #161b22; border: 1px solid #232a33;
          border-radius: 8px; padding: 10px 12px; }
  .stat .l { font-size: 10px; color: #8b98a5; text-transform: uppercase;
             letter-spacing: .04em; }
  .stat .v { font-size: 19px; font-weight: 650; margin-top: 3px;
             font-variant-numeric: tabular-nums; }
  .stat .u { font-size: 11px; color: #8b98a5; font-weight: 400; }
  #mix { margin-top: 16px; }
  #mix h3 { font-size: 12px; color: #8b98a5; text-transform: uppercase;
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
  @media (max-width: 900px) { #panel { display: none; } }
</style>
</head>
<body>
<div id="topbar">
  <h1>Germany — electricity by grid region</h1>
  <span class="sub" id="stamp">loading…</span>
  <a href="/">← dashboard</a>
  <div id="signals"></div>
</div>
<div id="wrap">
  <div id="mapbox">
    <svg id="map" preserveAspectRatio="xMidYMid meet"></svg>
    <div id="legend">
      <span id="lgname"></span>
      <div class="grad" id="lggrad"></div>
      <div class="lbl"><span id="lgmin"></span><span id="lgmax"></span></div>
    </div>
  </div>
  <div id="panel"><div class="hint">Click a region for details</div></div>
</div>
<div id="tip"></div>
<script>
const SIGNALS = {
  co2:    {label: "CO₂ intensity", unit: "g/kWh", stops: [[0,"#2bb673"],[150,"#8fce3f"],[300,"#f2c40f"],[500,"#e67e22"],[700,"#d64541"],[900,"#7d3c98"]], lo:0, hi:900},
  ren:    {label: "Renewable share", unit: "%", stops: [[0,"#d64541"],[35,"#f2c40f"],[65,"#8fce3f"],[100,"#2bb673"]], lo:0, hi:100},
  load:   {label: "Net load", unit: "MW", stops: [[0,"#123a5c"],[6000,"#1f6feb"],[20000,"#79c0ff"]], lo:0, hi:22000},
  self:   {label: "Self-sufficiency", unit: "%", stops: [[0,"#d64541"],[60,"#e67e22"],[90,"#f2c40f"],[130,"#8fce3f"],[200,"#2bb673"]], lo:0, hi:200},
};
let signal = "co2", data = null, geo = null, selZone = null;
const $ = id => document.getElementById(id);

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
const val = (z, sig) => z ? ({co2:z.co2_intensity_gkwh, ren:z.renewable_share_pct,
  load:z.load_mw, self:z.self_sufficiency_pct})[sig] : null;

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
  geo.features.forEach(f=>walk(f.geometry.coordinates));
  const pad=(x1-x0)*0.05;
  $("map").setAttribute("viewBox",[x0-pad,y0-pad,x1-x0+2*pad,y1-y0+2*pad].join(" "));
  $("map").innerHTML = geo.features.map(f=>{
    const code=f.properties.id.split("-")[1];
    return `<path class="state" data-state="${code}" data-name="${f.properties.name}" d="${pathFor(f.geometry)}"/>`;
  }).join("");
  document.querySelectorAll(".state").forEach(el=>{
    el.addEventListener("mousemove", e=>{
      const tip=$("tip"), z=zoneOf(el.dataset.state);
      const v=val(z,signal);
      tip.style.display="block";
      tip.innerHTML=`<b>${el.dataset.name}</b><span class="z">${z?z.zone:"—"} · ${z?z.tso:""}</span><br>`+
        (v!=null?`${SIGNALS[signal].label}: <b>${fmtVal(signal,v)}</b>`:"no data");
      tip.style.left=(e.clientX+14)+"px"; tip.style.top=(e.clientY+14)+"px";
    });
    el.addEventListener("mouseleave",()=>$("tip").style.display="none");
    el.addEventListener("click",()=>{selZone=zoneOf(el.dataset.state)?.zone??null;paint();renderPanel();});
  });
}
function zoneOf(stateCode){
  if(!data) return null;
  for(const zn in data.zones) if(data.zones[zn].states.includes(stateCode)) return data.zones[zn];
  return null;
}
function fmtVal(sig,v){
  if(sig==="load") return Math.round(v).toLocaleString("en-US")+" MW";
  return (sig==="co2"?Math.round(v):v.toFixed(1))+" "+SIGNALS[sig].unit;
}
function paint(){
  document.querySelectorAll(".state").forEach(el=>{
    const z=zoneOf(el.dataset.state);
    el.style.fill=colorFor(signal,val(z,signal));
    el.classList.toggle("sel", z&&z.zone===selZone);
    el.classList.toggle("dim", selZone&&z&&z.zone!==selZone);
  });
}
function renderChips(){
  $("signals").innerHTML=Object.entries(SIGNALS).map(([k,s])=>
    `<button class="chip${k===signal?" active":""}" data-sig="${k}">${s.label}</button>`).join("");
  document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{
    signal=c.dataset.sig;selZone=null;renderChips();paint();renderLegend();renderPanel();});
}
function renderLegend(){
  const s=SIGNALS[signal];
  $("lgname").textContent=s.label;
  const cols=s.stops.map(st=>st[1]).join(",");
  $("lggrad").style.background=`linear-gradient(90deg,${cols})`;
  $("lgmin").textContent=s.lo+" "+s.unit;
  $("lgmax").textContent=(signal==="co2"||signal==="load"||signal==="self"?s.hi+"+ ":s.hi+" ")+s.unit;
}
function renderPanel(){
  const p=$("panel");
  if(!selZone||!data.zones[selZone]){p.innerHTML='<div class="hint">Click a region for details</div>';return;}
  const z=data.zones[selZone];
  const vmax=Math.max(...z.generation.map(g=>g.mw),1);
  const names={"BW":"Baden-Württemberg","BY":"Bayern","BE":"Berlin","BB":"Brandenburg",
    "HB":"Bremen","HH":"Hamburg","HE":"Hessen","MV":"Mecklenburg-Vorpommern",
    "NI":"Niedersachsen","NW":"Nordrhein-Westfalen","RP":"Rheinland-Pfalz",
    "SL":"Saarland","SN":"Sachsen","ST":"Sachsen-Anhalt","SH":"Schleswig-Holstein","TH":"Thüringen"};
  p.innerHTML=`
    <h2>${z.zone}</h2>
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
    <div id="mix"><h3>Generation mix</h3>
      ${z.generation.map(g=>`<div class="mrow"><span>${g.name}</span><span class="mw">${Math.round(g.mw).toLocaleString("en-US")} MW</span><div class="mtrack"><div class="mbar${g.renewable?" ren":""}" style="width:${(100*g.mw/vmax).toFixed(1)}%"></div></div></div>`).join("")}
    </div>
    <div class="asof">Data as of ${z.as_of?new Date(z.as_of).toLocaleString("de-DE",{timeZone:"Europe/Berlin"}):"–"} (Europe/Berlin)</div>`;
}
async function refresh(){
  try{
    const r=await (await fetch("/api/regions")).json();
    if(r.error) throw new Error(r.error);
    data=r;
    const times=Object.values(r.zones).map(z=>z.as_of).filter(Boolean).sort();
    $("stamp").textContent="data as of "+
      (times.length?new Date(times.at(-1)).toLocaleString("de-DE",{timeZone:"Europe/Berlin"}):"–")+
      " · SMARD / Bundesnetzagentur";
    paint();
  }catch(e){
    $("stamp").innerHTML='<span style="color:#f85149">update failed: '+e.message+"</span>";
  }
}
(async()=>{
  geo=await (await fetch("/geo/states")).json();
  renderChips();renderLegend();drawMap();
  await refresh();
  setInterval(refresh,60000);
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
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/map":
                self._send(MAP_PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/data":
                self._json(snapshot())
            elif path == "/api/regions":
                self._json(regions_snapshot())
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
