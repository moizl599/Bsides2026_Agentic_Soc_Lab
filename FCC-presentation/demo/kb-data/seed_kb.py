#!/usr/bin/env python3
"""
Seed ChromaDB with knowledge base articles for both demos.
Collections: security_kb (email analyst), helpdesk_kb (helpdesk triage)
"""

import sys
import time
import chromadb

CHROMADB_HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
CHROMADB_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8100

# ── Security KB (for Email Analyst) ────────────────────────────────
SECURITY_KB = [
    {
        "id": "sec-001",
        "doc": (
            "Phishing Email Indicators: Emails that fail SPF, DKIM, and DMARC "
            "authentication checks are highly likely to be spoofed. Key indicators "
            "include: sender domain does not match Reply-To or Return-Path, use of "
            "urgency language ('verify now', 'account suspended'), display name "
            "mismatch (shows 'Microsoft' but email is from a different domain), "
            "recently registered domains (less than 30 days old), and use of "
            "suspicious TLDs (.xyz, .top, .click, .buzz)."
        ),
        "meta": {"category": "phishing", "severity": "high", "type": "indicator"},
    },
    {
        "id": "sec-002",
        "doc": (
            "Credential Harvesting Attack Pattern: Attackers send emails impersonating "
            "trusted services (Microsoft 365, Google Workspace, banking) with links to "
            "fake login pages. The URLs often use typosquatting (microsft, paypa1, amaz0n) "
            "and may include the target's username in the URL parameters to pre-fill "
            "the form. The landing pages are pixel-perfect copies hosted on compromised "
            "or disposable infrastructure. Response: Block domain at email gateway, "
            "check web proxy logs for any visits, force password reset for any "
            "users who clicked."
        ),
        "meta": {"category": "credential-theft", "severity": "critical", "type": "playbook"},
    },
    {
        "id": "sec-003",
        "doc": (
            "Safe Email Characteristics: Legitimate emails typically pass SPF, DKIM, "
            "and DMARC authentication. The sender domain matches the organization's "
            "known domain. Message-ID format is consistent with the claimed mail "
            "system. No urgency language pressuring immediate action. Links point "
            "to the organization's actual domain. X-Mailer matches expected software "
            "(e.g., Microsoft Outlook for a company using O365)."
        ),
        "meta": {"category": "legitimate", "severity": "info", "type": "indicator"},
    },
    {
        "id": "sec-004",
        "doc": (
            "Business Email Compromise (BEC): Unlike mass phishing, BEC targets "
            "specific individuals (CFO, finance team) with emails that appear to come "
            "from executives or trusted partners. BEC emails may pass authentication "
            "if the attacker compromised an actual account. Look for: unusual payment "
            "requests, changes to banking details, pressure to bypass normal approval "
            "processes, emails sent outside business hours."
        ),
        "meta": {"category": "bec", "severity": "critical", "type": "playbook"},
    },
    {
        "id": "sec-005",
        "doc": (
            "URL Analysis Best Practices: Check for IP addresses instead of domain "
            "names. Look for URL shorteners that hide the true destination. Check "
            "for excessive subdomains (login.secure.verify.bank.attacker.com). "
            "Look for homoglyph attacks (using 'rn' to mimic 'm', or Cyrillic "
            "characters that look like Latin). Check if the domain was recently "
            "registered. Verify SSL certificate matches the claimed organization."
        ),
        "meta": {"category": "url-analysis", "severity": "medium", "type": "procedure"},
    },
    {
        "id": "sec-006",
        "doc": (
            "Malware Delivery via Email: Attachments with double extensions "
            "(invoice.pdf.exe), password-protected ZIP files with the password in "
            "the email body, Office documents with macros, HTML attachments that "
            "redirect to download sites. Common payloads: information stealers, "
            "ransomware droppers, remote access trojans. Response: quarantine the "
            "email, block the sender, scan endpoints for indicators of compromise."
        ),
        "meta": {"category": "malware", "severity": "critical", "type": "playbook"},
    },
]

