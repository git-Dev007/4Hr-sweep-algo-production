"""
BTCUSD 4-Hour Liquidity Sweep Options Selling Algorithm — Production
Delta Exchange | Runs continuously, trades on 4H candle boundaries

Entry Logic (at each candle close: 05:30 / 09:30 / 13:30 IST):
  - Fetch last 2 completed 4H candles for BTCUSD
  - If last candle swept previous HIGH only → SELL CE
  - If last candle swept previous LOW only  → SELL PE
  - Both or neither swept                  → NO ENTRY

Strike Selection:
  - 05:30 / 09:30 entry: target premium = 400
  - 13:30 entry:         target premium = 200
  - Select the strike whose mark_price is closest to target

Risk Management:
  - 05:30 / 09:30: SL = entry_premium × 1.35
  - 13:30:         SL = entry_premium × 1.50

Profit Trailing (step-wise):
  50% → SL to breakeven | 55% → lock 5% | 60% → lock 10%
  65% → lock 15%        | 70% → lock 20% | 75% → EXIT NOW

Time Exit: position force-closed at next 4H candle boundary
No new entries at/after 17:30 IST
"""

import time
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from settings import (
    API_KEY, API_SECRET, BASE_URL,
    UNDERLYING_SYMBOL, FUTURES_SYMBOL, LEVERAGE, QUANTITY,
    TIMEZONE, CANDLE_CLOSE_TIMES, ALL_CANDLE_BOUNDARIES_HM,
    SESSION_END_HOUR, SESSION_END_MINUTE,
    TARGET_PREMIUMS, PREMIUM_TOLERANCE, SL_MULTIPLIERS, TRAIL_STEPS,
    PNL_CHECK_INTERVAL, ORDER_WAIT_TIMEOUT, MAX_RETRIES, RETRY_DELAY,
    LOG_DIR, LOG_LEVEL, CONTRACT_VALUE,
)
from delta_api import DeltaExchangeAPI
from trade_logger import TradeLogger


