# Electricity Prices · Europe

Electricity Maps-style interactive map of European bidding zones, colored by
day-ahead spot price. Dark theme, 72h playback at 15-min resolution, and a
per-zone detail panel (price chart, estimated carbon intensity, generation
mix, cross-border flows).

**Germany drill-down**: clicking the DE zone overlays the 16 Bundesländer
(colored by renewable share of their TSO control area) and lists the four
control areas — 50Hertz, Amprion, TenneT, TransnetBW — with live load,
generation and mix from SMARD. Clicking a state highlights its control-area
card. `index.html#de` deep-links straight to the German view.

## Run

```bash
cd electricity-price-map
python3 -m http.server 8321
# open http://localhost:8321/
```

Static only — no build step, Leaflet is vendored in `vendor/`.

## Data

All real, keyless, from api.energy-charts.info (Fraunhofer ISE / SMARD,
CC BY 4.0): `/price` (day-ahead), `/public_power` (generation mix), `/cbpf`
(cross-border physical flows). German regions come from smard.de
(Bundesnetzagentur): realised generation + consumption per TSO control area
at quarter-hour resolution (values are MWh per interval, converted to MW).
Carbon intensity estimated from the mix with IPCC AR5 factors. Zone polygons
from electricitymaps-contrib world.geojson; Bundesländer polygons in
`geo/de_states.geojson` (derived from the combined-dashboard geometry).

Refresh with:

```bash
python3 fetch_data.py   # rewrites data/data.json + geo/zones.geojson
sh refresh_loop.sh      # keeps data.json fresh every 30 min (pidfile-deduped)
```

The frontend reads only `data/data.json` — swap in a live API serving the
same payload later if needed.

## Caveats

- Zones without energy-charts price coverage (GB, Ireland, Italy, Balkans,
  islands) render as no-data.
- Basemap: Esri World Dark Gray (keyless). CARTO basemaps require an API key.
- SMARD publishes control-area data, not per-state: every Bundesland in a
  control area shares that area's mix; per-state load shown in tooltips is an
  estimate (control-area load × fixed consumption share). Bundesland→TSO
  boundaries are approximated by state borders — the real control areas split
  some states.
- Country-level generation mix (energy-charts) lags prices by ~2h, so the
  newest slider positions may show "no data at this time" for mix/CO₂.
