"""
Trade logger — records every signal, entry, SL change, trail step, and exit
to a persistent CSV file for audit and P&L review.
"""

import csv
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("trade_logger")

# Fields for the entry/exit summary log
TRADE_LOG_FIELDS = [
    "date",
    "entry_time",
    "exit_time",
    "signal",           # CE or PE
    "option_symbol",
    "strike_price",
    "expiry",
    "entry_premium",
    "exit_premium",
    "pnl_per_unit",
    "pnl_total_usd",
    "max_profit",
    "sl_level_initial",
    "sl_level_final",
    "trail_steps_hit",
    "exit_reason",
    "candle_entry_key", # e.g. "(5, 30)"
    "spot_at_entry",
    "spot_at_exit",
    "quantity",
    "leverage",
    "target_premium",
    "sl_multiplier",
]

# Fields for the detailed event log (every signal/SL-change/trail event)
EVENT_LOG_FIELDS = [
    "timestamp",
    "event_type",       # SIGNAL / NO_SIGNAL / ENTRY / SL_CHANGE / TRAIL_STEP / EXIT
    "detail",
]


class TradeLogger:
    def __init__(self, log_dir):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.trade_file = self.log_dir / "trade_history.csv"
        self.event_file = self.log_dir / "events.csv"

        self._ensure_header(self.trade_file, TRADE_LOG_FIELDS)
        self._ensure_header(self.event_file, EVENT_LOG_FIELDS)

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------
    def _ensure_header(self, filepath, fields):
        if not filepath.exists():
            with open(filepath, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()
            logger.info(f"Log created: {filepath}")

    def _append(self, filepath, fields, row):
        try:
            with open(filepath, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writerow(row)
        except Exception as e:
            logger.error(f"Failed to write to {filepath}: {e}")

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------
    def log_event(self, event_type, detail):
        """Log a generic event (signal evaluation, SL update, etc.)."""
        row = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "detail": detail,
        }
        self._append(self.event_file, EVENT_LOG_FIELDS, row)
        logger.info(f"[EVENT] {event_type}: {detail}")

    def log_entry(self, data):
        """Called on trade entry. data is a dict with entry fields."""
        self.log_event("ENTRY", (
            f"signal={data.get('signal')} symbol={data.get('symbol')} "
            f"premium={data.get('entry_premium'):.2f} "
            f"SL={data.get('sl_level'):.2f} "
            f"target={data.get('target_premium')} "
            f"spot={data.get('spot'):.1f}"
        ))

    def log_sl_change(self, old_sl, new_sl, reason):
        """Called when SL level changes due to trailing."""
        self.log_event("SL_CHANGE", f"SL {old_sl:.2f} → {new_sl:.2f} | {reason}")

    def log_trail_step(self, step_num, trigger_pct, locked_pct, current_premium, new_sl):
        """Called when a trail step is activated."""
        self.log_event("TRAIL_STEP", (
            f"step={step_num} trigger={trigger_pct*100:.0f}% "
            f"locked={locked_pct*100:.0f}% mark={current_premium:.2f} new_SL={new_sl:.2f}"
        ))

    def log_exit(self, data):
        """Called on trade exit — writes full trade record to trade_history.csv."""
        row = {
            "date":             datetime.utcnow().strftime("%Y-%m-%d"),
            "entry_time":       data.get("entry_time", ""),
            "exit_time":        data.get("exit_time", ""),
            "signal":           data.get("signal", ""),
            "option_symbol":    data.get("symbol", ""),
            "strike_price":     data.get("strike_price", ""),
            "expiry":           data.get("expiry", ""),
            "entry_premium":    round(data.get("entry_premium", 0), 4),
            "exit_premium":     round(data.get("exit_premium", 0), 4),
            "pnl_per_unit":     round(data.get("pnl_per_unit", 0), 4),
            "pnl_total_usd":    round(data.get("pnl_total_usd", 0), 4),
            "max_profit":       round(data.get("max_profit", 0), 4),
            "sl_level_initial": round(data.get("sl_level_initial", 0), 4),
            "sl_level_final":   round(data.get("sl_level_final", 0), 4),
            "trail_steps_hit":  data.get("trail_steps_hit", 0),
            "exit_reason":      data.get("reason", ""),
            "candle_entry_key": data.get("candle_key", ""),
            "spot_at_entry":    round(data.get("spot_at_entry", 0), 2),
            "spot_at_exit":     round(data.get("spot_at_exit", 0), 2),
            "quantity":         data.get("quantity", 0),
            "leverage":         data.get("leverage", 0),
            "target_premium":   data.get("target_premium", 0),
            "sl_multiplier":    data.get("sl_multiplier", 0),
        }
        self._append(self.trade_file, TRADE_LOG_FIELDS, row)
        self.log_event("EXIT", (
            f"signal={row['signal']} symbol={row['option_symbol']} "
            f"entry={row['entry_premium']} exit={row['exit_premium']} "
            f"pnl_unit={row['pnl_per_unit']:+.4f} "
            f"reason={row['exit_reason']}"
        ))
        logger.info(
            f"Trade logged: {row['option_symbol']} | "
            f"PnL/unit={row['pnl_per_unit']:+.4f} | "
            f"Total=${row['pnl_total_usd']:+.4f} | "
            f"Reason={row['exit_reason']}"
        )
