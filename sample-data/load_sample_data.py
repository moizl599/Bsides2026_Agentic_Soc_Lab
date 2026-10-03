#!/usr/bin/env python3
"""
Load realistic sample auth logs into Splunk + create an alert in TheHive.

Generates:
  - 30 days of normal login history for user "jsmith"
  - An impossible-travel scenario: Toronto login then Singapore 20 min later
  - An alert document in the Splunk "soc_alerts" index
  - A matching alert in TheHive (so the demo starts from case management)
  - Some noise logins from other users for realism

Usage:
  python load_sample_data.py [--splunk-url https://localhost:8089] [--hec-url http://localhost:8088] [--thehive-url http://localhost:9000]

No pip dependencies — uses plain HTTP.
"""

import argparse
import json
import random
import ssl
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ---- Config ----
TARGET_USER = "jsmith"
NORMAL_IPS = [
    {"ip": "203.0.113.10", "city": "Toronto", "country": "CA", "lat": 43.65, "lon": -79.38},
    {"ip": "203.0.113.11", "city": "Toronto", "country": "CA", "lat": 43.65, "lon": -79.38},
    {"ip": "198.51.100.5", "city": "New York", "country": "US", "lat": 40.71, "lon": -74.01},
]
ATTACK_IP = {"ip": "45.77.88.99", "city": "Singapore", "country": "SG", "lat": 1.35, "lon": 103.82}

OTHER_USERS = ["agarcia", "bwilson", "clee", "dkumar", "emartinez"]
OTHER_IPS = [
    {"ip": "192.0.2.20", "city": "London", "country": "GB", "lat": 51.51, "lon": -0.13},
    {"ip": "192.0.2.21", "city": "Berlin", "country": "DE", "lat": 52.52, "lon": 13.41},
    {"ip": "192.0.2.22", "city": "Chicago", "country": "US", "lat": 41.88, "lon": -87.63},
    {"ip": "192.0.2.23", "city": "Vancouver", "country": "CA", "lat": 49.28, "lon": -123.12},
    {"ip": "192.0.2.24", "city": "Mumbai", "country": "IN", "lat": 19.08, "lon": 72.88},
]

AUTH_INDEX = "auth_logs"
ALERT_INDEX = "soc_alerts"

NOW = datetime.now(timezone.utc)

# Splunk defaults
SPLUNK_USER = "admin"
SPLUNK_PASS = "BsidesLab1!"
HEC_TOKEN = "abcd1234-abcd-1234-abcd-1234abcd1234"

# TheHive defaults
THEHIVE_USER = "admin@thehive.local"
THEHIVE_PASS = "secret"

# Org-level SOC user (platform admin can't create alerts — needs org perms)
SOC_USER = "analyst@bsides-lab.local"
SOC_PASS = "thehive1234"

# SSL context that ignores self-signed certs (Splunk management port)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# ---- HTTP helpers ----
def splunk_rest(base_url, method, path, body=None, params=None):
    """Make an authenticated request to Splunk's REST API (port 8089)."""
    import base64
    url = f"{base_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    if isinstance(body, dict):
        data = urllib.parse.urlencode(body).encode()
        content_type = "application/x-www-form-urlencoded"
    elif isinstance(body, str):
        data = body.encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        data = body
        content_type = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", content_type)
    creds = base64.b64encode(f"{SPLUNK_USER}:{SPLUNK_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")

    try:
        resp = urllib.request.urlopen(req, timeout=30, context=SSL_CTX)
        raw = resp.read()
        try:
            return json.loads(raw), resp.status
        except json.JSONDecodeError:
            return {"raw": raw.decode()}, resp.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text), e.code
        except json.JSONDecodeError:
            return {"error": body_text}, e.code


def hec_send(hec_url, events):
    """Send events to Splunk via HTTP Event Collector (port 8088)."""
    # HEC accepts newline-delimited JSON
    lines = [json.dumps(e) for e in events]
    body = "\n".join(lines).encode()

    url = f"{hec_url}/services/collector/event"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Splunk {HEC_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        return result, resp.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text), e.code
        except json.JSONDecodeError:
            return {"error": body_text}, e.code


