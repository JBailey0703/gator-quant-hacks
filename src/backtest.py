"""Event study + portfolio backtest. Every rule comes from "Final rules" in HYPOTHESIS.md.
    python src/backtest.py          # 2018-2023: horizon choice; 2024: confirmation; variants; capacity
    python src/backtest.py --oos    # out-of-sample 2025-2026 with the locked horizon. Runs ONCE.
Outputs go to results/. Out-of-sample returns are never computed without --oos."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "data" / "events"
PRICES = ROOT / "data" / "prices" / "databento"
RESULTS = ROOT / "results"

HORIZONS = [1, 2, 3, 5, 10, 20, 40, 60]
MAX_HOLD = 60                                   # every period only uses events whose full 60-day hold fits inside it
DEV = ("2018-05-01", "2023-12-31")
VAL = ("2024-01-01", "2024-12-31")
OOS = ("2025-01-01", "2026-10-02")
BENCHMARK = "XBI"
CAPITAL = 1_000_000
MAX_POSITIONS, MAX_WEIGHT, ADV_SHARE = 20, 0.05, 0.01
COST, BORROW = 0.0020, 0.10                     # per side; short borrow per year
MIN_CAP, MAX_CAP, MIN_DV = 300e6, 10e9, 1.2e6   # market cap; Nasdaq-feed 20-day dollar volume
NASDAQ_SHARE = 0.24                             # only used for the capacity (impact) estimate
BOOT = 2000
SPLIT_FACTORS = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 16, 20, 25, 30, 35, 40, 45, 50, 60, 70, 75, 80, 100, 150, 200, 250]


# ---------- data ----------

def load_prices(ticker):
    files = sorted((PRICES / ticker).glob("*.csv")) if ticker else []
    if not files:
        return None
    df = pd.concat(pd.read_csv(f, dtype={"date": str}) for f in files)
    return df.drop_duplicates("date").sort_values("date").set_index("date")


def trial_repeats(ev):
    """Same company + same accepted trial match within 30 days = one readout (the first one is kept)."""
    first, repeats = {}, set()
    for e in ev.sort_values("reaction_day").itertuples():
        if not e.nct or e.spin_source == "text, match rejected":
            continue
        key, day = (e.cik, e.nct), pd.Timestamp(e.reaction_day)
        if key in first and (day - first[key]).days <= 30:
            repeats.add(e.adsh)
        else:
            first[key] = day
    return repeats


def load_events():
    ev = pd.read_csv(EVENTS / "spin_scores.csv", dtype=str).fillna("")
    kw = EVENTS / "keyword_spin.csv"
    if kw.exists():
        ev = ev.merge(pd.read_csv(kw, dtype=str), on="adsh", how="left").fillna("")
    else:
        ev["spin_keyword"] = ""
    manual = pd.read_csv(EVENTS / "manual_exclusions.csv", dtype=str)
    ev["pre_excluded"] = ev["adsh"].map(dict(zip(manual["adsh"], manual["reason"]))).fillna("")
    rep = ev["adsh"].isin(trial_repeats(ev)) & (ev["pre_excluded"] == "")
    ev.loc[rep, "pre_excluded"] = "repeat: same company and same trial within 30 days"
    return ev


# ---------- per-event table ----------

def daily_returns(px, cal, xbi_r, split_days):
    """Stock returns on the XBI calendar. Short gaps (no trades on a day) count as 0% return. After the
    stock's last real trade in our data, returns are left empty, so the portfolio exits at that last close.
    On reverse-split days the share-count change is removed: the day's price ratio is divided by the
    nearest standard split ratio."""
    close = px["close"].reindex(cal)
    r = close.ffill().pct_change()
    r[r.index > close.last_valid_index()] = np.nan              # stopped trading: no more returns
    for d in split_days:
        if d in r.index and r[d] == r[d]:                       # r[d] == r[d] is False for a missing value
            ratio = 1 + r[d]
            factor = min(SPLIT_FACTORS, key=lambda f: abs(np.log(ratio / f)))
            r[d] = ratio / factor - 1                           # keep the stock's own move that day
    return r, close, px["volume"].reindex(cal).fillna(0)


def event_table(ev, prices, cal, xbi_r, jumps, with_oos=False):
    pos = {d: i for i, d in enumerate(cal)}
    last = len(cal) - 1
    rows, cache = [], {}
    for e in ev.itertuples():
        row = {"adsh": e.adsh, "cik": e.cik, "ticker": e.ticker, "reaction_day": e.reaction_day,
               "timing_basis": e.timing_basis, "registry_source": e.registry_source,
               "event_time": e.accepted if e.timing_basis == "acceptance_time" else e.release_date + "T23:59",
               "spin_used": e.spin_used, "met_used": e.met_used, "spin_source": e.spin_source,
               "spin_text": e.spin_text, "met_text": e.met_text,
               "spin_registry": e.spin_registry, "met_registry": e.met_registry, "spin_keyword": e.spin_keyword,
               "excluded": ""}
        rows.append(row)
        if e.pre_excluded:
            row["excluded"] = e.pre_excluded
            continue
        if e.ticker_source != "release_text":
            row["excluded"] = "no ticker printed in the release (missing, or today's SEC ticker)"
            continue
        px = prices.get(e.ticker)
        if px is None:
            row["excluded"] = "no prices for the release's ticker"
            continue
        i = pos.get(e.reaction_day)
        if i is None or i < 21 or i + 1 > last:
            row["excluded"] = "reaction day outside price calendar"
            continue
        tj = jumps[jumps["ticker"] == e.ticker]
        if e.ticker not in cache:
            cache[e.ticker] = daily_returns(px, cal, xbi_r, set(tj.loc[tj["kind"] == "reverse_split", "day"]))
        r, close, vol = cache[e.ticker]
        prev, rd, entry = cal[i - 1], cal[i], cal[i + 1]
        if close[[prev, rd, entry]].isna().any():
            row["excluded"] = "no trade on previous day, reaction day or entry day"
            continue
        before = set(cal[max(0, i - 21): i + 2])               # up to and including the entry day only
        if ((tj["kind"] == "unverified") & tj["day"].isin(before)).any():
            row["excluded"] = "unverified price jump before entry"
            continue
        during = set(cal[i + 2: i + 62])                         # the days a 60-session hold covers
        row["jump_during_hold"] = bool(((tj["kind"] == "unverified") & tj["day"].isin(during)).any())
        row["split_during_hold"] = bool(((tj["kind"] == "reverse_split") & tj["day"].isin(during)).any())
        splits = tj.loc[tj["kind"] == "reverse_split", "day"]
        if e.shares_filed and ((splits > e.shares_filed) & (splits <= rd)).any():
            row["excluded"] = "reverse split between shares filing and event"
            continue
        if not e.shares_outstanding:
            row["excluded"] = "no shares outstanding"
            continue
        mcap = float(e.shares_outstanding) * close[prev]
        dv20 = float((close.ffill() * vol).iloc[i - 20:i].mean())
        row.update({"entry_idx": i + 1, "entry_date": entry, "mcap": mcap, "dv20": dv20,
                    "sigma20": float(r.iloc[i - 20:i].std()),
                    "first_day": float(r[rd] - xbi_r[rd])})
        if not MIN_CAP <= mcap <= MAX_CAP:
            row["excluded"] = "market cap outside $300M-$10B"
            continue
        if dv20 < MIN_DV:
            row["excluded"] = "illiquid (Nasdaq 20-day dollar volume < $1.2M)"
            continue
        for h in HORIZONS:
            j = i + 1 + h
            if j <= last and (with_oos or e.reaction_day < OOS[0]):   # out-of-sample returns only with --oos
                seg = slice(i + 2, j + 1)
                row[f"ar_{h}"] = float((1 + r.iloc[seg]).prod() - (1 + xbi_r.iloc[seg]).prod())
    t = pd.DataFrame(rows)
    t["period"] = np.select([t["reaction_day"] <= DEV[1], t["reaction_day"] <= VAL[1]], ["dev", "val"], "oos")
    return t, cache


# ---------- signals ----------

def direction(spin, met, first_day):
    """Final rules: spun (2-4) and first-day rise -> short; clean (0-1): met -> long, missed -> short."""
    if spin == "" or pd.isna(first_day):
        return 0
    if int(float(spin)) >= 2:
        return -1 if first_day > 0 else 0
    return {"yes": 1, "no": -1}.get(met, 0)


def signals(t, variant):
    if variant == "endpoint_only":
        return t["met_used"].map({"yes": 1, "no": -1}).fillna(0).astype(int)
    if variant == "firstday_only":
        return np.where(t["first_day"] > 0, -1, 0)
    spin, met = {"main": ("spin_used", "met_used"), "long_only": ("spin_used", "met_used"),
                 "costs_2x": ("spin_used", "met_used"), "no_date_only": ("spin_used", "met_used"),
                 "delisted_longs_lose_all": ("spin_used", "met_used"),
                 "unchanged_registry": ("spin_used", "met_used"), "no_split_trades": ("spin_used", "met_used"),
                 "text_only": ("spin_text", "met_text"),
                 "keyword": ("spin_keyword", "met_text"), "registry_today": ("spin_registry", "met_registry")}[variant]
    s, m = t[spin], t[met]
    if variant == "registry_today":                     # today's record if matched, else text-only
        s, m = s.where(s != "", t["spin_text"]), m.where(s != "", t["met_text"])
    d = np.array([direction(a, b, f) for a, b, f in zip(s, m, t["first_day"])])
    if variant == "long_only":
        d = np.where(d > 0, d, 0)
    if variant == "no_date_only":
        d = np.where(t["timing_basis"] == "acceptance_time", d, 0)
    if variant == "unchanged_registry":
        d = np.where(t["registry_source"] == "unchanged_since_release", d, 0)
    if variant == "no_split_trades":
        d = np.where(t["split_during_hold"].fillna(False).astype(bool), 0, d)
    return d


def group_label(t):
    d, spun = signals(t, "main"), t["spin_used"].replace("", "-1").astype(float) >= 2
    return np.select([(d < 0) & spun, d > 0, (d < 0) & ~spun], ["spun_short", "clean_long", "clean_short"], "no_trade")


def usable(t, cal, period):
    """Events of this period whose full 60-session hold ends inside the period (keeps periods separate)."""
    end = max(i for i, x in enumerate(cal) if x <= period[1])
    return t[(t["period"] == period[2]) & (t["entry_idx"] + MAX_HOLD <= end)]


# ---------- portfolio ----------

def portfolio(t, d, h, cal, rets, period, capital=CAPITAL, cost=COST, borrow=BORROW, impact=False, terminal_loss=False,
              nasdaq_share=NASDAQ_SHARE):
    """Daily simulation. Close of day t: mark to market, exits, then entries (earlier event time first)."""
    pos = {x: i for i, x in enumerate(cal)}
    trades = t.assign(direction=np.asarray(d))
    trades = usable(trades[trades["direction"] != 0], cal, period).sort_values(["entry_idx", "event_time", "ticker"])
    t0 = next(i for i, x in enumerate(cal) if x >= period[0])
    t1 = max(i for i, x in enumerate(cal) if x <= period[1])        # the equity curve never crosses into the next period
    todays = {k: g for k, g in trades.groupby("entry_idx")}
    equity, open_, curve, log, traded = capital, [], [], [], 0.0
    skipped = {"already_held": 0, "max_positions": 0, "no_room": 0}
    last_trade = {tk: int(np.flatnonzero(~np.isnan(a)).max()) for tk, a in rets.items() if (~np.isnan(a)).any()}
    for k in range(t0, int(t1) + 1):
        pnl = 0.0
        for p in open_:
            r = rets[p["ticker"]][k]
            r = 0.0 if np.isnan(r) else r
            fee = p["expo"] * borrow / 252 if p["dir"] < 0 else 0.0
            gain = p["dir"] * p["expo"] * r - fee
            p["expo"] *= 1 + r
            p["pnl"] += gain
            pnl += gain
        keep = []
        for p in open_:
            stopped = k >= p["stop_idx"] and p["stop_idx"] < len(cal) - 1     # last real trade was before today
            if p["exit_idx"] == k or stopped:
                c = p["expo"] * (cost + p["impact"])
                if stopped and terminal_loss and p["dir"] > 0:
                    c = p["expo"]                                          # worst case: a long loses everything
                pnl -= c
                p["pnl"] -= c
                traded += p["expo"]
                log.append({**{x: p[x] for x in ("ticker", "dir", "entry", "size", "pnl", "group")},
                            "exit": cal[k], "exit_reason": "stopped trading" if stopped and p["exit_idx"] != k else "horizon"})
            else:
                keep.append(p)
        open_ = keep
        equity += pnl
        for e in todays.get(k, pd.DataFrame()).itertuples():
            if e.ticker in {p["ticker"] for p in open_}:
                skipped["already_held"] += 1
                continue
            if len(open_) >= MAX_POSITIONS:
                skipped["max_positions"] += 1
                continue
            room = (equity - sum(p["expo"] for p in open_)) / (1 + cost)    # keep total invested <= 100% after the fee
            size = min(MAX_WEIGHT * equity, ADV_SHARE * e.dv20, room)       # limits apply at entry; no rebalancing
            if size <= 0:
                skipped["no_room"] += 1
                continue
            imp = e.sigma20 * np.sqrt(size / (e.dv20 / nasdaq_share)) if impact and e.sigma20 == e.sigma20 else 0.0
            c = size * (cost + imp)
            equity -= c
            traded += size
            open_.append({"ticker": e.ticker, "dir": e.direction, "expo": size, "size": size, "pnl": -c,
                          "exit_idx": k + h, "stop_idx": last_trade.get(e.ticker, len(cal) - 1),
                          "entry": cal[k], "impact": imp, "group": getattr(e, "group", "")})
        curve.append((cal[k], equity))
    return pd.DataFrame(curve, columns=["date", "equity"]), pd.DataFrame(log), traded, skipped


def metrics(curve, log, traded, skipped, capital=CAPITAL):
    eq = curve["equity"]
    r = eq.pct_change().dropna()
    years = max(len(r) / 252, 1e-9)
    out = {"ann_return": (eq.iloc[-1] / capital) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0,
           "ann_vol": r.std() * np.sqrt(252),
           "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan,
           "max_drawdown": (eq / eq.cummax() - 1).min(),
           "turnover_per_year": traded / eq.mean() / years,
           "trades": len(log), **{f"skipped_{k}": v for k, v in skipped.items()}}
    if len(log):
        ret = log["pnl"] / log["size"]
        out.update({"win_rate": (log["pnl"] > 0).mean(), "avg_trade_return": ret.mean(),
                    "exits_stopped_trading": int((log["exit_reason"] == "stopped trading").sum()),
                    "worst_trade_return": ret.min(), "best_trade_return": ret.max(),
                    "long_pnl": log.loc[log["dir"] > 0, "pnl"].sum(), "short_pnl": log.loc[log["dir"] < 0, "pnl"].sum(),
                    "top5_share_of_pnl": log["pnl"].nlargest(5).sum() / log["pnl"].sum() if log["pnl"].sum() else np.nan})
    return out


def yearly(curve):
    """Each year's return = product of that year's daily returns (includes each year's first session)."""
    r = curve["equity"].pct_change()
    r.iloc[0] = curve["equity"].iloc[0] / CAPITAL - 1
    return (1 + r).groupby(curve["date"].str[:4]).prod() - 1


# ---------- event study ----------

def boot(x, cik, seed=0):
    """Mean and 95% interval, resampling companies (a company's events stay together)."""
    df = pd.DataFrame({"x": x, "c": cik}).dropna()
    if df.empty:
        return np.nan, np.nan, np.nan, 0
    g = df.groupby("c")["x"].agg(["sum", "count"])
    idx = np.random.default_rng(seed).integers(0, len(g), size=(BOOT, len(g)))
    m = g["sum"].values[idx].sum(1) / g["count"].values[idx].sum(1)
    return df["x"].mean(), np.percentile(m, 2.5), np.percentile(m, 97.5), len(df)


def event_study(t, by, signed):
    rows = []
    for g, d in t.groupby(by):
        sign = np.sign(d["direction"]).replace(0, 1) if signed else 1
        for h in HORIZONS:
            mean, lo, hi, n = boot(d[f"ar_{h}"] * sign, d["cik"])
            rows.append({by: g, "horizon": h, "n": n, "mean": mean, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(rows, columns=[by, "horizon", "n", "mean", "ci_low", "ci_high"])


# ---------- horizon rule ----------

def choose_horizon(sweep):
    ann, shp = sweep.set_index("horizon")["ann_return"], sweep.set_index("horizon")["sharpe"]
    ok = [h for i, h in enumerate(HORIZONS)
          if all(ann[HORIZONS[j]] > 0 for j in (i - 1, i + 1) if 0 <= j < len(HORIZONS))]
    ok = [h for h in ok if np.isfinite(shp[h])]                 # a horizon with no trades can't be chosen
    if not ok:
        return 20, False
    best = max(shp[h] for h in ok)
    return max(h for h in ok if shp[h] >= best - 0.05), True


# ---------- plots ----------

def plot_edge(es, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    for g, d in es.groupby("group"):
        ax.plot(d["horizon"], d["mean"] * 100, marker="o", label=f"{g}")
        ax.fill_between(d["horizon"], d["ci_low"] * 100, d["ci_high"] * 100, alpha=0.15)
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_xscale("log")
    ax.set_xticks(HORIZONS)
    ax.set_xticklabels(HORIZONS)
    ax.set_xlabel("trading days after entry")
    ax.set_ylabel("trade-direction return vs XBI (%, gross: before costs)")
    ax.set_title(title)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_equity(curves, path, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(7, 3.5))
    for name, c in curves.items():
        ax.plot(pd.to_datetime(c["date"]), c["equity"] / CAPITAL, label=name)
    ax.axhline(1, color="grey", lw=0.8)
    ax.set_ylabel("equity (start = 1)")
    ax.set_title(title)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------- run ----------

VARIANTS = ["main", "long_only", "costs_2x", "endpoint_only", "firstday_only", "text_only",
            "registry_today", "keyword", "no_date_only", "unchanged_registry", "no_split_trades", "delisted_longs_lose_all"]


def run_variants(t, h, cal, rets, period):
    rows = []
    for v in VARIANTS:
        cost, borrow = (2 * COST, 2 * BORROW) if v == "costs_2x" else (COST, BORROW)
        c, log, traded, skipped = portfolio(t, signals(t, v), h, cal, rets, period, cost=cost, borrow=borrow,
                                            terminal_loss=(v == "delisted_longs_lose_all"))
        rows.append({"variant": v, **metrics(c, log, traded, skipped)})
    return pd.DataFrame(rows)


def prepare(with_oos=False):
    xbi = load_prices(BENCHMARK)
    cal = list(xbi.index[(xbi.index >= DEV[0]) & (xbi.index <= OOS[1])])
    xbi_r = xbi["close"].reindex(cal).pct_change()
    ev = load_events()
    jumps = pd.read_csv(EVENTS / "price_jumps.csv", dtype=str)
    prices = {tk: load_prices(tk) for tk in ev["ticker"].unique() if tk}
    t, cache = event_table(ev, prices, cal, xbi_r, jumps, with_oos)
    rets = {tk: v[0].values for tk, v in cache.items()}
    t["group"] = group_label(t)
    t["direction"] = signals(t, "main")
    return t, cal, rets


def in_sample():
    RESULTS.mkdir(exist_ok=True)
    t, cal, rets = prepare()
    t.drop(columns=[c for c in t.columns if c.startswith("ar_")]).to_csv(RESULTS / "event_table.csv", index=False)
    t.groupby(["period", "excluded"]).size().rename("events").to_csv(RESULTS / "exclusions.csv")
    elig = t[t["excluded"] == ""]
    print("eligible events by period and group:\n", elig.groupby(["period", "group"]).size().unstack(fill_value=0))

    for name, p in (("dev", DEV), ("val", VAL)):
        d = usable(elig, cal, (*p, name))
        es = event_study(d, "group", signed=True)
        es.to_csv(RESULTS / f"event_study_{name}.csv", index=False)
        event_study(d[d["spin_used"] != ""].assign(spin=d["spin_used"]), "spin", signed=False) \
            .to_csv(RESULTS / f"event_study_by_spin_{name}.csv", index=False)
        plot_edge(es, RESULTS / f"edge_curve_{name}.png", f"Where the edge lives ({name})")

    sweep = []
    for h in HORIZONS:
        c, log, traded, skipped = portfolio(elig, elig["direction"], h, cal, rets, (*DEV, "dev"))
        sweep.append({"horizon": h, **metrics(c, log, traded, skipped)})
    sweep = pd.DataFrame(sweep)
    sweep.to_csv(RESULTS / "horizon_sweep_dev.csv", index=False)
    h, plateau = choose_horizon(sweep)

    cv, logv, tv, sv = portfolio(elig, elig["direction"], h, cal, rets, (*VAL, "val"))
    mv = metrics(cv, logv, tv, sv)
    choice = {"horizon": h, "plateau_found": plateau, "confirmed_2024": bool(mv["sharpe"] > 0),
              "val_sharpe": mv["sharpe"], "chosen_at": datetime.now().isoformat(timespec="seconds")}
    (RESULTS / "horizon_choice.json").write_text(json.dumps(choice, indent=2))
    print("\nhorizon choice:", choice)

    curves = {}
    for name, p in (("dev", DEV), ("val", VAL)):
        run_variants(elig, h, cal, rets, (*p, name)).to_csv(RESULTS / f"variants_{name}.csv", index=False)
        c, log, traded, skipped = portfolio(elig, elig["direction"], h, cal, rets, (*p, name))
        curves[name] = c
        c.to_csv(RESULTS / f"equity_{name}.csv", index=False)
        log.to_csv(RESULTS / f"trades_{name}.csv", index=False)
        yearly(c).to_csv(RESULTS / f"yearly_{name}.csv")
    plot_equity(curves, RESULTS / "equity_in_sample.png", f"Net equity, H = {h} days (in-sample)")

    allh = []
    for name, p in (("val", VAL),):
        for hh in HORIZONS:
            c, log, traded, skipped = portfolio(elig, elig["direction"], hh, cal, rets, (*p, name))
            allh.append({"period": name, "horizon": hh, **metrics(c, log, traded, skipped)})
    pd.DataFrame(allh).to_csv(RESULTS / "all_horizons_val.csv", index=False)

    cap = []
    for share in (0.15, 0.24, 0.35):                             # assumed Nasdaq share of total volume
        for capital in (1e6, 5e6, 10e6, 25e6, 50e6, 100e6):
            for name, p in (("dev", DEV), ("val", VAL)):
                c, log, traded, skipped = portfolio(elig, elig["direction"], h, cal, rets, (*p, name),
                                                    capital=capital, impact=True, nasdaq_share=share)
                cap.append({"nasdaq_share": share, "capital": capital, "period": name,
                            **metrics(c, log, traded, skipped, capital)})
    pd.DataFrame(cap).to_csv(RESULTS / "capacity.csv", index=False)
    print("\nwrote results/ (event studies, horizon sweep, variants, equity, trades, capacity)")


def frozen_config(h):
    """Horizon + fingerprint of the inputs and this code. Any change gives a different fingerprint."""
    files = [EVENTS / "spin_scores.csv", EVENTS / "price_jumps.csv", EVENTS / "keyword_spin.csv",
             EVENTS / "manual_exclusions.csv", Path(__file__)]
    return {"horizon": h, "inputs_sha256": hashlib.sha256(b"".join(f.read_bytes() for f in files if f.exists())).hexdigest()}


def out_of_sample():
    choice = json.loads((RESULTS / "horizon_choice.json").read_text())
    h = choice["horizon"]
    config, lock = frozen_config(h), RESULTS / "OOS_LOCK.json"
    if lock.exists():
        first = json.loads(lock.read_text())
        if {k: first[k] for k in config} != config:
            raise SystemExit(f"out-of-sample was already run on {first['started']} with other settings or inputs. It runs once.")
        print("identical rerun of the frozen out-of-sample test (same horizon, inputs and code)")
    else:
        lock.write_text(json.dumps({**config, "started": datetime.now().isoformat(timespec="seconds")}, indent=2))
    t, cal, rets = prepare(with_oos=True)
    elig = usable(t[t["excluded"] == ""], cal, (*OOS, "oos"))
    es = event_study(elig, "group", signed=True)
    es.to_csv(RESULTS / "event_study_oos.csv", index=False)
    event_study(elig[elig["spin_used"] != ""].assign(spin=elig["spin_used"]), "spin", signed=False) \
        .to_csv(RESULTS / "event_study_by_spin_oos.csv", index=False)
    plot_edge(es, RESULTS / "edge_curve_oos.png", "Where the edge lives (out-of-sample)")
    run_variants(elig, h, cal, rets, (*OOS, "oos")).to_csv(RESULTS / "variants_oos.csv", index=False)
    c, log, traded, skipped = portfolio(elig, elig["direction"], h, cal, rets, (*OOS, "oos"))
    c.to_csv(RESULTS / "equity_oos.csv", index=False)
    log.to_csv(RESULTS / "trades_oos.csv", index=False)
    yearly(c).to_csv(RESULTS / "yearly_oos.csv")
    plot_equity({"out-of-sample": c}, RESULTS / "equity_oos.png", f"Net equity, H = {h} days (out-of-sample)")
    allh = []
    for hh in HORIZONS:
        c2, l2, t2, s2 = portfolio(elig, elig["direction"], hh, cal, rets, (*OOS, "oos"))
        allh.append({"horizon": hh, **metrics(c2, l2, t2, s2)})
    pd.DataFrame(allh).to_csv(RESULTS / "all_horizons_oos.csv", index=False)
    print("out-of-sample:", metrics(c, log, traded, skipped))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", action="store_true", help="run the out-of-sample test (once)")
    out_of_sample() if ap.parse_args().oos else in_sample()

