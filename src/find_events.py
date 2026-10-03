"""Find candidate trial-result press releases: SEC full-text search (2018+) and Massive's
clinical_trial_results tag (2022+). Gemini later decides which candidates are real trial results."""
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

START, END = "2018-05-01", "2026-09-02"          # same dates as HYPOTHESIS.md
BIOTECH_SICS = {"2834", "2836", "8731"}
PHRASES = ['"topline results"', '"top-line results"', '"topline data"', '"top-line data"', '"primary endpoint"']

SEC_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
CACHE = ROOT / "data" / "cache" / "sec_fts"
OUT_DIR = ROOT / "data" / "events"


# ---------- SEC full-text search ----------

def sec_search(phrase, start, end, offset):
    """One page (up to 100 hits) of SEC search results, saved to disk so reruns are free."""
    path = CACHE / f'{phrase.strip(chr(34)).replace(" ", "_")}_{start}_{offset}.json'
    if path.exists():
        return json.loads(path.read_text())
    params = {"q": phrase, "forms": "8-K", "dateRange": "custom",
              "startdt": start, "enddt": end, "from": offset}
    for attempt in range(5):
        r = requests.get(SEC_URL, params=params, headers=SEC_HEADERS, timeout=30)
        if r.status_code == 200:
            break
        wait = 2 ** attempt                      # 1, 2, 4, 8, 16 seconds
        print(f"   SEC error {r.status_code}, retrying in {wait}s")
        time.sleep(wait)
    r.raise_for_status()                         # still failing after 5 tries: stop
    time.sleep(0.15)  # SEC allows at most 10 requests per second
    data = r.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def find_sec():
    rows = []
    for month in pd.date_range(START, END, freq="MS"):
        start = month.strftime("%Y-%m-%d")
        end = min(month + pd.offsets.MonthEnd(0), pd.Timestamp(END)).strftime("%Y-%m-%d")
        for phrase in PHRASES:
            offset = 0
            while True:
                data = sec_search(phrase, start, end, offset)
                hits = data["hits"]["hits"]
                for h in hits:
                    s = h["_source"]
                    if not BIOTECH_SICS & set(s.get("sics") or []):
                        continue                       # not a biotech/pharma company
                    if not s.get("file_type", "").startswith("EX-99"):
                        continue                       # not the press release attachment
                    rows.append({"cik": s["ciks"][0], "adsh": s["adsh"], "file_date": s["file_date"],
                                 "doc_name": h["_id"].split(":", 1)[1],
                                 "company": s["display_names"][0], "sic": ";".join(s["sics"]),
                                 "items": ";".join(s.get("items") or []), "phrase": phrase.strip('"')})
                offset += len(hits)
                if not hits or offset >= data["hits"]["total"]["value"]:
                    break
        print(f"SEC {start[:7]}: {len(rows)} hits so far")
    df = pd.DataFrame(rows)
    # the same press release matches several phrases: keep one row, list the phrases
    df = (df.groupby(["cik", "adsh", "doc_name"], as_index=False)
            .agg(file_date=("file_date", "first"), company=("company", "first"),
                 sic=("sic", "first"), items=("items", "first"),
                 phrases=("phrase", lambda p: ";".join(sorted(set(p))))))
    return df.sort_values("file_date")


# ---------- Massive clinical_trial_results (2022+) ----------

def find_massive():
    headers = {"Authorization": f"Bearer {os.environ['MASSIVE_API_KEY']}"}
    url = "https://api.massive.com/stocks/filings/8-K/vX/disclosures"
    params = {"tertiary_category": "clinical_trial_results", "filing_date.gte": "2022-01-01",
              "filing_date.lte": END, "limit": 1000, "sort": "filing_date.asc"}
    rows = []
    while url:
        r = requests.get(url, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()
        for it in data.get("results", []):
            rows.append({"cik": it.get("cik"), "accession": it.get("accession_number"),
                         "filing_date": it.get("filing_date"),
                         "tickers": ";".join(it.get("tickers") or []),
                         "supporting_text": (it.get("supporting_text") or "")[:300],
                         "filing_url": it.get("filing_url")})
        url, params = data.get("next_url"), None   # next_url already contains the query
        print(f"Massive: {len(rows)} events so far")
    return pd.DataFrame(rows)


# ---------- run ----------

if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sec = find_sec()
    sec.to_csv(OUT_DIR / "sec_candidates.csv", index=False)
    print(f"\nSEC: {len(sec)} candidate press releases in {sec['adsh'].nunique()} filings")

    massive = find_massive()
    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    massive.to_csv(raw / "massive_trials.csv", index=False)  # licensed data: stays out of git
    print(f"Massive: {len(massive)} clinical_trial_results events since 2022")

    norm = lambda a: str(a).replace("-", "")
    sec_22 = set(sec.loc[sec["file_date"] >= "2022-01-01", "adsh"].map(norm))
    mas = set(massive["accession"].map(norm))
    print(f"2022+ overlap: SEC {len(sec_22)} filings, Massive {len(mas)}, in both {len(sec_22 & mas)}")