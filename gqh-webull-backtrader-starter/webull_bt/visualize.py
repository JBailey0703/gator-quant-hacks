"""Plotly-based visualization for backtrader backtests (scheme B).

Renders a styled, interactive HTML report: a strip of KPI cards (P&L, drawdown,
Sharpe, win rate, ...) on top, then one card per symbol holding a candlestick
chart (overlaid with strategy indicators and buy/sell markers) plus a volume
sub-panel, and finally a separate card for the portfolio equity curve with
drawdown overlaid. Each chart is a standalone figure in its own bordered card.
The report uses a dark "trading terminal" theme with range-selector buttons,
crosshair spikes and a high-contrast hover popup.

Two pieces work together:

  - ``RecorderAnalyzer``: a backtrader analyzer that, bar by bar, records each
    data feed's datetime/OHLC, any price-overlay indicators it can discover on
    the strategy, and the account equity series. It is intentionally
    non-invasive: strategies don't need to know it exists.

  - ``render_report()``: turns the recorded series plus the strategy's own
    ``closed_trades`` list into a single interactive Plotly HTML file.

Buy/sell markers reuse the ``closed_trades`` records that both bundled
strategies already populate (symbol/direction/entry_price/open_dt/exit_price/
close_dt), so no extra order bookkeeping is needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import backtrader as bt

from webull_bt.logging_utils import get_logger
from webull_bt.timeutils import to_market_tz


logger = get_logger("visualize")


# Indicator line names worth overlaying directly on the price chart. Momentum
# oscillators / cross signals (e.g. "crossover") are intentionally excluded:
# they don't share the price axis and would distort the candlestick scale.
_PRICE_OVERLAY_HINTS = ("ma", "sma", "ema", "wma", "band", "boll")


def _is_price_overlay_indicator(name: str) -> bool:
    """Heuristic: should an indicator named ``name`` be drawn on the price axis?

    Uses a positive-match whitelist of common moving-average / band style
    indicators rather than an else-fallback, so unknown indicators are left
    off the price chart by default (avoids distorting the candlestick scale).
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _PRICE_OVERLAY_HINTS)


class RecorderAnalyzer(bt.Analyzer):
    """Records per-symbol OHLC + overlay indicators + equity, for plotting.

    The analyzer discovers price-overlay indicators from ``strategy.inds`` when
    present (the convention used by ``dual_ma``). Strategies without an
    ``inds`` mapping (e.g. ``portfolio``) simply record OHLC + equity
    with no overlays; nothing breaks.

    Results (via ``get_analysis()``):
        {
          "symbols": {
             "AAPL": {
                "datetime": [datetime, ...],
                "open": [...], "high": [...], "low": [...], "close": [...],
                "volume": [...],
                "indicators": {"short_ma": [...], "long_ma": [...]},
             },
             ...
          },
          "equity": {"datetime": [...], "value": [...]},
        }
    """

    def __init__(self):
        # Per-symbol OHLC + indicator series, keyed by the feed's name.
        self._symbols: dict[str, dict[str, Any]] = {}
        # Portfolio equity series, sampled once per bar on the primary feed.
        self._equity_dt: list[datetime] = []
        self._equity_val: list[float] = []

        # Discover price-overlay indicators per data from the strategy's
        # `inds` mapping (dual_ma convention). Defensive: any strategy without
        # a usable `inds` mapping just gets no overlays.
        # Layout: {data: {indicator_label: indicator_line_obj}}
        self._overlays: dict[Any, dict[str, Any]] = {}
        inds = getattr(self.strategy, "inds", None)
        if isinstance(inds, dict):
            for data, ind_map in inds.items():
                if not isinstance(ind_map, dict):
                    continue
                picked: dict[str, Any] = {}
                for label, indicator in ind_map.items():
                    if _is_price_overlay_indicator(label):
                        picked[label] = indicator
                if picked:
                    self._overlays[data] = picked

        # Initialize per-symbol containers up front so symbols with no bars
        # still show up (empty) rather than silently vanishing.
        for data in self.strategy.datas:
            name = data._name or data._dataname
            self._symbols[name] = {
                "datetime": [],
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "volume": [],
                "indicators": {label: [] for label in self._overlays.get(data, {})},
            }

    def next(self):
        # Record one sample per data feed for the current bar.
        for data in self.strategy.datas:
            name = data._name or data._dataname
            store = self._symbols.get(name)
            if store is None:
                continue

            # A feed may not have a bar on every step (e.g. differing lengths);
            # backtrader keeps the last value, so guard with len(data) to only
            # record while the feed actually has data.
            if len(data) == 0:
                continue

            # backtrader datetimes are naive UTC; convert to market tz so the
            # chart's x-axis reads in US Eastern (matching the logs).
            store["datetime"].append(to_market_tz(data.datetime.datetime(0)))
            store["open"].append(float(data.open[0]))
            store["high"].append(float(data.high[0]))
            store["low"].append(float(data.low[0]))
            store["close"].append(float(data.close[0]))
            store["volume"].append(float(data.volume[0]))

            for label, indicator in self._overlays.get(data, {}).items():
                value = float(indicator[0])
                # Indicators emit NaN before their minimum period is reached;
                # keep NaN so Plotly leaves a gap instead of drawing a spike.
                store["indicators"][label].append(value)

        # Sample equity once per bar, timestamped by the primary feed.
        primary = self.strategy.datas[0]
        if len(primary) > 0:
            self._equity_dt.append(to_market_tz(primary.datetime.datetime(0)))
            self._equity_val.append(float(self.strategy.broker.getvalue()))

    def get_analysis(self):
        return {
            "symbols": self._symbols,
            "equity": {"datetime": self._equity_dt, "value": self._equity_val},
        }


