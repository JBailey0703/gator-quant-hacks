"""Cross-check only (Databento vs Webull for VRTX and IONS). Writes to data/prices/checks/, never to the backtest's price folder."""
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import databento as db
import pandas as pd
from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from webull_bt.feed import WebullBar, _locate_price_records

import logging
logging.getLogger("webull").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
PRICE_DIR = ROOT / "data" / "prices" / "checks"   # kept apart from data/prices/databento (backtest prices)
NY = ZoneInfo("America/New_York")
DATASET = "XNAS.ITCH"


def make_client():
    load_dotenv(ROOT / ".env")
    api = ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "us")
    api.add_endpoint("us", "api.webull.com")
    return DataClient(api)

def make_databento():
    load_dotenv(ROOT / ".env")
    return db.Historical(os.environ["DATABENTO_API_KEY"])

def check_cost(dbn, symbols, start, end):
    cost = dbn.metadata.get_cost(dataset=DATASET, symbols=symbols, schema="ohlcv-1h", start=start, end=end)
    print(f"estimated cost: ${cost:.2f}")
    return cost

def fetch_year(client, symbol, year, category):
    start = int(datetime(year, 1, 1, tzinfo=NY).timestamp() * 1000)
    end = int(datetime(year, 12, 31, 23, 59, tzinfo=NY).timestamp() * 1000)
    resp = client.market_data.get_batch_history_bar(
        symbols=[symbol], category=category, timespan="D", count="1200",
        real_time_required=False, trading_sessions="RTH",
        start_time=start, end_time=end,
    )
    rows = []
    for rec in _locate_price_records(resp.json()):
        bar = WebullBar.from_api(rec)
        rows.append({"date": bar.datetime.astimezone(NY).date(),
                     "open": bar.open, "high": bar.high, "low": bar.low,
                     "close": bar.close, "volume": bar.volume})
    return pd.DataFrame(rows).sort_values("date") if rows else pd.DataFrame()

def fetch_year_databento(dbn, symbol, year):
    start = max(f"{year}-01-01", "2018-05-01")
    end = min(f"{year + 1}-01-01", datetime.now().strftime("%Y-%m-%d"))
    data = dbn.timeseries.get_range(
        dataset=DATASET, schema="ohlcv-1h", stype_in="raw_symbol",
        symbols=[symbol], start=start, end=end,
    )
    df = data.to_df()
    if df.empty:
        return pd.DataFrame()
    df.index = df.index.tz_convert(NY)          # hour bars in New York time
    df = df.between_time("09:00", "15:00")      # 9:00–15:00 bars; the 15:00 bar ends at 4 PM
    daily = df.groupby(df.index.date).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"))
    daily.index.name = "date"
    return daily.reset_index()

def download(client, symbol, years, source, category="US_STOCK"):
    for year in years:
        path = PRICE_DIR / source / symbol / f"{year}.csv"
        if path.exists() and year < datetime.now().year:
            continue  # finished years never change, so don't re-download
        try:
            if source == "databento":
                df = fetch_year_databento(client, symbol, year)
            else:
                df = fetch_year(client, symbol, year, category)
        except Exception as e:
            if "INVALID_SYMBOL" in str(e):
                print(f"UNKNOWN {source} {symbol}: not available, skipping")
                break
            print(f"FAILED  {source} {symbol} {year}: {e}")
            continue
        if df.empty:
            print(f"NO DATA {source} {symbol} {year}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        print(f"saved   {source} {symbol} {year}: {len(df)} days")
        time.sleep(0.2)

def load(symbol, source):
    files = sorted((PRICE_DIR / source / symbol).glob("*.csv"))
    if not files:
        return pd.DataFrame()
    df = pd.concat(pd.read_csv(f, parse_dates=["date"]) for f in files)
    return df.drop_duplicates("date").sort_values("date")


def missing_report(symbols, source):
    calendar = set(load("XBI", source)["date"])  # XBI trades every market day
    rows = []
    for s in symbols:
        df = load(s, source)
        if df.empty:
            rows.append({"symbol": s, "days": 0, "note": "no data at all"})
            continue
        first, last = df["date"].min(), df["date"].max()
        expected = {d for d in calendar if first <= d <= last}
        missing = sorted(expected - set(df["date"]))
        ratio = df["close"] / df["close"].shift(1)
        jumps = df.loc[(ratio > 3) | (ratio < 1 / 3), "date"]
        rows.append({"symbol": s, "first": first.date(), "last": last.date(),
                     "days": len(df), "missing": len(missing),
                     "missing_dates": ";".join(str(d.date()) for d in missing[:20]),
                     "possible_splits": ";".join(str(d.date()) for d in jumps)})
    report = pd.DataFrame(rows)
    report.to_csv(PRICE_DIR / f"missing_report_{source}.csv", index=False)
    print(f"\n=== {source} ===")
    print(report.to_string(index=False))


def compare_sources(symbol):
    a, b = load(symbol, "databento"), load(symbol, "webull")
    m = a.merge(b, on="date", suffixes=("_db", "_wb"))
    diff = (m["close_db"] / m["close_wb"] - 1).abs()
    worst = m.loc[diff.idxmax(), "date"].date()
    print(f"compare {symbol}: {len(m)} days, median diff {diff.median():.3%}, max {diff.max():.3%} on {worst}")


if __name__ == "__main__":
    symbols = ["VRTX", "IONS", "SPRB", "CLVS"]  # test list; later, tickers from your events
    years = range(2018, 2027)

    dbn = make_databento()
    cost = check_cost(dbn, ["XBI"] + symbols, "2018-05-01", datetime.now().strftime("%Y-%m-%d"))
    if cost > 5:
        raise SystemExit("cost above $5: stop and check")
    for s in ["XBI"] + symbols:
        download(dbn, s, years, "databento")

    wb = make_client()
    download(wb, "XBI", years, "webull", category="US_ETF")
    for s in ["VRTX", "IONS"]:
        download(wb, s, years, "webull")

    missing_report(symbols, "databento")
    missing_report(["VRTX", "IONS"], "webull")
    for s in ["VRTX", "IONS"]:
        compare_sources(s)