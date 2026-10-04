"""Spin scoring: Gemini compares each trial-results press release with the trial's registered
endpoints (ClinicalTrials.gov) and scores spin 0-4. Every release is also scored text-only.
    python src/score_spin.py --test       # hand-labeled test set: accuracy + pre-release vs current check
    python src/score_spin.py --limit 40   # small cost test on real events
    python src/score_spin.py              # all events
"""
import argparse
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
MODEL = "gemini-3.1-flash-lite"
DOC_DIR = ROOT / "data" / "raw" / "docs"
HL_DOC_DIR = ROOT / "data" / "raw" / "hand_label_docs"
EVENTS = ROOT / "data" / "events"
SCORES = ROOT / "labels" / "spin_scores.json"
SNAPSHOTS = EVENTS / "registry_snapshots.csv"   # AACT pre-release registry text (aact_extract.py)
LABELS = ROOT / "labels" / "hand_labels.csv"
TEST_IDS = ["HL001", "HL003", "HL006", "HL008", "HL010", "HL011", "HL013"]
ITEM_MARKERS = ["Item 8.01", "Item 7.01", "Item 2.02", "Item 1.01"]
CUT_MARKERS = ["forward-looking statement", "safe harbor", "cautionary note"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
labels = pd.read_csv(LABELS, dtype=str).fillna("")


# ---------- the prompt ----------

def few_shot_block():
    rows = labels[labels["include_in_few_shot"].str.lower() == "true"]
    return "\n\n".join(
        f"EXAMPLE (spin {r.spin_score}, primary endpoint met: {r.primary_endpoint_met})\n"
        f"Registered primary endpoint: {r.primary_endpoint_definition}\n"
        f"What the release claims: {r.release_claims}\n"
        f"What it actually reports: {r.reported_results}\n"
        f"Why this score: {r.spin_rationale}"
        for r in rows.itertuples())


INSTRUCTIONS = f"""You are rating SPIN in a biotech press release that announces clinical-trial results.
Spin = how far the release's message drifts from what the trial was registered to test and what it actually
found: leading with secondary results or subgroups, calling a non-significant result a trend or a success,
burying or relabeling a failed primary endpoint, or overstating benefit.
Company names, tickers, drug names, people and dates are hidden as [REDACTED], [DRUG], [PERSON], [DATE].
Judge ONLY from the ClinicalTrials.gov registry entry (if available) and the press release below. Compare what
the release claims against what the trial was registered to test. Do not use outside knowledge about the
company, the drug, or what happened later.

Scoring rubric:
{labels["scoring_rubric"].iloc[0]}

{few_shot_block()}

Answer with:
- primary_endpoint_met: "yes", "no", or "unclear" (did the trial meet its registered or stated primary endpoint, per this release?)
- spin_score: 0 to 4, using the rubric
- rationale: one or two sentences"""


class Spin(BaseModel):
    primary_endpoint_met: str
    spin_score: int
    rationale: str


CONFIG = types.GenerateContentConfig(
    temperature=0, response_mime_type="application/json", response_schema=Spin,
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def ask(release_text, registry_text):
    reg = registry_text or "Not available. Judge the release on its own: does it clearly and accurately state whether the main goal was met?"
    contents = INSTRUCTIONS + f"\n\nREGISTERED ON CLINICALTRIALS.GOV:\n{reg}\n\nPRESS RELEASE:\n{release_text}"
    for attempt in range(6):
        try:
            resp = client.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
            p, u = resp.parsed, resp.usage_metadata
            met = p.primary_endpoint_met.strip().lower()
            return ({"met": met if met in ("yes", "no", "unclear") else "unclear",
                     "spin": max(0, min(4, int(p.spin_score))), "rationale": p.rationale},
                    u.prompt_token_count or 0, u.candidates_token_count or 0)
        except Exception:
            time.sleep(2 ** attempt)
    return None, 0, 0


# ---------- text preparation ----------

def release_body(text, n=7000):
    """Press release text without the 8-K cover page and without the legal boilerplate at the end."""
    if "SECURITIES AND EXCHANGE COMMISSION" in text[:300].upper():
        pos = [text.find(m) for m in ITEM_MARKERS if text.find(m) >= 0]
        if pos:
            text = text[min(pos):]
    low = text.lower()
    cuts = [low.find(m, 300) for m in CUT_MARKERS if low.find(m, 300) >= 0]
    if cuts:
        text = text[:min(cuts)]
    return text[:n]


MONTHS = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
DATES = [re.compile(MONTHS + r"\s+\d{1,2}(st|nd|rd|th)?,?\s+\d{4}", re.I),
         re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), re.compile(r"\b(19|20)\d{2}\b")]
DRUG_CODE = re.compile(r"\b(?!COVID)[A-Z]{1,6}-?\d{2,6}[A-Za-z]?\b")       # VX-814, KT-621, BXCL501, NCT01234567
PERSON = re.compile(r"\b(Dr|Mr|Ms|Mrs|Prof)\.?\s+[A-Z][a-z]+(\s+[A-Z]\.)?(\s+[A-Z][a-z]+)?")
SAID = re.compile(r"\b(said|says|commented|stated|added)\s+[A-Z][a-z]+(\s+[A-Z]\.)?\s+[A-Z][a-z]+")
GENERIC_WORDS = {"applied", "advanced", "global", "american", "international", "united", "first", "general",
                 "therapeutics", "pharmaceuticals", "biosciences", "biotherapeutics", "medical", "health"}


def clean_company(name):
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"\b(inc|corp|corporation|ltd|plc|llc|holdings|co|company|n\.?v|s\.?a|ag)\b\.?", "", name, flags=re.I)
    return " ".join(name.replace(",", " ").split())


