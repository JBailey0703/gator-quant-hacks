"""Download regular-hours daily prices for every event stock: one Databento request per year
per 500 stocks. Run without --go first to see the cost."""
import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATASET = "XNAS.ITCH"
OUT = ROOT / "data" / "prices" / "databento"
EVENTS = ROOT / "data" / "events"
NY = ZoneInfo("America/New_York")
CHUNK = 500


def year_range(year):
    start = max(f"{year}-01-01", "2018-05-01")                           # Databento starts 2018-05-01
    end = min(f"{year + 1}-01-01", datetime.now().strftime("%Y-%m-%d"))  # no future dates
    return start, end


def symbols_by_year(events):
    """Every year each stock needs: the event year, plus the year before/after near the edges."""
    years = {}
    for r in events.itertuples():
        if not r.ticker:
            continue
        d = pd.Timestamp(r.reaction_day)
        needed = {d.year}
        if d.month <= 3:
            needed.add(d.year - 1)   # 20-day volume average and previous close
        if d.month >= 9:
            needed.add(d.year + 1)   # up to 60 trading days after entry
        for y in needed:
            if 2018 <= y <= datetime.now().year:
                years.setdefault(y, set()).add(r.ticker)
    return years


def fetch(dbn, symbols, year):
    """Hour bars for many stocks -> regular-hours daily bars, one table per stock."""
    start, end = year_range(year)
    df = dbn.timeseries.get_range(dataset=DATASET, schema="ohlcv-1h", stype_in="raw_symbol",
                                  symbols=symbols, start=start, end=end).to_df()
    if df.empty:
        return {}
    df.index = df.index.tz_convert(NY)
    df = df.between_time("09:00", "15:00")          # the 15:00 bar ends at 4 PM
    df["date"] = df.index.date
    daily = df.groupby(["symbol", "date"]).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).reset_index()
    return {s: g.drop(columns="symbol") for s, g in daily.groupby("symbol")}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="actually download (without it: cost estimate only)")
    args = ap.parse_args()

    events = pd.read_csv(EVENTS / "event_records.csv", dtype=str).fillna("")
    dbn = db.Historical(os.environ["DATABENTO_API_KEY"])
    this_year = datetime.now().year

    plan = []
    for year, syms in sorted(symbols_by_year(events).items()):
        todo = sorted(s for s in syms if year == this_year or not (OUT / s / f"{year}.csv").exists())
        plan += [(year, todo[i:i + CHUNK]) for i in range(0, len(todo), CHUNK)]

    total = 0.0
    for year, chunk in plan:
        start, end = year_range(year)
        cost = dbn.metadata.get_cost(dataset=DATASET, symbols=chunk, schema="ohlcv-1h",
                                     stype_in="raw_symbol", start=start, end=end)
        total += cost
        print(f"{year}: {len(chunk)} stocks  ${cost:.2f}")
    print(f"total estimated cost: ${total:.2f}")
    if not args.go:
        raise SystemExit("dry run only. Add --go to download.")

    missing = []
    for year, chunk in plan:
        got = fetch(dbn, chunk, year)
        for sym, df in got.items():
            (OUT / sym).mkdir(parents=True, exist_ok=True)
            df.to_csv(OUT / sym / f"{year}.csv", index=False)
        missing += [(year, s) for s in chunk if s not in got]
        print(f"{year}: saved {len(got)} of {len(chunk)} stocks")
    pd.DataFrame(missing, columns=["year", "symbol"]).to_csv(ROOT / "data" / "prices" / "missing_symbols.csv", index=False)

    dates = {}
    have = 0
    for r in events.itertuples():
        f = OUT / r.ticker / f"{r.reaction_day[:4]}.csv"
        if r.ticker and f.exists():
            if f not in dates:
                dates[f] = set(pd.read_csv(f)["date"].astype(str))
            have += r.reaction_day[:10] in dates[f]
    print(f"\nevents with a price on their reaction day: {have} of {len(events)}")
    print(f"stock-years Databento could not find: {len(missing)}  -> data/prices/missing_symbols.csv")