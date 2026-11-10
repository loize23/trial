"""
streamlit_app.py -- Modern web UI for the ph-crypto-agent.

Run with:  streamlit run streamlit_app.py

Pages:
  Dashboard   -- live portfolio, positions, risk state, kill switch
  Backtest    -- run backtests, view equity curves and metrics
  Trade       -- run single cycles, view signals per symbol
  Settings    -- edit config.json (instrument types, leverage, risk)
  Journal     -- view trades.csv history

Requires:  pip install streamlit pandas plotly
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from agent.agent import TradingAgent
from agent.backtest import run_backtest
from agent.datafeed import fetch_candles, instrument_type_for, last_price
from agent.risk import RiskConfig, RiskState
from agent.strategy import list_strategies

# --------------------------------------------------------------------------- page config

st.set_page_config(
    page_title="ph-crypto-agent",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for a cleaner, more modern look
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    .metric-card { background: #f0f2f6; padding: 1rem; border-radius: 0.75rem; border: 1px solid #e6e8ef; }
    .metric-big { font-size: 1.75rem; font-weight: 700; margin: 0; }
    .metric-label { font-size: 0.85rem; color: #6b7280; margin: 0; }
    .positive { color: #10b981; }
    .negative { color: #ef4444; }
    .neutral { color: #6b7280; }
    .kill-switch-on { color: #ef4444; font-weight: 700; }
    .kill-switch-off { color: #10b981; font-weight: 700; }
    .sidebar-section { margin-bottom: 1.5rem; }
    .stButton > button { width: 100%; }
    </style>
    """,
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- helpers

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.json")
EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.example.json")


def load_config() -> dict:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def agent_for_config(cfg: dict) -> TradingAgent:
    return TradingAgent(cfg)


def pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "positive"
    if pnl < 0:
        return "negative"
    return "neutral"


def equity_curve_chart(equity_curve: list[float]) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            y=equity_curve,
            mode="lines",
            name="Equity",
            line=dict(color="#3b82f6", width=2),
        )
    )
    fig.update_layout(
        height=300,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Bar",
        yaxis_title="Equity (USDT)",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=11),
        showlegend=False,
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb")
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb")
    return fig


