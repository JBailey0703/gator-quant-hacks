# Webull Quant Backtesting Example · Participant Guide

> For students in the **Gator Quant Hacks 2026 · Systematic Trading Track**.
>
> This guide assumes you **know a little Python** but need **no** quant-finance
> background at all. Follow the steps and you'll have your first backtest running
> in about 15 minutes — and it even generates a slick interactive chart report for you.

---

## 1. What This Project Does For You

The competition asks you to build and optimize a trading strategy on historical data,
and judges will look at metrics like your **Sharpe ratio, max drawdown, and turnover**.

This project already wires up the tedious parts — the **data pipeline** and the
**backtesting engine** — so you can **focus entirely on your strategy logic**:

- **Data**: built-in `WebullData` pulls historical bars for US stocks/ETFs straight from
  the Webull OpenAPI. No scrapers, no CSV wrangling.
- **Backtesting**: built on the mature open-source framework
  [backtrader](https://github.com/mementum/backtrader), which computes returns, max drawdown,
  **Sharpe ratio**, win rate, and per-trade P&L for you.
- **Charts**: every backtest auto-generates an interactive HTML report (candlesticks +
  indicators + buy/sell markers + equity curve + KPI cards).
- **Two ready-made strategy examples**: a dual moving average strategy and a
  multi-symbol momentum rotation strategy. Copy and tweak to get going.
- **(Advanced) Simulated live trading**: run your strategy bar-by-bar like a real session,
  and even connect it to the real trading API to place orders.

> In one line: **you write the strategy, the framework handles the rest.**
>
> Note: you are scored on the track's rubric (your quant note and repo), not on the backtest
> summary alone. Every result you report must be **net of transaction costs** (this example
> sets none: add them with `cerebro.broker.setcommission(...)`) and include an
> **out-of-sample period** you never tuned on. See the Systematic Trading Track page for the
> full rules. Paper/live trading is not scored; simulated-live and real order placement are
> optional extras.

---

## 2. Project Layout at a Glance

The project splits into two parts: the reusable **`webull_bt` library** (data/trading/utils,
which you generally won't touch), and the **`examples/`** (entry scripts + strategies —
this is where you actually work).

```
backtrader-example/
├── webull_bt/                  # reusable library (generally no need to edit)
│   ├── feed.py                 #   core data feed: WebullData
│   ├── broker.py               #   real trading broker: WebullBroker (advanced)
│   ├── timeutils.py            #   timezone / trading-session helpers
│   ├── visualize.py            #   Plotly chart report generation
│   └── logging_utils.py        #   unified logging config
├── examples/                   # examples (you mostly work here)
│   ├── backtest/
│   │   ├── main.py             #   [backtest entry point] ← start here
│   │   └── .env                #   backtest config (put your credentials here)
│   ├── live/
│   │   ├── main.py             #   simulated-live / real trading entry (advanced)
│   │   └── .env                #   live config
│   └── strategies/             #   strategy folder ← your strategies go here
│       ├── dual_ma.py          #     example #1: dual moving average (single/multi symbol)
│       └── portfolio.py        #     example #2: multi-symbol momentum rotation
├── docs/                       # docs (including this guide)
└── pyproject.toml              # dependency manifest
```

---

## 3. Prerequisites

### 3.1 What You Need

- **Python 3.11 or newer** (run `python3 --version` in a terminal to check)
- A set of **Webull OpenAPI credentials** (`app_key` + `app_secret`). Log in to your Webull
  account and apply under Developer Tool → OpenAPI Management; see
  <https://developer.webull.com/apis/docs/authentication/IndividualApplicationAPI/>
- A terminal (Terminal on macOS/Linux, PowerShell or Git Bash on Windows)

### 3.2 Install Dependencies

The project uses [`uv`](https://docs.astral.sh/uv/) — a fast, hassle-free Python package
manager — to manage the environment. **`uv` is recommended**:

```bash
# Enter the project directory
cd backtrader-example

# One command sets up the virtual environment + installs dependencies
uv sync
```

If you prefer the traditional `pip`:

```bash
cd backtrader-example
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

Dependencies are all declared in `pyproject.toml`: `backtrader` (backtesting engine),
`webull-openapi-python-sdk` (data/trading API), `python-dotenv` (reads config), plus
`plotly` and `pandas` for the chart report.

---

## 4. Add Your Credentials

The project splits "backtest" and "live" into two independent entry points, each reading
its own `.env` config file. **Running a backtest is all you need to start, so you only need
to configure `examples/backtest/.env`.**

Copy `examples/backtest/.env.example` to `examples/backtest/.env` (the `.env` itself is
not shipped, since it holds credentials), then replace the credentials with your own:

```dotenv
WEBULL_APP_KEY=your_app_key
WEBULL_APP_SECRET=your_app_secret
WEBULL_API_ENDPOINT=api.webull.com
WEBULL_REGION_ID=us
```

> ⚠️ **Credentials are sensitive — never hardcode them into source, never commit them to
> Git.** The `.env` files are already in `.gitignore`; double-check before committing.
> Accessing US stock/ETF market data requires a valid market data subscription, otherwise
> the API may return 403.

---

## 5. Run Your First Backtest

Once credentials are set, run the backtest entry point from the project root:

```bash
uv run python examples/backtest/main.py
# or (when the virtual environment is already activated)
python examples/backtest/main.py
```

When it finishes, you'll see a result summary in the terminal, like this:

```
============================================================
[Backtest] Result Summary
============================================================
Starting cash    : 100000.00      # initial capital
Final cash       : 103250.00      # ending capital
Net P&L          : 3250.00 (3.25%) # net profit / return
Max drawdown     : 4.10% (4100.00) # max drawdown
Sharpe ratio     : 1.2500 (annualized)  # Sharpe ratio (annualized) ★ key judged metric
Total trades     : 12 (won=7, lost=5, win rate=58.33%)  # trade count / win rate
Trades net P&L   : 3250.00
============================================================
```

It then prints a **trade-by-trade breakdown** (entry/exit price, P&L, and holding period
for each trade).

It also auto-generates an interactive **`backtest_report.html`** in the
`examples/backtest/` directory — open it in a browser to see candlestick charts, overlaid
moving averages, buy/sell markers, the equity curve with drawdown, and KPI cards up top.

🎉 Congrats — you've completed a full backtest. From here it's all about tweaking config
and strategy to optimize those metrics.

---

## 6. Tune the Backtest via Config (No Code Needed)

Almost everything you'll commonly touch lives in `examples/backtest/.env`. Change it, then
just re-run the command above.

| Setting | Purpose | Common values |
| --- | --- | --- |
| `WEBULL_SYMBOLS` | Which symbols to trade (comma-separated, one or many) | `AAPL` or `AAPL,MSFT,GOOG` |
| `WEBULL_CATEGORY` | Security type | `US_STOCK`, `US_ETF`, … (any category the SDK supports) |
| `WEBULL_TIMESPAN` | Bar granularity | `D`=daily, `M1`=1-min, `M5`=5-min… |
| `WEBULL_COUNT` | How many bars to fetch | e.g. `1200` |
| `WEBULL_FROMDATE` / `WEBULL_TODATE` | Backtest time range (optional) | ISO 8601, timezone recommended |
| `WEBULL_STRATEGY` | Which strategy (use the **filename**, no `.py`) | `dual_ma` (default) or `portfolio` |
| `WEBULL_STRATEGY_PARAMS` | Override strategy params (no code change) | `short_period=10,long_period=30` |
| `WEBULL_VISUALIZE` | Whether to generate the HTML report | `true` (default) / `false` |
| `WEBULL_VISUALIZE_OUTPUT` | Report output filename | `backtest_report.html` (default) |
| `WEBULL_DISPLAY_TZ` | Display timezone for logs/charts | `America/New_York` (default, US Eastern) |
| `LOG_LEVEL` | Log verbosity | `INFO` (default), `DEBUG` (per-bar detail) |

On bar granularity: `WEBULL_TIMESPAN` supports `M1/M5/M15/M30/M60/M120/M240/D/W/M/Y`.
Use `M1` for **minute-level backtests**; use `D` (daily) for quickly validating a strategy
idea (smaller data, faster runs).

Example time range (intraday on a US Eastern trading day):

```dotenv
WEBULL_TIMESPAN=M1
WEBULL_FROMDATE=2026-09-15T09:30:00-04:00
WEBULL_TODATE=2026-09-15T16:00:00-04:00
```

When no time range is set, it defaults to fetching the most recent N bars per
`WEBULL_COUNT`.

---

## 7. The Two Example Strategies

Strategy files live in `examples/strategies/`. `WEBULL_STRATEGY` is set to the **strategy
filename** (without `.py`). Each strategy file ends with a line `STRATEGY_CLASS = ...` —
that's how the entry point finds your strategy class. Copy that line when you add your own.

### 7.1 Dual moving average — `dual_ma.py` (default)

The classic starter strategy, in one sentence (it runs **independently on each symbol** in
`WEBULL_SYMBOLS`):

- **Golden cross → buy**: when the short SMA crosses above the long SMA, buy if flat.
- **Death cross → close**: when the short SMA crosses below the long SMA, close if holding.

Two tunable params: `short_period` (default 5, short SMA period), `long_period` (default 20,
long SMA period).

In `examples/backtest/.env`:

```dotenv
WEBULL_STRATEGY=dual_ma
WEBULL_SYMBOLS=AAPL,TSLA
# Optional: tune params without touching code
WEBULL_STRATEGY_PARAMS=short_period=10,long_period=30
```

### 7.2 Multi-symbol momentum rotation — `portfolio.py`

Manages a basket of stocks at once, demonstrating "cross-sectional momentum rotation" — a
style that genuinely needs multiple symbols:

1. Rebalance every `rebalance_days` bars (not on every single bar).
2. Compute each symbol's trailing return over the past `lookback` bars as its "momentum
   score".
3. Rank by score, keep only the positive ones, and take the top `top_n`.
4. Allocate capital equally across the selected symbols (each capped at `max_weight`), and
   flatten any symbol no longer selected.

In `examples/backtest/.env`:

```dotenv
WEBULL_STRATEGY=portfolio
WEBULL_SYMBOLS=AAPL,MSFT,GOOG,AMZN
# Optional: tune params
WEBULL_STRATEGY_PARAMS=lookback=15,rebalance_days=3,top_n=2,max_weight=0.5
```

Tunable params: `lookback` (momentum window), `rebalance_days` (rebalance interval),
`top_n` (max symbols held), `max_weight` (per-symbol weight cap). Alongside the trade
breakdown, it also prints the ranking and selection at each rebalance.

---

## 8. Write Your Own Strategy

This is what the competition is really about. The steps are simple:

1. Create a new file under `examples/strategies/`, e.g. `my_strategy.py`.
2. Write a class subclassing `bt.Strategy` and implement `next()` (called once per new bar).
3. **Add a line at the end of the file**: `STRATEGY_CLASS = YourStrategyClass`.
4. Set `WEBULL_STRATEGY` in `.env` to the filename `my_strategy`, then re-run.

Minimal skeleton `examples/strategies/my_strategy.py`:

```python
import backtrader as bt

class MyStrategy(bt.Strategy):
    params = dict(period=14)   # your tunable params (overridable via WEBULL_STRATEGY_PARAMS)

    def __init__(self):
        # Define indicators here, e.g. an RSI
        self.rsi = bt.ind.RSI(period=self.p.period)

    def next(self):
        # Called once per bar; self.data.close[0] is the current close
        if not self.position:                 # currently flat
            if self.rsi < 30:                 # oversold -> buy
                self.buy()
        elif self.rsi > 70:                   # overbought -> close
            self.close()

# The entry point finds your strategy class via this line — don't forget it!
STRATEGY_CLASS = MyStrategy
```

Then:

```dotenv
WEBULL_STRATEGY=my_strategy
```

Handy backtrader APIs:

- `self.data.close[0]` / `open[0]` / `high[0]` / `low[0]` / `volume[0]`: current bar's data;
  `[-1]` is the previous bar.
- `self.buy()` / `self.sell()` / `self.close()`: place order / close position.
- `self.position`: current position (`if not self.position:` checks whether you're flat).
- `bt.ind.SMA` / `RSI` / `CrossOver` …: a large set of built-in technical indicators.
- backtrader official docs: <https://www.backtrader.com/docu/>

> Tip: this project's `WebullData` exposes an extra `trading_session` line marking whether
> each bar is pre-market/regular/after-hours/overnight (`PRE`/`RTH`/`ATH`/`OVN`). Decode it
> with `webull_bt.timeutils.decode_trading_session()` when needed — see the usage in
> `dual_ma.py`. Daily bars and above don't carry this info.
>
> To have your custom indicators show up in the chart report, follow `dual_ma.py`: store
> indicators in a `self.inds` dict, and the report generator will overlay moving-average
> style indicators onto the candlestick chart automatically.

---

## 9. Use the Feed Directly in Your Own Script (Without main.py)

If you'd rather build your own backtest flow, `WebullData` can be imported straight from the
`webull_bt` package. You construct the credentials and client; the feed only fetches data:

```python
import backtrader as bt
from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from webull_bt import WebullData   # import from the webull_bt package

# 1. Build a DataClient (read credentials from env vars; don't hardcode)
api_client = ApiClient(app_key, app_secret, "us")
api_client.add_endpoint("us", "api.webull.com")
data_client = DataClient(api_client)

# 2. Add the feed to cerebro
cerebro = bt.Cerebro()
cerebro.adddata(
    WebullData(
        dataname="AAPL",
        data_client=data_client,   # required
        category="US_STOCK",
        timespan="D",
        count=200,
    )
)
cerebro.addstrategy(MyStrategy)
cerebro.broker.setcash(100000.0)
cerebro.run()
```

Full parameter reference for `WebullData` is in its docstring in `webull_bt/feed.py`.

---

## 10. (Advanced) Simulated Live & Real Order Placement

> This part is not scored — it's an optional extra for those with spare time. **Always
> validate in the Sandbox/test environment first**; don't experiment with real money.

- `examples/live/main.py` is the simulated-live entry point. It uses a background thread to
  **poll** the historical bars endpoint, simulating bars arriving one by one to drive the
  strategy.
- By default `WEBULL_USE_BROKER=0`: it only runs the strategy and **places no orders**.
- Set `WEBULL_USE_BROKER=1` in `examples/live/.env` to connect the real trading API and
  place orders; `WEBULL_ENV` switches credentials/accounts between `prod` and `sandbox`.

Run:

```bash
python examples/live/main.py   # Ctrl+C to stop
```

⚠️ **Risk warning**: with `WEBULL_USE_BROKER=1`, strategy signals **place real orders**. The
trading API requires separate access approval. For details and the broker's order
type/status mapping, see `webull_bt/broker.py`.

---

## 11. FAQ

**Q: The API returns 403 / 401?**
A: Usually wrong credentials or no market data subscription. Check that `WEBULL_APP_KEY` /
`WEBULL_APP_SECRET` are correct and `WEBULL_API_ENDPOINT` is right, and confirm your account
has the relevant US market data entitlement.

**Q: `invalid WEBULL_STRATEGY=...` error?**
A: `WEBULL_STRATEGY` must be the **filename** of a strategy in `examples/strategies/`
(without `.py`), and that file must end with the line `STRATEGY_CLASS = YourStrategyClass`.

**Q: The backtest produced no trades?**
A: Possibly too little data (`WEBULL_COUNT` too small) so indicators haven't "warmed up"
yet, or the strategy's signals simply weren't triggered over that window. Try increasing
`WEBULL_COUNT` and set `LOG_LEVEL=DEBUG` to inspect each bar.

**Q: Can I skip the HTML report / does a failed report break the backtest?**
A: Set `WEBULL_VISUALIZE=false` to skip it. Even if report generation fails, it won't affect
the backtest itself — the summary is still printed.

**Q: What timezone are the times in the charts and logs?**
A: US Eastern by default (`America/New_York`), with daylight saving handled automatically.
Change `WEBULL_DISPLAY_TZ` to switch.

**Q: Minute-level vs. daily backtests — which should I use?**
A: Use daily (`D`) for validating ideas and fast iteration; use minute bars (`M1`) when your
strategy needs intraday detail. Minute data is large, so the first fetch is slower.

---

Have fun at Gator Quant Hacks, and may your Sharpe ratio be high 📈
