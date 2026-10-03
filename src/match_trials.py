"""Event records, part 3: match each event to its ClinicalTrials.gov trial and take the registered
endpoints from the registry version that existed BEFORE the release."""
import argparse
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MODEL = "gemini-3.1-flash-lite"
CT = "https://clinicaltrials.gov/api"
CACHE = ROOT / "data" / "cache" / "ctgov"
DOC_DIR = ROOT / "data" / "raw" / "docs"
EVENTS = ROOT / "data" / "events"
PICKS = ROOT / "labels" / "trial_matches.json"
ITEM_MARKERS = ["Item 8.01", "Item 7.01", "Item 2.02", "Item 1.01"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


# ---------- ClinicalTrials.gov requests (cached, at most 5 per second) ----------

_lock = threading.Lock()
_last = [0.0]


def throttle(gap=0.2):
    with _lock:
        wait = _last[0] + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def ct_get(path, params=None):
    key = path + json.dumps(params or {}, sort_keys=True)
    file = CACHE / (hashlib.md5(key.encode()).hexdigest() + ".json")
    if file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    for attempt in range(5):
        throttle()
        try:
            r = requests.get(CT + path, params=params, timeout=30)
            if r.status_code == 200:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(r.text, encoding="utf-8")
                return r.json()
            if r.status_code in (400, 404):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None


def summarize(study):
    """The registry facts we use, from one study record."""
    p = (study or {}).get("protocolSection", {})
    ident, design = p.get("identificationModule", {}), p.get("designModule", {})
    status, out = p.get("statusModule", {}), p.get("outcomesModule", {})
    return {
        "nct": ident.get("nctId", ""),
        "title": ident.get("briefTitle", ""),
        "acronym": ident.get("acronym", ""),
        "sponsor": p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
        "phase": ",".join(design.get("phases", [])),
        "allocation": design.get("designInfo", {}).get("allocation", ""),
        "masking": design.get("designInfo", {}).get("maskingInfo", {}).get("masking", ""),
        "enrollment": design.get("enrollmentInfo", {}).get("count", ""),
        "interventions": "; ".join(i.get("name", "") for i in p.get("armsInterventionsModule", {}).get("interventions", [])),
        "conditions": "; ".join(p.get("conditionsModule", {}).get("conditions", [])),
        "primary_completion": status.get("primaryCompletionDateStruct", {}).get("date", ""),
        "primary_outcomes": [f'{o.get("measure", "")} [{o.get("timeFrame", "")}]' for o in out.get("primaryOutcomes", [])],
        "secondary_outcomes": [o.get("measure", "") for o in out.get("secondaryOutcomes", [])][:15],
    }


# ---------- step 1: candidate trials ----------

def clean_company(name):
    """'SPRUCE BIOSCIENCES, INC. (SPRB) (CIK 0001683553)' -> 'SPRUCE BIOSCIENCES'"""
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"\b(inc|corp|corporation|ltd|plc|llc|holdings|n\.?v|s\.?a|ag)\b\.?", "", name, flags=re.I)
    return " ".join(name.replace(",", " ").split())


def candidates(ev):
    ncts = [n for n in ev.nct_ids.split(";") if n]
    if ncts:
        studies = [ct_get(f"/v2/studies/{n}") for n in ncts[:8]]
        return [summarize(s) for s in studies if s], "nct_in_text"
    params = {"query.spons": clean_company(ev.company), "pageSize": 10}
    if ev.drug:
        params["query.intr"] = ev.drug
    studies = (ct_get("/v2/studies", params) or {}).get("studies", [])
    if not studies and ev.drug:
        params = {"query.term": f"{ev.drug} {ev.condition}".strip(), "pageSize": 10}
        studies = (ct_get("/v2/studies", params) or {}).get("studies", [])
    return [summarize(s) for s in studies], "search"


# ---------- step 2: Gemini picks the right trial ----------

class Pick(BaseModel):
    nct: str
    confident: bool


PICK_PROMPT = """Below is the opening of a biotech press release announcing clinical-trial results,
followed by candidate trials from ClinicalTrials.gov. Which candidate is the trial whose results
the release announces? Answer with its NCT number, or "none" if no candidate matches.
Set confident to false if you are unsure."""

PICK_CONFIG = types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", response_schema=Pick,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def release_opening(adsh, n=1500):
    text = (DOC_DIR / f"{adsh}.txt").read_text(encoding="utf-8", errors="replace")
    if "SECURITIES AND EXCHANGE COMMISSION" in text[:300].upper():
        pos = [text.find(m) for m in ITEM_MARKERS if text.find(m) >= 0]
        if pos:
            text = text[min(pos):]
    return text[:n]


