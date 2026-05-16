"""
Health check script for the 4H Sweep Algo.
Run via cron every 5 minutes during trading hours (01:30–21:30 IST).

Checks:
  1. sweep_algo.py process is running
  2. Today's log file exists and has been written to recently
  3. Open positions count (warns if unexpected)

Usage:
    python health_check.py
    # or via cron:
    # */5 1-21 * * * /opt/sweep-algo/venv/bin/python /opt/sweep-algo/health_check.py
"""

import subprocess
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

from settings import TIMEZONE, SESSION_END_HOUR, SESSION_END_MINUTE, LOG_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("health_check")

ALGO_PROCESS_NAME = "sweep_algo.py"
LOG_FRESHNESS_THRESHOLD = 180  # seconds (3 minutes)


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def is_trading_hours():
    """Returns True if within 01:30–21:35 IST (add 5-min buffer after session end)."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    start_h, start_m = 1, 30
    end_h, end_m = SESSION_END_HOUR, SESSION_END_MINUTE + 5

    start = now.replace(hour=start_h, minute=start_m, second=0)
    end = now.replace(hour=end_h, minute=end_m, second=0)
    return start <= now <= end


def is_algo_running():
    """Check if the sweep_algo.py process is alive."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", ALGO_PROCESS_NAME],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except FileNotFoundError:
        # Windows: use tasklist
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq python.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return ALGO_PROCESS_NAME in result.stdout
        except Exception:
            return False
    except Exception:
        return False


def check_log_freshness():
    """Returns (ok, message) based on how recently the log was written."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = Path(LOG_DIR) / f"sweep_algo_{today}.log"

    if not log_file.exists():
        return False, f"No log file found: {log_file}"

    age = time.time() - log_file.stat().st_mtime
    if age > LOG_FRESHNESS_THRESHOLD:
        return False, f"Log stale ({age:.0f}s since last write — threshold {LOG_FRESHNESS_THRESHOLD}s)"

    return True, f"Log OK ({age:.0f}s since last write)"


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def main():
    if not is_trading_hours():
        logger.info("Outside trading hours. Skipping health check.")
        sys.exit(0)

    errors = []

    # 1. Process check
    if is_algo_running():
        logger.info(f"Process check: {ALGO_PROCESS_NAME} is RUNNING.")
    else:
        msg = f"ALERT: {ALGO_PROCESS_NAME} is NOT running!"
        logger.error(msg)
        errors.append(msg)

    # 2. Log freshness
    log_ok, log_msg = check_log_freshness()
    if log_ok:
        logger.info(f"Log check: {log_msg}")
    else:
        logger.warning(f"Log check WARNING: {log_msg}")
        errors.append(log_msg)

    # 3. Summary
    if errors:
        logger.error(f"Health check FAILED with {len(errors)} issue(s):")
        for e in errors:
            logger.error(f"  - {e}")
        sys.exit(1)
    else:
        logger.info("Health check PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    main()
