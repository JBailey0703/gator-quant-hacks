"""Were the hand-labeled trial releases kept by the filter?"""
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
labels = pd.read_csv(ROOT / "labels" / "hand_labels.csv")
verdicts = json.loads((ROOT / "labels" / "filter_verdicts.json").read_text())

for r in labels.itertuples():
    m = re.search(r"/data/\d+/(\d{18})/", r.source_url)
    acc = f"{m.group(1)[:10]}-{m.group(1)[10:12]}-{m.group(1)[12:]}" if m else None
    status = verdicts.get(acc, "not a candidate")
    print(f"{r.example_id}  {r.company[:28]:28}  {r.document_type[:30]:30}  filter: {status}")