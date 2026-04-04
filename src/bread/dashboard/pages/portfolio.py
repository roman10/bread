"""Portfolio overview page — KPI cards, equity curve, positions, orders."""

from __future__ import annotations

import dash
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html
from flask import current_app

from bread.dashboard.charts import make_drawdown_figure, make_equity_figure
from bread.dashboard.components import (
    format_currency,
    format_local_dt,
    format_pct,
    make_kpi_card,
    make_kpi_row,
    make_strategy_explanation,
    pnl_color,
)

dash.register_page(__name__, path="/", name="Portfolio")

# -- Strategy explanations (plain English) --

STRATEGY_EXPLANATIONS: dict[str, dict[str, str | list[str]]] = {
    "etf_momentum": {
        "summary": (
            "Buys ETFs that are in a healthy long-term uptrend but have temporarily "
            "pulled back — like buying something on sale when the overall trend is still up."
        ),
        "what": [
            "Check if the price is above its 200-day average (long-term trend is up).",
            "Look for the RSI indicator to drop below 30, meaning the ETF has dipped "
            "more than usual (oversold).",
            "Confirm the fast average (20-day) is above the medium average (50-day) — "
            "the uptrend is still intact.",
            "Check that trading volume is at least normal (no suspiciously thin days).",
            "If all checks pass, buy. Sell when RSI rises above 70 (overbought), or if "
            "the price drops below a safety stop-loss.",
        ],
        "why": (
            "Markets tend to move in trends. When a strong uptrend dips temporarily — "
            "because of short-term news or profit-taking — it often bounces back. This "
            "strategy tries to catch that bounce. It's one of the most studied effects "
            "in finance, called the 'momentum anomaly.'"
        ),
        "universe": (
            "This strategy works on any liquid stock or ETF. The current 10-ETF set "
            "(SPY, QQQ, IWM, DIA, XLF, XLK, XLE, XLV, GLD, TLT) was chosen for high "
            "liquidity and diversification across market caps, sectors, and safe havens. "
            "You could add individual stocks like AAPL or MSFT, but ETFs are safer for "
            "small accounts because they can't crash to zero the way a single company can."
        ),
        "effectiveness_good": [
            "Trending markets (most of the time stocks go up over long periods).",
            "Low-volatility environments where dips are shallow and recoveries predictable.",
        ],
        "effectiveness_bad": [
            "Choppy, sideways markets — the strategy may buy dips that keep dipping.",
            "Sudden bear markets — the 200-day average filter helps, but there's always "
            "a lag before it catches on.",
        ],
    },
    "bb_mean_reversion": {
        "summary": (
            "Buys when an ETF's price drops to an unusually low level compared to its "
            "recent range, betting it will bounce back toward the average — like a "
            "rubber band snapping back."
        ),
        "what": [
            "Calculate the Bollinger Bands: an upper and lower band around the 20-day "
            "average price. The bands show the 'normal' price range.",
            "When the price touches or drops below the lower band, it's unusually cheap.",
            "Confirm with RSI below 40 — the ETF is genuinely oversold, not just drifting.",
            "Buy the ETF. Sell when RSI climbs above 60 (price has recovered toward "
            "the middle), or if a safety stop-loss is hit.",
        ],
        "why": (
            "Prices tend to revert to their average over time. When an ETF drops far "
            "below its recent average, sellers are often exhausted and buyers step in. "
            "This 'mean reversion' effect is especially strong for ETFs because they "
            "hold hundreds of stocks, making extreme moves more likely to reverse."
        ),
        "universe": (
            "Works best on liquid, diversified ETFs that don't trend in one direction "
            "forever. The current 10-ETF universe is ideal. Individual stocks can also "
            "work but carry more risk — a stock can drop below its band and keep falling "
            "(bankruptcy risk). Sticking with ETFs makes mean reversion more reliable."
        ),
        "effectiveness_good": [
            "Range-bound or mildly trending markets where prices oscillate predictably.",
            "High-liquidity ETFs that attract institutional buyers at oversold levels.",
        ],
        "effectiveness_bad": [
            "Strong downtrends — the price can stay 'too low' for a long time (catching "
            "a falling knife).",
            "During market panics (COVID crash, 2008), mean reversion takes much longer.",
        ],
    },
    "macd_trend": {
        "summary": (
            "Follows momentum shifts by watching when a fast momentum line crosses above "
            "a slow one — like noticing the wind is picking up before the sails fill."
        ),
        "what": [
            "Calculate the MACD: the difference between a fast (12-day) and slow (26-day) "
            "moving average of the price.",
            "When the MACD line crosses above its signal line (a 9-day average of MACD "
            "itself), momentum is turning positive.",
            "Confirm with a 21-day trend filter — the price should be trending up, "
            "not just bouncing in a downtrend.",
            "Buy. Sell when MACD crosses back below its signal, or when the stop-loss "
            "is triggered.",
        ],
        "why": (
            "MACD measures how fast prices are changing. When the fast average starts "
            "pulling ahead of the slow average, it means recent buying pressure is "
            "increasing. This tends to persist for a while — trends have inertia."
        ),
        "universe": (
            "Works on virtually any liquid ticker — stocks, ETFs, or even crypto. The "
            "current ETF universe provides clean, less noisy signals. You could add "
            "large-cap stocks (AAPL, GOOGL, AMZN) or international ETFs (EFA, EEM) for "
            "more trading opportunities."
        ),
        "effectiveness_good": [
            "Clear trending markets where momentum builds gradually.",
            "After major reversals — MACD is often one of the first indicators to "
            "catch a new trend.",
        ],
        "effectiveness_bad": [
            "Choppy, flat markets produce many false crossover signals (whipsaws).",
            "The signal is lagging — by the time MACD crosses, a fast move may already "
            "be partly over.",
        ],
    },
    "ema_crossover": {
        "summary": (
            "Buys when a fast-moving average crosses above a slow one, signaling "
            "a new uptrend may be starting — like a faster car overtaking a slower one."
        ),
        "what": [
            "Track a fast 9-day average (EMA) and a slow 21-day average (EMA).",
            "When the fast crosses above the slow, the short-term trend has turned up.",
            "Confirm: price must be above the 200-day average (long-term trend is up) "
            "and RSI must be between 40-65 (not already overbought or oversold).",
            "Buy. Sell when RSI rises above 75, or if the stop-loss triggers.",
        ],
        "why": (
            "Moving average crossovers capture the moment when recent price momentum "
            "overtakes the broader trend. The 200-day filter keeps you on the right "
            "side of the big picture trend. It's a classic, widely-used signal because "
            "it's simple and works across many markets."
        ),
        "universe": (
            "General-purpose — works on any liquid ticker. ETFs produce cleaner signals "
            "with fewer false crossovers compared to individual stocks, which have more "
            "random noise. You could expand to sector ETFs like XLI, XLC, XLB for more "
            "opportunities."
        ),
        "effectiveness_good": [
            "Markets coming out of corrections — the crossover catches the start of new uptrends.",
            "Steadily trending environments.",
        ],
        "effectiveness_bad": [
            "Flat, sideways markets generate many false crossovers (buy-then-sell quickly).",
            "Very fast moves may be missed because the crossover happens after the initial pop.",
        ],
    },
    "sector_rotation": {
        "summary": (
            "Ranks all sectors by their recent performance, then buys the top 3 — "
            "like always sitting in the fastest lane on the highway."
        ),
        "what": [
            "Score each ETF using a blend of 5-day, 10-day, and 20-day returns (recent "
            "performance weighted more).",
            "Confirm: the ETF's price must be above its 50-day average (still trending up).",
            "Buy the top 3 ranked ETFs.",
            "Sell when an ETF's rank drops below 5th place, or if the stop-loss triggers.",
        ],
        "why": (
            "Different sectors lead at different times — tech booms, energy rallies, "
            "financial recoveries. Money tends to flow into sectors that are already "
            "outperforming (institutional herding). By always holding the strongest "
            "sectors, you ride these waves of capital flow."
        ),
        "universe": (
            "This strategy specifically needs sector ETFs to work — it ranks sectors "
            "against each other. The current set covers 4 sectors (XLF, XLK, XLE, XLV) "
            "plus broad market and safe havens. For better coverage, you could add XLU "
            "(utilities), XLI (industrials), XLC (communications), XLB (materials), and "
            "XLRE (real estate). More sectors = better rotation opportunities."
        ),
        "effectiveness_good": [
            "Markets with clear sector leadership (tech rally, energy boom, etc.).",
            "Periods of sector divergence — some sectors up while others are down.",
        ],
        "effectiveness_bad": [
            "Broad market selloffs where everything drops together — there's nowhere "
            "to rotate into.",
            "Rapid sector leadership changes can cause whipsaws.",
        ],
    },
    "risk_off_rotation": {
        "summary": (
            "Acts like a mood detector for the market — when stocks look risky, it "
            "moves money into safe havens like gold and bonds. When the coast is clear, "
            "it moves back into stocks."
        ),
        "what": [
            "Check SPY's 20-day return as a proxy for market mood.",
            "If SPY's return is negative (market falling), switch to 'risk-off' mode — "
            "buy safe havens like TLT (bonds) or GLD (gold).",
            "If SPY's return is positive (market rising), switch to 'risk-on' mode — "
            "buy the best-performing stock ETFs.",
            "Sell when the regime flips, or if the stop-loss triggers.",
        ],
        "why": (
            "In bad times, investors flee to safety — bonds and gold typically rise when "
            "stocks fall. This 'flight to safety' is one of the most consistent patterns "
            "in finance. Instead of sitting in cash during downturns, this strategy tries "
            "to profit from the flight to safety itself."
        ),
        "universe": (
            "This strategy requires a specific structure: equity ETFs (SPY, QQQ, IWM, "
            "DIA, XLF, XLK, XLE, XLV) for risk-on, and safe-haven ETFs (TLT, GLD) for "
            "risk-off. You could enhance it by adding AGG (broad bonds), SHY (short-term "
            "treasuries), or international havens. Cannot work with individual stocks alone "
            "— needs the equity-vs-haven split."
        ),
        "effectiveness_good": [
            "Clear risk-on / risk-off regimes (2020 COVID crash → recovery is a textbook example).",
            "Prolonged downturns — while other strategies lose money, this one pivots "
            "to safe assets.",
        ],
        "effectiveness_bad": [
            "Rare periods when both stocks AND bonds fall together (e.g., 2022 rate "
            "hike environment).",
            "Choppy markets where SPY briefly dips then recovers — causes unnecessary rotation.",
        ],
    },
    "breakout_squeeze": {
        "summary": (
            "Waits for periods when an ETF's price range gets unusually tight "
            "(a 'squeeze'), then buys when price breaks out upward — like a coiled "
            "spring releasing."
        ),
        "what": [
            "Measure the Bollinger Band width over the last 50 days.",
            "When the current width is in the lowest 20% historically, a squeeze is on — "
            "volatility is compressed.",
            "Wait for price to break above the upper Bollinger Band with above-average "
            "volume (1.2x normal).",
            "Buy the breakout. Sell after 10 days, or if the stop-loss triggers.",
        ],
        "why": (
            "Volatility is cyclical — periods of calm are almost always followed by "
            "periods of movement. When price compresses into a tight range, it often "
            "breaks out explosively. The volume confirmation helps ensure the breakout "
            "has real buying power behind it, not just a random blip."
        ),
        "universe": (
            "General-purpose — works on any liquid ticker. Individual stocks (AAPL, "
            "TSLA, NVDA) often have more dramatic squeezes and breakouts than ETFs, so "
            "adding them could increase both opportunities and potential returns. However, "
            "stock breakouts also fail more often. ETFs provide safer, if smaller, "
            "breakout signals."
        ),
        "effectiveness_good": [
            "Before major moves — earnings season, Fed announcements, or after prolonged "
            "consolidation.",
            "Low-volatility environments that precede trend changes.",
        ],
        "effectiveness_bad": [
            "False breakouts — price pops above the band then falls right back (hence "
            "the volume filter).",
            "ETFs squeeze less dramatically than individual stocks, so signals can be "
            "less frequent.",
        ],
    },
    "macd_divergence": {
        "summary": (
            "Spots 'hidden strength' — when an ETF's price makes a new low but its "
            "momentum doesn't, suggesting sellers are losing steam and a reversal is "
            "likely."
        ),
        "what": [
            "Watch for price to make a lower low over the last 20 days.",
            "At the same time, check if the MACD indicator made a higher low (didn't "
            "confirm the price drop).",
            "This mismatch (divergence) suggests the downtrend is weakening.",
            "Confirm RSI is below 45 (still in oversold territory — room to bounce).",
            "Buy. Sell when RSI rises above 65, or if the stop-loss triggers.",
        ],
        "why": (
            "When price drops to a new low but momentum (MACD) doesn't, it means each "
            "wave of selling is weaker than the last. Buyers are quietly stepping in. "
            "This is one of the most reliable reversal signals in technical analysis — "
            "used by professional traders worldwide."
        ),
        "universe": (
            "Works on any liquid ticker — stocks, ETFs, or indices. ETFs tend to give "
            "cleaner divergence signals because they're less prone to random spikes. "
            "Adding large-cap stocks could increase the number of divergence opportunities "
            "you see."
        ),
        "effectiveness_good": [
            "Near market bottoms — divergence often appears right before a V-shaped recovery.",
            "Orderly downtrends where sellers gradually exhaust.",
        ],
        "effectiveness_bad": [
            "Panics / crashes — divergence can appear multiple times on the way down "
            "before the real bottom.",
            "The signal is subjective and can produce false positives in choppy markets.",
        ],
    },
    "gap_fade": {
        "summary": (
            "When an ETF drops sharply overnight (a 'gap down'), this strategy bets "
            "it will bounce back during the trading day — like a knee-jerk overreaction "
            "that corrects itself."
        ),
        "what": [
            "Check if today's opening price is at least 1.5% below yesterday's close (a gap down).",
            "Confirm the long-term trend is still up (price above 200-day average) — "
            "the gap is likely an overreaction, not the start of a crash.",
            "Confirm RSI is below 40 (oversold — room to recover).",
            "Buy. Sell when RSI climbs above 60 (partial recovery), or if the stop-loss triggers.",
        ],
        "why": (
            "Overnight gaps are often driven by emotional reactions to news, futures "
            "trading, or overseas markets. When the underlying trend is healthy, these "
            "gaps tend to fill — meaning the price recovers part or all of the drop "
            "during regular trading. This is a well-documented intraday pattern."
        ),
        "universe": (
            "General-purpose, but ETFs gap less often than individual stocks. Adding "
            "volatile stocks (e.g., TSLA, NVDA, AMD) would significantly increase the "
            "number of gap-down opportunities. The tradeoff: stock gaps are less "
            "reliable — some are real crashes, not just overreactions. ETFs gap more "
            "rarely but recover more consistently."
        ),
        "effectiveness_good": [
            "Healthy bull markets where dips are buying opportunities.",
            "Gaps caused by temporary news (earnings misses in one sector, overseas "
            "turmoil) that don't change the fundamental picture.",
        ],
        "effectiveness_bad": [
            "Gaps caused by real fundamental shifts (rate hikes, recession signals) — "
            "these may not fill.",
            "Bear markets — gaps down are often the start of further selling, not overreactions.",
        ],
    },
}

