"""Check every trial match: does the matched ClinicalTrials.gov entry describe the trial whose results
the release reports? Gemini answers yes / no / unsure. Rule (Final rules in HYPOTHESIS.md): a match
answered "no" is marked not confident, and the event uses its text-only spin score.
Answers cached in labels/match_checks.json; summary in data/events/match_checks.csv."""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from google.genai import types
from pydantic import BaseModel

from score_spin import DOC_DIR, EVENTS, MODEL, ROOT, client, release_body

CHECKS = ROOT / "labels" / "match_checks.json"
PROMPT = """Does the ClinicalTrials.gov entry below describe the same clinical trial whose results this press
release reports? Compare the DRUG and the DISEASE in the entry with the drug and disease whose results the
release reports. Answer "no" if the entry's drug or disease is different from the one whose results are
reported, even if the release prints this entry's trial ID (IDs printed in releases are sometimes wrong).
Differences in sample size, enrollment, design details or wording are NOT reasons to answer "no".
Answer "yes" if it is the same drug and disease and plausibly the same study (or a part or cohort of it);
"unsure" if the release does not give enough detail. Give a one-sentence reason."""


class Check(BaseModel):
    same_trial: str
    reason: str


CONFIG = types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", response_schema=Check,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def check(ev):
    reg = (f"{ev.nct}: {ev.trial_title}" + (f" ({ev.acronym})" if ev.acronym else "")
           + f"\nPhase: {ev.registry_phase}\nPrimary endpoint(s): "
           + "; ".join(json.loads(ev.primary_outcomes or "[]")))
    text = release_body((DOC_DIR / f"{ev.adsh}.txt").read_text(encoding="utf-8", errors="replace"), 5000)
    contents = f"{PROMPT}\n\nCLINICALTRIALS.GOV ENTRY:\n{reg}\n\nPRESS RELEASE:\n{text}"
    for attempt in range(6):
        try:
            p = client.models.generate_content(model=MODEL, contents=contents, config=CONFIG).parsed
            ans = p.same_trial.strip().lower()
            return {"same_trial": ans if ans in ("yes", "no", "unsure") else "unsure", "reason": p.reason}
        except Exception:
            time.sleep(2 ** attempt)
    return None


if __name__ == "__main__":
    evs = pd.read_csv(EVENTS / "matched_records.csv", dtype=str).fillna("")
    evs = evs[evs["nct"] != ""]
    done = json.loads(CHECKS.read_text()) if CHECKS.exists() else {}
    todo = [ev for ev in evs.itertuples() if ev.adsh not in done]
    print(f"{len(done)} already checked, {len(todo)} to go")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(check, ev): ev.adsh for ev in todo}
        for i, f in enumerate(as_completed(futures), 1):
            if f.result():
                done[futures[f]] = f.result()
            if i % 100 == 0 or i == len(futures):
                CHECKS.write_text(json.dumps(done, indent=0))
                print(f"{i}/{len(futures)}")

    out = pd.DataFrame([{"adsh": a, **v} for a, v in done.items()])
    out.to_csv(EVENTS / "match_checks.csv", index=False)
    print(f"\nmatches checked: {len(out)}")
    print(f"  same trial: {out['same_trial'].value_counts().to_dict()}")
    print("  'no' -> match marked not confident; the event uses its text-only spin score")