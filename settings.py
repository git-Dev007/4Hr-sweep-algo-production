"""
4-Hour Liquidity Sweep Options Selling Algo — Production Settings
Reads all settings from environment variables (.env file).

Strategy summary:
  - Trade BTCUSD options on 4H candle boundaries (01:30/05:30/09:30/13:30 IST)
  - Entry at 05:30, 09:30, 13:30 only (no new entries at/after 17:30)
  - Sell CE if previous 4H candle high swept only
  - Sell PE if previous 4H candle low swept only
  - Strike selected by closest mark price to target premium
  - Step-wise trailing profit with hard 75% profit exit
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)


def _require(key):
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return val


# ----- API Credentials -----
API_KEY = _require("DELTA_API_KEY")
API_SECRET = _require("DELTA_API_SECRET")
BASE_URL = _require("DELTA_BASE_URL")

# ----- Instrument -----
UNDERLYING_SYMBOL = os.getenv("UNDERLYING_SYMBOL", "BTC")
FUTURES_SYMBOL = os.getenv("FUTURES_SYMBOL", "BTCUSD")   # For OHLCV (spot/perp)

# ----- Position Sizing -----
LEVERAGE = int(os.getenv("LEVERAGE", "200"))
QUANTITY = int(os.getenv("QUANTITY", "50"))

# ----- Timezone -----
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

# ----- Candle Schedule (IST) -----
# 4H candle boundaries: 01:30, 05:30, 09:30, 13:30, 17:30, 21:30 IST
# Valid entry windows (at candle CLOSE): 05:30, 09:30, 13:30
# No new entries at/after 17:30
CANDLE_CLOSE_TIMES = [(5, 30), (9, 30), (13, 30)]
ALL_CANDLE_BOUNDARIES_HM = [(1, 30), (5, 30), (9, 30), (13, 30), (17, 30), (21, 30)]
SESSION_END_HOUR = int(os.getenv("SESSION_END_HOUR", "17"))
SESSION_END_MINUTE = int(os.getenv("SESSION_END_MINUTE", "30"))

# ----- Target Premiums per Entry Session -----
# Strike is selected whose mark_price is CLOSEST to the target premium
TARGET_PREMIUMS = {
    (5, 30): float(os.getenv("TARGET_PREMIUM_0530", "400")),   # 05:30 entry
    (9, 30): float(os.getenv("TARGET_PREMIUM_0930", "400")),   # 09:30 entry
    (13, 30): float(os.getenv("TARGET_PREMIUM_1330", "200")),  # 13:30 entry
}

# Skip entry if no strike is within ±PREMIUM_TOLERANCE of target premium
PREMIUM_TOLERANCE = float(os.getenv("PREMIUM_TOLERANCE", "0.20"))

# ----- Stop Loss Multipliers per Entry Session -----
# SL level (option premium) = entry_premium + (entry_premium × multiplier)
# i.e. max loss = entry_premium × multiplier
SL_MULTIPLIERS = {
    (5, 30): float(os.getenv("SL_MULT_0530", "0.35")),    # 35% of max profit
    (9, 30): float(os.getenv("SL_MULT_0930", "0.35")),    # 35% of max profit
    (13, 30): float(os.getenv("SL_MULT_1330", "0.50")),   # 50% of max profit
}

# ----- Trailing Profit Steps -----
# Each tuple: (trigger_pct_of_max_profit, locked_profit_pct_of_max_profit)
# locked=None means EXIT IMMEDIATELY when trigger is reached
TRAIL_STEPS = [
    (0.50, 0.00),   # 50% running profit → SL to breakeven
    (0.55, 0.05),   # 55% running profit → lock 5%
    (0.60, 0.10),   # 60% running profit → lock 10%
    (0.65, 0.15),   # 65% running profit → lock 15%
    (0.70, 0.20),   # 70% running profit → lock 20%
    (0.75, None),   # 75% running profit → EXIT immediately
]

# ----- Polling Intervals -----
PNL_CHECK_INTERVAL = int(os.getenv("PNL_CHECK_INTERVAL", "5"))    # seconds
ORDER_WAIT_TIMEOUT = int(os.getenv("ORDER_WAIT_TIMEOUT", "30"))   # seconds

# ----- API Retry Settings -----
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.getenv("RETRY_DELAY", "2"))

# ----- Logging -----
LOG_DIR = os.getenv("LOG_DIR", str(Path(__file__).resolve().parent / "logs"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ----- Contract Details -----
CONTRACT_VALUE = float(os.getenv("CONTRACT_VALUE", "0.001"))  # BTC per contract

# ----- Liquidation Safety -----
# Emergency soft exit threshold: exit if mark price reaches entry × LIQ_SAFETY_MULT
# At 200x leverage BTC options, liq ≈ entry × 1.44; we exit at 1.40 (before liq)
# Hard stop order is ALSO placed on exchange at the SL level as primary protection
LIQ_SAFETY_MULT = float(os.getenv("LIQ_SAFETY_MULT", "1.40"))
