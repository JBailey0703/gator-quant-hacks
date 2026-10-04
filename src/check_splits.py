"""Classify one-day price jumps inside event windows: reverse split vs real move.
Databento prices are not split-adjusted. Rule (fixed before any returns):
 - rise > 3x: reverse split if jump-day volume < 5x its 10-day average, or an 8-K mentioning a
   reverse split was filed 45 days before to 5 days after; otherwise a real move
 - drop below 1/3: real move if jump-day volume >= 5x average; otherwise unverified (event excluded)
Output: data/events/price_jumps.csv (used by the backtest)."""
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
PRICES = ROOT / "data" / "prices" / "databento"
EVENTS = ROOT / "data" / "events"
BEFORE, AFTER = 21, 63            # window: 21 sessions before the reaction day to 63 after
JUMP = 3.0
VOLUME_X = 5.0


def load(ticker, cache={}):
    if ticker not in cache:
        files = sorted((PRICES / ticker).glob("*.csv")) if ticker else []
        cache[ticker] = (pd.concat(pd.read_csv(f) for f in files).drop_duplicates("date")
                         .sort_values("date").set_index("date") if files else None)
    return cache[ticker]


def split_8k(cik, day):
    """True if an 8-K mentioning a reverse split was filed 45 days before to 5 days after."""
    d = pd.Timestamp(day)
    for q in ('"reverse stock split"', '"reverse split"'):
        params = {"q": q, "forms": "8-K", "ciks": cik.zfill(10), "dateRange": "custom",
                  "startdt": (d - pd.Timedelta(days=45)).strftime("%Y-%m-%d"),
                  "enddt": (d + pd.Timedelta(days=5)).strftime("%Y-%m-%d")}
        for attempt in range(5):
            r = requests.get("https://efts.sec.gov/LATEST/search-index", params=params, headers=HEADERS, timeout=30)
            time.sleep(0.15)
            if r.status_code == 200:
                break
            time.sleep(2 ** attempt)
        if r.status_code == 200 and r.json().get("hits", {}).get("total", {}).get("value", 0) > 0:
            return True
    return False


if __name__ == "__main__":
    ev = pd.read_csv(EVENTS / "event_records.csv", dtype=str).fillna("")
    jumps = {}
    for r in ev.itertuples():
        px = load(r.ticker)
        if px is None or r.reaction_day not in px.index:
            continue
        i = px.index.get_loc(r.reaction_day)
        w = px.iloc[max(0, i - BEFORE): i + AFTER + 1]
        ratio = w["close"] / w["close"].shift(1)
        for day in ratio[(ratio > JUMP) | (ratio < 1 / JUMP)].index:
            j = px.index.get_loc(day)
            avg_vol = px["volume"].iloc[max(0, j - 10):j].mean()
            jumps[(r.ticker, day)] = {"ticker": r.ticker, "cik": r.cik, "day": day,
                                      "ratio": round(ratio[day], 4),
                                      "volume_x": round(px["volume"].iloc[j] / avg_vol, 2) if avg_vol else None}

    rows = []
    for k in jumps.values():
        quiet = k["volume_x"] is not None and k["volume_x"] < VOLUME_X
        if k["ratio"] > JUMP:
            filed = False if quiet else split_8k(k["cik"], k["day"])
            k["kind"] = "reverse_split" if quiet or filed else "real_move"
            k["evidence"] = "low volume" if quiet else ("8-K mentions reverse split" if filed else "high volume, no 8-K")
        else:
            k["kind"] = "unverified" if quiet else "real_move"
            k["evidence"] = "low volume on a crash" if quiet else "high volume"
        rows.append(k)

    out = pd.DataFrame(rows).sort_values(["ticker", "day"])
    out.to_csv(EVENTS / "price_jumps.csv", index=False)
    print(f"one-day jumps in event windows: {len(out)}")
    print(out.groupby(["kind", "evidence"]).size().to_string())