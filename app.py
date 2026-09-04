from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from market_data import (
    SYMBOLS,
    available_intervals,
    get_live_ltp,
    indicators,
    is_upstox_connected,
    load_data,
)
from paper_engine import INITIAL_CASH, MARGIN_MULTIPLIER, PaperEngine


st.set_page_config(page_title="CapitalSense Paper Desk", page_icon="📈", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 1500px; padding-top: 1.2rem;}
    [data-testid="stMetricValue"] {font-size: 1.35rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_engine() -> PaperEngine:
    return PaperEngine(Path(__file__).with_name("paper_trading.sqlite3"))


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.2f}"


def pct(value: float, base: float) -> str:
    """Format a percentage change relative to *base*."""
    if base == 0:
        return ""
    return f"{value / base * 100:+.2f}%"


engine = get_engine()
st.title("CapitalSense Paper Desk")
st.caption("MAET-inspired research and paper-execution workstation · no broker routing · no real money")

# ── Sidebar ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.subheader("Workspace")

    # Connection status
    if is_upstox_connected():
        st.success("🟢 Upstox Live")
    else:
        st.warning("🟡 Fallback mode (Yahoo / Demo)")

    search = st.text_input("Search symbol", placeholder="RELIANCE, TCS…").strip().upper()
    matches = [s for s in SYMBOLS if not search or search in s or search in SYMBOLS[s].upper()]
    if not matches:
        matches = list(SYMBOLS)
    symbol = st.selectbox("Instrument", matches, format_func=lambda x: f"{x} · {SYMBOLS[x]}")

    # Interval selector
    intervals = available_intervals()
    interval = st.selectbox(
        "Chart interval",
        intervals,
        index=intervals.index("1D") if "1D" in intervals else 0,
    )

    st.divider()
    st.caption("Paper account")
    st.write(f"Starting NAV: {money(INITIAL_CASH)}")
    st.info("Upstox provides live data when configured. Otherwise the app uses Yahoo Finance (delayed) or demo data.")
    confirm_reset = st.checkbox("I understand reset deletes this local paper account")
    if st.button("Reset account", disabled=not confirm_reset, use_container_width=True):
        engine.reset()
        st.success("Paper account reset")
        st.rerun()


# ── Data loading ─────────────────────────────────────────────────────────
frame, source = load_data(symbol, interval=interval)
frame = indicators(frame)
latest_price = float(frame["Close"].iloc[-1])

# Try live LTP (zero API calls if cached).
live_ltp = get_live_ltp(symbol)
if live_ltp is not None:
    latest_price = live_ltp

engine.process_quote(symbol, latest_price)
state = engine.state({symbol: latest_price})

# ── Metrics row ──────────────────────────────────────────────────────────
nav_delta = state["nav"] - INITIAL_CASH
unrealised = state["unrealised_pnl"]
realised = state["realised_pnl"]

metric_cols = st.columns(6)
metric_cols[0].metric(
    "Net Asset Value",
    money(state["nav"]),
    delta=f"{pct(nav_delta, INITIAL_CASH)}",
)
metric_cols[1].metric("Available Cash", money(state["free_margin"]))
metric_cols[2].metric("Allocated Margin", money(state["used_margin"]))
metric_cols[3].metric(
    "Unrealized P&L",
    money(unrealised),
    delta=f"{pct(unrealised, INITIAL_CASH)}",
)
metric_cols[4].metric(
    "Realized P&L",
    money(realised),
    delta=f"{pct(realised, INITIAL_CASH)}",
)
metric_cols[5].metric("LTP", money(latest_price))

# ── Chart + Order Ticket ─────────────────────────────────────────────────
chart_col, ticket_col = st.columns([2.25, 1], gap="large")

