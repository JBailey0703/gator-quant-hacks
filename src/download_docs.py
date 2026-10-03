"""Download the press release text for every candidate filing (one document per filing)."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
DOC_DIR = ROOT / "data" / "raw" / "docs"

_lock = threading.Lock()
_last = [0.0]


def throttle(min_gap=0.11):
    """Keeps all threads together under SEC's limit of 10 requests per second."""
    with _lock:
        wait = _last[0] + min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def pick_documents(cands):
    """One document per filing: prefer the EX-99.1 press release over the 8-K form text."""
    cands = cands.copy()
    cands["is_ex99"] = cands["doc_name"].str.lower().str.contains("99")
    cands = cands.sort_values(["adsh", "is_ex99", "doc_name"], ascending=[True, False, True])
    return cands.drop_duplicates("adsh")


def fetch(row):
    path = DOC_DIR / f"{row.adsh}.txt"
    if path.exists():
        return "cached"
    url = f"https://www.sec.gov/Archives/edgar/data/{int(row.cik)}/{row.adsh.replace('-', '')}/{row.doc_name}"
    for attempt in range(5):
        throttle()
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                break
            status = r.status_code
        except requests.RequestException as e:
            status = type(e).__name__
        time.sleep(2 ** attempt)
    else:
        return f"failed {status}"
    text = BeautifulSoup(r.content, "html.parser").get_text(" ")
    path.write_text(" ".join(text.split()), encoding="utf-8")  # plain text, extra spaces removed
    return "saved"


if __name__ == "__main__":
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    cands = pd.read_csv(ROOT / "data" / "events" / "sec_candidates.csv", dtype=str)
    docs = pick_documents(cands)
    print(f"{len(docs)} filings to download")
    counts = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, row) for row in docs.itertuples()]
        for i, f in enumerate(as_completed(futures), 1):
            status = f.result().split()[0]
            counts[status] = counts.get(status, 0) + 1
            if i % 500 == 0:
                print(f"{i}/{len(docs)}  {counts}")
    print("done:", counts)