def redact(text, terms):
    if not text:
        return text
    for t in sorted({t.strip() for t in terms if t and len(t.strip()) > 2}, key=len, reverse=True):
        text = re.sub(re.escape(t), "[REDACTED]", text, flags=re.I)
    text = DRUG_CODE.sub("[DRUG]", text)
    text = PERSON.sub("[PERSON]", text)
    text = SAID.sub(lambda m: m.group(1) + " [PERSON]", text)
    for d in DATES:
        text = d.sub("[DATE]", text)
    return text


def event_terms(ev):
    co = clean_company(ev.company)
    first = co.split()[0] if co else ""
    terms = [co, ev.ticker, ev.trial_name, ev.acronym] + re.split(r"[;,/()]", ev.drug)
    if len(first) >= 5 and first.lower() not in GENERIC_WORDS:
        terms.append(first)
    return terms


def registry_text(primaries, secondaries, phase, allocation, masking):
    lines = ["Primary endpoint(s):"] + [f"- {p}" for p in primaries]
    if secondaries:
        lines += ["Secondary endpoints:"] + [f"- {s}" for s in secondaries[:10]]
    lines.append(f"Design: phase {phase or 'not stated'}; allocation {allocation or 'n/a'}; masking {masking or 'n/a'}")
    return "\n".join(lines)


def aact_lookup():
    """{trial ID: AACT snapshot rows, oldest first}, only rows that have a primary endpoint."""
    snaps = pd.read_csv(SNAPSHOTS, dtype=str).fillna("")
    snaps = snaps[snaps["primary_outcomes"] != "[]"].sort_values("snapshot")
    return {nct: list(g.itertuples()) for nct, g in snaps.groupby("nct")}


def snapshot_before(ev, lookup):
    """Latest AACT snapshot dated before the event = the registry text public at the time (or None)."""
    event_date = ev.accepted[:10] if ev.timing_basis == "acceptance_time" else ev.release_date
    rows = [s for s in lookup.get(ev.nct, []) if s.snapshot < event_date]
    return rows[-1] if rows else None
# ---------- all events ----------

