import backtrader as bt

from webull_bt.logging_utils import get_logger
from webull_bt.timeutils import decode_trading_session, to_market_tz


logger = get_logger("strategy")


class DualMovingAverageStrategy(bt.Strategy):
    """Long-only dual moving average crossover strategy.

    Works with one or many data feeds. Each feed is traded independently:
    every symbol gets its own pair of SMAs, its own crossover signal and its
    own pending-order guard, and the strategy runs the same golden/death
    cross logic per symbol. With a single feed this behaves exactly like a
    plain single-symbol dual-MA strategy.

    Signal (per symbol):
      - Golden cross (short SMA crosses above long SMA) while flat -> buy.
      - Death cross (short SMA crosses below long SMA) while holding -> close
        the position.

    Only one order per symbol is ever in flight at a time: a new signal for a
    symbol is not acted on until that symbol's previous order has been fully
    resolved (filled, cancelled, rejected, etc.), tracked in ``self.orders``
    keyed by data feed and updated in ``notify_order()``. Without this guard,
    ``next()`` could re-submit an order on every bar while the broker is still
    processing the previous one.

    Params:

      - ``short_period`` (default 5): period of the fast/short SMA.
      - ``long_period`` (default 20): period of the slow/long SMA.
    """

    params = dict(
        short_period=5,
        long_period=20,
    )

    def __init__(self):
        # One set of indicators and one pending-order slot per data feed, so
        # every symbol is evaluated and traded independently.
        self.inds = {}
        self.orders = {}
        for data in self.datas:
            short_ma = bt.ind.SMA(data, period=self.p.short_period)
            long_ma = bt.ind.SMA(data, period=self.p.long_period)
            self.inds[data] = {
                "short_ma": short_ma,
                "long_ma": long_ma,
                "crossover": bt.ind.CrossOver(short_ma, long_ma),
            }
            # Currently pending order for this data (if any); prevents
            # stacking a new order while one is awaiting a broker response.
            self.orders[data] = None

        # Record each trade's opening/closing fills so we can report exact
        # entry/exit price and size (trade.size resets to 0 once closed, so
        # per-update history must be enabled to recover the original size).
        self.set_tradehistory(True)
        # Per-trade P&L records, appended on each trade close via
        # notify_trade(). Consumed by backtest/main.py to print a
        # trade-by-trade report at the end of the run.
        self.closed_trades = []

    def notify_order(self, order: bt.Order):
        """Called by backtrader whenever an order changes state."""
        if order.status in (order.Submitted, order.Accepted):
            # Still in flight; nothing to act on yet.
            return

        symbol = order.data._name or order.data._dataname
        if order.status == order.Completed:
            side = "BUY" if order.isbuy() else "SELL"
            logger.info(
                "[Order] %s %s completed: price=%.2f size=%s value=%.2f commission=%.2f",
                symbol, side, order.executed.price, order.executed.size,
                order.executed.value, order.executed.comm,
            )
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            logger.warning("[Order] %s %s: %s", symbol, order.getstatusname(), order.info)
        elif order.status == order.Expired:
            logger.info("[Order] %s expired", symbol)

        # The order has reached a final state; clear this symbol's guard so
        # the next signal in next() is allowed to submit a new one.
        self.orders[order.data] = None

    def notify_trade(self, trade):
        """Called by backtrader whenever a trade changes state; record the
        entry/exit detail and P&L when a trade closes."""
        if not trade.isclosed:
            return
        # trade.price stays at the average *entry* price after closing (it is
        # only updated while the position is increasing). The exit fill price
        # comes from the last recorded history event, and the entry size from
        # the first ("opening") history event.
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

    def next(self):
        # Evaluate and trade every data feed independently.
        for data in self.datas:
            self._handle_data(data)

    def _handle_data(self, data):
        symbol = data._name or data._dataname
        crossover = self.inds[data]["crossover"]

        # trading_session comes from the WebullData feed's extra
        # "trading_session" line (encoded as an int); decode it back to the
        # original string (e.g. "RTH", "PRE", "" for daily+ bars).
        session = decode_trading_session(data.trading_session[0])
        # High-frequency, per-bar diagnostic output; keep at DEBUG level to
        # avoid flooding logs during normal (INFO-level) runs.
        logger.debug(
            "%s %s close: %.2f open: %.2f high: %.2f low: %.2f session=%s",
            symbol, to_market_tz(data.datetime.datetime(0)), data.close[0],
            data.open[0], data.high[0], data.low[0], session,
        )

        # Don't stack a new signal on top of an order still awaiting a
        # broker response for this symbol.
        if self.orders[data] is not None:
            return

        position = self.getposition(data)
        if not position:
            if crossover > 0:  # golden cross: short SMA crossed above long SMA
                self.orders[data] = self.buy(data=data)
        elif crossover < 0:  # death cross: short SMA crossed below long SMA
            self.orders[data] = self.close(data=data)


# Convention used by backtest/main.py's dynamic strategy loader: pointing
# WEBULL_STRATEGY at this module's filename (without ".py") loads this class.
STRATEGY_CLASS = DualMovingAverageStrategy