# ── Helpdesk KB (for IT Helpdesk Triage) ───────────────────────────
HELPDESK_KB = [
    {
        "id": "hd-001",
        "doc": (
            "File Share Access Issues (SMB/CIFS): When users cannot access network "
            "shares, check: 1) Is the file server (fs01.internal) responding to ping? "
            "2) Is the SMB service running? Check with 'systemctl status smbd' on Linux "
            "or 'Get-Service LanmanServer' on Windows. 3) Check if the user's mapped "
            "drive letter is still connected. 4) Verify DNS resolution for the server "
            "hostname. 5) Check if the file share service has crashed (look for segfaults "
            "in /var/log/syslog). Common fix: restart the SMB service and verify shares "
            "are re-exported. If recurring crashes, check for memory issues or disk space."
        ),
        "meta": {"category": "file-services", "priority": "high", "kb_id": "KB-1001"},
    },
    {
        "id": "hd-002",
        "doc": (
            "High CPU Usage on Servers: If a server shows sustained high CPU usage "
            "(>80%), identify the offending process with 'top' or 'htop'. Common "
            "causes: 1) Stuck batch job or backup process that didn't complete. "
            "2) Runaway indexing service. 3) Malware/cryptominer. Action: identify "
            "the process, check if it's a legitimate scheduled task that's overrun "
            "its window, and either wait for completion or kill the process. If it's "
            "a backup-indexer, check the backup logs for errors and restart the job "
            "during off-hours."
        ),
        "meta": {"category": "server-performance", "priority": "medium", "kb_id": "KB-1002"},
    },
    {
        "id": "hd-003",
        "doc": (
            "VPN Connection Issues: If users report VPN timeouts or disconnections: "
            "1) Check VPN gateway health and capacity. 2) Verify user's VPN client "
            "version is current. 3) Check for split-tunnel vs full-tunnel config. "
            "4) Verify user's network allows VPN traffic (some public WiFi blocks "
            "IPSec/SSL VPN ports). 5) Check if MFA token is expired. Common fix: "
            "disconnect and reconnect VPN, clear VPN cache, or update client."
        ),
        "meta": {"category": "remote-access", "priority": "medium", "kb_id": "KB-1003"},
    },
    {
        "id": "hd-004",
        "doc": (
            "Password Reset Procedures: For standard users, direct to self-service "
            "portal at https://passwords.company.com. For VIP/executive users, verify "
            "identity through secondary channel (phone call to known number) before "
            "processing manual reset. For service accounts, follow change management "
            "process — update all dependent services after reset. Password must meet "
            "complexity requirements: 14+ characters, mixed case, number, special char."
        ),
        "meta": {"category": "access-management", "priority": "low", "kb_id": "KB-1004"},
    },
    {
        "id": "hd-005",
        "doc": (
            "Printer/Scanning Issues: Most printing issues are resolved by: "
            "1) Clearing the print queue (restart Print Spooler service). "
            "2) Reinstalling the printer driver. 3) Checking printer's IP address "
            "hasn't changed (use DHCP reservation or static IP). 4) For scanning, "
            "verify the scan-to-email SMTP relay is configured. Route to Desktop "
            "Support team for hardware issues (paper jams, toner, maintenance kits)."
        ),
        "meta": {"category": "peripherals", "priority": "low", "kb_id": "KB-1005"},
    },
    {
        "id": "hd-006",
        "doc": (
            "Multi-User Impact Assessment: When a ticket mentions 'other people are "
            "having the same issue' or 'our whole team can't access', this indicates "
            "a potential widespread outage rather than an individual user problem. "
            "Escalation criteria: 1) Upgrade priority to High if 3+ users affected. "
            "2) Check monitoring dashboards for infrastructure alerts. 3) Create an "
            "incident ticket if confirmed service outage. 4) Notify affected teams "
            "via status page or Slack. Route to Infrastructure Operations, not "
            "Desktop Support."
        ),
        "meta": {"category": "incident-management", "priority": "high", "kb_id": "KB-1006"},
    },
    {
        "id": "hd-007",
        "doc": (
            "Disk Space Alerts: When servers report disk usage >80%: 1) Identify "
            "largest directories with 'du -sh /*'. 2) Common culprits: old log files "
            "in /var/log, temp files in /tmp, failed backup staging in /var/backup. "
            "3) Check if log rotation is working. 4) Clear old backup staging files "
            "after verifying successful backup completion. 5) If /var/log is full, "
            "services may fail to write logs and could crash."
        ),
        "meta": {"category": "server-maintenance", "priority": "medium", "kb_id": "KB-1007"},
    },
]


def main():
    print(f"Connecting to ChromaDB at {CHROMADB_HOST}:{CHROMADB_PORT}...")

    # Retry connection
    client = None
    for attempt in range(30):
        try:
            client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
            client.heartbeat()
            print("  Connected to ChromaDB")
            break
        except Exception as e:
            if attempt < 29:
                print(f"  Waiting for ChromaDB... ({attempt + 1}/30)")
                time.sleep(2)
            else:
                print(f"  ERROR: Could not connect to ChromaDB: {e}")
                sys.exit(1)

    # Seed Security KB
    print("\nSeeding security_kb collection...")
    try:
        client.delete_collection("security_kb")
    except Exception:
        pass
    sec_col = client.create_collection(name="security_kb")
    sec_col.add(
        ids=[item["id"] for item in SECURITY_KB],
        documents=[item["doc"] for item in SECURITY_KB],
        metadatas=[item["meta"] for item in SECURITY_KB],
    )
    print(f"  ✓ Loaded {len(SECURITY_KB)} articles into security_kb")

    # Seed Helpdesk KB
    print("Seeding helpdesk_kb collection...")
    try:
        client.delete_collection("helpdesk_kb")
    except Exception:
        pass
    hd_col = client.create_collection(name="helpdesk_kb")
    hd_col.add(
        ids=[item["id"] for item in HELPDESK_KB],
        documents=[item["doc"] for item in HELPDESK_KB],
        metadatas=[item["meta"] for item in HELPDESK_KB],
    )
    print(f"  ✓ Loaded {len(HELPDESK_KB)} articles into helpdesk_kb")

    print(f"\n✓ Knowledge base seeded successfully ({len(SECURITY_KB) + len(HELPDESK_KB)} total articles)")


if __name__ == "__main__":
    main()
