/**
 * Splunk MCP Server — Agentic SOC Lab
 *
 * Two generic tools — the LLM decides what to query.
 *   1. run_query         — execute any SPL search against Splunk
 *   2. get_field_mappings — discover index schemas so the LLM knows what fields exist
 *
 * Guardrails:
 *   - Read-only: only search commands; no delete/update
 *   - Query timeout: 30s per search
 *   - Result cap: max 200 results per query to keep context window manageable
 *
 * Transport: Streamable HTTP on /mcp (also exposes /health for Docker).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import { z } from "zod";
import { randomUUID } from "node:crypto";

// ---- Config ----
const SPLUNK_URL = process.env.SPLUNK_URL || "https://localhost:8089";
const SPLUNK_USER = process.env.SPLUNK_USER || "admin";
const SPLUNK_PASSWORD = process.env.SPLUNK_PASSWORD || "BsidesLab1!";
const PORT = parseInt(process.env.MCP_SERVER_PORT || "3001", 10);
const QUERY_TIMEOUT_MS = 30000;
const MAX_RESULTS = 200;

// ---- Budget config ----
const TRIAGE_MAX_QUERIES = parseInt(process.env.TRIAGE_MAX_QUERIES || "8", 10);
const COST_PER_QUERY_USD = parseFloat(process.env.COST_PER_QUERY_USD || "0.03");
const TRIAGE_BUDGET_USD = parseFloat(process.env.TRIAGE_BUDGET_USD || "0.50");

const SPLUNK_AUTH = "Basic " + Buffer.from(`${SPLUNK_USER}:${SPLUNK_PASSWORD}`).toString("base64");

// ---- Per-triage budget tracker ----
let activeBudget = null;

function resetBudget(sessionId) {
  activeBudget = {
    session_id: sessionId || randomUUID(),
    queries_used: 0,
    max_queries: TRIAGE_MAX_QUERIES,
    budget_usd: TRIAGE_BUDGET_USD,
    cost_per_query_usd: COST_PER_QUERY_USD,
    started_at: new Date().toISOString(),
  };
  return activeBudget;
}

function getBudgetStatus() {
  if (!activeBudget) {
    return { error: true, reason: "No active triage session. Call /api/budget/start first." };
  }
  const spent = activeBudget.queries_used * activeBudget.cost_per_query_usd;
  const remaining = activeBudget.budget_usd - spent;
  const queriesRemaining = activeBudget.max_queries - activeBudget.queries_used;
  const exhausted = queriesRemaining <= 0 || remaining <= 0;
  return {
    session_id: activeBudget.session_id,
    queries_used: activeBudget.queries_used,
    queries_remaining: Math.max(0, queriesRemaining),
    max_queries: activeBudget.max_queries,
    estimated_cost_usd: parseFloat(spent.toFixed(4)),
    budget_remaining_usd: parseFloat(Math.max(0, remaining).toFixed(4)),
    budget_usd: activeBudget.budget_usd,
    budget_exhausted: exhausted,
    warning: exhausted
      ? "BUDGET EXHAUSTED — you must deliver your verdict now with the evidence you have."
      : queriesRemaining <= 2
        ? "Budget running low — wrap up your investigation soon."
        : null,
  };
}

function consumeQuery() {
  if (!activeBudget) resetBudget();
  activeBudget.queries_used++;
  return getBudgetStatus();
}

// ---- Splunk REST helpers ----

async function splunkFetch(path, options = {}) {
  const url = `${SPLUNK_URL}${path}`;
  const resp = await fetch(url, {
    ...options,
    headers: {
      Authorization: SPLUNK_AUTH,
      ...options.headers,
    },
  });
  return resp;
}

async function splunkSearch(spl, earliestTime = "-30d@d", latestTime = "now", maxResults = 50) {
  // Ensure SPL starts with "search" if it doesn't start with a pipe
  const fullSpl = spl.trim().startsWith("|") ? spl : (spl.trim().startsWith("search ") ? spl : `search ${spl}`);

  // Step 1: Create search job
  const createResp = await splunkFetch("/services/search/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      search: fullSpl,
      output_mode: "json",
      earliest_time: earliestTime,
      latest_time: latestTime,
      max_count: String(Math.min(maxResults, MAX_RESULTS)),
    }),
  });

  if (!createResp.ok) {
    const text = await createResp.text();
    throw new Error(`Splunk search creation failed (HTTP ${createResp.status}): ${text}`);
  }

  const createData = await createResp.json();
  const sid = createData.sid;
  if (!sid) throw new Error("No search ID returned from Splunk");

  // Step 2: Poll for completion
  const start = Date.now();
  while (Date.now() - start < QUERY_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, 500));

    const statusResp = await splunkFetch(
      `/services/search/jobs/${sid}?output_mode=json`
    );
    const statusData = await statusResp.json();
    const entry = statusData.entry?.[0]?.content;

    if (entry?.isDone) break;

    if (entry?.isFailed) {
      throw new Error(`Splunk search failed: ${JSON.stringify(entry.messages)}`);
    }
  }

  if (Date.now() - start >= QUERY_TIMEOUT_MS) {
    throw new Error("Splunk search timed out after 30s");
  }

  // Step 3: Get results
  const resultsResp = await splunkFetch(
    `/services/search/jobs/${sid}/results?output_mode=json&count=${Math.min(maxResults, MAX_RESULTS)}`
  );

  if (!resultsResp.ok) {
    const text = await resultsResp.text();
    throw new Error(`Failed to get results (HTTP ${resultsResp.status}): ${text}`);
  }

  return await resultsResp.json();
}

// ---- MCP Server ----
const server = new McpServer({
  name: "splunk-siem-mcp",
  version: "2.0.0",
});

// ---------- Tool 1: run_query ----------
server.tool(
  "run_query",
  "Execute a read-only SPL (Search Processing Language) query against Splunk. " +
    "Supports the full SPL syntax including search, stats, eval, where, " +
    "table, timechart, dedup, sort, and all transforming commands. " +
    "Returns matching events and/or statistical results. " +
    "Use this to investigate alerts by searching logs, building timelines, " +
    "checking for anomalies, or aggregating patterns. " +
    "If a query fails, read the error message and adjust your query.",
  {
    search: z
      .string()
      .describe(
        'SPL query to execute. Examples: ' +
          '"index=auth_logs user_name=jsmith | stats count by src_ip", ' +
          '"index=auth_logs | where src_country!=\"CA\" | table _time user_name src_ip src_city"'
      ),
    earliest_time: z
      .string()
      .optional()
      .describe(
        "Start of time range. Splunk time modifiers like -7d@d, -24h, " +
          "-30d@d, or ISO timestamps like 2026-01-01T00:00:00. " +
          "Defaults to -30d@d."
      ),
    latest_time: z
      .string()
      .optional()
      .describe(
        'End of time range. Defaults to "now".'
      ),
    max_results: z
      .number()
      .optional()
      .describe(
        "Maximum number of results to return (0-200). Set to 0 when you " +
          "only need stats/aggregate results. Defaults to 50."
      ),
  },
  async ({ search, earliest_time, latest_time, max_results }) => {
    try {
      const maxHits = Math.min(Math.max(max_results ?? 50, 0), MAX_RESULTS);
      const data = await splunkSearch(
        search,
        earliest_time || "-30d@d",
        latest_time || "now",
        maxHits
      );

      const results = data.results || [];
      const result = {
        total: results.length,
        fields: data.fields?.map((f) => f.name) || [],
      };

      if (results.length > 0) {
        // Clean up Splunk internal fields for readability
        result.results = results.map((r) => {
          const clean = {};
          for (const [k, v] of Object.entries(r)) {
            if (!k.startsWith("_") || k === "_time" || k === "_raw") {
              clean[k] = v;
            }
          }
          return clean;
        });
      }

      return {
        content: [
          { type: "text", text: JSON.stringify(result, null, 2) },
        ],
      };
    } catch (err) {
      const errorDetail = {
        error: true,
        reason: err.message,
        hint:
          "Read the error reason above. Common fixes: check field names " +
          "with get_field_mappings, verify the index name exists, check " +
          "SPL syntax (pipes separate commands, use quotes around values " +
          "with special characters).",
      };
      return {
        content: [
          { type: "text", text: JSON.stringify(errorDetail, null, 2) },
        ],
      };
    }
  }
);

// ---------- Tool 2: get_field_mappings ----------
server.tool(
  "get_field_mappings",
  "Get the field names and types available in a Splunk index. " +
    "Call this before writing queries so you know what fields are available " +
    "and their exact names. " +
    "If no index is specified, lists all available data indexes.",
  {
    index: z
      .string()
      .optional()
      .describe(
        "Index name to get field info for. Omit to list all available indexes."
      ),
  },
  async ({ index }) => {
    try {
      if (!index) {
        // List all non-internal indexes
        const resp = await splunkFetch(
          "/services/data/indexes?output_mode=json&count=100"
        );
        const data = await resp.json();
        const indexes = (data.entry || [])
          .filter((e) => !e.name.startsWith("_") && e.name !== "history" && e.name !== "summary")
          .map((e) => ({
            index: e.name,
            totalEventCount: e.content?.totalEventCount || "0",
            currentDBSizeMB: e.content?.currentDBSizeMB || "0",
          }));

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({ available_indexes: indexes }, null, 2),
            },
          ],
        };
      }

      // Get field summary for specific index using fieldsummary
      const data = await splunkSearch(
        `index=${index} | head 1000 | fieldsummary`,
        "-30d@d",
        "now",
        200
      );

      const results = data.results || [];
      const fields = {};
      for (const r of results) {
        const name = r.field;
        if (name && !name.startsWith("_") || name === "_time" || name === "_raw") {
          fields[name] = {
            type: r.distinct_count > 20 ? "text" : "keyword",
            distinct_values: r.distinct_count,
            sample_values: r.values ? r.values.substring(0, 200) : undefined,
          };
        }
      }

      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                index,
                field_count: Object.keys(fields).length,
                fields,
              },
              null,
              2
            ),
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({
              error: true,
              reason: err.message,
              hint: "Index may not exist. Call get_field_mappings without an index to list available indexes.",
            }),
          },
        ],
      };
    }
  }
);

// ---------- Tool 3: check_budget ----------
server.tool(
  "check_budget",
  "Check how many queries you have remaining and your estimated cost so far. " +
    "Call this before starting a new line of investigation to make sure " +
    "you have enough budget left. If budget is exhausted, you MUST stop " +
    "querying and deliver your triage verdict immediately with the " +
    "evidence gathered so far.",
  {},
  async () => {
    const status = getBudgetStatus();
    return {
      content: [
        { type: "text", text: JSON.stringify(status, null, 2) },
      ],
    };
  }
);

// ---- Express app with Streamable HTTP transport ----
const app = express();
app.use(express.json());

const sessions = new Map();

app.post("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] || randomUUID();
  let transport = sessions.get(sessionId);
  if (!transport) {
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => sessionId,
    });
    await server.connect(transport);
    sessions.set(sessionId, transport);
  }
  await transport.handleRequest(req, res);
});

app.get("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"];
  const transport = sessions.get(sessionId);
  if (transport) {
    await transport.handleRequest(req, res);
  } else {
    res.status(400).json({ error: "No session. POST first." });
  }
});

app.delete("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"];
  const transport = sessions.get(sessionId);
  if (transport) {
    await transport.handleRequest(req, res);
    sessions.delete(sessionId);
  }
  res.status(200).end();
});

// ============================================================
// REST API — plain HTTP endpoints for n8n tool nodes
// These mirror the MCP tools so n8n can call them directly.
// ============================================================

// POST /api/query — run a Splunk search (budget-enforced)
app.post("/api/query", async (req, res) => {
  try {
    // Budget check: refuse query if budget is exhausted
    const budget = getBudgetStatus();
    if (budget.budget_exhausted) {
      return res.json({
        error: true,
        reason: "BUDGET EXHAUSTED",
        budget,
        hint: "You have used all your allotted queries for this triage. Deliver your verdict now with the evidence you have gathered so far.",
      });
    }

    const { search, earliest_time, latest_time, max_results } = req.body;
    if (!search) {
      return res.status(400).json({ error: "search is required (SPL query string)" });
    }

    const maxHits = Math.min(Math.max(max_results ?? 50, 0), MAX_RESULTS);
    const data = await splunkSearch(
      search,
      earliest_time || "-30d@d",
      latest_time || "now",
      maxHits
    );

    // Consume a query from the budget
    const updatedBudget = consumeQuery();

    const results = data.results || [];
    const response = {
      total: results.length,
      fields: data.fields?.map((f) => f.name) || [],
      budget: updatedBudget,
    };

    if (results.length > 0) {
      response.results = results.map((r) => {
        const clean = {};
        for (const [k, v] of Object.entries(r)) {
          if (!k.startsWith("_") || k === "_time" || k === "_raw") {
            clean[k] = v;
          }
        }
        return clean;
      });
    }

    res.json(response);
  } catch (err) {
    res.status(400).json({
      error: true,
      reason: err.message,
      hint: "Check field names with GET /api/mappings/:index. Verify SPL syntax and index name.",
    });
  }
});

// GET /api/mappings — list all indexes
app.get("/api/mappings", async (_req, res) => {
  try {
    const resp = await splunkFetch(
      "/services/data/indexes?output_mode=json&count=100"
    );
    const data = await resp.json();
    const indexes = (data.entry || [])
      .filter((e) => !e.name.startsWith("_") && e.name !== "history" && e.name !== "summary")
      .map((e) => ({
        index: e.name,
        totalEventCount: e.content?.totalEventCount || "0",
      }));
    res.json({ available_indexes: indexes });
  } catch (err) {
    res.status(500).json({ error: true, reason: err.message });
  }
});

// GET /api/mappings/:index — get field mappings for an index
app.get("/api/mappings/:index", async (req, res) => {
  try {
    const idx = req.params.index;
    const data = await splunkSearch(
      `index=${idx} | head 1000 | fieldsummary`,
      "-30d@d",
      "now",
      200
    );

    const results = data.results || [];
    const fields = {};
    for (const r of results) {
      const name = r.field;
      if (name && (!name.startsWith("_") || name === "_time")) {
        fields[name] = r.distinct_count > 20 ? "text" : "keyword";
      }
    }

    res.json({ index: idx, field_count: Object.keys(fields).length, fields });
  } catch (err) {
    res.status(404).json({ error: true, reason: err.message });
  }
});

// POST /api/mappings — get field mappings (body: { index: "..." })
app.post("/api/mappings", async (req, res) => {
  try {
    const idx = req.body.index;
    if (!idx) {
      const resp = await splunkFetch(
        "/services/data/indexes?output_mode=json&count=100"
      );
      const data = await resp.json();
      const indexes = (data.entry || [])
        .filter((e) => !e.name.startsWith("_") && e.name !== "history" && e.name !== "summary")
        .map((e) => ({
          index: e.name,
          totalEventCount: e.content?.totalEventCount || "0",
        }));
      return res.json({ available_indexes: indexes });
    }

    const data = await splunkSearch(
      `index=${idx} | head 1000 | fieldsummary`,
      "-30d@d",
      "now",
      200
    );

    const results = data.results || [];
    const fields = {};
    for (const r of results) {
      const name = r.field;
      if (name && (!name.startsWith("_") || name === "_time")) {
        fields[name] = r.distinct_count > 20 ? "text" : "keyword";
      }
    }

    res.json({ index: idx, field_count: Object.keys(fields).length, fields });
  } catch (err) {
    res.status(404).json({ error: true, reason: err.message });
  }
});

// ---- Budget endpoints ----

// POST /api/budget/start — initialize a new triage budget session
app.post("/api/budget/start", async (req, res) => {
  const { session_id, max_queries, budget_usd } = req.body || {};
  const budget = resetBudget(session_id);
  if (max_queries) budget.max_queries = activeBudget.max_queries = max_queries;
  if (budget_usd) budget.budget_usd = activeBudget.budget_usd = budget_usd;
  console.log(`Budget session started: ${budget.session_id} (max ${budget.max_queries} queries, $${budget.budget_usd} budget)`);
  res.json(budget);
});

// GET /api/budget — check current budget status
app.get("/api/budget", async (_req, res) => {
  res.json(getBudgetStatus());
});

// POST /api/budget/check — check budget (for n8n tool node compatibility)
app.post("/api/budget/check", async (_req, res) => {
  res.json(getBudgetStatus());
});

// Health check for Docker
app.get("/health", async (_req, res) => {
  try {
    const resp = await splunkFetch("/services/server/info?output_mode=json");
    if (resp.ok) {
      res.json({ status: "ok", splunk: "connected", version: "2.0.0" });
    } else {
      res.status(503).json({ status: "error", splunk: "unhealthy" });
    }
  } catch {
    res.status(503).json({ status: "error", splunk: "disconnected" });
  }
});

app.listen(PORT, () => {
  console.log(`Splunk MCP Server v2 listening on :${PORT}`);
  console.log(`  MCP endpoint: http://localhost:${PORT}/mcp`);
  console.log(`  Health check: http://localhost:${PORT}/health`);
  console.log(`  Splunk:       ${SPLUNK_URL}`);
  console.log(`  Tools: run_query, get_field_mappings, check_budget`);
  console.log(`  Guardrails: read-only, 30s timeout, max ${MAX_RESULTS} results`);
  console.log(`  Budget: max ${TRIAGE_MAX_QUERIES} queries/triage, $${TRIAGE_BUDGET_USD} cap, ~$${COST_PER_QUERY_USD}/query`);
});
