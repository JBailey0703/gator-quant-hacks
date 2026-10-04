"""Parameter sensitivity: do nearby settings also work? (track rule: "nearby parameter values should also work").
SENSITIVITY, NOT SELECTION. The grid below was fixed and committed before this script was ever run on real
data (see VARIANTS.md). Every row is reported for all three periods; the main strategy is unchanged and no row
can replace it. Uses the frozen engine in backtest.py without editing it (the out-of-sample fingerprint stays valid).
    python src/sensitivity.py      ->  analysis/sensitivity.csv"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as b

OUT = b.ROOT / "analysis"
H = 60                                                         # the locked holding period
# one change at a time relative to the main strategy (spun = spin >= 2, short a spun release if day-one move > 0,
# 5% of equity per stock, 1% of 20-day Nasdaq dollar volume, 20 positions)
GRID = [
    ("main strategy",                      {}),
    ("spun = spin 1-4 (looser)",           {"spun_from": 1}),
    ("spun = spin 3-4 (stricter)",         {"spun_from": 3}),
    ("short spun only if day-one > +2%",   {"day_one_min": 0.02}),
    ("max 3% of equity per stock",         {"MAX_WEIGHT": 0.03}),
    ("max 10% of equity per stock",        {"MAX_WEIGHT": 0.10}),
    ("volume cap 0.5% of daily volume",    {"ADV_SHARE": 0.005}),
    ("volume cap 2% of daily volume",      {"ADV_SHARE": 0.02}),
    ("max 10 positions",                   {"MAX_POSITIONS": 10}),
    ("max 30 positions",                   {"MAX_POSITIONS": 30}),
]


def direction(spin, met, first_day, spun_from=2, day_one_min=0.0):
    """Same rule as backtest.direction, with the spun cutoff and day-one threshold as parameters."""
    if spin == "" or pd.isna(first_day):
        return 0
    if int(float(spin)) >= spun_from:
        return -1 if first_day > day_one_min else 0
    return {"yes": 1, "no": -1}.get(met, 0)


def run(t, cal, rets, periods):
    rows = []
    for name, change in GRID:
        signal = {k: change[k] for k in ("spun_from", "day_one_min") if k in change}
        sizing = {k: v for k, v in change.items() if k not in signal}
        d = np.array([direction(s, m, f, **signal) for s, m, f in zip(t["spin_used"], t["met_used"], t["first_day"])])
        saved = {k: getattr(b, k) for k in sizing}
        for k, v in sizing.items():
            setattr(b, k, v)
        try:
            for p, period in periods:
                c, log, traded, skipped = b.portfolio(t, d, H, cal, rets, period)
                m = b.metrics(c, log, traded, skipped)
                rows.append({"setting": name, "period": p, "ann_return": m["ann_return"], "sharpe": m["sharpe"],
                             "max_drawdown": m["max_drawdown"], "trades": m["trades"]})
        finally:
            for k, v in saved.items():
                setattr(b, k, v)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    t, cal, rets = b.prepare()                                 # no out-of-sample forward returns are computed
    elig = t[t["excluded"] == ""]
    periods = [("dev", (*b.DEV, "dev")), ("val", (*b.VAL, "val")), ("oos", (*b.OOS, "oos"))]
    out = run(elig, cal, rets, periods)
    OUT.mkdir(exist_ok=True)
    out.to_csv(OUT / "sensitivity.csv", index=False)
    wide = out.pivot(index="setting", columns="period", values="sharpe")[["dev", "val", "oos"]]
    print("Sharpe ratio (net) by setting:\n", wide.reindex([n for n, _ in GRID]).round(2).to_string())
    print(f"\nwrote {OUT / 'sensitivity.csv'}")
