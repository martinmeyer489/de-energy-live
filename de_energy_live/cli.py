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
from .sources import energy_charts, smard

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

    pweb = sub.add_parser("web", help="live web dashboard (http.server)")
    pweb.add_argument("--host", default="0.0.0.0")
    pweb.add_argument("--port", type=int, default=8000)
    pweb.set_defaults(f=cmd_web)

    pw = sub.add_parser("watch", help="auto-refreshing snapshot")
    pw.add_argument("-i", "--interval", type=int, default=300)
    pw.add_argument("--csv", metavar="PATH", help="also append snapshots to CSV")
    pw.set_defaults(f=cmd_watch)

    args = p.parse_args(argv)
    args.f(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
