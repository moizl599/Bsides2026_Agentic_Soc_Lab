#!/usr/bin/env python3
"""
BSides Lab — Interactive startup script.

Usage:
    python start_lab.py
"""

import json
import subprocess
import sys
import time
import ssl
import urllib.request
import urllib.error
import os

# ---- Config ----
LAB_DIR = os.path.dirname(os.path.abspath(__file__))
SPLUNK_URL = "http://localhost:8000"
SPLUNK_API = "https://localhost:8089"
MCP_URL = "http://localhost:3001/health"
THEHIVE_URL = "http://localhost:9000/api/v1/status/public"
N8N_URL = "http://localhost:5678/healthz"

SERVICES = [
    {"name": "Splunk Web", "url": SPLUNK_URL, "timeout": 180},
    {"name": "MCP Server", "url": MCP_URL, "timeout": 30},
    {"name": "TheHive", "url": THEHIVE_URL, "timeout": 180},
    {"name": "n8n", "url": N8N_URL, "timeout": 60},
]

# SSL context that ignores self-signed certs (Splunk management port)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ---- Helpers ----
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"


def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}")


def log_step(step, msg):
    print(f"\n{BOLD}{CYAN}[{step}]{RESET} {BOLD}{msg}{RESET}")


def run(cmd, check=True, capture=False):
    """Run a shell command from the lab directory."""
    kwargs = {"cwd": LAB_DIR, "shell": True}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        log(f"Command failed: {cmd}", RED)
        sys.exit(1)
    return result


def check_url(url, timeout=5):
    """Return True if url responds with 2xx."""
    try:
        req = urllib.request.Request(url, method="GET")
        ctx = SSL_CTX if url.startswith("https") else None
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return 200 <= resp.status < 400
    except Exception:
        return False


def wait_for_service(name, url, timeout_secs):
    """Poll a URL until it responds or timeout."""
    start = time.time()
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while time.time() - start < timeout_secs:
        if check_url(url):
            elapsed = int(time.time() - start)
            log(f"  {GREEN}✓{RESET} {name} is up ({elapsed}s)")
            return True
        print(f"  {spinner[i % len(spinner)]} Waiting for {name}...", end="\r")
        i += 1
        time.sleep(3)
    log(f"  {RED}✗{RESET} {name} did not respond within {timeout_secs}s", RED)
    return False


def check_docker():
    """Verify Docker is running."""
    result = run("docker info > /dev/null 2>&1", check=False, capture=True)
    if result.returncode != 0:
        log("Docker is not running. Start Docker and try again.", RED)
        sys.exit(1)


def check_env():
    """Check that .env has a real API key."""
    env_path = os.path.join(LAB_DIR, ".env")
    if not os.path.exists(env_path):
        log(".env file not found. Copy .env.example and set your DeepSeek API key.", RED)
        sys.exit(1)
    with open(env_path) as f:
        content = f.read()
    if "your-deepseek-api-key-here" in content:
        log(f"{YELLOW}⚠  DeepSeek API key not set in .env — the stack will start but the agent won't work until you set it.{RESET}")