with chart_col:
    st.subheader(f"{symbol} · {SYMBOLS.get(symbol, symbol)}")
    st.caption(f"{source} · {len(frame):,} bars · interval: {interval} · paper fills use the displayed Level-1 mark")
    chart_controls = st.columns(8)
    chart_type = chart_controls[0].selectbox("Chart", ["Candles", "Line"], label_visibility="collapsed")
    show_sma = chart_controls[1].checkbox("SMA", True)
    show_ema = chart_controls[2].checkbox("EMA", False)
    show_rsi = chart_controls[3].checkbox("RSI", False)
    show_volume = chart_controls[4].checkbox("Volume", True)
    show_macd = chart_controls[5].checkbox("MACD", False)
    show_vwap = chart_controls[6].checkbox("VWAP", False)
    show_bb = chart_controls[7].checkbox("BB", False)

    fig = go.Figure()
    if chart_type == "Candles":
        fig.add_trace(go.Candlestick(x=frame.index, open=frame["Open"], high=frame["High"], low=frame["Low"], close=frame["Close"], name=symbol))
    else:
        fig.add_trace(go.Scatter(x=frame.index, y=frame["Close"], mode="lines", name="Close"))
    if show_sma:
        fig.add_trace(go.Scatter(x=frame.index, y=frame["SMA"], mode="lines", name="SMA 20", line=dict(width=1)))
    if show_ema:
        fig.add_trace(go.Scatter(x=frame.index, y=frame["EMA"], mode="lines", name="EMA 50", line=dict(width=1)))
    if show_vwap:
        fig.add_trace(go.Scatter(x=frame.index, y=frame["VWAP"], mode="lines", name="VWAP", line=dict(dash="dash", width=1)))
    if show_bb:
        fig.add_trace(go.Scatter(x=frame.index, y=frame["BB_Upper"], mode="lines", name="BB Upper", line=dict(width=1, color="rgba(128,128,128,0.5)")))
        fig.add_trace(go.Scatter(x=frame.index, y=frame["BB_Lower"], mode="lines", name="BB Lower", fill="tonexty", fillcolor="rgba(128,128,128,0.08)", line=dict(width=1, color="rgba(128,128,128,0.5)")))
    if show_volume:
        fig.add_trace(go.Bar(x=frame.index, y=frame["Volume"], name="Volume", opacity=0.18, yaxis="y2"))
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price"),
        yaxis2=dict(title="Volume", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})

    # RSI sub-chart
    if show_rsi:
        rsi_frame = frame[["RSI"]].dropna()
        rsi_fig = go.Figure(go.Scatter(x=rsi_frame.index, y=rsi_frame["RSI"], name="RSI 14"))
        rsi_fig.add_hline(y=70, line_dash="dot", annotation_text="Overbought")
        rsi_fig.add_hline(y=30, line_dash="dot", annotation_text="Oversold")
        rsi_fig.update_layout(height=180, margin=dict(l=10, r=10, t=5, b=5), yaxis=dict(range=[0, 100]))
        st.plotly_chart(rsi_fig, use_container_width=True, config={"displaylogo": False})

    # MACD sub-chart
    if show_macd:
        macd_frame = frame[["MACD", "MACD_Signal", "MACD_Hist"]].dropna()
        macd_fig = go.Figure()
        macd_fig.add_trace(go.Scatter(x=macd_frame.index, y=macd_frame["MACD"], mode="lines", name="MACD", line=dict(width=1.5)))
        macd_fig.add_trace(go.Scatter(x=macd_frame.index, y=macd_frame["MACD_Signal"], mode="lines", name="Signal", line=dict(width=1.5)))
        colors = ["green" if v >= 0 else "red" for v in macd_frame["MACD_Hist"]]
        macd_fig.add_trace(go.Bar(x=macd_frame.index, y=macd_frame["MACD_Hist"], name="Histogram", marker_color=colors, opacity=0.5))
        macd_fig.update_layout(height=180, margin=dict(l=10, r=10, t=5, b=5))
        st.plotly_chart(macd_fig, use_container_width=True, config={"displaylogo": False})

