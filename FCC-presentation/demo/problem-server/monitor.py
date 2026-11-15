"""
Problem Server — Monitoring Agent
Flask app on port 9100 that exposes REAL metrics from this container.
The container deliberately has problems: high CPU (stress-ng), flaky
file-share service, high memory usage, and real log entries.
"""

import os
import time
import random
import subprocess
import logging
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# ── Track the flaky file-share service ──────────────────────────────
FILESHARE_PID_FILE = "/tmp/fileshare.pid"
_fileshare_crash_count = 0
_last_crash_time = None


def get_cpu_usage():
    """Read real CPU usage from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])

        # Store previous values
        prev = getattr(get_cpu_usage, "_prev", None)
        get_cpu_usage._prev = (idle, total)

        if prev is None:
            # First call — estimate from load average
            try:
                load = os.getloadavg()[0]
                cpu_count = os.cpu_count() or 1
                return min(round(load / cpu_count * 100, 1), 100.0)
            except Exception:
                return 85.0  # fallback high

        prev_idle, prev_total = prev
        diff_idle = idle - prev_idle
        diff_total = total - prev_total
        if diff_total == 0:
            return 0.0

        usage = round((1.0 - diff_idle / diff_total) * 100, 1)
        return min(usage, 100.0)
    except Exception:
        return 87.3  # fallback — always show high CPU


def get_memory_usage():
    """Read real memory usage from /proc/meminfo."""
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                meminfo[parts[0].rstrip(":")] = int(parts[1])  # kB

        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        return {
            "total_mb": round(total / 1024, 1),
            "used_mb": round(used / 1024, 1),
            "available_mb": round(available / 1024, 1),
            "usage_percent": round(used / total * 100, 1) if total > 0 else 0,
        }
    except Exception:
        return {"total_mb": 4096, "used_mb": 3686, "available_mb": 410, "usage_percent": 90.0}


def get_disk_usage():
    """Read real disk usage."""
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        return {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "usage_percent": round(used / total * 100, 1) if total > 0 else 0,
        }
    except Exception:
        return {"total_gb": 50.0, "used_gb": 42.5, "free_gb": 7.5, "usage_percent": 85.0}


def check_fileshare_service():
    """Check the flaky file-share service status."""
    global _fileshare_crash_count, _last_crash_time

    try:
        if os.path.exists(FILESHARE_PID_FILE):
            with open(FILESHARE_PID_FILE) as f:
                pid = int(f.read().strip())
            # Check if process is alive
            os.kill(pid, 0)
            return {"name": "smb-fileshare", "status": "running", "pid": pid, "uptime_seconds": int(time.time()) % 300}
        else:
            _fileshare_crash_count += 1
            _last_crash_time = datetime.now(timezone.utc).isoformat()
            return {
                "name": "smb-fileshare",
                "status": "crashed",
                "pid": None,
                "crash_count": _fileshare_crash_count,
                "last_crash": _last_crash_time,
                "error": "Process exited with signal 11 (SIGSEGV)"
            }
    except (ProcessLookupError, ValueError):
        _fileshare_crash_count += 1
        _last_crash_time = datetime.now(timezone.utc).isoformat()
        return {
            "name": "smb-fileshare",
            "status": "crashed",
            "pid": None,
            "crash_count": _fileshare_crash_count,
            "last_crash": _last_crash_time,
            "error": "Process not found — may have been OOM-killed"
        }


def get_services():
    """Check status of all services on this server."""
    services = []

    # Check stress-ng (CPU hog — should be running)
    try:
        result = subprocess.run(["pgrep", "-f", "stress-ng"], capture_output=True, text=True)
        stress_running = result.returncode == 0
        services.append({
            "name": "backup-indexer",
            "status": "running" if stress_running else "stopped",
            "description": "Nightly backup index rebuild (should have finished 6 hours ago)",
            "cpu_percent": get_cpu_usage() if stress_running else 0,
            "anomaly": "Process has been running for 18+ hours — normally completes in 2 hours" if stress_running else None,
        })
    except Exception:
        services.append({"name": "backup-indexer", "status": "unknown"})

    # Check the flaky file-share
    services.append(check_fileshare_service())

    # SSH — always running
    services.append({"name": "sshd", "status": "running", "port": 22})

    # Monitoring agent (this process)
    services.append({"name": "monitoring-agent", "status": "running", "port": 9100})

    return services


def get_recent_logs():
    """Return recent log entries from the problem server."""
    log_file = "/var/log/problem-server.log"
    logs = []

    try:
        if os.path.exists(log_file):
            with open(log_file) as f:
                lines = f.readlines()
            logs = [line.strip() for line in lines[-20:] if line.strip()]
    except Exception:
        pass

    # Always include some real-time entries
    now = datetime.now(timezone.utc)
    logs.extend([
        f"{now.isoformat()} [WARN] backup-indexer: CPU usage at {get_cpu_usage()}% — process has not completed",
        f"{now.isoformat()} [ERROR] smb-fileshare: Connection reset by peer — client timeout exceeded",
        f"{now.isoformat()} [WARN] disk: /var/log partition at {get_disk_usage()['usage_percent']}% capacity",
        f"{now.isoformat()} [ERROR] smb-fileshare: segfault at 0x7f3a2c001000 ip 0x7f3a34a12b42 sp 0x7ffe98276c10",
        f"{now.isoformat()} [INFO] monitoring-agent: Health check OK",
    ])

    return logs[-15:]


# ── Endpoints ───────────────────────────────────────────────────────

@app.route("/metrics/cpu", methods=["GET"])
def metrics_cpu():
    usage = get_cpu_usage()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return jsonify({
        "usage_percent": usage,
        "load_average": {"1min": round(load[0], 2), "5min": round(load[1], 2), "15min": round(load[2], 2)},
        "cpu_count": os.cpu_count() or 1,
        "status": "critical" if usage > 90 else "warning" if usage > 70 else "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/metrics/memory", methods=["GET"])
def metrics_memory():
    mem = get_memory_usage()
    mem["status"] = "critical" if mem["usage_percent"] > 90 else "warning" if mem["usage_percent"] > 80 else "ok"
    mem["timestamp"] = datetime.now(timezone.utc).isoformat()
    return jsonify(mem)


@app.route("/metrics/disk", methods=["GET"])
def metrics_disk():
    disk = get_disk_usage()
    disk["status"] = "critical" if disk["usage_percent"] > 90 else "warning" if disk["usage_percent"] > 80 else "ok"
    disk["timestamp"] = datetime.now(timezone.utc).isoformat()
    return jsonify(disk)


@app.route("/metrics/services", methods=["GET"])
def metrics_services():
    services = get_services()
    down_count = sum(1 for s in services if s.get("status") not in ("running",))
    return jsonify({
        "services": services,
        "total": len(services),
        "running": len(services) - down_count,
        "down": down_count,
        "status": "critical" if down_count > 1 else "warning" if down_count > 0 else "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/metrics/logs", methods=["GET"])
def metrics_logs():
    return jsonify({
        "entries": get_recent_logs(),
        "server": "fs01.internal",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "server": "fs01.internal", "agent": "monitoring-agent-1.0"})


if __name__ == "__main__":
    log.info("Problem Server monitoring agent starting on :9100")
    app.run(host="0.0.0.0", port=9100)
