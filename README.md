# de-energy-live

Real-time German electricity supply & demand, powered by the public
[energy-charts.info](https://energy-charts.info) API (Fraunhofer ISE).
No API key required. Data granularity: 15 minutes.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py now              # live snapshot: load, generation mix, price
python main.py today            # today's energy totals per source (GWh)
python main.py chart -o out.png # generation vs load chart
python main.py watch -i 60      # auto-refreshing snapshot
```

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
| `GET /total_power?country=de` | total generation + load + renewable share |
| `GET /public_power?country=de` | generation feeding the public grid |
| `GET /signal?country=de` | renewable-share forecast ("energy signal") |
| `GET /price?bzn=DE-LU` | day-ahead spot price |
