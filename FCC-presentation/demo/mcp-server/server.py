"""
FCC Demo — MCP Tool Server
Central tool gateway for both demos (Email Analyst + Helpdesk Triage).
n8n agent workflows call these REST endpoints as tools.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ── Config ──────────────────────────────────────────────────────────
CHROMADB_HOST = os.environ.get("CHROMADB_HOST", "chromadb")
CHROMADB_PORT = int(os.environ.get("CHROMADB_PORT", "8000"))
PROBLEM_SERVER_HOST = os.environ.get("PROBLEM_SERVER_HOST", "problem-server")
PROBLEM_SERVER_PORT = int(os.environ.get("PROBLEM_SERVER_PORT", "9100"))
PORT = int(os.environ.get("MCP_PORT", "3001"))

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = app.logger


def get_request_data():
    """Parse request body regardless of Content-Type.
    n8n toolHttpRequest nodes may send JSON without the header,
    or as form-encoded, or as raw text wrapping JSON."""
    # Capture raw body first (caches it for subsequent reads)
    raw = request.get_data(as_text=True)
    log.info(f"→ {request.method} {request.path} | CT: {request.content_type} | Body ({len(raw)} chars): {raw[:500]}")

    # Try JSON parse (force ignores Content-Type, silent returns None on failure)
    data = request.get_json(force=True, silent=True)
    if isinstance(data, dict) and data:
        return data
    # If get_json returned a string (double-encoded JSON), try parsing it
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    # Try form data
    if request.form:
        return dict(request.form)
    # Try raw body as JSON string directly
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    # Try query parameters as last resort
    if request.args:
        return dict(request.args)
    log.warning(f"  Could not parse any data from request to {request.path}")
    return {}


def smart_get(data, primary_key, alt_keys=None, pattern=None):
    """Try primary_key, then alt_keys, then find any value matching regex pattern,
    then fall back to first non-empty string value."""
    # Try primary key
    val = data.get(primary_key, "")
    if isinstance(val, str):
        val = val.strip()
    if val:
        return val
    # Try alternative key names
    for k in (alt_keys or []):
        val = data.get(k, "")
        if isinstance(val, str):
            val = val.strip()
        if val:
            log.info(f"  smart_get: found '{primary_key}' under alt key '{k}'")
            return val
    # Scan all values for pattern match
    if pattern:
        for k, v in data.items():
            if isinstance(v, str) and v.strip() and re.search(pattern, v.strip()):
                log.info(f"  smart_get: found '{primary_key}' via pattern in key '{k}'")
                return v.strip()
    # Scan keys — model may have put the value AS the key name (e.g. {"https://...": ""})
    if pattern:
        for k in data.keys():
            if isinstance(k, str) and re.search(pattern, k):
                log.info(f"  smart_get: found '{primary_key}' as key name '{k}'")
                return k
    # Fall back to first non-empty string value
    for k, v in data.items():
        if isinstance(v, str) and v.strip() and k != "collection":
            log.info(f"  smart_get: fallback — using key '{k}' for '{primary_key}'")
            return v.strip()
    return ""


# ── ChromaDB client (lazy init) ────────────────────────────────────
_chroma_client = None

def get_chroma():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        _chroma_client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    return _chroma_client

# ════════════════════════════════════════════════════════════════════
#  DEMO A — AI Email Analyst Tools
# ════════════════════════════════════════════════════════════════════

@app.route("/api/tools/analyze-headers", methods=["POST"])
def analyze_headers():
    """Parse email headers and extract security-relevant fields."""
    data = get_request_data()
    raw_email = smart_get(data, "raw_email", alt_keys=["email", "raw", "message", "text", "content", "body"])
    if not raw_email:
        return jsonify({"error": "raw_email is required"}), 400

    # Extract headers
    headers = {}
    spf = "none"
    dkim = "none"
    dmarc = "none"
    return_path = ""
    from_addr = ""
    reply_to = ""
    received_chain = []
    x_mailer = ""
    message_id = ""
    subject_line = ""

    for line in raw_email.split("\n"):
        line = line.strip()
        lower = line.lower()

        if lower.startswith("from:"):
            from_addr = line.split(":", 1)[1].strip()
        elif lower.startswith("return-path:"):
            return_path = line.split(":", 1)[1].strip()
        elif lower.startswith("reply-to:"):
            reply_to = line.split(":", 1)[1].strip()
        elif lower.startswith("received:"):
            received_chain.append(line.split(":", 1)[1].strip())
        elif lower.startswith("x-mailer:"):
            x_mailer = line.split(":", 1)[1].strip()
        elif lower.startswith("message-id:"):
            message_id = line.split(":", 1)[1].strip()
        elif lower.startswith("subject:"):
            subject_line = line.split(":", 1)[1].strip()
        elif "spf=" in lower:
            m = re.search(r"spf=(\w+)", lower)
            if m:
                spf = m.group(1)
        elif "dkim=" in lower:
            m = re.search(r"dkim=(\w+)", lower)
            if m:
                dkim = m.group(1)
        elif "dmarc=" in lower:
            m = re.search(r"dmarc=(\w+)", lower)
            if m:
                dmarc = m.group(1)

    # Extract domain from From address
    domain_match = re.search(r"@([\w.-]+)", from_addr)
    sender_domain = domain_match.group(1) if domain_match else "unknown"

    # Check for display name mismatch
    display_mismatch = False
    if "<" in from_addr and "@" in from_addr:
        display_part = from_addr.split("<")[0].strip().strip('"')
        email_part = re.search(r"<(.+?)>", from_addr)
        if email_part and display_part:
            email_domain = email_part.group(1).split("@")[1] if "@" in email_part.group(1) else ""
            if display_part.lower().replace(" ", "") != email_domain.lower().split(".")[0]:
                display_mismatch = True

    # Determine urgency language in subject
    urgency_words = ["urgent", "immediately", "action required", "verify now",
                     "suspended", "expire", "locked", "confirm", "unauthorized"]
    has_urgency = any(w in subject_line.lower() for w in urgency_words)

    result = {
        "sender": from_addr,
        "sender_domain": sender_domain,
        "return_path": return_path,
        "reply_to": reply_to,
        "subject": subject_line,
        "message_id": message_id,
        "x_mailer": x_mailer,
        "received_hops": len(received_chain),
        "authentication": {
            "spf": spf,
            "dkim": dkim,
            "dmarc": dmarc
        },
        "red_flags": {
            "display_name_mismatch": display_mismatch,
            "return_path_mismatch": return_path and sender_domain not in return_path,
            "reply_to_mismatch": reply_to and sender_domain not in reply_to,
            "urgency_language": has_urgency,
            "auth_failures": spf == "fail" or dkim == "fail" or dmarc == "fail"
        }
    }

    log.info(f"Header analysis: domain={sender_domain}, SPF={spf}, DKIM={dkim}")
    return jsonify(result)


@app.route("/api/tools/check-domain", methods=["POST"])
def check_domain():
    """Check domain reputation against known threat indicators."""
    data = get_request_data()
    domain = smart_get(data, "domain", alt_keys=["sender_domain", "host", "hostname"],
                       pattern=r'^[a-zA-Z0-9]([a-zA-Z0-9\-]*\.)+[a-zA-Z]{2,}$')
    if domain:
        domain = domain.strip().lower()
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    # Simulated threat intelligence — realistic but deterministic
    domain_hash = int(hashlib.md5(domain.encode()).hexdigest()[:8], 16)

    # Known malicious patterns
    suspicious_tlds = [".xyz", ".top", ".click", ".buzz", ".info", ".tk", ".ml", ".ga", ".cf"]
    has_suspicious_tld = any(domain.endswith(tld) for tld in suspicious_tlds)

    # Check for typosquatting patterns
    legitimate_domains = ["microsoft.com", "google.com", "apple.com", "amazon.com",
                          "paypal.com", "netflix.com", "linkedin.com", "dropbox.com"]
    typosquat_score = 0
    for legit in legitimate_domains:
        legit_base = legit.split(".")[0]
        domain_base = domain.split(".")[0]
        # Simple Levenshtein-like check
        if legit_base != domain_base and len(set(legit_base) & set(domain_base)) > len(legit_base) * 0.6:
            if len(domain_base) - len(legit_base) <= 3:
                typosquat_score = max(typosquat_score, 70)

    # Known bad domains for demo
    known_malicious = {
        "secure-microsft-verify.xyz": {"risk": "critical", "category": "phishing", "first_seen": "2026-11-10"},
        "login-paypa1.com": {"risk": "critical", "category": "credential-theft", "first_seen": "2026-11-08"},
        "amaz0n-security.click": {"risk": "high", "category": "phishing", "first_seen": "2026-11-12"},
        "dropbox-share-file.top": {"risk": "high", "category": "malware-delivery", "first_seen": "2026-11-09"},
        "update-chrome-now.buzz": {"risk": "critical", "category": "drive-by-download", "first_seen": "2026-11-11"},
    }

    if domain in known_malicious:
        info = known_malicious[domain]
        result = {
            "domain": domain,
            "risk_level": info["risk"],
            "risk_score": 95,
            "category": info["category"],
            "first_seen": info["first_seen"],
            "registrar": "NameSilo LLC",
            "registration_date": info["first_seen"],
            "country": "RU",
            "whois_privacy": True,
            "known_malicious": True,
            "threat_feeds_matched": ["PhishTank", "OpenPhish", "URLhaus"],
        }
    elif has_suspicious_tld:
        result = {
            "domain": domain,
            "risk_level": "high",
            "risk_score": 75,
            "category": "suspicious-tld",
            "registrar": "Unknown",
            "registration_date": "2026-11-01",
            "country": "Unknown",
            "whois_privacy": True,
            "known_malicious": False,
            "threat_feeds_matched": [],
            "notes": f"Domain uses suspicious TLD. Recently registered."
        }
    elif domain in ["microsoft.com", "google.com", "apple.com", "amazon.com", "paypal.com"]:
        result = {
            "domain": domain,
            "risk_level": "safe",
            "risk_score": 5,
            "category": "legitimate",
            "registrar": "MarkMonitor Inc.",
            "registration_date": "1991-05-02",
            "country": "US",
            "whois_privacy": False,
            "known_malicious": False,
            "threat_feeds_matched": [],
        }
    else:
        risk_score = (domain_hash % 40) + (30 if has_suspicious_tld else 0) + typosquat_score
        risk_score = min(risk_score, 100)
        result = {
            "domain": domain,
            "risk_level": "critical" if risk_score > 80 else "high" if risk_score > 60 else "medium" if risk_score > 40 else "low",
            "risk_score": risk_score,
            "category": "unknown",
            "registrar": "Various",
            "known_malicious": False,
            "threat_feeds_matched": [],
        }

    log.info(f"Domain check: {domain} → {result['risk_level']} ({result['risk_score']})")
    return jsonify(result)


@app.route("/api/tools/check-url", methods=["POST"])
def check_url():
    """Analyze URL for phishing/malware indicators."""
    data = get_request_data()
    url = smart_get(data, "target_url", alt_keys=["url", "link", "check_url", "query"],
                    pattern=r'https?://')
    if not url:
        return jsonify({"error": "url is required"}), 400

    indicators = []
    risk_score = 0

    # Check for IP address in URL
    if re.search(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", url):
        indicators.append("IP address used instead of domain name")
        risk_score += 30

    # Check for URL shorteners
    shorteners = ["bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd"]
    if any(s in url.lower() for s in shorteners):
        indicators.append("URL shortener detected — hides true destination")
        risk_score += 20

    # Check for suspicious patterns
    if "@" in url.split("//")[-1].split("/")[0]:
        indicators.append("@ symbol in URL — may redirect to attacker-controlled site")
        risk_score += 40

    if url.count(".") > 4:
        indicators.append("Excessive subdomains — common in phishing URLs")
        risk_score += 15

    if any(k in url.lower() for k in ["login", "verify", "secure", "account", "update", "confirm"]):
        indicators.append("Credential-harvesting keywords in URL path")
        risk_score += 20

    if any(k in url.lower() for k in [".exe", ".zip", ".scr", ".bat", ".ps1", ".msi"]):
        indicators.append("Executable file download detected")
        risk_score += 35

    # Check for HTTPS
    if not url.startswith("https://"):
        indicators.append("Not using HTTPS — data sent in cleartext")
        risk_score += 10

    # Check for data URI
    if url.startswith("data:"):
        indicators.append("Data URI — embedded content, possible encoded payload")
        risk_score += 40

    # Known malicious URLs for demo
    known_bad_urls = [
        "secure-microsft-verify.xyz/login/verify-account",
        "login-paypa1.com/signin/confirm",
        "amaz0n-security.click/verify",
    ]
    if any(bad in url for bad in known_bad_urls):
        indicators.append("URL matches known phishing URL in threat intelligence feeds")
        risk_score = 95

    risk_score = min(risk_score, 100)
    result = {
        "url": url,
        "risk_level": "critical" if risk_score > 80 else "high" if risk_score > 60 else "medium" if risk_score > 40 else "low",
        "risk_score": risk_score,
        "indicators": indicators,
        "safe_to_visit": risk_score < 30,
        "recommendation": "BLOCK — do not click" if risk_score > 60 else "CAUTION — verify before clicking" if risk_score > 30 else "Likely safe",
    }

    log.info(f"URL check: {url[:60]}... → {result['risk_level']} ({risk_score})")
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════
#  DEMO E — IT Helpdesk Triage Tools
# ════════════════════════════════════════════════════════════════════

@app.route("/api/tools/check-service-status", methods=["POST"])
def check_service_status():
    """Pull real metrics from the Problem Server container."""
    data = get_request_data()
    server_name = smart_get(data, "server", alt_keys=["hostname", "host", "server_name", "target"]) or "fs01.internal"
    metric_type = data.get("metric", "") or data.get("metric_type", "") or data.get("type", "") or "all"

    base_url = f"http://{PROBLEM_SERVER_HOST}:{PROBLEM_SERVER_PORT}"

    try:
        if metric_type == "all":
            endpoints = ["cpu", "memory", "disk", "services", "logs"]
        else:
            endpoints = [metric_type]

        results = {}
        for ep in endpoints:
            resp = requests.get(f"{base_url}/metrics/{ep}", timeout=5)
            if resp.ok:
                results[ep] = resp.json()
            else:
                results[ep] = {"error": f"Failed to fetch {ep} metrics"}

        result = {
            "server": server_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "degraded",
            "metrics": results
        }

        # Determine overall status from metrics
        issues = []
        cpu_data = results.get("cpu", {})
        if cpu_data.get("usage_percent", 0) > 80:
            issues.append(f"High CPU: {cpu_data.get('usage_percent')}%")
        mem_data = results.get("memory", {})
        if mem_data.get("usage_percent", 0) > 85:
            issues.append(f"High memory: {mem_data.get('usage_percent')}%")
        svc_data = results.get("services", {})
        for svc in svc_data.get("services", []):
            if svc.get("status") != "running":
                issues.append(f"Service down: {svc['name']} ({svc.get('status')})")

        result["issues_detected"] = issues
        result["status"] = "critical" if len(issues) > 2 else "degraded" if issues else "healthy"

        log.info(f"Service status: {server_name} → {result['status']} ({len(issues)} issues)")
        return jsonify(result)

    except requests.exceptions.ConnectionError:
        return jsonify({
            "server": server_name,
            "status": "unreachable",
            "error": "Cannot connect to monitoring agent",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


@app.route("/api/tools/lookup-asset", methods=["POST"])
def lookup_asset():
    """Look up user's assigned assets (simulated CMDB)."""
    data = get_request_data()
    username = smart_get(data, "username", alt_keys=["user", "name", "user_id", "userid"])
    if username:
        username = username.strip().lower()
    if not username:
        return jsonify({"error": "username is required"}), 400

    # Simulated asset database
    asset_db = {
        "jsmith": {
            "full_name": "John Smith",
            "department": "Finance",
            "title": "Senior Analyst",
            "location": "Toronto, ON",
            "devices": [
                {"type": "Laptop", "model": "Dell Latitude 5540", "os": "Windows 11", "hostname": "WS-FIN-042"},
                {"type": "Monitor", "model": "Dell U2722D", "asset_tag": "MON-2024-0891"},
            ],
            "network_drives": ["\\\\fs01.internal\\finance-shared", "\\\\fs01.internal\\reports"],
            "software": ["Microsoft 365", "SAP", "Power BI", "Slack"],
            "vip": False,
            "last_password_change": "2026-10-15",
        },
        "agarcia": {
            "full_name": "Ana Garcia",
            "department": "Engineering",
            "title": "DevOps Lead",
            "location": "Toronto, ON",
            "devices": [
                {"type": "Laptop", "model": "MacBook Pro 16\" M4", "os": "macOS 15", "hostname": "WS-ENG-108"},
                {"type": "Desktop", "model": "Mac Studio M4 Ultra", "os": "macOS 15", "hostname": "WS-ENG-109"},
            ],
            "network_drives": ["\\\\fs01.internal\\engineering"],
            "software": ["JetBrains Suite", "Docker Desktop", "VS Code", "Slack", "Jira"],
            "vip": False,
            "last_password_change": "2026-11-01",
        },
        "mbrown": {
            "full_name": "Michael Brown",
            "department": "Executive",
            "title": "CFO",
            "location": "Toronto, ON",
            "devices": [
                {"type": "Laptop", "model": "Lenovo ThinkPad X1 Carbon", "os": "Windows 11", "hostname": "WS-EXEC-003"},
                {"type": "Tablet", "model": "iPad Pro 13\"", "os": "iPadOS 19", "asset_tag": "TAB-2024-0012"},
            ],
            "network_drives": ["\\\\fs01.internal\\finance-shared", "\\\\fs01.internal\\executive"],
            "software": ["Microsoft 365", "SAP", "Bloomberg Terminal", "Slack"],
            "vip": True,
            "last_password_change": "2026-09-20",
        },
    }

    if username in asset_db:
        result = {"found": True, "user": asset_db[username]}
    else:
        result = {
            "found": False,
            "message": f"No asset record found for '{username}'",
            "suggestion": "Check username spelling or search the directory"
        }

    log.info(f"Asset lookup: {username} → {'found' if result['found'] else 'not found'}")
    return jsonify(result)


