"""One command to reproduce the results and check them against the committed ones.
    python run_all.py --check      no keys, no downloads (a few seconds): the regression tests, plus every
                                   headline number of the note recomputed from the committed results/
    python run_all.py              prices -> backtest (in-sample + identical out-of-sample rerun) -> risk
                                   diagnostics -> parameter sensitivity -> regression tests -> compare with results/
    python run_all.py --yes        same, and allow the Databento price download (about $8 on a fresh clone)
    python run_all.py --full --yes first rebuild the event pipeline from SEC (reuses the saved Gemini answers in
                                   labels/, so no Gemini calls are made, but those scripts need GEMINI_API_KEY and
                                   MASSIVE_API_KEY to start)
Needs .env with SEC_USER_AGENT and DATABENTO_API_KEY (see .env.example), except --check. Reproduced outputs go to
reproduced/; the committed results/ and analysis/ are never overwritten."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "reproduced"
# --full: the event pipeline in order. AACT registry snapshots are NOT rebuilt (37 GB of manual downloads);
# the committed data/events/registry_snapshots.csv is used instead.
FULL_STEPS = ["find_events.py", "download_docs.py", "filter_events.py", "extract_events.py", "build_events.py",
              "match_trials.py", "verify_matches.py", "score_spin.py", "keyword_spin.py", "check_splits.py"]


def run(script, *args, check=True):
    print(f"\n=== {script} {' '.join(args)}", flush=True)
    return subprocess.run([sys.executable, str(SRC / script), *args], cwd=ROOT, check=check).returncode


def need(*keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)} (copy .env.example to .env and fill it in)")


def prices(allow_download):
    run("download_event_prices.py", check=False)              # cost estimate only (exits non-zero by design)
    if allow_download:
        run("download_event_prices.py", "--go")
    elif not (ROOT / "data" / "prices" / "databento" / "XBI").exists():
        raise SystemExit("\nPrices are not downloaded yet. Re-run with --yes to allow the Databento download (cost above).")


def same_table(a, b):
    try:
        pd.testing.assert_frame_equal(pd.read_csv(a), pd.read_csv(b), check_exact=False, rtol=1e-9, atol=1e-9)
        return "identical"
    except AssertionError:
        return "DIFFERENT"


def fingerprint():
    """The out-of-sample lock's input fingerprint (backtest.frozen_config), rebuilt with the bytes the lock was
    written with on Windows: CSVs with CRLF line endings, backtest.py with LF, backslash paths. Git changes line
    endings on checkout (and pandas writes LF on macOS/Linux), so hashing raw bytes would report a difference
    for identical content. Any change to a number, a row or the code still changes the fingerprint."""
    events, prices = ROOT / "data" / "events", ROOT / "data" / "prices" / "databento"
    files = [events / "spin_scores.csv", events / "price_jumps.csv", events / "keyword_spin.csv",
             events / "manual_exclusions.csv", SRC / "backtest.py"]
    files += sorted(prices.rglob("*.csv"), key=lambda f: tuple(p.lower() for p in f.relative_to(ROOT).parts))
    digest = hashlib.sha256()
    for f in files:
        if f.exists():
            data = f.read_bytes().replace(b"\r\n", b"\n")
            if f.suffix == ".csv":
                data = data.replace(b"\n", b"\r\n")
            digest.update("\\".join(f.relative_to(ROOT).parts).encode())
            digest.update(data)
    return digest.hexdigest()


def compare():
    rows = []
    for committed, reproduced in [(ROOT / "results", OUT), (ROOT / "analysis", OUT / "analysis")]:
        for f in sorted(committed.glob("*.csv")):
            g = reproduced / f.name
            rows.append((f"{committed.name}/{f.name}", same_table(f, g) if g.exists() else "missing"))
    a, b = json.loads((ROOT / "results" / "horizon_choice.json").read_text()), json.loads((OUT / "horizon_choice.json").read_text())
    keys = ("horizon", "plateau_found", "confirmed_2024")
    rows.append(("results/horizon_choice.json (horizon, plateau, 2024)", "identical" if all(a[k] == b[k] for k in keys) else "DIFFERENT"))
    a, b = json.loads((ROOT / "results" / "OOS_LOCK.json").read_text()), json.loads((OUT / "OOS_LOCK.json").read_text())
    same = a["inputs_sha256"] == fingerprint() and all(a[k] == b[k] for k in ("horizon", "pandas", "numpy"))
    rows.append(("results/OOS_LOCK.json fingerprint (data + code + versions)", "identical" if same else "DIFFERENT"))
    return rows


def check_committed():
    """No keys needed: recompute the note's headline numbers from the committed equity curves and trade logs,
    and confirm they equal the engine's own summary (results/variants_*.csv, main row)."""
    res, ana = ROOT / "results", ROOT / "analysis"
    risk = pd.read_csv(ana / "risk_diagnostics.csv").set_index("period")
    rows, ok = {}, True
    for p, name in (("dev", "Development"), ("val", "Validation 2024"), ("oos", "Out-of-sample")):
        eq = pd.read_csv(res / f"equity_{p}.csv")["equity"]
        log = pd.read_csv(res / f"trades_{p}.csv")
        engine = pd.read_csv(res / f"variants_{p}.csv").set_index("variant").loc["main"]
        r = eq.pct_change().dropna()
        n, sr_d = len(r), r.mean() / r.std()
        mine = {"ann_return": (eq.iloc[-1] / 1e6) ** (252 / n) - 1, "ann_vol": r.std() * np.sqrt(252),
                "sharpe": sr_d * np.sqrt(252), "max_drawdown": (eq / eq.cummax() - 1).min(),
                "trades": len(log), "win_rate": (log["pnl"] > 0).mean()}
        ok &= all(np.isclose(mine[k], engine[k], rtol=1e-9) for k in mine)
        se = np.sqrt((1 + sr_d ** 2 / 2) / n) * np.sqrt(252)     # Lo (2002), i.i.d. daily returns
        rows[name] = {"Annual return": f"{mine['ann_return']:+.1%}", "Volatility": f"{mine['ann_vol']:.1%}",
                      "Sharpe ratio": f"{mine['sharpe']:.2f}",
                      "Sharpe 95% interval": f"{mine['sharpe'] - 1.96 * se:.2f} to {mine['sharpe'] + 1.96 * se:.2f}",
                      "Max drawdown": f"{mine['max_drawdown']:.1%}", "Turnover per year": f"{engine['turnover_per_year']:.1f}x",
                      "Trades": f"{mine['trades']}", "Win rate": f"{mine['win_rate']:.0%}",
                      "Beta to XBI": f"{risk.loc[p, 'beta_xbi']:.2f}",
                      "Alpha per year (XBI + S&P 500)": f"{risk.loc[p, 'two_factor_alpha_ann']:+.1%}"}
    print("\nNet of costs, recomputed from results/equity_*.csv and results/trades_*.csv:")
    print(pd.DataFrame(rows).to_string())
    sharpe = lambda p: pd.read_csv(res / f"variants_{p}.csv").set_index("variant")["sharpe"]
    variants = pd.DataFrame({"dev": sharpe("dev"), "val": sharpe("val"), "oos": sharpe("oos")})
    print("\nOne change at a time (net Sharpe), results/variants_*.csv:")
    print(variants.round(2).to_string())
    sens = pd.read_csv(ana / "sensitivity.csv").pivot(index="setting", columns="period", values="sharpe")
    print("\nParameter sensitivity (net Sharpe), analysis/sensitivity.csv:")
    print(sens[["dev", "val", "oos"]].round(2).to_string())
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="no keys: tests + headline numbers from the committed results")
    ap.add_argument("--yes", action="store_true", help="allow the Databento price download")
    ap.add_argument("--full", action="store_true", help="rebuild the event pipeline from SEC first")
    args = ap.parse_args()

    if args.check:
        tests_ok = run("../tests/test_backtest.py", check=False) == 0
        same = check_committed()
        print(f"\nregression tests: {'passed' if tests_ok else 'FAILED'}")
        print(f"recomputed numbers equal the engine's summary: {'yes' if same else 'NO'}")
        print("This check does not re-run the backtest from prices; python run_all.py --yes does (needs a Databento key).")
        sys.exit(0 if tests_ok and same else 1)

    load_dotenv(ROOT / ".env")
    need("SEC_USER_AGENT", "DATABENTO_API_KEY")
    if args.full:
        need("GEMINI_API_KEY", "MASSIVE_API_KEY")
        prices(args.yes)                                        # build_events and check_splits read the prices
        for step in FULL_STEPS:
            run(step)
    prices(args.yes)

    sys.path.insert(0, str(SRC))
    import backtest
    import risk_diagnostics
    import sensitivity
    OUT.mkdir(exist_ok=True)
    backtest.RESULTS = OUT
    print("\n=== backtest: in-sample (2018-2024)", flush=True)
    backtest.in_sample()
    print("\n=== backtest: out-of-sample (identical rerun of the frozen test)", flush=True)
    backtest.out_of_sample()
    print("\n=== risk diagnostics", flush=True)
    risk_diagnostics.main(results=OUT, out=OUT / "analysis")
    print("\n=== parameter sensitivity", flush=True)
    sensitivity.main(out=OUT / "analysis")
    tests_ok = run("../tests/test_backtest.py", check=False) == 0

    print("\n=== comparison with the committed results")
    rows = compare()
    for name, status in rows:
        print(f"  {status:9s}  {name}")
    ok = tests_ok and all(s == "identical" for _, s in rows)
    print(f"\nregression tests: {'passed' if tests_ok else 'FAILED'}")
    print("REPRODUCED: every committed result matches." if ok else "Some outputs differ from the committed results (see above).")
    sys.exit(0 if ok else 1)
