"""How many biotech trial-result filings tagged by Massive did our SEC search miss?"""
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
BIOTECH_SICS = {"2834", "2836", "8731"}
SIC_CACHE = ROOT / "data" / "cache" / "sic_by_cik.json"


def sic_lookup(ciks):
    """Industry code (SIC) for each company, from SEC. Saved to disk so it only runs once."""
    SIC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(SIC_CACHE.read_text()) if SIC_CACHE.exists() else {}
    todo = [c for c in ciks if c not in cache]
    print(f"looking up industry codes for {len(todo)} companies...")
    for i, cik in enumerate(todo, 1):
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=HEADERS, timeout=30)
        cache[cik] = r.json().get("sic", "") if r.status_code == 200 else ""
        time.sleep(0.15)  # SEC allows at most 10 requests per second
        if i % 100 == 0:
            print(f"   {i}/{len(todo)}")
            SIC_CACHE.write_text(json.dumps(cache))
    SIC_CACHE.write_text(json.dumps(cache))
    return cache


if __name__ == "__main__":
    norm = lambda a: str(a).replace("-", "")
    sec = pd.read_csv(ROOT / "data" / "events" / "sec_candidates.csv", dtype=str)
    mas = pd.read_csv(ROOT / "data" / "raw" / "massive_trials.csv", dtype=str)

    mas["cik"] = mas["cik"].str.zfill(10)
    mas["acc"] = mas["accession"].map(norm)
    mas = mas.drop_duplicates("acc")

    sics = sic_lookup(sorted(mas["cik"].unique()))
    mas["sic"] = mas["cik"].map(sics)
    bio = mas[mas["sic"].isin(BIOTECH_SICS)].copy()

    bio["found_by_sec"] = bio["acc"].isin(set(sec["adsh"].map(norm)))
    n, hit = len(bio), int(bio["found_by_sec"].sum())
    print(f"\nMassive trial-result filings: {len(mas)}")
    print(f"  biotech (SIC 2834/2836/8731): {n}")
    print(f"  found by our SEC search:      {hit} ({hit / n:.0%})")
    print(f"  missed:                       {n - hit}")

    missed = bio[~bio["found_by_sec"]]
    missed.to_csv(ROOT / "data" / "raw" / "massive_missed_biotech.csv", index=False)
    print("\n10 random misses (date, ticker, what Massive saw):")
    for r in missed.sample(min(10, len(missed)), random_state=1).itertuples():
        print(f"  {r.filing_date}  {str(r.tickers):8}  {str(r.supporting_text)[:120]}")