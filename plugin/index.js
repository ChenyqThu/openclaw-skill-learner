/**
 * jarvis-skill-learner — OpenClaw Plugin  (Phase 2)
 *
 * Phase 2 upgrades:
 *  • Idempotency dedup Sets for after_tool_call / agent_end / session_end
 *  • agent_end now extracts transcript from event.messages and fires HTTP
 *    POST to localhost:8300/evaluate (fire-and-forget)
 *  • Silent fallback to queue file when evaluate-server is unreachable
 *  • session_end slimmed down to: memory health + tool stats + fallback queue
 *
 * Security: No child_process. HTTP to localhost only. fs allowed.
 * Plugin must remain ESM (import / export default definePluginEntry).
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import http from "node:http";

// ─── Configuration ───────────────────────────────────────────────────────────
const TOOL_CALL_THRESHOLD = 5;
const DATA_DIR = path.join(os.homedir(), ".openclaw/workspace/data/skill-learner");
const MEMORY_MD_PATH = path.join(os.homedir(), ".openclaw/workspace/MEMORY.md");
const EVALUATE_SERVER_URL = "http://127.0.0.1:8300/evaluate";

// Memory health thresholds
const MEMORY_LINE_WARN = 250;
const MEMORY_LINE_DANGER = 300;

// Dedup Set hard cap
const DEDUP_MAX = 200;

// ─── Idempotency Sets ─────────────────────────────────────────────────────────
const processedToolCalls = new Set();  // key: `${runId}:${toolCallIndex}`
const processedAgentEnds = new Set();  // key: runId
const processedSessionEnds = new Set(); // key: sessionId

function dedupAdd(set, key) {
  if (set.size >= DEDUP_MAX) {
    // Remove the oldest entry (first inserted)
    const first = set.values().next().value;
    set.delete(first);
  }
  set.add(key);
}

// ─── Per-run state ───────────────────────────────────────────────────────────
const runStats = new Map();
const runSkillsUsed = new Map(); // runId → Set<skillName>

// Track which runs agent_end already fired HTTP for (so session_end can skip)
const agentEndFiredHttp = new Set(); // runId

// Daily tool usage accumulator
let dailyToolStats = {};
let dailyStatsDate = new Date().toISOString().split("T")[0];

function getOrCreateRun(runId) {
  if (!runId) runId = "__default__";
  if (!runStats.has(runId)) {
    runStats.set(runId, { toolCalls: [], toolCallIndex: 0, markedForAnalysis: false });
  }
  return runStats.get(runId);
}

// ─── Tool Usage Stats ─────────────────────────────────────────────────────────
function recordToolUsage(toolName, durationMs, error) {
  const today = new Date().toISOString().split("T")[0];
  if (today !== dailyStatsDate) {
    persistDailyStats().catch(() => {});
    dailyToolStats = {};
    dailyStatsDate = today;
  }
  if (!dailyToolStats[toolName]) {
    dailyToolStats[toolName] = { calls: 0, errors: 0, totalMs: 0 };
  }
  dailyToolStats[toolName].calls++;
  if (error) dailyToolStats[toolName].errors++;
  if (durationMs) dailyToolStats[toolName].totalMs += durationMs;
}

async function persistDailyStats() {
  if (Object.keys(dailyToolStats).length === 0) return;
  try {
    const statsFile = path.join(DATA_DIR, "tool-usage-stats.json");
    await fs.mkdir(DATA_DIR, { recursive: true });
    let allStats = {};
    try { allStats = JSON.parse(await fs.readFile(statsFile, "utf-8")); } catch { }
    allStats[dailyStatsDate] = dailyToolStats;
    const keys = Object.keys(allStats).sort();
    if (keys.length > 30) {
      for (const k of keys.slice(0, keys.length - 30)) delete allStats[k];
    }
    await fs.writeFile(statsFile, JSON.stringify(allStats, null, 2), "utf-8");
  } catch (err) {
    console.error("[skill-learner] Failed to persist tool stats:", err.message);
  }
}

// ─── Transcript Extraction from event.messages ───────────────────────────────
/**
 * Extract a session summary from the messages array provided in agent_end.
 * Format differs from JSONL: each item is { role, content } where content
 * may be a string or an array of content blocks.
 */
function extractFromMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return null;

  const userMessages = [];
  const assistantTexts = [];
  const toolCallNames = [];

  for (const msg of messages) {
    const role = msg?.role;
    const content = msg?.content;
    if (!role || !content) continue;

    if (role === "user") {
      const text = typeof content === "string" ? content
        : Array.isArray(content) ? content.map(b => b?.text || "").join(" ")
        : "";
      if (!text) continue;
      if (text.startsWith("System:") || text.includes("HEARTBEAT")) continue;
      userMessages.push(text.slice(0, 500));
    } else if (role === "assistant") {
      const blocks = Array.isArray(content) ? content : [];
      for (const block of blocks) {
        if (!block) continue;
        if (block.type === "toolCall" || block.type === "tool_use") {
          toolCallNames.push(block.name || block.tool_name || "unknown");
        } else if (block.type === "text" && block.text) {
          assistantTexts.push(block.text.slice(0, 300));
        }
      }
      // Handle plain string content from assistant
      if (typeof content === "string" && content.trim()) {
        assistantTexts.push(content.slice(0, 300));
      }
    }
  }

  return {
    toolCount: toolCallNames.length,
    toolNames: [...new Set(toolCallNames)],
    userMessages: userMessages.slice(0, 8),
    assistantTexts: assistantTexts.slice(0, 8),
  };
}

// ─── JSONL Transcript Extraction (fallback for session_end) ──────────────────
async function extractSessionSummary(sessionFile) {
  try {
    const content = await fs.readFile(sessionFile, "utf-8");
    const lines = content.trim().split("\n");
    const userMessages = [];
    const assistantTexts = [];
    const toolCallNames = [];

    for (const line of lines) {
      try {
        const obj = JSON.parse(line);
        if (obj.type !== "message") continue;
        const msg = obj.message;
        if (!msg) continue;
        const role = msg.role;
        const msgContent = msg.content;
        if (role === "user" && typeof msgContent === "string") {
          if (msgContent.startsWith("System:") || msgContent.includes("HEARTBEAT")) continue;
          userMessages.push(msgContent.slice(0, 500));
        } else if (role === "assistant" && Array.isArray(msgContent)) {
          for (const block of msgContent) {
            if (block?.type === "toolCall") {
              toolCallNames.push(block.name || "unknown");
            } else if (block?.type === "text" && block.text) {
              assistantTexts.push(block.text.slice(0, 300));
            }
          }
        }
      } catch { }
    }
    return {
      toolCount: toolCallNames.length,
      toolNames: [...new Set(toolCallNames)],
      userMessages: userMessages.slice(0, 8),
      assistantTexts: assistantTexts.slice(0, 8),
    };
  } catch (err) {
    console.error("[skill-learner] Failed to parse session JSONL:", err.message);
    return null;
  }
}

// ─── Queue File Writer (fallback / cron兜底) ──────────────────────────────────
async function writeQueueFile(payload) {
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const request = {
    id: requestId,
    ...payload,
    status: "pending",
  };
  const queueDir = path.join(DATA_DIR, "analysis-queue");
  await fs.mkdir(queueDir, { recursive: true });
  await fs.writeFile(
    path.join(queueDir, `${requestId}.json`),
    JSON.stringify(request, null, 2),
    "utf-8"
  );
  return requestId;
}

// ─── HTTP Fire-and-Forget to evaluate-server ─────────────────────────────────
/**
 * POST JSON body to localhost:8300/evaluate.
 * Non-blocking: we do not await a response.
 * On any error, silently write a queue file as fallback.
 */
