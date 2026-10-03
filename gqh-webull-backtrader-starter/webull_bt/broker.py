"""backtrader Broker backed by the Webull OpenAPI trading interface.

Follows the design of backtrader's official ``brokers/ibbroker.py``:
  - An ``OrderBase`` subclass (``WebullOrder``) carries the order and maps
    backtrader's order type/side/validity onto the Webull place-order
    request body.
  - A ``CommInfoBase`` subclass (``WebullCommInfo``) provides cost/value
    estimates; the real commission is settled by the broker, so this only
    ensures the Strategy's trade accounting doesn't error out.
  - ``WebullBroker`` maps backtrader's broker contract (buy/sell/cancel/
    getcash/getvalue/getposition/get_notification) onto the Webull trading
    API.

Key difference from ibbroker:
  IB relies on TWS push callbacks (orderStatus/execDetails/commissionReport)
  to update orders. The Webull trading API is request/response based, so
  this broker drives state transitions with a **background thread polling
  order details** instead, which is simpler and needs no extra store layer.

Risk warning: this broker submits real orders to Webull. Always validate the
full place/cancel/status-report flow in the Sandbox/Pre test environment
before connecting to production.
"""

from __future__ import annotations

import queue
import threading
import uuid
from datetime import date, datetime, timedelta

from backtrader import BrokerBase, Order, OrderBase
from backtrader.comminfo import CommInfoBase
from backtrader.position import Position
from backtrader.utils import date2num

from webull.trade.trade_client import TradeClient

from webull_bt.logging_utils import get_logger


logger = get_logger("broker")


# backtrader order type -> Webull order_type
_ORDER_TYPE_MAP = {
    None: "MARKET",
    Order.Market: "MARKET",
    Order.Limit: "LIMIT",
    Order.Stop: "STOP_LOSS",
    Order.StopLimit: "STOP_LOSS_LIMIT",
    Order.StopTrail: "TRAILING_STOP_LOSS",
    Order.Close: "MARKET_ON_CLOSE",
}

# Webull order status (see webull.trade.common.order_status.OrderStatus)
WB_SUBMITTED = "SUBMITTED"
WB_CANCELLED = "CANCELLED"
WB_FAILED = "FAILED"
WB_FILLED = "FILLED"
WB_PARTIAL_FILLED = "PARTIAL_FILLED"


