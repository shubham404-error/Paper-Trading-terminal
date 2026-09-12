from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

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

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
@import url('https://api.fontshare.com/v2/css?f[]=clash-display@400,500,600,700&display=swap');
html, body, [class*="css"]  {
    font-family: 'Inter', sans-serif !important;
}
h1, h2, h3, h4, h5, h6 {
    font-family: 'Clash Display', sans-serif !important;
}
</style>
""", unsafe_allow_html=True)


def get_dvm_context(symbol: str) -> dict | None:
    db_path = Path("data/capitalsense_dvm.sqlite")
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM dvm_scores WHERE symbol = ?", (symbol,)).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return None

# Auto-seed mock data for DVM Context
_db_path = Path("data/capitalsense_dvm.sqlite")
_db_path.parent.mkdir(exist_ok=True)
with sqlite3.connect(_db_path) as _conn:
    _conn.execute('CREATE TABLE IF NOT EXISTS dvm_scores (symbol TEXT PRIMARY KEY, score TEXT, swot_1 TEXT, swot_2 TEXT)')
    _conn.execute('INSERT OR IGNORE INTO dvm_scores VALUES (?, ?, ?, ?)', ('RELIANCE', 'CapitalSense DVM: 78/100', 'Strong Momentum, but Valuation is historically expensive.', 'High institutional holding indicates stability.'))
    _conn.execute('INSERT OR IGNORE INTO dvm_scores VALUES (?, ?, ?, ?)', ('TCS', 'CapitalSense DVM: 85/100', 'Excellent cash flow generation.', 'Growth metrics slightly below historical averages.'))


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



def main_page():
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
        if search and not matches:
            matches = [search]
        if not matches:
            matches = list(SYMBOLS)
        symbol = st.selectbox("Instrument", matches, format_func=lambda x: f"{x} · {SYMBOLS.get(x, 'Custom')}")
    
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
    
    from market_data import get_multiple_live_ltp
    
    # Fetch initial state to discover open positions
    initial_state = engine.state()
    active_symbols = list(set([p["Symbol"] for p in initial_state["positions"]] + [symbol]))
    
    # Fetch live quotes for all active symbols
    quotes_map = get_multiple_live_ltp(active_symbols)
    quotes_map[symbol] = latest_price
    
    # Update engine with the current quote and get true state
    engine.process_quote(symbol, latest_price)
    state = engine.state(quotes_map)
    
    # ── Metrics row ──────────────────────────────────────────────────────────
    nav_delta = state["nav"] - INITIAL_CASH
    unrealised = state["unrealised_pnl"]
    realised = state["realised_pnl"]
    
    metric_cols = st.columns(6)
    with metric_cols[0].container(border=True):
        st.metric(
            "Net Asset Value",
            money(state["nav"]),
            delta=f"{pct(nav_delta, INITIAL_CASH)}",
        )
    with metric_cols[1].container(border=True):
        st.metric("Available Cash", money(state["free_margin"]))
    with metric_cols[2].container(border=True):
        st.metric("Allocated Margin", money(state["used_margin"]))
    with metric_cols[3].container(border=True):
        st.metric(
            "Unrealized P&L",
            money(unrealised),
            delta=f"{pct(unrealised, INITIAL_CASH)}",
        )
    with metric_cols[4].container(border=True):
        st.metric(
            "Realized P&L",
            money(realised),
            delta=f"{pct(realised, INITIAL_CASH)}",
        )
    with metric_cols[5].container(border=True):
        st.metric("LTP", money(latest_price))
    
    # ── Chart + Order Ticket ─────────────────────────────────────────────────
    chart_col, ticket_col = st.columns([7, 3], gap="large")
    
    @st.fragment
    def render_chart(symbol: str, frame: pd.DataFrame, interval: str, source: str):
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
            
        # Phase 3: Trendlyne Consensus Sandbox
        with st.expander("External Consensus (Trendlyne)"):
            components.html("""
            <div style="text-align: center; font-family: sans-serif; padding: 20px; border: 1px solid #444; border-radius: 8px; color: #fff; background-color: #1e1e1e;">
                <h3 style="margin-top:0; font-family: 'Clash Display', sans-serif;">Trendlyne Consensus</h3>
                <p>Placeholder for Trendlyne Widget integration.</p>
            </div>
            """, height=120)
            st.caption("CapitalSense does not factor external widgets into its proprietary scoring.")
    
    with chart_col:
        render_chart(symbol, frame, interval, source)
    
    # ── Order Ticket ─────────────────────────────────────────────────────────
    with ticket_col:
        # Phase 2: Trade Context Integration
        dvm_context = get_dvm_context(symbol)
        with st.container(border=True):
            if dvm_context:
                st.markdown(f"**{dvm_context['score']}**")
                st.markdown(f"- {dvm_context['swot_1']}")
                st.markdown(f"- {dvm_context['swot_2']}")
            else:
                st.markdown("*No fundamental data available*")
                
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
            pos_qty = next((p["Quantity"] for p in state["positions"] if p["Symbol"] == symbol), 0)
            side_sign = 1 if side == "BUY" else -1
            delta = side_sign * int(quantity)
            
            if pos_qty * delta < 0:
                close_qty = min(abs(pos_qty), abs(delta))
                open_qty = abs(delta) - close_qty
                margin_required = (open_qty * latest_price) / MARGIN_MULTIPLIER
            else:
                margin_required = notional / MARGIN_MULTIPLIER
    
            # Phase 1: Margin Calculator Text
            st.markdown(f"**Margin Required:** {money(margin_required)}")
            avail_color = '#00cc00' if state['free_margin'] >= margin_required else '#ff4444'
            st.markdown(f"**Available Cash:** <span style='color: {avail_color}'>{money(state['free_margin'])}</span>", unsafe_allow_html=True)
            if margin_required > state["free_margin"] and margin_required > 0:
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
                        quotes=quotes_map,
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
    positions_tab, orders_tab, fills_tab, history_tab, scanner_tab = st.tabs([
        f"Positions ({len(state['positions'])})",
        f"Pending Orders ({len(state['open_orders'])})",
        f"Fills History ({len(state['fills'])})",
        f"Order History ({len(all_orders)})",
        "Arbitrage Scanner ⚡"
    ])
    
    with positions_tab:
        if state["positions"]:
            pos_data = state["positions"]
            # Add P&L % column
            for p in pos_data:
                avg = p.get("Avg Entry", 0)
                p["P&L %"] = f"{((p.get('Unrealized P&L', 0)) / (avg * p.get('Quantity', 1)) * 100):+.2f}%" if avg and p.get("Quantity") else "—"
            positions = pd.DataFrame(pos_data)
            
            def color_pnl(val):
                if pd.isna(val): return ''
                color = '#00cc00' if val > 0 else '#ff4444' if val < 0 else 'gray'
                return f'color: {color}'
                
            styled_positions = positions.style.map(color_pnl, subset=['Unrealized P&L'])
            
            st.dataframe(styled_positions, use_container_width=True, hide_index=True, column_config={
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
    
    with scanner_tab:
        st.subheader("F&O Cash-and-Carry Arbitrage Scanner")
        st.caption("Identify mispriced spot-futures spreads. **Watch-only mode.**")
        
        from arbitrage_engine import BROKERS, compute, rank_opportunities
        from market_data import get_arbitrage_snapshot
        
        col1, col2 = st.columns([1, 2])
        with col1:
            broker_name = st.selectbox("Select Broker Config", list(BROKERS.keys()))
            funding_rate = st.number_input("Margin Funding Cost (Annual %)", min_value=0.0, max_value=25.0, value=12.0, step=1.0) / 100.0
        
        if st.button("Run Scanner", type="primary", use_container_width=True):
            with st.spinner("Fetching live futures and spot prices..."):
                snapshot = get_arbitrage_snapshot(list(SYMBOLS.keys()))
                
                ops = []
                for item in snapshot:
                    try:
                        days_to_expiry = max(1, (pd.to_datetime(item["expiry_date"]).date() - pd.Timestamp.now().date()).days)
                        op = compute(
                            symbol=item["symbol"],
                            spot_price=item["spot_price"],
                            future_price=item["future_price"],
                            expiry_date=item["expiry_date"],
                            days_to_expiry=days_to_expiry,
                            lot_size=item["lot_size"],
                            broker_name=broker_name,
                            funding_rate_annual=funding_rate,
                            expected_dividends=0.0,
                            slippage_bps=5.0,
                            is_liquid=item["is_liquid"],
                            borrow_available=False
                        )
                        ops.append(op)
                    except Exception as exc:
                        st.error(f"Failed to process {item['symbol']}: {exc}")
                        
                ranked_ops = rank_opportunities(ops)
                
                if ranked_ops:
                    st.success(f"Found {len(ranked_ops)} active opportunities.")
                    for op in ranked_ops:
                        with st.container(border=True):
                            st.markdown(f"**{op.symbol}** (Exp: {op.expiry_date} • {op.days_to_expiry} days) | Lot Size: {op.lot_size} | **{op.classification}**")
                            
                            cols = st.columns(5)
                            cols[0].metric("Spot", money(op.spot_price))
                            cols[1].metric("Future", money(op.future_price))
                            cols[2].metric("Gross Spread", money(op.raw_premium * op.lot_size))
                            cols[3].metric("Est. Total Cost", money(op.total_cost_absolute))
                            cols[4].metric("Net Ann. Return", f"{op.net_annualized_return * 100:.2f}%")
                else:
                    st.info("No actionable arbitrage opportunities found above edge thresholds.")
                    
                with st.expander("View All Scanned Pairs (Raw Engine Output)"):
                    if not ops:
                        st.error("Zero pairs were processed. This means the Upstox API did not return any spot or futures data. Please double-check your API token.")
                    else:
                        st.write("Here is the exact math for every pair scanned. Most will correctly show as 'NO EDGE' because the strict delivery taxes (STT) and transaction costs consume the gross spread.")
                        debug_data = []
                        for o in sorted(ops, key=lambda x: x.net_annualized_return, reverse=True):
                            debug_data.append({
                                "Symbol": o.symbol,
                                "Spot": money(o.spot_price),
                                "Future": money(o.future_price),
                                "Spread": money(o.raw_premium * o.lot_size),
                                "Total Costs": money(o.total_cost_absolute),
                                "Net Return": money(o.net_return_absolute),
                                "Ann. Return %": f"{o.net_annualized_return * 100:.2f}%",
                                "Classification": o.classification
                            })
                        st.dataframe(debug_data, use_container_width=True)
                    
                st.warning("⚠️ **Dividends assumed to be zero.** Fair value is understated for dividend-paying underlyings near an ex-date.")
    
    st.caption("Research and education tool. Not a broker, not investment advice, and not a SEBI-registered intermediary.")
    

def guide_page():
    st.title("User Guide: Paper Desk")
    st.markdown("""
    Welcome to the CapitalSense Paper Desk. This is a risk-free environment to test your trading strategies with live market data, without routing real money to a broker.
    
    ### ?? Intended Outputs
    - **Live Execution:** Simulated order fills for market and limit orders based on real-time order book data (via Upstox) or delayed Yahoo Finance data.
    - **Trade Context:** The CapitalSense Scorecard appears before you execute, giving you a fundamental reality check on the asset's Quality, Valuation, and Trend.
    - **Portfolio Ledger:** A real-time P&L tracking table showing your active positions and historical execution log.
    
    ### ?? Required Inputs
    - **Instrument Search:** Use the sidebar to search for and select the asset you wish to trade.
    - **Order Ticket:** Located on the right side of the screen. Enter the quantity, select Buy/Sell, and choose between Market or Limit order types.
    - **Reset Account:** If you blow up your simulated account, you can reset it using the button in the sidebar (this wipes all local order history).
    
    ### ?? Best Practices
    - Never place a purely technical trade without checking the **CapitalSense Context Card** right above the buy button. If the score is low, reconsider your edge.
    - Use the **Trendlyne Consensus** expander below the chart to see if external analysts agree with your directional bias.
    """)

pages = {
    "Start": [
        st.Page(guide_page, title="User Guide", icon="📖", default=True)
    ],
    "Trading": [
        st.Page(main_page, title="Trading Terminal", icon="📈")
    ]
}

pg = st.navigation(pages, position="sidebar")
pg.run()