@app.route("/api/tools/update-ticket", methods=["POST"])
def update_ticket():
    """Update a helpdesk ticket with triage results."""
    data = get_request_data()
    ticket_id = data.get("ticket_id", "")

    # Support both nested triage object and flat keypair fields
    triage = data.get("triage", None)
    if not triage or not isinstance(triage, dict):
        # Build triage from flat fields (keypair body format)
        actions = [a for a in [data.get("action1"), data.get("action2"), data.get("action3")] if a]
        triage = {
            "category": data.get("category", "unknown"),
            "priority": data.get("priority", "medium"),
            "assigned_team": data.get("assigned_team", "General IT"),
            "summary": data.get("summary", ""),
            "recommended_actions": actions,
        }

    if not ticket_id:
        return jsonify({"error": "ticket_id is required"}), 400

    # Simulate ticket update
    result = {
        "ticket_id": ticket_id,
        "status": "triaged",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "triage_applied": {
            "category": triage.get("category", "unknown"),
            "priority": triage.get("priority", "medium"),
            "assigned_team": triage.get("assigned_team", "General IT"),
            "summary": triage.get("summary", ""),
            "recommended_actions": triage.get("recommended_actions", []),
        },
        "sla_target": "4 hours" if triage.get("priority") == "high" else "8 hours" if triage.get("priority") == "medium" else "24 hours",
    }

    log.info(f"Ticket update: {ticket_id} → {result['triage_applied']['priority']} / {result['triage_applied']['category']}")
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════
#  SHARED — Knowledge Base Query (ChromaDB)
# ════════════════════════════════════════════════════════════════════

