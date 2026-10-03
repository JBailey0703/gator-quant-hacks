| Date/time (ET) | Change | Reason | Results seen before change? |
| --- | --- | --- | --- |
| 2026-10-03 12:23 | History start moved from 2019-01-01 to 2018-05-01; added 2024 validation year; OOS moved to 2025-01-01 | Databento XNAS.ITCH history starts 2018-05-01; OOS still ≥ 20% of history per track rule | No |
| 2026-10-03 13:38 | Event search: added 10 phrases (positive data/results, interim/preliminary/initial data, clinical data, results/data from the Phase, statistically significant, efficacy data) and kept 8-K main text, not only EX-99 attachments | Recall vs Massive's independent clinical_trial_results tags (biotech, 2022+) was 46% (1,182 of 2,580). Misses used wording not in our phrases | No (coverage counts only, no returns) |
| 2026-10-03 15:14 | Filter prompt: count only releases whose main purpose is new human trial results; exclude earnings/corporate-update releases. Test sample made random across years | Hand check of 30 answers: most "true" were quarterly earnings releases repeating older trial news | No (filter labels only, no returns) |
| 2026-10-03 15:39 | Both trade types enter at the next session's open after the first reaction day's close; defined the reaction session and stock-minus-XBI close-to-close return. | First-day return is known only after the close; entry must follow signal availability to avoid same-close lookahead. | No returns evaluated during this change. |
| 2026-10-03 15:49 | Entry price: next session's close instead of next session's open | Our regular-hours prices are built from hour bars; the 9:00 bar includes pre-market trades, so opens are not reliable. Closes are exact | No returns evaluated |


Recall after broadening: 88% (2,268 of 2,580).