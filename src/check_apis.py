"""Check that Gemini, ClinicalTrials.gov and Massive all respond."""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Gemini ---
from google import genai
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
print("Gemini models with 'flash-lite' in the name:")
for m in client.models.list():
    if "flash-lite" in m.name:
        print("  ", m.name)
reply = client.models.generate_content(model="gemini-3.1-flash-lite", contents="Reply with the word OK.")
print("Gemini says:", reply.text.strip())

# --- ClinicalTrials.gov (no key) ---
r = requests.get("https://clinicaltrials.gov/api/v2/studies/NCT04368728", timeout=30)
study = r.json()["protocolSection"]
print("\nClinicalTrials.gov trial:", study["identificationModule"]["briefTitle"])
for o in study["outcomesModule"]["primaryOutcomes"]:
    print("   registered primary endpoint:", o["measure"][:100])

# --- Massive ---
headers = {"Authorization": f"Bearer {os.environ['MASSIVE_API_KEY']}"}
r = requests.get("https://api.massive.com/stocks/taxonomies/vX/disclosures", headers=headers, timeout=30)
print("\nMassive status:", r.status_code)
items = r.json().get("results", [])
print("Massive categories:", len(items))
for it in items:
    text = str(it).lower()
    if "clinical" in text or "trial" in text or "fda" in text:
        print("   biotech-related:", it)