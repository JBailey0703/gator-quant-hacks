"""TradingView Lightweight Charts renderer for backtrader backtests (scheme A).

An alternative to ``visualize.py`` (Plotly) that renders the same recorded
data with TradingView's `Lightweight Charts <https://github.com/tradingview/
lightweight-charts>`_ library for a券商级 (broker-grade) candlestick feel:
smooth zoom/pan, a synced crosshair, overlaid moving-average lines, buy/sell
markers and a volume sub-pane per symbol, plus an equity/drawdown chart. Hover
tooltips show the bar's OHLCV and any trade (with realized PnL) on the symbol
charts, and the equity value / drawdown % on the equity chart.

This module only builds the HTML/JS; the data still comes from
``RecorderAnalyzer`` in ``visualize.py`` (unchanged) and the strategy's
``closed_trades``. ``render_report_lwc()`` mirrors ``render_report()``'s
signature so ``main.py`` can pick an engine at runtime.

Design notes:
  - The library is loaded from a CDN (``include`` a <script> tag); the page
    itself is a self-contained HTML file otherwise.
  - Lightweight Charts renders a UNIX timestamp's *UTC* wall-clock. To show US
    Eastern wall-clock on the axis, we pass each bar's Eastern wall-clock time
    reinterpreted as a UTC epoch (see ``_epoch_seconds``). This is the standard
    trick for LWC and keeps the displayed times matching the logs.
  - Everything is themed to match the Plotly report's dark palette (_THEME) so
    the two engines look like the same product.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from webull_bt.logging_utils import get_logger
from webull_bt.visualize import (
    _THEME,
    _fmt_money,
    _kpi_cards_html,
    _compute_drawdown_series,
    _group_trades_by_symbol,
    _coerce_dt,
)


logger = get_logger("visualize_lwc")


# Line colors cycled across overlay indicators, matching visualize.py's palette.
_INDICATOR_COLORS = ("#f5a623", "#bd93f9", "#50fa7b", "#ff79c6", "#8be9fd")

# Pin a specific Lightweight Charts major version so the JS API we target
# (v4: addCandlestickSeries/addHistogramSeries/addLineSeries/setMarkers) stays
# stable regardless of what "latest" becomes.
_LWC_CDN = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"


def _epoch_seconds(dt: datetime | None) -> int | None:
    """Return a UNIX-second timestamp whose UTC wall-clock equals ``dt``'s
    local wall-clock, so Lightweight Charts displays the market-local time.

    ``dt`` here is a market-tz aware datetime (produced by RecorderAnalyzer via
    to_market_tz). LWC always renders a timestamp as its UTC wall-clock, so we
    strip the tzinfo (keeping the Eastern wall-clock digits) and re-stamp it as
    UTC before converting to epoch seconds. Returns None for None input.
    """
    if dt is None:
        return None
    naive_local = dt.replace(tzinfo=None)
    return int(naive_local.replace(tzinfo=timezone.utc).timestamp())


def _candles_json(s: dict) -> list[dict]:
    """Build Lightweight Charts candlestick data from a recorded symbol series.

    Skips any bar whose timestamp can't be resolved so one bad row can't break
    the whole chart.
    """
    out: list[dict] = []
    for dt, o, h, l, c in zip(
        s["datetime"], s["open"], s["high"], s["low"], s["close"]
    ):
        ts = _epoch_seconds(dt)
        if ts is None:
            continue
        out.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
    return out


def _volume_json(s: dict) -> list[dict]:
    """Build volume histogram data, colored by each bar's up/down direction."""
    out: list[dict] = []
    for dt, o, c, v in zip(s["datetime"], s["open"], s["close"], s["volume"]):
        ts = _epoch_seconds(dt)
        if ts is None:
            continue
        color = _THEME["up"] if c >= o else _THEME["down"]
        out.append({"time": ts, "value": v, "color": color})
    return out


