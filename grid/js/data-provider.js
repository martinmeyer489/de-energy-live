/* Data provider.
 *
 * Currently loads the bundled snapshot (data/data.json) — realistic
 * ENTSO-E/EIA-style values with 24 hourly points per zone.
 *
 * To plug in a live feed, replace loadDataset() with a fetch to your API
 * that returns the same shape:
 * {
 *   generatedAt: ISO8601,
 *   signals: { price|carbon|renewable: {label, unit} },
 *   zones:   { ZONE_ID: {name, flag, currency, price, carbon, renewable,
 *                        lowCarbon, demandGW, mix:{source:%}, center:[lon,lat],
 *                        series:{price:[24],carbon:[24],renewable:[24]}} },
 *   exchanges: [{f: "ZONE_A", t: "ZONE_B", mw: signed_MW_A_to_B}]
 * }
 */
window.DataProvider = (() => {
  async function loadDataset() {
    const res = await fetch("data/data.json");
    if (!res.ok) throw new Error("data.json: HTTP " + res.status);
    return res.json();
  }

  async function loadGeometries() {
    const res = await fetch("data/zones.geojson");
    if (!res.ok) throw new Error("zones.geojson: HTTP " + res.status);
    return res.json();
  }

  /* MaStR battery storage units — columnar JSON, see build/build_batteries.py */
  async function loadBatteries() {
    const res = await fetch("data/batteries.json");
    if (!res.ok) throw new Error("batteries.json: HTTP " + res.status);
    return res.json();
  }

  /* Value of `signal` in `zone` at hour h (falls back to the snapshot value). */
  function valueAt(zone, signal, h) {
    const s = zone.series && zone.series[signal];
    return s ? s[h % s.length] : zone[signal];
  }

  return { loadDataset, loadGeometries, loadBatteries, valueAt };
})();