def score_event(ev, mode, snap=None):
    text = release_body((DOC_DIR / f"{ev.adsh}.txt").read_text(encoding="utf-8", errors="replace"))
    terms = event_terms(ev)
    reg = None
    if mode == "registry":
        reg = registry_text(json.loads(ev.primary_outcomes or "[]"), json.loads(ev.secondary_outcomes or "[]"),
                            ev.registry_phase, ev.allocation, ev.masking)
    elif mode == "aact":
        reg = registry_text(json.loads(snap.primary_outcomes), json.loads(snap.secondary_outcomes),
                            snap.phase, snap.allocation, snap.masking)
    return ask(redact(text, terms), redact(reg, terms))


def run_events(limit):
    evs = pd.read_csv(EVENTS / "matched_records.csv", dtype=str).fillna("")
    if limit:
        evs = evs.sample(n=limit, random_state=3)
    scores = json.loads(SCORES.read_text()) if SCORES.exists() else {}
    lookup = aact_lookup()
    rejected = set()
    if (EVENTS / "match_checks.csv").exists():
        mc = pd.read_csv(EVENTS / "match_checks.csv", dtype=str)
        rejected = set(mc.loc[mc["same_trial"] == "no", "adsh"])   # wrong trial -> not confident -> text-only
    snap_of = {ev.adsh: snapshot_before(ev, lookup) for ev in evs.itertuples() if ev.nct}
    tasks = ([(ev, "text") for ev in evs.itertuples()] + [(ev, "registry") for ev in evs.itertuples() if ev.nct]
             + [(ev, "aact") for ev in evs.itertuples() if snap_of.get(ev.adsh) is not None])
    tasks = [(ev, m) for ev, m in tasks if f"{ev.adsh}|{m}" not in scores]
    print(f"{len(scores)} already scored, {len(tasks)} to go")

    tok_in = tok_out = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(score_event, ev, m, snap_of.get(ev.adsh)): (ev.adsh, m) for ev, m in tasks}
        for i, f in enumerate(as_completed(futures), 1):
            res, t_in, t_out = f.result()
            tok_in, tok_out = tok_in + t_in, tok_out + t_out
            if res:
                adsh, m = futures[f]
                scores[f"{adsh}|{m}"] = res
            if i % 50 == 0 or i == len(futures):
                SCORES.write_text(json.dumps(scores, indent=0))
                print(f"{i}/{len(futures)}   tokens in {tok_in:,}  out {tok_out:,}")

    rows = []
    for ev in evs.itertuples():
        t, r = scores.get(f"{ev.adsh}|text"), scores.get(f"{ev.adsh}|registry")
        a, s = scores.get(f"{ev.adsh}|aact"), snap_of.get(ev.adsh)
        used = t if ev.adsh in rejected else (a or t)
        rows.append({"adsh": ev.adsh, "nct": ev.nct, "registry_source": ev.registry_source,
                     "spin_text": t["spin"] if t else "", "met_text": t["met"] if t else "",
                     "spin_registry": r["spin"] if r else "", "met_registry": r["met"] if r else "",
                     "rationale_registry": r["rationale"] if r else "", "rationale_text": t["rationale"] if t else "",
                     "aact_snapshot": s.snapshot if a and s is not None else "",
                     "spin_aact": a["spin"] if a else "", "met_aact": a["met"] if a else "",
                     "rationale_aact": a["rationale"] if a else "",
                     "spin_used": used["spin"] if used else "", "met_used": used["met"] if used else "",
                     "spin_source": ("text, match rejected" if ev.adsh in rejected and t else "aact" if a else ("text" if t else ""))})
    out = evs.merge(pd.DataFrame(rows), on=["adsh", "nct", "registry_source"], how="left")
    out.to_csv(EVENTS / ("spin_scores_test.csv" if limit else "spin_scores.csv"), index=False)

    both = out[(out["spin_registry"] != "") & (out["spin_text"] != "")]
    print(f"\nevents: {len(out)}   scored text-only: {(out['spin_text'] != '').sum()}   scored vs registry: {(out['spin_registry'] != '').sum()}")
    print(f"  registry spin scores: {out['spin_registry'].astype(str).value_counts().sort_index().to_dict()}")
    print(f"  text-only spin scores: {out['spin_text'].astype(str).value_counts().sort_index().to_dict()}")
    print(f"  endpoint met (registry): {out['met_registry'].value_counts().to_dict()}")
    if len(both):
        diff = (both["spin_registry"].astype(int) - both["spin_text"].astype(int)).abs()
        print(f"  registry vs text-only: same score {(diff == 0).mean():.0%}, within 1 point {(diff <= 1).mean():.0%}")
        print(f"  AACT pre-release spin scores: {out['spin_aact'].astype(str).value_counts().sort_index().to_dict()}")
        print(f"  endpoint met (AACT): {out['met_aact'].value_counts().to_dict()}")
        print(f"  spin used for trading comes from: {out['spin_source'].value_counts().to_dict()}")
        pair = out[(out["spin_aact"] != "") & (out["spin_registry"] != "")]
        if len(pair):
            d = (pair["spin_aact"].astype(int) - pair["spin_registry"].astype(int)).abs()
            print(f"  AACT vs today's record: same score {(d == 0).mean():.0%}, within 1 point {(d <= 1).mean():.0%}")
    if limit:
        print(f"  tokens per call: in {tok_in / max(len(tasks), 1):.0f}, out {tok_out / max(len(tasks), 1):.0f}")