def _indicator_json(s: dict, label: str) -> list[dict]:
    """Build a single overlay indicator line series ({time, value}).

    NaN values (indicator warmup) are dropped so the line simply starts once
    the indicator has values, instead of drawing through NaNs.
    """
    out: list[dict] = []
    series = s.get("indicators", {}).get(label, [])
    for dt, val in zip(s["datetime"], series):
        ts = _epoch_seconds(dt)
        if ts is None or val is None or val != val:  # val != val filters NaN
            continue
        out.append({"time": ts, "value": val})
    return out


def _markers_json(trades: list[dict]) -> list[dict]:
    """Build Lightweight Charts markers for a symbol's closed trades.

    A LONG trade's entry is a buy (up arrow below the bar) and its exit a sell
    (down arrow above); a SHORT trade is the mirror image. Markers must be
    sorted ascending by time, which LWC requires.
    """
    markers: list[dict] = []
    for t in trades:
        is_long = t.get("direction", "LONG") == "LONG"
        entry_ts = _epoch_seconds(_coerce_dt(t.get("open_dt")))
        exit_ts = _epoch_seconds(_coerce_dt(t.get("close_dt")))
        entry_price = t.get("entry_price")
        exit_price = t.get("exit_price")
        pnl = t.get("pnlcomm", t.get("pnl", 0.0))

        if entry_ts is not None and entry_price is not None:
            markers.append({
                "time": entry_ts,
                "position": "belowBar" if is_long else "aboveBar",
                "color": _THEME["buy"] if is_long else _THEME["sell"],
                "shape": "arrowUp" if is_long else "arrowDown",
                "text": f"{'BUY' if is_long else 'SELL'} {entry_price:.2f}",
            })
        if exit_ts is not None and exit_price is not None:
            markers.append({
                "time": exit_ts,
                "position": "aboveBar" if is_long else "belowBar",
                "color": _THEME["sell"] if is_long else _THEME["buy"],
                "shape": "arrowDown" if is_long else "arrowUp",
                "text": f"{'SELL' if is_long else 'BUY'} {exit_price:.2f} ({pnl:+.2f})",
            })

    markers.sort(key=lambda m: m["time"])
    return markers


def _trade_events_json(trades: list[dict]) -> dict[str, list[dict]]:
    """Build a ``{ timestamp -> [event, ...] }`` map of trade fills for tooltips.

    Keyed by the same epoch-second timestamps used for the candles, so the
    front-end can look up "did anything trade on this bar?" when the crosshair
    moves. A single bar can carry more than one event (e.g. an exit of one
    trade and an entry of another), hence a list per timestamp.

    Each event: ``{action, price, size, pnl}`` where ``pnl`` is only meaningful
    on the closing leg (entry legs report null pnl). ``action`` is one of
    BUY / SELL / SELL(short) / BUY(cover) to match the marker semantics.

    JSON object keys must be strings, so timestamps are stringified; the
    front-end converts back with Number().
    """
    events: dict[str, list[dict]] = {}

    def _add(ts: int | None, event: dict) -> None:
        if ts is None:
            return
        events.setdefault(str(ts), []).append(event)

    for t in trades:
        is_long = t.get("direction", "LONG") == "LONG"
        entry_ts = _epoch_seconds(_coerce_dt(t.get("open_dt")))
        exit_ts = _epoch_seconds(_coerce_dt(t.get("close_dt")))
        pnl = t.get("pnlcomm", t.get("pnl", 0.0))
        size = t.get("size")

        _add(entry_ts, {
            "action": "BUY" if is_long else "SELL(short)",
            "price": t.get("entry_price"),
            "size": size,
            "pnl": None,  # entry leg has no realized pnl yet
        })
        _add(exit_ts, {
            "action": "SELL" if is_long else "BUY(cover)",
            "price": t.get("exit_price"),
            "size": size,
            "pnl": pnl,  # realized pnl (incl. commission) booked on close
        })

    return events