def _first(mapping: dict, *keys, default=None):
    """Return the first present, non-empty value among the given keys.

    Used to tolerate field-naming differences across API responses.
    """
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def _to_float(value, default: float = 0.0) -> float:
    """Safely cast an API-returned string value to float."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class WebullCommInfo(CommInfoBase):
    """Commission info.

    The real commission is settled by Webull; this only provides an
    approximate cost/value calculation so the Strategy's trade accounting
    keeps working (same approach as ibbroker).
    """

    def getvaluesize(self, size, price):
        return abs(size) * price

    def getoperationcost(self, size, price):
        """Return the cash cost required for an operation."""
        return abs(size) * price


class WebullOrder(OrderBase):
    """Maps a backtrader order onto a Webull place-order request body.

    ``client_order_id`` uses a uuid hex (32 chars, within the API's 32-char
    limit) as the unique identifier for subsequent query/cancel calls.
    """

    def __init__(self, action: str, **kwargs):
        # Must set ordtype before super().__init__(); OrderBase relies on it.
        self.ordtype = self.Buy if action == "BUY" else self.Sell

        # Allow callers to override request fields via kwargs (e.g.
        # support_trading_session).
        self._extra_order_fields = kwargs.pop("order_fields", None) or {}

        super().__init__(**kwargs)

        self.client_order_id = uuid.uuid4().hex  # 32 chars, within API limit
        self.wb_order_id = None  # Webull-side order id (filled after placement)
        self.action = action
        # Cumulative filled quantity reported so far, used to compute the
        # incremental fill on each update.
        self._filled_size = 0.0
        # The last Webull status seen during polling, used to de-duplicate
        # notifications.
        self._last_wb_status = None
        # Rejection reason (filled in when placement fails), for the
        # strategy side to inspect.
        self.reject_reason = None

    def __str__(self):
        base = super().__str__()
        return "\n".join(
            [
                base,
                f"Ref: {self.ref}",
                f"client_order_id: {self.client_order_id}",
                f"wb_order_id: {self.wb_order_id}",
                f"Action: {self.action}",
                f"Size: {self.size}",
                f"OrderType: {_ORDER_TYPE_MAP.get(self.exectype)}",
            ]
        )

    def _time_in_force(self) -> str:
        """Map backtrader's ``valid`` onto Webull's ``time_in_force``.

        Webull stock orders only support DAY / GTC, with no IOC/GTD concept,
        so:
          - valid is None -> GTC (good till cancelled)
          - valid is a DAY timedelta -> DAY
          - anything else (specific date, etc.) -> DAY (conservative choice
            to avoid exceeding the API's semantics)
        """
        valid = self.valid
        if valid is None:
            return "GTC"
        if isinstance(valid, timedelta):
            return "DAY" if valid == self.DAY else "DAY"
        if isinstance(valid, (datetime, date)):
            return "DAY"
        if valid == 0:
            return "DAY"
        return "DAY"

    def to_request(self, market: str = "US", trading_session: str = "CORE") -> dict:
        """Build a single order object for the Webull ``new_orders`` request body.

        Field specification per the official Stock Trading documentation.
        """
        order_type = _ORDER_TYPE_MAP.get(self.exectype, "MARKET")

        req = {
            "client_order_id": self.client_order_id,
            "combo_type": "NORMAL",
            "symbol": self.data._name or self.data._dataname,
            "instrument_type": "EQUITY",
            "market": market,
            "order_type": order_type,
            "side": self.action,
            # API expects a string; quantity is always positive (direction
            # is conveyed by "side").
            "quantity": str(abs(self.size)),
            "entrust_type": "QTY",
            "time_in_force": self._time_in_force(),
            "support_trading_session": trading_session,
        }

        # Add price fields depending on order type.
        if order_type == "LIMIT":
            req["limit_price"] = str(self.price)
        elif order_type == "STOP_LOSS":
            req["stop_price"] = str(self.price)
        elif order_type == "STOP_LOSS_LIMIT":
            req["stop_price"] = str(self.price)
            req["limit_price"] = str(self.pricelimit)
        elif order_type == "TRAILING_STOP_LOSS":
            # Trailing stop: prefer amount, fall back to percentage.
            if self.trailamount is not None:
                req["trailing_type"] = "AMOUNT"
                req["trailing_stop_step"] = str(self.trailamount)
            elif self.trailpercent is not None:
                req["trailing_type"] = "PERCENTAGE"
                req["trailing_stop_step"] = str(self.trailpercent)
            # Trailing stop only supports DAY.
            req["time_in_force"] = "DAY"

        # Caller-supplied overrides (applied last, may override the defaults above).
        req.update(self._extra_order_fields)
        return req


class WebullBroker(BrokerBase):
    """Broker backed by the Webull OpenAPI trading interface.

    Parameters:

      - ``trade_client`` (required): an initialized
        ``webull.trade.trade_client.TradeClient``. Credentials and endpoint
        are the caller's responsibility; this broker only uses it to issue
        trading requests.

      - ``account_id`` (required): the trading account id, obtainable via
        ``trade_client.account_v2.get_account_list()``.

      - ``market`` (default "US"): the market to place orders in.

      - ``trading_session`` (default "CORE"): trading session, CORE=regular
        hours / ALL=including pre/post market / NIGHT=overnight session.

      - ``poll_interval`` (default 2.0): order status polling interval in
        seconds.

      - ``sync_positions`` (default True): whether to sync existing
        positions from Webull on startup.
    """

    params = (
        ("trade_client", None),
        ("account_id", None),
        ("market", "US"),
        ("trading_session", "CORE"),
        ("poll_interval", 2.0),
        ("sync_positions", True),
    )

    def __init__(self):
        super().__init__()

        if self.p.trade_client is None:
            raise ValueError(
                "missing trade_client: pass an initialized "
                "webull.trade.trade_client.TradeClient instance"
            )
        if not self.p.account_id:
            raise ValueError(
                "missing account_id: obtainable via "
                "trade_client.account_v2.get_account_list()"
            )

        self.startingcash = self.cash = 0.0
        self.startingvalue = self.value = 0.0

        self._lock_orders = threading.Lock()
        self.orderbyid: dict[str, WebullOrder] = {}  # client_order_id -> order
        self.positions: dict[str, Position] = {}  # symbol -> Position
        self.notifs = queue.Queue()

        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        super().start()
        self._stop_event.clear()

        # Initialize cash/value.
        self.startingcash = self.cash = self.getcash()
        self.startingvalue = self.value = self.getvalue()
        logger.info(
            "[Broker] started: account_id=%s cash=%.2f value=%.2f",
            self.p.account_id, self.cash, self.value,
        )

        if self.p.sync_positions:
            self._sync_positions()

        # Start the order status polling thread.
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="Webull-Broker-Poll", daemon=True
        )
        self._poll_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None
        super().stop()

    # ------------------------------------------------------------------ #
    # Cash and positions
    # ------------------------------------------------------------------ #
    def getcash(self) -> float:
        """Query available cash."""
        try:
            resp = self.p.trade_client.account_v2.get_account_balance(self.p.account_id)
        except Exception:
            logger.exception("[Broker] balance query raised an exception")
            return self.cash

        if getattr(resp, "status_code", None) != 200:
            logger.warning("[Broker] balance query failed: status=%s", getattr(resp, "status_code", None))
            return self.cash

        data = resp.json() or {}
        # Prefer the account-level cash figure; fall back to the currency breakdown.
        cash = _first(data, "total_cash_balance")
        if cash is None:
            assets = data.get("account_currency_assets") or []
            if assets:
                cash = _first(assets[0], "cash_balance", "settled_cash", "buying_power")

        self.cash = _to_float(cash, self.cash)
        return self.cash

    def getvalue(self, datas=None) -> float:
        """Query total account value (net liquidation value)."""
        try:
            resp = self.p.trade_client.account_v2.get_account_balance(self.p.account_id)
        except Exception:
            logger.exception("[Broker] value query raised an exception")
            return self.value

        if getattr(resp, "status_code", None) != 200:
            return self.value

        data = resp.json() or {}
        value = _first(data, "total_net_liquidation_value", "total_asset")
        if value is None:
            assets = data.get("account_currency_assets") or []
            if assets:
                value = _first(assets[0], "net_liquidation_value", "market_value")

        self.value = _to_float(value, self.value)
        return self.value

    def _sync_positions(self):
        """Fetch current positions from Webull into the local Position table."""
        try:
            resp = self.p.trade_client.account_v2.get_account_position(self.p.account_id)
        except Exception:
            logger.exception("[Broker] position sync raised an exception")
            return

        if getattr(resp, "status_code", None) != 200:
            logger.warning("[Broker] position sync failed: status=%s", getattr(resp, "status_code", None))
            return

        payload = resp.json() or []
        # The response may be a plain list or wrapped in a dict.
        items = payload if isinstance(payload, list) else (
            payload.get("positions") or payload.get("items") or []
        )

        count = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = _first(item, "symbol", "ticker")
            if not symbol:
                continue
            size = _to_float(_first(item, "quantity", "position", "total_quantity"))
            price = _to_float(_first(item, "cost_price", "avg_cost", "average_cost", "last_price"))
            self.positions[symbol] = Position(size=size, price=price)
            count += 1
            logger.debug("[Broker] synced position: %s size=%s price=%s", symbol, size, price)

        logger.info("[Broker] position sync complete: %d symbol(s)", count)

    def getposition(self, data, clone=True):
        symbol = data._name or data._dataname
        pos = self.positions.setdefault(symbol, Position())
        return pos.clone() if clone else pos

    def getcommissioninfo(self, data):
        return WebullCommInfo(mult=1.0, stocklike=True)

    # ------------------------------------------------------------------ #
    # Order placement / cancellation
    # ------------------------------------------------------------------ #
    def buy(self, owner, data, size, price=None, plimit=None,
            exectype=None, valid=None, tradeid=0, **kwargs):
        order = self._makeorder(
            "BUY", owner, data, size, price, plimit, exectype, valid, tradeid, **kwargs
        )
        return self.submit(order)

    def sell(self, owner, data, size, price=None, plimit=None,
             exectype=None, valid=None, tradeid=0, **kwargs):
        order = self._makeorder(
            "SELL", owner, data, size, price, plimit, exectype, valid, tradeid, **kwargs
        )
        return self.submit(order)

    def _makeorder(self, action, owner, data, size, price=None, plimit=None,
                   exectype=None, valid=None, tradeid=0, **kwargs) -> WebullOrder:
        order = WebullOrder(
            action,
            owner=owner, data=data, size=size, price=price, pricelimit=plimit,
            exectype=exectype, valid=valid, tradeid=tradeid,
            **kwargs,
        )
        order.addcomminfo(self.getcommissioninfo(data))
        return order

    def submit(self, order: WebullOrder) -> WebullOrder:
        """Submit an order to Webull."""
        order.submit(self)
        self.notify(order)  # report Submitted first, matching backtrader's status semantics

        req = order.to_request(
            market=self.p.market, trading_session=self.p.trading_session
        )
        logger.debug("[Broker] placing order: %s", req)

        try:
            resp = self.p.trade_client.order_v2.place_order(
                account_id=self.p.account_id, new_orders=[req]
            )
        except Exception as err:
            # Business validation failures (insufficient buying power,
            # non-tradable symbol, etc.) are raised as ServerException;
            # extract the error_code/message onto the order so the strategy
            # side can inspect the reason.
            order.reject_reason = self._describe_error(err)
            logger.error(
                "[Broker] order rejected: client_order_id=%s %s",
                order.client_order_id, order.reject_reason,
            )
            order.reject(self)
            self.notify(order)
            return order

        status = getattr(resp, "status_code", None)
        if status != 200:
            order.reject_reason = (
                f"http_status={status} body={getattr(resp, 'text', '')}"
            )
            logger.error("[Broker] order rejected: client_order_id=%s %s",
                         order.client_order_id, order.reject_reason)
            order.reject(self)
            self.notify(order)
            return order

        body = resp.json()
        order.wb_order_id = self._extract_order_id(body)
        logger.info(
            "[Broker] order placed: client_order_id=%s wb_order_id=%s",
            order.client_order_id, order.wb_order_id,
        )
        logger.debug("[Broker] place_order response: %s", body)

        with self._lock_orders:
            self.orderbyid[order.client_order_id] = order

        order.accept(self)
        self.notify(order)
        return order

    @staticmethod
    def _describe_error(err: Exception) -> str:
        """Format an SDK exception into a single readable error description.

        The Webull SDK's ServerException carries error_code / error_msg /
        http_status / request_id; the business failure reason (e.g.
        insufficient buying power) lives in error_code and needs to be
        surfaced.
        """
        code = getattr(err, "error_code", None)
        msg = getattr(err, "error_msg", None)
        http_status = getattr(err, "http_status", None)
        request_id = getattr(err, "request_id", None)
        if code or msg:
            return (
                f"error_code={code} message={msg} "
                f"http_status={http_status} request_id={request_id}"
            )
        return f"{type(err).__name__}: {err}"

    @staticmethod
    def _extract_order_id(body):
        """Extract the Webull order id from a place-order response
        (tolerating multiple response shapes)."""
        if isinstance(body, dict):
            oid = _first(body, "order_id", "orderId")
            if oid:
                return oid
            orders = body.get("orders") or body.get("new_orders")
            if isinstance(orders, list) and orders and isinstance(orders[0], dict):
                return _first(orders[0], "order_id", "orderId")
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            return _first(body[0], "order_id", "orderId")
        return None

    def cancel(self, order: WebullOrder):
        """Cancel an order."""
        with self._lock_orders:
            known = self.orderbyid.get(order.client_order_id)
        if known is None:
            return  # not an order managed by this broker
        if order.status == Order.Cancelled:
            return  # already cancelled

        logger.info("[Broker] cancelling order: client_order_id=%s", order.client_order_id)
        try:
            resp = self.p.trade_client.order_v2.cancel_order(
                account_id=self.p.account_id, client_order_id=order.client_order_id
            )
        except Exception:
            logger.exception("[Broker] cancel request raised an exception: client_order_id=%s", order.client_order_id)
            return

        status = getattr(resp, "status_code", None)
        if status != 200:
            logger.warning(
                "[Broker] cancel request failed: status=%s body=%s",
                status, getattr(resp, "text", ""),
            )
            return
        logger.info("[Broker] cancel request accepted: client_order_id=%s", order.client_order_id)
        # The actual state change is confirmed by polling; not marked
        # cancelled here directly.

    def orderstatus(self, order):
        with self._lock_orders:
            o = self.orderbyid.get(order.client_order_id, order)
        return o.status

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    def notify(self, order):
        self.notifs.put(order.clone())

    def get_notification(self):
        try:
            return self.notifs.get(False)
        except queue.Empty:
            return None

    def next(self):
        self.notifs.put(None)  # notification boundary marker

    # ------------------------------------------------------------------ #
    # Order status polling
    # ------------------------------------------------------------------ #
    def _poll_loop(self):
        """Background loop polling in-flight orders to drive backtrader's
        order state transitions."""
        while not self._stop_event.is_set():
            try:
                self._poll_open_orders()
            except Exception:
                logger.exception("[Broker] order polling raised an exception")
            self._stop_event.wait(self.p.poll_interval)

    def _poll_open_orders(self):
        """Iterate over still-alive orders, query their details, and update state."""
        with self._lock_orders:
            pending = [o for o in self.orderbyid.values() if o.alive()]

        for order in pending:
            detail = self._fetch_order_detail(order.client_order_id)
            if detail is None:
                continue
            self._update_order(order, detail)

    def _fetch_order_detail(self, client_order_id: str):
        """Query the detail of a single order."""
        logger.debug("[Broker] fetching order detail: client_order_id=%s", client_order_id)
        try:
            resp = self.p.trade_client.order_v2.get_order_detail(
                account_id=self.p.account_id, client_order_id=client_order_id
            )
        except Exception:
            logger.exception("[Broker] order detail query raised an exception: %s", client_order_id)
            return None

        if getattr(resp, "status_code", None) != 200:
            logger.warning(
                "[Broker] order detail query failed: client_order_id=%s status=%s",
                client_order_id, getattr(resp, "status_code", None),
            )
            return None

        body = resp.json()
        if isinstance(body, list):
            body = body[0] if body else None
        return body if isinstance(body, dict) else None

    def _update_order(self, order: WebullOrder, detail: dict):
        """Update the backtrader order state based on the order detail and
        notify the strategy."""
        wb_status = str(
            _first(detail, "order_status", "status", default="")
        ).upper().replace(" ", "_")

        filled = _to_float(
            _first(detail, "filled_quantity", "filled_qty", "cumulative_quantity")
        )
        avg_price = _to_float(
            _first(detail, "avg_filled_price", "average_price", "avg_price", "filled_price")
        )

        # Skip entirely when neither the status nor the fill quantity has
        # changed, to avoid spamming duplicate notifications.
        has_new_fill = filled > order._filled_size and avg_price > 0
        if wb_status == order._last_wb_status and not has_new_fill:
            logger.debug(
                "[Broker] order status unchanged, skipping: client_order_id=%s status=%s",
                order.client_order_id, wb_status,
            )
            return
        order._last_wb_status = wb_status

        logger.info(
            "[Broker] order status: client_order_id=%s status=%s filled=%s avg_price=%s",
            order.client_order_id, wb_status, filled, avg_price,
        )

        # Register the fill detail first if there is a new fill.
        if has_new_fill:
            self._register_execution(order, filled, avg_price)

        # Positive matching by status; no fallback via else.
        if wb_status == WB_FILLED:
            order.completed()
            self.notify(order)
        elif wb_status == WB_PARTIAL_FILLED:
            order.partial()
            self.notify(order)
        elif wb_status == WB_CANCELLED:
            order.cancel()
            self.notify(order)
        elif wb_status == WB_FAILED:
            order.reject(self)
            self.notify(order)
        elif wb_status == WB_SUBMITTED:
            if order.status != Order.Accepted:
                order.accept(self)
                self.notify(order)
        else:
            logger.warning(
                "[Broker] unrecognized order status: client_order_id=%s status=%r",
                order.client_order_id, wb_status,
            )

    def _register_execution(self, order: WebullOrder, filled: float, price: float):
        """Register the newly filled quantity against the order and position."""
        # Incremental (signed) fill quantity for this update.
        delta = filled - order._filled_size
        order._filled_size = filled
        size = delta if order.isbuy() else -delta

        symbol = order.data._name or order.data._dataname
        position = self.positions.setdefault(symbol, Position())
        pprice_orig = position.price
        psize, pprice, opened, closed = position.update(size, price)

        comminfo = order.comminfo
        closedvalue = comminfo.getoperationcost(closed, pprice_orig) if closed else 0.0
        openedvalue = comminfo.getoperationcost(opened, price) if opened else 0.0
        # The real commission is settled by the broker; treated as 0 here,
        # which does not affect state transitions.
        closedcomm = 0.0
        openedcomm = 0.0
        pnl = comminfo.profitandloss(-closed, pprice_orig, price) if closed else 0.0

        # In live trading, margin is controlled by the broker; use the
        # latest price as a placeholder (same approach as ibbroker).
        margin = order.data.close[0] if len(order.data) else price

        order.execute(
            date2num(datetime.now()), size, price,
            closed, closedvalue, closedcomm,
            opened, openedvalue, openedcomm,
            margin, pnl,
            psize, pprice,
        )
        logger.info(
            "[Broker] execution registered: %s size=%s price=%s -> position size=%s price=%s",
            symbol, size, price, psize, pprice,
        )
