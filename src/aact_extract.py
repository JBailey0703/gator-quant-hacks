"""Pre-release registry text from AACT monthly snapshots (CTTI; public ClinicalTrials.gov data).
Each snapshot zip holds a PostgreSQL dump; pg_restore pulls out three tables (no database needed).
Only our matched trials are kept, saved to data/events/registry_snapshots.csv.
--delete removes each zip after it is processed."""
import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "data" / "events"
OUT = EVENTS / "registry_snapshots.csv"
TMP = ROOT / "data" / "raw" / "aact_tmp.dmp"
PG_RESTORE = r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe"
TABLES = {                                # only the columns we use (never status, results, enrollment)
    "studies": ["nct_id", "study_first_posted_date", "phase"],
    "designs": ["nct_id", "allocation", "masking"],
    "design_outcomes": ["nct_id", "outcome_type", "measure", "time_frame"],
}
ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}


def unescape(v):
    """PostgreSQL text format: \\N = empty, backslash escapes for tabs/newlines."""
    return "" if v == r"\N" else re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), v)


def read_table(name, ncts):
    """One table from the dump, only our trials and the columns we use."""
    proc = subprocess.Popen([PG_RESTORE, "-a", "-t", name, "-f", "-", str(TMP)],
                            stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    cols, rows = None, []
    for line in proc.stdout:
        if cols is None:
            if line.startswith("COPY "):
                cols = line[line.index("(") + 1:line.index(")")].split(", ")
                keep = [cols.index(c) for c in TABLES[name]]
                nct_i = cols.index("nct_id")
            continue
        if line.startswith("\\."):
            break
        vals = line.rstrip("\n").split("\t")
        if vals[nct_i] in ncts:
            rows.append([unescape(vals[i]) for i in keep])
    proc.wait()
    if cols is None:
        raise RuntimeError(f"pg_restore could not read table {name} (exit code {proc.returncode})")
    return pd.DataFrame(rows, columns=TABLES[name])


def snapshot_rows(snap, ncts):
    studies = read_table("studies", ncts)
    designs = read_table("designs", ncts).drop_duplicates("nct_id").set_index("nct_id")
    outs = read_table("design_outcomes", ncts)
    outs["outcome_type"] = outs["outcome_type"].str.lower()
    by_trial = dict(tuple(outs.groupby("nct_id")))
    rows = []
    for s in studies.itertuples():
        o = by_trial.get(s.nct_id, outs.iloc[0:0])
        prim, sec = o[o["outcome_type"] == "primary"], o[o["outcome_type"] == "secondary"]
        d = designs.loc[s.nct_id] if s.nct_id in designs.index else {"allocation": "", "masking": ""}
        rows.append({
            "nct": s.nct_id, "snapshot": snap, "first_posted": s.study_first_posted_date, "phase": s.phase,
            "allocation": d["allocation"], "masking": d["masking"],
            "primary_outcomes": json.dumps([f"{m} [{t}]" for m, t in zip(prim["measure"], prim["time_frame"])]),
            "secondary_outcomes": json.dumps(list(sec["measure"])[:15]),
        })
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=str(Path.home() / "Downloads"), help="folder with the AACT zips")
    ap.add_argument("--delete", action="store_true", help="delete each zip after it is processed")
    args = ap.parse_args()

    m = pd.read_csv(EVENTS / "matched_records.csv", dtype=str).fillna("")
    ncts = set(m.loc[m["nct"] != "", "nct"])
    done = pd.read_csv(OUT, dtype=str) if OUT.exists() else pd.DataFrame(columns=["snapshot"])
    have = set(done["snapshot"])

    for z in sorted(Path(args.folder).glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            dump = next((i for i in zf.infolist() if i.filename in ("postgres_data.dmp", "postgres.dmp")), None)
            if dump is None:
                continue                                       # some other zip, not an AACT snapshot
            snap = "%04d-%02d-%02d" % dump.date_time[:3]       # snapshot date = when the dump was made
            if snap in have:
                print(f"{snap}: already processed")
            else:
                with zf.open(dump) as src, open(TMP, "wb") as dst:
                    while chunk := src.read(16 * 1024 * 1024):
                        dst.write(chunk)
                rows = snapshot_rows(snap, ncts)
                TMP.unlink()
                done = pd.concat([done, pd.DataFrame(rows)], ignore_index=True)
                done.to_csv(OUT, index=False)
                have.add(snap)
                n_prim = sum(r["primary_outcomes"] != "[]" for r in rows)
                print(f"{snap}: {len(rows)} of our {len(ncts)} trials found, {n_prim} with a primary endpoint")
        if args.delete:
            z.unlink()
            print(f"   deleted {z.name}")