def pnl_chart(trades: list[dict]) -> go.Figure:
    if not trades:
        fig = go.Figure()
        fig.update_layout(height=250, margin=dict(l=20, r=20, t=20, b=20))
        return fig

    pnls = [t["pnl"] for t in trades]
    colors = ["#10b981" if p > 0 else "#ef4444" for p in pnls]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=list(range(1, len(pnls) + 1)),
            y=pnls,
            marker_color=colors,
            name="PnL",
        )
    )
    fig.update_layout(
        height=250,
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis_title="Trade #",
        yaxis_title="PnL (USDT)",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(size=11),
        showlegend=False,
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb")
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb")
    return fig


def load_trades() -> pd.DataFrame | None:
    cfg = load_config()
    path = cfg.get("trades_csv", "data/trades.csv")
    if os.path.exists(path):
        return pd.read_csv(path)
    return None


# --------------------------------------------------------------------------- session state

if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()
if "agent" not in st.session_state:
    st.session_state.agent = agent_for_config(st.session_state.cfg)
if "last_summary" not in st.session_state:
    st.session_state.last_summary = None
if "backtest_results" not in st.session_state:
    st.session_state.backtest_results = None

cfg = st.session_state.cfg
agent = st.session_state.agent

# --------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("📈 ph-crypto-agent")
    st.caption("Paper-first crypto trading agent")

    st.divider()

    mode = cfg.get("mode", "paper")
    mode_color = "neutral" if mode == "paper" else "negative"
    st.markdown(
        f"**Mode:** <span class='{mode_color}'>{mode.upper()}</span>",
        unsafe_allow_html=True,
    )

    # Kill switch
    kill_exists = os.path.exists("KILL")
    ks_class = "kill-switch-on" if kill_exists else "kill-switch-off"
    ks_text = "ACTIVE — trading blocked" if kill_exists else "inactive"
    st.markdown(
        f"Kill switch: <span class='{ks_class}'>{ks_text}</span>",
        unsafe_allow_html=True,
    )

    if st.button("🗑️ Delete KILL file", disabled=not kill_exists, use_container_width=True):
        if os.path.exists("KILL"):
            os.remove("KILL")
            st.rerun()

    st.divider()

    st.subheader("Symbols")
    for sym in cfg.get("symbols", []):
        itype = instrument_type_for(sym, cfg)
        inst = cfg.get("symbol_config", {}).get(sym, cfg.get("default_instrument", {}))
        lev = inst.get("leverage", 1) if isinstance(inst, dict) else 1
        st.write(f"• {sym}  <small>({itype}, {lev}x)</small>", unsafe_allow_html=True)

    st.divider()

    page = st.radio(
        "Navigate",
        ["Dashboard", "Backtest", "Trade", "Settings", "Journal"],
        label_visibility="collapsed",
    )

    st.divider()
    st.caption("Not financial advice. Paper trading by default.")

# --------------------------------------------------------------------------- dashboard

if page == "Dashboard":
    st.title("Dashboard")

    # Top metrics row
    col1, col2, col3, col4, col5 = st.columns(5)

    try:
        prices = agent.prices()
        equity = agent.broker.equity(prices)
        start = float(agent.broker.state.get("starting_balance", equity) or equity)
        ret = (equity - start) / start * 100.0 if start else 0.0
    except Exception as e:
        st.error(f"Failed to fetch prices: {e}")
        equity = start = ret = 0.0
        prices = {}

    with col1:
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.markdown(f'<p class="metric-big">${equity:,.2f}</p>', unsafe_allow_html=True)
        st.markdown('<p class="metric-label">Equity</p>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with col2:
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.markdown(f'<p class="metric-big">{ret:+.2f}%</p>', unsafe_allow_html=True)
        st.markdown('<p class="metric-label">Return</p>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with col3:
        cash = float(agent.broker.state.get("cash", 0) or 0)
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.markdown(f'<p class="metric-big">${cash:,.2f}</p>', unsafe_allow_html=True)
        st.markdown('<p class="metric-label">Cash</p>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with col4:
        n_pos = len(agent.broker.positions)
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.markdown(f'<p class="metric-big">{n_pos}</p>', unsafe_allow_html=True)
        st.markdown('<p class="metric-label">Open Positions</p>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with col5:
        halted = agent.risk.state.halted_reason or "—"
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.markdown(f'<p class="metric-big">{halted[:20]}</p>', unsafe_allow_html=True)
        st.markdown('<p class="metric-label">Risk Status</p>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    # Active strategy info
    strat_info = list_strategies().get(agent.strategy_name, {})
    col_strat1, col_strat2 = st.columns([2, 3])
    with col_strat1:
        st.markdown(f"**Strategy:** {strat_info.get('display_name', agent.strategy_name)}")
        st.markdown(f"**Name:** `{agent.strategy_name}`")
    with col_strat2:
        st.caption(strat_info.get("description", "No description available."))

    st.divider()

    # Run cycle button
    col_btn, col_info = st.columns([1, 4])
    with col_btn:
        if st.button("▶️ Run Trading Cycle", type="primary", use_container_width=True):
            try:
                summary = agent.cycle()
                st.session_state.last_summary = summary
                st.success("Cycle completed")
                st.rerun()
            except Exception as e:
                st.error(f"Cycle error: {e}")

    with col_info:
        st.caption("Runs one full cycle: exits → entries → risk gate → persist.")

    st.divider()

    # Positions table
    st.subheader("Open Positions")
    if agent.broker.positions:
        rows = []
        for sym, pos in agent.broker.positions.items():
            mark = prices.get(sym, pos["entry_price"])
            side = pos.get("side", "long")
            itype = pos.get("instrument_type", "spot")
            qty = pos["qty"]
            entry = pos["entry_price"]
            mult = pos.get("contract_multiplier", 1)
            if itype == "spot":
                unrealised = (mark - entry) * qty
            else:
                if side == "long":
                    unrealised = (mark - entry) * qty * mult
                else:
                    unrealised = (entry - mark) * qty * mult
            liq = pos.get("liquidation_price")
            rows.append({
                "Symbol": sym,
                "Side": side.title(),
                "Type": itype,
                "Entry": f"{entry:,.6f}",
                "Mark": f"{mark:,.6f}",
                "Unrealised": f"{unrealised:+.2f}",
                "SL": f"{pos.get('stop_loss', '—')}",
                "TP": f"{pos.get('take_profit', '—')}",
                "Liq": f"{liq:,.6f}" if liq else "—",
                "Leverage": pos.get("leverage", 1),
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No open positions.")

    st.divider()

    # Risk state
    st.subheader("Risk Guardrails")
    rs = agent.risk.state
    col_r1, col_r2, col_r3 = st.columns(3)
    with col_r1:
        st.metric("Consecutive Losses", rs.consecutive_losses)
        st.metric("Equity Peak", f"${rs.equity_peak:,.2f}")
    with col_r2:
        st.metric("Day Start Equity", f"${rs.day_start_equity:,.2f}")
        st.metric("Halted Reason", rs.halted_reason or "None")
    with col_r3:
        dd = (rs.equity_peak - equity) / rs.equity_peak * 100.0 if rs.equity_peak else 0.0
        st.metric("Drawdown", f"{dd:.2f}%")
        st.metric("Rejected (last 5)", len(rs.rejected))

    # Last cycle summary
    if st.session_state.last_summary:
        st.divider()
        st.subheader("Last Cycle Summary")
        st.json(st.session_state.last_summary)

# --------------------------------------------------------------------------- backtest

elif page == "Backtest":
    st.title("Backtest")

    st.caption("Test the strategy on real historical candles. No look-ahead bias.")

    col_b1, col_b2 = st.columns([1, 2])

    with col_b1:
        st.subheader("Parameters")
        default_symbols = ",".join(cfg.get("symbols", ["BTC/USDT"]))
        symbols_input = st.text_input("Symbols (comma-separated)", value=default_symbols)
        timeframe = st.selectbox("Timeframe", ["1h", "4h", "1d"], index=0)
        limit = st.number_input("Candles", min_value=100, max_value=5000, value=300, step=100)
        starting_balance = st.number_input("Starting Balance (USDT)", min_value=100.0, value=float(cfg.get("starting_balance", 1000.0)), step=100.0)

        run_btn = st.button("🚀 Run Backtest", type="primary", use_container_width=True)

        if run_btn:
            bt_cfg = dict(cfg)
            bt_cfg["timeframe"] = timeframe
            bt_cfg["symbols"] = [s.strip() for s in symbols_input.split(",") if s.strip()]
            bt_cfg["starting_balance"] = starting_balance
            try:
                with st.spinner("Fetching data and running backtest..."):
                    results = run_backtest(bt_cfg, bt_cfg["symbols"], limit)
                st.session_state.backtest_results = results
                st.success("Backtest complete")
            except Exception as e:
                st.error(f"Backtest failed: {e}")
                st.exception(e)

    with col_b2:
        st.subheader("Results")
        results = st.session_state.backtest_results
        if results:
            for sym, m in results.items():
                if "error" in m:
                    st.error(f"{sym}: {m['error']}")
                    continue

                with st.container():
                    st.markdown(f"### {sym}  ({m.get('instrument_type', 'spot')} {m.get('leverage', 1)}x)")

                    r1, r2, r3, r4 = st.columns(4)
                    with r1:
                        st.metric("Return", f"{m['total_return_pct']:+.2f}%", f"B&H: {m['buy_and_hold_pct']:+.2f}%")
                    with r2:
                        st.metric("Max Drawdown", f"{m['max_drawdown_pct']:.2f}%")
                    with r3:
                        st.metric("Win Rate", f"{m['win_rate_pct']}%")
                    with r4:
                        st.metric("Profit Factor", f"{m['profit_factor']}")

                    r5, r6, r7, r8 = st.columns(4)
                    with r5:
                        st.metric("Trades", m["trades"])
                    with r6:
                        st.metric("Avg Hold", f"{m['avg_hold_bars']} bars")
                    with r7:
                        st.metric("Fees", f"{m['total_fees']:.2f}")
                    with r8:
                        st.metric("Funding", f"{m.get('total_funding', 0):.2f}")

                    # Equity curve
                    if m.get("equity_curve"):
                        fig = equity_curve_chart(m["equity_curve"])
                        st.plotly_chart(fig, use_container_width=True, key=f"equity_{sym}")

                    # PnL distribution
                    if m.get("trades_detail"):
                        fig2 = pnl_chart(m["trades_detail"])
                        st.plotly_chart(fig2, use_container_width=True, key=f"pnl_{sym}")

                    st.divider()

            # Download JSON
            st.download_button(
                "📥 Download Results (JSON)",
                data=json.dumps(results, indent=2),
                file_name=f"backtest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
            )
        else:
            if results is not None:
                total_trades = sum(m.get("trades", 0) for m in results.values())
                if total_trades == 0:
                    st.warning("⚠️ No trades were generated. Try: (1) a different timeframe, (2) a different strategy in Settings, or (3) more candles.")
            st.info("Click **🚀 Run Backtest** to test the strategy on live market data.")

# --------------------------------------------------------------------------- trade

elif page == "Trade":
    st.title("Trade / Signals")

    st.caption("View current signals and run trading cycles.")

    col_t1, col_t2 = st.columns([1, 2])

    with col_t1:
        st.subheader("Signals per Symbol")
        if st.button("🔄 Refresh Signals", use_container_width=True):
            st.rerun()

        for sym in cfg.get("symbols", []):
            itype = instrument_type_for(sym, cfg)
            try:
                candles = fetch_candles(sym, cfg.get("timeframe", "1h"), cfg.get("candles", 300), agent.source, itype)
                sig = agent.strategy.evaluate(sym, candles, cfg)
                price = float(candles[-1]["c"])

                st.markdown(f"**{sym}**  <small>({itype})</small>", unsafe_allow_html=True)

                if sig.action == "buy":
                    st.success(f"🟢 BUY  @ ${price:,.6f}  |  {sig.reason}")
                    st.caption(f"SL: {sig.stop_loss}  TP: {sig.take_profit}  Strength: {sig.strength:.2f}")
                elif sig.action == "sell":
                    can_short = itype in ("futures", "forex")
                    if can_short and sig.side == "short":
                        st.warning(f"🔴 SHORT  @ ${price:,.6f}  |  {sig.reason}")
                    else:
                        st.info(f"🔴 SELL (exit long)  @ ${price:,.6f}  |  {sig.reason}")
                    st.caption(f"SL: {sig.stop_loss}  TP: {sig.take_profit}  Strength: {sig.strength:.2f}")
                else:
                    st.info(f"⚪ HOLD  @ ${price:,.6f}  |  {sig.reason}")

                with st.expander("Indicators"):
                    st.json(sig.indicators)

                st.divider()
            except Exception as e:
                st.error(f"{sym}: {e}")

    with col_t2:
        st.subheader("Quick Actions")
        if st.button("▶️ Run One Cycle", type="primary", use_container_width=True):
            try:
                summary = agent.cycle()
                st.session_state.last_summary = summary
                st.success("Cycle executed")
                st.rerun()
            except Exception as e:
                st.error(f"Cycle error: {e}")

        st.divider()

        st.subheader("Manual Position Close")
        if agent.broker.positions:
            sym_to_close = st.selectbox("Symbol", list(agent.broker.positions.keys()))
            if st.button("❌ Close Position", use_container_width=True):
                try:
                    mark = prices.get(sym_to_close, 0) if (prices := agent.prices()) else 0
                    rec = agent.broker.close_position(sym_to_close, mark, "manual_close")
                    agent.risk.record_close(rec.get("pnl", 0.0))
                    agent.broker.state["risk"] = agent.risk.state.to_dict()
                    agent.broker.save()
                    st.success(f"Closed {sym_to_close} — PnL {rec.get('pnl', 0):+.2f}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Close failed: {e}")
        else:
            st.info("No positions to close.")

        st.divider()

        st.subheader("Reset Paper State")
        if st.button("🔄 Reset to Starting Balance", type="secondary", use_container_width=True):
            agent.broker.reset()
            st.session_state.agent = agent_for_config(cfg)
            st.success("Paper state reset")
            st.rerun()

# --------------------------------------------------------------------------- settings

elif page == "Settings":
    st.title("Settings")

    st.caption("Edit configuration. Changes are saved to config/config.json.")

    tab1, tab2, tab3, tab4 = st.tabs(["General", "Strategies", "Strategy Params", "Risk"])

    with tab1:
        st.subheader("General")
        mode = st.selectbox("Mode", ["paper", "live"], index=0 if cfg.get("mode") == "paper" else 1)
        base_currency = st.text_input("Base Currency", value=cfg.get("base_currency", "USDT"))
        starting_balance = st.number_input("Starting Balance", value=float(cfg.get("starting_balance", 1000.0)), step=100.0)
        loop_seconds = st.number_input("Loop Seconds", value=int(cfg.get("loop_seconds", 300)), step=60)
        source = st.text_input("Exchange (data source)", value=cfg.get("exchange", {}).get("id", "binance"))

        st.subheader("Active Strategy")
        available = list_strategies()
        strategy_names = list(available.keys())
        current_strategy = cfg.get("strategy_name", "trend_pullback")
        strategy_name = st.selectbox(
            "Strategy",
            strategy_names,
            index=strategy_names.index(current_strategy) if current_strategy in strategy_names else 0,
            format_func=lambda n: f"{available[n]['display_name']} ({n})",
        )
        st.caption(available[strategy_name]["description"] if strategy_name in available else "")

        st.subheader("Symbols")
        symbols_text = st.text_area("Symbols (one per line)", value="\n".join(cfg.get("symbols", [])))

    with tab2:
        st.subheader("Strategy Descriptions")
        for name, info in available.items():
            with st.expander(f"{info['display_name']} ({name})"):
                st.write(info["description"])

    with tab3:
        st.subheader("Strategy Parameters")
        s = cfg.get("strategy", {})
        strategy_name_sel = cfg.get("strategy_name", "trend_pullback")

        if strategy_name_sel == "trend_pullback":
            fast_ema = st.number_input("Fast EMA", value=int(s.get("fast_ema", 12)), step=1, key="sp_fast_ema")
            slow_ema = st.number_input("Slow EMA", value=int(s.get("slow_ema", 26)), step=1, key="sp_slow_ema")
            rsi_period = st.number_input("RSI Period", value=int(s.get("rsi_period", 14)), step=1, key="sp_rsi_period")
            rsi_max = st.number_input("RSI Max Entry", value=float(s.get("rsi_max_entry", 70)), step=1.0, key="sp_rsi_max")
            rsi_min = st.number_input("RSI Min Entry (shorts)", value=float(s.get("rsi_min_entry", 30)), step=1.0, key="sp_rsi_min")
            atr_period = st.number_input("ATR Period", value=int(s.get("atr_period", 14)), step=1, key="sp_atr_period")
            atr_stop = st.number_input("ATR Stop Mult", value=float(s.get("atr_stop_mult", 2.0)), step=0.1, key="sp_atr_stop")
            atr_target = st.number_input("ATR Target Mult", value=float(s.get("atr_target_mult", 3.0)), step=0.1, key="sp_atr_target")
        elif strategy_name_sel == "rsi_mean_reversion":
            rsi_period = st.number_input("RSI Period", value=int(s.get("rsi_period", 14)), step=1, key="sp_rsi_period")
            rsi_buy = st.number_input("RSI Buy (Oversold)", value=float(s.get("rsi_buy", 30)), step=1.0, key="sp_rsi_buy")
            rsi_sell = st.number_input("RSI Sell (Overbought)", value=float(s.get("rsi_sell", 70)), step=1.0, key="sp_rsi_sell")
            atr_period = st.number_input("ATR Period", value=int(s.get("atr_period", 14)), step=1, key="sp_atr_period")
            atr_stop = st.number_input("ATR Stop Mult", value=float(s.get("atr_stop_mult", 2.0)), step=0.1, key="sp_atr_stop")
            atr_target = st.number_input("ATR Target Mult", value=float(s.get("atr_target_mult", 3.0)), step=0.1, key="sp_atr_target")
        elif strategy_name_sel == "bollinger_bands":
            bb_period = st.number_input("BB Period", value=int(s.get("bb_period", 20)), step=1, key="sp_bb_period")
            bb_std = st.number_input("BB Std Dev", value=float(s.get("bb_std", 2.0)), step=0.1, key="sp_bb_std")
            atr_period = st.number_input("ATR Period", value=int(s.get("atr_period", 14)), step=1, key="sp_atr_period")
            atr_stop = st.number_input("ATR Stop Mult", value=float(s.get("atr_stop_mult", 2.0)), step=0.1, key="sp_atr_stop")
            atr_target = st.number_input("ATR Target Mult", value=float(s.get("atr_target_mult", 3.0)), step=0.1, key="sp_atr_target")
        elif strategy_name_sel == "macd_crossover":
            fast_ema = st.number_input("Fast EMA", value=int(s.get("fast_ema", 12)), step=1, key="sp_fast_ema")
            slow_ema = st.number_input("Slow EMA", value=int(s.get("slow_ema", 26)), step=1, key="sp_slow_ema")
            macd_signal = st.number_input("MACD Signal Period", value=int(s.get("macd_signal", 9)), step=1, key="sp_macd_signal")
            atr_period = st.number_input("ATR Period", value=int(s.get("atr_period", 14)), step=1, key="sp_atr_period")
            atr_stop = st.number_input("ATR Stop Mult", value=float(s.get("atr_stop_mult", 2.0)), step=0.1, key="sp_atr_stop")
            atr_target = st.number_input("ATR Target Mult", value=float(s.get("atr_target_mult", 3.0)), step=0.1, key="sp_atr_target")

    with tab4:
        st.subheader("Risk Parameters")
        r = cfg.get("risk", {})
        max_pos = st.number_input("Max Position % of Equity", value=float(r.get("max_position_pct_of_equity", 0.10)), step=0.01, format="%.2f")
        risk_pct = st.number_input("Risk Per Trade %", value=float(r.get("risk_per_trade_pct", 1.0)), step=0.1)
        max_positions = st.number_input("Max Open Positions", value=int(r.get("max_open_positions", 3)), step=1)
        daily_loss = st.number_input("Daily Loss Limit %", value=float(r.get("daily_loss_limit_pct", 4.0)), step=0.5)
        dd_halt = st.number_input("Drawdown Halt %", value=float(r.get("total_drawdown_halt_pct", 15.0)), step=0.5)
        max_losses = st.number_input("Max Consecutive Losses", value=int(r.get("max_consecutive_losses", 4)), step=1)
        min_order = st.number_input("Min Order Quote", value=float(r.get("min_order_quote", 10.0)), step=1.0)
        max_leverage = st.number_input("Max Leverage", value=float(r.get("max_leverage", 5.0)), step=1.0)

    st.divider()

    if st.button("💒 Save Configuration", type="primary", use_container_width=True):
        new_cfg = dict(cfg)
        new_cfg["mode"] = mode
        new_cfg["base_currency"] = base_currency
        new_cfg["starting_balance"] = starting_balance
        new_cfg["loop_seconds"] = loop_seconds
        new_cfg["strategy_name"] = strategy_name
        new_cfg.setdefault("exchange", {})["id"] = source
        new_cfg["symbols"] = [s.strip() for s in symbols_text.split("\n") if s.strip()]

        # Build strategy params based on selected strategy
        strat_params = {}
        if strategy_name_sel == "trend_pullback":
            strat_params = {
                "fast_ema": fast_ema,
                "slow_ema": slow_ema,
                "rsi_period": rsi_period,
                "rsi_max_entry": rsi_max,
                "rsi_min_entry": rsi_min,
                "atr_period": atr_period,
                "atr_stop_mult": atr_stop,
                "atr_target_mult": atr_target,
            }
        elif strategy_name_sel == "rsi_mean_reversion":
            strat_params = {
                "rsi_period": rsi_period,
                "rsi_buy": rsi_buy,
                "rsi_sell": rsi_sell,
                "atr_period": atr_period,
                "atr_stop_mult": atr_stop,
                "atr_target_mult": atr_target,
            }
        elif strategy_name_sel == "bollinger_bands":
            strat_params = {
                "bb_period": bb_period,
                "bb_std": bb_std,
                "atr_period": atr_period,
                "atr_stop_mult": atr_stop,
                "atr_target_mult": atr_target,
            }
        elif strategy_name_sel == "macd_crossover":
            strat_params = {
                "fast_ema": fast_ema,
                "slow_ema": slow_ema,
                "macd_signal": macd_signal,
                "atr_period": atr_period,
                "atr_stop_mult": atr_stop,
                "atr_target_mult": atr_target,
            }
        new_cfg["strategy"] = strat_params

        new_cfg["risk"] = {
            "max_position_pct_of_equity": max_pos,
            "risk_per_trade_pct": risk_pct,
            "max_open_positions": max_positions,
            "daily_loss_limit_pct": daily_loss,
            "total_drawdown_halt_pct": dd_halt,
            "max_consecutive_losses": max_losses,
            "min_order_quote": min_order,
            "require_stop_loss": True,
            "max_leverage": max_leverage,
            "liquidation_buffer_pct": 2.0,
        }
        try:
            save_config(new_cfg)
            st.session_state.cfg = new_cfg
            st.session_state.agent = agent_for_config(new_cfg)
            st.success("Configuration saved. Reload the app to apply all changes.")
        except Exception as e:
            st.error(f"Save failed: {e}")

# --------------------------------------------------------------------------- journal

elif page == "Journal":
    st.title("Trade Journal")

    df = load_trades()
    if df is not None and not df.empty:
        st.subheader("Trade History")

        # Summary metrics
        total_pnl = df["pnl"].sum() if "pnl" in df.columns else 0
        win_count = (df["pnl"] > 0).sum() if "pnl" in df.columns else 0
        loss_count = (df["pnl"] <= 0).sum() if "pnl" in df.columns else 0
        win_rate = win_count / len(df) * 100 if len(df) else 0

        j1, j2, j3, j4 = st.columns(4)
        with j1:
            st.metric("Total Trades", len(df))
        with j2:
            st.metric("Total PnL", f"{total_pnl:+.2f}")
        with j3:
            st.metric("Wins", win_count)
        with j4:
            st.metric("Win Rate", f"{win_rate:.1f}%")

        st.divider()

        # Filter
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            symbol_filter = st.selectbox("Filter by Symbol", ["All"] + sorted(df["symbol"].unique().tolist()) if "symbol" in df.columns else ["All"])
        with col_f2:
            side_filter = st.selectbox("Filter by Side", ["All"] + sorted(df["side"].unique().tolist()) if "side" in df.columns else ["All"])

        filtered = df.copy()
        if symbol_filter != "All" and "symbol" in filtered.columns:
            filtered = filtered[filtered["symbol"] == symbol_filter]
        if side_filter != "All" and "side" in filtered.columns:
            filtered = filtered[filtered["side"] == side_filter]

        st.dataframe(filtered, use_container_width=True, hide_index=True)

        st.download_button(
            "📥 Download CSV",
            data=filtered.to_csv(index=False),
            file_name="trades_export.csv",
            mime="text/csv",
        )
    else:
        st.info("No trades yet. Run some cycles to generate trade history.")

# --------------------------------------------------------------------------- footer

st.divider()
st.caption(
    "ph-crypto-agent — Paper-first crypto trading agent. "
    "Not financial advice. Most retail bots lose money. Start on paper."
)
