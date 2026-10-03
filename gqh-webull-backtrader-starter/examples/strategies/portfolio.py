"""A multi-asset (portfolio) momentum rotation strategy.

Unlike ``dual_ma.py`` (single-symbol dual moving average), this strategy
trades a basket of symbols at once. It is meant to be used with multiple
``WebullData`` feeds added to the same ``Cerebro`` instance, one per symbol
(each feed's ``dataname`` becomes ``data._name``).

Design: cross-sectional momentum rotation
  - Every ``rebalance_days`` bars, compute each symbol's trailing return over
    the last ``lookback`` bars (its "momentum score").
  - Rank symbols by momentum score, keep only those with a positive score,
    and take the top ``top_n``.
  - Rebalance to hold the selected symbols at equal weight (``1/top_n`` of
    portfolio value each, capped by ``max_weight``), using
    ``order_target_percent`` so backtrader computes the buy/sell/adjust
    orders automatically. Symbols not selected are flattened to zero.

This is a simple, well-known style of strategy (relative-strength rotation)
chosen because it naturally requires multiple data feeds and demonstrates
patterns specific to multi-asset trading: cross-sectional ranking, capital
allocation across positions, and periodic rebalancing (as opposed to
per-bar signals on a single instrument).
"""

from __future__ import annotations

import backtrader as bt

from webull_bt.logging_utils import get_logger
from webull_bt.timeutils import to_market_tz


logger = get_logger("portfolio")


class PortfolioMomentumStrategy(bt.Strategy):
    """Cross-sectional momentum rotation across multiple symbols.

    Params:

      - ``lookback`` (default 20): number of bars used to compute the
        trailing return (momentum score) for each symbol.

      - ``rebalance_days`` (default 5): rebalance every N bars. Rebalancing
        on every single bar is usually unnecessary and increases turnover
        and transaction costs.

      - ``top_n`` (default 3): maximum number of symbols to hold at once,
        chosen from those with positive momentum. If fewer than ``top_n``
        symbols have positive momentum, only those are held (cash is not
        forced to be fully invested).

      - ``max_weight`` (default 0.35): maximum portfolio weight per symbol,
        even when fewer than ``top_n`` symbols qualify (caps concentration
        risk from equal-weighting a very small basket).
    """

    params = dict(
        lookback=20,
        rebalance_days=5,
        top_n=3,
        max_weight=0.35,
    )

    def __init__(self):
        # One SMA-based "ready" check per data: momentum needs `lookback`
        # bars of history before it can be computed.
        self._bar_count = 0
        # Records of each rebalance decision, for post-run reporting.
        self.rebalance_log = []
        # Per-trade P&L records, same convention as dual_ma.py's
        # DualMovingAverageStrategy, populated via notify_trade().
        self.set_tradehistory(True)
        self.closed_trades = []

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        entry_size = trade.history[0].event.size if trade.history else trade.size
        exit_price = trade.history[-1].event.price if trade.history else trade.price
        self.closed_trades.append({
            "symbol": trade.getdataname(),
            "direction": "LONG" if trade.long else "SHORT",
            "size": entry_size,
            "entry_price": trade.price,
            "exit_price": exit_price,
            # backtrader returns these as naive UTC; convert to market tz for
            # display in logs and the visualization report.
            "open_dt": to_market_tz(trade.open_datetime()),
            "close_dt": to_market_tz(trade.close_datetime()),
            "pnl": trade.pnl,
            "pnlcomm": trade.pnlcomm,
            "commission": trade.commission,
            "bars_held": trade.barlen,
        })

    def _momentum_score(self, data) -> float | None:
        """Trailing return over ``lookback`` bars: close[0] / close[-lookback] - 1.

        Returns None if there isn't enough history yet for this data feed.
        """
        lookback = self.p.lookback
        if len(data) <= lookback:
            return None
        past = data.close[-lookback]
        if not past:
            return None
        return (data.close[0] / past) - 1.0

    def next(self):
        self._bar_count += 1

        # Only rebalance every `rebalance_days` bars (and not on bar 0).
        if self._bar_count % self.p.rebalance_days != 0:
            return

        # Compute momentum scores for every symbol that has enough history.
        scores: list[tuple[str, float, object]] = []
        for data in self.datas:
            score = self._momentum_score(data)
            if score is not None:
                scores.append((data._name, score, data))

        if not scores:
            logger.debug("[Portfolio] rebalance skipped: no symbol has enough history yet")
            return

        # Rank by momentum descending; keep only positive-momentum symbols.
        scores.sort(key=lambda x: x[1], reverse=True)
        selected = [s for s in scores[: self.p.top_n] if s[1] > 0]

        weight = min(1.0 / len(selected), self.p.max_weight) if selected else 0.0
        selected_names = {name for name, _, _ in selected}

        # naive UTC from backtrader -> market tz for display/logging.
        dt = to_market_tz(self.datas[0].datetime.datetime(0))
        logger.info(
            "[Portfolio] rebalance @ %s: ranked=%s selected=%s weight=%.2f each",
            dt,
            [(n, round(s, 4)) for n, s, _ in scores],
            sorted(selected_names),
            weight,
        )
        self.rebalance_log.append({
            "datetime": dt,
            "ranked": [(n, s) for n, s, _ in scores],
            "selected": sorted(selected_names),
            "weight": weight,
        })

        # Flatten any symbol no longer selected.
        for data in self.datas:
            if data._name not in selected_names and self.getposition(data).size:
                logger.info("[Portfolio] flattening %s (dropped out of top momentum)", data._name)
                self.order_target_percent(data=data, target=0.0)

        # Rebalance selected symbols to the target equal weight.
        for name, score, data in selected:
            self.order_target_percent(data=data, target=weight)


# Convention used by backtest/main.py's dynamic strategy loader: pointing
# WEBULL_STRATEGY at this module's filename (without ".py") loads this class.
STRATEGY_CLASS = PortfolioMomentumStrategy
