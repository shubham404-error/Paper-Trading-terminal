"""Pure computational engine for F&O Cash-and-Carry Arbitrage.

This module contains zero network calls. It computes mispricing, fair value, 
total execution costs (statutory + broker-specific), and net annualized returns 
for spot-vs-future arbitrage opportunities.
"""

from dataclasses import dataclass
from typing import List, Optional

# --- Statutory Tax & Exchange Rates (Budget 2026) ---
# Universal across all brokers
GST_RATE = 0.18
SEBI_TURNOVER_FEE = 0.000001  # ₹10 per crore

# Equity Delivery (Spot Leg)
CASH_STT_BUY = 0.001       # 0.1%
CASH_STT_SELL = 0.001      # 0.1%
CASH_EXC_TXN = 0.0000297   # NSE cash exchange charge
CASH_STAMP_BUY = 0.00015   # 0.015%

# Equity Futures (Futures Leg)
FUT_STT_SELL = 0.0005      # 0.05% (Budget 2026)
FUT_STT_BUY = 0.0          # 0%
FUT_EXC_TXN = 0.0000183    # NSE futures exchange charge
FUT_STAMP_BUY = 0.00002    # 0.002%


@dataclass
class BrokerProfile:
    name: str
    delivery_brokerage: float  # e.g., 0 for Zerodha, 20 for Upstox/Groww
    futures_brokerage: float   # typically 20 for all discount brokers
    dp_charge: float           # Depository participant charge for selling delivery stock


# Pre-configured profiles for the highest active users brokers
BROKERS = {
    "Zerodha": BrokerProfile("Zerodha", delivery_brokerage=0.0, futures_brokerage=20.0, dp_charge=15.34),
    "Groww": BrokerProfile("Groww", delivery_brokerage=20.0, futures_brokerage=20.0, dp_charge=15.93),
    "Angel One": BrokerProfile("Angel One", delivery_brokerage=0.0, futures_brokerage=20.0, dp_charge=20.0),
    "Upstox": BrokerProfile("Upstox", delivery_brokerage=20.0, futures_brokerage=20.0, dp_charge=18.50),
}


@dataclass
class ArbitrageOpportunity:
    symbol: str
    spot_price: float
    future_price: float
    expiry_date: str
    days_to_expiry: int
    lot_size: int
    
    # Engine computed fields
    fair_value: float = 0.0
    raw_premium: float = 0.0
    mispricing: float = 0.0
    
    # Cost & Return fields
    total_cost_absolute: float = 0.0
    capital_deployed: float = 0.0
    net_return_absolute: float = 0.0
    net_annualized_return: float = 0.0
    capital_efficiency: float = 0.0
    
    # Classification
    classification: str = "UNCLASSIFIED"


def _calculate_total_costs(
    spot_price: float, 
    future_price: float, 
    lot_size: int, 
    broker: BrokerProfile, 
    slippage_bps: float
) -> float:
    """Calculates fully-loaded round-trip costs for cash-and-carry arb."""
    spot_notional = spot_price * lot_size
    fut_notional = future_price * lot_size

    # 1. Slippage (Notional loss due to bid-ask spread across both legs)
    slippage_cost = (spot_notional + fut_notional) * (slippage_bps / 10000)

    # 2. Spot Leg Costs (Buy now, Sell at expiry)
    # Buy Costs
    cash_brokerage_buy = min(broker.delivery_brokerage, spot_notional * 0.025)
    cash_exc_buy = spot_notional * CASH_EXC_TXN
    cash_stt_buy = spot_notional * CASH_STT_BUY
    cash_stamp_buy = spot_notional * CASH_STAMP_BUY
    cash_sebi_buy = spot_notional * SEBI_TURNOVER_FEE
    cash_gst_buy = (cash_brokerage_buy + cash_exc_buy) * GST_RATE
    
    # Sell Costs (at convergence, assuming spot price ~= future price)
    cash_brokerage_sell = min(broker.delivery_brokerage, fut_notional * 0.025)
    cash_exc_sell = fut_notional * CASH_EXC_TXN
    cash_stt_sell = fut_notional * CASH_STT_SELL
    cash_sebi_sell = fut_notional * SEBI_TURNOVER_FEE
    cash_gst_sell = (cash_brokerage_sell + cash_exc_sell + broker.dp_charge) * GST_RATE
    
    spot_total_cost = (
        cash_brokerage_buy + cash_exc_buy + cash_stt_buy + cash_stamp_buy + cash_sebi_buy + cash_gst_buy +
        cash_brokerage_sell + cash_exc_sell + cash_stt_sell + cash_sebi_sell + broker.dp_charge + cash_gst_sell
    )

    # 3. Futures Leg Costs (Sell now, Buy to close at expiry)
    # Sell Costs
    fut_brokerage_sell = min(broker.futures_brokerage, fut_notional * 0.025)
    fut_exc_sell = fut_notional * FUT_EXC_TXN
    fut_stt_sell = fut_notional * FUT_STT_SELL
    fut_sebi_sell = fut_notional * SEBI_TURNOVER_FEE
    fut_gst_sell = (fut_brokerage_sell + fut_exc_sell) * GST_RATE
    
    # Buy Costs (closing the short future)
    fut_brokerage_buy = min(broker.futures_brokerage, fut_notional * 0.025)
    fut_exc_buy = fut_notional * FUT_EXC_TXN
    fut_stamp_buy = fut_notional * FUT_STAMP_BUY
    fut_sebi_buy = fut_notional * SEBI_TURNOVER_FEE
    fut_gst_buy = (fut_brokerage_buy + fut_exc_buy) * GST_RATE

    fut_total_cost = (
        fut_brokerage_sell + fut_exc_sell + fut_stt_sell + fut_sebi_sell + fut_gst_sell +
        fut_brokerage_buy + fut_exc_buy + fut_stamp_buy + fut_sebi_buy + fut_gst_buy
    )

    return slippage_cost + spot_total_cost + fut_total_cost


