"""Event records, part 2: one verified record per readout.
Adds SEC acceptance time, reaction day (exchange calendar), ticker fallback, shares outstanding
known before the event, and removes repeat announcements."""
import json
import os
import re
import time
from bisect import bisect_left
from datetime import date, datetime, time as dtime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
CACHE = ROOT / "data" / "cache"
EVENTS = ROOT / "data" / "events"
LAST_EVENT_DAY = date(2026, 9, 2)     # from HYPOTHESIS.md: events through 2026-09-02
REPEAT_WINDOW_DAYS = 30               # same company + same drug within 30 days = one readout
MAX_FILING_LAG_DAYS = 7               # 8-K is due within 4 business days; longer gaps = date can't be trusted

# ---------- SEC lookups (cached, so reruns are free) ----------

def sec_json(url, path):
    if path.exists():
        return json.loads(path.read_text())
    for attempt in range(5):
        r = requests.get(url, headers=HEADERS, timeout=30)
        time.sleep(0.12)                              # SEC: at most 10 requests per second
        if r.status_code == 200:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(r.text)
            return r.json()
        if r.status_code == 404:
            return {}
        time.sleep(2 ** attempt)
    return {}


def acceptance_times(cik):
    """{filing ID: acceptance time} for every filing of a company, plus its current tickers."""
    sub = sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json",
                   CACHE / "submissions" / f"CIK{cik}.json")
    if not sub:
        return {}, []
    blocks = [sub["filings"]["recent"]]
    for f in sub["filings"].get("files", []):        # older filings live on extra pages
        blocks.append(sec_json(f"https://data.sec.gov/submissions/{f['name']}",
                               CACHE / "submissions" / f["name"]))
    times = {}
    for b in blocks:
        times.update(zip(b.get("accessionNumber", []), b.get("acceptanceDateTime", [])))
    return times, sub.get("tickers", [])


def shares_history(cik):
    """Every shares-outstanding number the company reported, with the date it was filed."""
    for concept in ("dei/EntityCommonStockSharesOutstanding", "us-gaap/CommonStockSharesOutstanding"):
        d = sec_json(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{concept}.json",
                     CACHE / "shares" / f"CIK{cik}_{concept.split('/')[1]}.json")
        rows = d.get("units", {}).get("shares", []) if d else []
        if rows:
            return pd.DataFrame(rows)[["end", "filed", "val"]]
    return pd.DataFrame(columns=["end", "filed", "val"])


# ---------- trading calendar ----------

def sessions():
    """Trading days = days XBI traded."""
    files = sorted((ROOT / "data" / "prices" / "databento" / "XBI").glob("*.csv"))
    days = pd.concat(pd.read_csv(f, parse_dates=["date"]) for f in files)["date"].dt.date
    return sorted(set(days))


def close_time(d):
    """4 PM, except early-close days (1 PM): July 3, Christmas Eve, day after Thanksgiving."""
    early = ((d.month == 7 and d.day == 3) or (d.month == 12 and d.day == 24)
             or (d.month == 11 and d.weekday() == 4 and 23 <= d.day <= 29))
    return dtime(13, 0) if early else dtime(16, 0)


def reaction_day(ts, sess):
    """First trading session whose close is after time ts (New York time)."""
    for d in sess[bisect_left(sess, ts.date()):]:
        if datetime.combine(d, close_time(d)) > ts:
            return d
    return None

