# de-energy-live

Real-time German electricity supply & demand from two public, keyless APIs:

- **energy-charts.info** (Fraunhofer ISE) — 15-min generation mix, load,
  renewable share, day-ahead price
- **smard.de** (Bundesnetzagentur) — official grid load ("Netzlast") and
  realised generation by source

Plus register data from the **Marktstammdatenregister (MaStR)** — the full
fleet of German generation/storage/consumer units with capacity, location
(PLZ) and operator, via the daily bulk export (no API key).

Pure Python 3.10+ standard library for data fetching; `matplotlib` is only
needed for the `chart` command.

## Setup

```bash
pip install -r requirements.txt   # matplotlib, for `chart`
pip install -e .                  # optional: `de-energy` command
```

## Usage

```bash
python main.py now                    # live snapshot: load, generation mix, price
python main.py now --csv data/out.csv # snapshot + append to CSV
python main.py today                  # today's energy totals per source (GWh)
python main.py smard                  # official BNetzA load + generation
python main.py chart -o out.png       # generation vs load chart
python main.py regions                # snapshot per TSO control area
python main.py watch -i 60            # auto-refreshing snapshot
python main.py web                    # web dashboard on 0.0.0.0:8000

# MaStR register data (one-time bulk download, then offline queries):
python main.py mastr update           # fetch+parse Gesamtdatenexport -> data/mastr/mastr.db
python main.py mastr summary          # totals per unit category
python main.py mastr top -n 10        # largest units per category
python main.py mastr top --type offshore -n 20   # one category only
python main.py mastr batteries -n 15  # largest storage units
python main.py mastr plz 10115        # units + consumers at a postcode
python main.py mastr plz --top -n 20  # postcodes ranked by registered capacity
# or, after `pip install -e .`:
de-energy now
```

`mastr update` downloads only the needed members of the
`Gesamtdatenexport_*.zip` (HTTP range reads against
download.marktstammdatenregister.de — the full zip is ~3.3 GB, the needed
parts ~1.8 GB), caches them under `data/mastr/zip/` and builds a sqlite db
(`data/mastr/mastr.db`, gitignored). Re-run it any time to pick up the
latest daily export; unchanged members are reused. Options:
`--groups solar,wind` (subset), `--skip-operators` (skip the ~165 MB
Marktakteure files; operator column then shows raw MaStR numbers),
`--force`, `-j N` (parse workers).

`mastr top` categories: coal, lignite, gas, thermal (other), biomass,
hydro, pumped (pumped storage), offshore, onshore, solar (Freifläche
parks), battery, nuclear (all decommissioned — use `--all`), othergen —
plus `offshore-parks`/`onshore-parks` which aggregate turbines into wind
parks. Add `--all` to include planned/decommissioned units (default
filter: `In Betrieb`).

**Demand-by-PLZ caveat:** MaStR registers consumer *units*
(Stromverbraucher/Gasverbraucher) but no consumption in MW — so
`mastr plz --top` ranks postcodes by registered generation+storage
capacity and lists consumer-unit counts separately. True demand by PLZ
is not available in MaStR.

## Web UI

```bash
python main.py web --port 8000        # then open http://localhost:8000
```

A single-page dashboard showing the live grid picture — current load,
total generation, renewable share, national CO2 intensity estimate,
day-ahead spot price, per-source generation bars, the stacked
supply-vs-demand chart, and the regional control-area map (below).
The page fetches fresh data every 60 s; server-side responses are
cached ~55 s so the upstream API isn't hammered. Pure stdlib
(`http.server`) — no extra dependencies; `/chart.png` reuses the
`chart` command's matplotlib figure (needs `matplotlib`, else that
one image fails but the page still works).

