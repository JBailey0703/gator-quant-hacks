"""Timezone and trading-session helpers shared by the feed and strategies.

These utilities used to live inside ``feed.py``, but they are generic enough
to be reused by strategies and the report renderer without pulling in the feed
itself. Keeping them here gives every consumer a single, dependency-light
import site.

Two concerns live here:

  - Display timezone conversion (``to_market_tz``): backtrader stores every
    bar's datetime as a *naive UTC* value, so any timestamp read back off a
    data/line is UTC. For display (logs, charts) we convert to the market
    timezone. A named IANA zone is used (not a hardcoded -4/-5 offset) so US
    daylight-saving transitions are handled automatically. Override via
    ``WEBULL_DISPLAY_TZ`` (e.g. "America/New_York").

  - Trading-session encode/decode + bar-time parsing: the Webull bars endpoint
    tags each bar with the trading session it belongs to (PRE/RTH/ATH/OVN) and
    returns times like ``2021-12-28T09:00:09.945+0000``. backtrader lines only
    store floats, so the session string is encoded to an int for the
    ``trading_session`` line and can be decoded back with
    ``decode_trading_session``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from webull_bt.logging_utils import get_logger


logger = get_logger("timeutils")


# ---------------------------------------------------------------------------- #
# Display timezone
# ---------------------------------------------------------------------------- #
_DEFAULT_DISPLAY_TZ = "America/New_York"


def _resolve_display_tz() -> ZoneInfo:
    """Resolve the display timezone from WEBULL_DISPLAY_TZ (default US Eastern).

    Falls back to the default zone (logging a warning) if the configured name
    is not a valid IANA timezone, so a bad env value can't break a run.
    """
    name = os.environ.get("WEBULL_DISPLAY_TZ", _DEFAULT_DISPLAY_TZ).strip() or _DEFAULT_DISPLAY_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning(
            "[Timeutils] invalid WEBULL_DISPLAY_TZ=%r; falling back to %s",
            name, _DEFAULT_DISPLAY_TZ,
        )
        return ZoneInfo(_DEFAULT_DISPLAY_TZ)


MARKET_TZ = _resolve_display_tz()


def to_market_tz(dt: datetime | None) -> datetime | None:
    """Convert a backtrader UTC timestamp to a market-timezone aware datetime.

    backtrader timestamps are naive UTC; a naive input is therefore assumed to
    be UTC. An already tz-aware input is converted from its own zone. Returns
    None unchanged so callers can pass optional timestamps through safely.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MARKET_TZ)


# ---------------------------------------------------------------------------- #
# Bar time parsing
# ---------------------------------------------------------------------------- #

# Webull's historical bars endpoint returns time like: 2021-12-28T09:00:09.945+0000
# Support both with/without milliseconds, and both +0000 / +00:00 offset styles.
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
)


def parse_bar_time(raw: str) -> datetime:
    """Parse a Webull bar time string into a timezone-aware datetime.

    :param raw: time string like "2021-12-28T09:00:09.945+0000"
    :return: timezone-aware datetime (UTC semantics; actual offset from input)
    """
    if raw is None:
        raise ValueError("bar time is None")

    text = raw.strip()
    last_err: Exception | None = None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError as err:
            last_err = err

    # Fallback: try ISO8601 parsing (Python 3.11's fromisoformat supports +0000 / Z)
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"failed to parse bar time: {raw!r}") from last_err


# ---------------------------------------------------------------------------- #
# Trading session encode/decode
# ---------------------------------------------------------------------------- #

# Webull's bars endpoint tags each bar with the trading session it belongs to
# (e.g. "RTH" for regular trading hours). This is the closest thing to a
# per-bar "status" the API exposes. backtrader lines only store floats, so the
# session string is encoded to an int for the ``trading_session`` line and can
# be decoded back with ``decode_trading_session``.
_TRADING_SESSION_CODES = {
    "": 0,
    "PRE": 1,
    "RTH": 2,
    "ATH": 3,
    "OVN": 4,
}
_TRADING_SESSION_NAMES = {v: k for k, v in _TRADING_SESSION_CODES.items()}
_TRADING_SESSION_UNKNOWN = -1


def encode_trading_session(value: str | None) -> int:
    """Encode a trading-session string into the int stored on the line."""
    if value is None:
        return _TRADING_SESSION_CODES[""]
    return _TRADING_SESSION_CODES.get(value, _TRADING_SESSION_UNKNOWN)


def decode_trading_session(code) -> str:
    """Decode a value read off the ``trading_session`` line back to a string.

    :param code: the float/int read from ``data.trading_session[0]``
    :return: the original session string, or "UNKNOWN" for an unrecognized code
    """
    return _TRADING_SESSION_NAMES.get(int(code), "UNKNOWN")