# ---------------------------------------------------------------------------- #
# Rendering
# ---------------------------------------------------------------------------- #

def _group_trades_by_symbol(closed_trades: list[dict]) -> dict[str, list[dict]]:
    """Group a strategy's ``closed_trades`` records by symbol name."""
    grouped: dict[str, list[dict]] = {}
    for trade in closed_trades:
        grouped.setdefault(trade["symbol"], []).append(trade)
    return grouped


def _coerce_dt(value) -> datetime | None:
    """Coerce a trade timestamp to a datetime for plotting.

    ``closed_trades`` stores whatever ``trade.open_datetime()`` /
    ``close_datetime()`` returned (a datetime). Guard against None/other types
    so a single odd record can't break the whole report.
    """
    if isinstance(value, datetime):
        return value
    return None


def _build_trade_markers(trades: list[dict]) -> tuple[list, list, list, list, list, list]:
    """Split trades into buy/sell marker coordinates + hover text.

    For a LONG trade the entry is a buy and the exit is a sell; for a SHORT
    trade the entry is a sell (open short) and the exit is a buy (cover). This
    keeps the marker semantics correct for both bundled strategies.

    Returns (buy_x, buy_y, buy_text, sell_x, sell_y, sell_text).
    """
    buy_x, buy_y, buy_text = [], [], []
    sell_x, sell_y, sell_text = [], [], []

    for t in trades:
        is_long = t.get("direction", "LONG") == "LONG"
        entry_dt = _coerce_dt(t.get("open_dt"))
        exit_dt = _coerce_dt(t.get("close_dt"))
        entry_price = t.get("entry_price")
        exit_price = t.get("exit_price")
        pnl = t.get("pnlcomm", t.get("pnl", 0.0))

        # Entry leg
        if entry_dt is not None and entry_price is not None:
            text = (
                f"{'BUY' if is_long else 'SELL(open short)'} "
                f"{t.get('symbol', '')}<br>price={entry_price:.2f} "
                f"size={t.get('size', '')}"
            )
            if is_long:
                buy_x.append(entry_dt); buy_y.append(entry_price); buy_text.append(text)
            else:
                sell_x.append(entry_dt); sell_y.append(entry_price); sell_text.append(text)

        # Exit leg
        if exit_dt is not None and exit_price is not None:
            text = (
                f"{'SELL' if is_long else 'BUY(cover)'} "
                f"{t.get('symbol', '')}<br>price={exit_price:.2f} "
                f"pnl={pnl:.2f}"
            )
            if is_long:
                sell_x.append(exit_dt); sell_y.append(exit_price); sell_text.append(text)
            else:
                buy_x.append(exit_dt); buy_y.append(exit_price); buy_text.append(text)

    return buy_x, buy_y, buy_text, sell_x, sell_y, sell_text