# ============================================================
# Logging Setup
# ============================================================
def setup_logging():
    log_dir = Path(LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"sweep_algo_{today}.log"
    fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format=fmt,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


logger = logging.getLogger("sweep_algo")


# ============================================================
# Candle Schedule Helpers
# ============================================================
def _localize(dt, tz):
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def boundary_dt(base_dt, h, m):
    """Return base_dt with hour=h, minute=m, second=0 (same timezone)."""
    return base_dt.replace(hour=h, minute=m, second=0, microsecond=0)


def get_next_entry_candle_close(now):
    """
    Return the next candle close time that is a valid entry window
    (05:30, 09:30, 13:30 IST) and is in the future.
    If past 13:30 for today, return tomorrow's 05:30.
    """
    for h, m in CANDLE_CLOSE_TIMES:
        t = boundary_dt(now, h, m)
        if now < t:
            return t
    # Past 13:30 → tomorrow's first entry window
    tomorrow = now + timedelta(days=1)
    return boundary_dt(tomorrow, CANDLE_CLOSE_TIMES[0][0], CANDLE_CLOSE_TIMES[0][1])


def get_next_4h_boundary(now):
    """
    Return the next 4H candle boundary from ALL_CANDLE_BOUNDARIES_HM.
    This is used as the force-exit time when a position is open.
    """
    for h, m in ALL_CANDLE_BOUNDARIES_HM:
        t = boundary_dt(now, h, m)
        if now < t:
            return t
    # Past 21:30 → 01:30 next day
    tomorrow = now + timedelta(days=1)
    return boundary_dt(tomorrow, 1, 30)


def is_session_ended(now):
    """Returns True if current time is at/after the session end (17:30 IST)."""
    return now >= boundary_dt(now, SESSION_END_HOUR, SESSION_END_MINUTE)


def is_valid_entry_key(h, m):
    return (h, m) in TARGET_PREMIUMS


# ============================================================
# 4H OHLCV Sweep Detection
# ============================================================
def fetch_last_two_4h_candles(api, now_ts):
    """
    Fetch the last two completed 4H candles for FUTURES_SYMBOL.
    Returns (prev_candle, last_closed_candle) as dicts with keys:
      open, high, low, close, time
    Raises on failure.
    """
    # 12h window; query 2 min AFTER now so the just-closed candle is committed in the API.
    # Then filter out any candle whose open time is >= the candle-close boundary.
    end_ts = now_ts + 120
    start_ts = end_ts - (12 * 3600)

    candles = api.get_ohlcv(FUTURES_SYMBOL, resolution="4h", start=start_ts, end=end_ts)

    # Drop any candle that opened at or after now (in-progress candle)
    completed = [c for c in (candles or []) if int(c["time"]) < now_ts]

    if len(completed) < 2:
        raise RuntimeError(
            f"Need ≥2 completed 4H candles, got {len(completed)}. "
            f"Symbol={FUTURES_SYMBOL}"
        )

    prev = completed[-2]
    last = completed[-1]

    logger.info(
        f"4H candles | Previous: H={prev['high']:.1f} L={prev['low']:.1f} "
        f"({datetime.utcfromtimestamp(prev['time']).strftime('%m-%d %H:%M')} UTC)"
    )
    logger.info(
        f"4H candles | Last closed: H={last['high']:.1f} L={last['low']:.1f} "
        f"({datetime.utcfromtimestamp(last['time']).strftime('%m-%d %H:%M')} UTC)"
    )
    return prev, last


def evaluate_sweep(prev, last):
    """
    Check sweep condition:
      last.high > prev.high → HIGH swept
      last.low  < prev.low  → LOW  swept

    Returns:
      'CE'  — only high swept (expect rejection from highs → sell call)
      'PE'  — only low swept  (expect rejection from lows  → sell put)
      None  — both or neither swept (no entry)
    """
    # HIGH sweep: wick above prev high AND close back below prev high (rejection confirmed)
    high_swept = last["high"] > prev["high"] and last["close"] < prev["high"]

    # LOW sweep: wick below prev low AND close back above prev low (rejection confirmed)
    low_swept  = last["low"] < prev["low"] and last["close"] > prev["low"]

    if high_swept and low_swept:
        logger.info(
            f"BOTH sides swept & rejected -> NO ENTRY | H wick={last['high']:.1f}>prev_H={prev['high']:.1f} L wick={last['low']:.1f}<prev_L={prev['low']:.1f} close={last['close']:.1f}"
        )
        return None
    elif high_swept:
        logger.info(
            f"HIGH sweep confirmed: wick={last['high']:.1f}>prev_H={prev['high']:.1f}, close={last['close']:.1f}<prev_H (rejection) -> SELL CE"
        )
        return "CE"
    elif low_swept:
        logger.info(
            f"LOW sweep confirmed: wick={last['low']:.1f}<prev_L={prev['low']:.1f}, close={last['close']:.1f}>prev_L (rejection) -> SELL PE"
        )
        return "PE"
    else:
        logger.info(
            f"No confirmed sweep -> NO ENTRY | H={last['high']:.1f}(prev={prev['high']:.1f}) L={last['low']:.1f}(prev={prev['low']:.1f}) C={last['close']:.1f}"
        )
        return None


# ============================================================
# Option Strike Selection (Premium-Based)
# ============================================================
def find_strike_by_premium(tickers, contract_type, target_premium, tolerance_pct):
    """
    Select the option strike whose current mark_price is CLOSEST to target_premium.
    Rejects the result if the closest strike is farther than ±tolerance_pct from target.

    Args:
        tickers:        List of ticker dicts from get_option_tickers()
        contract_type:  'call_options' or 'put_options'
        target_premium: Target mark price (e.g. 400)
        tolerance_pct:  Maximum allowed deviation (e.g. 0.20 = ±20%)

    Returns:
        Ticker dict of the selected option, or None if no suitable strike found.
    """
    best = None
    best_dist = float("inf")

    for t in tickers:
        if t.get("contract_type") != contract_type:
            continue
        mark = t.get("mark_price")
        if mark is None:
            continue
        try:
            mark = float(mark)
        except (ValueError, TypeError):
            continue
        if mark <= 0:
            continue

        dist = abs(mark - target_premium)
        if dist < best_dist:
            best_dist = dist
            best = t

    if best is None:
        logger.warning(f"No live {contract_type} options found in ticker list.")
        return None

    best_mark = float(best["mark_price"])
    deviation = abs(best_mark - target_premium) / target_premium

    if deviation > tolerance_pct:
        logger.warning(
            f"Closest {contract_type} strike: {best.get('symbol')} "
            f"mark={best_mark:.1f}, deviation={deviation*100:.1f}% > "
            f"tolerance {tolerance_pct*100:.0f}% of target {target_premium} → SKIP"
        )
        return None

    logger.info(
        f"Strike selected: {best.get('symbol')} | mark={best_mark:.2f} | "
        f"target={target_premium} | deviation={deviation*100:.1f}% | "
        f"strike={best.get('strike_price')}"
    )
    return best


def get_nearest_expiry_tickers(api, underlying, now_date):
    """
    Fetch option tickers and filter to nearest expiry ≥ today.
    Returns list of filtered tickers.
    """
    tickers = api.get_option_tickers(underlying)
    if not tickers:
        raise RuntimeError("No option tickers returned from API!")

    nearest_expiry = None
    for t in tickers:
        parts = t.get("symbol", "").split("-")
        if len(parts) >= 4:
            try:
                exp = datetime.strptime(parts[-1], "%d%m%y").date()
                t["_expiry_date"] = exp
                if exp >= now_date and (nearest_expiry is None or exp < nearest_expiry):
                    nearest_expiry = exp
            except ValueError:
                pass

    if nearest_expiry is None:
        raise RuntimeError("No valid expiry dates found in option chain!")

    filtered = [t for t in tickers if t.get("_expiry_date") == nearest_expiry]
    logger.info(
        f"Option chain: nearest expiry={nearest_expiry.strftime('%d-%m-%Y')} | "
        f"{len(filtered)} options"
    )
    return filtered, nearest_expiry


def get_spot_from_tickers(tickers):
    for t in tickers:
        try:
            sp = float(t.get("spot_price", 0) or 0)
            if sp > 0:
                return sp
        except (ValueError, TypeError):
            pass
    return 0.0


# ============================================================
# Trail Tracker
# ============================================================
class TrailTracker:
    """
    Tracks which trailing steps have fired and the current SL level.

    For a short option position (we SOLD premium):
      - We want premium to FALL (profit = entry_premium - current_premium)
      - SL is hit when current_premium RISES to sl_premium
      - Trail steps LOWER sl_premium as profit grows (ratchets favorably)
    """

    def __init__(self, entry_premium, max_profit, sl_mult, trade_logger=None):
        self.entry_premium = entry_premium
        self.max_profit = max_profit
        self.sl_mult = sl_mult

        # Initial SL: entry_premium + (max_profit * sl_mult)
        self.sl_premium = entry_premium + (max_profit * sl_mult)
        self.initial_sl = self.sl_premium

        self.steps_activated = 0
        self.should_exit_immediately = False
        self._trade_logger = trade_logger

        logger.info(
            f"TrailTracker init | entry={entry_premium:.2f} max_profit={max_profit:.2f} "
            f"sl_mult={sl_mult*100:.0f}% initial_SL={self.sl_premium:.2f}"
        )

    def update(self, current_premium):
        """
        Evaluate trail steps against current premium.
        Called every poll cycle while position is open.
        Returns True if anything changed.
        """
        running_profit = self.entry_premium - current_premium
        running_pct = running_profit / self.max_profit if self.max_profit > 0 else 0

        changed = False
        for i, (trigger_pct, lock_pct) in enumerate(TRAIL_STEPS):
            if i < self.steps_activated:
                continue
            if running_pct < trigger_pct:
                break  # Steps are ordered ascending; no need to check further

            self.steps_activated = i + 1

            if lock_pct is None:
                # 75% profit → immediate exit signal
                self.should_exit_immediately = True
                logger.info(
                    f"TRAIL EXIT: {trigger_pct*100:.0f}% profit reached "
                    f"(running={running_profit:.2f}). Signal: EXIT NOW."
                )
                if self._trade_logger:
                    self._trade_logger.log_event(
                        "TRAIL_STEP",
                        f"step={i+1} trigger=75% → EXIT signal"
                    )
            else:
                # Ratchet SL to lock in profit
                new_sl = self.entry_premium - (self.max_profit * lock_pct)
                if new_sl < self.sl_premium:
                    old_sl = self.sl_premium
                    self.sl_premium = new_sl
                    changed = True
                    logger.info(
                        f"TRAIL STEP {i+1}: {trigger_pct*100:.0f}% profit | "
                        f"running={running_profit:.2f} | "
                        f"SL: {old_sl:.2f} → {new_sl:.2f} "
                        f"(locks {lock_pct*100:.0f}% of max_profit)"
                    )
                    if self._trade_logger:
                        self._trade_logger.log_trail_step(
                            i + 1, trigger_pct, lock_pct, current_premium, new_sl
                        )

        return changed

    def is_sl_hit(self, current_premium):
        return current_premium >= self.sl_premium


# ============================================================
# Core Algorithm
# ============================================================
class SweepAlgo:
    def __init__(self):
        self.api = DeltaExchangeAPI(
            BASE_URL, API_KEY, API_SECRET,
            max_retries=MAX_RETRIES,
            retry_delay=RETRY_DELAY,
        )
        self.tz = pytz.timezone(TIMEZONE)
        self.trade_logger = TradeLogger(LOG_DIR)

        self._reset_position()

    def _reset_position(self):
        self.product_id = None
        self.option_symbol = None
        self.option_type = None         # 'CE' or 'PE'
        self.entry_premium = 0.0
        self.max_profit = 0.0
        self.trail_tracker = None
        self.entry_time = None
        self.force_exit_time = None     # Next 4H boundary after entry
        self.candle_entry_key = None    # (h, m) tuple
        self.initial_sl = 0.0
        self.sl_multiplier = 0.0
        self.target_premium = 0.0
        self.spot_at_entry = 0.0
        self.nearest_expiry = None
        self.is_position_open = False

    def now(self):
        return datetime.now(self.tz)

    # ----------------------------------------------------------------
    # Pre-flight
    # ----------------------------------------------------------------
    def preflight_check(self):
        logger.info("=" * 55)
        logger.info("Running pre-flight checks...")

        try:
            balances = self.api.get_wallet_balances()
            if isinstance(balances, list):
                for b in balances:
                    bal = float(b.get("balance", 0))
                    if bal > 0:
                        logger.info(f"  Wallet {b.get('asset_symbol','?')}: {bal:.6f}")
            logger.info("  Auth: OK")
        except Exception as e:
            logger.error(f"  Auth FAILED: {e}")
            raise

        try:
            tickers = self.api.get_option_tickers(UNDERLYING_SYMBOL)
            logger.info(f"  Option chain: OK ({len(tickers)} tickers)")
        except Exception as e:
            logger.error(f"  Option chain FAILED: {e}")
            raise

        try:
            candles = self.api.get_ohlcv(FUTURES_SYMBOL, resolution="4h")
            logger.info(f"  OHLCV feed: OK ({len(candles)} candles returned)")
        except Exception as e:
            logger.error(f"  OHLCV feed FAILED: {e}")
            raise

        logger.info("Pre-flight checks PASSED")
        logger.info("=" * 55)

    # ----------------------------------------------------------------
    # Entry Evaluation
    # ----------------------------------------------------------------
    def try_enter(self, candle_close_time):
        """
        Evaluate sweep signal at the given candle close and enter if valid.
        candle_close_time: datetime (IST) of the candle that just closed.
        Returns True if a position was opened.
        """
        now = self.now()
        candle_key = (candle_close_time.hour, candle_close_time.minute)

        logger.info("-" * 55)
        logger.info(
            f"Evaluating entry at candle close "
            f"{candle_close_time.strftime('%H:%M IST')} | key={candle_key}"
        )

        if is_session_ended(now):
            logger.info(
                f"Session ended ({SESSION_END_HOUR:02d}:{SESSION_END_MINUTE:02d} IST). "
                "Skipping entry."
            )
            self.trade_logger.log_event("NO_SIGNAL", "Session ended — no entry")
            return False

        if self.is_position_open:
            logger.warning("Position already open — skipping new entry.")
            return False

        if not is_valid_entry_key(*candle_key):
            logger.info(f"Candle key {candle_key} not in entry schedule — skip.")
            return False

        # ── 1. Fetch last 2 completed 4H candles ──────────────────
        try:
            now_ts = int(now.timestamp())
            prev_candle, last_candle = fetch_last_two_4h_candles(self.api, now_ts)
        except Exception as e:
            logger.error(f"Cannot fetch 4H candle data: {e}. Skipping entry.")
            self.trade_logger.log_event("NO_SIGNAL", f"OHLCV fetch failed: {e}")
            return False

        # ── 2. Evaluate sweep ──────────────────────────────────────
        signal = evaluate_sweep(prev_candle, last_candle)
        if signal is None:
            self.trade_logger.log_event(
                "NO_SIGNAL",
                f"No clean sweep at {candle_close_time.strftime('%H:%M')} | "
                f"prev H={prev_candle['high']:.1f} L={prev_candle['low']:.1f} | "
                f"last H={last_candle['high']:.1f} L={last_candle['low']:.1f}"
            )
            return False

        self.trade_logger.log_event(
            "SIGNAL",
            f"{signal} sweep at {candle_close_time.strftime('%H:%M')} | "
            f"last H={last_candle['high']:.1f} L={last_candle['low']:.1f} | "
            f"prev H={prev_candle['high']:.1f} L={prev_candle['low']:.1f}"
        )

        # ── 3. Fetch option chain & select strike ──────────────────
        target_premium = TARGET_PREMIUMS[candle_key]
        sl_mult = SL_MULTIPLIERS[candle_key]
        contract_type = "call_options" if signal == "CE" else "put_options"

        try:
            tickers, nearest_expiry = get_nearest_expiry_tickers(
                self.api, UNDERLYING_SYMBOL, now.date()
            )
        except Exception as e:
            logger.error(f"Cannot fetch option chain: {e}. Skipping entry.")
            self.trade_logger.log_event("NO_SIGNAL", f"Option chain failed: {e}")
            return False

        spot = get_spot_from_tickers(tickers)
        if spot <= 0:
            logger.error("Could not determine spot price. Skipping entry.")
            return False
        logger.info(f"Spot: ${spot:,.1f}")

        option = find_strike_by_premium(tickers, contract_type, target_premium, PREMIUM_TOLERANCE)
        if option is None:
            self.trade_logger.log_event(
                "NO_SIGNAL",
                f"No suitable {signal} strike near premium {target_premium}"
            )
            return False

        entry_mark = float(option["mark_price"])
        product_id = option["product_id"]
        symbol = option["symbol"]
        sl_level = entry_mark + (entry_mark * sl_mult)

        logger.info(
            f"ENTRY PLAN: SELL {signal} | {symbol} | "
            f"mark={entry_mark:.2f} (target={target_premium}) | "
            f"SL={sl_level:.2f} ({sl_mult*100:.0f}% mult) | "
            f"spot={spot:.1f}"
        )

        # ── 4. Set leverage ────────────────────────────────────────
        try:
            self.api.set_leverage(product_id, LEVERAGE)
            logger.info(f"Leverage set to {LEVERAGE}x for {symbol}")
        except Exception as e:
            logger.warning(f"Leverage set failed (may already be set): {e}")

        # ── 5. Place sell order ────────────────────────────────────
        try:
            order = self.api.place_order(
                product_id=product_id,
                size=QUANTITY,
                side="sell",
                order_type="market_order",
            )
        except Exception as e:
            logger.error(f"Order placement FAILED: {e}. No position opened.")
            self.trade_logger.log_event("ENTRY_FAIL", str(e))
            return False

        self._wait_for_fill(order, signal)

        # ── 6. Confirm actual fill price from position ─────────────
        actual_entry = self._get_actual_entry_price(product_id)
        if actual_entry and actual_entry > 0:
            entry_mark = actual_entry
            sl_level = entry_mark + (entry_mark * sl_mult)
            logger.info(f"Actual fill: {entry_mark:.2f} | Updated SL: {sl_level:.2f}")

        # ── 7. Store position state ────────────────────────────────
        self.product_id = product_id
        self.option_symbol = symbol
        self.option_type = signal
        self.entry_premium = entry_mark
        self.max_profit = entry_mark
        self.sl_multiplier = sl_mult
        self.target_premium = target_premium
        self.spot_at_entry = spot
        self.nearest_expiry = nearest_expiry
        self.candle_entry_key = candle_key
        self.entry_time = self.now()
        self.is_position_open = True

        # Force exit at the NEXT 4H boundary
        self.force_exit_time = get_next_4h_boundary(self.now())

        # ── 8. Initialise trail tracker ────────────────────────────
        self.trail_tracker = TrailTracker(
            entry_premium=self.entry_premium,
            max_profit=self.max_profit,
            sl_mult=sl_mult,
            trade_logger=self.trade_logger,
        )
        self.initial_sl = self.trail_tracker.sl_premium

        logger.info(
            f"POSITION OPEN: {self.option_type} {self.option_symbol} | "
            f"entry={self.entry_premium:.2f} | SL={self.initial_sl:.2f} | "
            f"force_exit={self.force_exit_time.strftime('%H:%M IST')}"
        )

        self.trade_logger.log_entry({
            "signal": signal,
            "symbol": symbol,
            "entry_premium": self.entry_premium,
            "sl_level": self.initial_sl,
            "target_premium": target_premium,
            "spot": spot,
            "quantity": QUANTITY,
            "leverage": LEVERAGE,
            "candle_key": str(candle_key),
        })

        return True

    # ----------------------------------------------------------------
    # Fill & Position Helpers
    # ----------------------------------------------------------------
    def _wait_for_fill(self, order_response, label):
        order_id = (
            order_response.get("id")
            if isinstance(order_response, dict)
            else None
        )
        if not order_id:
            logger.info(f"{label} order appears filled immediately (no order ID returned).")
            return

        deadline = time.time() + ORDER_WAIT_TIMEOUT
        while time.time() < deadline:
            try:
                o = self.api.get_order(order_id)
                state = o.get("state", "")
                if state in ("closed", "filled"):
                    logger.info(f"{label} order {order_id} confirmed filled.")
                    return
                if state in ("cancelled", "rejected"):
                    raise RuntimeError(f"{label} order {state}! id={order_id}")
            except Exception as e:
                if "not found" in str(e).lower():
                    logger.info(f"{label} order filled (removed from active orders).")
                    return
                logger.warning(f"Checking {label} order: {e}")
            time.sleep(1)

        logger.warning(f"{label} fill not confirmed within {ORDER_WAIT_TIMEOUT}s. Proceeding.")

    def _get_actual_entry_price(self, product_id):
        time.sleep(1)
        try:
            positions = self.api.get_margined_positions(product_ids=[product_id])
            if isinstance(positions, list):
                for pos in positions:
                    if pos.get("product_id") == product_id:
                        ep = float(pos.get("entry_price", 0) or 0)
                        if ep > 0:
                            return ep
        except Exception as e:
            logger.warning(f"Could not confirm fill price from position: {e}")
        return None

    def _get_current_mark_price(self):
        """Fetch current mark price for the open option."""
        # Try direct ticker first (fastest, no auth needed)
        try:
            ticker = self.api.get_ticker(self.option_symbol)
            if isinstance(ticker, dict):
                mark = ticker.get("mark_price")
                if mark:
                    return float(mark)
        except Exception as e:
            logger.debug(f"Ticker fetch failed for {self.option_symbol}: {e}")

        # Fallback: position endpoint
        try:
            positions = self.api.get_margined_positions(product_ids=[self.product_id])
            if isinstance(positions, list):
                for pos in positions:
                    if pos.get("product_id") == self.product_id:
                        mark = pos.get("mark_price")
                        if mark:
                            return float(mark)
        except Exception as e:
            logger.warning(f"Position mark price fetch failed: {e}")

        return None

    def _get_current_spot(self):
        try:
            tickers, _ = get_nearest_expiry_tickers(
                self.api, UNDERLYING_SYMBOL, self.now().date()
            )
            return get_spot_from_tickers(tickers)
        except Exception:
            return 0.0

    # ----------------------------------------------------------------
    # Exit
    # ----------------------------------------------------------------
    def exit_position(self, reason):
        """Close the open sell position and record the trade."""
        if not self.is_position_open:
            return

        logger.info(f"EXITING | {self.option_symbol} | Reason: {reason}")

        # Get exit mark price before placing order
        exit_premium = self._get_current_mark_price()

        # Place buy-to-close order
        exit_success = False
        for attempt in range(1, 3):
            try:
                self.api.place_order(
                    product_id=self.product_id,
                    size=QUANTITY,
                    side="buy",
                    order_type="market_order",
                    reduce_only=True,
                )
                exit_success = True
                logger.info(f"Exit order placed (attempt {attempt})")
                break
            except Exception as e:
                logger.error(f"Exit order attempt {attempt} failed: {e}")
                if attempt < 2:
                    time.sleep(2)

        if not exit_success:
            logger.critical(
                f"CRITICAL: Could not close position {self.option_symbol}. "
                "Manual intervention required!"
            )

        # Use SL level as fallback exit price estimate
        if exit_premium is None:
            exit_premium = (
                self.trail_tracker.sl_premium
                if self.trail_tracker
                else self.initial_sl
            )
            logger.warning(
                f"Exit mark price unavailable, estimating at SL={exit_premium:.2f}"
            )

        pnl_per_unit = self.entry_premium - exit_premium
        pnl_total_usd = pnl_per_unit * QUANTITY * CONTRACT_VALUE

        exit_time = self.now()
        spot_at_exit = self._get_current_spot()

        logger.info(
            f"TRADE CLOSED | {self.option_type} {self.option_symbol} | "
            f"entry={self.entry_premium:.2f} exit={exit_premium:.2f} "
            f"pnl_unit={pnl_per_unit:+.2f} pnl_total_usd={pnl_total_usd:+.4f} BTC | "
            f"reason={reason}"
        )

        self.trade_logger.log_exit({
            "entry_time":       self.entry_time.isoformat() if self.entry_time else "",
            "exit_time":        exit_time.isoformat(),
            "signal":           self.option_type,
            "symbol":           self.option_symbol,
            "strike_price":     self._parse_strike(self.option_symbol),
            "expiry":           self.nearest_expiry.strftime("%d%m%y") if self.nearest_expiry else "",
            "entry_premium":    self.entry_premium,
            "exit_premium":     exit_premium,
            "pnl_per_unit":     pnl_per_unit,
            "pnl_total_usd":    pnl_total_usd,
            "max_profit":       self.max_profit,
            "sl_level_initial": self.initial_sl,
            "sl_level_final":   self.trail_tracker.sl_premium if self.trail_tracker else self.initial_sl,
            "trail_steps_hit":  self.trail_tracker.steps_activated if self.trail_tracker else 0,
            "reason":           reason,
            "candle_key":       str(self.candle_entry_key),
            "spot_at_entry":    self.spot_at_entry,
            "spot_at_exit":     spot_at_exit,
            "quantity":         QUANTITY,
            "leverage":         LEVERAGE,
            "target_premium":   self.target_premium,
            "sl_multiplier":    self.sl_multiplier,
        })

        self._reset_position()

    @staticmethod
    def _parse_strike(symbol):
        """Extract strike price from option symbol like C-BTC-90000-310125."""
        try:
            return int(symbol.split("-")[2])
        except (IndexError, ValueError):
            return 0

    # ----------------------------------------------------------------
    # Position Monitoring Loop
    # ----------------------------------------------------------------
    def monitor_position(self):
        """
        Poll the open position every PNL_CHECK_INTERVAL seconds.
        Exits on: SL hit, trail exit signal, 75% profit, or candle close.
        """
        logger.info(
            f"Monitoring {self.option_type} {self.option_symbol} | "
            f"entry={self.entry_premium:.2f} | "
            f"max_profit={self.max_profit:.2f} | "
            f"SL={self.trail_tracker.sl_premium:.2f} | "
            f"force_exit={self.force_exit_time.strftime('%H:%M IST')}"
        )

        consecutive_failures = 0

        while self.is_position_open:
            now = self.now()

            # ── Time-based force exit ──────────────────────────────
            if now >= self.force_exit_time:
                self.exit_position("CANDLE_CLOSE (time-based force exit)")
                return

            # ── Get current mark price ─────────────────────────────
            current_premium = self._get_current_mark_price()
            if current_premium is None:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    logger.error(
                        "5 consecutive failures fetching mark price. "
                        "Force-exiting for safety."
                    )
                    self.exit_position("SAFETY_EXIT (mark price unavailable)")
                    return
                logger.warning(
                    f"Could not get mark price ({consecutive_failures}/5). Retrying..."
                )
                time.sleep(PNL_CHECK_INTERVAL)
                continue

            consecutive_failures = 0
            running_profit = self.entry_premium - current_premium
            running_pct    = running_profit / self.max_profit if self.max_profit > 0 else 0
            time_left      = (self.force_exit_time - now).total_seconds()

            # Log every poll cycle at INFO so cron log shows full live detail
            logger.info(
                f"[TICK] {self.option_type} {self.option_symbol} | "
                f"mark={current_premium:.2f} | "
                f"entry={self.entry_premium:.2f} | "
                f"profit={running_profit:+.2f} ({running_pct*100:.1f}%) | "
                f"SL={self.trail_tracker.sl_premium:.2f} | "
                f"trail_steps={self.trail_tracker.steps_activated} | "
                f"force_exit={self.force_exit_time.strftime('%H:%M')} "
                f"({time_left/60:.1f}min left)"
            )

            # ── Update trail steps ─────────────────────────────────
            self.trail_tracker.update(current_premium)

            # ── Immediate exit at 75% profit ──────────────────────
            if self.trail_tracker.should_exit_immediately:
                self.exit_position("TRAIL_EXIT (75% profit target reached)")
                return

            # ── Stop loss ─────────────────────────────────────────
            if self.trail_tracker.is_sl_hit(current_premium):
                self.exit_position(
                    f"STOP_LOSS (mark={current_premium:.2f} >= SL={self.trail_tracker.sl_premium:.2f})"
                )
                return

            time.sleep(PNL_CHECK_INTERVAL)

    # ----------------------------------------------------------------
    # Main Loop
    # ----------------------------------------------------------------
    def run(self):
        logger.info("=" * 60)
        logger.info("4H Liquidity Sweep Options Selling Algo — LIVE")
        logger.info(f"  Underlying : {UNDERLYING_SYMBOL}")
        logger.info(f"  Futures    : {FUTURES_SYMBOL} (OHLCV source)")
        logger.info(f"  Quantity   : {QUANTITY} contracts per trade")
        logger.info(f"  Leverage   : {LEVERAGE}x")
        logger.info(f"  Entry windows (IST): 05:30, 09:30, 13:30")
        logger.info(f"  Session ends (IST) : {SESSION_END_HOUR:02d}:{SESSION_END_MINUTE:02d}")
        logger.info("=" * 60)

        self.preflight_check()

        while True:
            now = self.now()

            if not self.is_position_open:
                next_close = get_next_entry_candle_close(now)
                wait_secs = (next_close - now).total_seconds()

                if is_session_ended(now):
                    # Wait until tomorrow's first entry window
                    tomorrow = now + timedelta(days=1)
                    next_window = boundary_dt(
                        tomorrow,
                        CANDLE_CLOSE_TIMES[0][0],
                        CANDLE_CLOSE_TIMES[0][1],
                    )
                    wait_secs = (next_window - now).total_seconds()
                    logger.info(
                        f"Session ended. Next window: "
                        f"{next_window.strftime('%Y-%m-%d %H:%M IST')} "
                        f"({wait_secs/3600:.1f}h)"
                    )
                    self._sleep_until(next_window)
                    continue

                if wait_secs > 30:
                    logger.info(
                        f"Waiting for candle close at "
                        f"{next_close.strftime('%H:%M IST')} "
                        f"({wait_secs/60:.1f} min)"
                    )
                    self._sleep_until(next_close)

                # Give exchange a few seconds to finalise the candle
                time.sleep(8)

                # Evaluate entry at the candle close that just passed
                target_close = boundary_dt(
                    self.now(),
                    next_close.hour,
                    next_close.minute,
                )
                self.try_enter(target_close)

            if self.is_position_open:
                self.monitor_position()

            time.sleep(1)

    def _sleep_until(self, target_dt):
        """Sleep in short chunks until target_dt to remain interruptible."""
        while True:
            remaining = (target_dt - self.now()).total_seconds()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 30))


# ============================================================
# Entry Point
# ============================================================
if __name__ == "__main__":
    setup_logging()
    algo = SweepAlgo()
    try:
        algo.run()
    except KeyboardInterrupt:
        logger.info("Algo stopped by user (KeyboardInterrupt).")
        if algo.is_position_open:
            logger.warning(
                f"⚠  Position still open: {algo.option_symbol}. "
                "Please close manually on Delta Exchange!"
            )
    except Exception as exc:
        logger.critical(f"Algo crashed: {exc}", exc_info=True)
        sys.exit(1)
