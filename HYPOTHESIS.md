# Hypothesis

## Statement

We expect liquid US small- and mid-cap biotech stocks to move toward what the trial data actually supports over the 5 to 20 trading days after a trial-results press release, because investors price the headline, not the substance: a spun release pushes the price too high and it falls back, while a clean result is underpriced at first and keeps drifting in its direction. The edge persists because reading trial data takes expertise most retail traders lack, these stocks get little analyst coverage, and betting against an overpriced small biotech is costly for arbitrageurs.

Predictions:

1. Higher spin score → larger decline after the release.
2. Clean results → continued drift in the direction of the result.

Fails if: high-spin and low-spin releases perform the same, clean results show no drift, or the out-of-sample result is near zero after costs.

## Settings

- Development (in-sample): 2018-05-01 to 2023-12-31
- Validation (in-sample): 2024-01-01 to 2024-12-31
- Out-of-sample: 2025-01-01 to 2026-10-02 (events through 2026-09-02; run once)
- Universe: SIC 2834, 2836, 8731; market cap $300M–$10B on event date; 20-day avg dollar volume ≥ $5M
- Event: press release or 8-K reporting clinical-trial results
- Entry: close of release day if published before 4:00 PM ET, else next day's close
- Gemini: gemini-3.1-flash-lite, temperature 0, names/tickers/dates redacted
- Readout: primary endpoint met (yes/no/unclear); spin score 0–4 = gap between what the release claims and the primary endpoint registered on ClinicalTrials.gov (registry version posted before the release)
- Validation only (not traded): ClinicalTrials.gov posted results and FDA decisions after the event
- Spin groups: top vs bottom third, cut points from in-sample only
- Spun trade: top-third spin and first-day return vs XBI > 0 → short
- Clean trade: bottom-third spin → long if primary met, short if missed
- Horizons: 5 and 20 trading days
- Benchmark: XBI
- Position size: min(5% of capital, 1% of 20-day dollar volume)
- Max positions: 20; max gross exposure: 100%
- Costs: 20 bps per side; short borrow 10%/yr; stress test at 2× costs

## Planned variants

1. Long-only
2. Keyword-rule spin score instead of Gemini
3. 5-day vs 20-day horizon
4. Text-only spin score (no ClinicalTrials.gov match), also the fallback for unmatched events