@app.route("/api/tools/query-kb", methods=["POST"])
def query_kb():
    """Query the ChromaDB knowledge base."""
    data = get_request_data()
    query_text = smart_get(data, "query", alt_keys=["search", "q", "text", "search_query"])
    collection_name = data.get("collection", "security_kb")
    n_results = data.get("n_results", 3)

    if not query_text:
        return jsonify({"error": "query is required"}), 400

    try:
        client = get_chroma()
        collection = client.get_collection(name=collection_name)
        results = collection.query(query_texts=[query_text], n_results=n_results)

        docs = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            dist = results["distances"][0][i] if results["distances"] else None
            docs.append({
                "content": doc,
                "metadata": meta,
                "relevance_score": round(1.0 - (dist / 2.0), 3) if dist is not None else None,
            })

        result = {
            "query": query_text,
            "collection": collection_name,
            "results_count": len(docs),
            "results": docs,
        }

        log.info(f"KB query: '{query_text[:40]}...' → {len(docs)} results from {collection_name}")
        return jsonify(result)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "hint": "Knowledge base may not be loaded yet. Run the seed script."
        }), 500


# ════════════════════════════════════════════════════════════════════
#  Health & Status
# ════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """Health check for Docker."""
    checks = {"mcp_server": "ok"}

    # Check ChromaDB
    try:
        client = get_chroma()
        client.heartbeat()
        checks["chromadb"] = "connected"
    except Exception:
        checks["chromadb"] = "disconnected"

    # Check Problem Server
    try:
        resp = requests.get(f"http://{PROBLEM_SERVER_HOST}:{PROBLEM_SERVER_PORT}/health", timeout=2)
        checks["problem_server"] = "connected" if resp.ok else "unhealthy"
    except Exception:
        checks["problem_server"] = "disconnected"

    all_ok = all(v in ("ok", "connected") for v in checks.values())
    return jsonify({"status": "ok" if all_ok else "degraded", "checks": checks, "version": "1.0.0"})


@app.route("/api/tools", methods=["GET"])
def list_tools():
    """List available tools for the agent."""
    tools = [
        {"name": "analyze-headers", "method": "POST", "path": "/api/tools/analyze-headers",
         "description": "Parse email headers and extract security-relevant fields"},
        {"name": "check-domain", "method": "POST", "path": "/api/tools/check-domain",
         "description": "Check domain reputation against threat intelligence"},
        {"name": "check-url", "method": "POST", "path": "/api/tools/check-url",
         "description": "Analyze URL for phishing/malware indicators"},
        {"name": "query-kb", "method": "POST", "path": "/api/tools/query-kb",
         "description": "Query the security knowledge base"},
        {"name": "check-service-status", "method": "POST", "path": "/api/tools/check-service-status",
         "description": "Pull real metrics from monitored servers"},
        {"name": "lookup-asset", "method": "POST", "path": "/api/tools/lookup-asset",
         "description": "Look up user's assigned IT assets"},
        {"name": "update-ticket", "method": "POST", "path": "/api/tools/update-ticket",
         "description": "Update helpdesk ticket with triage results"},
    ]
    return jsonify({"tools": tools, "count": len(tools)})


if __name__ == "__main__":
    log.info(f"FCC Demo MCP Server starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
