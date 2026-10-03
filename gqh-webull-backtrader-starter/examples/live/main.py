"""Run simulated-live trading with the Webull OpenAPI data feed, optionally
connected to the real trading broker.

Configuration is read from live/.env (never hardcode credentials).

    uv run python live/main.py
or
    python live/main.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the project importable when run as a script (python examples/live/main.py):
#   - PROJECT_ROOT hosts the reusable ``webull_bt`` package.
#   - the sibling ``strategies`` dir lets the example strategies be imported.
_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(_HERE.parent.parent / "strategies"))

import backtrader as bt
from dotenv import load_dotenv

from webull.core.client import ApiClient
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

from webull_bt.broker import WebullBroker
from webull_bt.feed import WebullLiveData
from webull_bt.logging_utils import get_logger, setup_logging

from dual_ma import DualMovingAverageStrategy


logger = get_logger("live.main")


def resolve_env_credentials() -> tuple[str, str, str]:
    """Resolve (app_key, app_secret, api_endpoint) based on WEBULL_ENV.

    WEBULL_ENV selects which credential/endpoint set to use:
      - "sandbox": WEBULL_SANDBOX_APP_KEY / WEBULL_SANDBOX_APP_SECRET /
        WEBULL_SANDBOX_API_ENDPOINT
      - "prod" or unset (default): WEBULL_APP_KEY / WEBULL_APP_SECRET /
        WEBULL_API_ENDPOINT

    Both market data and trading use this same resolved credential/endpoint
    set unless WEBULL_TRADE_ENDPOINT overrides the endpoint for trading.
    """
    env = os.environ.get("WEBULL_ENV", "prod").lower()

    if env == "sandbox":
        app_key = os.environ.get("WEBULL_SANDBOX_APP_KEY")
        app_secret = os.environ.get("WEBULL_SANDBOX_APP_SECRET")
        api_endpoint = os.environ.get(
            "WEBULL_SANDBOX_API_ENDPOINT", "us-sandbox-api.pre.webullbroker.com"
        )
        missing_hint = "WEBULL_SANDBOX_APP_KEY and WEBULL_SANDBOX_APP_SECRET"
    else:
        app_key = os.environ.get("WEBULL_APP_KEY")
        app_secret = os.environ.get("WEBULL_APP_SECRET")
        api_endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.webull.com")
        missing_hint = "WEBULL_APP_KEY and WEBULL_APP_SECRET"

    if not app_key or not app_secret:
        raise SystemExit(
            f"missing credentials for WEBULL_ENV={env!r}: set {missing_hint} "
            "in live/.env or the environment"
        )

    logger.info("[Env] using WEBULL_ENV=%s api_endpoint=%s", env, api_endpoint)
    return app_key, app_secret, api_endpoint


def build_data_client() -> DataClient:
    """Read credentials from the environment and build a DataClient
    (credentials must never be hardcoded)."""
    app_key, app_secret, api_endpoint = resolve_env_credentials()
    region_id = os.environ.get("WEBULL_REGION_ID", "us")

    api_client = ApiClient(app_key, app_secret, region_id)
    api_client.add_endpoint(region_id, api_endpoint)
    return DataClient(api_client)


def build_live_feed(data_client: DataClient) -> WebullLiveData:
    """Build the simulated-live feed: polls the historical bars endpoint in
    the background to drive the strategy."""
    # live mode trades a single symbol; if WEBULL_SYMBOLS has multiple
    # comma-separated symbols, only the first one is used here.
    symbol = os.environ.get("WEBULL_SYMBOLS", "AAPL").split(",")[0].strip().upper()
    category = os.environ.get("WEBULL_CATEGORY", "US_STOCK")
    timespan = os.environ.get("WEBULL_TIMESPAN", "M1")
    # Not exposed via .env for now; adjust these defaults directly if needed.
    poll_interval = 5.0
    fetch_count = 20
    backfill = 0

    return WebullLiveData(
        dataname=symbol,
        data_client=data_client,
        category=category,
        timespan=timespan,
        poll_interval=poll_interval,
        fetch_count=fetch_count,
        backfill=backfill,
        trading_sessions="PRE,RTH,ATH,OVN",
    )


def resolve_env_account_id() -> str | None:
    """Resolve the configured account id based on WEBULL_ENV.

      - "sandbox": WEBULL_SANDBOX_ACCOUNT_ID
      - "prod" or unset (default): WEBULL_ACCOUNT_ID
    """
    env = os.environ.get("WEBULL_ENV", "prod").lower()
    if env == "sandbox":
        return os.environ.get("WEBULL_SANDBOX_ACCOUNT_ID")
    return os.environ.get("WEBULL_ACCOUNT_ID")


def build_broker() -> WebullBroker:
    """Build the Webull trading broker.

    Warning: this broker submits real orders. If the account id is not
    configured for the current WEBULL_ENV, the first cash account in the
    account list is auto-selected; it is recommended to set the account id
    explicitly to avoid trading on the wrong account.
    """
    app_key, app_secret, api_endpoint = resolve_env_credentials()
    region_id = os.environ.get("WEBULL_REGION_ID", "us")
    # The trading API endpoint may differ from the market data endpoint;
    # WEBULL_TRADE_ENDPOINT overrides it when set.
    trade_endpoint = os.environ.get("WEBULL_TRADE_ENDPOINT", api_endpoint)

    api_client = ApiClient(app_key, app_secret, region_id)
    api_client.add_endpoint(region_id, trade_endpoint)
    trade_client = TradeClient(api_client)

    account_id = resolve_env_account_id()
    if not account_id:
        account_id = _pick_account_id(trade_client)
        logger.info("[Live] account id not set for the current WEBULL_ENV, "
                   "auto-selected account: %s", account_id)

    return WebullBroker(
        trade_client=trade_client,
        account_id=account_id,
        market=os.environ.get("WEBULL_TRADE_MARKET", "US"),
        trading_session=os.environ.get("WEBULL_TRADING_SESSION", "CORE"),
        # Not exposed via .env for now; adjust this default directly if needed.
        poll_interval=2.0,
    )


def _pick_account_id(trade_client: TradeClient) -> str:
    """Pick a usable account from the account list (prefer a cash account)."""
    resp = trade_client.account_v2.get_account_list()
    if getattr(resp, "status_code", None) != 200:
        raise SystemExit(f"failed to query account list: status={getattr(resp, 'status_code', None)}")

    accounts = resp.json() or []
    if not accounts:
        raise SystemExit("account list is empty, please confirm trading access has been granted")

    for acct in accounts:
        if acct.get("account_class") == "INDIVIDUAL_CASH":
            return acct["account_id"]
    return accounts[0]["account_id"]


def run_live() -> None:
    data_client = build_data_client()

    cerebro = bt.Cerebro()
    cerebro.addstrategy(DualMovingAverageStrategy)
    cerebro.adddata(build_live_feed(data_client))
    cerebro.addsizer(bt.sizers.FixedSize, stake=10)

    # WEBULL_USE_BROKER=1 connects the real trading broker (submits real
    # orders!); otherwise backtrader's built-in simulated broker is used and
    # the strategy runs without placing any orders.
    use_broker = os.environ.get("WEBULL_USE_BROKER", "0") == "1"
    if use_broker:
        broker = build_broker()
        cerebro.setbroker(broker)
        logger.warning("[Live] connected to the real Webull trading broker; "
                       "strategy signals will place real orders")
    else:
        cerebro.broker.setcash(100000.0)
        logger.info("[Live] using the simulated broker (no real orders will be placed)")

    logger.info("[Live] starting cash: %.2f", cerebro.broker.getcash())
    logger.info("[Live] started, polling for simulated real-time data; Ctrl+C to stop")
    try:
        cerebro.run()
    except KeyboardInterrupt:
        logger.info("[Live] stopped")


def main() -> None:
    # Load environment variables from live/.env (managed via python-dotenv).
    load_dotenv(Path(__file__).resolve().parent / ".env")
    setup_logging()
    run_live()


if __name__ == "__main__":
    main()