def load_sample_data():
    """Load sample data into Splunk + create TheHive alert."""
    script = os.path.join(LAB_DIR, "sample-data", "load_sample_data.py")
    result = run(
        f"{sys.executable} {script} --splunk-url {SPLUNK_API} --hec-url http://localhost:8088 --thehive-url http://localhost:9000",
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            print(f"  {line}")
        return True
    else:
        log(f"  Failed to load sample data:\n{result.stderr}", RED)
        return False


# ---- Commands ----
def cmd_start(fresh=False):
    log(f"\n{BOLD}{'=' * 50}{RESET}")
    log(f"{BOLD}  BSides Lab — Agentic SOC Demo{RESET}")
    log(f"{BOLD}{'=' * 50}{RESET}")

    # Pre-flight
    log_step("1/6", "Pre-flight checks")
    check_docker()
    log(f"  {GREEN}✓{RESET} Docker is running")
    check_env()

    # Fresh start?
    if fresh:
        log_step("2/6", "Tearing down existing lab (--fresh)")
        run("docker compose down -v", check=False)
        log(f"  {GREEN}✓{RESET} Clean slate")
    else:
        log_step("2/6", "Checking for existing containers")
        result = run("docker compose ps -q", capture=True)
        if result.stdout.strip():
            log(f"  {YELLOW}↻{RESET} Existing containers found — restarting")
            run("docker compose down", check=False)

    # Build & start
    log_step("3/6", "Building and starting containers")
    run("docker compose up -d --build")
    log(f"  {GREEN}✓{RESET} Containers started")

    # Wait for services
    log_step("4/6", "Waiting for services to become healthy")
    all_up = True
    for svc in SERVICES:
        if not wait_for_service(svc["name"], svc["url"], svc["timeout"]):
            all_up = False

    if not all_up:
        log(
            f"\n{YELLOW}Some services didn't come up in time. They may still be starting — "
            f"run 'python start_lab.py --status' to check.{RESET}"
        )

    # Load sample data
    log_step("5/6", "Loading sample data into Splunk")
    load_sample_data()

    # Summary
    log_step("6/6", "Lab is ready")
    print(f"""
{BOLD}Services:{RESET}
  Splunk Web     →  {CYAN}http://localhost:8000{RESET}  (admin / BsidesLab1!)
  n8n            →  {CYAN}http://localhost:5678{RESET}
  TheHive        →  {CYAN}http://localhost:9000{RESET}  (admin@thehive.local / secret)
  MCP Server     →  {CYAN}http://localhost:3001/mcp{RESET}
  MinIO Console  →  {CYAN}http://localhost:9002{RESET}

{BOLD}Next steps:{RESET}
  1. Set your DeepSeek API key in .env (if you haven't)
  2. Open n8n at http://localhost:5678
  3. Import the workflow from n8n-workflow/agentic_soc_triage.json
  4. Create a DeepSeek API credential and trigger the triage
""")


def cmd_stop():
    log_step("1/1", "Shutting down lab")
    run("docker compose down")
    log(f"  {GREEN}✓{RESET} All containers stopped")


def cmd_status():
    log(f"\n{BOLD}BSides Lab — Service Status{RESET}\n")
    for svc in SERVICES:
        if check_url(svc["url"]):
            log(f"  {GREEN}✓{RESET} {svc['name']:20s} UP")
        else:
            log(f"  {RED}✗{RESET} {svc['name']:20s} DOWN")

    # Extra: check Splunk search counts
    import base64
    try:
        creds = base64.b64encode(b"admin:BsidesLab1!").decode()
        req = urllib.request.Request(
            f"{SPLUNK_API}/services/search/jobs/export?output_mode=json&search=search%20index%3Dauth_logs%20%7C%20stats%20count",
            method="GET",
        )
        req.add_header("Authorization", f"Basic {creds}")
        resp = urllib.request.urlopen(req, timeout=10, context=SSL_CTX)
        raw = resp.read().decode()
        for line in raw.strip().split("\n"):
            try:
                data = json.loads(line)
                if "result" in data:
                    log(f"\n  auth_logs: {data['result'].get('count', '?')} events")
                    break
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    print()


def cmd_reload_data():
    """Reload sample data without restarting containers."""
    log_step("1/2", "Checking Splunk")
    if not check_url(SPLUNK_URL):
        log("  Splunk is not running. Start the lab first.", RED)
        return
    log(f"  {GREEN}✓{RESET} Splunk is up")

    log_step("2/2", "Loading sample data")
    load_sample_data()
    log(f"\n  {GREEN}✓{RESET} Data reloaded — timestamps are fresh for your demo")


def cmd_logs():
    """Tail logs from all containers."""
    log(f"\n{BOLD}Tailing logs (Ctrl+C to stop)...{RESET}\n")
    try:
        subprocess.run(
            "docker compose logs -f --tail=50",
            cwd=LAB_DIR, shell=True,
        )
    except KeyboardInterrupt:
        print()


def show_menu():
    """Display the interactive menu and return the choice."""
    print(f"""
{BOLD}{'=' * 50}
  BSides Lab — Agentic SOC Demo
{'=' * 50}{RESET}

  {CYAN}1{RESET}  Start lab
  {CYAN}2{RESET}  Start lab (fresh — wipe everything, clean slate)
  {CYAN}3{RESET}  Stop lab
  {CYAN}4{RESET}  Check status
  {CYAN}5{RESET}  Reload sample data (fresh timestamps for demo)
  {CYAN}6{RESET}  Tail container logs
  {CYAN}0{RESET}  Exit
""")
    try:
        choice = input(f"  {BOLD}Select an option: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return "0"
    return choice


# ---- Main ----
def main():
    while True:
        choice = show_menu()

        if choice == "1":
            cmd_start(fresh=False)
        elif choice == "2":
            # Confirm destructive action
            try:
                confirm = input(f"\n  {YELLOW}This will delete all volumes and data. Continue? (y/N): {RESET}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                continue
            if confirm == "y":
                cmd_start(fresh=True)
            else:
                log("  Cancelled.", YELLOW)
        elif choice == "3":
            cmd_stop()
        elif choice == "4":
            cmd_status()
        elif choice == "5":
            cmd_reload_data()
        elif choice == "6":
            cmd_logs()
        elif choice == "0":
            log(f"\n  {GREEN}See you at BSides.{RESET}\n")
            break
        else:
            log(f"  Invalid option: {choice}", RED)

        # Pause before showing menu again
        try:
            input(f"\n  {BOLD}Press Enter to continue...{RESET}")
        except (KeyboardInterrupt, EOFError):
            print()
            break


if __name__ == "__main__":
    main()