Endpoints: `/` dashboard · `/map` → redirects to `/#regions` ·
`/api/data` JSON snapshot (incl. `co2_intensity_gkwh`) ·
`/api/regions` per-zone JSON · `/api/mastr` register top-units
JSON (only after `mastr update`) · `/api/mastr/capacity`
installed capacity per source (`?bundesland=BY|Bayern` optional) ·
`/api/mastr/batteries` paginated battery units
(`?limit=&offset=&plz=<prefix>&bundesland=&sort=&all_types=1`) ·
`/api/mastr/map?type=<slug>` per-Bundesland aggregation +
geolocated markers for one unit layer (battery, storage, solar,
solar-park, onshore, offshore, coal, gas, biomass, hydro, nuclear;
`?markers=` caps markers, default 2000 — only coords inside a
Germany bbox count as located; the rest live in the state totals) ·
`/geo/states` GeoJSON ·
`/chart.png` · `/healthz`. The dashboard shows "Largest registered
units", "Installed capacity", a browsable "Battery storage"
section (with a small map) and a "MaStR unit map" section
(per-state choropleth + largest located units per layer) once the
MaStR cache exists.

### Regional map (dashboard section)

An Electricity Maps-style view of Germany split into its four TSO
control areas — 50Hertz (east), Amprion (west/Ruhr), TenneT
(north+south backbone), TransnetBW (Baden-Württemberg) — embedded in
the main page. Each zone's live net load and realised generation come
from SMARD's per-region series (15-min resolution), and the map
colours the federal states by selectable signals: estimated CO2
intensity (generation mix × lifecycle emission factors), renewable
share, net load, and self-sufficiency (generation/load). Click a
region for its mix breakdown. State→zone assignment is approximate —
control-area borders do not follow state borders exactly. The
national CO2 card uses the same lifecycle factors on the
energy-charts national mix.

## Data

- **Supply**: generation by source (solar, wind on/offshore, hydro, biomass,
  gas, coal, etc.) in MW
- **Demand**: grid load and residual load in MW
- **Renewable share** of load (actual + forecast signal for the day)
- **Day-ahead spot price** for the DE-LU bidding zone
- **Cross-border electricity trading** (net import/export)

API endpoints used (all public, no auth):

| Endpoint | Content |
|---|---|
| `GET api.energy-charts.info/total_power?bzn=DE-LU` | total generation + load + renewable share |
| `GET api.energy-charts.info/signal?country=de` | renewable-share forecast ("energy signal") |
| `GET api.energy-charts.info/price?bzn=DE-LU` | day-ahead spot price |
| `GET smard.de/app/chart_data/<filter>/DE/...` | official load & realised generation |
| `GET smard.de/app/chart_data/<filter>/<50Hertz|Amprion|TenneT|TransnetBW>/...` | per-control-area load & generation |

## Layout

```
de_energy_live/
  model.py               TimeSeries container
  sources/
    energy_charts.py     Fraunhofer ISE energy-charts client
    smard.py             Bundesnetzagentur SMARD client
    mastr.py             MaStR Gesamtdatenexport: remote-zip range reader,
                         streaming XML scanner, sqlite cache builder
  cli.py                 argparse CLI (now/today/smard/chart/regions/web/watch/mastr)
  regional.py            per-control-area snapshots (load, mix, CO2, self-sufficiency)
  web.py                 stdlib http.server dashboard + regional map
  bundeslaender.geo.json state polygons for the map (deutschlandGeoJSON, CC-BY)
main.py                  thin entry point
```

## Tests

```bash
python3 -m unittest discover -s tests   # stdlib, no deps needed
# or: pytest                            # if installed
```

Note: SMARD reports energy per interval (MWh); the client converts to
average MW. energy-charts reports power (MW) directly.

## Web stack (single container)

`docker compose up -d` builds and runs the whole energy stack — public entry
on `127.0.0.1:8090` (serve via Cloudflare Tunnel or your own proxy):

- `/` — unified layer map (`map/`): EU price choropleth with 72h/15-min
  playback, world carbon/renewable choropleths, flow animation, MaStR unit
  markers + per-state capacity
- `/grid/` — world power-grid map (`grid/`), live data via
  `grid/build/refresh_loop.sh` (15-min refresh)
- `/price/` — European price map (`price/`), data via
  `price/refresh_loop.sh` (30-min refresh)
- `/de/`, `/api/*`, `/geo/*` — MaStR/grid dashboard + API (`main.py web`,
  needs `data/mastr/mastr.db`, ~9.7M units — gitignored, kept on the host)
- `/tabs` — legacy multi-app iframe shell (`unified/`)

Data under `data/`, `grid/data/*.json`, `price/data/` is generated at
runtime and gitignored.
