"""Run a historical backtest with the Webull OpenAPI data feed.

Configuration is read from backtest/.env (never hardcode credentials).

    uv run python backtest/main.py
or
    python backtest/main.py
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the project importable when run as a script (python examples/backtest/main.py):
#   - PROJECT_ROOT hosts the reusable ``webull_bt`` package.
#   - the sibling ``strategies`` dir lets WEBULL_STRATEGY name a strategy by its
#     bare module name (e.g. "dual_ma"), loaded via _load_strategy_class().
_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parent.parent.parent
STRATEGIES_DIR = _HERE.parent.parent / "strategies"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(STRATEGIES_DIR))

import backtrader as bt
from dotenv import load_dotenv

from webull.core.client import ApiClient
from webull.data.data_client import DataClient

from webull_bt.feed import WebullData
from webull_bt.logging_utils import get_logger, setup_logging
from webull_bt.visualize import RecorderAnalyzer, render_report
from webull_bt.visualize_lwc import render_report_lwc


logger = get_logger("backtest.main")


def _load_strategy_class(module_name: str) -> type[bt.Strategy]:
    """Dynamically load a strategy class by its module (file) name.

    ``module_name`` is the strategy file's name without the ``.py``
    extension (e.g. "dual_ma" for examples/strategies/dual_ma.py). The module
    must expose a module-level ``STRATEGY_CLASS`` attribute pointing at the
    ``bt.Strategy`` subclass to run; every strategy in this project follows
    this convention. Adding a new strategy file under examples/strategies/
    that follows the same convention makes it usable here with no changes to
    this loader.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(
            f"invalid WEBULL_STRATEGY={module_name!r}: could not import module "
            f"{module_name!r} ({exc}); it must be a .py file in examples/strategies/"
        ) from exc

    strategy_cls = getattr(module, "STRATEGY_CLASS", None)
    if strategy_cls is None or not (
        isinstance(strategy_cls, type) and issubclass(strategy_cls, bt.Strategy)
    ):
        raise SystemExit(
            f"invalid WEBULL_STRATEGY={module_name!r}: module {module_name!r} does not "
            "define a module-level STRATEGY_CLASS pointing at a bt.Strategy subclass"
        )
    return strategy_cls


def _parse_strategy_params() -> dict:
    """Parse WEBULL_STRATEGY_PARAMS into kwargs for cerebro.addstrategy().

    Format: comma-separated ``key=value`` pairs, e.g.
    "short_period=10,long_period=30". Values are coerced to int, then
    float, then bool ("true"/"false"), falling back to the raw string.
    This lets any strategy's ``params`` be tuned from .env without touching
    code, regardless of which strategy is selected.
    """
    raw = os.environ.get("WEBULL_STRATEGY_PARAMS", "").strip()
    if not raw:
        return {}

    params = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(
                f"invalid WEBULL_STRATEGY_PARAMS entry {pair!r}: expected key=value"
            )
        key, _, value = pair.partition("=")
        params[key.strip()] = _coerce_param_value(value.strip())
    return params


def _coerce_param_value(value: str):
    """Best-effort string -> int/float/bool coercion for env-sourced params."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def build_data_client() -> DataClient:
    """Read credentials from the environment and build a DataClient
    (credentials must never be hardcoded)."""
    app_key = os.environ.get("WEBULL_APP_KEY")
    app_secret = os.environ.get("WEBULL_APP_SECRET")
    if not app_key or not app_secret:
        raise SystemExit(
            "missing credentials: set WEBULL_APP_KEY and WEBULL_APP_SECRET "
            "in backtest/.env or the environment"
        )

    region_id = os.environ.get("WEBULL_REGION_ID", "us")
    api_endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.webull.com")

    api_client = ApiClient(app_key, app_secret, region_id)
    api_client.add_endpoint(region_id, api_endpoint)
    return DataClient(api_client)


def _parse_datetime_env(name: str) -> datetime | None:
    """Parse an optional ISO 8601 datetime from the environment.

    A timezone offset is recommended. Naive values are interpreted as UTC so
    the API request remains deterministic across machines.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(
            f"invalid {name}: {raw!r}; use ISO 8601, e.g. "
            "2026-09-15T09:30:00-04:00"
        ) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def build_feed(data_client: DataClient, symbol: str) -> WebullData:
    """Build a single WebullData feed for ``symbol``.

    ``symbol`` is required and explicit; callers resolve the symbol list via
    ``_parse_symbols()`` first, so a single env var (``WEBULL_SYMBOLS``)
    drives any strategy regardless of how many symbols it trades.
    """
    timespan = os.environ.get("WEBULL_TIMESPAN", "D")
    category = os.environ.get("WEBULL_CATEGORY", "US_STOCK")
    count = int(os.environ.get("WEBULL_COUNT", "200"))
    fromdate = _parse_datetime_env("WEBULL_FROMDATE")
    todate = _parse_datetime_env("WEBULL_TODATE")
    if fromdate and todate and fromdate > todate:
        raise SystemExit("WEBULL_FROMDATE must be earlier than or equal to WEBULL_TODATE")

    return WebullData(
        dataname=symbol,
        data_client=data_client,
        category=category,
        timespan=timespan,
        count=count,
        fromdate=fromdate,
        todate=todate,
        trading_sessions="RTH",
    )