function fireEvaluate(body, fallbackSessionFile) {
  const data = JSON.stringify(body);
  const options = {
    hostname: "127.0.0.1",
    port: 8300,
    path: "/evaluate",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(data),
    },
  };

  const req = http.request(options, (res) => {
    // Drain response to free socket
    res.resume();
    console.log(`[skill-learner] 🚀 evaluate-server responded: ${res.statusCode}`);
  });

  req.on("error", (err) => {
    console.warn(`[skill-learner] ⚠️ evaluate-server unreachable (${err.message}), writing queue fallback`);
    // Fallback: write queue file for 3:30 AM cron
    const fallbackPayload = {
      sessionFile: fallbackSessionFile || null,
      createdAt: body.timestamp || new Date().toISOString(),
      toolCount: body.toolCount,
      toolNames: body.toolNames,
      userMessages: body.userMessages,
      assistantTexts: body.assistantTexts,
      skillsUsed: body.skillsUsed,
      runId: body.runId,
      agentId: body.agentId,
      sessionKey: body.sessionKey,
    };
    writeQueueFile(fallbackPayload).catch(() => {});
  });

  req.setTimeout(5000, () => {
    req.destroy();
    console.warn("[skill-learner] ⚠️ evaluate-server timeout, writing queue fallback");
    const fallbackPayload = {
      sessionFile: fallbackSessionFile || null,
      createdAt: body.timestamp || new Date().toISOString(),
      toolCount: body.toolCount,
      toolNames: body.toolNames,
      userMessages: body.userMessages,
      assistantTexts: body.assistantTexts,
      skillsUsed: body.skillsUsed,
      runId: body.runId,
      agentId: body.agentId,
      sessionKey: body.sessionKey,
    };
    writeQueueFile(fallbackPayload).catch(() => {});
  });

  req.write(data);
  req.end();
}

