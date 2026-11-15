#!/usr/bin/env python3
"""
FCC Demo — Master Startup Script

Three modes, one script:
  1. Start   — spin up everything, wait for healthy, load KB
  2. Reset   — wipe all data and start fresh
  3. Shutdown — stop everything

Usage:
    python start_demo.py
"""

import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# ── Config ──────────────────────────────────────────────────────────
LAB_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICES = [
    {"name": "Demo UI",        "url": "http://localhost:8080",        "timeout": 30},
    {"name": "MCP Server",     "url": "http://localhost:3001/health", "timeout": 60},
    {"name": "ChromaDB",       "url": "http://localhost:8100/api/v1/heartbeat", "timeout": 60},
    {"name": "Problem Server", "url": "http://localhost:9100/health", "timeout": 60},
    {"name": "n8n",            "url": "http://localhost:5678/healthz", "timeout": 90},
]

# ── Colors ──────────────────────────────────────────────────────────
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
DIM = "\033[2m"
RESET = "\033[0m"


def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}")


def log_step(step, msg):
    print(f"\n{BOLD}{CYAN}[{step}]{RESET} {BOLD}{msg}{RESET}")


def run(cmd, check=True, capture=False):
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
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        return 200 <= resp.status < 400
    except Exception:
        return False


def wait_for_service(name, url, timeout_secs):
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
    result = run("docker info > /dev/null 2>&1", check=False, capture=True)
    if result.returncode != 0:
        log("Docker is not running. Start Docker and try again.", RED)
        sys.exit(1)


def check_env():
    env_path = os.path.join(LAB_DIR, ".env")
    if not os.path.exists(env_path):
        example = os.path.join(LAB_DIR, ".env.example")
        if os.path.exists(example):
            import shutil
            shutil.copy2(example, env_path)
            log(f"  {YELLOW}⚠  Created .env from .env.example — set your DeepSeek API key{RESET}")
        else:
            log(f"  {YELLOW}⚠  No .env file found — n8n agent workflows will need a DeepSeek API key configured{RESET}")
        return

    with open(env_path) as f:
        content = f.read()
    if "your-deepseek-api-key-here" in content:
        log(f"  {YELLOW}⚠  DeepSeek API key not set in .env — Demo UI works standalone, but n8n agent workflows need the key{RESET}")


def seed_knowledge_base():
    """Seed ChromaDB with KB articles via the MCP Server container."""
    result = run(
        "docker compose exec -T mcp-server python /app/kb-data/seed_kb.py chromadb 8000",
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.strip():
                print(f"  {line.strip()}")
        return True
    else:
        log(f"  {RED}Failed to seed KB:{RESET}\n{result.stderr}", RED)
        return False


# ── Commands ────────────────────────────────────────────────────────

def cmd_start(fresh=False):
    log(f"\n{BOLD}{'═' * 54}{RESET}")
    log(f"{BOLD}  {'🔄' if fresh else '▶'}  FCC Demo — Agentic AI Live Demo{RESET}")
    log(f"{BOLD}{'═' * 54}{RESET}")

    # Pre-flight
    log_step("1/5", "Pre-flight checks")
    check_docker()
    log(f"  {GREEN}✓{RESET} Docker is running")
    check_env()

    # Fresh start?
    if fresh:
        log_step("2/5", "Tearing down existing demo (fresh start)")
        run("docker compose down -v", check=False)
        log(f"  {GREEN}✓{RESET} Clean slate")
    else:
        log_step("2/5", "Checking for existing containers")
        result = run("docker compose ps -q", capture=True)
        if result.stdout.strip():
            log(f"  {YELLOW}↻{RESET} Existing containers found — restarting")
            run("docker compose down", check=False)

    # Build & start
    log_step("3/5", "Building and starting containers")
    run("docker compose up -d --build")
    log(f"  {GREEN}✓{RESET} Containers started")

    # Wait for services
    log_step("4/5", "Waiting for services to become healthy")
    all_up = True
    for svc in SERVICES:
        if not wait_for_service(svc["name"], svc["url"], svc["timeout"]):
            all_up = False

    if not all_up:
        log(f"\n{YELLOW}Some services didn't come up in time — they may still be starting.{RESET}")

    # Seed knowledge base
    log_step("5/5", "Seeding knowledge base")
    seed_knowledge_base()

    # Summary
    print(f"""
{BOLD}{'═' * 54}{RESET}
{BOLD}  ✅  FCC Demo is ready!{RESET}
{BOLD}{'═' * 54}{RESET}

{BOLD}Services:{RESET}
  Demo UI        →  {CYAN}http://localhost:8080{RESET}
  n8n            →  {CYAN}http://localhost:5678{RESET}
  MCP Server     →  {CYAN}http://localhost:3001/health{RESET}
  ChromaDB       →  {CYAN}http://localhost:8100{RESET}
  Problem Server →  {CYAN}http://localhost:9100/health{RESET}

{BOLD}Demo Modes:{RESET}
  {MAGENTA}Standalone{RESET} — Demo UI calls MCP server directly (works without DeepSeek key)
  {MAGENTA}Full Agent{RESET} — n8n workflows with DeepSeek (needs API key + workflow import)

{BOLD}To use Full Agent mode:{RESET}
  1. Open n8n at http://localhost:5678
  2. Import workflows from n8n-workflows/
  3. Create a DeepSeek API credential (paste your API key)
  4. Attach the credential to the DeepSeek Chat Model node
  5. Activate the workflows
""")


def cmd_stop():
    log_step("1/1", "Shutting down demo")
    run("docker compose down")
    log(f"  {GREEN}✓{RESET} All containers stopped")


def cmd_status():
    log(f"\n{BOLD}FCC Demo — Service Status{RESET}\n")
    for svc in SERVICES:
        if check_url(svc["url"]):
            log(f"  {GREEN}✓{RESET} {svc['name']:20s} UP")
        else:
            log(f"  {RED}✗{RESET} {svc['name']:20s} DOWN")
    print()


def cmd_logs():
    log(f"\n{BOLD}Tailing logs (Ctrl+C to stop)...{RESET}\n")
    try:
        subprocess.run("docker compose logs -f --tail=50", cwd=LAB_DIR, shell=True)
    except KeyboardInterrupt:
        print()


def show_menu():
    print(f"""
{BOLD}{'═' * 54}
  FCC Demo — Agentic AI Live Demo
{'═' * 54}{RESET}

  {CYAN}1{RESET}  Start demo
  {CYAN}2{RESET}  Start demo (fresh — wipe everything, clean slate)
  {CYAN}3{RESET}  Stop demo
  {CYAN}4{RESET}  Check status
  {CYAN}5{RESET}  Tail container logs
  {CYAN}0{RESET}  Exit
""")
    try:
        choice = input(f"  {BOLD}Select an option: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return "0"
    return choice


# ── Main ────────────────────────────────────────────────────────────
def main():
    while True:
        choice = show_menu()

        if choice == "1":
            cmd_start(fresh=False)
        elif choice == "2":
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
            cmd_logs()
        elif choice == "0":
            log(f"\n  {GREEN}See you at the FCC Forum.{RESET}\n")
            break
        else:
            log(f"  Invalid option: {choice}", RED)

        try:
            input(f"\n  {BOLD}Press Enter to continue...{RESET}")
        except (KeyboardInterrupt, EOFError):
            print()
            break


if __name__ == "__main__":
    main()
