#!/bin/bash
# ============================================================
# View logs for the 4H Sweep Algo
# Usage:
#   ./view_logs.sh         — live tail today's algo log
#   ./view_logs.sh events  — live tail events log
#   ./view_logs.sh trades  — show trade history CSV
#   ./view_logs.sh health  — tail health check log
# ============================================================

LOG_DIR="${LOG_DIR:-logs}"
TODAY=$(date +%Y-%m-%d)

case "${1:-algo}" in
    algo)
        echo "=== Algo log: $TODAY ==="
        tail -f "$LOG_DIR/sweep_algo_$TODAY.log"
        ;;
    events)
        echo "=== Events log ==="
        tail -50 "$LOG_DIR/events.csv"
        ;;
    trades)
        echo "=== Trade History ==="
        if command -v column &>/dev/null; then
            column -t -s ',' "$LOG_DIR/trade_history.csv" | less -S
        else
            cat "$LOG_DIR/trade_history.csv"
        fi
        ;;
    health)
        echo "=== Health log ==="
        tail -30 "$LOG_DIR/health.log"
        ;;
    *)
        echo "Usage: $0 [algo|events|trades|health]"
        exit 1
        ;;
esac