def _equity_json(equity: dict) -> tuple[list[dict], list[dict]]:
    """Build equity-curve and drawdown-% line data for the equity chart."""
    dd = _compute_drawdown_series(equity["value"])
    eq_out: list[dict] = []
    dd_out: list[dict] = []
    for dt, val, d in zip(equity["datetime"], equity["value"], dd):
        ts = _epoch_seconds(dt)
        if ts is None:
            continue
        eq_out.append({"time": ts, "value": val})
        dd_out.append({"time": ts, "value": d})
    return eq_out, dd_out


def _page_html_lwc(title: str, kpi_html: str, charts_payload: dict) -> str:
    """Assemble the full self-contained HTML page for the LWC report.

    ``charts_payload`` is JSON-serialized and handed to the page's JS, which
    builds one Lightweight Charts instance per symbol card plus the equity card.
    """
    t = _THEME
    payload_json = json.dumps(charts_payload)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<script src="{_LWC_CDN}"></script>
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
    border-radius: 12px; padding: 6px 10px 12px; margin-bottom: 18px;
  }}
  .chart-card-title {{
    color: {t['text']}; font-size: 15px; font-weight: 600;
    padding: 10px 6px 8px; border-bottom: 1px solid {t['panel_border']};
    margin-bottom: 10px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .reset-btn {{
    font-size: 12px; font-weight: 500; color: {t['text']};
    background: {t['bg']}; border: 1px solid {t['panel_border']};
    border-radius: 6px; padding: 4px 10px; cursor: pointer;
    transition: border-color .15s, color .15s;
  }}
  .reset-btn:hover {{ border-color: {t['accent']}; color: {t['accent']}; }}
  .chart-holder {{ width: 100%; height: 460px; }}
  .chart-holder.equity {{ height: 320px; }}
  .legend {{ font-size: 12px; color: {t['muted']}; padding: 2px 6px 8px; }}
  .legend .sw {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin: 0 4px 0 12px; vertical-align: middle; }}
  .legend-item {{
    cursor: pointer; user-select: none; padding: 2px 4px; border-radius: 4px;
    transition: background .12s, opacity .12s;
  }}
  .legend-item:hover {{ background: rgba(255,255,255,0.06); }}
  .legend-item.legend-off {{ opacity: 0.4; text-decoration: line-through; }}
  .chart-holder {{ position: relative; }}
  .lwc-tooltip {{
    position: absolute; display: none; z-index: 20; pointer-events: none;
    background: rgba(28,35,51,0.96); border: 1px solid {t['accent']};
    border-radius: 8px; padding: 8px 10px; font-size: 12px; line-height: 1.5;
    color: {t['text']}; white-space: nowrap;
    box-shadow: 0 4px 14px rgba(0,0,0,0.4);
  }}
  .lwc-tooltip .tt-title {{ font-weight: 600; margin-bottom: 4px; }}
  .lwc-tooltip .tt-row span:first-child {{ color: {t['muted']}; margin-right: 8px; }}
  .lwc-tooltip .tt-up {{ color: {t['up']}; }}
  .lwc-tooltip .tt-down {{ color: {t['down']}; }}
  .lwc-tooltip .tt-ohlc {{ display: flex; gap: 14px; margin-bottom: 2px; }}
  .lwc-tooltip .tt-kv {{ display: inline-flex; align-items: baseline; }}
  .lwc-tooltip .tt-k {{ color: {t['muted']}; margin-right: 6px; }}
  .lwc-tooltip .tt-v {{ font-variant-numeric: tabular-nums; }}
  .lwc-tooltip .tt-trade {{ margin-top: 5px; padding-top: 5px; border-top: 1px solid {t['panel_border']}; }}
</style>
</head>
<body>
  <div class="report-title">{title}</div>
  <div class="report-sub">Interactive backtest report (TradingView Lightweight Charts) &middot; scroll to zoom, drag to pan, hover for crosshair</div>
  {kpi_html}
  <div id="cards"></div>

<script>
const PAYLOAD = {payload_json};
const THEME = {json.dumps(t)};
const INDICATOR_COLORS = {json.dumps(_INDICATOR_COLORS)};

function baseChartOptions(height) {{
  return {{
    height: height,
    layout: {{
      background: {{ type: 'solid', color: 'rgba(0,0,0,0)' }},
      textColor: THEME.text,
      fontSize: 12,
    }},
    grid: {{
      vertLines: {{ color: 'rgba(255,255,255,0.05)' }},
      horzLines: {{ color: 'rgba(255,255,255,0.05)' }},
    }},
    rightPriceScale: {{ borderColor: THEME.panel_border }},
    timeScale: {{
      borderColor: THEME.panel_border,
      timeVisible: true,
      secondsVisible: false,
      // Pin both edges to the data's first/last bar so the user can't scroll
      // past the data, and can't zoom out wider than the full range (with both
      // edges fixed there's nowhere further to zoom out to).
      fixLeftEdge: true,
      fixRightEdge: true,
      lockVisibleTimeRangeOnResize: true,
      // Don't let the right edge drift beyond the last bar on scroll.
      rightBarStaysOnScroll: true,
    }},
    // Disable kinetic/mouse-wheel behaviours that let the view slide past the
    // data; keep pressed-mouse panning and pinch/wheel zoom within range.
    handleScroll: {{
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: false,
    }},
    handleScale: {{
      mouseWheel: true,
      pinch: true,
      axisPressedMouseMove: true,
      axisDoubleClickReset: true,
    }},
    crosshair: {{ mode: 0 }},
    autoSize: true,
  }};
}}

// Build a clickable legend: each item toggles its series' visibility. Clicking
// dims/strikes the item and hides the series; clicking again restores it.
function buildToggleLegend(container, toggles) {{
  container.innerHTML = '';
  toggles.forEach(t => {{
    let visible = true;
    const item = document.createElement('span');
    item.className = 'legend-item';
    item.innerHTML = `<span class="sw" style="background:${{t.color}}"></span>${{t.label}}`;
    item.addEventListener('click', () => {{
      visible = !visible;
      t.series.applyOptions({{ visible: visible }});
      item.classList.toggle('legend-off', !visible);
    }});
    container.appendChild(item);
  }});
}}

// Build a card title bar: the title text on the left and a "Reset layout"
// button on the right. Returns the bar element and the button so the caller
// can wire the button to that chart's reset handler.
function makeCardTitle(text) {{
  const bar = document.createElement('div');
  bar.className = 'chart-card-title';
  const label = document.createElement('span');
  label.textContent = text;
  const resetBtn = document.createElement('button');
  resetBtn.className = 'reset-btn';
  resetBtn.type = 'button';
  resetBtn.textContent = 'Reset layout';
  bar.appendChild(label);
  bar.appendChild(resetBtn);
  return {{ bar, resetBtn }};
}}

function buildSymbolCard(sym) {{
  const card = document.createElement('div');
  card.className = 'chart-card';

  const {{ bar: titleBar, resetBtn }} = makeCardTitle(
    sym.name + '  ·  price / indicators / trades / volume'
  );
  card.appendChild(titleBar);

  // Legend is populated after the series exist so each item can toggle its
  // series' visibility on click.
  const legend = document.createElement('div');
  legend.className = 'legend';
  card.appendChild(legend);

  const holder = document.createElement('div');
  holder.className = 'chart-holder';
  card.appendChild(holder);
  document.getElementById('cards').appendChild(card);

  // Floating tooltip shown on crosshair move (OHLCV + any trade on the bar).
  const tooltip = document.createElement('div');
  tooltip.className = 'lwc-tooltip';
  holder.appendChild(tooltip);

  const chart = LightweightCharts.createChart(holder, baseChartOptions(460));

  const candle = chart.addCandlestickSeries({{
    upColor: THEME.up, downColor: THEME.down,
    borderUpColor: THEME.up, borderDownColor: THEME.down,
    wickUpColor: THEME.up, wickDownColor: THEME.down,
  }});
  candle.setData(sym.candles);

  // Collect the series that the legend can toggle on/off.
  const toggles = [];

  sym.indicators.forEach((ind, i) => {{
    const color = INDICATOR_COLORS[i % INDICATOR_COLORS.length];
    const line = chart.addLineSeries({{
      color: color, lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    }});
    line.setData(ind.data);
    toggles.push({{ label: ind.label, color: color, series: line }});
  }});

  // Volume as a histogram pinned to the bottom ~20% of the pane.
  const vol = chart.addHistogramSeries({{
    priceFormat: {{ type: 'volume' }},
    priceScaleId: 'vol',
  }});
  vol.setData(sym.volume);
  chart.priceScale('vol').applyOptions({{ scaleMargins: {{ top: 0.8, bottom: 0 }} }});
  toggles.push({{ label: 'volume', color: THEME.muted, series: vol }});

  // Buy/sell markers are hosted on a dedicated, fully transparent line series
  // added LAST, so it draws on top of the candles, indicator lines and volume
  // — the markers stay in the top layer and are never occluded. The line data
  // uses the candle close prices only to give the markers a valid price scale;
  // the line itself is invisible. Hiding this series hides the markers too.
  if (sym.markers && sym.markers.length) {{
    const markerSeries = chart.addLineSeries({{
      color: 'rgba(0,0,0,0)', lineWidth: 1,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false,
    }});
    markerSeries.setData(sym.candles.map(c => ({{ time: c.time, value: c.close }})));
    markerSeries.setMarkers(sym.markers);
    // One legend entry covers both buy and sell markers (same series).
    toggles.push({{ label: 'trades', color: THEME.buy, series: markerSeries }});
  }}

  buildToggleLegend(legend, toggles);

  attachTooltip(chart, holder, tooltip, candle, vol, sym);

  const applyLayout = () => chart.timeScale().fitContent();
  applyLayout();
  resetBtn.addEventListener('click', applyLayout);
}}

// Wire up the hover tooltip: on crosshair move, read the bar's OHLCV from the
// series under the cursor and append any trade fills recorded on that bar.
function attachTooltip(chart, holder, tooltip, candle, vol, sym) {{
  const events = sym.tradeEvents || {{}};
  chart.subscribeCrosshairMove(param => {{
    if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) {{
      tooltip.style.display = 'none';
      return;
    }}
    const bar = param.seriesData.get(candle);
    if (!bar) {{ tooltip.style.display = 'none'; return; }}
    const volBar = param.seriesData.get(vol);
    const volVal = volBar ? volBar.value : null;

    const upClass = bar.close >= bar.open ? 'tt-up' : 'tt-down';
    const chg = bar.close - bar.open;
    const chgPct = bar.open ? (chg / bar.open * 100) : 0;

    // param.time is our epoch-second key; format it as the market wall-clock.
    const d = new Date(param.time * 1000);
    const label = d.toISOString().slice(0, 16).replace('T', ' ');

    let html = `<div class="tt-title">${{sym.name}} · ${{label}}</div>`;
    html += '<div class="tt-ohlc">'
          + `<span class="tt-kv"><span class="tt-k">O</span><span class="tt-v">${{fmt(bar.open)}}</span></span>`
          + `<span class="tt-kv"><span class="tt-k">H</span><span class="tt-v">${{fmt(bar.high)}}</span></span>`
          + `<span class="tt-kv"><span class="tt-k">L</span><span class="tt-v">${{fmt(bar.low)}}</span></span>`
          + `<span class="tt-kv"><span class="tt-k">C</span><span class="tt-v ${{upClass}}">${{fmt(bar.close)}}</span></span>`
          + '</div>';
    html += `<div class="tt-row"><span>Chg</span>`
          + `<b class="${{upClass}}">${{fmt(chg)}} (${{chgPct >= 0 ? '+' : ''}}${{chgPct.toFixed(2)}}%)</b></div>`;
    if (volVal != null) html += `<div class="tt-row"><span>Vol</span>${{fmtInt(volVal)}}</div>`;

    const evs = events[String(param.time)];
    if (evs && evs.length) {{
      html += '<div class="tt-trade">';
      evs.forEach(e => {{
        const pnlTxt = (e.pnl == null)
          ? ''
          : ` · PnL <b class="${{e.pnl >= 0 ? 'tt-up' : 'tt-down'}}">${{e.pnl >= 0 ? '+' : ''}}${{fmt(e.pnl)}}</b>`;
        const priceTxt = (e.price == null) ? '' : ` @ ${{fmt(e.price)}}`;
        const sizeTxt = (e.size == null || e.size === '') ? '' : ` ×${{e.size}}`;
        html += `<div class="tt-row"><span>${{e.action}}</span>${{priceTxt}}${{sizeTxt}}${{pnlTxt}}</div>`;
      }});
      html += '</div>';
    }}
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';

    // Position the tooltip near the cursor, flipping to stay inside the holder.
    const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    let x = param.point.x + 16, y = param.point.y + 16;
    if (x + w > holder.clientWidth) x = param.point.x - w - 16;
    if (y + h > holder.clientHeight) y = param.point.y - h - 16;
    tooltip.style.left = Math.max(0, x) + 'px';
    tooltip.style.top = Math.max(0, y) + 'px';
  }});
}}