TICKER = re.compile(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?")


def pick_ticker(raw, current):
    """Ticker as printed in the release. If it lists several (joint releases) or isn't a ticker
    (a company name, drug code, foreign code), use the filing company's SEC ticker."""
    raw = raw.split(":")[-1].replace("$", "").strip().upper()
    parts = [p.strip() for p in re.split(r",|/|;|\bAND\b", raw) if p.strip()]
    for p in parts:
        if p in current:
            return p, "release_text"
    if len(parts) == 1 and TICKER.fullmatch(parts[0]):
        return parts[0], "release_text"
    if current:
        return current[0], "sec_current"
    return "", "missing"

# ---------- build ----------

if __name__ == "__main__":
    ev = pd.read_csv(EVENTS / "extracted.csv", dtype=str).fillna("")
    sess = sessions()
    n_all = len(ev)
    ev = ev[ev["previously_announced"].str.lower() != "true"]
    n_flagged = n_all - len(ev)

    ciks = sorted(ev["cik"].unique())
    print(f"looking up {len(ciks)} companies at SEC (cached after the first run)...")
    info = {}
    for i, cik in enumerate(ciks, 1):
        info[cik] = (*acceptance_times(cik), shares_history(cik))
        if i % 100 == 0:
            print(f"   {i}/{len(ciks)}")

    rows, excluded = [], []
    start = sess[0]                                              # first day we have prices
    for r in ev.itertuples():
        times, current_tickers, shares = info[r.cik]
        accepted = times.get(r.adsh, "")
        acc_dt = (pd.Timestamp(accepted).tz_convert("America/New_York").tz_localize(None).to_pydatetime()
                  if accepted else None)                         # SEC gives UTC ("Z"); convert to New York time
        rel = pd.to_datetime(r.release_date, errors="coerce")
        rel_d = None if pd.isna(rel) else rel.date()
        acc_d = acc_dt.date() if acc_dt else None

        reason = ""
        if acc_d is None and rel_d is None:
            reason = "no date"
        elif acc_d and rel_d and rel_d > acc_d:
            reason = "release date after SEC filing (contradiction)"
        elif acc_d and rel_d and (acc_d - rel_d).days > MAX_FILING_LAG_DAYS:
            reason = f"8-K filed more than {MAX_FILING_LAG_DAYS} days after release (date unverified)"
        elif min(d for d in (acc_d, rel_d) if d) < start:
            reason = "before price data starts"
        if reason:
            excluded.append({"adsh": r.adsh, "company": r.company, "release_date": r.release_date,
                             "accepted": acc_dt.isoformat() if acc_dt else "", "reason": reason})
            continue

        if acc_d and (rel_d is None or rel_d == acc_d):
            ts, basis = acc_dt, "acceptance_time"                # filed the same day as the release
        else:
            ts, basis = datetime.combine(rel_d, dtime(23, 59)), "release_date_only"   # time unknown: assume after the close
        rday = reaction_day(ts, sess)
        if rday is None or rday > LAST_EVENT_DAY:
            continue

        ticker, t_src = pick_ticker(r.ticker, current_tickers)

        known = shares[pd.to_datetime(shares["filed"]) < pd.Timestamp(rday)]   # only numbers filed before the event
        last = known.sort_values(["filed", "end"]).iloc[-1] if len(known) else None

        rows.append({
            "adsh": r.adsh, "cik": r.cik, "company": r.company, "ticker": ticker, "ticker_source": t_src,
            "release_date": r.release_date, "accepted": acc_dt.isoformat() if acc_dt else "", "timing_basis": basis,
            "reaction_day": rday, "drug": r.drug, "phase": r.phase, "condition": r.condition,
            "trial_name": r.trial_name, "nct_ids": r.nct_ids,
            "shares_outstanding": last["val"] if last is not None else "",
            "shares_filed": last["filed"] if last is not None else "",
            "doc_url": f"https://www.sec.gov/Archives/edgar/data/{int(r.cik)}/{r.adsh.replace('-', '')}/{r.doc_name}",
        })

    df = pd.DataFrame(rows).sort_values("reaction_day")
    df["drug_key"] = df["drug"].str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
    keep, last_seen = [], {}
    for r in df.itertuples():
        key = (r.cik, r.drug_key) if r.drug_key else (r.cik, r.adsh)
        prev = last_seen.get(key)
        if prev is not None and (r.reaction_day - prev).days <= REPEAT_WINDOW_DAYS:
            continue                                   # same drug, same company, within 30 days: a repeat
        last_seen[key] = r.reaction_day
        keep.append(r.Index)
    final = df.loc[keep].drop(columns="drug_key")
    final.to_csv(EVENTS / "event_records.csv", index=False)
    pd.DataFrame(excluded).to_csv(EVENTS / "excluded_events.csv", index=False)

    print(f"\nevents in:                          {n_all}")
    print(f"  dropped, flagged as repeats:      {n_flagged}")
    print(f"  dropped, bad or unverifiable date: {len(excluded)}  -> data/events/excluded_events.csv")
    print(f"  dropped, after {LAST_EVENT_DAY}:     {len(ev) - len(df) - len(excluded)}")
    print(f"  dropped, same drug within {REPEAT_WINDOW_DAYS} days: {len(df) - len(final)}")
    print(f"event records:                      {len(final)}  -> data/events/event_records.csv")
    print(f"  timing from acceptance time:      {(final['timing_basis'] == 'acceptance_time').sum()}")
    print(f"  timing from release date only:    {(final['timing_basis'] == 'release_date_only').sum()}")
    print(f"  ticker source: {final['ticker_source'].value_counts().to_dict()}")
    print(f"  shares outstanding found:         {(final['shares_outstanding'] != '').mean():.0%}")