# ── Order Ticket ─────────────────────────────────────────────────────────
with ticket_col:
    st.subheader("Order Ticket")
    st.caption("Orders are simulated against the latest displayed quote.")
    with st.form("order_ticket"):
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True, format_func=lambda x: "BUY" if x == "BUY" else "SELL / SHORT")
        order_type = st.selectbox("Order type", ["MARKET", "LIMIT", "STOP_LOSS_LIMIT"])
        quantity = st.number_input("Quantity (shares)", min_value=1, value=10, step=1)
        limit_price = None
        stop_price = None
        if order_type in {"LIMIT", "STOP_LOSS_LIMIT"}:
            limit_price = st.number_input("Limit price", min_value=0.05, value=round(latest_price, 2), step=0.05)
        if order_type == "STOP_LOSS_LIMIT":
            stop_price = st.number_input("Stop trigger", min_value=0.05, value=round(latest_price, 2), step=0.05)
        use_bracket = st.checkbox("Attach stop loss / take profit")
        stop_loss = take_profit = None
        if use_bracket:
            bracket_cols = st.columns(2)
            stop_loss = bracket_cols[0].number_input("Stop loss", min_value=0.05, value=max(0.05, round(latest_price * 0.95, 2)), step=0.05)
            take_profit = bracket_cols[1].number_input("Take profit", min_value=0.05, value=round(latest_price * 1.10, 2), step=0.05)

        # Margin preview
        notional = float(quantity) * latest_price
        margin_required = notional / MARGIN_MULTIPLIER
        st.write(f"Estimated notional: {money(notional)}")
        st.write(f"Margin required: {money(margin_required)}")
        if margin_required > state["free_margin"]:
            st.warning(f"⚠️ Exceeds available margin ({money(state['free_margin'])})")

        submitted = st.form_submit_button(f"{side} {int(quantity)} {symbol}", use_container_width=True, type="primary")
        if submitted:
            try:
                result = engine.place_order(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=int(quantity),
                    current_price=latest_price,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                if result["status"] == "REJECTED":
                    st.error(f"Order rejected: {result.get('reject_reason', 'Insufficient margin')}")
                else:
                    st.success(f"Order {result['status'].lower()}: {result['order_id'][:8]}")
                    st.rerun()
            except ValueError as exc:
                st.error(str(exc))

# ── Tabs: Positions / Orders / Fills / History ───────────────────────────
st.divider()

all_orders = state.get("all_orders", [])
positions_tab, orders_tab, fills_tab, history_tab = st.tabs([
    f"Positions ({len(state['positions'])})",
    f"Pending Orders ({len(state['open_orders'])})",
    f"Fills History ({len(state['fills'])})",
    f"Order History ({len(all_orders)})",
])

with positions_tab:
    if state["positions"]:
        pos_data = state["positions"]
        # Add P&L % column
        for p in pos_data:
            avg = p.get("Avg Entry", 0)
            p["P&L %"] = f"{((p.get('Unrealized P&L', 0)) / (avg * p.get('Quantity', 1)) * 100):+.2f}%" if avg and p.get("Quantity") else "—"
        positions = pd.DataFrame(pos_data)
        st.dataframe(positions, use_container_width=True, hide_index=True, column_config={
            "Avg Entry": st.column_config.NumberColumn(format="₹%.2f"),
            "LTP": st.column_config.NumberColumn(format="₹%.2f"),
            "Unrealized P&L": st.column_config.NumberColumn(format="₹%.2f"),
            "Stop Loss": st.column_config.NumberColumn(format="₹%.2f"),
            "Take Profit": st.column_config.NumberColumn(format="₹%.2f"),
        })
    else:
        st.info("No open positions. Place an order to execute a paper trade.")

with orders_tab:
    if state["open_orders"]:
        orders = pd.DataFrame(state["open_orders"])
        st.dataframe(orders, use_container_width=True, hide_index=True)
        cancel_id = st.selectbox("Cancel order", orders["id"].tolist(), format_func=lambda x: x[:8])
        if st.button("Cancel selected order"):
            engine.cancel_order(cancel_id)
            st.rerun()
    else:
        st.info("No pending orders.")

with fills_tab:
    if state["fills"]:
        fills = pd.DataFrame(state["fills"])
        st.dataframe(fills, use_container_width=True, hide_index=True, column_config={
            "price": st.column_config.NumberColumn(format="₹%.2f"),
            "quantity": st.column_config.NumberColumn(format="%d"),
        })
    else:
        st.info("No paper fills recorded in this local account.")

with history_tab:
    if all_orders:
        history = pd.DataFrame(all_orders)
        st.dataframe(history, use_container_width=True, hide_index=True, column_config={
            "limit_price": st.column_config.NumberColumn(format="₹%.2f"),
            "stop_price": st.column_config.NumberColumn(format="₹%.2f"),
        })
    else:
        st.info("No order history. Orders will appear here once placed.")

st.caption("Research and education tool. Not a broker, not investment advice, and not a SEBI-registered intermediary.")
