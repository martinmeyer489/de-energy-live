"""CLI for live German electricity supply/demand data."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from .client import (
    BERLIN,
    LOAD_NAMES,
    RENEWABLE_SOURCES,
    fetch_price,
    fetch_signal,
    fetch_total_power,
)

BAR_WIDTH = 30


def _bar(value: float, vmax: float) -> str:
    if vmax <= 0:
        return ""
    n = round(BAR_WIDTH * value / vmax)
    return "█" * n


def _fmt_mw(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def cmd_now(_args: argparse.Namespace) -> None:
    power = fetch_total_power()
    idx = power.latest_data_index()
    ts = power.times()[idx]
    s = power.series

    def at(*names: str) -> float | None:
        for name in names:
            vals = s.get(name)
            if vals and idx < len(vals) and vals[idx] is not None:
                return vals[idx]
        return None

    load = at(*LOAD_NAMES)
    res = at("Residual load")
    ren_share = at("Renewable share of load")
    trade = at("Cross border electricity trading")

    print(f"\nGerman power grid — {ts:%a %d %b %Y %H:%M} (Europe/Berlin)")
    print("Source: Fraunhofer ISE, energy-charts.info\n")

    print("DEMAND")
    print(f"  Load ........................ {_fmt_mw(load):>8} MW")
    print(f"  Residual load ............... {_fmt_mw(res):>8} MW")
    if ren_share is not None:
        print(f"  Renewable share of load ..... {ren_share:7.1f} %")

    print("\nSUPPLY (generation)")
    gen = []
    for name, vals in power.generation_sources().items():
        v = vals[idx] if idx < len(vals) else None
        if v is not None:
            gen.append((name, v))
    gen.sort(key=lambda x: x[1], reverse=True)
    vmax = gen[0][1] if gen else 1.0
    for name, v in gen:
        tag = " R" if name in RENEWABLE_SOURCES else "  "
        print(f"{tag} {name:<31} {_fmt_mw(v):>8}  {_bar(v, vmax)}")
    if trade is not None:
        label = "net export" if trade >= 0 else "net import"
        print(f"\n  Cross-border trading ........ {abs(trade):>8,.0f} MW ({label})")

    try:
        price = fetch_price().latest()
        if price:
            pt, pv = price
            print(f"\nPRICE")
            print(f"  Day-ahead DE-LU @ {pt:%H:%M} ...... {pv:7.2f} EUR/MWh")
    except Exception:
        pass

    try:
        sig = fetch_signal()
        shares = sig.get("share", [])
        if shares:
            print(f"  Forecast peak renewables today ... {max(shares):.0f} % of load")
    except Exception:
        pass
    print()


def cmd_today(_args: argparse.Namespace) -> None:
    power = fetch_total_power()
    totals = power.daily_totals_gwh()
    ren = sum(v for k, v in totals.items() if k in RENEWABLE_SOURCES)
    conv = sum(v for k, v in totals.items() if k not in RENEWABLE_SOURCES)

    print(f"\nGeneration totals — {datetime.now(BERLIN):%d %b %Y} (so far)")
    print(f"{'Source':<34} {'GWh':>8}")
    print("-" * 44)
    for name, gwh in sorted(totals.items(), key=lambda x: x[1], reverse=True):
        tag = "R" if name in RENEWABLE_SOURCES else " "
        print(f"[{tag}] {name:<31} {gwh:>8.1f}")
    print("-" * 44)
    print(f"  {'Renewables':<32} {ren:>8.1f}")
    print(f"  {'Conventional':<32} {conv:>8.1f}")
    print(f"  {'Total':<32} {ren + conv:>8.1f}")
    print()


def cmd_chart(args: argparse.Namespace) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    power = fetch_total_power()
    times = power.times()
    idx = power.latest_data_index()
    times, sl = times[: idx + 1], slice(0, idx + 1)

    gen = {
        k: [v or 0 for v in vals[sl]]
        for k, vals in power.generation_sources().items()
    }
    gen = dict(sorted(gen.items(), key=lambda x: -max(x[1]) if x[1] else 0))

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.stackplot(times, gen.values(), labels=gen.keys(), alpha=0.85)
    for name in LOAD_NAMES:
        load = power.series.get(name)
        if load:
            ax.plot(
                times,
                [v or 0 for v in load[sl]],
                "k--",
                lw=1.5,
                label="Load",
            )
            break
    ax.set_title(f"German electricity supply vs demand — {datetime.now(BERLIN):%d %b %Y}")
    ax.set_ylabel("MW")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=BERLIN))
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout()

    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fig.savefig(out, dpi=120)
    print(f"Saved {out}")


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
        "(data: energy-charts.info / Fraunhofer ISE)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("now", help="latest supply/demand snapshot").set_defaults(f=cmd_now)
    sub.add_parser("today", help="today's energy totals per source").set_defaults(
        f=cmd_today
    )

    pc = sub.add_parser("chart", help="render today's generation vs load to PNG")
    pc.add_argument("-o", "--out", default="charts/today.png")
    pc.set_defaults(f=cmd_chart)

    pw = sub.add_parser("watch", help="auto-refreshing snapshot")
    pw.add_argument("-i", "--interval", type=int, default=300)
    pw.set_defaults(f=cmd_watch)

    args = p.parse_args(argv)
    args.f(args)
    return 0