// ─── Memory Health Check ──────────────────────────────────────────────────────
async function checkMemoryHealth() {
  try {
    const content = await fs.readFile(MEMORY_MD_PATH, "utf-8");
    const lines = content.split("\n").length;
    const chars = content.length;
    const recentEntries = (content.match(/^### \[2026-/gm) || []).length;
    const status = lines > MEMORY_LINE_DANGER ? "danger" : lines > MEMORY_LINE_WARN ? "warning" : "healthy";
    const health = {
      timestamp: new Date().toISOString(),
      memoryLines: lines,
      memoryChars: chars,
      recentEntries,
      status,
    };
    await fs.mkdir(DATA_DIR, { recursive: true });
    await fs.writeFile(
      path.join(DATA_DIR, "memory-health.json"),
      JSON.stringify(health, null, 2),
      "utf-8"
    );
    if (status !== "healthy") {
      console.warn(`[skill-learner] ⚠️ Memory health: ${status} (${lines} lines, ${recentEntries} recent entries)`);
    }
    return health;
  } catch (err) {
    console.error("[skill-learner] Memory health check failed:", err.message);
    return null;
  }
}

// ─── Plugin Entry ────────────────────────────────────────────────────────────
export default definePluginEntry({
  id: "jarvis-skill-learner",
  name: "Jarvis Skill Learner",
  description: "Auto-detect reusable patterns from complex sessions and create skill drafts",

  register: (api) => {
    console.log("[skill-learner] 🧠 Plugin registered (Phase 2). Hooks: after_tool_call, agent_end, session_end");

    // ── Hook 1: after_tool_call ───────────────────────────────────────────────
    api.on("after_tool_call", (event, ctx) => {
      const runId = ctx.runId || event.runId || "__default__";
      const run = getOrCreateRun(runId);

      // Idempotency check
      const tcIndex = run.toolCallIndex++;
      const dedupKey = `${runId}:${tcIndex}`;
      if (processedToolCalls.has(dedupKey)) return;
      dedupAdd(processedToolCalls, dedupKey);

      run.toolCalls.push({
        name: event.toolName,
        durationMs: event.durationMs || 0,
        error: event.error || null,
      });
      recordToolUsage(event.toolName, event.durationMs, event.error);

      // Detect skill loading via Read_tool on SKILL.md
      if (event.toolName === "Read_tool" || event.toolName === "read") {
        const filePath = event.params?.path || "";
        const skillMatch = filePath.match(/\/skills\/([^/]+)\/SKILL\.md/);
        if (skillMatch) {
          const skillName = skillMatch[1];
          if (!runSkillsUsed.has(runId)) runSkillsUsed.set(runId, new Set());
          runSkillsUsed.get(runId).add(skillName);
          console.log(`[skill-learner] 📖 Skill loaded: ${skillName}`);
        }
      }
    });

    // ── Hook 2: agent_end — extract transcript + HTTP fire-and-forget ─────────
    api.on("agent_end", (event, ctx) => {
      const runId = ctx.runId || "__default__";

      // Idempotency check
      if (processedAgentEnds.has(runId)) return;
      dedupAdd(processedAgentEnds, runId);

      const run = runStats.get(runId);
      if (!run) return;

      const count = run.toolCalls.length;
      if (count < TOOL_CALL_THRESHOLD) {
        // Housekeeping
        if (runStats.size > 20) {
          const keys = [...runStats.keys()];
          for (const k of keys.slice(0, keys.length - 10)) runStats.delete(k);
        }
        return;
      }

      run.markedForAnalysis = true;
      console.log(`[skill-learner] 🔍 Run ${runId}: ${count} tool calls → triggering real-time evaluation`);

      // Collect skills used in this run
      const usedSkills = [];
      for (const [rid, skills] of runSkillsUsed) {
        if (rid === runId || rid === "__default__") {
          for (const s of skills) usedSkills.push(s);
        }
      }

      // Extract transcript from event.messages
      let summary = null;
      if (event.messages && Array.isArray(event.messages)) {
        summary = extractFromMessages(event.messages);
      }

      // Build payload
      const payload = {
        runId: runId,
        agentId: ctx.agentId || "jarvis",
        sessionKey: ctx.sessionKey || "",
        sessionId: ctx.sessionId || "",
        toolCount: count,
        toolNames: [...new Set(run.toolCalls.map(t => t.name))],
        skillsUsed: [...new Set(usedSkills)],
        userMessages: summary?.userMessages || [],
        assistantTexts: summary?.assistantTexts || [],
        timestamp: new Date().toISOString(),
      };

      // Mark so session_end skips re-queuing
      agentEndFiredHttp.add(runId);

      // Fire-and-forget HTTP POST
      fireEvaluate(payload, null);

      // Housekeeping: keep last 10 runs
      if (runStats.size > 20) {
        const keys = [...runStats.keys()];
        for (const k of keys.slice(0, keys.length - 10)) runStats.delete(k);
      }
    });

    // ── Hook 3: session_end — health + stats + fallback queue only ────────────
    api.on("session_end", async (event, ctx) => {
      const sessionId = ctx.sessionId || event.sessionId || "__default__";

      // Idempotency check
      if (processedSessionEnds.has(sessionId)) return;
      dedupAdd(processedSessionEnds, sessionId);

      // Memory health check
      try { await checkMemoryHealth(); } catch { }

      // Persist tool stats
      try { await persistDailyStats(); } catch { }

      // Fallback queue: only if agent_end did NOT already fire HTTP
      // (covers edge case where session_end fires without a preceding agent_end)
      const sessionFile = event.sessionFile;
      let hasUnfiredRun = false;
      for (const [runId, run] of runStats) {
        if (run.markedForAnalysis && !agentEndFiredHttp.has(runId)) {
          hasUnfiredRun = true;
          run.markedForAnalysis = false;

          if (sessionFile) {
            try {
              const summary = await extractSessionSummary(sessionFile);
              if (summary && summary.toolCount >= TOOL_CALL_THRESHOLD) {
                const usedSkills = [];
                for (const [, skills] of runSkillsUsed) {
                  for (const s of skills) usedSkills.push(s);
                }
                const requestId = await writeQueueFile({
                  sessionFile,
                  createdAt: new Date().toISOString(),
                  toolCount: summary.toolCount,
                  toolNames: summary.toolNames,
                  userMessages: summary.userMessages,
                  assistantTexts: summary.assistantTexts,
                  skillsUsed: [...new Set(usedSkills)],
                  runId,
                  agentId: ctx.agentId || "jarvis",
                  sessionKey: ctx.sessionKey || "",
                });
                console.log(`[skill-learner] 📋 Fallback queue written: ${requestId}`);
              }
            } catch (err) {
              console.error("[skill-learner] Fallback queue failed:", err.message);
            }
          }
          break; // Only process first unmarked run
        }
      }

      if (!hasUnfiredRun) {
        console.log("[skill-learner] ✅ session_end: agent_end already handled evaluation");
      }
    });

    // ── Hook 4: gateway_start ────────────────────────────────────────────────
    api.on("gateway_start", () => {
      console.log("[skill-learner] 🧠 Skill Learner Phase 2 active. Threshold: ≥" + TOOL_CALL_THRESHOLD + " tool calls → real-time HTTP evaluation.");
    });
  },
});
