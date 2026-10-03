# BSides Toronto — Agentic SOC Demo Lab

Portable lab for the BSides Toronto talk: *"Agentic SOC, Demystified: From Buzzword to Working Pipeline."*

## Architecture

```
┌─────────────┐     webhook      ┌─────────┐     DeepSeek API     ┌──────────────┐
│  TheHive 5  │ ──────────────▶  │   n8n   │ ──────────────────▶  │  DeepSeek v4 │
│  (cases)    │                  │  (SOAR)  │ ◀──────────────────  │   Pro (LLM)  │
└─────────────┘                  └────┬────┘                       └──────────────┘
                                      │
                         reads OKF    │  tool calls via MCP
                         playbooks    │
                              ┌───────┴───────┐
                              │               │
                         ┌────▼────┐    ┌─────▼──────┐
                         │ OKF     │    │ MCP Server │
                         │ Bundle  │    │ (Splunk)   │
                         │ (files) │    └─────┬──────┘
                         └─────────┘          │
                                        ┌─────▼──────────┐
                                        │   Splunk        │
                                        │ (SIEM / logs)   │
                                        └─────────────────┘
```

**Flow (agentic tool loop):**
1. Alert lands in TheHive (case management)
2. n8n webhook fires on new alert
3. n8n loads all 55 OKF playbooks into the agent's context
4. n8n sends alert context + playbooks to DeepSeek v4 Pro with MCP tools
5. **Agent loop begins:**
   - DeepSeek reads the playbooks, selects the most relevant one, and states its reasoning
   - Calls `get_field_mappings` to learn the index schema
   - Writes its own SPL query and calls `run_query`
   - Reads the results — if the query failed or returned nothing useful,
     it reads the error, fixes the query, and retries
   - Decides what to investigate next based on what it found
   - Repeats until it has enough evidence to make a verdict (max 10 iterations)
6. DeepSeek writes a triage summary with verdict, severity, evidence, and recommended action
7. n8n posts the enrichment back to TheHive

**Key design choice:** The MCP server is deliberately dumb — two generic tools
(`run_query` and `get_field_mappings`). The LLM writes its own SPL queries from
the prose in the playbook. This is a real agent, not a SOAR flow with an LLM wrapper.

## Prerequisites

