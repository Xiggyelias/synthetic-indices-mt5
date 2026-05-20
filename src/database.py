"""
MySQL Database Layer
====================
Centralised MySQL connection, schema setup, and CRUD for all bot data.
Falls back to JSON if MySQL is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("database")

# ── Try to import MySQL connector ────────────────────────────────────────────
try:
    import mysql.connector
    from mysql.connector import pooling
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False
    logger.warning("mysql-connector-python not installed; falling back to JSON storage.")


# ── Schema DDL ───────────────────────────────────────────────────────────────
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    ticket            INT NOT NULL,
    symbol            VARCHAR(20) NOT NULL,
    direction         VARCHAR(10) NOT NULL,
    entry_price       DECIMAL(18,6) NOT NULL DEFAULT 0,
    exit_price        DECIMAL(18,6) NOT NULL DEFAULT 0,
    volume            DECIMAL(10,4) NOT NULL DEFAULT 0,
    pnl               DECIMAL(12,2) NOT NULL DEFAULT 0,
    r_achieved        DECIMAL(8,4)  NOT NULL DEFAULT 0,
    confluence_score  INT           NOT NULL DEFAULT 0,
    session           VARCHAR(30)   DEFAULT 'Unknown',
    opened_at         VARCHAR(60),
    closed_at         VARCHAR(60),
    win               TINYINT(1)    DEFAULT 0,
    commission        DECIMAL(10,2) DEFAULT 0,
    swap              DECIMAL(10,2) DEFAULT 0,
    confluence_signals JSON,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_symbol  (symbol),
    INDEX idx_closed  (closed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS journal_entries (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    ticket               INT NOT NULL,
    symbol               VARCHAR(20) NOT NULL,
    direction            VARCHAR(10) NOT NULL,
    entry_price          DECIMAL(18,6) NOT NULL DEFAULT 0,
    exit_price           DECIMAL(18,6) NOT NULL DEFAULT 0,
    volume               DECIMAL(10,4) NOT NULL DEFAULT 0,
    opened_at            VARCHAR(60),
    closed_at            VARCHAR(60),
    pnl                  DECIMAL(12,2) NOT NULL DEFAULT 0,
    r_achieved           DECIMAL(8,4)  NOT NULL DEFAULT 0,
    outcome              VARCHAR(20),
    commission           DECIMAL(10,2) DEFAULT 0,
    swap                 DECIMAL(10,2) DEFAULT 0,
    confluence_score     INT           DEFAULT 0,
    confluence_signals   JSON,
    session              VARCHAR(30)   DEFAULT 'Unknown',
    regime               VARCHAR(30)   DEFAULT 'Unknown',
    htf_trend            VARCHAR(30)   DEFAULT 'Unknown',
    entry_reason         TEXT,
    setup_type           VARCHAR(50),
    initial_sl           DECIMAL(18,6) DEFAULT 0,
    initial_tp           DECIMAL(18,6) DEFAULT 0,
    planned_rr           DECIMAL(8,4)  DEFAULT 0,
    actual_rr            DECIMAL(8,4)  DEFAULT 0,
    moved_to_be          TINYINT(1)    DEFAULT 0,
    partial_closed       TINYINT(1)    DEFAULT 0,
    partial_close_r      DECIMAL(8,4)  DEFAULT 0,
    trailing_used        TINYINT(1)    DEFAULT 0,
    structure_break_exit TINYINT(1)    DEFAULT 0,
    what_went_right      TEXT,
    what_went_wrong      TEXT,
    lesson_learned       TEXT,
    would_take_again     VARCHAR(20)   DEFAULT 'Yes',
    notes                TEXT,
    tags                 JSON,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_jnl_symbol (symbol),
    INDEX idx_jnl_closed (closed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS forward_test_trades (
    id                    INT AUTO_INCREMENT PRIMARY KEY,
    trade_id              BIGINT NOT NULL UNIQUE,
    direction             VARCHAR(10) NOT NULL,
    entry_time            DATETIME,
    entry_price           DECIMAL(18,6) NOT NULL DEFAULT 0,
    expected_entry_price  DECIMAL(18,6) NOT NULL DEFAULT 0,
    exit_time             DATETIME,
    exit_price            DECIMAL(18,6),
    profit_loss           DECIMAL(12,2),
    slippage_pips         DECIMAL(8,2)  DEFAULT 0,
    spread_at_entry       DECIMAL(8,2)  DEFAULT 0,
    news_affected         TINYINT(1)    DEFAULT 0,
    exit_reason           VARCHAR(20),
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ft_entry (entry_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS daily_metrics (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    metric_date        DATE NOT NULL UNIQUE,
    trades_taken       INT           DEFAULT 0,
    total_pnl          DECIMAL(12,2) DEFAULT 0,
    total_slippage     DECIMAL(8,2)  DEFAULT 0,
    max_spread         DECIMAL(8,2)  DEFAULT 0,
    uptime_percentage  DECIMAL(5,2)  DEFAULT 100,
    news_blocks        INT           DEFAULT 0,
    connection_issues  INT           DEFAULT 0,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_dm_date (metric_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_state (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    state_key   VARCHAR(100) NOT NULL UNIQUE,
    state_value JSON,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


# ── Connection manager ────────────────────────────────────────────────────────
class DatabaseManager:
    """
    Wraps MySQL with an optional JSON fallback.
    Usage:
        db = DatabaseManager.from_env()
        with db.cursor() as cur:
            cur.execute("SELECT ...")
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "trading_bot",
        pool_size: int = 5,
    ):
        self._cfg = dict(host=host, port=port, user=user, password=password, database=database)
        self._pool: Optional[pooling.MySQLConnectionPool] = None
        self._available = False
        self._pool_size = pool_size

        if MYSQL_AVAILABLE:
            self._connect(pool_size)

    # ── factory ──────────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "DatabaseManager":
        return cls(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            database=os.getenv("MYSQL_DATABASE", "trading_bot"),
        )

    # ── internal ─────────────────────────────────────────────────────────────
    def _connect(self, pool_size: int) -> None:
        try:
            # Create the database if it doesn't exist yet
            tmp = mysql.connector.connect(
                host=self._cfg["host"],
                port=self._cfg["port"],
                user=self._cfg["user"],
                password=self._cfg["password"],
                connection_timeout=5,
            )
            cur = tmp.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self._cfg['database']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            tmp.commit()
            cur.close()
            tmp.close()

            self._pool = pooling.MySQLConnectionPool(
                pool_name="bot_pool",
                pool_size=pool_size,
                connection_timeout=10,
                **self._cfg,
            )
            self._available = True
            logger.info("MySQL connected → %s@%s/%s", self._cfg["user"], self._cfg["host"], self._cfg["database"])
            self._run_schema()
        except Exception as exc:
            logger.warning("MySQL unavailable (%s); using JSON fallback.", exc)
            self._available = False

    def _run_schema(self) -> None:
        statements = [s.strip() for s in _SCHEMA_SQL.split(";") if s.strip()]
        with self.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)

    # ── public API ────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self._available

    @contextmanager
    def cursor(self):
        if not self._available or self._pool is None:
            raise RuntimeError("MySQL is not available.")
        conn = self._pool.get_connection()
        cur = conn.cursor(dictionary=True)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    # ── trades ────────────────────────────────────────────────────────────────
    def insert_trade(self, t: Dict[str, Any]) -> int:
        sql = """
            INSERT INTO trades
              (ticket, symbol, direction, entry_price, exit_price, volume,
               pnl, r_achieved, confluence_score, session, opened_at, closed_at,
               win, commission, swap, confluence_signals)
            VALUES
              (%(ticket)s, %(symbol)s, %(direction)s, %(entry_price)s, %(exit_price)s,
               %(volume)s, %(pnl)s, %(r_achieved)s, %(confluence_score)s, %(session)s,
               %(opened_at)s, %(closed_at)s, %(win)s, %(commission)s, %(swap)s,
               %(confluence_signals)s)
        """
        row = dict(t)
        row["confluence_signals"] = json.dumps(row.get("confluence_signals") or {})
        row["win"] = int(bool(row.get("win", False)))
        with self.cursor() as cur:
            cur.execute(sql, row)
            return cur.lastrowid

    def fetch_trades(self, period_days: int = 30) -> List[Dict]:
        sql = """
            SELECT * FROM trades
            WHERE closed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY closed_at DESC
        """
        with self.cursor() as cur:
            cur.execute(sql, (period_days,))
            rows = cur.fetchall()
        for r in rows:
            if isinstance(r.get("confluence_signals"), str):
                r["confluence_signals"] = json.loads(r["confluence_signals"])
        return rows

    # ── journal ───────────────────────────────────────────────────────────────
    def insert_journal_entry(self, e: Dict[str, Any]) -> int:
        sql = """
            INSERT INTO journal_entries
              (ticket, symbol, direction, entry_price, exit_price, volume,
               opened_at, closed_at, pnl, r_achieved, outcome, commission, swap,
               confluence_score, confluence_signals, session, regime, htf_trend,
               entry_reason, setup_type, initial_sl, initial_tp, planned_rr,
               actual_rr, moved_to_be, partial_closed, partial_close_r,
               trailing_used, structure_break_exit, what_went_right,
               what_went_wrong, lesson_learned, would_take_again, notes, tags)
            VALUES
              (%(ticket)s, %(symbol)s, %(direction)s, %(entry_price)s, %(exit_price)s,
               %(volume)s, %(opened_at)s, %(closed_at)s, %(pnl)s, %(r_achieved)s,
               %(outcome)s, %(commission)s, %(swap)s, %(confluence_score)s,
               %(confluence_signals)s, %(session)s, %(regime)s, %(htf_trend)s,
               %(entry_reason)s, %(setup_type)s, %(initial_sl)s, %(initial_tp)s,
               %(planned_rr)s, %(actual_rr)s, %(moved_to_be)s, %(partial_closed)s,
               %(partial_close_r)s, %(trailing_used)s, %(structure_break_exit)s,
               %(what_went_right)s, %(what_went_wrong)s, %(lesson_learned)s,
               %(would_take_again)s, %(notes)s, %(tags)s)
        """
        row = dict(e)
        row["confluence_signals"] = json.dumps(row.get("confluence_signals") or {})
        row["tags"] = json.dumps(row.get("tags") or [])
        row.setdefault("moved_to_be", 0)
        row.setdefault("partial_closed", 0)
        row.setdefault("trailing_used", 0)
        row.setdefault("structure_break_exit", 0)
        with self.cursor() as cur:
            cur.execute(sql, row)
            return cur.lastrowid

    def fetch_journal_entries(self, limit: int = 200) -> List[Dict]:
        sql = "SELECT * FROM journal_entries ORDER BY closed_at DESC LIMIT %s"
        with self.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
        for r in rows:
            for col in ("confluence_signals", "tags"):
                if isinstance(r.get(col), str):
                    r[col] = json.loads(r[col])
        return rows

    # ── forward test trades ───────────────────────────────────────────────────
    def upsert_ft_trade(self, t: Dict[str, Any]) -> None:
        sql = """
            INSERT INTO forward_test_trades
              (trade_id, direction, entry_time, entry_price, expected_entry_price,
               exit_time, exit_price, profit_loss, slippage_pips, spread_at_entry,
               news_affected, exit_reason)
            VALUES
              (%(trade_id)s, %(direction)s, %(entry_time)s, %(entry_price)s,
               %(expected_entry_price)s, %(exit_time)s, %(exit_price)s,
               %(profit_loss)s, %(slippage_pips)s, %(spread_at_entry)s,
               %(news_affected)s, %(exit_reason)s)
            ON DUPLICATE KEY UPDATE
              exit_time        = VALUES(exit_time),
              exit_price       = VALUES(exit_price),
              profit_loss      = VALUES(profit_loss),
              exit_reason      = VALUES(exit_reason)
        """
        row = dict(t)
        row["news_affected"] = int(bool(row.get("news_affected", False)))
        with self.cursor() as cur:
            cur.execute(sql, row)

    def fetch_ft_trades(self, include_open: bool = True) -> List[Dict]:
        sql = "SELECT * FROM forward_test_trades"
        if not include_open:
            sql += " WHERE exit_time IS NOT NULL"
        sql += " ORDER BY entry_time DESC"
        with self.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    # ── daily metrics ─────────────────────────────────────────────────────────
    def upsert_daily_metrics(self, m: Dict[str, Any]) -> None:
        sql = """
            INSERT INTO daily_metrics
              (metric_date, trades_taken, total_pnl, total_slippage, max_spread,
               uptime_percentage, news_blocks, connection_issues)
            VALUES
              (%(metric_date)s, %(trades_taken)s, %(total_pnl)s, %(total_slippage)s,
               %(max_spread)s, %(uptime_percentage)s, %(news_blocks)s, %(connection_issues)s)
            ON DUPLICATE KEY UPDATE
              trades_taken      = VALUES(trades_taken),
              total_pnl         = VALUES(total_pnl),
              total_slippage    = VALUES(total_slippage),
              max_spread        = VALUES(max_spread),
              uptime_percentage = VALUES(uptime_percentage),
              news_blocks       = VALUES(news_blocks),
              connection_issues = VALUES(connection_issues)
        """
        with self.cursor() as cur:
            cur.execute(sql, m)

    def fetch_daily_metrics(self, days: int = 30) -> List[Dict]:
        sql = """
            SELECT * FROM daily_metrics
            WHERE metric_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            ORDER BY metric_date DESC
        """
        with self.cursor() as cur:
            cur.execute(sql, (days,))
            return cur.fetchall()

    # ── bot state (k/v store) ─────────────────────────────────────────────────
    def set_state(self, key: str, value: Any) -> None:
        sql = """
            INSERT INTO bot_state (state_key, state_value)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE state_value = VALUES(state_value)
        """
        with self.cursor() as cur:
            cur.execute(sql, (key, json.dumps(value)))

    def get_state(self, key: str, default: Any = None) -> Any:
        sql = "SELECT state_value FROM bot_state WHERE state_key = %s"
        with self.cursor() as cur:
            cur.execute(sql, (key,))
            row = cur.fetchone()
        if row is None:
            return default
        val = row["state_value"]
        if isinstance(val, str):
            return json.loads(val)
        return val

    # ── performance summary ───────────────────────────────────────────────────
    def get_win_rate(self, period_days: int = 30) -> Optional[float]:
        sql = """
            SELECT
                COUNT(*)                          AS total,
                SUM(CASE WHEN win=1 THEN 1 END)   AS wins
            FROM trades
            WHERE closed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        with self.cursor() as cur:
            cur.execute(sql, (period_days,))
            row = cur.fetchone()
        if row and row["total"]:
            return (row["wins"] or 0) / row["total"] * 100
        return None