# ---------- hand-labeled test set ----------

def hand_label_text(row):
    path = HL_DOC_DIR / f"{row.example_id}.txt"
    if not path.exists():
        r = requests.get(row.source_url, headers={"User-Agent": os.environ["SEC_USER_AGENT"]}, timeout=30)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(" ".join(BeautifulSoup(r.content, "html.parser").get_text(" ").split()), encoding="utf-8")
    return path.read_text(encoding="utf-8")


def current_primary(nct):
    r = requests.get(f"https://clinicaltrials.gov/api/v2/studies/{nct}", timeout=30)
    outs = r.json().get("protocolSection", {}).get("outcomesModule", {}).get("primaryOutcomes", [])
    return [f'{o.get("measure", "")} [{o.get("timeFrame", "")}]' for o in outs]


def run_test():
    rows = labels[labels["example_id"].isin(TEST_IDS)]
    out = []
    for r in rows.itertuples():
        terms = [clean_company(r.company)] + ([r.trial] if len(r.trial.split()) <= 2 else [])
        text = redact(release_body(hand_label_text(r)), terms)
        a = b = None
        if r.nct_id:
            a, _, _ = ask(text, redact("Primary endpoint(s):\n- " + r.primary_endpoint_definition, terms))
            b, _, _ = ask(text, redact("Primary endpoint(s):\n" + "\n".join("- " + p for p in current_primary(r.nct_id)), terms))
        c, _, _ = ask(text, None)
        out.append({"id": r.example_id, "company": r.company, "hand_spin": int(r.spin_score), "hand_met": r.primary_endpoint_met,
                    "A_prerelease": a["spin"] if a else "", "A_met": a["met"] if a else "",
                    "B_current": b["spin"] if b else "", "C_text_only": c["spin"] if c else ""})
    df = pd.DataFrame(out)
    df.to_csv(ROOT / "labels" / "spin_test_results.csv", index=False)
    print(df.to_string(index=False))

    reg = df[df["A_prerelease"] != ""]
    a, b, h = reg["A_prerelease"].astype(int), reg["B_current"].astype(int), reg["hand_spin"]
    c = df["C_text_only"].astype(int)
    print(f"\nA (pre-release registry) vs hand label: exact {(a == h).mean():.0%}, within 1 point {((a - h).abs() <= 1).mean():.0%}")
    print(f"A endpoint-met vs hand label:           {(reg['A_met'] == reg['hand_met']).mean():.0%}")
    print(f"A vs B (pre-release vs current record): same score {(a == b).mean():.0%}, within 1 point {((a - b).abs() <= 1).mean():.0%}")
    print(f"C (text-only) vs hand label:            exact {(c == df['hand_spin']).mean():.0%}, within 1 point {((c - df['hand_spin']).abs() <= 1).mean():.0%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="score the hand-labeled test set")
    ap.add_argument("--limit", type=int, help="score only this many events (cost test)")
    args = ap.parse_args()
    run_test() if args.test else run_events(args.limit)