- Docker & Docker Compose
- ~6 GB RAM free (Splunk 2 GB + Cassandra 512 MB + TheHive 512 MB + the rest)
- A DeepSeek API key (get one at https://platform.deepseek.com)
- Python 3.9+ (for the sample data loader — no pip dependencies)

## Quick Start

```bash
# 1. Clone / copy this directory
cd bsides-lab

# 2. Set your DeepSeek API key
#    Edit .env → replace the DEEPSEEK_API_KEY value with your real key

# 3. Start the stack
docker compose up -d

# 4. Wait for healthy services (~90-120 seconds for Splunk + Cassandra)
docker compose ps      # all should show "healthy" or "running"

# 5. Load sample data into Splunk + create TheHive alert
python sample-data/load_sample_data.py

# 6. Verify
#    Splunk Web: http://localhost:8000 (admin / BsidesLab1!)
#    Search: index=auth_logs | stats count
#    MCP health: curl http://localhost:3001/health
```

## Services & Ports

| Service        | URL                          | Purpose                          |
|---------------|------------------------------|----------------------------------|
| Splunk Web    | http://localhost:8000        | SIEM UI — search & dashboards    |
| Splunk HEC    | http://localhost:8088        | HTTP Event Collector (ingestion) |
| Splunk Mgmt   | https://localhost:8089       | REST API (management)            |
| n8n           | http://localhost:5678        | Orchestration / workflows        |
| TheHive       | http://localhost:9000        | Case management                  |
| MCP Server    | http://localhost:3001/mcp    | Splunk query tools (MCP)         |
| MCP Health    | http://localhost:3001/health | MCP server health check          |
| MinIO Console | http://localhost:9002        | TheHive file storage UI          |

## Logins

| Service  | Username / Login               | Password     |
|----------|-------------------------------|--------------|
| Splunk   | `admin`                       | `BsidesLab1!` |
| TheHive  | `admin@thehive.local`         | `secret`     |
| TheHive  | `analyst@bsides-lab.local`    | `thehive1234` |

The SOC analyst user is created automatically by the data loader in the `bsides-lab` org with `org-admin` profile.

## MCP Server Tools

The MCP server is intentionally minimal — three tools. The LLM decides what to query.

| Tool                 | What it does                                                      |
|----------------------|-------------------------------------------------------------------|
| `run_query`          | Execute any read-only SPL query against Splunk                    |
| `get_field_mappings` | Discover index schemas — field names, types, available indexes    |
| `check_budget`       | Check remaining query budget and estimated cost for this triage   |

**Guardrails baked into the server:**
- **Read-only** — search commands only; no delete/update
- **Query timeout** — 30s per search (async job with polling)
- **Result cap** — max 200 results per query to keep context window manageable
- **Error feedback** — errors returned as structured JSON with hints so the LLM can self-correct
- **Per-triage budget** — max queries and dollar cap per investigation (see below)

## Budget Guardrails

Each triage run gets a query budget to prevent runaway API costs. The agent sees its remaining budget after every query and is instructed to wrap up when it runs low.

| Setting              | Default  | Env Var               | What it controls                            |
|----------------------|----------|-----------------------|---------------------------------------------|
| Max queries          | 8        | `TRIAGE_MAX_QUERIES`  | Hard cap on Splunk queries per triage       |
| Cost per query       | $0.03    | `COST_PER_QUERY_USD`  | Estimated LLM cost per tool-call iteration  |
| Budget cap           | $0.50    | `TRIAGE_BUDGET_USD`   | Dollar ceiling per triage run               |

**How it works:**
1. The n8n workflow calls `/api/budget/start` before the agent loop begins
2. Each `run_query` call decrements the budget and returns the remaining balance
3. When budget is low (<=2 queries left), the response includes a warning
4. When budget is exhausted, `run_query` refuses and tells the agent to deliver its verdict
5. The final triage report in TheHive includes a cost summary

Tune these in `.env` — set `TRIAGE_MAX_QUERIES=4` for a faster demo, or `TRIAGE_MAX_QUERIES=15` if you want the agent to dig deeper.

## Splunk Indexes

| Index       | Sourcetype   | Contents                         |
|-------------|-------------|----------------------------------|
| `auth_logs` | `auth_event` | Authentication events (logins)   |
| `soc_alerts`| `soc_alert`  | Detection alerts from SIEM rules |

**Field naming:** Fields use underscores (`src_ip`, `src_city`, `user_name`, `event_outcome`) — not ECS dots. The field `source` is reserved by Splunk as metadata, so source IPs use `src_ip`.

## OKF Playbooks

Playbooks live in `./playbooks/` as OKF markdown files. The agent reads all playbooks and selects the most relevant one for each alert — no tag-matching logic, pure LLM reasoning.

**55 playbooks** covering the standard SOC detection catalog: impossible travel, brute force, credential stuffing, lateral movement, privilege escalation, data exfiltration, DNS tunneling, ransomware, phishing, malware C2, password spray, MFA fatigue, token theft, Kerberoasting, DCSync, pass-the-hash, suspicious PowerShell, LOLBins, process injection, insider threat, cloud misconfig, WAF bypass, SQL injection, and more.

Only `impossible_travel.md` has backing sample data for the live demo — the rest exist so the agent's playbook selection looks realistic (choosing 1 out of 55, not 1 out of 2).

To add a new detection: drop a new `.md` file with `tags:` in the frontmatter. No code changes needed.

## n8n Workflow Setup

A pre-built workflow JSON is included at `n8n-workflow/agentic_soc_triage.json`. Import it via **Settings → Import from File** in n8n, then configure the DeepSeek credential.

The workflow uses n8n's **native AI Agent node** with the **DeepSeek Chat Model** sub-node — no custom code for the agent loop. The canvas looks like this:

```
Demo Trigger → Fetch Alert & Match Playbook → Triage Agent
                                                  ├── DeepSeek v4 Pro (Chat Model)
                                                  ├── Run SIEM Query (HTTP Request Tool)
                                                  └── Get Index Schema (HTTP Request Tool)
```

**Setup after import:**
1. Open the **DeepSeek v4 Pro** node → create a new DeepSeek API credential with your API key
2. The two HTTP Request Tool nodes point at `http://mcp-server:3001/api/query` and `/api/mappings` — no changes needed if running in Docker
3. Click **Test Workflow** to trigger the impossible-travel triage

The loop is where the magic happens. The LLM reads "check whether the same session
ID appears from two different source IPs" in the playbook, figures out it needs a
`stats dc(src_ip) by session_id` aggregation, writes the SPL, runs it, and interprets
the results. No one told it the query — it reasoned its way there from prose.

## Sample Data

The `load_sample_data.py` script populates Splunk via HEC and creates a TheHive alert:
- ~60-90 normal logins for user `jsmith` over 30 days (Toronto/NYC)
- 2 impossible-travel logins: Toronto → Singapore, 20 min apart, **same session_id**
- 1 alert event in the `soc_alerts` index
- ~80 noise logins from other users
- 1 alert in TheHive (so the demo starts from case management)

No pip dependencies — uses plain `urllib`.

## Tearing Down

```bash
docker compose down -v   # -v removes volumes (fresh start)
```

## Demo Tips for BSides

- Run the data loader right before going on stage so timestamps are fresh
- Have a recorded screen capture as fallback (demo gods)
- The demo is ONE flow: trigger → query → triage. Don't show autonomous response
- Keep the impossible-travel scenario — it's the strongest single use case
- The shared-session signal is the "aha" moment — make sure it's visible
