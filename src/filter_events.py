"""Gemini pass 1: which candidate filings actually report human clinical-trial results?"""
import argparse
import json
import os
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
VERDICTS = ROOT / "labels" / "filter_verdicts.json"
BATCH = 25                                            # press releases per Gemini request
ITEM_MARKERS = ["Item 8.01", "Item 7.01", "Item 2.02", "Item 1.01"]
EARNINGS = re.compile(r"financial results|(first|second|third|fourth) quarter|full[- ]year|fiscal year", re.I)

PROMPT = """You will see the opening text of several press releases from biotech companies.
For each one, answer: is the MAIN PURPOSE of this release to announce NEW results or data
from a HUMAN clinical trial (efficacy or safety results, including topline, interim, or data
presented at a conference)? Judge mainly by the headline.

Answer false for:
- quarterly or annual financial results, and "corporate update" or "business update" releases,
  even if they mention trial data (those usually repeat results announced earlier)
- trial starts or enrollment updates
- animal or lab (preclinical) studies
- regulatory filings or FDA decisions without new trial data
- financings, investor presentations, and announcements of upcoming presentations without data

Return one object per press release with its id."""


class Verdict(BaseModel):
    id: int
    is_trial_result: bool


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
CONFIG = types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", response_schema=list[Verdict],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def snippet(adsh, n=1200):
    """The first ~1,200 characters of the release (headline + first paragraph)."""
    text = (DOC_DIR / f"{adsh}.txt").read_text(encoding="utf-8", errors="replace")
    if "SECURITIES AND EXCHANGE COMMISSION" in text[:300].upper():   # 8-K form: skip the cover page
        positions = [text.find(m) for m in ITEM_MARKERS if text.find(m) >= 0]
        if positions:
            text = text[min(positions):]
    return text[:n]


def classify(batch):
    """Send one batch to Gemini. Returns {filing: True/False}, input tokens, output tokens."""
    contents = PROMPT + "".join(f"\n\n--- id {i} ---\n{snippet(a)}" for i, a in enumerate(batch))
    for attempt in range(6):
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
            verdicts = {v.id: v.is_trial_result for v in resp.parsed}
            u = resp.usage_metadata
            return ({a: verdicts.get(i) for i, a in enumerate(batch)},
                    u.prompt_token_count or 0, u.candidates_token_count or 0)
        except Exception:
            time.sleep(2 ** attempt)                  # rate limit or server error: wait, retry
    return ({a: None for a in batch}, 0, 0)           # gave up: left unclassified, retried next run


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only classify this many filings (cost test)")
    args = ap.parse_args()

    cands = pd.read_csv(ROOT / "data" / "events" / "sec_candidates.csv", dtype=str)
    filings = cands.drop_duplicates("adsh")
    done = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
    todo = [a for a in filings["adsh"] if a not in done and (DOC_DIR / f"{a}.txt").exists()]
    skip = {a for a in todo if EARNINGS.search(snippet(a)[:400])}
    done.update({a: False for a in skip})            # earnings releases: excluded by rule, no Gemini needed
    todo = [a for a in todo if a not in skip]
    print(f"{len(skip)} earnings/annual-results releases excluded by rule")
    if args.limit:
        import random
        random.seed(1)
        todo = random.sample(todo, args.limit)   # random filings from all years, not just the first month
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"{len(done)} already classified, {len(todo)} to go in {len(batches)} batches")

    tok_in = tok_out = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(classify, b) for b in batches]
        for i, f in enumerate(as_completed(futures), 1):
            result, t_in, t_out = f.result()
            done.update({a: v for a, v in result.items() if v is not None})
            tok_in += t_in
            tok_out += t_out
            if i % 20 == 0 or i == len(batches):
                VERDICTS.parent.mkdir(parents=True, exist_ok=True)
                VERDICTS.write_text(json.dumps(done, indent=0))
                print(f"{i}/{len(batches)} batches   tokens in {tok_in:,}  out {tok_out:,}")

    n = max(len(todo), 1)
    print(f"\ntokens per filing: in {tok_in / n:.0f}, out {tok_out / n:.0f}")
    print(f"projected for all {len(filings)} filings: "
          f"in {tok_in / n * len(filings):,.0f}, out {tok_out / n * len(filings):,.0f}")

    yes = {a for a, v in done.items() if v}
    filings[filings["adsh"].isin(yes)].to_csv(ROOT / "data" / "events" / "trial_events.csv", index=False)
    print(f"classified {len(done)}; trial results: {len(yes)}  -> data/events/trial_events.csv")