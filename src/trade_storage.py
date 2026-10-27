"""
QUANTA-SYNTH Institutional Trade Storage Engine
===============================================
Hybrid MongoDB + Local JSON persistence layer for 24/7 autonomous surveillance.
Connects natively to Dokploy internal MongoDB service with automatic fallback to local disk.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("synthetic-trade-storage")

DEFAULT_MONGO_URI = (
    "mongodb://mongo:tpmghdyeiklrfz4f@trader-trader-chom2a:27017/?authSource=admin&directConnection=true"
)
DEFAULT_DB_NAME = "quanta_synth"


class TradeStorageManager:
    """
    Manages lifecycle of open and closed trades across MongoDB and local state files.
    Ensures zero data loss across container rebuilds, hot-reloads, and dashboard refreshes.
    """

    def __init__(
        self,
        mongo_uri: Optional[str] = None,
        db_name: str = DEFAULT_DB_NAME,
        state_file: Optional[Path] = None,
        timeout_ms: int = 1500,
    ):
        self.state_file = state_file or (Path(__file__).parent.parent / "data" / "synthetic_state.json")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        self.mongo_uri = mongo_uri or os.getenv("MONGODB_URI", DEFAULT_MONGO_URI)
        self.db_name = os.getenv("MONGODB_DATABASE", db_name)
        self.timeout_ms = timeout_ms

        self.client = None
        self.db = None
        self.trades_col = None
        self.state_col = None
        self.is_connected = False
        self.connection_error: Optional[str] = None

        self._init_connection()
        self._sync_seed_data_if_needed()

    def _init_connection(self) -> None:
        """Attempt connection to MongoDB with fast timeout."""
        try:
            import pymongo

            # Connect with short timeout so local execution doesn't hang
            self.client = pymongo.MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=self.timeout_ms,
                connectTimeoutMS=self.timeout_ms,
            )
            # Ping database to verify connection
            self.client.admin.command("ping")
            self.db = self.client[self.db_name]
            self.trades_col = self.db["trades"]
            self.state_col = self.db["portfolio_state"]

            # Create ticket index
            self.trades_col.create_index("ticket", unique=True)
            self.trades_col.create_index("status")
            self.trades_col.create_index("opened_at")

            self.is_connected = True
            logger.info("🍃 Connected to MongoDB @ %s (DB: %s)", self._masked_uri(), self.db_name)
        except Exception as exc:
            self.is_connected = False
            self.connection_error = str(exc)
            logger.warning(
                "⚠️ MongoDB connection not available (%s). Using local JSON cache (%s).",
                type(exc).__name__,
                self.state_file,
            )

    def _masked_uri(self) -> str:
        """Return URI with password masked for safe logging."""
        if "@" in self.mongo_uri:
            prefix, host = self.mongo_uri.split("@", 1)
            return f"mongodb://***:***@{host}"
        return self.mongo_uri

    def _sync_seed_data_if_needed(self) -> None:
        """Seed MongoDB from state_file if Mongo is connected but empty, or vice versa."""
        if self.is_connected and self.trades_col is not None:
            try:
                count = self.trades_col.count_documents({})
                if count == 0 and self.state_file.exists():
                    data = json.loads(self.state_file.read_text(encoding="utf-8"))
                    # Seed open trades
                    for tkt, t in data.get("open_trades", {}).items():
                        doc = {**t, "ticket": str(tkt), "status": "OPEN", "updated_at": datetime.now(timezone.utc).isoformat()}
                        self.trades_col.update_one({"ticket": str(tkt)}, {"$set": doc}, upsert=True)
                    # Seed closed trades
                    for t in data.get("closed_trades", []):
                        tkt = str(t.get("ticket", f"C-{abs(hash(t.get('opened_at', '')) % 100000)}"))
                        doc = {**t, "ticket": tkt, "status": "CLOSED", "updated_at": datetime.now(timezone.utc).isoformat()}
                        self.trades_col.update_one({"ticket": tkt}, {"$set": doc}, upsert=True)
                    # Seed portfolio state
                    self.state_col.update_one(
                        {"_id": "portfolio_state"},
                        {
                            "$set": {
                                "equity": float(data.get("equity", 10000.0)),
                                "balance": float(data.get("balance", 10000.0)),
                                "mode": data.get("mode", "CLOUD_AUTONOMOUS_LIVE_WEB"),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                        },
                        upsert=True,
                    )
                    logger.info("🍃 Seeded initial state file records into MongoDB.")
            except Exception as e:
                logger.warning("Error syncing seed data to MongoDB: %s", e)

    def _load_disk_state(self) -> Dict[str, Any]:
        """Read state from local JSON file."""
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "open_trades": {},
            "closed_trades": [],
            "equity": 10000.0,
            "balance": 10000.0,
            "mode": "CLOUD_AUTONOMOUS_LIVE_WEB",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_disk_state(self, state: Dict[str, Any]) -> None:
        """Write state to local JSON file safely."""
        try:
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to write local state file: %s", exc)

    # ── High-Level Storage API ──────────────────────────────────────────────────

    def get_all_state(self) -> Dict[str, Any]:
        """Fetch unified state dictionary containing open_trades, closed_trades, equity, balance."""
        if self.is_connected and self.trades_col is not None and self.state_col is not None:
            try:
                # Query Mongo
                open_docs = list(self.trades_col.find({"status": "OPEN"}, {"_id": 0}))
                open_trades = {str(d["ticket"]): d for d in open_docs if "ticket" in d}

                closed_docs = list(
                    self.trades_col.find({"status": "CLOSED"}, {"_id": 0}).sort("closed_at", -1).limit(200)
                )

                account_doc = self.state_col.find_one({"_id": "portfolio_state"}) or {}
                equity = float(account_doc.get("equity", 10000.0))
                balance = float(account_doc.get("balance", 10000.0))
                mode = str(account_doc.get("mode", "CLOUD_AUTONOMOUS_LIVE_WEB"))

                state = {
                    "open_trades": open_trades,
                    "closed_trades": closed_docs,
                    "equity": equity,
                    "balance": balance,
                    "mode": mode,
                    "updated_at": account_doc.get("updated_at", datetime.now(timezone.utc).isoformat()),
                }
                # Sync mirror to disk
                self._save_disk_state(state)
                return state
            except Exception as exc:
                logger.warning("MongoDB read failed (%s), falling back to disk", exc)

        return self._load_disk_state()

    def save_open_trade(self, ticket: str, trade_dict: Dict[str, Any]) -> None:
        """Save or update an active open trade."""
        now_str = datetime.now(timezone.utc).isoformat()
        doc = {**trade_dict, "ticket": str(ticket), "status": "OPEN", "updated_at": now_str}

        # 1. Update MongoDB if connected
        if self.is_connected and self.trades_col is not None:
            try:
                self.trades_col.update_one({"ticket": str(ticket)}, {"$set": doc}, upsert=True)
            except Exception as e:
                logger.error("Failed to upsert trade %s into MongoDB: %s", ticket, e)

        # 2. Update local state
        state = self._load_disk_state()
        state.setdefault("open_trades", {})[str(ticket)] = doc
        self._save_disk_state(state)

    def close_trade(
        self,
        ticket: str,
        exit_price: float,
        pnl_usd: float,
        pnl_r: float,
        close_reason: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a trade as closed, calculate outcome, update account equity and balance.
        Returns the finalized closed trade document.
        """
        now_str = datetime.now(timezone.utc).isoformat()

        # Determine explicit Outcome
        if pnl_usd > 0 or "PROFIT" in close_reason.upper() or "TP" in close_reason.upper():
            outcome = "WIN"
        elif pnl_usd < 0 or "STOP" in close_reason.upper() or "LOSS" in close_reason.upper():
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        # 1. Read existing trade
        existing_trade = None
        state = self._load_disk_state()

        if str(ticket) in state.get("open_trades", {}):
            existing_trade = state["open_trades"].pop(str(ticket))
        elif self.is_connected and self.trades_col is not None:
            existing_trade = self.trades_col.find_one({"ticket": str(ticket)}, {"_id": 0})

        if not existing_trade:
            existing_trade = {"ticket": str(ticket)}

        closed_doc = {
            **existing_trade,
            "ticket": str(ticket),
            "status": "CLOSED",
            "exit_price": round(exit_price, 5),
            "pnl_usd": round(pnl_usd, 2),
            "pnl_r": round(pnl_r, 2),
            "outcome": outcome,
            "close_reason": close_reason,
            "closed_at": now_str,
            "updated_at": now_str,
        }

        # Update balance & equity
        new_equity = round(float(state.get("equity", 10000.0)) + pnl_usd, 2)
        new_balance = round(float(state.get("balance", 10000.0)) + pnl_usd, 2)
        state["equity"] = new_equity
        state["balance"] = new_balance

        # Append to closed trades on disk
        state.setdefault("closed_trades", []).append(closed_doc)
        if len(state["closed_trades"]) > 300:
            state["closed_trades"] = state["closed_trades"][-300:]
        self._save_disk_state(state)

        # 2. Update MongoDB
        if self.is_connected and self.trades_col is not None and self.state_col is not None:
            try:
                self.trades_col.update_one({"ticket": str(ticket)}, {"$set": closed_doc}, upsert=True)
                self.state_col.update_one(
                    {"_id": "portfolio_state"},
                    {
                        "$set": {
                            "equity": new_equity,
                            "balance": new_balance,
                            "updated_at": now_str,
                        }
                    },
                    upsert=True,
                )
            except Exception as e:
                logger.error("Failed to close trade %s in MongoDB: %s", ticket, e)

        return closed_doc

    def update_portfolio_state(
        self,
        equity: float,
        balance: float,
        mode: str = "CLOUD_AUTONOMOUS_LIVE_WEB",
    ) -> None:
        """Update account equity, balance, and operational mode."""
        now_str = datetime.now(timezone.utc).isoformat()
        state = self._load_disk_state()
        state["equity"] = round(equity, 2)
        state["balance"] = round(balance, 2)
        state["mode"] = mode
        self._save_disk_state(state)

        if self.is_connected and self.state_col is not None:
            try:
                self.state_col.update_one(
                    {"_id": "portfolio_state"},
                    {
                        "$set": {
                            "equity": round(equity, 2),
                            "balance": round(balance, 2),
                            "mode": mode,
                            "updated_at": now_str,
                        }
                    },
                    upsert=True,
                )
            except Exception as e:
                logger.error("Failed to update portfolio state in MongoDB: %s", e)

    def reset_history(self, starting_equity: float = 10000.0, starting_balance: float = 10000.0) -> None:
        """Clear closed trades and restore clean equity/balance."""
        now_str = datetime.now(timezone.utc).isoformat()

        # Update disk
        state = self._load_disk_state()
        state["closed_trades"] = []
        state["equity"] = round(starting_equity, 2)
        state["balance"] = round(starting_balance, 2)
        self._save_disk_state(state)

        # Update MongoDB
        if self.is_connected and self.trades_col is not None and self.state_col is not None:
            try:
                self.trades_col.delete_many({"status": "CLOSED"})
                self.state_col.update_one(
                    {"_id": "portfolio_state"},
                    {
                        "$set": {
                            "equity": round(starting_equity, 2),
                            "balance": round(starting_balance, 2),
                            "updated_at": now_str,
                        }
                    },
                    upsert=True,
                )
                logger.info("🧹 History reset completed in MongoDB.")
            except Exception as e:
                logger.error("Failed to reset history in MongoDB: %s", e)

    def get_status_summary(self) -> Dict[str, Any]:
        """Diagnostics summary for dashboard display."""
        total_open = 0
        total_closed = 0
        if self.is_connected and self.trades_col is not None:
            try:
                total_open = self.trades_col.count_documents({"status": "OPEN"})
                total_closed = self.trades_col.count_documents({"status": "CLOSED"})
            except Exception:
                pass
        else:
            state = self._load_disk_state()
            total_open = len(state.get("open_trades", {}))
            total_closed = len(state.get("closed_trades", []))

        return {
            "is_connected": self.is_connected,
            "engine": "MongoDB" if self.is_connected else "Local File Cache",
            "host": "trader-trader-chom2a:27017" if self.is_connected else "Local Disk",
            "database": self.db_name if self.is_connected else "synthetic_state.json",
            "open_trades_count": total_open,
            "closed_trades_count": total_closed,
            "error": self.connection_error,
        }