def _compute_drawdown_series(values: list[float]) -> list[float]:
    """Compute a running drawdown percentage series from an equity curve.

    drawdown[i] = (running_max - value[i]) / running_max * 100, i.e. how far
    (in %) the equity is below its historical peak at each point. Returned as
    non-negative percentages (0 at new highs). Empty input -> empty output.
    """
    out: list[float] = []
    peak = None
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            out.append((peak - v) / peak * 100.0)
        else:
            out.append(0.0)
    return out


# Colors shared between the Plotly figure and the surrounding HTML shell so the
# report reads as one cohesive, dark "trading terminal" style theme.
_THEME = {
    "bg": "#0e1117",
    "panel": "#161b22",
    "panel_border": "#232a34",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "up": "#26a69a",
    "down": "#ef5350",
    "buy": "#00e676",
    "sell": "#ff5252",
    "equity": "#4f9cf9",
    "drawdown": "#ef5350",
    "accent": "#4f9cf9",
}

# Distinct line colors cycled across overlay indicators (short_ma/long_ma/...).
_INDICATOR_COLORS = ("#f5a623", "#bd93f9", "#50fa7b", "#ff79c6", "#8be9fd")


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def _kpi_cards_html(metrics: dict | None) -> str:
    """Build the top KPI card strip as an HTML fragment.

    Returns an empty string when no metrics are supplied, so the report still
    renders (charts only). Cards for P&L / drawdown are color-coded by sign so
    the headline result is readable at a glance.
    """
    if not metrics:
        return ""

    pnl = metrics.get("pnl", 0.0)
    pnl_pct = metrics.get("pnl_pct", 0.0)
    sharpe = metrics.get("sharpe_ratio")
    sharpe_txt = f"{sharpe:.3f}" if sharpe is not None else "N/A"
    pnl_cls = "pos" if pnl >= 0 else "neg"

    # Labels are emitted into HTML verbatim; use the &amp; entity for "&".
    cards = [
        ("Starting Cash", _fmt_money(metrics.get("starting_value", 0.0)), ""),
        ("Final Value", _fmt_money(metrics.get("final_value", 0.0)), ""),
        ("Net P&amp;L", f"{_fmt_money(pnl)} ({pnl_pct:+.2f}%)", pnl_cls),
        ("Max Drawdown",
         f"{metrics.get('max_drawdown_pct', 0.0):.2f}% "
         f"({_fmt_money(metrics.get('max_drawdown_money', 0.0))})", "neg"),
        ("Sharpe (ann.)", sharpe_txt, ""),
        ("Win Rate",
         f"{metrics.get('win_rate', 0.0):.1f}% "
         f"({metrics.get('won', 0)}/{metrics.get('total_trades', 0)})", ""),
        ("Trades",
         f"{metrics.get('total_trades', 0)} "
         f"(W{metrics.get('won', 0)}/L{metrics.get('lost', 0)})", ""),
        ("Trades Net P&amp;L", _fmt_money(metrics.get("net_pnl", 0.0)),
         "pos" if metrics.get("net_pnl", 0.0) >= 0 else "neg"),
    ]

    items = "\n".join(
        f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value {cls}">{value}</div></div>'
        for label, value, cls in cards
    )
    return f'<div class="kpi-grid">{items}</div>'


def _chart_card_html(card_title: str, chart_html: str) -> str:
    """Wrap a single Plotly chart fragment in its own titled card."""
    return (
        '<div class="chart-card">'
        f'<div class="chart-card-title">{card_title}</div>'
        f'<div class="chart-card-body">{chart_html}</div>'
        "</div>"
    )


