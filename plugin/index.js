/**
 * jarvis-skill-learner — OpenClaw Plugin
 *
 * Hooks into the agent lifecycle to automatically detect complex sessions
 * (≥N tool calls) and evaluate whether they contain reusable workflow patterns
 * worth saving as Skills.
 *
 * Architecture:
 *   after_tool_call  → accumulate per-run tool call stats
 *   agent_end        → check if threshold met, mark run for analysis
 *   session_end      → read session transcript, write analysis request file
 *                      (external cron/script picks up and calls Gemini)
 *
 * Security: No child_process, no direct network calls from plugin.
 * Gemini evaluation is done by an external script triggered via cron or manually.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";

// ─── Configuration ───────────────────────────────────────────────────────────
const TOOL_CALL_THRESHOLD = 5;
const DATA_DIR = path.join(os.homedir(), ".openclaw/workspace/data/skill-learner");
const SKILLS_OUTPUT_DIR = path.join(os.homedir(), ".openclaw/workspace/skills/auto-learned");
const MEMORY_MD_PATH = path.join(os.homedir(), ".openclaw/workspace/MEMORY.md");

// Memory health thresholds
const MEMORY_LINE_WARN = 250;
const MEMORY_LINE_DANGER = 300;

// ─── Per-run state ───────────────────────────────────────────────────────────
const runStats = new Map();

// Track which skills were loaded during each run
// Detected via Read_tool calls to paths containing /skills/
const runSkillsUsed = new Map(); // runId → Set<skillName>

// Daily tool usage accumulator
let dailyToolStats = {};
let dailyStatsDate = new Date().toISOString().split("T")[0];

function getOrCreateRun(runId) {
  if (!runId) runId = "__default__";
  if (!runStats.has(runId)) {
    runStats.set(runId, { toolCalls: [], markedForAnalysis: false });
  }
  return runStats.get(runId);
}

// ─── Tool Usage Stats (pure file I/O, no network) ───────────────────────────
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

    // Keep only last 30 days
    const keys = Object.keys(allStats).sort();
    if (keys.length > 30) {
      for (const k of keys.slice(0, keys.length - 30)) delete allStats[k];
    }
    await fs.writeFile(statsFile, JSON.stringify(allStats, null, 2), "utf-8");
  } catch (err) {
    console.error("[skill-learner] Failed to persist tool stats:", err.message);
  }
}

// ─── Session Transcript Extraction (pure file I/O) ───────────────────────────
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
    console.error("[skill-learner] Failed to parse session:", err.message);
    return null;
  }
}

// ─── Analysis Request Writer (queues for external evaluation) ────────────────
async function queueForAnalysis(sessionFile, usedSkills = []) {
  const summary = await extractSessionSummary(sessionFile);
  if (!summary || summary.toolCount < TOOL_CALL_THRESHOLD) return;

  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const request = {
    id: requestId,
    sessionFile,
    createdAt: new Date().toISOString(),
    toolCount: summary.toolCount,
    toolNames: summary.toolNames,
    userMessages: summary.userMessages,
    assistantTexts: summary.assistantTexts,
    skillsUsed: [...new Set(usedSkills)],
    status: "pending",
  };

  const queueDir = path.join(DATA_DIR, "analysis-queue");
  await fs.mkdir(queueDir, { recursive: true });
  await fs.writeFile(
    path.join(queueDir, `${requestId}.json`),
    JSON.stringify(request, null, 2),
    "utf-8"
  );

  console.log(`[skill-learner] 📋 Queued session for analysis: ${requestId} (${summary.toolCount} tool calls, tools: ${summary.toolNames.slice(0, 5).join(", ")})`);
  return requestId;
}

// ─── Memory Health Check (pure file I/O) ─────────────────────────────────────
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

    // Write health status for other tools to consume
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
    console.log("[skill-learner] 🧠 Plugin registered. Hooks: after_tool_call, agent_end, session_end");

    // ── Hook 1: after_tool_call — accumulate stats + detect skill usage ──
    api.on("after_tool_call", (event, ctx) => {
      const runId = ctx.runId || event.runId || "__default__";
      const run = getOrCreateRun(runId);
      run.toolCalls.push({
        name: event.toolName,
        durationMs: event.durationMs || 0,
        error: event.error || null,
      });
      recordToolUsage(event.toolName, event.durationMs, event.error);

      // Detect skill loading: Read_tool reading a SKILL.md file
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

    // ── Hook 2: agent_end — check threshold ──
    api.on("agent_end", (event, ctx) => {
      const runId = ctx.runId || "__default__";
      const run = runStats.get(runId);
      if (!run) return;

      const count = run.toolCalls.length;
      if (count >= TOOL_CALL_THRESHOLD) {
        run.markedForAnalysis = true;
        console.log(`[skill-learner] 🔍 Run marked for analysis (${count} tool calls)`);
      }

      // Housekeeping: keep last 10 runs
      if (runStats.size > 20) {
        const keys = [...runStats.keys()];
        for (const k of keys.slice(0, keys.length - 10)) runStats.delete(k);
      }
    });

    // ── Hook 3: session_end — queue analysis + health check + persist stats ──
    api.on("session_end", async (event, ctx) => {
      const sessionFile = event.sessionFile;

      // Check if any run was marked for analysis
      let hasMarkedRun = false;
      for (const [, run] of runStats) {
        if (run.markedForAnalysis) {
          hasMarkedRun = true;
          run.markedForAnalysis = false;
          break;
        }
      }

      // Queue session for external Gemini evaluation
      if (hasMarkedRun && sessionFile) {
        try {
          // Collect skills that were actually used in this session
          const usedSkills = [];
          for (const [, skills] of runSkillsUsed) {
            for (const s of skills) usedSkills.push(s);
          }
          await queueForAnalysis(sessionFile, usedSkills);
        } catch (err) {
          console.error("[skill-learner] Queue failed:", err.message);
        }
      }

      // Memory health check (piggyback)
      try { await checkMemoryHealth(); } catch { }

      // Persist tool stats
      try { await persistDailyStats(); } catch { }
    });

    // ── Hook 4: gateway_start ──
    api.on("gateway_start", () => {
      console.log("[skill-learner] 🧠 Skill Learner active. Threshold: ≥" + TOOL_CALL_THRESHOLD + " tool calls → queue for analysis.");
    });
  },
});
