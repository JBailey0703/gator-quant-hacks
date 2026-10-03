"""Event records, part 1: extract announcement facts from each trial-result release with Gemini
(dateline date, ticker, drug, phase, condition, trial name, repeat flag), plus NCT IDs by regex."""
import argparse
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MODEL = "gemini-3.1-flash-lite"
DOC_DIR = ROOT / "data" / "raw" / "docs"
OUT_JSON = ROOT / "labels" / "extracted.json"
BATCH = 5                 # releases per Gemini request
CHARS = 4000              # characters of each release sent to Gemini
NCT = re.compile(r"NCT\d{8}")
ITEM_MARKERS = ["Item 8.01", "Item 7.01", "Item 2.02", "Item 1.01"]

PROMPT = """Each item below is the opening of a biotech press release announcing clinical-trial results.
For each item, extract these facts exactly as stated in the text. Use "" if not stated.
- release_date: the date in the dateline (e.g. "SAN DIEGO, May 2, 2018 --"), as YYYY-MM-DD
- exchange: the exchange of the company's stock, e.g. "Nasdaq", "NYSE", "NYSE American"
- ticker: the company's stock ticker as written in the release
- drug: the drug or product name or code the results are about
- phase: the trial phase, e.g. "1", "1b", "2", "2a", "3"
- condition: the disease or condition studied
- trial_name: the trial's name or acronym, if given
- previously_announced: true if the release says these results were already announced before
  (an update, repeat, or fuller presentation of earlier topline results), else false
Return one object per item with its id."""


class Facts(BaseModel):
    id: int
    release_date: str
    exchange: str
    ticker: str
    drug: str
    phase: str
    condition: str
    trial_name: str
    previously_announced: bool


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
CONFIG = types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", response_schema=list[Facts],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def read_text(adsh):
    return (DOC_DIR / f"{adsh}.txt").read_text(encoding="utf-8", errors="replace")


def opening(text):
    """Start of the release. For 8-K forms: the ticker table on the cover page + the Item text."""
    if "SECURITIES AND EXCHANGE COMMISSION" not in text[:300].upper():
        return text[:CHARS]
    sym = text.find("Trading Symbol")
    cover = text[sym:sym + 400] if sym >= 0 else ""
    pos = [text.find(m) for m in ITEM_MARKERS if text.find(m) >= 0]
    body = text[min(pos):] if pos else text
    return (cover + " ... " + body)[:CHARS]


def extract(batch):
    contents = PROMPT + "".join(f"\n\n--- id {i} ---\n{opening(read_text(a))}" for i, a in enumerate(batch))
    for attempt in range(6):
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
            facts = {f.id: f.model_dump(exclude={"id"}) for f in resp.parsed}
            u = resp.usage_metadata
            return ({a: facts.get(i) for i, a in enumerate(batch)},
                    u.prompt_token_count or 0, u.candidates_token_count or 0)
        except Exception:
            time.sleep(2 ** attempt)
    return ({a: None for a in batch}, 0, 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only process this many releases (test)")
    args = ap.parse_args()

    events = pd.read_csv(ROOT / "data" / "events" / "trial_events.csv", dtype=str)
    done = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
    todo = [a for a in events["adsh"] if a not in done]
    if args.limit:
        random.seed(1)
        todo = random.sample(todo, min(args.limit, len(todo)))
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"{len(done)} already done, {len(todo)} to go in {len(batches)} batches")

    tok_in = tok_out = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(extract, b) for b in batches]
        for i, f in enumerate(as_completed(futures), 1):
            result, t_in, t_out = f.result()
            done.update({a: v for a, v in result.items() if v is not None})
            tok_in += t_in
            tok_out += t_out
            if i % 20 == 0 or i == len(batches):
                OUT_JSON.write_text(json.dumps(done, indent=0))
                print(f"{i}/{len(batches)} batches   tokens in {tok_in:,}  out {tok_out:,}")

    rows = []
    for r in events.itertuples():
        f = done.get(r.adsh)
        if f:
            nct = ";".join(sorted(set(NCT.findall(read_text(r.adsh)))))   # trial IDs anywhere in the text
            rows.append({"adsh": r.adsh, "cik": r.cik, "file_date": r.file_date, "company": r.company,
                         "doc_name": r.doc_name, **f, "nct_ids": nct})
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "data" / "events" / "extracted.csv", index=False)

    lag = (pd.to_datetime(df["file_date"]) - pd.to_datetime(df["release_date"], errors="coerce")).dt.days
    print(f"\nevents extracted: {len(df)}")
    print(f"  ticker found:          {(df['ticker'] != '').mean():.0%}")
    print(f"  NCT ID found:          {(df['nct_ids'] != '').mean():.0%}")
    print(f"  previously announced:  {df['previously_announced'].mean():.0%}")
    print(f"  filed 1 day after release date: {(lag == 1).sum()},  2+ days after: {(lag >= 2).sum()}")