function fmt(v) {{ return (v == null) ? '-' : Number(v).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}}); }}
function fmtInt(v) {{ return (v == null) ? '-' : Number(v).toLocaleString(undefined, {{maximumFractionDigits: 0}}); }}

function buildEquityCard(eq) {{
  const card = document.createElement('div');
  card.className = 'chart-card';
  const {{ bar: titleBar, resetBtn }} = makeCardTitle('Portfolio equity & drawdown');
  card.appendChild(titleBar);

  const legend = document.createElement('div');
  legend.className = 'legend';
  card.appendChild(legend);

  const holder = document.createElement('div');
  holder.className = 'chart-holder equity';
  card.appendChild(holder);
  document.getElementById('cards').appendChild(card);

  const tooltip = document.createElement('div');
  tooltip.className = 'lwc-tooltip';
  holder.appendChild(tooltip);

  const chart = LightweightCharts.createChart(holder, baseChartOptions(320));
  const eqSeries = chart.addAreaSeries({{
    lineColor: THEME.equity, topColor: 'rgba(79,156,249,0.30)', bottomColor: 'rgba(79,156,249,0.02)',
    lineWidth: 2, priceScaleId: 'right',
  }});
  eqSeries.setData(eq.equity);

  // Drawdown on a separate left scale, inverted so deeper dips point down.
  const ddSeries = chart.addAreaSeries({{
    lineColor: THEME.drawdown, topColor: 'rgba(239,83,80,0.25)', bottomColor: 'rgba(239,83,80,0.02)',
    lineWidth: 1, priceScaleId: 'dd',
  }});
  ddSeries.setData(eq.drawdown);
  chart.priceScale('dd').applyOptions({{ invertScale: true, scaleMargins: {{ top: 0.6, bottom: 0 }} }});

  buildToggleLegend(legend, [
    {{ label: 'equity', color: THEME.equity, series: eqSeries }},
    {{ label: 'drawdown %', color: THEME.drawdown, series: ddSeries }},
  ]);

  attachEquityTooltip(chart, holder, tooltip, eqSeries, ddSeries);

  const applyLayout = () => chart.timeScale().fitContent();
  applyLayout();
  resetBtn.addEventListener('click', applyLayout);
}}

