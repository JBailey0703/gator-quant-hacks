"""Risk diagnostics on the frozen backtest results. Measurement only: reads results/ and prices, never
changes them, and never re-runs the strategy. Written after the out-of-sample run (see VARIANTS.md).
    python src/risk_diagnostics.py
Writes analysis/risk_diagnostics.csv (one row per period):
  tail risk      worst day / week / month, 1-day VaR and expected shortfall (95%, 99%), skew, kurtosis
  exposure       average and maximum gross / net exposure, positions held, largest position weight
  factors        beta to XBI and to SPY, and alpha after both
  correlation    how much positions held at the same time move together (raw and direction-adjusted)
  regime         returns in down vs up biotech months, and in the 5 worst XBI months
SPY prices are saved under data/prices/benchmarks/ (outside the folder fingerprinted by the OOS lock)."""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as b

ROOT = b.ROOT
RESULTS = ROOT / "results"
OUT = ROOT / "analysis"
SPY_DIR = ROOT / "data" / "prices" / "benchmarks" / "SPY"
PERIODS = ("dev", "val", "oos")


def spy_closes():
    """SPY daily closes built the same way as all other prices (hour bars, regular hours). Downloads once."""
    if not SPY_DIR.exists():
        import databento as db
        from dotenv import load_dotenv
        import download_event_prices as dl
        load_dotenv(ROOT / ".env")
        dbn = db.Historical(os.environ["DATABENTO_API_KEY"])
        cost = sum(dbn.metadata.get_cost(dataset=dl.DATASET, symbols=["SPY"], schema="ohlcv-1h",
                                         stype_in="raw_symbol", start=dl.year_range(y)[0], end=dl.year_range(y)[1])
                   for y in range(2018, 2027))
        print(f"downloading SPY 2018-2026 (estimated ${cost:.2f})")
        if cost > 2:
            raise SystemExit("SPY download would cost more than $2; stopping.")
        SPY_DIR.mkdir(parents=True)
        for y in range(2018, 2027):
            got = dl.fetch(dbn, ["SPY"], y)
            if "SPY" in got:
                got["SPY"].to_csv(SPY_DIR / f"{y}.csv", index=False)
    df = pd.concat(pd.read_csv(f, dtype={"date": str}) for f in sorted(SPY_DIR.glob("*.csv")))
    return df.drop_duplicates("date").set_index("date")["close"]


def daily_returns(ticker, dates):
    """Stock returns on the period's dates; no return after the stock's last trade (like the engine)."""
    px = b.load_prices(ticker)
    close = px["close"].reindex(dates)
    r = close.ffill().pct_change()
    last = close.last_valid_index()
    if last is not None:
        r[r.index > last] = np.nan
    return r


def tail(eq, monthly):
    r = eq.pct_change().dropna()
    q95, q99 = r.quantile(0.05), r.quantile(0.01)
    return {"worst_day": r.min(), "worst_day_date": r.idxmin(),
            "worst_week_5d": eq.pct_change(5).min(), "worst_month_21d": eq.pct_change(21).min(),
            "var95_1d": q95, "es95_1d": r[r <= q95].mean(), "var99_1d": q99, "es99_1d": r[r <= q99].mean(),
            "skew": r.skew(), "excess_kurtosis": r.kurt(), "pct_months_positive": (monthly > 0).mean()}


def factors(eq, xbi, spy):
    r = eq.pct_change()
    d = pd.concat([r, xbi.reindex(eq.index).pct_change(), spy.reindex(eq.index).pct_change()], axis=1,
                  keys=["p", "xbi", "spy"]).dropna()
    beta = lambda col: np.cov(d["p"], d[col])[0, 1] / d[col].var()
    X = np.column_stack([np.ones(len(d)), d["xbi"], d["spy"]])
    coef = np.linalg.lstsq(X, d["p"].values, rcond=None)[0]
    resid = d["p"].values - X @ coef
    return {"beta_xbi": beta("xbi"), "beta_spy": beta("spy"), "corr_xbi": d["p"].corr(d["xbi"]),
            "corr_spy": d["p"].corr(d["spy"]), "two_factor_beta_xbi": coef[1], "two_factor_beta_spy": coef[2],
            "two_factor_alpha_ann": coef[0] * 252, "two_factor_info_ratio": coef[0] / resid.std() * np.sqrt(252)}


