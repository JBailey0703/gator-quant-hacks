# Hypothesis
> The **Final rules** section and its addenda at the bottom are authoritative. Where they differ, the latest addendum wins. Earlier sections are kept unchanged to show what was committed first.

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
- First reaction day: first trading session whose actual close is after the release timestamp; use the exchange calendar, including holidays and early closes.
- First-day return vs XBI: stock return minus XBI return, both measured from the preceding trading session's close to the first reaction day's close.
- Entry (both trade types): finalize the signal after the first reaction day's close using information available by that close, then enter at the following trading session's regular-hours close, with costs applied.
- Gemini: gemini-3.1-flash-lite, temperature 0, names/tickers/dates redacted
- Readout: primary endpoint met (yes/no/unclear); spin score 0–4 = gap between what the release claims and the primary endpoint registered on ClinicalTrials.gov (registry version posted before the release)
- Validation only (not traded): ClinicalTrials.gov posted results and FDA decisions after the event
- Spin groups: top vs bottom third, cut points from in-sample only
- Spun trade: top-third spin and first-day return vs XBI > 0 → short
- Clean trade: bottom-third spin → long if primary met, short if missed
- Horizons: event-study returns reported at 1, 2, 3, 5, 10, 20, 40, 60 trading days for every group, in-sample and out-of-sample
- Trading horizon selection rule (fixed in advance): the horizon with the highest net-of-cost Sharpe on 2018–2023, among horizons whose two neighbors on the list also have positive net returns (a plateau, not a spike); ties go to the longer horizon (lower turnover). Confirmed on 2024, then locked before the out-of-sample run. All horizons are reported, not only the chosen one.
- Benchmark: XBI
- Position size: min(5% of capital, 1% of 20-day dollar volume)
- Max positions: 20; max gross exposure: 100%
- Costs: 20 bps per side; short borrow 10%/yr; stress test at 2× costs

## Planned variants

1. Long-only
2. Keyword-rule spin score instead of Gemini
3. All horizons (1, 2, 3, 5, 10, 20, 40, 60 trading days) reported; trading horizon chosen by the rule above
4. Text-only spin score (no ClinicalTrials.gov match), also the fallback for unmatched events

## Implementation rules (fixed before any returns were computed)

- Spin score used for trading: registry-comparison score when the event is matched to a ClinicalTrials.gov trial; text-only score otherwise
- Spin groups: clean = score 0-1, spun = score 2-4 (rubric definitions; replaces top/bottom third because scores are whole numbers and thirds create ties)
- Liquidity: 20-session average dollar volume before the reaction day, divided by 0.24 (measured Nasdaq-feed share of total volume), must be at least $5M
- Market cap: shares outstanding filed before the event x close of the session before the reaction day, between $300M and $10B
- One open position per stock; if 20 positions are open, new signals are skipped
- Robustness: results also reported without the 475 events timed by release date only, for the 168 events with unchanged registry records, and with text-only spin for all events

## Final rules (committed 2026-10-03 night, after a mock-judge review, before any returns were computed)