// Hover tooltip for the equity chart: shows the equity value and drawdown %
// at the crosshair position.
function attachEquityTooltip(chart, holder, tooltip, eqSeries, ddSeries) {{
  chart.subscribeCrosshairMove(param => {{
    if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) {{
      tooltip.style.display = 'none';
      return;
    }}
    const eqPoint = param.seriesData.get(eqSeries);
    const ddPoint = param.seriesData.get(ddSeries);
    if (!eqPoint && !ddPoint) {{ tooltip.style.display = 'none'; return; }}

    const eqVal = eqPoint ? eqPoint.value : null;
    const ddVal = ddPoint ? ddPoint.value : null;

    const d = new Date(param.time * 1000);
    const label = d.toISOString().slice(0, 16).replace('T', ' ');

    let html = `<div class="tt-title">${{label}}</div>`;
    if (eqVal != null) html += `<div class="tt-row"><span>Equity</span><b>${{fmt(eqVal)}}</b></div>`;
    if (ddVal != null) html += `<div class="tt-row"><span>Drawdown</span>`
          + `<b class="${{ddVal > 0 ? 'tt-down' : 'tt-up'}}">${{ddVal.toFixed(2)}}%</b></div>`;
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';

    const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    let x = param.point.x + 16, y = param.point.y + 16;
    if (x + w > holder.clientWidth) x = param.point.x - w - 16;
    if (y + h > holder.clientHeight) y = param.point.y - h - 16;
    tooltip.style.left = Math.max(0, x) + 'px';
    tooltip.style.top = Math.max(0, y) + 'px';
  }});
}}