def _page_html(title: str, kpi_html: str, cards_html: str) -> str:
    """Wrap the KPI strip + one-or-more chart cards in a styled full HTML page.

    ``cards_html`` is the pre-concatenated HTML of every chart card (each built
    via ``_chart_card_html``), so each chart lives in its own bordered card.
    """
    t = _THEME
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: {t['bg']}; color: {t['text']};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .report-title {{ font-size: 20px; font-weight: 600; margin: 0 0 4px; color: {t['text']}; }}
  .report-sub {{ color: {t['muted']}; font-size: 13px; margin: 0 0 20px; }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 22px;
  }}
  .kpi-card {{
    background: {t['panel']}; border: 1px solid {t['panel_border']};
    border-radius: 10px; padding: 14px 16px;
  }}
  .kpi-label {{ color: {t['muted']}; font-size: 12px; margin-bottom: 6px; letter-spacing: .3px; }}
  .kpi-value {{ font-size: 18px; font-weight: 600; color: {t['text']}; }}
  .kpi-value.pos {{ color: {t['up']}; }}
  .kpi-value.neg {{ color: {t['down']}; }}
  .chart-card {{
    background: {t['panel']}; border: 1px solid {t['panel_border']};
    border-radius: 12px; padding: 6px 10px 10px; margin-bottom: 18px;
  }}
  .chart-card-title {{
    color: {t['text']}; font-size: 15px; font-weight: 600;
    padding: 10px 6px 8px; border-bottom: 1px solid {t['panel_border']};
    margin-bottom: 6px;
  }}
</style>
</head>
<body>
  <div class="report-title">{title}</div>
  <div class="report-sub">Interactive backtest report &middot; drag to zoom, double-click to reset, click legend to toggle series</div>
  {kpi_html}
  {cards_html}
