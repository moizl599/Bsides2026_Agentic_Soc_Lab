---
type: index
title: Agentic SOC — OKF Playbook Bundle
description: >
  Index of triage playbooks for the agentic SOC lab.
  Each playbook is a standalone OKF concept file.
  The agent matches alert tags to playbook tags to select the right SOP.
timestamp: 2026-08-31T00:00:00Z
---

# Playbook Bundle

| File                      | Tags                                      | Level |
|---------------------------|--------------------------------------------|-------|
| [impossible_travel.md]    | impossible-travel, session-theft, identity | L1    |
| [brute_force.md]          | brute-force, credential-access, identity   | L1    |

## How routing works

1. An alert arrives with one or more tags (e.g. `impossible-travel`).
2. The orchestrator scans this index (or the `tags` frontmatter in each file) for a matching playbook.
3. The matching playbook body is handed to the agent as its standing instructions.
4. If no playbook matches, the agent falls back to a generic triage template.
