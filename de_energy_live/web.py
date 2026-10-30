"""Live web dashboard for German electricity supply & demand.

Pure stdlib (http.server). Serves:
  /            single-page dashboard (auto-refreshes every ~60 s)
  /api/data    current snapshot as JSON
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
from zoneinfo import ZoneInfo

from .cli import _generation_snapshot, _latest, _today
from .sources import energy_charts

BERLIN = ZoneInfo("Europe/Berlin")
CACHE_TTL = 55  # seconds; upstream resolves at 15 min anyway

_cache: dict = {"snapshot": None, "expires": 0.0, "error": None}
_lock = threading.Lock()


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
            elif path == "/api/data":
                self._json(snapshot())
            elif path == "/chart.png":
                self._send(chart_png(), "image/png")
            elif path == "/healthz":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            if path == "/api/data":
                self._json({"error": str(e), "generation": []}, 502)
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