</body>
</html>"""


def _apply_common_layout(fig, height: int) -> None:
    """Apply the shared dark theme + readable hover styling to a figure.

    Crucially sets an explicit ``hoverlabel`` (dark background, light text,
    subtle border): the default unified-hover popup under ``plotly_dark`` can
    render with low text/background contrast and be hard to read.
    """
    fig.update_layout(
        height=height,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1,
                    font=dict(color=_THEME["text"])),
        margin=dict(l=60, r=30, t=30, b=40),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=_THEME["text"]),
        bargap=0.1,
        hoverlabel=dict(
            bgcolor="#1c2333",
            bordercolor=_THEME["accent"],
            font=dict(color=_THEME["text"], size=12),
        ),
    )
    # Crosshair spike lines on hover for precise reading across all axes.
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikedash="dot", spikecolor=_THEME["muted"])


def _build_symbol_figure(name: str, s: dict, trades: list[dict]):
    """Build a standalone figure for one symbol: price (candles + indicators +
    trade markers) on top, its volume beneath. Returns a plotly Figure."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        vertical_spacing=0.04, row_heights=[0.76, 0.24],
    )

    fig.add_trace(
        go.Candlestick(
            x=s["datetime"],
            open=s["open"], high=s["high"], low=s["low"], close=s["close"],
            name=name,
            increasing_line_color=_THEME["up"],
            decreasing_line_color=_THEME["down"],
            showlegend=False,
        ),
        row=1, col=1,
    )

    # Overlay indicators (e.g. short_ma / long_ma), each a distinct color.
    for j, (label, series) in enumerate(s.get("indicators", {}).items()):
        fig.add_trace(
            go.Scatter(
                x=s["datetime"], y=series, mode="lines", name=label,
                line=dict(width=1.4, color=_INDICATOR_COLORS[j % len(_INDICATOR_COLORS)]),
            ),
            row=1, col=1,
        )

    # Buy/sell markers from closed trades for this symbol.
    buy_x, buy_y, buy_text, sell_x, sell_y, sell_text = _build_trade_markers(trades)
    if buy_x:
        fig.add_trace(
            go.Scatter(
                x=buy_x, y=buy_y, mode="markers", name="buy",
                marker=dict(symbol="triangle-up", size=12, color=_THEME["buy"],
                            line=dict(width=1, color="#003d1a")),
                text=buy_text, hoverinfo="text",
            ),
            row=1, col=1,
        )
    if sell_x:
        fig.add_trace(
            go.Scatter(
                x=sell_x, y=sell_y, mode="markers", name="sell",
                marker=dict(symbol="triangle-down", size=12, color=_THEME["sell"],
                            line=dict(width=1, color="#5c0000")),
                text=sell_text, hoverinfo="text",
            ),
            row=1, col=1,
        )

    # Volume bars, colored by each bar's up/down direction.
    vol_colors = [
        _THEME["up"] if c >= o else _THEME["down"]
        for o, c in zip(s["open"], s["close"])
    ]
    fig.add_trace(
        go.Bar(
            x=s["datetime"], y=s["volume"], name="volume",
            marker=dict(color=vol_colors), opacity=0.6, showlegend=False,
        ),
        row=2, col=1,
    )

    # Range-selector buttons on the price row; range-slider off on both.
    fig.update_xaxes(
        rangeslider_visible=False,
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(step="all", label="All"),
            ],
            bgcolor=_THEME["panel"],
            activecolor=_THEME["accent"],
            font=dict(color=_THEME["text"]),
        ),
        row=1, col=1,
    )
    fig.update_xaxes(rangeslider_visible=False, row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Vol", row=2, col=1)

    _apply_common_layout(fig, height=560)
    return fig


def _build_equity_figure(equity: dict):
    """Build a standalone equity figure: equity curve + drawdown % on a
    secondary (inverted) axis. Returns a plotly Figure."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=equity["datetime"], y=equity["value"], mode="lines",
            name="Equity", line=dict(width=1.8, color=_THEME["equity"]),
            fill="tozeroy", fillcolor="rgba(79,156,249,0.10)",
        ),
        secondary_y=False,
    )
    dd_series = _compute_drawdown_series(equity["value"])
    fig.add_trace(
        go.Scatter(
            x=equity["datetime"], y=dd_series, mode="lines",
            name="Drawdown %", line=dict(width=1.0, color=_THEME["drawdown"]),
            fill="tozeroy", fillcolor="rgba(239,83,80,0.12)",
        ),
        secondary_y=True,
    )
    fig.update_yaxes(title_text="Value", secondary_y=False)
    # Invert the drawdown axis so deeper drawdowns dip downward visually.
    fig.update_yaxes(title_text="Drawdown %", secondary_y=True,
                     autorange="reversed", showgrid=False)
    fig.update_xaxes(rangeslider_visible=False)
    _apply_common_layout(fig, height=360)
    return fig


def render_report(
    recorded: dict,
    closed_trades: list[dict],
    output_path: str,
    title: str = "Backtest Visualization",
    metrics: dict | None = None,
) -> str:
    """Render an interactive Plotly HTML report and write it to ``output_path``.

    :param recorded: the dict returned by ``RecorderAnalyzer.get_analysis()``.
    :param closed_trades: the strategy's ``closed_trades`` list (may be empty).
    :param output_path: where to write the HTML file.
    :param title: report title shown at the top of the page.
    :param metrics: optional summary metrics (from main._compute_metrics) used
        to render the top KPI cards; omitted -> charts only.
    :return: the path written (same as ``output_path``).

    Each chart lives in its own titled card: one card per symbol (candles +
    indicators + trade markers, with a volume sub-panel), plus a final
    portfolio equity/drawdown card. The KPI card strip sits on top.
    """
    symbols = recorded.get("symbols", {})
    equity = recorded.get("equity", {"datetime": [], "value": []})
    trades_by_symbol = _group_trades_by_symbol(closed_trades or [])

    # Only plot symbols that actually recorded bars.
    symbol_names = [name for name, s in symbols.items() if s["datetime"]]
    if not symbol_names:
        logger.warning("[Visualize] no symbol bars recorded; nothing to plot")

    has_equity = bool(equity["datetime"])
    if not symbol_names and not has_equity:
        raise ValueError("nothing to visualize: no bars and no equity recorded")

    # Build each chart as its own figure -> its own card. Only the first
    # fragment inlines plotly.js (via CDN); the rest reuse it to avoid bloating
    # the file with repeated copies of the library.
    cards: list[str] = []
    first = True

    def _fragment(fig) -> str:
        nonlocal first
        html = fig.to_html(
            include_plotlyjs=("cdn" if first else False),
            full_html=False,
        )
        first = False
        return html

    for name in symbol_names:
        fig = _build_symbol_figure(name, symbols[name], trades_by_symbol.get(name, []))
        cards.append(_chart_card_html(
            f"{name} &middot; price / indicators / trades / volume", _fragment(fig)
        ))

    if has_equity:
        fig = _build_equity_figure(equity)
        cards.append(_chart_card_html("Portfolio equity &amp; drawdown", _fragment(fig)))

    page = _page_html(title, _kpi_cards_html(metrics), "\n".join(cards))
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    logger.info(
        "[Visualize] report written: %s (%d symbol card(s), equity=%s, kpi=%s)",
        output_path, len(symbol_names), has_equity, bool(metrics),
    )
    return output_path
