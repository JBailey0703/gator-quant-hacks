"""Keyword-rule spin score (planned variant 2: what does Gemini add over a word list?).
Score 0-4 = number of distinct spin phrases found in the release body, capped at 4. The phrase list is the
common spin patterns from the medical literature (trend language, subgroup/post-hoc emphasis, favorable
reframing). Fixed before any returns. Output: data/events/keyword_spin.csv."""
import re

import pandas as pd

from score_spin import DOC_DIR, EVENTS, release_body

PATTERNS = [r"\btrend", r"\bnumerical", r"\bnominal", r"\bpost[- ]hoc", r"\bsub-?group", r"\bexploratory",
            r"\bencouraging", r"\bclinically meaningful", r"\bfavorabl", r"\bsignal of"]


def keyword_spin(text):
    low = text.lower()
    return min(4, sum(bool(re.search(p, low)) for p in PATTERNS))


if __name__ == "__main__":
    ev = pd.read_csv(EVENTS / "event_records.csv", dtype=str).fillna("")
    rows = [{"adsh": a, "spin_keyword": keyword_spin(release_body(
        (DOC_DIR / f"{a}.txt").read_text(encoding="utf-8", errors="replace")))} for a in ev["adsh"]]
    out = pd.DataFrame(rows)
    out.to_csv(EVENTS / "keyword_spin.csv", index=False)
    print(f"keyword spin scores for {len(out)} events: {out['spin_keyword'].value_counts().sort_index().to_dict()}")
