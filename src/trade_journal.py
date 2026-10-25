"""
Trade Journal System
====================
Comprehensive trade journaling with reasoning, confluence breakdown,
and lessons learned for continuous improvement.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from database import DatabaseManager
    _DB_MODULE_OK = True
except ImportError:
    _DB_MODULE_OK = False


@dataclass
class JournalEntry:
    """Single trade journal entry"""
    # Basic trade info
    ticket: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    volume: float
    
    # Timing
    opened_at: str
    closed_at: str
    
    # Results
    pnl: float
    r_achieved: float
    outcome: str  # WIN, LOSS, BREAKEVEN, PARTIAL
    commission: float = 0.0
    swap: float = 0.0
    
    # Confluence details
    confluence_score: int = 0
    confluence_signals: Dict[str, bool] = field(default_factory=dict)
    
    # Context
    session: str = "Unknown"
    regime: str = "Unknown"
    htf_trend: str = "Unknown"
    
    # Trade reasoning
    entry_reason: str = ""
    setup_type: str = ""  # pullback, breakout, reversal
    
    # Risk management
    initial_sl: float = 0.0
    initial_tp: float = 0.0
    planned_rr: float = 0.0
    actual_rr: float = 0.0
    
    # Trade management
    moved_to_be: bool = False
    partial_closed: bool = False
    partial_close_r: float = 0.0
    trailing_used: bool = False
    structure_break_exit: bool = False
    
    # Lessons learned
    what_went_right: str = ""
    what_went_wrong: str = ""
    lesson_learned: str = ""
    would_take_again: str = "Yes"  # Yes, No, Modified
    
    # Metadata
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return asdict(self)


class TradeJournal:
    """
    Manages trade journal entries with automatic lesson prompts
    and performance insights.
    """
    
    def __init__(self, journal_path: str = "data/trade_journal.json", db: "DatabaseManager" = None):
        self.journal_path = journal_path
        self.entries: List[JournalEntry] = []
        self.logger = logging.getLogger("trade-journal")
        self._db: Optional["DatabaseManager"] = db
        self._use_db = False

        if db is None and _DB_MODULE_OK:
            try:
                self._db = DatabaseManager.from_env()
                self._use_db = self._db.available
            except Exception as e:
                self.logger.warning("DB init failed: %s — using JSON fallback", e)

        if self._use_db:
            self.logger.info("TradeJournal using MySQL")
            self._load_from_db()
        else:
            self._load()
    
    def _load(self) -> None:
        """Load journal from JSON (fallback)."""
        if not os.path.exists(self.journal_path):
            return
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            self.entries = []
            for e in raw.get("entries", []):
                try:
                    self.entries.append(JournalEntry(**e))
                except Exception as ex:
                    self.logger.warning("Failed to load journal entry: %s", ex)
        except Exception as e:
            self.logger.error("Failed to load journal: %s", e)

    def _load_from_db(self) -> None:
        """Load journal entries from MySQL."""
        try:
            rows = self._db.fetch_journal_entries(limit=500)
            self.entries = []
            for r in rows:
                try:
                    r.pop("id", None)
                    r.pop("created_at", None)
                    r["moved_to_be"] = bool(r.pop("moved_to_be", False))
                    r["partial_closed"] = bool(r.pop("partial_closed", False))
                    r["trailing_used"] = bool(r.pop("trailing_used", False))
                    r["structure_break_exit"] = bool(r.pop("structure_break_exit", False))
                    r["confluence_signals"] = r.get("confluence_signals") or {}
                    r["tags"] = r.get("tags") or []
                    self.entries.append(JournalEntry(**r))
                except Exception as ex:
                    self.logger.warning("Failed to load DB journal entry: %s", ex)
        except Exception as e:
            self.logger.error("Failed to load journal from DB: %s", e)
    
    def save(self) -> None:
        """Persist to MySQL or JSON."""
        if self._use_db:
            return  # add_entry() writes directly
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_entries": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }
        os.makedirs(os.path.dirname(self.journal_path) or ".", exist_ok=True)
        tmp = f"{self.journal_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.journal_path)
    
    def add_entry(
        self,
        ticket: int,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        volume: float,
        opened_at: str,
        closed_at: str,
        pnl: float,
        r_achieved: float,
        confluence_score: int = 0,
        confluence_signals: Dict[str, bool] = None,
        session: str = "Unknown",
        regime: str = "Unknown",
        entry_reason: str = "",
        setup_type: str = "",
        initial_sl: float = 0.0,
        initial_tp: float = 0.0,
        planned_rr: float = 0.0,
        moved_to_be: bool = False,
        partial_closed: bool = False,
        commission: float = 0.0,
        swap: float = 0.0,
        structure_break_exit: bool = False,
        notes: str = "",
    ) -> JournalEntry:
        """Add a new journal entry"""
        
        # Determine outcome
        if abs(r_achieved) < 0.1:
            outcome = "BREAKEVEN"
        elif r_achieved > 0:
            outcome = "WIN" if not partial_closed else "PARTIAL"
        else:
            outcome = "LOSS"
        
        # Generate automatic lessons based on outcome and confluence
        what_went_right = ""
        what_went_wrong = ""
        lesson_learned = ""
        would_take_again = "Yes"
        
        if outcome == "WIN":
            if confluence_score >= 7:
                what_went_right = "High confluence setup (score: {confluence_score}). Entry aligned with multiple signals."
            elif partial_closed:
                what_went_right = "Managed risk well with partial close at 1.5R."
            
            if r_achieved >= 2.0:
                lesson_learned = "Let winners run! Achieved {r_achieved:.1f}R by holding through the move."
            elif r_achieved < 1.0:
                what_went_wrong = "Exited too early. Could have held for more profit."
                would_take_again = "Modified"
        
        elif outcome == "LOSS":
            if structure_break_exit:
                what_went_right = "Followed exit rules - closed on structure invalidation."
                lesson_learned = "Structure break detection saved larger loss."
            elif confluence_score < 5:
                what_went_wrong = "Low confluence score ({confluence_score}). Should have skipped this setup."
                would_take_again = "No"
            else:
                what_went_wrong = "Setup looked good but market moved against position."
                lesson_learned = "Review market conditions and timing. Consider tighter entries."
        
        elif outcome == "BREAKEVEN":
            lesson_learned = "Trade scratched. Review entry timing and stop placement."
            would_take_again = "Modified"
        
        entry = JournalEntry(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            volume=volume,
            opened_at=opened_at,
            closed_at=closed_at,
            pnl=pnl,
            r_achieved=r_achieved,
            outcome=outcome,
            commission=commission,
            swap=swap,
            confluence_score=confluence_score,
            confluence_signals=confluence_signals or {},
            session=session,
            regime=regime,
            entry_reason=entry_reason,
            setup_type=setup_type,
            initial_sl=initial_sl,
            initial_tp=initial_tp,
            planned_rr=planned_rr,
            actual_rr=abs(r_achieved),
            moved_to_be=moved_to_be,
            partial_closed=partial_closed,
            structure_break_exit=structure_break_exit,
            what_went_right=what_went_right,
            what_went_wrong=what_went_wrong,
            lesson_learned=lesson_learned,
            would_take_again=would_take_again,
            notes=notes,
        )
        
        self.entries.append(entry)

        if self._use_db:
            try:
                row = entry.to_dict()
                row["moved_to_be"] = int(row.pop("moved_to_be", False))
                row["partial_closed"] = int(row.pop("partial_closed", False))
                row["trailing_used"] = int(row.pop("trailing_used", False))
                row["structure_break_exit"] = int(row.pop("structure_break_exit", False))
                self._db.insert_journal_entry(row)
            except Exception as e:
                self.logger.warning("DB journal insert failed, using JSON: %s", e)
                self.save()
        else:
            self.save()

        self.logger.info(
            "Journal entry added: %s %s ticket=%d outcome=%s R=%.2f",
            symbol, direction.upper(), ticket, outcome, r_achieved
        )
        return entry
    
    def format_entry_summary(self, entry: JournalEntry) -> str:
        """Format a journal entry for Telegram notification"""
        outcome_emoji = {
            "WIN": "✅",
            "LOSS": "❌",
            "BREAKEVEN": "➖",
            "PARTIAL": "📊",
        }.get(entry.outcome, "❓")
        
        signals_text = ""
        if entry.confluence_signals:
            signals_text = " | ".join(
                f"{'✓' if v else '✗'}{k}"
                for k, v in entry.confluence_signals.items()
            )
        
        summary = f"""
📝 TRADE JOURNAL ENTRY
{'=' * 25}
{outcome_emoji} {entry.outcome}: {entry.symbol} {entry.direction.upper()}
Ticket: {entry.ticket}

📊 Results
• PnL: ${entry.pnl:.2f} | R: {entry.r_achieved:.2f}
• Entry: {entry.entry_price:.5f} → Exit: {entry.exit_price:.5f}

🎯 Confluence: {entry.confluence_score}/10
{signals_text}

📍 Context
• Session: {entry.session}
• Regime: {entry.regime}
• Setup: {entry.setup_type or 'N/A'}

💡 Lessons
• Right: {entry.what_went_right or 'N/A'}
• Wrong: {entry.what_went_wrong or 'N/A'}
• Lesson: {entry.lesson_learned or 'N/A'}
• Take Again? {entry.would_take_again}
"""
        return summary.strip()
    
