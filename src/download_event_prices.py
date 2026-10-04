"""Download daily prices for every event stock (and XBI) from Databento hour bars 09:00-16:00 ET
(the 9:00 bar includes 9:00-9:30 pre-market trades: closes are exact, volume slightly includes
pre-market). One request per year per 500 stocks. Run without --go first to see the cost."""
import argparse
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATASET = "XNAS.ITCH"
OUT = ROOT / "data" / "prices" / "databento"
MISSING = ROOT / "data" / "prices" / "missing_symbols.csv"
EVENTS = ROOT / "data" / "events"
NY = ZoneInfo("America/New_York")
CHUNK = 500
START = "2018-05-01"                  # Databento XNAS.ITCH starts here
END = "2026-10-03"                    # fixed data end (exclusive): last session 2026-10-02
BENCHMARK = "XBI"


def year_range(year):
    return max(f"{year}-01-01", START), min(f"{year + 1}-01-01", END)


def early_close(d):
    """1 PM close: July 3, Christmas Eve, day after Thanksgiving (holidays have no bars anyway)."""
    return ((d.month == 7 and d.day == 3) or (d.month == 12 and d.day == 24)
            or (d.month == 11 and d.weekday() == 4 and 23 <= d.day <= 29))


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
            if 2018 <= y <= int(END[:4]):
                years.setdefault(y, {BENCHMARK}).add(r.ticker)
    return years


def fetch(dbn, symbols, year):
    """Hour bars -> regular-hours daily bars, one table per stock.
    Bars are labeled by their start time; keep 09:00 through the bar that ends at the close
    (15:00 bar on normal days, 12:00 bar on 1 PM early-close days)."""
    start, end = year_range(year)
    df = dbn.timeseries.get_range(dataset=DATASET, schema="ohlcv-1h", stype_in="raw_symbol",
                                  symbols=symbols, start=start, end=end).to_df()
    if df.empty:
        return {}
    df.index = df.index.tz_convert(NY)
    df["date"] = df.index.date
    last_bar = df["date"].map(lambda d: 12 if early_close(d) else 15)
    df = df[(df.index.hour >= 9) & (df.index.hour <= last_bar)]
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
    known_missing = set()
    if MISSING.exists():
        known_missing = {(int(y), s) for y, s in pd.read_csv(MISSING, dtype=str).itertuples(index=False)}

    plan = []
    for year, syms in sorted(symbols_by_year(events).items()):
        todo = sorted(s for s in syms
                      if not (OUT / s / f"{year}.csv").exists() and (year, s) not in known_missing)
        plan += [(year, todo[i:i + CHUNK]) for i in range(0, len(todo), CHUNK)]

    total = 0.0
    for year, chunk in plan:
        start, end = year_range(year)
        try:
            cost = dbn.metadata.get_cost(dataset=DATASET, symbols=chunk, schema="ohlcv-1h",
                                         stype_in="raw_symbol", start=start, end=end)
        except db.BentoClientError as err:
            if "symbology" not in str(err):
                raise                                    # key, access or rate-limit problem: stop and show it
            cost = 0.0                                   # none of these symbols trade on Nasdaq that year
        total += cost
        print(f"{year}: {len(chunk)} stocks  ${cost:.2f}")
    print(f"total estimated cost: ${total:.2f}")
    if not args.go:
        raise SystemExit("dry run only. Add --go to download.")

    missing = set(known_missing)
    for year, chunk in plan:
        try:
            got = fetch(dbn, chunk, year)
        except db.BentoClientError as err:
            if "symbology" not in str(err):
                raise                                    # key, access or rate-limit problem: stop and show it
            got = {}
        for sym, df in got.items():
            (OUT / sym).mkdir(parents=True, exist_ok=True)
            df.to_csv(OUT / sym / f"{year}.csv", index=False)
        missing |= {(year, s) for s in chunk if s not in got}
        print(f"{year}: saved {len(got)} of {len(chunk)} stocks")
    pd.DataFrame(sorted(missing), columns=["year", "symbol"]).to_csv(MISSING, index=False)

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