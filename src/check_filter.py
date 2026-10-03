"""Show 15 'yes' and 15 'no' filter answers with the start of each release, to check by eye."""
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
verdicts = json.loads((ROOT / "labels" / "filter_verdicts.json").read_text())
random.seed(1)
for answer in (True, False):
    picks = [a for a, v in verdicts.items() if v is answer]
    print(f"\n===== Gemini said {answer} ({len(picks)} total) =====")
    for a in random.sample(picks, min(15, len(picks))):
        text = (ROOT / "data" / "raw" / "docs" / f"{a}.txt").read_text(encoding="utf-8", errors="replace")
        print(f"\n[{a}] {text[:250]}")