(function () {{
  PAYLOAD.symbols.forEach(buildSymbolCard);
  if (PAYLOAD.equity) buildEquityCard(PAYLOAD.equity);

  // Keep charts responsive to window width changes.
  window.addEventListener('resize', () => {{
    document.querySelectorAll('.chart-holder').forEach(h => {{
      // autoSize handles width; nothing extra needed, but this hook is here
      // for future per-chart resize logic.
    }});
  }});
}})();
</script>
</body>
</html>"""


def render_report_lwc(
    recorded: dict,
    closed_trades: list[dict],
    output_path: str,
    title: str = "Backtest Visualization",
    metrics: dict | None = None,
) -> str:
    """Render a TradingView Lightweight Charts HTML report to ``output_path``.

    Signature mirrors ``visualize.render_report`` so the two engines are
    interchangeable from the caller's point of view.

    :param recorded: the dict returned by ``RecorderAnalyzer.get_analysis()``.
    :param closed_trades: the strategy's ``closed_trades`` list (may be empty).
    :param output_path: where to write the HTML file.
    :param title: report title shown at the top of the page.
    :param metrics: optional summary metrics for the top KPI cards.
    :return: the path written (same as ``output_path``).
    """
    symbols = recorded.get("symbols", {})
    equity = recorded.get("equity", {"datetime": [], "value": []})
    trades_by_symbol = _group_trades_by_symbol(closed_trades or [])

    symbol_names = [name for name, s in symbols.items() if s["datetime"]]
    if not symbol_names:
        logger.warning("[VisualizeLWC] no symbol bars recorded; nothing to plot")

    has_equity = bool(equity["datetime"])
    if not symbol_names and not has_equity:
        raise ValueError("nothing to visualize: no bars and no equity recorded")

    payload_symbols = []
    for name in symbol_names:
        s = symbols[name]
        indicators = [
            {"label": label, "data": _indicator_json(s, label)}
            for label in s.get("indicators", {})
        ]
        symbol_trades = trades_by_symbol.get(name, [])
        payload_symbols.append({
            "name": name,
            "candles": _candles_json(s),
            "volume": _volume_json(s),
            "indicators": indicators,
            "markers": _markers_json(symbol_trades),
            # Per-bar trade fills, for the hover tooltip's PnL/trade line.
            "tradeEvents": _trade_events_json(symbol_trades),
        })

    payload: dict = {"symbols": payload_symbols}
    if has_equity:
        eq, dd = _equity_json(equity)
        payload["equity"] = {"equity": eq, "drawdown": dd}

    page = _page_html_lwc(title, _kpi_cards_html(metrics), payload)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    logger.info(
        "[VisualizeLWC] report written: %s (%d symbol card(s), equity=%s, kpi=%s)",
        output_path, len(symbol_names), has_equity, bool(metrics),
    )
    return output_path
