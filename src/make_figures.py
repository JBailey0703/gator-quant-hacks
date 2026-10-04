"""Figures for the research note (QUANT_NOTE.pdf). Reads the frozen results/ files (never changes them)
and, for Figure 1 only, the XBI prices downloaded by run_all.py. Writes figures/*.png (300 dpi) and .pdf:
fig1_equity and fig2_ingredients (Figures 1-2), figA1_horizons and figA2_event_study (Appendix A).
    python src/make_figures.py
Colors: the first three slots of a colorblind-validated categorical palette (blue, orange, aqua);
gray for reference series. Every multi-series chart has a legend; no dual axes."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RESULTS, ANALYSIS, OUT = ROOT / "results", ROOT / "analysis", ROOT / "figures"
PRICES = ROOT / "data" / "prices" / "databento"

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"           # validated slots 1-3
INK, INK2, MUTED, GRID, BASE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SHADE_VAL, SHADE_OOS = "#f3f2ee", "#eaf3fc"
PERIOD = {"dev": ("In-sample 2018–23", BLUE), "val": ("Validation 2024", ORANGE),
          "oos": ("Out-of-sample 2025–26", AQUA)}
HORIZONS = [1, 2, 3, 5, 10, 20, 40, 60]
FULL, COL = 7.0, 3.45                                          # inches: two-column page widths

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 7.5, "axes.titlesize": 8,
    "axes.labelsize": 7.5, "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
    "axes.edgecolor": BASE, "axes.linewidth": 0.6, "axes.labelcolor": INK2, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.major.size": 0, "ytick.major.size": 0,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.5, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "lines.solid_capstyle": "round"})


def save(fig, name):
    OUT.mkdir(exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"{name}.{ext}", dpi=300, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)
    print("wrote", OUT / f"{name}.png")


def equity(p):
    return pd.read_csv(RESULTS / f"equity_{p}.csv", dtype={"date": str}).set_index("date")["equity"]


def xbi_close():
    files = sorted((PRICES / "XBI").glob("*.csv"))
    return pd.concat(pd.read_csv(f, dtype={"date": str}) for f in files).drop_duplicates("date").set_index("date")["close"]


# ---------- Fig 1: cumulative % growth per period (each starts at $1M) vs XBI, with drawdowns ----------
def fig1():
    x_all = xbi_close()
    ticks = {"dev": (["2019-01-02", "2021-01-04", "2023-01-03"], ["2019", "2021", "2023"]),
             "val": (["2024-01-02", "2024-07-01"], ["Jan '24", "Jul '24"]),
             "oos": (["2025-01-02", "2025-07-01", "2026-01-02", "2026-07-01"], ["Jan '25", "Jul '25", "Jan '26", "Jul '26"])}
    titles = {"dev": "In-sample 2018–23", "val": "Validation 2024", "oos": "Out-of-sample 2025–26 (run once)"}
    fig, axes = plt.subplots(2, 3, figsize=(FULL, 2.5), sharey="row",
                             gridspec_kw={"width_ratios": [3.3, 1.35, 2.05], "height_ratios": [3, 1.1],
                                          "hspace": 0.12, "wspace": 0.07})
    for col, p in enumerate(("dev", "val", "oos")):
        e = equity(p)
        dates = pd.to_datetime(e.index)
        s = (e / 1e6 - 1) * 100
        x = x_all.reindex(e.index).ffill()
        xr = (x / x.iloc[0] - 1) * 100
        ax, dd = axes[0, col], axes[1, col]
        ax.axhline(0, color=BASE, lw=0.7, zorder=1)
        ax.plot(dates, xr.values, color=MUTED, lw=1.0, label="XBI biotech index (buy and hold)", zorder=2)
        ax.plot(dates, s.values, color=BLUE, lw=1.6, label="Strategy, net of costs", zorder=3)
        ax.set_title(titles[p], loc="left", color=INK, fontweight="bold", fontsize=7.5)
        ax.text(0.03, 0.96, f"Strategy {s.iloc[-1]:+.1f}%\nXBI {xr.iloc[-1]:+.1f}%".replace("-", "−"), transform=ax.transAxes,
                ha="left", va="top", fontsize=7, color=INK2, linespacing=1.25)
        d = e / e.cummax() - 1
        dd.fill_between(dates, d.values * 100, 0, color=BLUE, alpha=0.22, lw=0)
        dd.plot(dates, d.values * 100, color=BLUE, lw=0.8)
        dd.text(0.97, 0.08, f"max {d.min() * 100:.1f}%".replace("-", "−"), transform=dd.transAxes, ha="right", va="bottom",
                fontsize=6.8, color=INK2)
        dd.axhline(0, color=BASE, lw=0.6)
        for a in (ax, dd):
            a.set_xlim(dates[0], dates[-1])
        pos, lab = ticks[p]
        dd.set_xticks(pd.to_datetime(pos))
        dd.set_xticklabels(lab)
        ax.set_xticks(pd.to_datetime(pos))
        ax.set_xticklabels([])
    axes[0, 0].set_ylabel("Cumulative return")
    axes[0, 0].yaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v:+.0f}%" if v else "0%"))
    axes[0, 0].set_ylim(-45, 110)
    axes[1, 0].set_ylabel("Drawdown")
    axes[1, 0].yaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    axes[1, 0].set_ylim(-24, 2)
    h, l = axes[0, 0].get_legend_handles_labels()
    fig.legend(h[::-1], l[::-1], loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.06), handlelength=1.8)
    save(fig, "fig1_equity")


# ---------- Fig A2: event study, in-sample vs out-of-sample ----------
GROUPS = [("spun_short", "Spun + rose on day 1 → short", ORANGE),
          ("clean_long", "Clean + endpoint met → long", BLUE),
          ("clean_short", "Clean + endpoint missed → short", AQUA),
          ("no_trade", "No trade (reference)", MUTED)]


def fig_a2():
    fig, axes = plt.subplots(1, 2, figsize=(FULL, 2.25), sharey=True, gridspec_kw={"wspace": 0.06})
    for ax, (p, title) in zip(axes, (("dev", "In-sample 2018–23"), ("oos", "Out-of-sample 2025–26 (run once)"))):
        es = pd.read_csv(RESULTS / f"event_study_{p}.csv")
        ax.axhline(0, color=BASE, lw=0.7)
        notes = []
        for g, label, c in GROUPS:
            d = es[es["group"] == g].sort_values("horizon")
            lw, alpha = (1.0, 0.0) if g == "no_trade" else (1.7, 0.13)
            if alpha:
                ax.fill_between(d["horizon"], d["ci_low"] * 100, d["ci_high"] * 100, color=c, alpha=alpha, lw=0)
            ax.plot(d["horizon"], d["mean"] * 100, color=c, lw=lw, marker="o", ms=3.2,
                    markeredgecolor="white", markeredgewidth=0.6, label=label)
            notes.append(f"{label.split(' →')[0].split(' (')[0]}: n={int(d['n'].iloc[0])}")
        ax.set_xscale("log")
        ax.set_xticks(HORIZONS)
        ax.xaxis.set_major_formatter(mtick.FixedFormatter([str(h) for h in HORIZONS]))
        ax.xaxis.set_minor_locator(mtick.NullLocator())
        ax.set_xlabel("Trading days after entry")
        ax.set_title(title, loc="left", color=INK, fontweight="bold")
        n = {g: int(es[es["group"] == g]["n"].iloc[0]) for g, *_ in GROUPS}
        ax.text(0.02, 0.03, f"events: {n['spun_short']} / {n['clean_long']} / {n['clean_short']} / {n['no_trade']}",
                transform=ax.transAxes, fontsize=7, color=INK2, va="bottom")
    axes[0].set_ylabel("Return vs XBI, trade direction\n(%, gross of costs)")
    axes[0].yaxis.set_major_formatter(mtick.FuncFormatter(lambda v, _: f"{v:+.0f}%".replace("-", "−") if v else "0%"))
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.16), handlelength=1.6, columnspacing=1.4)
    save(fig, "figA2_event_study")

# ---------- Fig A1: Sharpe ratio by holding period (one panel per period) ----------
def fig3():
    data = {"dev": pd.read_csv(RESULTS / "horizon_sweep_dev.csv"), "val": pd.read_csv(RESULTS / "all_horizons_val.csv"),
            "oos": pd.read_csv(RESULTS / "all_horizons_oos.csv")}
    pos = np.arange(len(HORIZONS))
    fig, axes = plt.subplots(1, 3, figsize=(FULL, 1.95), sharey=True, gridspec_kw={"wspace": 0.08})
    lo = min(d["sharpe"].min() for d in data.values())
    hi = max(d["sharpe"].max() for d in data.values())
    for ax, p in zip(axes, ("dev", "val", "oos")):
        label, c = PERIOD[p]
        s = data[p].set_index("horizon").reindex(HORIZONS)["sharpe"]
        ax.axvspan(2.5, 5.5, color="#f3f2ee", lw=0, zorder=0)
        ax.axhline(0, color=BASE, lw=0.7, zorder=1)
        colors = [c if h == 60 else matplotlib.colors.to_rgba(c, 0.32) for h in HORIZONS]
        ax.bar(pos, s.values, width=0.7, color=colors, zorder=3)
        v60 = s.loc[60]
        ax.text(7, v60 + (0.08 if v60 >= 0 else -0.08), f"{v60:.2f}".replace("-", "−"), ha="center", va="bottom" if v60 >= 0 else "top",
                fontsize=7, color=INK, fontweight="bold")
        ax.set_title(label, loc="left", color=INK, fontweight="bold")
        ax.set_xticks(pos)
        ax.set_xticklabels(HORIZONS)
        ax.grid(axis="x", visible=False)
        ax.set_xlim(-0.6, 7.6)
        ax.set_ylim(lo - 0.35, hi + 0.45)
        ax.text(4.0, 0.03, "hypothesized\n5–20 days", transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=6, color=MUTED, linespacing=1.05)
    axes[1].set_xlabel("Holding period (trading days)")
    axes[0].set_ylabel("Sharpe ratio (net)")
    save(fig, "figA1_horizons")


# ---------- Fig 2 (body): what each ingredient adds, one change at a time ----------
INGREDIENTS = [("main", "Main strategy"),
               ("endpoint_only", "Spin ignored\n(endpoint only)"),
               ("keyword", "Keyword count\ninstead of Gemini"),
               ("text_only", "Release text only\n(no registry)"),
               ("registry_today", "Today's registry\n(not pre-release)"),
               ("firstday_only", "Fade every\nday-one rise")]


def fig2():
    dev = pd.read_csv(RESULTS / "variants_dev.csv").set_index("variant")["sharpe"]
    oos = pd.read_csv(RESULTS / "variants_oos.csv").set_index("variant")["sharpe"]
    fig, ax = plt.subplots(figsize=(3.13, 2.35))
    y = np.arange(len(INGREDIENTS))
    h = 0.36
    ax.axvline(0, color=INK2, lw=0.8, zorder=1)
    for vals, off, c, name in ((dev, -h / 2, BLUE, "In-sample 2018–23"), (oos, h / 2, AQUA, "Out-of-sample 2025–26")):
        v = np.array([vals[k] for k, _ in INGREDIENTS])
        ax.axvline(vals["main"], color=c, lw=0.8, ls=(0, (2, 2)), alpha=0.6, zorder=1)
        ax.barh(y + off, v, height=h, color=c, zorder=3, label=name)
        for yy, x in zip(y + off, v):
            ax.text(x + (0.04 if x >= 0 else -0.04), yy, f"{x:.2f}".replace("-", "−"), ha="left" if x >= 0 else "right", va="center",
                    fontsize=6.5, color=INK)
    ax.set_yticks(y)
    ax.set_yticklabels([n for _, n in INGREDIENTS], fontsize=7, linespacing=1.0)
    for lab in ax.get_yticklabels():
        lab.set_color(INK2)
    ax.get_yticklabels()[0].set_fontweight("bold")
    ax.get_yticklabels()[0].set_color(INK)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", length=0, pad=3)
    ax.set_xlim(-1.55, 2.0)
    ax.set_xlabel("Sharpe ratio, net of costs")
    ax.legend(loc="lower center", bbox_to_anchor=(0.38, 1.0), ncol=2, fontsize=6.6, handlelength=1.2,
              handletextpad=0.4, columnspacing=1.0, borderaxespad=0.2)
    save(fig, "fig2_ingredients")


if __name__ == "__main__":
    for f in (fig1, fig2, fig3, fig_a2):
        f()