def _parse_symbols() -> list[str]:
    """Parse WEBULL_SYMBOLS (comma-separated), shared by every strategy,
    falling back to a single default symbol if unset."""
    raw = os.environ.get("WEBULL_SYMBOLS", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return symbols or ["AAPL"]


def _compute_metrics(
    cerebro: bt.Cerebro, strat: bt.Strategy, starting_value: float
) -> dict:
    """Compute the backtest's summary metrics into a plain dict.

    Centralizes the metric math so both the text log (``_print_results``) and
    the HTML report (``render_report``'s KPI cards) read from the same source
    instead of recomputing. ``sharpe_ratio`` may be None when SharpeRatio
    can't be computed (too few returns or zero volatility).
    """
    final_value = cerebro.broker.getvalue()
    pnl = final_value - starting_value
    pnl_pct = (pnl / starting_value * 100.0) if starting_value else 0.0

    dd = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()
    sharpe = strat.analyzers.sharpe.get_analysis()
    sharpe_ratio = sharpe.get("sharperatio")

    total_trades = trades.get("total", {}).get("total", 0)
    won = trades.get("won", {}).get("total", 0)
    lost = trades.get("lost", {}).get("total", 0)
    win_rate = (won / total_trades * 100.0) if total_trades else 0.0
    net_pnl = trades.get("pnl", {}).get("net", {}).get("total", 0.0)

    return {
        "starting_value": starting_value,
        "final_value": final_value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "max_drawdown_pct": dd.get("max", {}).get("drawdown", 0.0),
        "max_drawdown_money": dd.get("max", {}).get("moneydown", 0.0),
        "sharpe_ratio": sharpe_ratio,
        "total_trades": total_trades,
        "won": won,
        "lost": lost,
        "win_rate": win_rate,
        "net_pnl": net_pnl,
    }


def _print_results(strat: bt.Strategy, metrics: dict) -> None:
    """Log a summary report of the backtest results: P&L, returns, drawdown,
    and trade statistics. Metrics come from ``_compute_metrics``."""
    starting_value = metrics["starting_value"]
    final_value = metrics["final_value"]
    pnl = metrics["pnl"]
    pnl_pct = metrics["pnl_pct"]
    sharpe_ratio = metrics["sharpe_ratio"]
    total_trades = metrics["total_trades"]
    won = metrics["won"]
    lost = metrics["lost"]
    win_rate = metrics["win_rate"]
    net_pnl = metrics["net_pnl"]

    logger.info("=" * 60)
    logger.info("[Backtest] Result Summary")
    logger.info("=" * 60)
    logger.info("Starting cash    : %.2f", starting_value)
    logger.info("Final cash       : %.2f", final_value)
    logger.info("Net P&L          : %.2f (%.2f%%)", pnl, pnl_pct)
    logger.info("Max drawdown     : %.2f%% (%.2f)",
               metrics["max_drawdown_pct"], metrics["max_drawdown_money"])
    logger.info("Sharpe ratio     : %s (annualized)",
               f"{sharpe_ratio:.4f}" if sharpe_ratio is not None else "N/A")
    logger.info("Total trades     : %d (won=%d, lost=%d, win rate=%.2f%%)",
               total_trades, won, lost, win_rate)
    logger.info("Trades net P&L   : %.2f", net_pnl)
    logger.info("=" * 60)

    closed_trades = getattr(strat, "closed_trades", [])
    if closed_trades:
        logger.info("[Backtest] Trade-by-trade detail (%d closed trade(s)):", len(closed_trades))
        for i, t in enumerate(closed_trades, start=1):
            logger.info(
                "  #%d %s %s size=%s entry=%.2f@%s exit=%.2f@%s "
                "pnl=%.2f pnlcomm=%.2f commission=%.2f bars=%d",
                i, t["symbol"], t["direction"], t["size"],
                t["entry_price"], t["open_dt"],
                t["exit_price"], t["close_dt"],
                t["pnl"], t["pnlcomm"], t["commission"], t["bars_held"],
            )
        logger.info("=" * 60)

    rebalance_log = getattr(strat, "rebalance_log", [])
    if rebalance_log:
        logger.info("[Backtest] Rebalance history (%d rebalance(s)):", len(rebalance_log))
        for r in rebalance_log:
            logger.info(
                "  %s -> selected=%s weight=%.2f each (ranked=%s)",
                r["datetime"], r["selected"], r["weight"],
                [(n, round(s, 4)) for n, s in r["ranked"]],
            )
        logger.info("=" * 60)


def _maybe_render_report(
    strat: bt.Strategy, metrics: dict, strategy_module: str, symbols: list[str]
) -> None:
    """Generate the interactive HTML report unless disabled.

    Controlled by env vars:
      - ``WEBULL_VISUALIZE`` (default "true"): set to "false"/"0" to skip.
      - ``WEBULL_VISUALIZE_ENGINE`` (default "plotly"): "plotly" for the Plotly
        report (visualize.py) or "lwc" for the TradingView Lightweight Charts
        report (visualize_lwc.py). Both consume the same recorded data.
      - ``WEBULL_VISUALIZE_OUTPUT``: output path, resolved relative to
        backtest/main.py's directory when relative. Defaults depend on the
        engine ("backtest_report.html" / "backtest_report_lwc.html") so the two
        engines don't overwrite each other.

    Rendering failures are logged but never abort the run: the backtest itself
    (and its text summary) has already completed successfully by this point.
    """
    flag = os.environ.get("WEBULL_VISUALIZE", "true").strip().lower()
    if flag in ("false", "0", "no", "off"):
        logger.info("[Backtest] visualization disabled (WEBULL_VISUALIZE=%s)", flag)
        return

    # Select the rendering engine (positive whitelist match, explicit error on
    # anything else so a typo doesn't silently fall back).
    engine = os.environ.get("WEBULL_VISUALIZE_ENGINE", "plotly").strip().lower()
    if engine == "plotly":
        renderer, default_output = render_report, "backtest_report.html"
    elif engine == "lwc":
        renderer, default_output = render_report_lwc, "backtest_report_lwc.html"
    else:
        logger.error(
            "[Backtest] invalid WEBULL_VISUALIZE_ENGINE=%r (use 'plotly' or 'lwc'); "
            "skipping visualization", engine,
        )
        return

    output = os.environ.get("WEBULL_VISUALIZE_OUTPUT", default_output).strip()
    output_path = Path(output)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parent / output_path

    recorded = strat.analyzers.recorder.get_analysis()
    closed_trades = getattr(strat, "closed_trades", [])
    title = f"Backtest: {strategy_module} [{', '.join(symbols)}]"
    try:
        renderer(
            recorded, closed_trades, str(output_path), title=title, metrics=metrics
        )
    except Exception:
        logger.exception("[Backtest] failed to render visualization report")


def run_backtest() -> None:
    data_client = build_data_client()

    cerebro = bt.Cerebro()

    symbols = _parse_symbols()
    for symbol in symbols:
        cerebro.adddata(build_feed(data_client, symbol=symbol), name=symbol)

    # WEBULL_STRATEGY names a strategy module (its .py filename, without the
    # extension) living in examples/strategies/, e.g. "dual_ma" or
    # "portfolio". The module must expose STRATEGY_CLASS; see
    # _load_strategy_class(). WEBULL_STRATEGY_PARAMS optionally overrides that
    # strategy's params, e.g. "short_period=10,long_period=30".
    strategy_module = os.environ.get("WEBULL_STRATEGY", "dual_ma").strip()
    strategy_cls = _load_strategy_class(strategy_module)
    strategy_params = _parse_strategy_params()
    logger.info(
        "[Backtest] strategy=%s symbols=%s params=%s",
        strategy_module, symbols, strategy_params,
    )
    cerebro.addstrategy(strategy_cls, **strategy_params)
    # A default sizer for strategies that submit orders without an explicit
    # size (e.g. plain buy()/close()). Strategies that always pass an
    # explicit size (order_target_percent, order_target_value, etc.) simply
    # never consult this sizer.
    cerebro.addsizer(bt.sizers.FixedSize, stake=10)

    cerebro.broker.setcash(100000.0)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    # Sharpe ratio computed on the data's own timeframe/compression (inferred
    # by WebullData from the timespan), so minute/daily backtests are scored
    # on the granularity they actually ran at. annualize=True reports it in
    # the conventional annualized form.
    data0 = cerebro.datas[0]
    cerebro.addanalyzer(
        bt.analyzers.SharpeRatio,
        _name="sharpe",
        timeframe=data0._timeframe,
        compression=data0._compression,
        riskfreerate=0.0,
        annualize=True,
    )
    # Records per-symbol OHLC + overlay indicators + equity for the Plotly
    # report (see visualize.py). Non-invasive: strategies are unaware of it.
    cerebro.addanalyzer(RecorderAnalyzer, _name="recorder")

    starting_value = cerebro.broker.getvalue()
    logger.info("[Backtest] starting cash: %.2f", starting_value)
    results = cerebro.run(runonce=False)
    strat = results[0]
    metrics = _compute_metrics(cerebro, strat, starting_value)
    _print_results(strat, metrics)
    _maybe_render_report(strat, metrics, strategy_module, symbols)


def main() -> None:
    # Load environment variables from backtest/.env (managed via python-dotenv).
    load_dotenv(Path(__file__).resolve().parent / ".env")
    setup_logging()
    run_backtest()


if __name__ == "__main__":
    main()
