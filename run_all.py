"""One command to reproduce the results and check them against the committed ones.
    python run_all.py              fast: prices -> backtest (in-sample + identical out-of-sample rerun)
                                   -> risk diagnostics -> regression tests -> compare with results/
    python run_all.py --yes        same, and allow the Databento price download (about $8 on a fresh clone)
    python run_all.py --full --yes first rebuild the event pipeline from SEC (reuses the saved Gemini
                                   answers in labels/, so no Gemini calls unless those files are deleted)
Needs .env with SEC_USER_AGENT and DATABENTO_API_KEY (see .env.example). Reproduced outputs go to
reproduced/; the committed results/ and analysis/ are never overwritten."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

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


def same_table(a, b):
    try:
        pd.testing.assert_frame_equal(pd.read_csv(a), pd.read_csv(b), check_exact=False, rtol=1e-9, atol=1e-9)
        return "identical"
    except AssertionError:
        return "DIFFERENT"


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
    rows.append(("results/OOS_LOCK.json fingerprint (data + code + versions)",
                 "identical" if all(a[k] == b[k] for k in ("horizon", "inputs_sha256", "pandas", "numpy")) else "DIFFERENT"))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="allow the Databento price download")
    ap.add_argument("--full", action="store_true", help="rebuild the event pipeline from SEC first")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    need("SEC_USER_AGENT", "DATABENTO_API_KEY")

    if args.full:
        for step in FULL_STEPS:
            run(step)

    run("download_event_prices.py", check=False)              # cost estimate only (exits non-zero by design)
    if args.yes:
        run("download_event_prices.py", "--go")
    elif not (ROOT / "data" / "prices" / "databento" / "XBI").exists():
        raise SystemExit("\nPrices are not downloaded yet. Re-run with --yes to allow the Databento download (cost above).")

    sys.path.insert(0, str(SRC))
    import backtest
    import risk_diagnostics
    OUT.mkdir(exist_ok=True)
    backtest.RESULTS = OUT
    print("\n=== backtest: in-sample (2018-2024)", flush=True)
    backtest.in_sample()
    print("\n=== backtest: out-of-sample (identical rerun of the frozen test)", flush=True)
    backtest.out_of_sample()
    print("\n=== risk diagnostics", flush=True)
    risk_diagnostics.main(results=OUT, out=OUT / "analysis")
    tests_ok = run("../tests/test_backtest.py", check=False) == 0

    print("\n=== comparison with the committed results")
    rows = compare()
    for name, status in rows:
        print(f"  {status:9s}  {name}")
    ok = tests_ok and all(s == "identical" for _, s in rows)
    print(f"\nregression tests: {'passed' if tests_ok else 'FAILED'}")
    print("REPRODUCED: every committed result matches." if ok else "Some outputs differ from the committed results (see above).")
    sys.exit(0 if ok else 1)
