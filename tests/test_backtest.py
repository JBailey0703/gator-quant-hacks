"""Regression tests for src/backtest.py on invented data (no real prices, no API keys needed).
    python tests/test_backtest.py
Each line prints PASS or FAIL. Covers the fixes from the mock-judge reviews: lookahead, splits,
period isolation, stopped trading, tickers, exclusions/repeats, limits, reporting, comparisons, OOS lock."""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import backtest as b

results = []


def check(name, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


# ---------- invented market ----------
cal = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-01-02", periods=700)]
b.DEV, b.VAL, b.OOS = ("2023-01-02", "2023-12-31"), ("2024-01-01", "2024-12-31"), ("2025-01-01", cal[-1])
n = len(cal)
xbi_close = pd.Series(np.cumprod(np.where(np.arange(n) == 100, 1.02, 1.0)) * 100, index=cal)   # XBI +2% on day 100
xbi_r = xbi_close.pct_change()


def flat(p=10.0, days=n, vol=1e6):
    return pd.DataFrame({"close": p, "volume": vol}, index=cal[:days])


prices = {k: flat() for k in ("J", "K", "N", "L", "V", "O", "M")}
S = flat(); S.iloc[100:, 0] = 100.0; prices["S"] = S                     # pure 1-for-10 reverse split on day 100
D = flat(); D = D.iloc[:60]; D.iloc[45:, 0] = 12.0; prices["D"] = D      # +20% on day 45, last trade day 59
E = flat(); E = E.iloc[:42]; prices["E"] = E                             # last trade on its entry day (41)
for k in range(25):
    prices[f"F{k}"] = flat(vol=1e8)                                      # many same-day signals
jumps = pd.DataFrame([{"ticker": "J", "day": cal[70], "kind": "unverified"},     # during the hold
                      {"ticker": "K", "day": cal[35], "kind": "unverified"},     # before the signal
                      {"ticker": "N", "day": cal[41], "kind": "unverified"},     # on the entry day
                      {"ticker": "S", "day": cal[100], "kind": "reverse_split"}])
base = dict(timing_basis="acceptance_time", registry_source="", release_date="", spin_text="0", met_text="no",
            spin_registry="", met_registry="", spin_keyword="0", spin_source="aact", shares_filed="2022-01-01",
            shares_outstanding="1e8", ticker_source="release_text", pre_excluded="", nct="", spin_used="0", met_used="yes")
late23 = max(i for i, d in enumerate(cal) if d <= "2023-12-31") - 20
late24 = max(i for i, d in enumerate(cal) if d <= "2024-12-31") - 20
oos_i = min(i for i, d in enumerate(cal) if d >= "2025-01-01") + 5
rows = [("e_J", "J", 40), ("e_K", "K", 40), ("e_N", "N", 40), ("e_S", "S", 90), ("e_D", "D", 40), ("e_E", "E", 40),
        ("e_L", "L", late23), ("e_V", "V", late24), ("e_O", "O", oos_i), ("e_M", "M", 200)] + \
       [(f"e_F{k}", f"F{k}", 150) for k in range(25)]
ev = pd.DataFrame([{**base, "adsh": a, "cik": a, "ticker": tk, "reaction_day": cal[i], "accepted": cal[i] + "T08:00"}
                   for a, tk, i in rows])
ev.loc[ev.adsh == "e_M", ["spin_source", "spin_registry", "met_registry"]] = ["text", "3", "no"]
extra = [{**base, "adsh": "e_sec", "cik": "x", "ticker": "J", "reaction_day": cal[50], "accepted": cal[50] + "T08:00",
          "ticker_source": "sec_current"},
         {**base, "adsh": "e_man", "cik": "y", "ticker": "J", "reaction_day": cal[52], "accepted": cal[52] + "T08:00",
          "pre_excluded": "journal publication"}]
ev = pd.concat([ev, pd.DataFrame(extra)], ignore_index=True)

t, cache = b.event_table(ev, prices, cal, xbi_r, jumps)
rets = {k: v[0].values for k, v in cache.items()}
t["group"], t["direction"] = b.group_label(t), b.signals(t, "main")
T = t.set_index("adsh")

# ---------- 1. no lookahead in eligibility ----------
check("1a jump during the hold keeps the trade", T.loc["e_J", "excluded"] == "")
check("1b ...and flags it", bool(T.loc["e_J", "jump_during_hold"]))
check("1c jump before the signal excludes", T.loc["e_K", "excluded"] == "unverified price jump before the signal")
check("1d jump on the entry day keeps the trade (that close is unknown at order time)",
      T.loc["e_N", "excluded"] == "" and bool(T.loc["e_N", "jump_during_hold"]))
# ---------- 2. splits ----------
check("2a pure 1-for-10 split day return = 0% while XBI is +2%", abs(cache["S"][0][cal[100]]) < 1e-12)
check("2b split during hold flagged", bool(T.loc["e_S", "split_during_hold"]))
check("2c no_split_trades sensitivity drops it", b.signals(t, "no_split_trades")[t.index[t.adsh == "e_S"][0]] == 0)
# ---------- 3. period isolation ----------
check("3a late-2023 event excluded before any return is computed",
      T.loc["e_L", "excluded"] == "60-day window crosses the period end" and pd.isna(T.loc["e_L", "ar_1"]))
check("3b late-2024 event excluded the same way", T.loc["e_V", "excluded"] == "60-day window crosses the period end")
c, log, tr, sk = b.portfolio(t, t["direction"], 60, cal, rets, (*b.VAL, "val"))
check("3c validation equity curve ends inside 2024", c["date"].max() <= "2024-12-31")
check("3d out-of-sample returns not computed without --oos", pd.isna(T.loc["e_O", "ar_5"]))
t2, _ = b.event_table(ev, prices, cal, xbi_r, jumps, with_oos=True)
check("3e ...but computed with --oos", not pd.isna(t2.set_index("adsh").loc["e_O", "ar_5"]))
# ---------- 4. stocks that stop trading ----------
dd = t[t.adsh == "e_D"]
c, log, tr, sk = b.portfolio(dd, [1], 40, cal, rets, (*b.DEV, "dev"))
size = log.iloc[0]["size"]
check("4a exits at the last real close", log.iloc[0]["exit_reason"] == "stopped trading" and log.iloc[0]["exit"] == cal[59])
check("4b long P&L = real move to last close minus 2 fees",
      abs(log.iloc[0]["pnl"] - (size * 0.2 - size * .002 - size * 1.2 * .002)) < 1e-6)
c, log, tr, sk = b.portfolio(dd, [-1], 40, cal, rets, (*b.DEV, "dev"))
fees = sum(size * (1.2 if k >= 46 else 1.0) * 0.10 / 252 for k in range(42, 60))
check("4c short pays borrow only while trading", abs(log.iloc[0]["pnl"] - (-size * 0.2 - size * .002 - size * 1.2 * .002 - fees)) < 1e-6)
c, log, tr, sk = b.portfolio(t[t.adsh == "e_E"], [-1], 40, cal, rets, (*b.DEV, "dev"))
size_e = log.iloc[0]["size"]
check("4d trading stops on the entry day: no extra day of borrow", abs(log.iloc[0]["pnl"] - (-2 * size_e * .002)) < 1e-6)
c, log, tr, sk = b.portfolio(dd, [1], 40, cal, rets, (*b.DEV, "dev"), terminal_loss=1.0)
check("4e worst case: a long that stops trading loses 100%", abs(log.iloc[0]["pnl"] - (size * 0.2 - size * .002 - size * 1.2)) < 1e-6)
c, log, tr, sk = b.portfolio(dd, [1], 40, cal, rets, (*b.DEV, "dev"), terminal_loss=0.30)
check("4f middle case: loses 30% then pays the exit fee",
      abs(log.iloc[0]["pnl"] - (size * 0.2 - size * .002 - size * 1.2 * 0.30 - size * 1.2 * 0.70 * .002)) < 1e-6)
check("4g event-study return stops at the last trade (stock +20%, XBI frozen too)", abs(T.loc["e_D", "ar_40"] - 0.2) < 1e-12)
# ---------- 5/6. tickers, exclusions, repeats ----------
check("5 today's SEC ticker excluded", T.loc["e_sec", "excluded"].startswith("no ticker printed"))
check("6a manual exclusion applied", T.loc["e_man", "excluded"] == "journal publication")
rep = pd.DataFrame([{"adsh": "a1", "cik": "1", "nct": "NCT1", "spin_source": "aact", "reaction_day": "2023-01-10"},
                    {"adsh": "a2", "cik": "1", "nct": "NCT1", "spin_source": "aact", "reaction_day": "2023-02-01"},
                    {"adsh": "a3", "cik": "1", "nct": "NCT1", "spin_source": "aact", "reaction_day": "2023-04-01"},
                    {"adsh": "a4", "cik": "1", "nct": "NCT1", "spin_source": "text, match rejected", "reaction_day": "2023-04-05"}])
check("6b same company + trial within 30 days = repeat", b.trial_repeats(rep) == {"a2"})
# ---------- 7. limits and reporting ----------
ff = t[t.adsh.str.startswith("e_F")]
c, log, tr, sk = b.portfolio(ff, ff["direction"], 5, cal, rets, (*b.DEV, "dev"))
check("7a at most 20 positions; others skipped with a reason", len(log) == 20 and sk["max_positions"] == 5)
check("7b gross <= 100% of equity after entry fees", log["size"].sum() <= b.CAPITAL - log["size"].sum() * 0.002 + 1e-6)
ff_imp = ff.assign(sigma20=0.05)
c, log, tr, sk = b.portfolio(ff_imp, ff_imp["direction"], 5, cal, rets, (*b.DEV, "dev"), impact=True)
paid = (log["size"] * 0.002).sum() + sum(s * 0.05 * np.sqrt(s / (1e9 / 0.24)) for s in log["size"])
check("7c with impact costs, gross still <= equity after all entry costs", log["size"].sum() <= b.CAPITAL - paid + 1e-6)
curve = pd.DataFrame({"date": ["2023-12-29", "2024-01-02", "2024-01-03"], "equity": [1e6, 1.1e6, 1.1e6]})
check("7d yearly return includes the first session", abs(b.yearly(curve)["2024"] - 0.10) < 1e-12)
check("7e horizon picker: no valid Sharpe -> 20", b.choose_horizon(pd.DataFrame(
    {"horizon": b.HORIZONS, "ann_return": [0.1] * 8, "sharpe": [np.nan] * 8})) == (20, False))
check("7f horizon picker: plateau + longer within 0.05", b.choose_horizon(pd.DataFrame(
    {"horizon": b.HORIZONS, "ann_return": [.1, -.1, .2, .3, .25, .2, .1, -.05],
     "sharpe": [2, 0, 1, 1.5, 1.52, 1.0, .5, 0]})) == (10, True))
check("7g empty group keeps event-study columns", list(b.event_study(t.iloc[0:0], "group", True).columns)[:2] == ["group", "horizon"])
# ---------- 8. comparisons change one thing ----------
d_main, d_kw = b.signals(t, "main"), b.signals(t, "keyword")
check("8a keyword variant uses the main endpoint labels (same spin -> same trades)", (d_main == d_kw).all())
i_m = t.index[t.adsh == "e_M"][0]
check("8b today's-registry variant leaves text-scored events unchanged", b.signals(t, "registry_today")[i_m] == d_main[i_m])
# ---------- 9. out-of-sample lock and fingerprint ----------
tmp = Path(tempfile.mkdtemp())
for f in ("spin_scores.csv", "price_jumps.csv", "keyword_spin.csv", "manual_exclusions.csv"):
    (tmp / f).write_text("x\n")
(tmp / "prices" / "AAA").mkdir(parents=True)
(tmp / "prices" / "AAA" / "2025.csv").write_text("date,close\n2025-01-02,10\n")
b.ROOT, b.RESULTS, b.EVENTS, b.PRICES = tmp, tmp, tmp, tmp / "prices"
h1 = b.frozen_config(5)["inputs_sha256"]
(tmp / "prices" / "AAA" / "2025.csv").write_text("date,close\n2025-01-02,11\n")
check("9a fingerprint changes when a price file changes", b.frozen_config(5)["inputs_sha256"] != h1)
(tmp / "horizon_choice.json").write_text(json.dumps({"horizon": 5}))
b.prepare = lambda with_oos=False: (b.event_table(ev, prices, cal, xbi_r, jumps, with_oos)[0].assign(
    group=lambda d: b.group_label(d), direction=lambda d: b.signals(d, "main")), cal, rets)
def quiet_oos():
    with contextlib.redirect_stdout(io.StringIO()):          # invented data: don't print its "out-of-sample" metrics
        b.out_of_sample()
quiet_oos()
check("9b lock written before results", (tmp / "OOS_LOCK.json").exists())
try:
    quiet_oos()
    check("9c identical rerun allowed", True)
except SystemExit:
    check("9c identical rerun allowed", False)
(tmp / "spin_scores.csv").write_text("changed\n")
try:
    quiet_oos()
    check("9d changed rerun refused", False)
except SystemExit:
    check("9d changed rerun refused", True)

print(f"\n{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
