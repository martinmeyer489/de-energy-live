/* Unified energy map — one Leaflet map, toggleable layers.
 *
 * Data sources (reused, no new fetchers):
 *   /price/data/data.json + geo/zones.geojson  EU day-ahead prices, 288×15min (electricity-price-map pipeline)
 *   /price/geo/de_states.geojson               16 Bundesländer polygons
 *   /grid/data/data.json + data/zones.geojson  world zones: price/carbon/renewable, 24h series, exchanges
 *   /api/mastr/map?type=<slug>                 MaStR register: state aggregates + geolocated unit markers
 */
/* global L */
(() => {
  const $ = (id) => document.getElementById(id);
  const tip = $("tooltip");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  const num = (v, d=0) => (v==null||isNaN(v)) ? "–" : (+v).toFixed(d);
  const fmtMW = (mw) => mw >= 1000 ? (mw/1000).toFixed(1)+" GW"
                     : mw >= 1 ? mw.toFixed(1)+" MW" : Math.round(mw*1000)+" kW";

  const fmtDT = new Intl.DateTimeFormat("en-GB", { weekday:"short", day:"numeric",
    month:"short", hour:"2-digit", minute:"2-digit", timeZone:"Europe/Berlin" });

  /* ---------- color ramps ---------- */
  function hex2rgb(h){ return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)]; }
  function interpStops(st, v){
    if (v==null||isNaN(v)) return "#1a2233";
    if (v<=st[0][0]) return st[0][1];
    for (let i=1;i<st.length;i++){
      if (v<=st[i][0]){
        const [v0,c0]=st[i-1],[v1,c1]=st[i];
        const t=(v-v0)/(v1-v0), a=hex2rgb(c0), b=hex2rgb(c1);
        return `rgb(${a.map((x,j)=>Math.round(x+(b[j]-x)*t)).join(",")})`;
      }
    }
    return st[st.length-1][1];
  }
  // diverging price ramp (electricity-price-map style), domain set from data
  const STOP_LOW  = [[0,"#0b7a75"],[0.5,"#2aa881"],[1,"#4cc38a"]];
  const STOP_HIGH = [[0,"#8fce5e"],[0.35,"#f2d23c"],[0.65,"#f08a3c"],[0.85,"#e5484d"],[1,"#9b4dca"]];
  function ramp(stops,t){
    t=Math.max(0,Math.min(1,t));
    for(let i=1;i<stops.length;i++) if(t<=stops[i][0]){
      const [t0,c0]=stops[i-1],[t1,c1]=stops[i];
      return hexLerp(c0,c1,(t-t0)/((t1-t0)||1));
    }
    return stops[stops.length-1][1];
  }
  function hexLerp(c1,c2,t){
    const a=hex2rgb(c1),b=hex2rgb(c2);
    return `rgb(${a.map((x,j)=>Math.round(x+(b[j]-x)*t)).join(",")})`;
  }
  let pLo=-10, pHi=100;
  const priceColor = (v) => v==null ? "#232b35"
    : v<=0 ? ramp(STOP_LOW, 1-(pLo<0?Math.max(0,(v-pLo)/(0-pLo)):1))
           : ramp(STOP_HIGH, pHi>0?Math.min(1,v/pHi):0);

  const WSIG = {
    wcarbon:{ key:"carbon", label:"Carbon intensity", unit:"gCO₂eq/kWh",
      stops:[[0,"#2aa364"],[150,"#b6d84c"],[350,"#f5a623"],[550,"#e2543a"],[820,"#7d3a2d"]] },
    wrenew:{ key:"renewable", label:"Renewable share", unit:"%",
      stops:[[0,"#e2543a"],[25,"#f5a623"],[50,"#b6d84c"],[75,"#4dc47d"],[100,"#2aa364"]] },
    wprice:{ key:"price", label:"Electricity price", unit:"/MWh",
      stops:[[-20,"#1db97c"],[0,"#2aa364"],[60,"#b6d84c"],[110,"#f5a623"],[170,"#e2543a"],[260,"#7d3a2d"]] },
  };
  const MIX_COLORS = {
    nuclear:"#b86bd6",hydro:"#3f8fdf",wind:"#6fd3c9",solar:"#f5d74d",
    coal:"#5a5f6e",gas:"#e2764a",biomass:"#7cc24d",other:"#8b98ad",
    wind_onshore:"#6fd3c9",wind_offshore:"#4db0c9",lignite:"#7a6146",
    hard_coal:"#4a4f5e",oil:"#8a5a3a",waste:"#9a8f6a",pumped:"#5aa0e0",
    other_renew:"#a3c96e",
  };

  /* ---------- map & panes ---------- */
  const q = new URLSearchParams(location.search);
  const map = L.map("map",{ minZoom:2, maxZoom:11, worldCopyJump:true, zoomControl:false });
  L.control.zoom({position:"topright"}).addTo(map);
  map.setView([+q.get("lat")||50.4, +q.get("lon")||10.3], +q.get("z")||5);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom:12, attribution:"Tiles © Esri · prices: energy-charts/SMARD (CC BY 4.0) · units: MaStR",
  }).addTo(map);
  [["zones",340],["states",350],["flows",360],["units",380]].forEach(([n,z])=>{
    map.createPane(n); map.getPane(n).style.zIndex=z;
  });
  const unitRenderer = L.canvas({ pane:"units", padding:0.4 });

  /* ---------- state ---------- */
  const S = {
    mode:"price", tIdx:0, hour:new Date().getUTCHours(),
    playing:false, timer:null,
    price:null, priceGeo:null, world:null, worldGeo:null, deGeo:null,
    zoneLayer:null, statesLayer:null,
    mastr:{},          // slug -> {data, group:L.layerGroup}
    mastrOrder:[],     // checkbox order for "last enabled" tracking
    flowGroup:null, flows:[], flowAnim:false,
  };
  window.__map = map;  window.__S = S;   // debug/testing handles

  /* ---------- time control ---------- */
  function timelineLen(){ return S.mode==="price"&&S.price ? S.price.timeline.length
                        : S.mode[0]==="w" ? 24 : 0; }
  function curIdx(){ return S.mode==="price" ? S.tIdx : S.hour; }
  function setIdx(i){
    if (S.mode==="price") S.tIdx=i; else S.hour=i;
    updateTimeUI(); restyleZones();
  }
  function updateTimeUI(){
    const n=timelineLen(), s=$("slider");
    s.max=Math.max(0,n-1); s.value=Math.min(curIdx(),n-1||0);
    const dis = !n;
    s.disabled=dis; $("play").disabled=dis;
    $("timeSec").style.opacity = dis? .45 : 1;
    $("tlabel").textContent =
      S.mode==="price"&&S.price ? fmtDT.format(new Date(S.price.timeline[S.tIdx]*1000))+" (Berlin)"
      : S.mode[0]==="w" ? String(S.hour).padStart(2,"0")+":00 UTC"
      : "no time-varying layer";
  }
  $("slider").addEventListener("input", e=>setIdx(+e.target.value));
  $("play").addEventListener("click", ()=>{
    S.playing=!S.playing; $("play").textContent=S.playing?"❚❚":"▶";
    clearInterval(S.timer);
    if (S.playing){
      const step=()=>setIdx((curIdx()+1)%timelineLen());
      S.timer=setInterval(step, S.mode==="price"?140:800);
    }
  });

  /* ---------- zone choropleth ---------- */
  function zoneVal(mode, zid, f){
    if (mode==="price"){
      const z=S.price&&S.price.zones[zid];
      return z&&z.price? z.price[S.tIdx] : null;
    }
    if (mode[0]==="w"){
      const sig=WSIG[mode].key, z=S.world&&S.world.zones[zid];
      if(!z) return null;
      const s=z.series&&z.series[sig];
      return s? s[S.hour%s.length] : z[sig];
    }
    return null;
  }
  function styleZone(f){
    const v=zoneVal(S.mode, f.properties.zoneName||f.properties.zone, f);
    const col = S.mode==="price" ? priceColor(v)
              : S.mode[0]==="w" ? interpStops(WSIG[S.mode].stops, v)
              : "#1a2233";
    return { pane:"zones", fillColor:col, fillOpacity:v==null?0.5:0.8,
             color:v==null?"#232c40":"rgba(255,255,255,0.28)", weight:v==null?0.4:1 };
  }
  function restyleZones(){
    if (S.zoneLayer) S.zoneLayer.eachLayer(l=>l.setStyle(styleZone(l.feature)));
    renderLegend();
  }

  function zoneTipHtml(zid, f){
    if (S.mode==="price"){
      const z=S.price&&S.price.zones[zid];
      const v=z&&z.price? z.price[S.tIdx] : null;
      return `<div class="tt-name">${esc(z?z.label:zid)}</div>
        <div class="tt-val"><span>day-ahead</span><b>${v==null?"no data":v.toFixed(2)+" €/MWh"}</b></div>`;
    }
    if (S.mode[0]==="w"){
      const z=S.world&&S.world.zones[zid];
      if(!z) return `<div class="tt-name">${esc(f.properties.name||zid)}</div><div class="tt-val"><span></span><b>no data</b></div>`;
      const g=(k)=>{const s=z.series&&z.series[k];return s?s[S.hour%s.length]:z[k];};
      return `<div class="tt-name">${z.flag||""} ${esc(z.name)}</div>
        <div class="tt-val"><span>⚡ price</span><b>${z.currency||""}${num(g("price"))}/MWh</b></div>
        <div class="tt-val"><span>🌿 carbon</span><b>${num(g("carbon"))} g/kWh</b></div>
        <div class="tt-val"><span>♻️ renewable</span><b>${num(g("renewable"))} %</b></div>`;
    }
    return `<div class="tt-name">${esc(f.properties.name||f.properties.zoneName||zid)}</div>`;
  }
  function showTip(e, html){
    tip.innerHTML=html; tip.classList.remove("hidden");
    const ev=e.originalEvent||e;
    tip.style.left=Math.min(ev.clientX+2, innerWidth-300)+"px";
    tip.style.top =Math.min(ev.clientY+2, innerHeight-140)+"px";
  }

  function sparkline(vals, cur){
    const w=250,h=40, ok=vals.filter(v=>v!=null&&!isNaN(v));
    if(!ok.length) return "";
    const lo=Math.min(...ok),hi=Math.max(...ok);
    const y=v=>h-4-((v-lo)/((hi-lo)||1))*(h-8);
    const pts=vals.map((v,i)=>v==null?null:`${(i/(vals.length-1)*w).toFixed(1)},${y(v).toFixed(1)}`)
      .filter(Boolean).join(" ");
    const cx=cur/(vals.length-1)*w;
    const mk=vals[cur]!=null?`<circle cx="${cx.toFixed(1)}" cy="${y(vals[cur]).toFixed(1)}" r="3" fill="#fff"/>`:"";
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block;margin-top:6px">
      <polyline points="${pts}" fill="none" stroke="#4a90d9" stroke-width="1.5"/>${mk}</svg>`;
  }

  function zonePopup(zid, f, latlng){
    let html;
    if (S.mode==="price"){
      const z=S.price&&S.price.zones[zid]; if(!z||!z.price) return;
      const v=z.price[S.tIdx];
      const last=z.price.slice(Math.max(0,S.tIdx-95),S.tIdx+1).filter(x=>x!=null);
      const st=last.length?`24h min ${Math.min(...last).toFixed(0)} ·
        avg ${(last.reduce((a,b)=>a+b,0)/last.length).toFixed(0)} ·
        max ${Math.max(...last).toFixed(0)} €/MWh`:"";
      html=`<div class="pp-name">${esc(z.label)}${z.bzn?` <span style="color:var(--mut);font-weight:400">· ${esc(z.bzn)}</span>`:""}</div>
        <div class="pp-row"><span>day-ahead price</span><b style="color:${priceColor(v)}">${v==null?"–":v.toFixed(2)+" €/MWh"}</b></div>
        ${v==null?"":`<div class="pp-row"><span></span><b>${(v/10).toFixed(2)} ct/kWh</b></div>`}
        <div class="pp-row"><span></span><b style="font-weight:400">${st}</b></div>
        ${sparkline(z.price,S.tIdx)}`;
    } else {
      const z=S.world&&S.world.zones[zid]; if(!z) return;
      const g=(k)=>{const s=z.series&&z.series[k];return s?s[S.hour%s.length]:z[k];};
      const mix=z.mixMW?Object.entries(z.mixMW):Object.entries(z.mix||{}).map(([k,v])=>[k,(z.demandGW||0)*10*v]);
      const mixRows=mix.filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1]).slice(0,5)
        .map(([k,v])=>`<div class="pp-row"><span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${MIX_COLORS[k]||"#888"};margin-right:4px"></span>${k}</span><b>${Math.round(v)} MW</b></div>`).join("");
      html=`<div class="pp-name">${z.flag||""} ${esc(z.name)}</div>
        <div class="pp-row"><span>⚡ price</span><b>${z.currency||""}${num(g("price"))}/MWh</b></div>
        <div class="pp-row"><span>🌿 carbon</span><b>${num(g("carbon"))} gCO₂eq/kWh</b></div>
        <div class="pp-row"><span>♻️ renewable</span><b>${num(g("renewable"))} %</b></div>
        <div class="pp-row"><span>📊 demand</span><b>${num(z.demandGW,1)} GW</b></div>
        ${sparkline((z.series&&z.series[WSIG[S.mode]?WSIG[S.mode].key:"price"])||[],S.hour)}
        ${mixRows}`;
    }
    L.popup({maxWidth:300}).setLatLng(latlng).setContent(html).openOn(map);
  }

  function bindZoneEvents(f, layer){
    const zid=f.properties.zoneName||f.properties.zone;
    layer.on({
      mouseover:e=>{layer.setStyle({weight:2,color:"rgba(255,255,255,0.85)"});layer.bringToFront();showTip(e,zoneTipHtml(zid,f));},
      mousemove:e=>showTip(e,zoneTipHtml(zid,f)),
      mouseout:()=>{S.zoneLayer&&S.zoneLayer.resetStyle(layer);tip.classList.add("hidden");},
      click:e=>zonePopup(zid,f,e.latlng),
    });
  }

  function rebuildZoneLayer(){
    if (S.zoneLayer){ map.removeLayer(S.zoneLayer); S.zoneLayer=null; }
    let geo=null;
    if (S.mode==="price") geo=S.priceGeo;
    else if (S.mode[0]==="w") geo=S.worldGeo;
    if (geo){
      S.zoneLayer=L.geoJSON(geo,{style:styleZone,onEachFeature:bindZoneEvents,pane:"zones"}).addTo(map);
    }
    updateTimeUI(); renderLegend();
  }

  /* ---------- data loading (lazy) ---------- */
  const fetchJ=(u)=>fetch(u).then(r=>{if(!r.ok)throw new Error(u+" → "+r.status);return r.json();});
  function ensurePrice(){
    if (S.priceGeo) return Promise.resolve();
    if (!S._p) S._p=Promise.all([fetchJ("/price/data/data.json"),fetchJ("/price/geo/zones.geojson")])
      .then(([d,g])=>{
        S.price=d; S.priceGeo=g;
        const all=[]; // robust price domain: 5th/95th pct of all prices
        for(const z of Object.values(d.zones)) if(z.price)
          for(const x of z.price) if(x!=null) all.push(x);
        all.sort((a,b)=>a-b);
        if(all.length){ pLo=Math.min(0,all[Math.floor(all.length*0.05)]);
                        pHi=Math.max(50,all[Math.floor(all.length*0.95)]); }
        // start playback at "now" (or newest slot)
        const now=Date.now()/1000; let best=0,bd=1e18;
        d.timeline.forEach((t,i)=>{const dd=Math.abs(t-now);if(dd<bd){bd=dd;best=i;}});
        S.tIdx=best;
      }).catch(e=>{S._p=null;throw e;});
    return S._p;
  }
  function ensureWorld(){
    if (S.worldGeo) return Promise.resolve();
    if (!S._w) S._w=Promise.all([fetchJ("/grid/data/data.json"),fetchJ("/grid/data/zones.geojson")])
      .then(([d,g])=>{S.world=d;S.worldGeo=g;})
      .catch(e=>{S._w=null;throw e;});
    return S._w;
  }
  function ensureDeGeo(){
    if (S.deGeo) return Promise.resolve();
    if (!S._d) S._d=fetchJ("/price/geo/de_states.geojson").then(g=>{S.deGeo=g;})
      .catch(e=>{S._d=null;throw e;});
    return S._d;
  }

  /* ---------- mode switching ---------- */
  document.querySelectorAll('input[name="mode"]').forEach(r=>r.addEventListener("change",()=>{
    if(!r.checked) return;
    S.mode=r.value;
    const need = S.mode==="price"?ensurePrice : S.mode[0]==="w"?ensureWorld : ()=>Promise.resolve();
    need().then(rebuildZoneLayer)
      .catch(e=>{$("err").textContent="Layer failed to load: "+e.message;$("err").classList.remove("hidden");});
    if (S.playing){   // restart timer at the new timeline's speed (stop if static)
      clearInterval(S.timer);
      if (timelineLen())
        S.timer=setInterval(()=>setIdx((curIdx()+1)%timelineLen()), S.mode==="price"?140:800);
      else { S.playing=false; $("play").textContent="▶"; }
    }
  }));

  /* ---------- exchange flows (world dataset) ---------- */
  function bearing(a,b){
    const pa=map.project(a,3),pb=map.project(b,3);
    return Math.atan2(pb.x-pa.x,-(pb.y-pa.y))*180/Math.PI;
  }
  function clearFlows(){
    S.flows.forEach(f=>f.markers.forEach(m=>map.removeLayer(m)));
    if(S.flowGroup){map.removeLayer(S.flowGroup);S.flowGroup=null;}
    S.flows=[];
  }
  function buildFlows(){
    clearFlows();
    if(!$("lyrFlows").checked||!S.world) return;
    S.flowGroup=L.layerGroup().addTo(map);
    S.world.exchanges.forEach(e=>{
      const A=S.world.zones[e.f],B=S.world.zones[e.t];
      if(!A||!B||!A.center||!B.center)return;
      let a=L.latLng(A.center[1],A.center[0]),b=L.latLng(B.center[1],B.center[0]);
      let mw=e.mw;
      if(mw<0){[a,b]=[b,a];mw=-mw;}
      const dist=a.distanceTo(b);
      const t0=Math.min(0.25,60000/dist||0),t1=1-t0;
      L.polyline([a,b],{pane:"flows",className:"flow-line",weight:1.2,interactive:false}).addTo(S.flowGroup);
      const ang=bearing(a,b),n=mw>1500?3:mw>500?2:1,markers=[];
      for(let i=0;i<n;i++){
        const ic=L.divIcon({className:"flow-particle",iconSize:[10,10],
          html:`<div style="transform:rotate(${ang.toFixed(1)}deg)"></div>`});
        markers.push(L.marker(a,{icon:ic,interactive:false,keyboard:false,pane:"flows"}).addTo(S.flowGroup));
      }
      S.flows.push({a,b,t0,t1,markers,speed:0.10+Math.min(mw,4000)/4000*0.22,phase:0});
    });
  }
  $("lyrFlows").addEventListener("change",()=>{ensureWorld().then(buildFlows);});
  let lastT=0;
  (function tick(ts){
    const dt=Math.min(0.05,(ts-lastT)/1000||0);lastT=ts;
    S.flows.forEach(f=>{
      f.phase=(f.phase+dt*f.speed)%1;
      f.markers.forEach((m,i)=>{
        const t=f.t0+((f.phase+i/f.markers.length)%1)*(f.t1-f.t0);
        m.setLatLng(L.latLng(f.a.lat+(f.b.lat-f.a.lat)*t,f.a.lng+(f.b.lng-f.a.lng)*t));
      });
    });
    requestAnimationFrame(tick);
  })();

  /* ---------- MaStR unit layers ---------- */
  const MASTR_COLOR={
    battery:"#4dd0a6",storage:"#7a9cc6",solar:"#f5d74d","solar-park":"#e8a93c",
    onshore:"#6fd3c9",offshore:"#4db0c9",gas:"#e05d4f",coal:"#8d8577",
    biomass:"#7cc24d",hydro:"#4a80b4",nuclear:"#b86bd6",
  };
  const mRadius=(mw)=>Math.max(2.2,Math.min(13,2+Math.sqrt(Math.max(0.001,mw))*0.9));

  function fetchMastr(slug,markers){
    const key=slug+":"+markers;
    if(S.mastr[key])return Promise.resolve(S.mastr[key].data);
    return fetchJ(`/api/mastr/map?type=${slug}&markers=${markers}`).then(d=>{
      S.mastr[key]={data:d};return d;
    });
  }
  function unitPopup(u,label){
    return `<div class="pp-name">${esc(u.name)||label+" unit"}</div>
      <div class="pp-row"><span>capacity</span><b>${fmtMW(u.mw||0)}</b></div>
      ${u.subtype?`<div class="pp-row"><span>type</span><b>${esc(u.subtype)}</b></div>`:""}
      ${u.ort||u.plz?`<div class="pp-row"><span>location</span><b>${esc([u.plz,u.ort].filter(Boolean).join(" "))}</b></div>`:""}
      <div class="pp-row"><span>MaStR</span><b>${esc(u.mastr_nr)}</b></div>`;
  }

  /* The units canvas is pointer-events:none so zone clicks pass through;
     hit-test markers manually (radius in px, same units as layerPoint). */
  map.on("click", e=>{
    const p=map.latLngToLayerPoint(e.latlng);
    let best=null,bd=1e9;
    for(const k in S.mastr){
      const g=S.mastr[k]&&S.mastr[k].group;
      if(!g||!map.hasLayer(g))continue;
      g.eachLayer(m=>{
        const mp=map.latLngToLayerPoint(m.getLatLng());
        const d=Math.hypot(mp.x-p.x,mp.y-p.y);
        if(d<=m.getRadius()+4&&d<bd){bd=d;best=m;}
      });
    }
    if(best)best.openPopup(e.latlng);
  });
  function buildUnitLayer(slug,label,markers){
    const col=MASTR_COLOR[slug]||"#9fb4d8";
    return L.layerGroup(markers.map(u=>L.circleMarker([u.lat,u.lon],{
      renderer:unitRenderer,pane:"units",radius:mRadius(u.mw),
      color:"#0b0e14",weight:0.5,fillColor:col,fillOpacity:0.85,
    }).bindPopup(unitPopup(u,label))));
  }
  function mastrToggle(slug,label,cb){
    const key=slug+":4000", rec=S.mastr[key];
    if(!cb.checked){
      if(rec&&rec.group){map.removeLayer(rec.group);}
      S.mastrOrder=S.mastrOrder.filter(s=>s!==slug);return;
    }
    if(rec&&rec.group){rec.group.addTo(map);}
    else{
      cb.closest("label").classList.add("disabled");
      fetchMastr(slug,4000).then(d=>{
        const r=S.mastr[key];
        r.group=buildUnitLayer(slug,label,d.markers);
        if(cb.checked)r.group.addTo(map);
        const st=cb.closest("label").querySelector(".mstat");
        if(st)st.textContent=`top ${d.markers.length.toLocaleString("en")}`+
          (d.with_coords>d.markers.length?` of ${d.with_coords.toLocaleString("en")}`:"")+
          ` · ${fmtMW(d.total_mw||0)}`;
      }).catch(()=>{cb.checked=false;})
        .finally(()=>cb.closest("label").classList.remove("disabled"));
    }
    if(!S.mastrOrder.includes(slug))S.mastrOrder.push(slug);
    syncAggType();
  }

  function buildMastrUI(layerList){
    const box=$("mastrBox");box.innerHTML="";
    const sel=$("aggType");sel.innerHTML="";
    // batteries on by default; the rest are opt-in (solar has millions of units)
    const DEFAULT_ON=new Set(["battery"]);
    layerList.forEach(({slug,label})=>{
      const opt=document.createElement("option");opt.value=slug;opt.textContent=label;sel.appendChild(opt);
      const lb=document.createElement("label");
      lb.innerHTML=`<input type="checkbox" data-slug="${slug}"> ${esc(label)} <span class="mstat"></span>`;
      box.appendChild(lb);
      lb.querySelector("input").addEventListener("change",e=>mastrToggle(slug,label,e.target));
      if(DEFAULT_ON.has(slug)){const cb=lb.querySelector("input");cb.checked=true;mastrToggle(slug,label,cb);}
    });
  }

  /* ---------- MaStR state totals (DE choropleth) ---------- */
  const AGG_STOPS=[[0,"#22303c"],[0.35,"#2a5e8a"],[0.7,"#f08a3c"],[1,"#e5484d"]];
  function aggType(){return $("aggType").value||S.mastrOrder[S.mastrOrder.length-1]||"battery";}
  function styleState(f,d){
    const st=(d.states||[]).find(s=>s.name===f.properties.name);
    const max=Math.max(1,...d.states.map(s=>s.mw||0));
    const t=st?Math.sqrt(st.mw/max):0;
    return {pane:"states",fillColor:ramp(AGG_STOPS,t),fillOpacity:st?0.75:0.35,
            color:"rgba(255,255,255,0.35)",weight:1};
  }
  function rebuildStates(){
    if(S.statesLayer){map.removeLayer(S.statesLayer);S.statesLayer=null;}
    if(!$("lyrStates").checked)return;
    const slug=aggType();
    Promise.all([ensureDeGeo(),fetchMastr(slug,0)]).then(([,d])=>{
      if(!$("lyrStates").checked)return;
      S.statesLayer=L.geoJSON(S.deGeo,{pane:"states",
        style:f=>styleState(f,d),
        onEachFeature:(f,l)=>{
          const st=(d.states||[]).find(s=>s.name===f.properties.name);
          l.on({
            mouseover:e=>{l.setStyle({weight:2,color:"#fff"});showTip(e,
              `<div class="tt-name">${esc(f.properties.name)}</div>
               <div class="tt-val"><span>${esc(d.label||slug)}</span><b>${st?fmtMW(st.mw):"no data"}</b></div>
               <div class="tt-val"><span>units</span><b>${st?st.units.toLocaleString("en"):"–"}</b></div>`);},
            mousemove:e=>showTip(e,tip.innerHTML),
            mouseout:()=>{S.statesLayer.resetStyle(l);tip.classList.add("hidden");},
          });
        }}).addTo(map);
    });
  }
  $("lyrStates").addEventListener("change",rebuildStates);
  $("aggType").addEventListener("change",rebuildStates);
  function syncAggType(){
    const last=S.mastrOrder[S.mastrOrder.length-1];
    if(last)$("aggType").value=last;
    if($("lyrStates").checked)rebuildStates();
  }

  /* ---------- legend ---------- */
  function renderLegend(){
    const lg=$("legend");
    if(S.mode==="none"){lg.classList.add("hidden");return;}
    lg.classList.remove("hidden");
    let grad,ticks,title;
    if(S.mode==="price"){
      title=`Day-ahead price (€/MWh)`;
      const st=[];for(let i=0;i<=20;i++){st.push(`${priceColor(pLo+(pHi-pLo)*i/20)} ${i*5}%`);}
      grad=`linear-gradient(90deg,${st.join(",")})`;
      ticks=[Math.round(pLo),Math.round((pLo+pHi)/2),Math.round(pHi)+"+"];
    }else{
      const s=WSIG[S.mode];if(!s){lg.classList.add("hidden");return;}
      title=`${s.label} (${s.unit})`;
      grad=`linear-gradient(90deg,${s.stops.map(x=>`${x[1]} ${(x[0]-s.stops[0][0])/(s.stops[s.stops.length-1][0]-s.stops[0][0])*100}%`).join(",")})`;
      ticks=[s.stops[0][0],s.stops[Math.floor(s.stops.length/2)][0],s.stops[s.stops.length-1][0]+"+"];
    }
    $("legendTitle").textContent=title;
    $("legendBar").style.background=grad;
    $("legendTicks").innerHTML=ticks.map(t=>`<span>${t}</span>`).join("");
  }

  /* ---------- coordinate readout ---------- */
  const coords=$("coords");
  map.on("mousemove",e=>{
    coords.innerHTML=`${e.latlng.lat.toFixed(4)}, ${e.latlng.lng.toFixed(4)} <span class="z">· z${map.getZoom()}</span>`;
  });
  map.on("mouseout",()=>{coords.textContent="–, –";});
  map.on("zoomend",()=>{
    coords.innerHTML=coords.innerHTML.replace(/· z\d+/,`· z${map.getZoom()}`);
  });

  /* ---------- boot ---------- */
  fetchMastr("battery",4000)
    .then(d=>buildMastrUI(d.layers||[{slug:"battery",label:"Battery"}]))
    .catch(()=>{$("mastrBox").innerHTML='<div class="dim">MaStR API unavailable</div>';});

  ensurePrice().then(()=>{
    rebuildZoneLayer();
    const ageMin=Math.round((Date.now()/1000-(S.price.generated_at||0))/60);
    $("clock").textContent=`⚡ day-ahead prices · updated ${ageMin<1?"just now":ageMin+" min ago"} · `+
      `${Object.keys(S.price.zones).length} bidding zones · click a zone for details`;
  }).catch(e=>{
    $("err").textContent="Failed to load price data: "+e.message;
    $("err").classList.remove("hidden");
  });
})();
