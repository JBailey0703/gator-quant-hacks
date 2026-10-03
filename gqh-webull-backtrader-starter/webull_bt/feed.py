"""backtrader data feed backed by the Webull OpenAPI.

Follows the design of backtrader's official ``feeds/yahoo.py``
(``YahooFinanceData``): the online fetch logic lives in ``start()``, and the
bar-by-bar feed logic lives in ``_load()``. Unlike the Yahoo feed, which
disguises the data as CSV to reuse the CSV parser, the Webull endpoint
returns JSON, so this subclasses ``bt.feed.DataBase`` directly and drives it
with an in-memory queue for a more straightforward implementation.

A single ``WebullData`` supports two modes via the ``live`` parameter:
  - ``live=False`` (default): backtest mode, ``start()`` fetches N bars once.
  - ``live=True``: simulated live mode, a background thread **polls** the
    historical bars endpoint every ``poll_interval`` seconds and feeds newly
    closed bars to the strategy.

``WebullLiveData`` is a convenience subclass for ``live=True``, equivalent to
``WebullData(live=True)``.

Timezone conversion and trading-session encode/decode helpers live in
``webull_bt.timeutils``; see examples/ for usage examples.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import backtrader as bt
from backtrader.utils import date2num

from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.data_client import DataClient

from webull_bt.logging_utils import get_logger
from webull_bt.timeutils import encode_trading_session, parse_bar_time


logger = get_logger("feed")


# ---------------------------------------------------------------------------- #
# Bar data structure
# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WebullBar:
    """A single bar, with all fields converted to types backtrader can use directly."""

    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trading_session: str = ""

    @classmethod
    def from_api(cls, record: dict) -> "WebullBar":
        """Build a WebullBar from a single record returned by the historical
        bars endpoint.

        Each record has the shape:
            {"time": "...", "open": "1.33", "close": "1.33",
             "high": "1.33", "low": "1.33", "volume": "10",
             "trading_session": "RTH"}
        OHLCV fields are strings and must be explicitly cast to float.
        ``trading_session`` is not documented for every timespan (daily bars
        return an empty string) so it is read defensively.
        """
        return cls(
            datetime=parse_bar_time(record["time"]),
            open=float(record["open"]),
            high=float(record["high"]),
            low=float(record["low"]),
            close=float(record["close"]),
            volume=float(record.get("volume", 0) or 0),
            trading_session=record.get("trading_session") or "",
        )

    def datetime_utc_naive(self) -> datetime:
        """Return a naive UTC datetime, suitable for backtrader's date2num.

        backtrader internally works with naive datetimes (plus an optional tz
        parameter), so this normalizes to UTC and strips tzinfo to avoid
        double-offset issues.
        """
        return self.datetime.astimezone(timezone.utc).replace(tzinfo=None)


# Webull timespan -> (backtrader TimeFrame, compression)
# Used to auto-infer timeframe/compression when not explicitly set, which
# helps with resampling scenarios.
_TIMESPAN_TO_TIMEFRAME = {
    "M1": (bt.TimeFrame.Minutes, 1),
    "M5": (bt.TimeFrame.Minutes, 5),
    "M15": (bt.TimeFrame.Minutes, 15),
    "M30": (bt.TimeFrame.Minutes, 30),
    "M60": (bt.TimeFrame.Minutes, 60),
    "M120": (bt.TimeFrame.Minutes, 120),
    "M240": (bt.TimeFrame.Minutes, 240),
    "D": (bt.TimeFrame.Days, 1),
    "W": (bt.TimeFrame.Weeks, 1),
    "M": (bt.TimeFrame.Months, 1),
    "Y": (bt.TimeFrame.Years, 1),
}


def _datetime_to_epoch_ms(value: datetime | None) -> int | None:
    """Convert a datetime to a UTC epoch timestamp in milliseconds.

    Naive datetimes are treated as UTC. The backtest entry point normally
    supplies timezone-aware values from WEBULL_FROMDATE/WEBULL_TODATE.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _locate_price_records(payload) -> list:
    """Locate the list of price records within a historical bars response.

    The single-symbol endpoint ``/market-data/stocks/bars/get`` response can
    take two common shapes:
      1. {"result": [{"symbol":..., "result":[<price records>]}]}
      2. a plain list of price records [{time, open, ...}, ...]
    Both shapes are handled here to avoid failing the whole parse due to
    structural differences.
    """
    # Shape 2: top level is directly a list of price records
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and "time" in payload[0]:
            return payload
        # Alternate form of shape 1: top level is a list of SymbolData
        records: list = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("result"), list):
                records.extend(item["result"])
        if records:
            return records
        return payload

    # Shape 1: {"result": [...]}
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            # result can be either price records directly, or SymbolData wrappers
            if result and isinstance(result[0], dict) and "time" in result[0]:
                return result
            records = []
            for item in result:
                if isinstance(item, dict) and isinstance(item.get("result"), list):
                    records.extend(item["result"])
            return records

    return []


