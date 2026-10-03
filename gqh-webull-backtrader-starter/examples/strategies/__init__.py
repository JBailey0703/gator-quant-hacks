"""Example backtrader strategies built on the ``webull_bt`` package.

These are illustrative strategies, not part of the reusable library. Each
module exposes a module-level ``STRATEGY_CLASS`` so the backtest entry point
can load it dynamically via the ``WEBULL_STRATEGY`` env var (its filename
without ``.py``, e.g. "dual_ma" or "portfolio").
"""
