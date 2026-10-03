#!/usr/bin/env python3
"""Round 3: Try org-scoped user creation and admin org membership."""
import json, urllib.request, urllib.error, base64

URL = "http://localhost:9000"
ADMIN = "admin@thehive.local"
ADMIN_PW = "secret"
SOC = "analyst@bsides-lab.local"  # try a different login
SOC_PW = "thehive1234"
ORG_ID = "~4149304"  # bsides-lab org id from previous run
ORG_NAME = "bsides-lab"

def req(method, path, body=None, headers=None):
    url = f"{URL}{path}"
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            r.add_header(k, v)
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        raw = resp.read()
        if not raw or not raw.strip():
            return {}, resp.status
        try:
            return json.loads(raw), resp.status
        except json.JSONDecodeError:
            return {"_raw": raw.decode().strip()}, resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return json.loads(raw), e.code
        except:
            return {"_raw": raw}, e.code

def basic(user, pw):
    return {"Authorization": f"Basic {base64.b64encode(f'{user}:{pw}'.encode()).decode()}"}

admin_hdrs = basic(ADMIN, ADMIN_PW)

# ---- Approach 1: Org-scoped user creation ----
print("=== A1: POST /api/v1/organisation/{orgId}/user ===")
r, s = req("POST", f"/api/v1/organisation/{ORG_ID}/user", {
    "login": SOC, "name": "SOC Analyst", "profile": "org-admin",
}, admin_hdrs)
print(f"  HTTP {s}: {json.dumps(r)[:200]}")

print("\n=== A2: POST /api/v1/organisation/{orgName}/user ===")
r, s = req("POST", f"/api/v1/organisation/{ORG_NAME}/user", {
    "login": "analyst2@bsides.local", "name": "SOC 2", "profile": "org-admin",
}, admin_hdrs)
print(f"  HTTP {s}: {json.dumps(r)[:200]}")

# ---- Approach 2: Add admin to bsides-lab org ----
print("\n=== A3: PUT admin user to add bsides-lab org membership ===")
r, s = req("PUT", "/api/v1/user/~8416", {
    "organisation": ORG_NAME, "profile": "org-admin",
}, admin_hdrs)
print(f"  HTTP {s}: {json.dumps(r)[:200]}")

print("\n=== A4: PATCH admin user ===")
r, s = req("PATCH", "/api/v1/user/~8416", {
    "organisation": ORG_NAME, "profile": "org-admin",
}, admin_hdrs)
print(f"  HTTP {s}: {json.dumps(r)[:200]}")

# ---- Approach 3: org ID in body instead of name ----
print("\n=== A5: Create user with org ID in body ===")
r, s = req("POST", "/api/v1/user", {
    "login": "analyst3@bsides.local", "name": "SOC 3",
    "profile": "org-admin", "organisation": ORG_ID,
}, admin_hdrs)
print(f"  HTTP {s}: {json.dumps(r)[:200]}")

# ---- List users again to see what we've got ----
print("\n=== Users after experiments ===")
r, s = req("POST", "/api/v1/query", {"query":[{"_name":"listUser"}]}, admin_hdrs)
if isinstance(r, list):
    for u in r:
        print(f"  {u.get('login')} org={u.get('organisation')} profile={u.get('profile')} id={u.get('_id')}")

# ---- If any user ended up in bsides-lab, test alert creation ----
print("\n=== Test alert as any bsides-lab user ===")
if isinstance(r, list):
    for u in r:
        if u.get("organisation") == ORG_NAME:
            uid = u["_id"]
            login = u["login"]
            print(f"  Found user in bsides-lab: {login} ({uid})")
            # Set password
            req("POST", f"/api/v1/user/{uid}/password/set", {"password": SOC_PW}, admin_hdrs)
            # Generate key
            kr, ks = req("POST", f"/api/v1/user/{uid}/key/renew", headers=admin_hdrs)
            key = kr.get("_raw", str(kr)).strip().strip('"')
            print(f"  Key: {key[:16]}...")
            # Test Basic auth
            rr, rs = req("GET", "/api/v1/user/current", headers=basic(login, SOC_PW))
            print(f"  Basic auth: HTTP {rs}")
            # Test Bearer
            rr, rs = req("GET", "/api/v1/user/current", headers={"Authorization": f"Bearer {key}"})
            print(f"  Bearer key: HTTP {rs}")
            # Try alert
            alert = {"type":"detection","source":"SIEM","sourceRef":"diag-final",
                     "title":"Test","description":"test","severity":2,
                     "date":1000000000000,"tags":["test"]}
            rr, rs = req("POST", "/api/v1/alert", alert, {"Authorization": f"Bearer {key}"})
            print(f"  Create alert: HTTP {rs}: {json.dumps(rr)[:120]}")
            break
    else:
        print("  No user in bsides-lab org found.")

print("\nDone.")
