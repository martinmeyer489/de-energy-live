"""CLI for live German electricity supply & demand data."""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .model import TimeSeries
from .sources import energy_charts, mastr, smard

BERLIN = ZoneInfo("Europe/Berlin")
BAR_WIDTH = 30


def _fmt_mw(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _bar(value: float, vmax: float) -> str:
    if vmax <= 0:
        return ""
    return "█" * round(BAR_WIDTH * value / vmax)


def _today() -> date:
    return datetime.now(BERLIN).date()


def _latest(mix: dict[str, TimeSeries], *names: str,
            before: datetime | None = None) -> float | None:
    for name in names:
        ts = mix.get(name)
        if ts:
            hit = ts.latest(before)
            if hit:
                return hit[1]
    return None


def _generation_snapshot(mix: dict[str, TimeSeries]) -> tuple[datetime | None, list[tuple[str, float]]]:
    """Latest timestamp with generation data + per-source values at/before it."""
    hits = []
    for name, ts in mix.items():
        if name in energy_charts.GENERATION_TYPES:
            hit = ts.latest()
            if hit:
                hits.append((name, hit))
    if not hits:
        return None, []
    as_of = max(t for _, (t, _) in hits)
    rows = sorted(
        ((name, ts.latest(as_of)[1]) for name, ts in mix.items()
         if name in energy_charts.GENERATION_TYPES and ts.latest(as_of)),
        key=lambda x: x[1], reverse=True,
    )
    return as_of, rows


def _print_snapshot(mix: dict[str, TimeSeries], source: str) -> None:
    as_of, gen = _generation_snapshot(mix)
    stamp = as_of.astimezone(BERLIN) if as_of else datetime.now(BERLIN)
    print(f"\nGerman power grid — {stamp:%a %d %b %Y %H:%M} (Europe/Berlin)")
    print(f"Source: {source}\n")

    load = _latest(mix, "Load (incl. self-consumption)", "Load", before=as_of)
    res = _latest(mix, "Residual load", before=as_of)
    ren_share = _latest(mix, "Renewable share of load", before=as_of)
    trade = _latest(mix, "Cross border electricity trading", before=as_of)

    print("DEMAND")
    print(f"  Load ........................ {_fmt_mw(load):>8} MW")
    print(f"  Residual load ............... {_fmt_mw(res):>8} MW")
    if ren_share is not None:
        print(f"  Renewable share of load ..... {ren_share:7.1f} %")

    print("\nSUPPLY (generation)")
    vmax = gen[0][1] if gen else 1.0
    for name, v in gen:
        tag = " R" if name in energy_charts.RENEWABLE_TYPES else "  "
        print(f"{tag} {name:<31} {_fmt_mw(v):>8}  {_bar(v, vmax)}")
    total = sum(v for _, v in gen)
    print(f"\n  Total generation ............ {total:>8,.0f} MW")
    if trade is not None:
        label = "net export" if trade >= 0 else "net import"
        print(f"  Cross-border trading ........ {abs(trade):>8,.0f} MW ({label})")


def _append_csv(path: str, mix: dict[str, TimeSeries]) -> None:
    as_of, gen = _generation_snapshot(mix)
    if as_of is None:
        return
    load = _latest(mix, "Load (incl. self-consumption)", "Load", before=as_of)
    ren = _latest(mix, "Renewable share of load", before=as_of)
    row = {
        "timestamp_utc": as_of.isoformat(),
        "load_mw": load,
        "total_generation_mw": sum(v for _, v in gen),
        "renewable_share_pct": ren,
        **{f"gen_{k.lower().replace(' ', '_').replace('/', '_')}_mw": v for k, v in gen},
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if write_header:
            w.writeheader()
        w.writerow(row)
    print(f"Appended snapshot to {path}")


def cmd_now(args: argparse.Namespace) -> None:
    mix = energy_charts.fetch_power_mix(_today(), _today())
    _print_snapshot(mix, "Fraunhofer ISE, energy-charts.info")

    try:
        hit = energy_charts.fetch_price(_today(), _today()).latest()
        if hit:
            pt, pv = hit
            print(f"\nPRICE")
            print(f"  Day-ahead DE-LU @ {pt.astimezone(BERLIN):%H:%M} ...... {pv:7.2f} EUR/MWh")
    except Exception:
        pass

    try:
        shares = energy_charts.fetch_signal().get("share", [])
        if shares:
            print(f"  Forecast peak renewables today ... {max(shares):.0f} % of load")
    except Exception:
        pass
    print()

    if args.csv:
        _append_csv(args.csv, mix)


def cmd_today(_args: argparse.Namespace) -> None:
    mix = energy_charts.fetch_power_mix(_today(), _today())
    totals = {}
    for name, ts in mix.items():
        if name not in energy_charts.GENERATION_TYPES:
            continue
        gwh = sum(v for v in ts.values if v is not None) * 0.25 / 1000.0
        if gwh:
            totals[name] = gwh

    ren = sum(v for k, v in totals.items() if k in energy_charts.RENEWABLE_TYPES)
    conv = sum(v for k, v in totals.items() if k not in energy_charts.RENEWABLE_TYPES)

    print(f"\nGeneration totals — {datetime.now(BERLIN):%d %b %Y} (so far)")
    print(f"{'Source':<34} {'GWh':>8}")
    print("-" * 44)
    for name, gwh in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        tag = "R" if name in energy_charts.RENEWABLE_TYPES else " "
        print(f"[{tag}] {name:<31} {gwh:>8.1f}")
    print("-" * 44)
    print(f"  {'Renewables':<32} {ren:>8.1f}")
    print(f"  {'Conventional':<32} {conv:>8.1f}")
    print(f"  {'Total':<32} {ren + conv:>8.1f}")
    print()


def cmd_smard(_args: argparse.Namespace) -> None:
    print("\nOfficial Bundesnetzagentur data (smard.de)\n")
    try:
        load = smard.fetch_net_load("quarterhour")
        hit = load.latest()
        if hit:
            t, v = hit
            print("DEMAND")
            print(f"  Netzlast @ {t.astimezone(BERLIN):%H:%M} ......... {_fmt_mw(v):>8} MW")
    except Exception as e:
        print(f"  Netzlast unavailable ({e})")

    print("\nSUPPLY (realised generation, hourly)")
    gen = smard.fetch_generation("hour")
    hits = []
    for name, ts in gen.items():
        hit = ts.latest()
        if hit:
            hits.append((name, hit))
    if hits:
        as_of = max(t for _, (t, _) in hits)
        rows = sorted(
            ((n, ts.latest(as_of)[1]) for n, ts in gen.items() if ts.latest(as_of)),
            key=lambda x: x[1], reverse=True,
        )
        vmax = rows[0][1] if rows else 1.0
        for name, v in rows:
            print(f"    {name:<44} {_fmt_mw(v):>8}  {_bar(v, vmax)}")
        print(f"\n  as of {as_of.astimezone(BERLIN):%H:%M} Europe/Berlin")
    print()


def build_chart_figure(mix: dict[str, TimeSeries]):
    """Stacked generation vs load figure for the given power mix."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    as_of, _ = _generation_snapshot(mix)
    end = as_of or datetime.now(BERLIN).astimezone()

    gen = {}
    for name, ts in mix.items():
        if name not in energy_charts.GENERATION_TYPES:
            continue
        w = ts.window(ts.timestamps[0], end)
        gen[name] = w
    gen = dict(sorted(gen.items(), key=lambda x: -(max(
        (v for v in x[1].values if v is not None), default=0))))

    times = [t.astimezone(BERLIN) for t in next(iter(gen.values())).timestamps]
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.stackplot(
        times,
        *[[v or 0 for v in ts.values] for ts in gen.values()],
        labels=gen.keys(),
        alpha=0.85,
    )
    for lname in ("Load (incl. self-consumption)", "Load"):
        load_ts = mix.get(lname)
        if load_ts:
            w = load_ts.window(load_ts.timestamps[0], end)
            ax.plot([t.astimezone(BERLIN) for t in w.timestamps],
                    [v or 0 for v in w.values], "k--", lw=1.5, label="Load")
            break
    ax.set_title(f"German electricity supply vs demand — {datetime.now(BERLIN):%d %b %Y}")
    ax.set_ylabel("MW")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=BERLIN))
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def cmd_regions(_args: argparse.Namespace) -> None:
    from . import regional

    print("\nGermany by control area (smard.de / Bundesnetzagentur)\n")
    data = regional.all_zones()
    for name, z in data["zones"].items():
        print(f"  {name} ({z['tso']})")
        print(f"    Net load ........... {_fmt_mw(z['load_mw']):>8} MW")
        print(f"    Generation ......... {_fmt_mw(z['total_generation_mw']):>8} MW")
        if z["renewable_share_pct"] is not None:
            print(f"    Renewable share .... {z['renewable_share_pct']:>7.1f} %")
        if z["co2_intensity_gkwh"] is not None:
            print(f"    CO2 intensity ...... {z['co2_intensity_gkwh']:>7.0f} g/kWh")
        if z["self_sufficiency_pct"] is not None:
            print(f"    Self-sufficiency ... {z['self_sufficiency_pct']:>7.1f} %")
        if z["as_of"]:
            t = datetime.fromisoformat(z["as_of"]).astimezone(BERLIN)
            print(f"    as of {t:%H:%M} Europe/Berlin")
        print()
    for name, err in data["errors"].items():
        print(f"  {name}: unavailable ({err})")
    print()


def cmd_chart(args: argparse.Namespace) -> None:
    mix = energy_charts.fetch_power_mix(_today(), _today())
    fig = build_chart_figure(mix)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fig.savefig(args.out, dpi=120)
    print(f"Saved {args.out}")


def cmd_web(args: argparse.Namespace) -> None:
    from . import web

    web.serve(args.host, args.port)


# --- MaStR register data -----------------------------------------------------

# display groups for `mastr top`: (label, slug, SQL predicate on units)
MASTR_TOP_GROUPS = [
    ("Hard coal (Steinkohle)", "coal",
     "category='verbrennung' AND subtype='Steinkohle'"),
    ("Lignite (Braunkohle)", "lignite",
     "category='verbrennung' AND subtype='Braunkohle'"),
    ("Natural gas", "gas",
     "category='verbrennung' AND subtype IN ('Erdgas','andere Gase')"),
    ("Other thermal (oil/waste/heat/H2)", "thermal",
     "category='verbrennung' AND subtype NOT IN ('Steinkohle','Braunkohle','Erdgas','andere Gase')"),
    ("Biomass", "biomass", "category='biomasse'"),
    ("Hydro", "hydro", "category='wasser'"),
    ("Pumped storage", "pumped",
     "category='speicher' AND subtype='Pumpspeicher'"),
    ("Wind offshore", "offshore",
     "category='wind' AND subtype='Windenergie auf See'"),
    ("Wind onshore", "onshore",
     "category='wind' AND subtype='Windenergie an Land'"),
    ("Solar parks (Freifläche)", "solar",
     "category='solar' AND subtype='Freiflächensolaranlage'"),
    ("Battery storage", "battery",
     "category='speicher' AND subtype!='Pumpspeicher'"),
    ("Nuclear (all decommissioned)", "nuclear", "category='kernkraft'"),
    ("Gas producer / geothermal / other", "othergen",
     "category IN ('gaserzeuger','geo')"),
]

# extra groups aggregated at park level (wind turbines roll up to parks)
MASTR_PARK_GROUPS = [
    ("Largest wind parks (offshore, Σ turbines)", "offshore-parks",
     "category='wind' AND subtype='Windenergie auf See' AND plant!=''"),
    ("Largest wind parks (onshore, Σ turbines)", "onshore-parks",
     "category='wind' AND subtype='Windenergie an Land' AND plant!=''"),
]


def _mastr_db(args):
    try:
        return mastr.open_db(getattr(args, "data_dir", mastr.DATA_DIR))
    except RuntimeError as e:
        print(f"\n{e}\n")
        raise SystemExit(1)


def _mastr_rows(conn, where: str, n: int, include_all: bool):
    status = "" if include_all else "AND status='In Betrieb'"
    return conn.execute(
        f"SELECT * FROM units WHERE {where} {status} "
        "ORDER BY netto_mw DESC LIMIT ?", (n,),
    ).fetchall()


def _print_unit_rows(conn, rows, show_subtype: bool = True) -> None:
    ops = mastr.operator_names(conn, (r["operator_abr"] for r in rows))
    for r in rows:
        op = ops.get(r["operator_abr"]) or r["operator_abr"] or "—"
        plant = r["plant"] if r["plant"] and r["plant"] != r["name"] else ""
        label = r["name"] or plant or r["mastr_nr"]
        if plant:
            label = f"{plant} · {label}" if r["name"] else plant
        sub = f"  [{r['subtype']}]" if show_subtype and r["subtype"] else ""
        mw = f"{r['netto_mw']:,.0f} MW" if r["netto_mw"] is not None else "      —"
        # big plants often lack postal addresses in MaStR (cadastral only) —
        # fall back to Landkreis so the location column isn't blank
        loc = f"{r['plz']} {r['ort']}".strip() or r["landkreis"]
        yr = (r["inbetrieb"] or "")[:4]
        status = "" if r["status"] == "In Betrieb" else f" ({r['status']})"
        print(f"    {mw:>10}  {label[:38]:<38} {op[:30]:<30} {loc[:22]:<22} {yr}{sub}{status}")


def cmd_mastr_update(args: argparse.Namespace) -> None:
    groups = args.groups.split(",") if args.groups else None
    mastr.update(
        data_dir=args.data_dir,
        groups=groups,
        skip_operators=args.skip_operators,
        force=args.force,
        jobs=args.jobs,
    )


def _print_park_rows(conn, rows) -> None:
    for r in rows:
        mw = f"{r['s']:,.0f} MW"
        loc = (f"{r['plz'] or ''} {r['ort'] or ''}".strip()
               or (r["lk"] or "") or (r["bl"] or ""))
        print(f"    {mw:>10}  {r['plant'][:38]:<38} {r['n']:>3} turbines"
              f"  {loc[:24]}")


def _mastr_park_rows(conn, where: str, n: int, include_all: bool):
    status = "" if include_all else "AND status='In Betrieb'"
    return conn.execute(
        f"SELECT plant, COUNT(*) n, SUM(netto_mw) s, MAX(plz) plz,"
        f" MAX(ort) ort, MAX(landkreis) lk, MAX(bundesland) bl FROM units"
        f" WHERE {where} {status} GROUP BY plant ORDER BY s DESC LIMIT ?",
        (n,),
    ).fetchall()


def cmd_mastr_top(args: argparse.Namespace) -> None:
    conn = _mastr_db(args)
    groups = [(l, s, w, False) for l, s, w in MASTR_TOP_GROUPS]
    groups += [(l, s, w, True) for l, s, w in MASTR_PARK_GROUPS]
    if args.type:
        groups = [g for g in groups if g[1] == args.type]
        if not groups:
            slugs = ", ".join(g[1] for g in groups)
            raise SystemExit(f"unknown type {args.type!r} — one of: {slugs}")
    print("\nLargest registered units — MaStR Gesamtdatenexport")
    print("(Nettonennleistung; default filter: Betriebsstatus = In Betrieb)\n")
    for label, _slug, where, is_park in groups:
        if is_park:
            rows = _mastr_park_rows(conn, where, args.n, args.all)
            if rows:
                print(f"  {label}")
                _print_park_rows(conn, rows)
                print()
            continue
        rows = _mastr_rows(conn, where, args.n, args.all)
        if not rows:
            continue
        print(f"  {label}")
        _print_unit_rows(conn, rows, show_subtype=(args.type is not None))
        print()
    conn.close()


def cmd_mastr_batteries(args: argparse.Namespace) -> None:
    conn = _mastr_db(args)
    where = "category='speicher'" if args.all_types else (
        "category='speicher' AND subtype='Batterie'")
    rows = _mastr_rows(conn, where, args.n, args.all)
    print("\nLargest electricity storage units — MaStR"
          + (" (all types)" if args.all_types else " (batteries)") + "\n")
    _print_unit_rows(conn, rows)
    total = conn.execute(
        "SELECT COUNT(*) c, SUM(netto_mw) s FROM units WHERE " + where
    ).fetchone()
    print(f"\n  {total['c']:,} storage units registered, "
          f"{(total['s'] or 0):,.0f} MW total"
          + ("" if args.all_types else
             "  — pumped storage via `mastr top --type pumped`") + "\n")
    conn.close()


def cmd_mastr_plz(args: argparse.Namespace) -> None:
    conn = _mastr_db(args)
    if args.top:
        _mastr_plz_top(conn, args)
        return
    if not args.plz:
        raise SystemExit("usage: mastr plz <PLZ>  (or: mastr plz --top)")
    plz = args.plz
    rows = conn.execute(
        "SELECT * FROM units WHERE plz=? ORDER BY netto_mw DESC LIMIT ?",
        (plz, args.n),
    ).fetchall()
    agg = conn.execute(
        "SELECT COUNT(*) c, SUM(netto_mw) s FROM units WHERE plz=?", (plz,)
    ).fetchone()
    cons = conn.execute(
        "SELECT * FROM consumers WHERE plz=? ORDER BY name LIMIT 50", (plz,)
    ).fetchall()
    print(f"\nMaStR units at PLZ {plz}")
    print(f"  {agg['c']:,} generation/storage units, "
          f"{(agg['s'] or 0):,.0f} MW net registered capacity")
    by_cat = conn.execute(
        "SELECT category, COUNT(*) c, SUM(netto_mw) s FROM units "
        "WHERE plz=? GROUP BY category ORDER BY s DESC", (plz,),
    ).fetchall()
    for b in by_cat:
        print(f"    {b['category']:<15} {b['c']:>5} units  {(b['s'] or 0):>9,.1f} MW")
    if rows:
        print(f"\n  largest units (top {min(len(rows), args.n)}):")
        _print_unit_rows(conn, rows)
    if cons:
        print(f"\n  registered consumers ({len(cons)}):")
        ops = mastr.operator_names(conn, (c["operator_abr"] for c in cons))
        for c in cons:
            op = ops.get(c["operator_abr"]) or ""
            print(f"    {c['kind'][:5]:<5} {c['name'][:45]:<45} {op[:30]}")
    print("\n  note: MaStR registers consumer *units* but no demand MW — "
          "capacity shown is generation/storage, not consumption.")
    print()
    conn.close()


def _mastr_plz_top(conn, args) -> None:
    rows = conn.execute(
        "SELECT plz, COUNT(*) c, SUM(netto_mw) s FROM units "
        "WHERE plz != '' GROUP BY plz ORDER BY s DESC LIMIT ?", (args.n,),
    ).fetchall()
    # largest unit's Ort per displayed PLZ (only n lookups, not per group)
    top_ort = {
        r["plz"]: conn.execute(
            "SELECT ort FROM units WHERE plz=? ORDER BY netto_mw DESC LIMIT 1",
            (r["plz"],)).fetchone()["ort"]
        for r in rows
    }
    cons = {
        r["plz"]: r["c"]
        for r in conn.execute(
            "SELECT plz, COUNT(*) c FROM consumers GROUP BY plz")
    }
    print("\nTop PLZ areas by registered generation+storage capacity — MaStR\n")
    print(f"    {'PLZ':<7} {'Ort (largest unit)':<24} {'units':>7} {'MW':>10}  consumers")
    for r in rows:
        print(f"    {r['plz']:<7} {(top_ort.get(r['plz']) or '')[:24]:<24} "
              f"{r['c']:>7,} {(r['s'] or 0):>10,.0f}  {cons.get(r['plz'], 0)}")
    print("\n  'consumers' = registered Strom/Gas consumption units "
          "(MaStR carries no demand MW).")
    print()
    conn.close()


def cmd_mastr_summary(args: argparse.Namespace) -> None:
    conn = _mastr_db(args)
    meta = mastr._load_meta(getattr(args, "data_dir", mastr.DATA_DIR))
    print("\nMaStR register summary")
    if meta.get("export_url"):
        print(f"  export: {meta['export_url'].rsplit('/', 1)[-1]} "
              f"(built {meta.get('built_at', '?')})")
    rows = conn.execute(
        "SELECT category, COUNT(*) c, SUM(netto_mw) s FROM units "
        "GROUP BY category ORDER BY s DESC").fetchall()
    print(f"\n    {'category':<14} {'units':>9} {'netto MW':>12}   biggest unit")
    for r in rows:
        top = conn.execute(
            "SELECT name, netto_mw, status FROM units WHERE category=? "
            "ORDER BY netto_mw DESC LIMIT 1", (r["category"],)).fetchone()
        tn = (f"{(top['name'] or '?')[:32]} ({(top['netto_mw'] or 0):,.0f} MW"
              + ("" if top["status"] == "In Betrieb"
                 else f", {top['status'][:9]}") + ")"
              if top else "")
        print(f"    {r['category']:<14} {r['c']:>9,} {(r['s'] or 0):>12,.0f}   {tn}")
    cons = conn.execute(
        "SELECT kind, COUNT(*) c FROM consumers GROUP BY kind").fetchall()
    print("\n  consumers: " + ", ".join(f"{c['kind']}: {c['c']:,}" for c in cons))
    print()
    conn.close()


def cmd_watch(args: argparse.Namespace) -> None:
    try:
        while True:
            os.system("clear")
            cmd_now(args)
            print(f"Refreshing every {args.interval}s — Ctrl+C to stop")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="de-energy",
        description="Real-time German electricity supply & demand "
        "(data: energy-charts.info / Fraunhofer ISE, smard.de / Bundesnetzagentur)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("now", help="latest supply/demand snapshot")
    pn.add_argument("--csv", metavar="PATH", help="also append snapshot to CSV")
    pn.set_defaults(f=cmd_now)

    sub.add_parser("today", help="today's energy totals per source").set_defaults(
        f=cmd_today
    )

    sub.add_parser(
        "smard", help="official Bundesnetzagentur figures (smard.de)"
    ).set_defaults(f=cmd_smard)

    pc = sub.add_parser("chart", help="render today's generation vs load to PNG")
    pc.add_argument("-o", "--out", default="charts/today.png")
    pc.set_defaults(f=cmd_chart)

    sub.add_parser(
        "regions", help="per-control-area snapshot (50Hertz, Amprion, TenneT, TransnetBW)"
    ).set_defaults(f=cmd_regions)

    pweb = sub.add_parser("web", help="JSON API server (http.server)")
    pweb.add_argument("--host", default="0.0.0.0")
    pweb.add_argument("--port", type=int, default=8000)
    pweb.set_defaults(f=cmd_web)

    pw = sub.add_parser("watch", help="auto-refreshing snapshot")
    pw.add_argument("-i", "--interval", type=int, default=300)
    pw.add_argument("--csv", metavar="PATH", help="also append snapshots to CSV")
    pw.set_defaults(f=cmd_watch)

    pm = sub.add_parser(
        "mastr", help="MaStR register: biggest plants, batteries, PLZ aggregates"
    )
    pm.add_argument("--data-dir", default=mastr.DATA_DIR,
                    help="cache dir (default data/mastr)")
    msub = pm.add_subparsers(dest="mcmd", required=True)

    mu = msub.add_parser("update",
                         help="download + parse the Gesamtdatenexport (cached)")
    mu.add_argument("--groups", metavar="LIST",
                    help="comma-separated categories to parse "
                    f"(default all: {','.join(mastr.UNIT_SPECS)})")
    mu.add_argument("--skip-operators", action="store_true",
                    help="don't resolve operator names (saves ~165 MB)")
    mu.add_argument("--force", action="store_true",
                    help="re-parse even if the export is unchanged")
    mu.add_argument("-j", "--jobs", type=int, default=2)
    mu.set_defaults(f=cmd_mastr_update)

    mt = msub.add_parser("top", help="largest units per category")
    mt.add_argument("-n", type=int, default=10)
    mt.add_argument("--type", metavar="SLUG",
                    help="one group only (coal,lignite,gas,thermal,biomass,"
                    "hydro,pumped,offshore,onshore,solar,battery,nuclear,"
                    "othergen,offshore-parks,onshore-parks)")
    mt.add_argument("--all", action="store_true",
                    help="include planned/decommissioned units")
    mt.set_defaults(f=cmd_mastr_top)

    mb = msub.add_parser("batteries", help="largest battery storage units")
    mb.add_argument("-n", type=int, default=15)
    mb.add_argument("--all", action="store_true",
                    help="include planned/decommissioned units")
    mb.add_argument("--all-types", action="store_true",
                    help="all storage types, not just batteries")
    mb.set_defaults(f=cmd_mastr_batteries)

    mplz = msub.add_parser("plz", help="units at a postcode, or top postcodes")
    mplz.add_argument("plz", nargs="?", help="German postcode, e.g. 10115")
    mplz.add_argument("--top", action="store_true",
                    help="rank PLZs by registered capacity")
    mplz.add_argument("-n", type=int, default=15)
    mplz.set_defaults(f=cmd_mastr_plz)

    msub.add_parser("summary", help="register totals per category").set_defaults(
        f=cmd_mastr_summary)

    args = p.parse_args(argv)
    args.f(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
