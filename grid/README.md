# Power Grid Map

An Electricity Maps–style interactive web app: a dark world map where every
country — and sub-national regions where data exists — is colored by a live
electricity signal.

## Run it

```bash
cd electricity-map
python3 -m http.server 8000
# open http://localhost:8000
```

Any static file server works. `file://` will NOT work because the data files
are loaded with `fetch()`.

## Features

- **Signals** (top bar): electricity price, carbon intensity, renewable share.
  Choropleth + legend update instantly.
- **Germany in regional detail**: all 16 Bundesländer are separate zones,
  grouped into the 4 TSO control areas (50Hertz, Amprion, TenneT, TransnetBW).
  Clicking a Bundesland shows its TSO badge, the control area's **live
  generation mix in real MW** (wind onshore/offshore, solar, lignite, gas,
  biomass, hydro, pumped storage, …), the control-area load, and an estimated
  per-state demand (population share).
- **Sub-national detail elsewhere**: US ISO/RTO regions, Italy's 6 market
  zones, Norway NO1–NO5, Sweden SE1–SE4, Denmark DK1/DK2, all-island SEM.
- **Hover** for a tooltip; **click** for a detail panel with all signals, a
  24h sparkline, generation mix and cross-border exchanges.
- **Time slider / play** scrubs the last 24h; it starts at the current UTC
  hour.
- **Animated exchange arrows** for real cross-border physical flows.
- Zones marked `● LIVE` serve real data; missing/unavailable fields are
  tagged `ᵉˢᵗ` or fall back to the bundled snapshot (non-European zones).

## Data (live, no API keys)

Rebuild/refresh with:

```bash
python3 build/build_zones.py      # geometry + fallback snapshot
python3 build/fetch_live.py       # overlay live data onto data/data.json
python3 build/build_batteries.py  # MaStR battery layer -> data/batteries.json (on demand)
```

The 🔋 Batteries topbar toggle lazy-loads `data/batteries.json` (~6.3k
geocoded in-service German battery units, ~4.9 GW) as canvas circleMarkers —
open with `?batteries=1` (plus `&lat=&lon=&z=` for a shareable view).

| Layer | Source | Notes |
|---|---|---|
| Prices | `api.energy-charts.info /price` | day-ahead per bidding zone (CC BY 4.0 / SMARD); all German states share DE-LU |
| Generation mix + load (Europe) | `api.energy-charts.info /public_power` | country-level; subzones share it |
| German regions | `smard.de` keyless chart API | realised generation by source + consumption per TSO control area |
| Exchange flows | `api.energy-charts.info /cbpf` | real physical flows; mapped to the bordering Bundesland for Germany |
| Carbon / renewable / low-carbon | computed | IPCC AR5 lifecycle emission factors applied to the real mix |

Estimated: per-Bundesland demand (control-area load × population share),
demand for sub-zones sharing a country, and everything outside Europe.

`data/zones.geojson` is built by `build/build_zones.py` from Natural Earth
admin-1 / johan-world.geo.json / PublicaMundi US states.

## Stack

Vanilla HTML/JS + Leaflet 1.9.4 (vendored in `vendor/`), Esri dark basemap
tiles. No build step, no API keys.