### Events and timing
- Event time: SEC acceptance time converted to New York time when the 8-K was filed on the release date; otherwise the release date at 11:59 PM (time unknown, assumed after the close).
- Excluded: release date after the SEC filing; 8-K filed more than 7 days after the release date; dates before 2018-05-01 (`data/events/excluded_events.csv`).
- First reaction day, first-day return vs XBI and entry (next session's regular-hours close) as in Settings.

### Prices
- Databento XNAS.ITCH hour bars from 09:00 through the bar ending at the close (4 PM; 1 PM on early-close days); close = last Nasdaq regular-hours trade. Data end: 2026-10-02.
- Splits (`src/check_splits.py`): one-day rise above 3x = reverse split if jump-day volume < 5x its 10-day average or an 8-K mentions a reverse split (45 days before to 5 days after); split-day stock return set equal to XBI's. Drop below 1/3 with volume < 5x average = unverified → event excluded. Split between the shares-outstanding filing and the reaction day → event excluded.
- Events without a price on the previous session, reaction day or entry day are excluded. A position in a stock that stops trading is closed at its last available close.

### Signal
- Registry text: from the latest AACT snapshot dated before the event date that contains the matched trial. Spin used for trading: that registry score; text-only score if the trial is not in an earlier snapshot, the event is unmatched, or the match is marked not confident. Scores against today's registry record are a labeled diagnostic only.
- Groups: clean = spin 0-1, spun = spin 2-4.
- Short: spun and first-day return vs XBI > 0. Long: clean and primary endpoint met. Short: clean and primary endpoint missed. No trade otherwise (spun that fell, endpoint unclear). "Endpoint met" comes from the same scoring call as the spin used.

### Universe (on the event, using data before the reaction day)
- SIC 2834, 2836, 8731.
- Market cap $300M-$10B = latest shares outstanding filed before the reaction day x close of the session before the reaction day.
- Liquidity: 20-session average Nasdaq-feed dollar volume before the reaction day >= $1.2M. (Same threshold as "$5M total dollar volume" at the measured 24% Nasdaq share, stated in the units we actually measure.)

### Portfolio
- Starting capital $1,000,000; stock positions only (no XBI hedge); marked to market at daily closes; idle cash earns 0.
- Size at entry: min(5% of current equity, 1% of 20-session average Nasdaq-feed dollar volume).
- Max 20 open positions, gross exposure <= 100%, one position per stock (a new signal for a stock already held is skipped).
- When more signals arrive on one day than free slots: earlier event time first, then ticker alphabetically.
- On each close, exits are processed before entries.
- Exit at the close H sessions after the entry session.
- Costs: 20 bps of traded value on entry and on exit; short borrow 10%/yr charged per session held (1/252). Stress test: 40 bps and 20%/yr.
- Sharpe = annualized mean / standard deviation of daily portfolio returns (including idle days), net of all costs.

### Horizon selection
- H is chosen from 1, 2, 3, 5, 10, 20, 40, 60 sessions using events with reaction days 2018-05-01 to 2023-12-31 (each trade belongs to the period of its reaction day and is held to its full exit).
- Eligible: H whose neighbor(s) on the list (one neighbor for 1 and 60) have positive net annual return. Choose the eligible H with the highest net Sharpe; Sharpe within 0.05 → the longer H. If none is eligible: H = 20, and the note reports that the plateau condition failed.
- Confirmed if net Sharpe at H on 2024 is > 0. If not confirmed, the out-of-sample run still uses the same H and the note reports it. No re-selection.
- Out-of-sample: events with reaction days 2025-01-01 onward whose full H-session hold ends by 2026-10-02; run once.

### Evidence reported (not traded)
- Event study: return vs XBI from entry to each horizon, by trade group and by spin score 0-4; 95% bootstrap intervals resampling companies (2,000 draws).
- Baselines: endpoint-only (long met, short missed, spin ignored); first-day-only (short every first-day rise, spin ignored); keyword spin; text-only spin.
- Robustness: without date-only events; only events whose current registry record is unchanged since the release; text-only spin for all events; today's registry record; 2x costs; long-only.

## Final rules addendum (2026-10-03 ~23:20 ET, after a second mock-judge review, before any returns were computed)

- Data-quality exclusion: only an unverified price jump from 21 sessions before the reaction day through the entry day excludes an event. Later jumps keep the trade with real prices (flagged).
- Reverse splits: the split-day return is the day's price ratio divided by the nearest standard split ratio (replaces "equal to XBI's return"). Side result: without trades that have a split during the hold.
- Periods: an event belongs to a period only if its entry plus 60 sessions ends inside that period (all horizons, event studies and portfolios); equity curves end at the period end. Out-of-sample: events whose 60-session window ends by 2026-10-02. Out-of-sample returns are computed only by `--oos`; its lock (horizon + SHA-256 fingerprint of inputs and code) is written at the start; identical reruns are allowed, changed reruns refused.
- Stocks that stop trading: exit at the last real close, no borrow afterwards. Side result: longs in such stocks lose 100%.
- Tickers: only tickers printed in the release are used.
- Hand exclusions: only those listed with a reason in `data/events/manual_exclusions.csv`. Repeats: also same company + same accepted trial within 30 days.
- Match confidence: a match-check answer of "no" or "unsure", or the matcher's own "not confident" flag, uses the text-only spin score.
- Limits: 5% per stock and 100% gross apply at entry (100% after the entry fee); no rebalancing.
- Reporting: event studies are gross (before costs); net evidence is the portfolio. Capacity shown with Nasdaq share of total volume 15%, 24% and 35%.

## Final rules addendum 2 (2026-10-04 ~00:15 ET, after a third mock-judge review, before any returns were computed)

- Data-quality exclusion: the window ends on the reaction day (the entry-day close is unknown when the order is placed); a jump on the entry day is only flagged.
- The 60-session period-window rule is applied before any return is computed (exclusion reason "60-day window crosses the period end").
- No short borrow is charged after a stock's last real trade. Added side result: longs in stocks that stop trading lose 30% (alongside the 100% worst case). Main rule unchanged: exit at the last real close (disclosed as optimistic).
- Comparisons change one thing: the keyword variant uses the main strategy's endpoint labels; the today's-registry variant uses today's record only for events the main strategy scores against the pre-release (AACT) record.
- The out-of-sample fingerprint covers every price file and the pandas/numpy versions; the out-of-sample run also saves its event table (flags only, no returns).
- Capacity runs leave room for the price-impact cost when sizing.
- Known limitations kept and disclosed, with in-sample sizes: inferred split ratios (0 of 457 in-sample trades have a split during the hold); exit at the last real close (5 of 457).