def exposure(trades, rets, eq):
    long_, short_ = pd.Series(0.0, index=eq.index), pd.Series(0.0, index=eq.index)
    count, max_w = pd.Series(0, index=eq.index), pd.Series(0.0, index=eq.index)
    for t in trades.itertuples():
        r = rets[t.ticker].loc[t.entry:t.exit].iloc[1:].fillna(0)
        path = pd.concat([pd.Series([t.size], index=[t.entry]), t.size * (1 + r).cumprod()])
        path = path[path.index < t.exit]                       # held overnight after entry, until the exit close
        (long_ if t.dir > 0 else short_).loc[path.index] += path.values
        count.loc[path.index] += 1
        max_w.loc[path.index] = np.maximum(max_w.loc[path.index], path.values / eq.loc[path.index].values)
    gross, net = (long_ + short_) / eq, (long_ - short_) / eq
    return {"avg_gross_exposure": gross.mean(), "max_gross_exposure": gross.max(), "avg_net_exposure": net.mean(),
            "avg_positions": count.mean(), "max_positions": count.max(), "max_position_weight": max_w.max(),
            "pct_days_a_position_over_10pct": (max_w > 0.10).mean()}


def position_correlation(trades, rets):
    """Average correlation of daily returns for every pair of positions held at the same time (>= 10 shared days).
    Raw = do the stocks move together; directional = does our P&L on them move together (a long and a short
    of two correlated stocks hedge each other)."""
    raw, direc = [], []
    tr = trades.sort_values("entry").reset_index(drop=True)
    for i in range(len(tr)):
        a = tr.loc[i]
        for j in range(i + 1, len(tr)):
            c = tr.loc[j]
            if c.entry >= a.exit:
                break                                            # sorted by entry: no later trade overlaps a
            lo, hi = max(a.entry, c.entry), min(a.exit, c.exit)
            d = pd.concat([rets[a.ticker].loc[lo:hi].iloc[1:], rets[c.ticker].loc[lo:hi].iloc[1:]], axis=1).dropna()
            if len(d) >= 10 and d.std().min() > 0:
                rho = d.corr().iloc[0, 1]
                raw.append(rho)
                direc.append(rho * a.dir * c.dir)
    return {"overlapping_pairs": len(raw), "avg_pair_corr_raw": np.mean(raw) if raw else np.nan,
            "avg_pair_corr_directional": np.mean(direc) if direc else np.nan}


def regime(monthly, xbi_monthly):
    d = pd.concat([monthly, xbi_monthly], axis=1, keys=["p", "xbi"]).dropna()
    worst = d.nsmallest(5, "xbi")
    return {"avg_month_when_xbi_down": d.loc[d["xbi"] < 0, "p"].mean(), "avg_month_when_xbi_up": d.loc[d["xbi"] >= 0, "p"].mean(),
            "months_xbi_down": int((d["xbi"] < 0).sum()), "avg_month_in_5_worst_xbi_months": worst["p"].mean(),
            "xbi_avg_in_those_months": worst["xbi"].mean(), "worst_xbi_months": ", ".join(worst.index)}


def monthly_returns(series, capital=None):
    m = series.groupby(series.index.str[:7]).last()
    first = (capital if capital else series.iloc[0])
    return m.pct_change().fillna(m.iloc[0] / first - 1)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    xbi = b.load_prices(b.BENCHMARK)["close"]
    spy = spy_closes()
    rows = []
    for p in PERIODS:
        eq = pd.read_csv(RESULTS / f"equity_{p}.csv", dtype={"date": str}).set_index("date")["equity"]
        trades = pd.read_csv(RESULTS / f"trades_{p}.csv", dtype={"entry": str, "exit": str})
        rets = {tk: daily_returns(tk, eq.index) for tk in trades["ticker"].unique()}
        monthly = monthly_returns(eq, b.CAPITAL)
        xbi_m = monthly_returns(xbi.reindex(eq.index))
        rows.append({"period": p, **tail(eq, monthly), **exposure(trades, rets, eq), **factors(eq, xbi, spy),
                     **position_correlation(trades, rets), **regime(monthly, xbi_m)})
    out = pd.DataFrame(rows).set_index("period")
    out.to_csv(OUT / "risk_diagnostics.csv")
    pd.set_option("display.width", 200)
    print(out.T.to_string())
    print(f"\nwrote {OUT / 'risk_diagnostics.csv'}")
