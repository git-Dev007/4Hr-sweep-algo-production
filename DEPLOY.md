# 4H Sweep Algo BTCUSD — Deployment Guide

## Strategy Summary

| Parameter | Value |
|---|---|
| Instrument | BTCUSD Options (CE / PE) |
| Timeframe | 4-Hour candles |
| Direction | Options SELLING (premium collection) |
| Entry trigger | Liquidity sweep of previous 4H candle H/L |
| Entry windows (IST) | 05:30 / 09:30 / 13:30 |
| Session end | No new trades at/after 17:30 IST |
| Quantity | 50 contracts |
| Leverage | 200x |

### Entry Logic
| Last Closed Candle vs Previous | Action |
|---|---|
| Sweeps previous HIGH only | SELL CE (nearest premium to target) |
| Sweeps previous LOW only | SELL PE (nearest premium to target) |
| Sweeps BOTH | NO ENTRY |
| Sweeps NEITHER | NO ENTRY |

### Target Premiums
| Entry Time | Target Premium |
|---|---|
| 05:30 | 400 |
| 09:30 | 400 |
| 13:30 | 200 |

### Stop Loss
| Entry Time | SL Multiplier | Example (entry=400) |
|---|---|---|
| 05:30 | 35% | SL at 540 (loss 140) |
| 09:30 | 35% | SL at 540 (loss 140) |
| 13:30 | 50% | SL at 300 (loss 100) |

### Profit Trailing
| Running Profit % | New SL |
|---|---|
| 50% | Breakeven |
| 55% | Lock 5% |
| 60% | Lock 10% |
| 65% | Lock 15% |
| 70% | Lock 20% |
| **75%** | **EXIT immediately** |

---

## Prerequisites

- Python 3.10+
- Delta Exchange account (India) with API credentials
- VPS/server in a low-latency location (Mumbai recommended)

---

## Quick Start

### 1. Clone / upload files

```bash
mkdir -p /opt/sweep-algo
cd /opt/sweep-algo
# Upload all production/ files here
```

### 2. Run setup script

```bash
sudo bash setup.sh
```

### 3. Configure credentials

```bash
nano /opt/sweep-algo/.env
```

Fill in:
```
DELTA_API_KEY=your_real_key
DELTA_API_SECRET=your_real_secret
DELTA_BASE_URL=https://api.india.delta.exchange
```

### 4. Test run

```bash
sudo -u algo /opt/sweep-algo/venv/bin/python /opt/sweep-algo/sweep_algo.py
```

Watch output for pre-flight checks, then `Ctrl+C` after verification.

### 5. Enable cron (auto-start daily)

Cron is already installed by `setup.sh`. Verify:

```bash
crontab -l -u algo
```

---

## Manual Operations

### Start manually
```bash
sudo -u algo /opt/sweep-algo/venv/bin/python /opt/sweep-algo/sweep_algo.py &
```

### Stop
```bash
pkill -f sweep_algo.py
```

### View live log
```bash
./view_logs.sh
```

### View trade history
```bash
./view_logs.sh trades
```

### View all events (signals, SL changes, trail steps)
```bash
./view_logs.sh events
```

---

## File Structure

```
production/
├── sweep_algo.py      ← Main algorithm (entry point)
├── delta_api.py       ← Delta Exchange API client
├── settings.py        ← Config loaded from .env
├── trade_logger.py    ← CSV trade & event logging
├── health_check.py    ← Process health monitor (run via cron)
├── requirements.txt
├── setup.sh           ← One-time server setup
├── view_logs.sh       ← Log viewer helper
├── .env.example       ← Environment template
├── .env               ← Your actual credentials (never commit)
└── logs/
    ├── sweep_algo_YYYY-MM-DD.log  ← Daily algo log
    ├── events.csv                 ← All signal/entry/exit events
    ├── trade_history.csv          ← One row per trade
    └── health.log                 ← Health check results
```

---

## Important Notes

1. **Only one position open at a time** — the algo skips new entries if a position is active.
2. **Maximum hold time = one 4H candle** — positions are force-closed at the next 4H boundary.
3. **No entries after 17:30 IST** — the algo waits until 05:30 the next day.
4. **Testnet first** — set `DELTA_BASE_URL=https://cdn-ind.testnet.deltaex.org` for paper trading.
5. **API key permissions** — enable: Read + Trade. Do NOT enable Withdrawal.
