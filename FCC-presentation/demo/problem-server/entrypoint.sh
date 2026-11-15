#!/bin/sh
# Problem Server Entrypoint
# Starts real problems + monitoring agent

echo "[$(date -u +%FT%TZ)] Problem Server booting..."
LOG="/var/log/problem-server.log"
touch "$LOG"

# ── 1. Start CPU hog (simulates stuck backup-indexer) ──────────
echo "[$(date -u +%FT%TZ)] [INFO] Starting backup-indexer process..." >> "$LOG"
stress-ng --cpu 1 --cpu-load 75 --timeout 0 &
echo "[$(date -u +%FT%TZ)] [WARN] backup-indexer: Process started — expected completion: 2 hours" >> "$LOG"

# ── 2. Start flaky file-share service ──────────────────────────
# This script runs in a loop: start, run for a bit, crash, repeat
(
  while true; do
    echo "[$(date -u +%FT%TZ)] [INFO] smb-fileshare: Starting service..." >> "$LOG"
    # Create a dummy process to represent the file share
    sleep 120 &
    FSPID=$!
    echo "$FSPID" > /tmp/fileshare.pid
    echo "[$(date -u +%FT%TZ)] [INFO] smb-fileshare: Service running (PID $FSPID)" >> "$LOG"

    # Run for 45-90 seconds then "crash"
    CRASH_DELAY=$((45 + RANDOM % 45))
    sleep "$CRASH_DELAY"

    # Kill the process to simulate crash
    kill "$FSPID" 2>/dev/null
    rm -f /tmp/fileshare.pid
    echo "[$(date -u +%FT%TZ)] [ERROR] smb-fileshare: Process crashed (signal 11)" >> "$LOG"
    echo "[$(date -u +%FT%TZ)] [ERROR] smb-fileshare: segfault at 0x7f3a2c001000 ip 0x7f3a34a12b42" >> "$LOG"
    echo "[$(date -u +%FT%TZ)] [WARN] smb-fileshare: Restarting in 30 seconds..." >> "$LOG"

    # Wait before restart
    sleep 30
  done
) &

# ── 3. Generate log entries periodically ───────────────────────
(
  while true; do
    echo "[$(date -u +%FT%TZ)] [WARN] backup-indexer: Still running — CPU at $(cat /proc/loadavg | awk '{print $1}')" >> "$LOG"
    echo "[$(date -u +%FT%TZ)] [INFO] monitoring-agent: Metrics collected" >> "$LOG"
    sleep 60

    # Occasional disk warning
    echo "[$(date -u +%FT%TZ)] [WARN] disk: Large temp files accumulating in /var/backup/staging" >> "$LOG"
    sleep 60
  done
) &

# ── 4. Allocate memory (simulates memory pressure) ─────────────
# Allocate ~200MB of memory and hold it
(
  python3 -c "
import time
# Allocate ~200MB
data = [bytearray(1024*1024) for _ in range(200)]
while True:
    time.sleep(3600)
" &
)

echo "[$(date -u +%FT%TZ)] [INFO] All problem processes started" >> "$LOG"
echo "[$(date -u +%FT%TZ)] Problem Server ready — monitoring agent on :9100"

# ── 5. Start monitoring agent (foreground) ─────────────────────
exec python3 /app/monitor.py