def pick(ev, cands):
    lines = [f"{c['nct']} | {c['title']} | acronym: {c['acronym']} | phase: {c['phase']} | "
             f"sponsor: {c['sponsor']} | drugs: {c['interventions'][:150]} | "
             f"conditions: {c['conditions'][:150]} | primary completion: {c['primary_completion']}"
             for c in cands]
    contents = (PICK_PROMPT + f"\n\nPRESS RELEASE (dated {ev.release_date}):\n{release_opening(ev.adsh)}"
                + "\n\nCANDIDATES:\n" + "\n".join(lines))
    for attempt in range(6):
        try:
            p = client.models.generate_content(model=MODEL, contents=contents, config=PICK_CONFIG).parsed
            nct = p.nct.strip().upper()
            return (nct if nct in {c["nct"] for c in cands} else "none"), bool(p.confident)
        except Exception:
            time.sleep(2 ** attempt)
    return None, False


# ---------- step 3: registry version from before the release ----------

def version_before(nct, cutoff):
    """The history feed blocks scripts (403), so use the current record and check whether it was
    last updated before the release. If so, the current record IS the pre-release version."""
    study = ct_get(f"/v2/studies/{nct}")
    if not study:
        return None, "not_found", ""
    last = study.get("protocolSection", {}).get("statusModule", {}).get("lastUpdatePostDateStruct", {}).get("date", "")
    return study, ("unchanged_since_release" if last and last < cutoff else "updated_after_release"), last

# ---------- one event ----------

def process(ev, saved_pick):
    rec = {"adsh": ev.adsh, "nct": "", "match_method": "", "confident": "", "registry_source": "",
           "registry_version": "", "registry_version_date": ""}
    cands, method = candidates(ev)
    new_pick = None
    if not cands:
        rec["match_method"] = "no_candidates"
        return rec, new_pick
    if method == "nct_in_text" and len(cands) == 1:
        nct, conf = cands[0]["nct"], True
        rec["match_method"] = "nct_in_text_single"
    else:
        if saved_pick:
            nct, conf = saved_pick["nct"], saved_pick["confident"]
        else:
            nct, conf = pick(ev, cands)
            if nct is None:
                rec["match_method"] = "gemini_failed"
                return rec, new_pick
            new_pick = {"nct": nct, "confident": conf}
        rec["match_method"] = f"gemini_pick_{method}"
    rec["confident"] = conf
    if nct == "none":
        return rec, new_pick

    cutoff = ev.release_date or ev.reaction_day
    study, source, vdate = version_before(nct, cutoff)
    ver = ""
    s = summarize(study)
    rec.update({"nct": nct, "registry_source": source, "registry_version": ver,
                "registry_version_date": vdate, "trial_title": s["title"], "acronym": s["acronym"],
                "registry_phase": s["phase"], "allocation": s["allocation"], "masking": s["masking"],
                "enrollment": s["enrollment"], "primary_outcomes": json.dumps(s["primary_outcomes"]),
                "secondary_outcomes": json.dumps(s["secondary_outcomes"])})
    return rec, new_pick


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only process this many events (test)")
    args = ap.parse_args()

    ev = pd.read_csv(EVENTS / "event_records.csv", dtype=str).fillna("")
    if args.limit:
        ev = ev.sample(n=args.limit, random_state=1)
    picks = json.loads(PICKS.read_text()) if PICKS.exists() else {}

    records = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(process, r, picks.get(r.adsh)) for r in ev.itertuples()]
        for i, f in enumerate(as_completed(futures), 1):
            rec, new_pick = f.result()
            records.append(rec)
            if new_pick:
                picks[rec["adsh"]] = new_pick
            if i % 100 == 0 or i == len(futures):
                PICKS.write_text(json.dumps(picks, indent=0))
                print(f"{i}/{len(futures)} events")

    out = ev.merge(pd.DataFrame(records), on="adsh", how="left")
    out.to_csv(EVENTS / ("matched_records_test.csv" if args.limit else "matched_records.csv"), index=False)

    matched = out[out["nct"] != ""]
    print(f"\nevents: {len(out)}")
    print(f"  matched to a trial:           {len(matched)} ({len(matched) / len(out):.0%})")
    print(f"  match method: {out['match_method'].value_counts().to_dict()}")
    print(f"  Gemini unsure:                {(matched['confident'].astype(str) == 'False').sum()}")
    print(f"  registry: {matched['registry_source'].value_counts().to_dict()}")
    print("\n5 random matches to check by eye:")
    for r in matched.sample(min(5, len(matched)), random_state=2).itertuples():
        print(f"  {r.company[:30]:30} drug: {r.drug[:25]:25} -> {r.nct} {str(r.trial_title)[:60]}")