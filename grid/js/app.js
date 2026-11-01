/* global L, DataProvider */
(() => {
  const SIG = {
    price:     { label: "Electricity price", unit: "/MWh",
                 stops: [[-20,"#1db97c"],[0,"#2aa364"],[60,"#b6d84c"],[110,"#f5a623"],[170,"#e2543a"],[260,"#7d3a2d"]] },
    carbon:    { label: "Carbon intensity", unit: "gCO₂eq/kWh",
                 stops: [[0,"#2aa364"],[150,"#b6d84c"],[350,"#f5a623"],[550,"#e2543a"],[820,"#7d3a2d"]] },
    renewable: { label: "Renewable share", unit: "%",
                 stops: [[0,"#e2543a"],[25,"#f5a623"],[50,"#b6d84c"],[75,"#4dc47d"],[100,"#2aa364"]] },
  };
  const MIX_COLORS = {
    nuclear:"#b86bd6", hydro:"#3f8fdf", wind:"#6fd3c9", solar:"#f5d74d",
    coal:"#5a5f6e", gas:"#e2764a", biomass:"#7cc24d", other:"#8b98ad",
    wind_onshore:"#6fd3c9", wind_offshore:"#4db0c9", lignite:"#7a6146",
    hard_coal:"#4a4f5e", oil:"#8a5a3a", waste:"#9a8f6a", pumped:"#5aa0e0",
    other_renew:"#a3c96e",
  };
  const MIX_LABEL = {
    wind_onshore:"wind onshore", wind_offshore:"wind offshore",
    hard_coal:"hard coal", other_renew:"other renew.", pumped:"pumped hydro",
  };

  let dataset, geoLayer, signal = "carbon", hour = 12, selected = null, zoneClicked = false;
  let flows = [], flowLines = [], playing = false, playTimer = null;

  const $ = (id) => document.getElementById(id);
  const tip = $("tooltip");

  /* ---------- color helpers ---------- */
  function hex2rgb(h) { return [parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)]; }
  function interpStops(st, v) {
    if (v == null || isNaN(v)) return "#1a2233";
    if (v <= st[0][0]) return st[0][1];
    for (let i = 1; i < st.length; i++) {
      if (v <= st[i][0]) {
        const [v0,c0] = st[i-1], [v1,c1] = st[i];
        const t = (v - v0) / (v1 - v0);
        const a = hex2rgb(c0), b = hex2rgb(c1);
        return `rgb(${a.map((x,j)=>Math.round(x+(b[j]-x)*t)).join(",")})`;
      }
    }
    return st[st.length-1][1];
  }
  function colorFor(sig, v) { return interpStops(SIG[sig].stops, v); }

  /* ---------- map ---------- */
  const map = L.map("map", { zoomControl: true, minZoom: 2, maxZoom: 7,
    worldCopyJump: true, attributionControl: true });
  window.__map = map;  // debug/testing handle
  const q = new URLSearchParams(location.search);   // shareable view: ?lat=&lon=&z=
  map.setView([+q.get("lat") || 46, +q.get("lon") || 10], +q.get("z") || 3);

  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 12,
    attribution: "Esri &copy; · demo data",
  }).addTo(map);

  function zoneOf(f) { return f.properties.zone; }
  function zoneData(zid) { return dataset.zones[zid]; }

  function featureStyle(f) {
    const z = zoneData(zoneOf(f));
    const v = z ? DataProvider.valueAt(z, signal, hour) : null;
    return { fillColor: colorFor(signal, v), fillOpacity: z ? 0.75 : 0.35,
             color: z ? "#0b0e14" : "#232c40", weight: z ? 0.8 : 0.4 };
  }

  function fmtPrice(z) {
    const v = DataProvider.valueAt(z, "price", hour);
    return `${z.currency}${v.toFixed(0)}/MWh`;
  }

  function refreshColors() {
    geoLayer && geoLayer.setStyle(featureStyle);
  }

  function showTooltip(e, f) {
    const zid = zoneOf(f), z = zoneData(zid);
    let html;
    if (!z) {
      html = `<div class="tt-name">${f.properties.name}</div><div class="tt-val">no data</div>`;
    } else {
      const p = DataProvider.valueAt(z, "price", hour),
            c = DataProvider.valueAt(z, "carbon", hour),
            r = DataProvider.valueAt(z, "renewable", hour);
      html = `<div class="tt-name">${z.flag||""} ${z.name}${z.tso?` <span class="tt-tso">${z.tso}</span>`:""}</div>
        <div class="tt-val"><span>⚡ price</span><b>${z.currency}${num(p)}/MWh</b></div>
        <div class="tt-val"><span>🌿 carbon</span><b>${num(c)} g/kWh</b></div>
        <div class="tt-val"><span>♻️ renewable</span><b>${num(r)} %</b></div>`;
    }
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const x = Math.min(e.originalEvent.clientX + 14, innerWidth - 180);
    const y = Math.min(e.originalEvent.clientY + 14, innerHeight - 110);
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }

  /* ---------- detail panel ---------- */
  function sparkline(vals) {
    const w = 280, h = 42, ok = vals.filter(v => v != null && !isNaN(v));
    if (!ok.length) return "";
    const lo = Math.min(...ok), hi = Math.max(...ok);
    const y = v => h-4-((v-lo)/((hi-lo)||1))*(h-8);
    const pts = vals.map((v,i) => v==null?null:`${(i/(vals.length-1)*w).toFixed(1)},${y(v).toFixed(1)}`)
      .filter(Boolean).join(" ");
    const cx = hour/(vals.length-1)*w;
    const marker = vals[hour]!=null
      ? `<circle cx="${cx}" cy="${y(vals[hour]).toFixed(1)}" r="3" fill="#fff"/>` : "";
    return `<svg class="spark" viewBox="0 0 ${w} ${h}">
      <polyline points="${pts}" fill="none" stroke="#4a90d9" stroke-width="1.6"/>${marker}
    </svg>`;
  }

  const num = (v, d=0) => (v==null||isNaN(v)) ? "–" : v.toFixed(d);

  function renderPanel(zid) {
    const z = zoneData(zid);
    const p = DataProvider.valueAt(z,"price",hour), c = DataProvider.valueAt(z,"carbon",hour),
          r = DataProvider.valueAt(z,"renewable",hour);
    const estOf = (f) => z.estimated && z.estimated.includes(f) ? "ᵉˢᵗ" : "";
    // generation mix: real MW when the fetcher provided them (live zones),
    // else estimate MW from the share of demand
    let mixRows, mixHead;
    if (z.mixMW) {
      const tot = Object.values(z.mixMW).reduce((a,b)=>a+b,0) || 1;
      mixHead = `Generation mix — live${z.tso?` · ${z.tso} area`:""}`;
      mixRows = Object.entries(z.mixMW).filter(([,v])=>v>0)
        .sort((a,b)=>b[1]-a[1])
        .map(([k,v])=>`<div class="mixrow"><span class="dot" style="background:${MIX_COLORS[k]||"#888"}"></span>
          <span class="n">${MIX_LABEL[k]||k}</span><span class="mw">${Math.round(v)} MW</span><span class="p">${(v/tot*100).toFixed(1)}%</span></div>`).join("");
    } else {
      const demand = z.demandGW * 1000;
      mixHead = "Generation mix (est. output)";
      mixRows = Object.entries(z.mix).filter(([,v])=>v>0)
        .sort((a,b)=>b[1]-a[1])
        .map(([k,v])=>`<div class="mixrow"><span class="dot" style="background:${MIX_COLORS[k]||"#888"}"></span>
          <span class="n">${MIX_LABEL[k]||k}</span><span class="mw">${Math.round(demand*v/100)} MW</span><span class="p">${v}%</span></div>`).join("");
    }
    const barSrc = z.mixMW
      ? Object.entries(z.mixMW).map(([k,v])=>[k, v/(Object.values(z.mixMW).reduce((a,b)=>a+b,0)||1)*100])
      : Object.entries(z.mix);
    const bar = barSrc.filter(([,v])=>v>0)
      .map(([k,v])=>`<div style="width:${v}%;background:${MIX_COLORS[k]||"#888"}"></div>`).join("");
    const xch = dataset.exchanges.filter(e=>e.f===zid||e.t===zid)
      .map(e=>{
        const out = e.f===zid, other = zoneData(out?e.t:e.f);
        const mw = Math.abs(e.mw);
        return `<div class="xrow"><span>${out?"→ export to":"← import from"} ${other?other.name:e.t}</span><b>${mw} MW</b></div>`;
      }).join("");
    const tsoCard = z.tso
      ? `<div class="p-card"><div class="k">⚙️ TSO control area</div><div class="v">${z.tso}<span class="u">${z.tsoLoadGW?" · "+z.tsoLoadGW+" GW load":""}</span></div></div>` : "";

    $("panelBody").innerHTML = `
      <div class="p-zone">${z.flag||""} ${z.name} ${z.live?'<span class="live-chip">● LIVE</span>':""}</div>
      <div class="p-id">${zid} · ${String(hour).padStart(2,"0")}:00 UTC</div>
      <div class="p-grid">
        <div class="p-card"><div class="k">⚡ price</div><div class="v">${z.currency}${num(p)}<span class="u">/MWh</span></div></div>
        <div class="p-card"><div class="k">🌿 carbon</div><div class="v">${num(c)}<span class="u"> gCO₂eq/kWh</span></div></div>
        <div class="p-card"><div class="k">♻️ renewable</div><div class="v">${num(r)}<span class="u"> %</span></div></div>
        <div class="p-card"><div class="k">🔋 low-carbon</div><div class="v">${num(z.lowCarbon)}<span class="u"> %</span></div></div>
        <div class="p-card"><div class="k">📊 demand${estOf("demandGW")}</div><div class="v">${num(z.demandGW,1)}<span class="u"> GW</span></div></div>
        ${tsoCard}
      </div>
      <div class="p-h">${SIG[signal].label} — last 24h</div>
      ${sparkline(z.series[signal]||[])}
      <div class="p-h">${mixHead}</div>
      <div class="mixbar">${bar}</div>
      ${mixRows}
      ${xch ? `<div class="p-h">Cross-border exchanges${z.live?" (live)":" (est.)"}</div>${xch}` : ""}
      ${z.sources?`<div class="p-src">${z.sources}</div>`:""}`;
    $("panel").classList.remove("hidden");
  }

  /* ---------- exchange flow arrows ---------- */
  function bearing(a, b) { // a,b L.LatLng -> deg
    const pa = map.project(a, 3), pb = map.project(b, 3);
    return Math.atan2(pb.x - pa.x, -(pb.y - pa.y)) * 180 / Math.PI;
  }

  function buildFlows() {
    flows.forEach(f => f.markers.forEach(m => map.removeLayer(m)));
    flowLines.forEach(l => map.removeLayer(l));
    flows = []; flowLines = [];
    if (!$("flowToggle").checked) return;

    dataset.exchanges.forEach(e => {
      let A = zoneData(e.f), B = zoneData(e.t);
      if (!A || !B || !A.center || !B.center) return;
      let a = L.latLng(A.center[1], A.center[0]), b = L.latLng(B.center[1], B.center[0]);
      let mw = e.mw;
      if (mw < 0) { [a,b] = [b,a]; mw = -mw; }
      const dist = a.distanceTo(b);
      // shorten the line so arrows sit on the border region, not city centers
      const t0 = Math.min(0.25, 60000/dist || 0), t1 = 1 - t0;
      const seg = L.polyline([a,b], { className: "flow-line", weight: 1.2, interactive: false });
      seg.addTo(map); flowLines.push(seg);
      const ang = bearing(a,b);
      const n = mw > 1500 ? 3 : mw > 500 ? 2 : 1;
      const markers = [];
      for (let i = 0; i < n; i++) {
        const ic = L.divIcon({ className: "flow-particle", iconSize: [10,10],
          html: `<div style="transform:rotate(${ang.toFixed(1)}deg)"></div>` });
        const m = L.marker(a, { icon: ic, interactive: false, keyboard: false });
        m.addTo(map); markers.push(m);
      }
      flows.push({ a, b, t0, t1, markers, speed: 0.10 + Math.min(mw,4000)/4000*0.22, phase: 0 });
    });
  }

  let lastT = 0;
  function tick(ts) {
    const dt = Math.min(0.05, (ts - lastT) / 1000 || 0); lastT = ts;
    flows.forEach(f => {
      f.phase = (f.phase + dt * f.speed) % 1;
      f.markers.forEach((m,i) => {
        const t = f.t0 + ((f.phase + i / f.markers.length) % 1) * (f.t1 - f.t0);
        m.setLatLng(L.latLng(f.a.lat + (f.b.lat - f.a.lat)*t, f.a.lng + (f.b.lng - f.a.lng)*t));
      });
    });
    requestAnimationFrame(tick);
  }

  /* ---------- battery storage layer (MaStR register, Germany only) ---------- */
  const BATT_STOPS = [[0,"#2aa364"],[1,"#b6d84c"],[10,"#f5a623"],[50,"#e2543a"],[140,"#d63a5a"]];
  let battLayer = null, battLoading = null;

  const esc = (s) => String(s).replace(/[&<>"']/g,
    c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));

  function battRadius(mw) { return Math.min(11, 2 + Math.sqrt(mw) * 0.9); }

  function battPopup(b) { // b: [lat,lon,mw,name,op,ort,plz,bl,since]
    const rows = [
      ["capacity", b[2] >= 1 ? `${b[2].toFixed(1)} MW` : `${Math.round(b[2]*1000)} kW`],
      b[4] && ["operator", esc(b[4])],
      (b[5] || b[6] || b[7]) && ["location", esc(`${[b[6], b[5]].filter(Boolean).join(" ")}${b[7] ? ", " + b[7] : ""}`)],
      b[8] && ["in service", esc(b[8])],
    ].filter(Boolean)
      .map(([k, v]) => `<div class="tt-val"><span>${k}</span><b>${v}</b></div>`).join("");
    return `<div class="tt-name">🔋 ${esc(b[3]) || "Battery storage"}</div>${rows}`;
  }

  function buildBatteries(doc) {
    const renderer = L.canvas({ padding: 0.3 });
    return L.layerGroup(doc.batteries.map(b => {
      const m = L.circleMarker([b[0], b[1]], {
        renderer, radius: battRadius(b[2]),
        color: "#0b0e14", weight: 0.5,
        fillColor: interpStops(BATT_STOPS, b[2]), fillOpacity: 0.85,
      });
      m.bindPopup(battPopup(b));
      return m;
    }));
  }

  function toggleBatteries() {
    const on = $("battToggle").checked;
    if (battLayer) { on ? battLayer.addTo(map) : map.removeLayer(battLayer); return; }
    if (!on) return;
    if (!battLoading) {
      battLoading = DataProvider.loadBatteries()
        .then(doc => { battLayer = buildBatteries(doc); return true; })
        .catch(err => { battLoading = null; console.error("batteries:", err);
                        $("battToggle").checked = false; return false; });
    }
    battLoading.then(ok => { if (ok && $("battToggle").checked) battLayer.addTo(map); });
  }

  /* ---------- legend ---------- */
  function renderLegend() {
    const st = SIG[signal].stops;
    $("legendTitle").textContent = `${SIG[signal].label} (${SIG[signal].unit})`;
    const grad = `linear-gradient(90deg, ${st.map(s=>`${s[1]} ${(s[0]-st[0][0])/(st[st.length-1][0]-st[0][0])*100}%`).join(",")})`;
    $("legendBar").style.background = grad;
    $("legendTicks").innerHTML =
      `<span>${st[0][0]}</span><span>${st[Math.floor(st.length/2)][0]}</span><span>${st[st.length-1][0]}+</span>`;
  }

  /* ---------- boot ---------- */
  Promise.all([DataProvider.loadDataset(), DataProvider.loadGeometries()])
    .then(([data, geo]) => {
      dataset = data;
      const liveN = Object.values(data.zones).filter(z => z.live).length;
      $("clock").textContent =
        (liveN ? "● live · " : "snapshot ") +
        new Date(data.generatedAt).toUTCString().slice(17,22) + " UTC · " +
        Object.keys(data.zones).length + " zones (" + liveN + " live) · " +
        data.exchanges.length + " interconnectors";

      // start the timeline at the current UTC hour
      hour = new Date().getUTCHours();
      $("hourSlider").value = hour;
      $("hourLabel").textContent = String(hour).padStart(2,"0") + ":00";

      geoLayer = L.geoJSON(geo, {
        style: featureStyle,
        onEachFeature: (f, layer) => {
          layer.on({
            mouseover: (e) => { layer.setStyle({ weight: 2, color: "#fff" }); showTooltip(e, f); },
            mousemove: (e) => showTooltip(e, f),
            mouseout: () => { geoLayer.resetStyle(layer); tip.classList.add("hidden"); },
            click: () => {
              zoneClicked = true; setTimeout(() => zoneClicked = false, 0);
              const zid = zoneOf(f);
              if (zoneData(zid)) { selected = zid; renderPanel(zid); }
            },
          });
        },
      }).addTo(map);

      buildFlows();
      requestAnimationFrame(tick);
      renderLegend();
    })
    .catch(err => {
      document.body.insertAdjacentHTML("beforeend",
        `<div style="position:absolute;top:80px;left:20px;z-index:2000;background:#5a1f1f;padding:12px;border-radius:8px">Failed to load data: ${err.message}<br/>Serve via a web server (e.g. <code>python3 -m http.server</code>) — file:// won't work.</div>`);
    });

  /* ---------- UI wiring ---------- */
  document.querySelectorAll(".sig-btn").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".sig-btn").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    signal = b.dataset.sig;
    refreshColors(); renderLegend();
    if (selected) renderPanel(selected);
  }));

  $("hourSlider").addEventListener("input", (e) => {
    hour = +e.target.value;
    $("hourLabel").textContent = String(hour).padStart(2,"0") + ":00";
    refreshColors();
    if (selected) renderPanel(selected);
  });

  $("playBtn").addEventListener("click", () => {
    playing = !playing;
    $("playBtn").textContent = playing ? "⏸" : "▶";
    if (playing) {
      playTimer = setInterval(() => {
        hour = (hour + 1) % 24;
        $("hourSlider").value = hour;
        $("hourLabel").textContent = String(hour).padStart(2,"0") + ":00";
        refreshColors();
        if (selected) renderPanel(selected);
      }, 700);
    } else clearInterval(playTimer);
  });

  $("flowToggle").addEventListener("change", buildFlows);
  $("battToggle").addEventListener("change", toggleBatteries);
  if (q.get("batteries") === "1") {
    $("battToggle").checked = true;
    toggleBatteries();
  }
  $("panelClose").addEventListener("click", () => { $("panel").classList.add("hidden"); selected = null; });
  map.on("click", () => { if (!zoneClicked) { $("panel").classList.add("hidden"); selected = null; } });
})();