def compute(
    symbol: str,
    spot_price: float,
    future_price: float,
    expiry_date: str,
    days_to_expiry: int,
    lot_size: int,
    broker_name: str = "Zerodha",
    funding_rate_annual: float = 0.0,
    expected_dividends: float = 0.0,
    slippage_bps: float = 5.0,
    is_liquid: bool = True,
    borrow_available: bool = False
) -> ArbitrageOpportunity:
    """Core pure function to evaluate an arbitrage opportunity."""
    
    op = ArbitrageOpportunity(
        symbol=symbol,
        spot_price=spot_price,
        future_price=future_price,
        expiry_date=expiry_date,
        days_to_expiry=days_to_expiry,
        lot_size=lot_size
    )

    # Boundary conditions
    if days_to_expiry <= 0 or not is_liquid:
        op.classification = "AVOID" if not is_liquid else "NO EDGE"
        return op

    years_to_expiry = days_to_expiry / 365.0
    
    # TDD Equations
    op.raw_premium = future_price - spot_price
    op.fair_value = (spot_price * (1 + (funding_rate_annual * years_to_expiry))) - expected_dividends
    op.mispricing = future_price - op.fair_value

    # Cost & Returns
    broker = BROKERS.get(broker_name, BROKERS["Zerodha"])
    op.total_cost_absolute = _calculate_total_costs(spot_price, future_price, lot_size, broker, slippage_bps)
    
    gross_profit = op.raw_premium * lot_size
    op.net_return_absolute = gross_profit - op.total_cost_absolute
    
    # Capital required: 100% cash for spot delivery + assumed 20% margin for short future
    fut_margin_req = future_price * lot_size * 0.20
    op.capital_deployed = (spot_price * lot_size) + fut_margin_req
    
    # Return metrics
    net_return_pct = op.net_return_absolute / op.capital_deployed
    op.net_annualized_return = net_return_pct * (365.0 / days_to_expiry)
    op.capital_efficiency = op.net_annualized_return / op.capital_deployed

    # Taxonomy Classification
    if op.raw_premium < 0:
        if borrow_available:
            op.classification = "REVERSE ARB"
        else:
            op.classification = "AVOID"
    elif op.net_return_absolute <= 0:
        op.classification = "NO EDGE"
    elif op.net_annualized_return >= 0.12 and op.mispricing > 0:
        op.classification = "TRUE ARBITRAGE"
    elif op.net_annualized_return >= 0.06:
        op.classification = "STRONG CARRY"
    elif (gross_profit > (op.total_cost_absolute / 2)):
        op.classification = "WATCH"
    else:
        op.classification = "NO EDGE"

    return op


def rank_opportunities(ops: List[ArbitrageOpportunity]) -> List[ArbitrageOpportunity]:
    """Filters out noise and ranks the remainder by capital efficiency."""
    filtered = [op for op in ops if op.classification not in ("AVOID", "NO EDGE")]
    return sorted(filtered, key=lambda x: x.capital_efficiency, reverse=True)
