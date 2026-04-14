/**
 * jarvis-skill-learner — OpenClaw Plugin  (Phase 3: Self-Evolution)
 *
 * Track 0 (Skill Learning):
 *  • Idempotency dedup Sets for after_tool_call / agent_end / session_end
 *  • agent_end extracts transcript + fires HTTP POST to localhost:8300/evaluate
 *  • Silent fallback to queue file when evaluate-server is unreachable
 *
 * Track 1 (Skill Evolution — Phase 3):
 *  • Friction signal detection in after_tool_call and agent_end hooks
 *  • Signals: user correction, explicit feedback, repeated failure,
 *    error after skill read, manual trigger ("优化 skill X")
 *  • Friction data piggybacks on existing HTTP POST (no new network calls)
 *  • frictionWeight >= 4 triggers Darwin evolution loop server-side
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
const TOOL_CALL_THRESHOLD = 8;
const DATA_DIR = path.join(os.homedir(), ".openclaw/workspace/data/skill-learner");
const MEMORY_MD_PATH = path.join(os.homedir(), ".openclaw/workspace/MEMORY.md");
const EVALUATE_SERVER_URL = "http://127.0.0.1:8300/evaluate";

// Memory health thresholds
const MEMORY_LINE_WARN = 250;
const MEMORY_LINE_DANGER = 300;

// Dedup Set hard cap
const DEDUP_MAX = 200;

// Friction detection thresholds
const FRICTION_THRESHOLD = 4;        // frictionWeight >= 4 triggers evolution
const FRICTION_ERROR_WINDOW = 5;     // tool calls after skill read to watch for errors
const FRICTION_REPEAT_FAIL = 2;      // repeated failures of same tool to count as friction

// ─── Friction Detection Patterns ────────────────────────────────────────────
const FRICTION_CORRECTION_PATTERNS = /不对|不是这样|应该|错了|wrong|redo|重做|有问题|搞错|不行/;
const FRICTION_FEEDBACK_PATTERNS = /skill\s*有问题|技能有问题|skill.*不好用|skill.*不对/i;
const FRICTION_OPTIMIZE_PATTERN = /(?:优化|改进|提升)\s*(?:skill|技能)?\s*[「"']?(\S+?)[」"']?\s*$/i;
const FRICTION_OPTIMIZE_EN = /optimize\s+skill\s+["']?(\S+?)["']?\s*$/i;

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
const runFriction = new Map();   // runId → { signals: [], totalWeight: 0, targetSkill: null }

// Track which runs agent_end already fired HTTP for (so session_end can skip)
const agentEndFiredHttp = new Set(); // runId

// Daily tool usage accumulator
let dailyToolStats = {};
let dailyStatsDate = new Date().toISOString().split("T")[0];

function getOrCreateRun(runId) {
  if (!runId) runId = "__default__";
  if (!runStats.has(runId)) {
    runStats.set(runId, {
      toolCalls: [], toolCallIndex: 0, markedForAnalysis: false,
      errorsByTool: {},          // toolName → consecutive error count
      lastSkillReadIndex: -1,    // index of last SKILL.md read
      lastSkillReadName: null,   // name of last skill read
    });
  }
  return runStats.get(runId);
}

function getOrCreateFriction(runId) {
  if (!runId) runId = "__default__";
  if (!runFriction.has(runId)) {
    runFriction.set(runId, { signals: [], totalWeight: 0, targetSkill: null });
  }
  return runFriction.get(runId);
}

function addFrictionSignal(runId, type, weight, evidence, skillName) {
  const friction = getOrCreateFriction(runId);
  friction.signals.push({ type, weight, evidence: (evidence || "").slice(0, 200) });
  friction.totalWeight += weight;
  if (skillName && !friction.targetSkill) {
    friction.targetSkill = skillName;
  }
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
  let lastInboundMessageId = null; // feishu om_xxx extracted from System: header

  for (const msg of messages) {
    const role = msg?.role;
    const content = msg?.content;
    if (!role || !content) continue;

    if (role === "user") {
      const text = typeof content === "string" ? content
        : Array.isArray(content) ? content.map(b => b?.text || "").join(" ")
        : "";
      if (!text) continue;
      if (text.includes("HEARTBEAT")) continue;
      // Extract feishu message id from System header: [msg:om_xxx]
      if (text.startsWith("System:")) {
        const midMatch = text.match(/\[msg:(om_[^,\]\s]+)\]/);
        if (midMatch) lastInboundMessageId = midMatch[1];
        continue; // skip system headers from userMessages
      }
      userMessages.push(text.slice(0, 1000));
    } else if (role === "assistant") {
      const blocks = Array.isArray(content) ? content : [];
      for (const block of blocks) {
        if (!block) continue;
        if (block.type === "toolCall" || block.type === "tool_use") {
          toolCallNames.push(block.name || block.tool_name || "unknown");
        } else if (block.type === "text" && block.text) {
          assistantTexts.push(block.text.slice(0, 500));
        }
      }
      // Handle plain string content from assistant
      if (typeof content === "string" && content.trim()) {
        assistantTexts.push(content.slice(0, 500));
      }
    }
  }

  return {
    toolCount: toolCallNames.length,
    toolNames: [...new Set(toolCallNames)],
    userMessages: userMessages.slice(0, 12),
    assistantTexts: assistantTexts.slice(0, 12),
    lastInboundMessageId,
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
          userMessages.push(msgContent.slice(0, 1000));
        } else if (role === "assistant" && Array.isArray(msgContent)) {
          for (const block of msgContent) {
            if (block?.type === "toolCall") {
              toolCallNames.push(block.name || "unknown");
            } else if (block?.type === "text" && block.text) {
              assistantTexts.push(block.text.slice(0, 500));
            }
          }
        }
      } catch { }
    }
    return {
      toolCount: toolCallNames.length,
      toolNames: [...new Set(toolCallNames)],
      userMessages: userMessages.slice(0, 12),
      assistantTexts: assistantTexts.slice(0, 12),
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

function buildFallbackPayload(body, fallbackSessionFile) {
  return {
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
}

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
    writeQueueFile(buildFallbackPayload(body, fallbackSessionFile)).catch(() => {});
  });

  req.setTimeout(5000, () => {
    req.destroy();
    console.warn("[skill-learner] ⚠️ evaluate-server timeout, writing queue fallback");
    writeQueueFile(buildFallbackPayload(body, fallbackSessionFile)).catch(() => {});
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
    const year = new Date().getFullYear();
    const recentEntries = (content.match(new RegExp(`^### \\[${year}-`, "gm")) || []).length;
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
    console.log("[skill-learner] 🧠 Plugin registered (Phase 3: Self-Evolution). Hooks: after_tool_call, agent_end, session_end");

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

      // ── Friction: track errors per tool ────────────────────────────────────
      if (event.error) {
        const toolName = event.toolName;
        run.errorsByTool[toolName] = (run.errorsByTool[toolName] || 0) + 1;

        // Signal: repeated failure of same tool (>= FRICTION_REPEAT_FAIL)
        if (run.errorsByTool[toolName] >= FRICTION_REPEAT_FAIL) {
          addFrictionSignal(runId, "repeated_failure", 2,
            `${toolName} failed ${run.errorsByTool[toolName]}x consecutively`,
            run.lastSkillReadName);
        }

        // Signal: error within FRICTION_ERROR_WINDOW calls after skill read
        if (run.lastSkillReadIndex >= 0 && (tcIndex - run.lastSkillReadIndex) <= FRICTION_ERROR_WINDOW) {
          addFrictionSignal(runId, "error_after_skill_read", 2,
            `${toolName} error ${tcIndex - run.lastSkillReadIndex} calls after reading ${run.lastSkillReadName}`,
            run.lastSkillReadName);
        }
      } else {
        // Reset consecutive error count on success
        run.errorsByTool[event.toolName] = 0;
      }

      // Detect skill loading via Read_tool on SKILL.md
      if (event.toolName === "Read_tool" || event.toolName === "read") {
        const filePath = event.params?.path || "";
        const skillMatch = filePath.match(/\/skills\/([^/]+)\/SKILL\.md/);
        if (skillMatch) {
          const skillName = skillMatch[1];
          if (!runSkillsUsed.has(runId)) runSkillsUsed.set(runId, new Set());
          runSkillsUsed.get(runId).add(skillName);
          // Track for friction correlation
          run.lastSkillReadIndex = tcIndex;
          run.lastSkillReadName = skillName;
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
        // Housekeeping: keep last 10 runs, clean all related maps/sets
        if (runStats.size > 20) {
          const keys = [...runStats.keys()];
          for (const k of keys.slice(0, keys.length - 10)) {
            runStats.delete(k);
            runSkillsUsed.delete(k);
            runFriction.delete(k);
            agentEndFiredHttp.delete(k);
          }
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

      // ── Friction + Correction signals ────────────────────────────────────────
      const friction = getOrCreateFriction(runId);
      const userMsgs = summary?.userMessages || [];
      const correctionSignals = []; // Track 2: user modeling
      for (const msg of userMsgs) {
        // Manual trigger: "优化 skill X" / "optimize skill X"
        const manualMatch = msg.match(FRICTION_OPTIMIZE_PATTERN) || msg.match(FRICTION_OPTIMIZE_EN);
        if (manualMatch) {
          const targetName = manualMatch[1];
          friction.targetSkill = targetName;
          friction.totalWeight = 999; // forced trigger
          addFrictionSignal(runId, "manual_trigger", 999, `用户要求优化: ${targetName}`, targetName);
          break;
        }
        // User correction: "不对/错了/wrong/redo"
        if (FRICTION_CORRECTION_PATTERNS.test(msg)) {
          addFrictionSignal(runId, "user_correction", 3,
            msg.slice(0, 100),
            friction.targetSkill || (usedSkills.length === 1 ? usedSkills[0] : null));
          // Track 2: capture correction context for user modeling
          correctionSignals.push({ type: "user_correction", text: msg.slice(0, 300) });
        }
        // Explicit feedback: "这个 skill 有问题"
        if (FRICTION_FEEDBACK_PATTERNS.test(msg)) {
          addFrictionSignal(runId, "explicit_feedback", 3,
            msg.slice(0, 100),
            friction.targetSkill || (usedSkills.length === 1 ? usedSkills[0] : null));
          correctionSignals.push({ type: "explicit_feedback", text: msg.slice(0, 300) });
        }
      }

      // Determine if evolution should be triggered
      const triggerEvolution = friction.totalWeight >= FRICTION_THRESHOLD && !!friction.targetSkill;
      if (triggerEvolution) {
        console.log(`[skill-learner] 🧬 Friction detected (weight=${friction.totalWeight}) → evolution trigger for: ${friction.targetSkill}`);
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
        lastInboundMessageId: summary?.lastInboundMessageId || null,
        timestamp: new Date().toISOString(),
        // Track 1: friction signals for skill evolution
        frictionSignals: friction.signals,
        frictionWeight: friction.totalWeight,
        frictionSkill: friction.targetSkill,
        triggerEvolution,
        // Track 2: correction signals for user modeling
        correctionSignals,
      };

      // Mark so session_end skips re-queuing
      agentEndFiredHttp.add(runId);

      // Fire-and-forget HTTP POST
      fireEvaluate(payload, null);

      // Housekeeping: keep last 10 runs, clean all related maps/sets
      if (runStats.size > 20) {
        const keys = [...runStats.keys()];
        for (const k of keys.slice(0, keys.length - 10)) {
          runStats.delete(k);
          runSkillsUsed.delete(k);
          agentEndFiredHttp.delete(k);
        }
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