# -- AG Grid column definitions --

_POSITION_COLS = [
    {"field": "symbol", "headerName": "Symbol", "width": 90},
    {"field": "qty", "headerName": "Qty", "width": 70, "type": "numericColumn"},
    {
        "field": "entry_price",
        "headerName": "Entry",
        "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "current_price",
        "headerName": "Current",
        "width": 100,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toFixed(2)"},
    },
    {
        "field": "unrealized_pnl",
        "headerName": "P&L",
        "width": 110,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "(params.value >= 0 ? '+$' : '-$') + Math.abs(params.value).toFixed(2)"
        },
        "cellStyle": {
            "function": "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "unrealized_pct",
        "headerName": "P&L %",
        "width": 90,
        "type": "numericColumn",
        "valueFormatter": {
            "function": "(params.value >= 0 ? '+' : '') + params.value.toFixed(1) + '%'"
        },
        "cellStyle": {
            "function": "params.value >= 0 ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "market_value",
        "headerName": "Value",
        "width": 110,
        "type": "numericColumn",
        "valueFormatter": {"function": "'$' + params.value.toLocaleString()"},
    },
]

_ORDER_COLS = [
    {"field": "symbol", "headerName": "Symbol", "width": 90},
    {"field": "side", "headerName": "Side", "width": 70},
    {"field": "qty", "headerName": "Qty", "width": 70},
    {"field": "type", "headerName": "Type", "width": 100},
    {"field": "status", "headerName": "Status", "width": 100},
    {"field": "submitted_at", "headerName": "Submitted", "flex": 1},
]

_STRATEGY_COLS = [
    {
        "field": "name",
        "headerName": "Strategy",
        "width": 160,
        "cellStyle": {"color": "#00bc8c", "cursor": "pointer", "textDecoration": "underline"},
    },
    {
        "field": "status",
        "headerName": "Status",
        "width": 110,
        "cellStyle": {
            "function": (
                "params.value === 'active' ? {'color': '#00bc8c'} : "
                "params.value === 'disabled' ? {'color': '#888'} : "
                "{'color': '#f39c12'}"
            )
        },
    },
    {"field": "enabled", "headerName": "Enabled", "width": 90},
    {"field": "modes", "headerName": "Modes", "width": 120},
    {
        "field": "weight",
        "headerName": "Weight",
        "width": 80,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(2)"},
    },
    {"field": "universe", "headerName": "Universe", "flex": 1, "tooltipField": "universe"},
]

_SIGNAL_COLS = [
    {"field": "time", "headerName": "Time", "width": 200, "sort": "desc"},
    {"field": "strategy", "headerName": "Strategy", "width": 140},
    {"field": "symbol", "headerName": "Symbol", "width": 80},
    {
        "field": "direction",
        "headerName": "Direction",
        "width": 90,
        "cellStyle": {
            "function": "params.value === 'BUY' ? {'color': '#00bc8c'} : {'color': '#e74c3c'}"
        },
    },
    {
        "field": "strength",
        "headerName": "Strength",
        "width": 90,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(2)"},
    },
    {
        "field": "stop_loss_pct",
        "headerName": "Stop %",
        "width": 80,
        "type": "numericColumn",
        "valueFormatter": {"function": "params.value.toFixed(1) + '%'"},
    },
    {"field": "reason", "headerName": "Reason", "flex": 1},
]

_EVENT_COLS = [
    {"field": "time", "headerName": "Time", "width": 170, "sort": "desc"},
    {"field": "symbol", "headerName": "Symbol", "width": 80},
    {
        "field": "severity",
        "headerName": "Severity",
        "width": 100,
        "cellStyle": {
            "function": (
                "params.value === 'HIGH' ? {'color': '#e74c3c'} : "
                "params.value === 'MEDIUM' ? {'color': '#f39c12'} : "
                "{'color': '#888'}"
            )
        },
    },
    {
        "field": "headline",
        "headerName": "Headline",
        "flex": 1,
        "tooltipField": "details",
    },
    {"field": "event_type", "headerName": "Type", "width": 100},
]

# -- Layout --

layout = dbc.Container(
    [
        html.Div(id="portfolio-kpi-row"),
        html.H6("Bot Activity", className="text-muted mb-2 mt-3"),
        html.Div(id="bot-activity-row"),
        dbc.Row(
            [
                dbc.Col(html.Div(id="equity-chart"), md=7),
                dbc.Col(html.Div(id="drawdown-chart"), md=5),
            ],
            className="mb-4",
        ),
        html.H6("Strategy Status", className="text-muted mb-2"),
        html.Small(
            "Click a strategy name to learn how it works.",
            className="text-muted d-block mb-2",
        ),
        html.Div(id="strategy-status-panel"),
        dbc.Modal(
            [
                dbc.ModalHeader(dbc.ModalTitle(id="strategy-modal-title")),
                dbc.ModalBody(id="strategy-modal-body"),
                dbc.ModalFooter(
                    dbc.Button(
                        "Close",
                        id="close-strategy-modal",
                        className="ms-auto",
                        n_clicks=0,
                    )
                ),
            ],
            id="strategy-info-modal",
            size="lg",
            is_open=False,
        ),
        html.H6("Open Positions", className="text-muted mb-2 mt-4"),
        html.Div(id="positions-table"),
        html.H6("Open Orders", className="text-muted mb-2 mt-4"),
        html.Div(id="orders-table"),
        html.H6("Recent Signals", className="text-muted mb-2 mt-4"),
        dbc.Row(
            [
                dbc.Col(
                    [
                        dcc.Dropdown(
                            id="signals-strategy-filter",
                            placeholder="All strategies",
                            clearable=True,
                            className="dash-bootstrap",
                        ),
                    ],
                    md=3,
                ),
            ],
            className="mb-2",
        ),
        html.Div(id="signals-table"),
        html.H6("Event Alerts", className="text-muted mb-2 mt-4"),
        html.Small(
            "Detected by Claude's research scanner.",
            className="text-muted d-block mb-2",
        ),
        html.Div(id="events-table"),
    ],
    fluid=True,
)


# -- Callbacks --


@callback(
    Output("portfolio-kpi-row", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_kpi(_n: int) -> dbc.Row:
    data = current_app.config["data"]
    s = data.get_account_summary()
    cards = [
        make_kpi_card("Equity", format_currency(s["equity"]), color="light"),
        make_kpi_card(
            "Daily P&L",
            format_currency(s["daily_pnl"], show_sign=True),
            subtitle=format_pct(s["daily_pct"], show_sign=True),
            color=pnl_color(s["daily_pnl"]),
        ),
        make_kpi_card(
            "Buying Power",
            format_currency(s["buying_power"]),
            color="info",
        ),
        make_kpi_card(
            "Drawdown",
            format_pct(s["drawdown_pct"]),
            color="danger"
            if s["drawdown_pct"] > 5
            else "warning"
            if s["drawdown_pct"] > 2
            else "secondary",
        ),
    ]
    return make_kpi_row(cards)


@callback(
    Output("equity-chart", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_equity_chart(_n: int) -> dcc.Graph:
    data = current_app.config["data"]
    summaries = data.get_equity_curve(days=90)
    fig = make_equity_figure(summaries)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


@callback(
    Output("drawdown-chart", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_drawdown_chart(_n: int) -> dcc.Graph:
    data = current_app.config["data"]
    series = data.get_drawdown_series()
    fig = make_drawdown_figure(series)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


@callback(
    Output("positions-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_positions(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    positions = data.get_positions()
    if not positions:
        return html.P("No open positions", className="text-muted")
    return dag.AgGrid(
        rowData=positions,
        columnDefs=_POSITION_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


@callback(
    Output("orders-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_orders(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    orders = data.get_open_orders()
    if not orders:
        return html.P("No open orders", className="text-muted")
    return dag.AgGrid(
        rowData=orders,
        columnDefs=_ORDER_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


# -- Bot Activity, Strategy Status, Recent Signals callbacks --


@callback(
    Output("bot-activity-row", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_bot_activity(_n: int) -> dbc.Row:
    data = current_app.config["data"]
    activity = data.get_bot_activity()

    last_tick = activity["last_tick"]
    last_tick_str = format_local_dt(last_tick, fmt="%-I:%M:%S %p %Z", fallback="Never")

    cards = [
        make_kpi_card(
            "Market",
            activity["market_status"],
            subtitle=activity["market_next"],
            color=activity["market_status_color"],
        ),
        make_kpi_card("Bot Status", activity["status"], color=activity["status_color"]),
        make_kpi_card("Last Tick", last_tick_str, color="light"),
        make_kpi_card("Ticks Today", str(activity["ticks_today"]), color="info"),
        make_kpi_card("Signals Today", str(activity["signals_today"]), color="info"),
        make_kpi_card("Trades Today", str(activity["trades_today"]), color="info"),
    ]
    return make_kpi_row(cards)


@callback(
    Output("strategy-status-panel", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_strategy_status(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    strategies = data.get_strategy_status()
    if not strategies:
        return html.P("No strategies configured", className="text-muted")
    return dag.AgGrid(
        id="strategy-status-grid",
        rowData=strategies,
        columnDefs=_STRATEGY_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={"domLayout": "autoHeight"},
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


@callback(
    Output("strategy-info-modal", "is_open"),
    Output("strategy-modal-title", "children"),
    Output("strategy-modal-body", "children"),
    Input("strategy-status-grid", "cellClicked"),
    Input("close-strategy-modal", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_strategy_modal(
    cell: dict[str, object] | None, _close_clicks: int
) -> tuple[bool, str, list[object]]:
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update, dash.no_update, dash.no_update  # type: ignore[return-value]

    trigger_id = ctx.triggered[0]["prop_id"].split(".")[0]

    if trigger_id == "close-strategy-modal":
        return False, "", []

    # Cell click — only open for the "name" column
    if cell and cell.get("colId") == "name":
        name = str(cell["value"])
        info = STRATEGY_EXPLANATIONS.get(name)
        if info:
            display_name = name.replace("_", " ").title()
            return True, display_name, make_strategy_explanation(info)

    return dash.no_update, dash.no_update, dash.no_update  # type: ignore[return-value]


@callback(
    Output("signals-strategy-filter", "options"),
    Input("refresh-interval", "n_intervals"),
)
def update_signals_filter_options(_n: int) -> list[dict[str, str]]:
    data = current_app.config["data"]
    return [{"label": s, "value": s} for s in data.strategy_names]


@callback(
    Output("signals-table", "children"),
    Input("signals-strategy-filter", "value"),
    Input("refresh-interval", "n_intervals"),
)
def update_signals_table(strategy: str | None, _n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    signals = data.get_recent_signals(hours=24, strategy=strategy)
    if not signals:
        return html.P("No signals in the last 24 hours", className="text-muted")
    return dag.AgGrid(
        rowData=signals,
        columnDefs=_SIGNAL_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={
            "domLayout": "autoHeight",
            "pagination": True,
            "paginationPageSize": 15,
        },
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )


@callback(
    Output("events-table", "children"),
    Input("refresh-interval", "n_intervals"),
)
def update_events_table(_n: int) -> dag.AgGrid | html.P:
    data = current_app.config["data"]
    events = data.get_recent_events(hours=48)
    if not events:
        return html.P("No recent event alerts", className="text-muted")
    return dag.AgGrid(
        rowData=events,
        columnDefs=_EVENT_COLS,
        defaultColDef={"sortable": True, "resizable": True},
        dashGridOptions={
            "domLayout": "autoHeight",
            "pagination": True,
            "paginationPageSize": 10,
            "tooltipShowDelay": 300,
        },
        className="ag-theme-alpine-dark",
        style={"width": "100%"},
    )