class WebullData(bt.feed.DataBase):
    """Fetches bars from the Webull OpenAPI and feeds them to backtrader.

    The ``live`` parameter switches between backtest and simulated live mode;
    both modes share the same enum validation, bar parsing, and lines
    population logic.

    In addition to the standard OHLCV/datetime lines, this feed exposes a
    ``trading_session`` line carrying the Webull-reported session for each
    bar (PRE/RTH/ATH/OVN), encoded as an int. In ``next()``, decode it with::

        from webull_bt.timeutils import decode_trading_session
        session = decode_trading_session(self.data.trading_session[0])

    Daily bars and above do not carry a session (the API returns an empty
    string), which decodes to "" (empty session, encoded as 0).

    Common parameters:

      - ``dataname``: security symbol, e.g. "AAPL" (backtrader's dataname).

      - ``data_client`` (required): an initialized
        ``webull.data.data_client.DataClient``. Credentials and client
        construction (app_key/app_secret/endpoint, etc.) are the caller's
        responsibility; this feed only uses it to issue market data requests.

      - ``category`` (default "US_STOCK"): security type, see the
        ``Category`` enum names.

      - ``timespan`` (default "M1"): bar granularity, see the ``Timespan``
        enum names (M1/M5/M15/M30/M60/M120/M240/D/W/M/Y).

      - ``trading_sessions`` (default None): trading session(s), e.g.
        "PRE,RTH,ATH,OVN".

      - ``live`` (default False): False=backtest; True=simulated live
        (background polling).

    Backtest-only parameters (live=False):

      - ``count`` (default 200): number of bars to fetch in one shot, max
        1200 (max 1650 for M1).

      - ``fromdate`` / ``todate``: standard backtrader parameters for local
        time filtering; only bars within [fromdate, todate] are fed to the
        strategy.

    Live-only parameters (live=True):

      - ``poll_interval`` (default 5.0): polling interval in seconds. Choose
        a sensible value based on bar granularity and API rate limits.

      - ``fetch_count`` (default 20): number of bars fetched per poll, large
        enough to cover any bars that may have appeared between polls, to
        avoid missing bars.

      - ``backfill`` (default 0): number of recent historical bars to
        backfill on startup (to warm up indicators). 0 means no backfill,
        only feed bars newer than "now"; >0 fetches that many historical
        bars first, then continues polling for new ones.

      - ``qcheck`` (default 0.5): wait interval (seconds) backtrader uses
        when polling this feed's queue.
    """

    lines = ("trading_session",)

    params = (
        ("data_client", None),
        ("category", "US_STOCK"),
        ("timespan", "M1"),
        ("trading_sessions", None),
        ("live", False),
        # Backtest mode
        ("count", 200),
        # Live mode
        ("poll_interval", 5.0),
        ("fetch_count", 20),
        ("backfill", 0),
        ("qcheck", 0.5),
    )

    def __init__(self):
        super().__init__()
        # data_client is required: credentials/client construction are the
        # caller's responsibility.
        if self.p.data_client is None:
            raise ValueError(
                "missing data_client: pass an initialized "
                "webull.data.data_client.DataClient instance"
            )
        # Validate enum parameters early to surface config errors ASAP
        # (positive whitelist match, no fallback via else).
        if self.p.category not in Category.__members__:
            raise ValueError(
                f"invalid category: {self.p.category!r}, "
                f"allowed values: {list(Category.__members__)}"
            )
        if self.p.timespan not in Timespan.__members__:
            raise ValueError(
                f"invalid timespan: {self.p.timespan!r}, "
                f"allowed values: {list(Timespan.__members__)}"
            )

        # Auto-infer timeframe/compression from timespan when not explicitly set.
        tf_comp = _TIMESPAN_TO_TIMEFRAME.get(self.p.timespan)
        if tf_comp is not None:
            self.p.timeframe, self.p.compression = tf_comp

        self._data_client: DataClient | None = None
        # Queue of bars pending delivery. Filled once in backtest mode;
        # continuously appended by the background thread in live mode.
        self._q: "queue.Queue[WebullBar]" = queue.Queue()
        # The last bar timestamp delivered to backtrader, to enforce strict
        # monotonic ordering.
        self._last_loaded_dtnum: float | None = None

        # Live-mode only
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        # Timestamp (tz-aware) of the most recently enqueued bar, used for
        # de-duplication (only enqueue newer bars).
        self._last_enqueued_dt: datetime | None = None

    def islive(self) -> bool:
        # True in live mode, which tells backtrader to disable
        # preload/runonce and drive bar-by-bar.
        return bool(self.p.live)

    def haslivedata(self) -> bool:
        return self.p.live and not self._q.empty()

    def start(self):
        super().start()
        self._last_loaded_dtnum = None
        with self._q.mutex:  # clear the queue (in case the instance is re-run)
            self._q.queue.clear()

        self._data_client = self.p.data_client

        if self.p.live:
            self._start_live()
        else:
            self._start_backtest()

    # ------------------------------------------------------------------ #
    # Backtest mode
    # ------------------------------------------------------------------ #
    def _start_backtest(self):
        """Fetch historical data once and fill the queue."""
        start_time = _datetime_to_epoch_ms(self.p.fromdate)
        end_time = _datetime_to_epoch_ms(self.p.todate)
        logger.debug(
            "[Backtest] requesting bars: symbols=%s category=%s timespan=%s "
            "count=%s real_time_required=%s trading_sessions=%s "
            "start_time=%s end_time=%s",
            [self.p.dataname],
            self.p.category,
            self.p.timespan,
            self.p.count,
            False,
            self.p.trading_sessions,
            start_time,
            end_time,
        )
        response = self._data_client.market_data.get_batch_history_bar(
            symbols=[self.p.dataname],
            category=self.p.category,
            timespan=self.p.timespan,
            count=str(self.p.count),
            real_time_required=False,
            trading_sessions=self.p.trading_sessions,
            start_time=start_time,
            end_time=end_time,
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            body = getattr(response, "text", "")
            logger.error(
                "[Backtest] bars request failed: symbol=%s status=%s body=%s",
                self.p.dataname, status, body,
            )
            raise RuntimeError(
                f"Webull historical bars request failed: symbol={self.p.dataname} "
                f"status={status} body={body}"
            )

        records = _locate_price_records(response.json())
        bars = [WebullBar.from_api(r) for r in records if r]
        # The API returns "the most recent N bars", usually in descending
        # time order; backtrader requires ascending order, so sort here
        # rather than relying on the API's return order.
        bars.sort(key=lambda b: b.datetime)
        for bar in bars:
            self._q.put(bar)

        if bars:
            logger.info(
                "[Backtest] request succeeded: status=%s parsed %d bars, range %s ~ %s",
                status, len(bars), bars[0].datetime, bars[-1].datetime,
            )
        else:
            logger.warning("[Backtest] request succeeded but no bars parsed: status=%s", status)

    # ------------------------------------------------------------------ #
    # Live mode (polling)
    # ------------------------------------------------------------------ #
    def _start_live(self):
        """Optionally backfill historical bars, then start the polling thread."""
        self._stop_event.clear()
        self._last_enqueued_dt = None

        # backfill>0: fetch the most recent `backfill` bars on startup to warm up.
        if self.p.backfill and self.p.backfill > 0:
            try:
                bars = self._fetch_bars(int(self.p.backfill), context="Backfill")
            except Exception:
                logger.exception("[Live-Backfill] backfill request raised an exception")
                bars = []
            for bar in bars:
                self._q.put(bar)
                self._last_enqueued_dt = bar.datetime
            if bars:
                logger.info("[Live-Backfill] enqueued %d backfill bars", len(bars))

        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="Webull-Poll", daemon=True
        )
        self._poll_thread.start()

    def _fetch_bars(self, count: int, context: str = "Poll") -> list[WebullBar]:
        """Issue one historical bars request, returning bars sorted ascending
        by time.

        :param count: number of bars to fetch in this request
        :param context: log tag identifying the call site (e.g. "Backfill"/"Poll")
        """
        logger.debug(
            "[Live-%s] requesting bars: symbols=%s category=%s timespan=%s "
            "count=%s real_time_required=%s trading_sessions=%s",
            context,
            [self.p.dataname],
            self.p.category,
            self.p.timespan,
            count,
            False,
            self.p.trading_sessions,
        )
        response = self._data_client.market_data.get_batch_history_bar(
            symbols=[self.p.dataname],
            category=self.p.category,
            timespan=self.p.timespan,
            count=str(count),
            real_time_required=False,
            trading_sessions=self.p.trading_sessions,
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            logger.warning(
                "[Live-%s] request failed: symbol=%s status=%s body=%s",
                context, self.p.dataname, status, getattr(response, "text", ""),
            )
            return []
        records = _locate_price_records(response.json())
        bars = [WebullBar.from_api(r) for r in records if r]
        bars.sort(key=lambda b: b.datetime)
        if bars:
            logger.debug(
                "[Live-%s] request succeeded: status=%s parsed %d bars, range %s ~ %s",
                context, status, len(bars), bars[0].datetime, bars[-1].datetime,
            )
            # Log the detail of every bar returned by this request.
            for bar in bars:
                logger.debug(
                    "[Live-%s] bar %s O=%.4f H=%.4f L=%.4f C=%.4f V=%.0f",
                    context, bar.datetime,
                    bar.open, bar.high, bar.low, bar.close, bar.volume,
                )
        else:
            logger.debug("[Live-%s] request succeeded but no bars: status=%s", context, status)
        return bars

    def _poll_loop(self):
        """Background polling loop: periodically fetch bars and enqueue any
        newer than what's already enqueued."""
        while not self._stop_event.is_set():
            try:
                bars = self._fetch_bars(int(self.p.fetch_count), context="Poll")
            except Exception:
                # A single failed fetch should not stop the live loop; retry
                # on the next cycle.
                logger.exception("[Live-Poll] polling request raised an exception")
                bars = []

            new_count = 0
            for bar in bars:
                if (
                    self._last_enqueued_dt is None
                    or bar.datetime > self._last_enqueued_dt
                ):
                    self._q.put(bar)
                    self._last_enqueued_dt = bar.datetime
                    new_count += 1

            if new_count:
                logger.info(
                    "[Live-Poll] enqueued %d new bar(s), latest time %s",
                    new_count, self._last_enqueued_dt,
                )
            else:
                logger.debug("[Live-Poll] no new bars this cycle")

            # Use an interruptible wait so stop() can return promptly.
            self._stop_event.wait(self.p.poll_interval)

    def stop(self):
        if self.p.live:
            self._stop_event.set()
            if self._poll_thread is not None:
                self._poll_thread.join(timeout=2.0)
                self._poll_thread = None
        super().stop()

    # ------------------------------------------------------------------ #
    # Shared bar-by-bar loading
    # ------------------------------------------------------------------ #
    def _load(self) -> bool | None:
        # Backtest contract: True=loaded one bar / False=exhausted, done.
        # Live contract: True=loaded one bar / None=no data yet (retry later)
        #   / False=done.
        while True:
            bar = self._next_bar()
            if bar is None:
                # Backtest: empty queue means done (False).
                # Live: empty queue means "nothing yet" (None).
                return None if self.p.live else False

            dtnum = date2num(bar.datetime_utc_naive())

            # Apply fromdate/todate local filtering in backtest mode.
            if not self.p.live:
                if dtnum < self.fromdate:
                    continue  # earlier than start, discard and try next
                if dtnum > self.todate:
                    return False  # already ascending, past the cutoff means done

            # Enforce strict monotonic time; skip any bar that doesn't advance.
            if self._last_loaded_dtnum is not None and dtnum <= self._last_loaded_dtnum:
                if self.p.live:
                    return None  # live: let backtrader retry later
                continue  # backtest: try the next bar

            self._last_loaded_dtnum = dtnum

            self.lines.datetime[0] = dtnum
            self.lines.open[0] = bar.open
            self.lines.high[0] = bar.high
            self.lines.low[0] = bar.low
            self.lines.close[0] = bar.close
            self.lines.volume[0] = bar.volume
            self.lines.openinterest[0] = 0.0
            self.lines.trading_session[0] = encode_trading_session(bar.trading_session)
            return True

    def _next_bar(self) -> WebullBar | None:
        """Pop the next bar from the queue; return None if unavailable.

        Live mode blocks up to ``qcheck`` seconds to avoid busy-waiting;
        backtest mode never blocks and treats an empty queue as "done".
        """
        try:
            if self.p.live:
                timeout = self.p.qcheck if self.p.qcheck and self.p.qcheck > 0 else None
                if timeout:
                    return self._q.get(timeout=timeout)
                return self._q.get_nowait()
            return self._q.get_nowait()
        except queue.Empty:
            return None


class WebullLiveData(WebullData):
    """Convenience subclass equivalent to ``WebullData(live=True)``: enables
    simulated live polling by default."""

    params = (("live", True),)
