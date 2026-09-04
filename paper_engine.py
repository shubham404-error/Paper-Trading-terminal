"""Small, persistent paper-trading engine for the CapitalSense Streamlit app.

The engine mirrors the MAET concepts: accounts, orders, fills, positions,
market/limit/stop orders, bracket exits, margin, and a resettable paper ledger.
It deliberately never routes orders to a broker.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INITIAL_CASH = 1_000_000.0
MARGIN_MULTIPLIER = 5.0

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperEngine:
    def __init__(self, db_path: str | Path, initial_cash: float = INITIAL_CASH) -> None:
        """Initialize the PaperEngine with a database path and initial cash balance."""
        self.db_path = str(db_path)
        self.initial_cash = float(initial_cash)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_cash REAL NOT NULL,
                    realised_pnl REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    order_type TEXT NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT', 'STOP_LOSS_LIMIT')),
                    quantity INTEGER NOT NULL,
                    limit_price REAL,
                    stop_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    status TEXT NOT NULL,
                    reject_reason TEXT,
                    exit_reason TEXT,
                    created_at TEXT NOT NULL,
                    filled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    filled_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    quantity INTEGER NOT NULL,
                    avg_entry REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    trailing_distance REAL,
                    trailing_is_percent INTEGER NOT NULL DEFAULT 0,
                    trail_anchor REAL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders(symbol, status);
                CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
                """
            )
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN exit_reason TEXT")
            except sqlite3.OperationalError:
                pass

            if conn.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
                stamp = now_iso()
                conn.execute(
                    "INSERT INTO account (id, initial_cash, realised_pnl, created_at, updated_at) VALUES (1, ?, 0, ?, ?)",
                    (self.initial_cash, stamp, stamp),
                )

    def reset(self) -> None:
        """Reset the paper trading account to its initial state, clearing all orders and positions."""
        with self._connect() as conn:
            stamp = now_iso()
            conn.execute("DELETE FROM fills")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM positions")
            conn.execute(
                "UPDATE account SET initial_cash = ?, realised_pnl = 0, updated_at = ? WHERE id = 1",
                (self.initial_cash, stamp),
            )
        log.info("Account reset with initial cash: %s", self.initial_cash)

    def _account(self, conn: sqlite3.Connection) -> sqlite3.Row:
        return conn.execute("SELECT * FROM account WHERE id = 1").fetchone()

    @staticmethod
    def _position_pnl(position: sqlite3.Row, price: float) -> float:
        return (float(price) - float(position["avg_entry"])) * int(position["quantity"])

    def _used_margin(self, conn: sqlite3.Connection, quotes: dict[str, float]) -> float:
        total = 0.0
        for position in conn.execute("SELECT * FROM positions").fetchall():
            mark = float(quotes.get(position["symbol"], position["avg_entry"]))
            total += abs(int(position["quantity"]) * mark) / MARGIN_MULTIPLIER
        return total

    def state(self, quotes: dict[str, float] | None = None) -> dict[str, Any]:
        """Return the current state of the paper trading account, including balances, positions, orders, and fills."""
        quotes = quotes or {}
        with self._connect() as conn:
            account = self._account(conn)
            positions = []
            unrealised = 0.0
            for row in conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall():
                mark = float(quotes.get(row["symbol"], row["avg_entry"]))
                pnl = self._position_pnl(row, mark)
                unrealised += pnl
                positions.append(
                    {
                        "Symbol": row["symbol"],
                        "Quantity": int(row["quantity"]),
                        "Avg Entry": float(row["avg_entry"]),
                        "LTP": mark,
                        "Unrealized P&L": pnl,
                        "Stop Loss": row["stop_loss"],
                        "Take Profit": row["take_profit"],
                    }
                )
            realised = float(account["realised_pnl"])
            nav = float(account["initial_cash"]) + realised + unrealised
            used_margin = self._used_margin(conn, quotes)
            open_orders = [dict(row) for row in conn.execute(
                "SELECT * FROM orders WHERE status IN ('PENDING', 'TRIGGER_PENDING') ORDER BY created_at DESC"
            ).fetchall()]
            fills = [dict(row) for row in conn.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT 100"
            ).fetchall()]
            all_orders = [dict(row) for row in conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT 200"
            ).fetchall()]
            return {
                "nav": nav,
                "initial_cash": float(account["initial_cash"]),
                "realised_pnl": realised,
                "unrealised_pnl": unrealised,
                "used_margin": used_margin,
                "free_margin": max(0.0, nav - used_margin),
                "positions": positions,
                "open_orders": open_orders,
                "fills": fills,
                "all_orders": all_orders,
            }

    def _check_margin(self, quantity: int, price: float) -> str | None:
        margin_req = (quantity * price) / MARGIN_MULTIPLIER
        free_margin = self.state().get("free_margin", 0.0)
        if free_margin < margin_req:
            return f"Insufficient margin. Required: {margin_req:.2f}, Available: {free_margin:.2f}"
        return None

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: int,
        current_price: float,
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Place a new paper trading order."""
        symbol = symbol.strip().upper()
        side = side.upper()
        order_type = order_type.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("Side must be BUY or SELL")
        if order_type not in {"MARKET", "LIMIT", "STOP_LOSS_LIMIT"}:
            raise ValueError("Unsupported order type")
        if quantity <= 0:
            raise ValueError("Quantity must be a positive whole number")
        if current_price <= 0:
            raise ValueError("A valid market quote is required")
        if order_type == "LIMIT" and (limit_price is None or limit_price <= 0):
            raise ValueError("Limit price is required")
        if order_type == "STOP_LOSS_LIMIT" and (stop_price is None or limit_price is None):
            raise ValueError("Stop and limit prices are required")

        order_id = str(uuid.uuid4())
        stamp = now_iso()

        margin_error = self._check_margin(quantity, float(current_price))
        if margin_error:
            log.warning("Order %s rejected: %s", order_id, margin_error)
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO orders
                    (id, symbol, side, order_type, quantity, limit_price, stop_price, stop_loss, take_profit, status, reject_reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'REJECTED', ?, ?)""",
                    (order_id, symbol, side, order_type, quantity, limit_price, stop_price, stop_loss, take_profit, margin_error, stamp),
                )
            return {"order_id": order_id, "status": "REJECTED", "reject_reason": margin_error}

        log.info("Order %s placed successfully: %s %s %s", order_id, side, quantity, symbol)

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO orders
                (id, symbol, side, order_type, quantity, limit_price, stop_price, stop_loss, take_profit, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                (order_id, symbol, side, order_type, quantity, limit_price, stop_price, stop_loss, take_profit, stamp),
            )
            if order_type == "MARKET":
                self._fill_order(conn, order_id, float(current_price), reduce_only=False)
            elif order_type == "LIMIT" and self._limit_is_marketable(side, float(current_price), float(limit_price)):
                self._fill_order(conn, order_id, float(current_price), reduce_only=False)
            elif order_type == "STOP_LOSS_LIMIT":
                if self._stop_triggered(side, float(current_price), float(stop_price)) and self._limit_is_marketable(side, float(current_price), float(limit_price)):
                    self._fill_order(conn, order_id, float(current_price), reduce_only=False)
                else:
                    conn.execute("UPDATE orders SET status = 'TRIGGER_PENDING' WHERE id = ?", (order_id,))
        return {"order_id": order_id, "status": self.order_status(order_id)}

    @staticmethod
    def _limit_is_marketable(side: str, price: float, limit_price: float) -> bool:
        return price <= limit_price if side == "BUY" else price >= limit_price

    @staticmethod
    def _stop_triggered(side: str, price: float, stop_price: float) -> bool:
        return price >= stop_price if side == "BUY" else price <= stop_price

    def order_status(self, order_id: str) -> str:
        """Retrieve the status of a specific order by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
            return row["status"] if row else "UNKNOWN"

    def cancel_order(self, order_id: str) -> None:
        """Cancel a pending or trigger-pending order."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET status = 'CANCELLED' WHERE id = ? AND status IN ('PENDING', 'TRIGGER_PENDING')",
                (order_id,),
            )

    def process_quote(self, symbol: str, price: float) -> list[str]:
        """Mark pending orders and bracket exits against a verified quote."""
        symbol = symbol.strip().upper()
        price = float(price)
        filled: list[str] = []
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT * FROM orders WHERE symbol = ? AND status IN ('PENDING', 'TRIGGER_PENDING') ORDER BY created_at",
                (symbol,),
            ).fetchall()
            for order in pending:
                eligible = False
                if order["order_type"] == "LIMIT":
                    eligible = self._limit_is_marketable(order["side"], price, float(order["limit_price"]))
                elif order["order_type"] == "STOP_LOSS_LIMIT":
                    eligible = self._stop_triggered(order["side"], price, float(order["stop_price"])) and self._limit_is_marketable(order["side"], price, float(order["limit_price"]))
                if eligible:
                    self._fill_order(conn, order["id"], price, reduce_only=False)
                    filled.append(order["id"])

            position = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
            if position:
                anchor = float(position["trail_anchor"] or price)
                if int(position["quantity"]) > 0:
                    anchor = max(anchor, price)
                else:
                    anchor = min(anchor, price)
                conn.execute("UPDATE positions SET trail_anchor = ?, updated_at = ? WHERE symbol = ?", (anchor, now_iso(), symbol))
                exit_reason = self._bracket_reason(position, price, anchor)
                if exit_reason:
                    side = "SELL" if int(position["quantity"]) > 0 else "BUY"
                    qty = abs(int(position["quantity"]))
                    order_id = str(uuid.uuid4())
                    stamp = now_iso()
                    log.info("Bracket exit triggered for %s: %s", symbol, exit_reason)
                    conn.execute(
                        "INSERT INTO orders (id, symbol, side, order_type, quantity, status, exit_reason, created_at) VALUES (?, ?, ?, 'MARKET', ?, 'PENDING', ?, ?)",
                        (order_id, symbol, side, qty, exit_reason, stamp),
                    )
                    self._fill_order(conn, order_id, price, reduce_only=True)
                    filled.append(order_id)
        return filled

    @staticmethod
    def _bracket_reason(position: sqlite3.Row, price: float, anchor: float) -> str | None:
        qty = int(position["quantity"])
        stop = position["stop_loss"]
        take = position["take_profit"]
        trailing = position["trailing_distance"]
        if qty > 0:
            if stop is not None and price <= float(stop):
                return "Stop loss triggered"
            if take is not None and price >= float(take):
                return "Take profit triggered"
            if trailing is not None:
                trail = anchor - (anchor * float(trailing) / 100 if position["trailing_is_percent"] else float(trailing))
                if price <= trail:
                    return "Trailing stop triggered"
        else:
            if stop is not None and price >= float(stop):
                return "Stop loss triggered"
            if take is not None and price <= float(take):
                return "Take profit triggered"
            if trailing is not None:
                trail = anchor + (anchor * float(trailing) / 100 if position["trailing_is_percent"] else float(trailing))
                if price >= trail:
                    return "Trailing stop triggered"
        return None

    def _fill_order(self, conn: sqlite3.Connection, order_id: str, price: float, reduce_only: bool) -> None:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None or order["status"] not in {"PENDING", "TRIGGER_PENDING"}:
            return

        side_sign = 1 if order["side"] == "BUY" else -1
        delta = side_sign * int(order["quantity"])
        position = conn.execute("SELECT * FROM positions WHERE symbol = ?", (order["symbol"],)).fetchone()
        old_qty = int(position["quantity"]) if position else 0
        old_avg = float(position["avg_entry"]) if position else 0.0
        if reduce_only and old_qty == 0:
            return
        if reduce_only and old_qty * delta > 0:
            return

        if old_qty == 0 or old_qty * delta > 0:
            new_qty = old_qty + delta
            new_avg = price if old_qty == 0 else ((abs(old_qty) * old_avg) + (abs(delta) * price)) / abs(new_qty)
            realised_delta = 0.0
        else:
            close_qty = min(abs(old_qty), abs(delta))
            realised_delta = close_qty * (price - old_avg) * (1 if old_qty > 0 else -1)
            new_qty = old_qty + delta
            new_avg = old_avg if new_qty != 0 and (old_qty * new_qty > 0) else price

        conn.execute(
            "UPDATE account SET realised_pnl = realised_pnl + ?, updated_at = ? WHERE id = 1",
            (realised_delta, now_iso()),
        )
        fill_id = str(uuid.uuid4())
        stamp = now_iso()
        conn.execute(
            "INSERT INTO fills (id, order_id, symbol, side, quantity, price, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fill_id, order_id, order["symbol"], order["side"], int(order["quantity"]), price, stamp),
        )
        conn.execute("UPDATE orders SET status = 'FILLED', filled_at = ? WHERE id = ?", (stamp, order_id))
        log.info("Order %s filled at %s", order_id, price)

        if new_qty == 0:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (order["symbol"],))
        elif position and old_qty * delta > 0:
            conn.execute(
                """UPDATE positions SET quantity = ?, avg_entry = ?, updated_at = ? WHERE symbol = ?""",
                (new_qty, new_avg, stamp, order["symbol"]),
            )
        else:
            conn.execute("DELETE FROM positions WHERE symbol = ?", (order["symbol"],))
            conn.execute(
                """INSERT INTO positions
                (symbol, quantity, avg_entry, stop_loss, take_profit, trail_anchor, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (order["symbol"], new_qty, new_avg, order["stop_loss"], order["take_profit"], price, stamp),
            )