def thehive_request(base_url, method, path, body=None, user=None, password=None, api_key=None):
    """Make an authenticated HTTP request to TheHive 5."""
    import base64
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    else:
        u = user or THEHIVE_USER
        p = password or THEHIVE_PASS
        creds = base64.b64encode(f"{u}:{p}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read()
        if not raw or not raw.strip():
            return {}, resp.status
        return json.loads(raw), resp.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return json.loads(body_text), e.code
        except json.JSONDecodeError:
            return {"error": body_text}, e.code


def setup_thehive_user(thehive_url):
    """Create an org-level SOC user in TheHive with alert/case permissions.

    TheHive 5's platform admin org doesn't support case/alert operations.
    We create a 'bsides-lab' org, add a user with org-admin profile, and
    generate a Bearer API key.

    CRITICAL: The 'organisation' field in POST /api/v1/user requires the
    org's internal _id (e.g. '~4149304'), NOT the org name string. Passing
    the name is silently ignored and the user lands in the admin org.
    """
    import base64
    ORG_NAME = "bsides-lab"

    # Step 1: List orgs to find or create bsides-lab
    resp, status = thehive_request(
        thehive_url, "POST", "/api/v1/query",
        {"query": [{"_name": "listOrganisation"}]},
    )
    org_id = None
    if status < 300 and isinstance(resp, list):
        for o in resp:
            if o.get("name") == ORG_NAME:
                org_id = o.get("_id")
                break
        print(f"  Existing orgs: {[o.get('name') for o in resp]}")

    # Step 2: Create the org if it doesn't exist
    if not org_id:
        resp, status = thehive_request(
            thehive_url, "POST", "/api/v1/organisation",
            {"name": ORG_NAME, "description": "BSides Lab SOC"},
        )
        if status == 201:
            org_id = resp.get("_id")
            print(f"  Created organisation: {ORG_NAME} (id: {org_id})")
        else:
            print(f"  Create org failed HTTP {status}: {resp}")
            return False
    else:
        print(f"  Organisation exists: {ORG_NAME} (id: {org_id})")

    # Step 3: Check if SOC user already exists in the right org
    resp, status = thehive_request(
        thehive_url, "POST", "/api/v1/query",
        {"query": [
            {"_name": "listUser"},
            {"_name": "filter", "_eq": {"_field": "login", "_value": SOC_USER}},
        ]},
    )
    user_id = None
    if status < 300 and isinstance(resp, list) and resp:
        existing = resp[0]
        if existing.get("organisation") == ORG_NAME:
            user_id = existing.get("_id")
            print(f"  User already exists in {ORG_NAME}: {SOC_USER} ({user_id})")
        else:
            print(f"  User exists but in wrong org '{existing.get('organisation')}' — will create new")

    # Step 4: Create user if needed (with org INTERNAL ID, not name!)
    if not user_id:
        user_payload = {
            "login": SOC_USER,
            "name": "SOC Analyst",
            "profile": "org-admin",
            "organisation": org_id,
            "password": SOC_PASS,
        }
        resp, status = thehive_request(thehive_url, "POST", "/api/v1/user", user_payload)
        if status == 201:
            user_id = resp.get("_id")
            print(f"  Created user: {SOC_USER} in org '{ORG_NAME}' (id: {user_id})")
        else:
            print(f"  Create user failed HTTP {status}: {resp}")
            return False

    # Step 5: Set password (idempotent)
    resp, status = thehive_request(
        thehive_url, "POST", f"/api/v1/user/{user_id}/password/set",
        {"password": SOC_PASS},
    )
    print(f"  Password set: HTTP {status}")

    # Step 6: Generate API key
    admin_creds = base64.b64encode(f"{THEHIVE_USER}:{THEHIVE_PASS}".encode()).decode()
    url = f"{thehive_url}/api/v1/user/{user_id}/key/renew"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {admin_creds}")
    try:
        key_resp = urllib.request.urlopen(req, timeout=30)
        api_key = key_resp.read().decode().strip().strip('"')
        print(f"  API key generated: {api_key[:8]}...")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        print(f"  Could not generate API key (HTTP {e.code}): {body}")
        return False

    # Step 7: Verify Bearer auth works
    verify_url = f"{thehive_url}/api/v1/user/current"
    verify_req = urllib.request.Request(verify_url, method="GET")
    verify_req.add_header("Authorization", f"Bearer {api_key}")
    try:
        vresp = urllib.request.urlopen(verify_req, timeout=10)
        vdata = json.loads(vresp.read())
        print(f"  Verified: login={vdata.get('login')}, org={vdata.get('organisation')}")
    except urllib.error.HTTPError as ve:
        print(f"  WARNING: Bearer verify failed HTTP {ve.code} — key may not work")

    return api_key


DEVICES = [
    {"device_type": "desktop", "device_os": "Windows 10", "browser": "Chrome 125"},
    {"device_type": "desktop", "device_os": "macOS 14", "browser": "Safari 17"},
    {"device_type": "desktop", "device_os": "Windows 11", "browser": "Edge 125"},
    {"device_type": "laptop", "device_os": "macOS 15", "browser": "Chrome 126"},
    {"device_type": "mobile", "device_os": "iOS 17", "browser": "Safari Mobile"},
    {"device_type": "mobile", "device_os": "Android 14", "browser": "Chrome Mobile 125"},
    {"device_type": "tablet", "device_os": "iPadOS 17", "browser": "Safari Mobile"},
]

# jsmith's usual device — consistent for normal logins
JSMITH_DEVICE = {"device_type": "desktop", "device_os": "Windows 10", "browser": "Chrome 125"}

USER_AGENTS = {
    "Chrome 125": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Chrome 126": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Safari 17": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Edge 125": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Safari Mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Chrome Mobile 125": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
}


def make_login(user, ip_info, timestamp, outcome="success", session_id=None,
               device=None, auth_method=None, mfa_used=None):
    """Build a single auth-log event for Splunk.

    Field names use underscores (Splunk-friendly) instead of dots.
    Avoids 'source' as a field name since it's reserved in Splunk.
    """
    if device is None:
        device = JSMITH_DEVICE if user == TARGET_USER else random.choice(DEVICES)
    if auth_method is None:
        auth_method = "password"
    if mfa_used is None:
        mfa_used = True if auth_method == "password" else False

    browser = device["browser"]
    ua = USER_AGENTS.get(browser, USER_AGENTS["Chrome 125"])

    return {
        "time": timestamp.isoformat(),
        "event_category": "authentication",
        "event_type": "start",
        "event_outcome": outcome,
        "user_name": user,
        "src_ip": ip_info["ip"],
        "src_city": ip_info["city"],
        "src_country": ip_info["country"],
        "src_lat": ip_info["lat"],
        "src_lon": ip_info["lon"],
        "session_id": session_id or str(uuid.uuid4()),
        "device_type": device["device_type"],
        "device_os": device["device_os"],
        "browser": browser,
        "user_agent": ua,
        "auth_method": auth_method,
        "mfa_used": mfa_used,
    }


def generate_normal_history(days=30):
    """30 days of jsmith logging in 1-3 times/day from known IPs."""
    docs = []
    for d in range(days, 0, -1):
        day = NOW - timedelta(days=d)
        logins_today = random.randint(1, 3)
        for _ in range(logins_today):
            hour = random.randint(7, 22)
            minute = random.randint(0, 59)
            ts = day.replace(hour=hour, minute=minute, second=random.randint(0, 59))
            ip = random.choice(NORMAL_IPS)
            docs.append(make_login(TARGET_USER, ip, ts))
    return docs


def generate_impossible_travel():
    """The demo scenario: Toronto then Singapore 20 min apart, shared session.

    Key signals for the agent to discover:
    - Same session_id from two countries 20 min apart
    - Toronto: password + MFA (legitimate login)
    - Singapore: token auth, no MFA (session replay / stolen token)
    - Same device fingerprint suggests cloned session, not a real device
    """
    shared_session = str(uuid.uuid4())
    ts_toronto = NOW - timedelta(hours=1)
    ts_singapore = ts_toronto + timedelta(minutes=20)

    # Legitimate login — password + MFA from jsmith's usual device
    toronto_login = make_login(
        TARGET_USER, NORMAL_IPS[0], ts_toronto,
        session_id=shared_session,
        device=JSMITH_DEVICE,
        auth_method="password",
        mfa_used=True,
    )

    # Attacker replays stolen session token — no MFA challenge
    singapore_login = make_login(
        TARGET_USER, ATTACK_IP, ts_singapore,
        session_id=shared_session,
        device=JSMITH_DEVICE,  # same fingerprint — cloned session
        auth_method="token",
        mfa_used=False,
    )
    return [toronto_login, singapore_login], shared_session


def generate_alert(session_id):
    """Create an alert event for the soc_alerts index."""
    alert_id = str(uuid.uuid4())
    return {
        "time": NOW.isoformat(),
        "alert_id": alert_id,
        "alert_title": f"Impossible Travel: {TARGET_USER}",
        "alert_severity": "high",
        "alert_tags": "impossible-travel,identity,session-theft",
        "alert_description": (
            f"User {TARGET_USER} logged in from Toronto (CA) and Singapore (SG) "
            f"within 20 minutes. Session ID {session_id} seen from both locations."
        ),
        "user_name": TARGET_USER,
        "src_ips": f"{NORMAL_IPS[0]['ip']},{ATTACK_IP['ip']}",
        "src_countries": "CA,SG",
        "session_id": session_id,
        "alert_status": "new",
    }, alert_id


def generate_noise(count=80):
    """Other users logging in normally for realism."""
    docs = []
    for _ in range(count):
        user = random.choice(OTHER_USERS)
        ip = random.choice(OTHER_IPS)
        d = random.randint(0, 30)
        ts = NOW - timedelta(days=d, hours=random.randint(0, 23), minutes=random.randint(0, 59))
        outcome = "success" if random.random() > 0.05 else "failure"
        docs.append(make_login(user, ip, ts, outcome=outcome))
    return docs


def create_splunk_index(splunk_url, index_name):
    """Create a Splunk index via REST API (idempotent — ignores if exists)."""
    resp, status = splunk_rest(
        splunk_url, "POST", "/services/data/indexes",
        {"name": index_name, "datatype": "event", "output_mode": "json"},
    )
    if status == 201:
        print(f"  Created index: {index_name}")
    elif status == 409 or "already exists" in str(resp):
        print(f"  Index exists: {index_name}")
    else:
        print(f"  Index creation HTTP {status}: {resp}")


def hec_bulk(hec_url, index_name, sourcetype, docs):
    """Send a batch of events to Splunk HEC."""
    events = []
    for doc in docs:
        # Extract timestamp and convert to epoch
        ts_str = doc.pop("time", None)
        epoch = None
        if ts_str:
            try:
                dt = datetime.fromisoformat(ts_str)
                epoch = dt.timestamp()
            except ValueError:
                pass

        event = {
            "index": index_name,
            "sourcetype": sourcetype,
            "event": doc,
        }
        if epoch:
            event["time"] = epoch

        events.append(event)

    # Send in batches of 50 to avoid overwhelming HEC
    ok_count = 0
    err_count = 0
    batch_size = 50
    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        resp, status = hec_send(hec_url, batch)
        if status == 200 and resp.get("code") == 0:
            ok_count += len(batch)
        else:
            err_count += len(batch)
            print(f"  HEC batch error: {resp}")

    return ok_count, err_count


def create_thehive_alert(thehive_url, session_id, alert_id, soc_api_key=None):
    """Create an alert in TheHive so the demo starts from case management."""
    alert_payload = {
        "type": "detection",
        "source": "SIEM",
        "sourceRef": alert_id,
        "title": f"Impossible Travel: {TARGET_USER}",
        "description": (
            f"User **{TARGET_USER}** logged in from **Toronto (CA)** and "
            f"**Singapore (SG)** within 20 minutes.\n\n"
            f"- Toronto login: `203.0.113.10` at {(NOW - timedelta(hours=1)).strftime('%H:%M:%S UTC')}\n"
            f"- Singapore login: `45.77.88.99` at {(NOW - timedelta(hours=1) + timedelta(minutes=20)).strftime('%H:%M:%S UTC')}\n"
            f"- Shared session ID: `{session_id}`\n\n"
            f"This alert requires triage — is this legitimate travel or session theft?"
        ),
        "severity": 3,
        "date": int(NOW.timestamp() * 1000),
        "tags": ["impossible-travel", "identity", "session-theft"],
        "flag": False,
        "tlp": 2,
        "pap": 2,
        "summary": "Impossible travel detected — awaiting agent triage",
    }

    if soc_api_key and isinstance(soc_api_key, str):
        resp, status = thehive_request(
            thehive_url, "POST", "/api/v1/alert", alert_payload,
            api_key=soc_api_key
        )
    else:
        resp, status = thehive_request(
            thehive_url, "POST", "/api/v1/alert", alert_payload,
            user=SOC_USER, password=SOC_PASS
        )
    if status >= 400:
        print(f"  Warning: Could not create TheHive alert (HTTP {status}): {resp}")
        print(f"  The SIEM data is loaded — you can create the alert in TheHive manually.")
        return None

    thehive_id = resp.get("_id", "unknown")
    print(f"  Created TheHive alert: {thehive_id}")
    return thehive_id


def main():
    parser = argparse.ArgumentParser(description="Load sample data for Agentic SOC lab")
    parser.add_argument("--splunk-url", default="https://localhost:8089",
                        help="Splunk REST API URL (management port)")
    parser.add_argument("--hec-url", default="http://localhost:8088",
                        help="Splunk HEC URL")
    parser.add_argument("--thehive-url", default="http://localhost:9000",
                        help="TheHive URL")
    args = parser.parse_args()

    splunk_url = args.splunk_url.rstrip("/")
    hec_url = args.hec_url.rstrip("/")
    thehive_url = args.thehive_url.rstrip("/")

    # Verify Splunk is reachable
    try:
        resp, status = splunk_rest(splunk_url, "GET", "/services/server/info",
                                   params={"output_mode": "json"})
        version = "unknown"
        if isinstance(resp, dict) and resp.get("entry"):
            version = resp["entry"][0].get("content", {}).get("version", "unknown")
        print(f"Connected to Splunk {version} at {splunk_url}")
    except Exception as e:
        print(f"Cannot connect to Splunk at {splunk_url}: {e}")
        print("Make sure Splunk is running and port 8089 is accessible.")
        return

    # Create Splunk indexes
    print("\nCreating indexes...")
    create_splunk_index(splunk_url, AUTH_INDEX)
    create_splunk_index(splunk_url, ALERT_INDEX)

    # Generate data
    print("\nGenerating sample data...")
    auth_docs = []

    normal = generate_normal_history()
    print(f"  {len(normal)} normal logins for {TARGET_USER} (30 days)")
    auth_docs.extend(normal)

    travel_docs, session_id = generate_impossible_travel()
    print(f"  2 impossible-travel logins (shared session: {session_id[:8]}...)")
    auth_docs.extend(travel_docs)

    noise = generate_noise()
    print(f"  {len(noise)} noise logins from other users")
    auth_docs.extend(noise)

    alert_doc, alert_id = generate_alert(session_id)
    print(f"  1 alert (ID: {alert_id[:8]}...)")

    # Send to Splunk via HEC
    print(f"\nSending {len(auth_docs)} auth events + 1 alert to Splunk HEC...")
    ok, errs = hec_bulk(hec_url, AUTH_INDEX, "auth_event", auth_docs)
    print(f"  auth_logs: {ok} indexed, {errs} errors")

    ok, errs = hec_bulk(hec_url, ALERT_INDEX, "soc_alert", [alert_doc])
    print(f"  soc_alerts: {ok} indexed, {errs} errors")

    # Set up TheHive org user
    print("\nSetting up TheHive SOC user...")
    soc_api_key = setup_thehive_user(thehive_url)

    # Create alert in TheHive
    print("\nCreating alert in TheHive...")
    thehive_id = create_thehive_alert(thehive_url, session_id, alert_id, soc_api_key)

    # Summary
    print(f"\nDone!")
    print(f"  Splunk auth_logs:  ~{len(auth_docs)} events")
    print(f"  Splunk soc_alerts: 1 event")
    if thehive_id:
        print(f"  TheHive:           alert {thehive_id}")
    print(f"\n  Alert ID for demo: {alert_id}")
    print(f"  Target user:       {TARGET_USER}")
    print(f"  Shared session:    {session_id}")
    print(f"\n  Splunk Web UI:     http://localhost:8000  (admin / {SPLUNK_PASS})")
    print(f"  TheHive:           http://localhost:9000")


if __name__ == "__main__":
    main()
