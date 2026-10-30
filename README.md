# de-energy-live

Real-time German electricity supply & demand from two public, keyless APIs:

- **energy-charts.info** (Fraunhofer ISE) — 15-min generation mix, load,
  renewable share, day-ahead price
- **smard.de** (Bundesnetzagentur) — official grid load ("Netzlast") and
  realised generation by source

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
python main.py watch -i 60            # auto-refreshing snapshot
python main.py web                    # web dashboard on 0.0.0.0:8000
# or, after `pip install -e .`:
de-energy now
```

## Web UI

```bash
python main.py web --port 8000        # then open http://localhost:8000
```

A single-page dashboard showing the live grid picture — current load,
total generation, renewable share, day-ahead spot price, per-source
generation bars, and the stacked supply-vs-demand chart. The page fetches
fresh data every 60 s; server-side responses are cached ~55 s so the
upstream API isn't hammered. Pure stdlib (`http.server`) — no extra
dependencies; `/chart.png` reuses the `chart` command's matplotlib figure
(needs `matplotlib`, else that one image fails but the page still works).

Endpoints: `/` dashboard · `/api/data` JSON snapshot · `/chart.png` ·
`/healthz`.

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

## Layout

```
de_energy_live/
  model.py               TimeSeries container
  sources/
    energy_charts.py     Fraunhofer ISE energy-charts client
    smard.py             Bundesnetzagentur SMARD client
  cli.py                 argparse CLI (now/today/smard/chart/web/watch)
  web.py                 stdlib http.server dashboard (/, /api/data, /chart.png)
main.py                  thin entry point
```

## Tests

```bash
python3 -m unittest discover -s tests   # stdlib, no deps needed
# or: pytest                            # if installed
```

Note: SMARD reports energy per interval (MWh); the client converts to
average MW. energy-charts reports power (MW) directly.
