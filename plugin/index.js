/**
 * jarvis-skill-learner — OpenClaw Plugin  (Phase 4: SDK-Native Integration)
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
 * Phase 4 — OpenClaw SDK-native integration (2026-04-17):
 *  • B.1 first-class:  api.registerTool("skill_learner_nominate", ...) — no polyfill needed
 *  • C.1.b params:     after_tool_call.event.params is already fully passed through;
 *                      we now capture (with redaction + truncation) for evaluation context
 *  • C.1.c sub_agent:  subagent_spawned / subagent_ended hooks build parent↔child runId map;
 *                      child agent_end payload now reaches parent's Skill eval context
 *  • Polyfill paths preserved for back-compat (write → nominations/*.json still works)
 *
 * Security: No child_process. HTTP to localhost only. fs allowed.
 * Plugin must remain ESM (import / export default definePluginEntry).
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "@sinclair/typebox";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import http from "node:http";

// ─── Configuration ───────────────────────────────────────────────────────────
// Phase A.6: raised 8 → 15 to cut spam-triggered evaluations while we wait on
// Phase B (agent-nominated learning). The ≥15 cutoff alone still misses real
// nominations for shorter novel sessions, but Phase A's downstream validators
// drop the resulting noise. After Phase B lands, this reverts to a lower bound
// and the sufficient trigger becomes `nominated OR frictionWeight >= 3`.
const TOOL_CALL_THRESHOLD = 15;
const DATA_DIR = path.join(os.homedir(), ".openclaw/workspace/data/skill-learner");
const MEMORY_MD_PATH = path.join(os.homedir(), ".openclaw/workspace/MEMORY.md");
const EVALUATE_SERVER_URL = "http://127.0.0.1:8300/evaluate";

// Track 4 (Curator): per-skill telemetry sidecar
const SKILL_USAGE_FILE = path.join(DATA_DIR, "skill-usage.json");
// Threshold: at agent_end, if a skill was read AND the run had >= this many
// tool calls, bump applied_count. Smaller than TOOL_CALL_THRESHOLD so we
// capture lightweight sessions that still applied a skill.
const APPLIED_TOOLCALL_THRESHOLD = 5;

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
      // Phase B.2: agent self-nomination
      nominated: false,          // true if agent called skill_learner_nominate
      nominationPayload: null,   // full payload from the tool call (topic/pain_point/etc)
      nominationCount: 0,        // hard cap 3 per run
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

// ─── Track 4 (Curator) — Per-skill Usage Telemetry ───────────────────────────
// Increments read_count / applied_count / patch_count in skill-usage.json.
// Plugin writes are best-effort: cross-process races with curator scripts are
// handled by the Python side's fcntl lock; plugin's own concurrency is serialized
// here via usageWriteChain (a single Promise chain per process). Atomic write
// via tmp + rename so the file is never half-written.
let usageWriteChain = Promise.resolve();

function _emptyUsageEntry(nowIso) {
  return {
    read_count: 0,
    applied_count: 0,
    patch_count: 0,
    last_read_at: null,
    last_applied_at: null,
    last_patched_at: null,
    state: "active",
    state_changed_at: nowIso,
    archived_at: null,
    archive_path: null,
  };
}

function bumpSkillUsage(skillName, counterKey, lastKey) {
  if (!skillName || typeof skillName !== "string") return Promise.resolve();
  usageWriteChain = usageWriteChain.then(async () => {
    try {
      await fs.mkdir(DATA_DIR, { recursive: true });
      let doc = { _meta: { schema_version: 1, last_curator_tick_at: null, last_llm_review_at: null }, skills: {} };
      try {
        doc = JSON.parse(await fs.readFile(SKILL_USAGE_FILE, "utf-8"));
      } catch { /* missing or unreadable — start fresh */ }
      if (!doc._meta) doc._meta = { schema_version: 1, last_curator_tick_at: null, last_llm_review_at: null };
      if (!doc.skills) doc.skills = {};

      const nowIso = new Date().toISOString();
      if (!doc.skills[skillName]) doc.skills[skillName] = _emptyUsageEntry(nowIso);
      const entry = doc.skills[skillName];
      entry[counterKey] = (entry[counterKey] || 0) + 1;
      entry[lastKey] = nowIso;

      const tmp = SKILL_USAGE_FILE + ".tmp";
      await fs.writeFile(tmp, JSON.stringify(doc, null, 2), "utf-8");
      await fs.rename(tmp, SKILL_USAGE_FILE);
    } catch (err) {
      console.error(`[skill-learner] bumpSkillUsage(${skillName}, ${counterKey}) failed:`, err.message);
    }
  });
  // Errors are caught above; surface a settled promise so callers can fire-and-forget.
  return usageWriteChain;
}

const bumpSkillRead    = (n) => bumpSkillUsage(n, "read_count",    "last_read_at");
const bumpSkillApplied = (n) => bumpSkillUsage(n, "applied_count", "last_applied_at");
const bumpSkillPatched = (n) => bumpSkillUsage(n, "patch_count",   "last_patched_at");

// Match `/skills/<name>/SKILL.md` (top-level, auto-learned, or _archived/ subtree).
// Captures the immediate parent dir as the skill name.
const SKILL_MD_PATH_RE = /\/skills\/(?:auto-learned\/|_archived\/)?([^/]+)\/SKILL\.md$/;

function extractSkillNameFromPath(p) {
  if (typeof p !== "string") return null;
  const m = p.match(SKILL_MD_PATH_RE);
  return m ? m[1] : null;
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

// ─── Phase 4 (C.1.b): tool-call params capture with redaction ────────────────
// OpenClaw's after_tool_call event.params is already fully passed through — this
// helper sanitizes + truncates so we can include richer signals in evaluation
// payloads without leaking secrets or blowing up prompt budgets.
const REDACT_KEYS = /(password|secret|token|api[_-]?key|auth|private[_-]?key|credential|bearer)/i;
const MAX_STRING_LEN = 2000;
const MAX_PARAMS_BYTES = 8000; // hard cap per tool call

function sanitizeParams(params) {
  if (!params || typeof params !== "object") return null;
  const out = {};
  let totalBytes = 0;
  for (const [k, v] of Object.entries(params)) {
    if (REDACT_KEYS.test(k)) {
      out[k] = "[REDACTED]";
      continue;
    }
    let serialized;
    if (typeof v === "string") {
      serialized = v.length > MAX_STRING_LEN ? v.slice(0, MAX_STRING_LEN) + `…[+${v.length - MAX_STRING_LEN}]` : v;
    } else if (v == null || typeof v === "number" || typeof v === "boolean") {
      serialized = v;
    } else {
      try {
        const s = JSON.stringify(v);
        serialized = s.length > MAX_STRING_LEN ? s.slice(0, MAX_STRING_LEN) + `…[+${s.length - MAX_STRING_LEN}]` : s;
      } catch { serialized = "[unserializable]"; }
    }
    // Also redact if the serialized value obviously contains a bearer token shape
    if (typeof serialized === "string" && /Bearer\s+[A-Za-z0-9\-_\.]{20,}/.test(serialized)) {
      serialized = serialized.replace(/Bearer\s+[A-Za-z0-9\-_\.]{20,}/g, "Bearer [REDACTED]");
    }
    out[k] = serialized;
    totalBytes += JSON.stringify(serialized || "").length + k.length;
    if (totalBytes > MAX_PARAMS_BYTES) { out.__truncated = true; break; }
  }
  return out;
}

// Per-run tool-call trace with sanitized params (bounded ring buffer).
const TOOL_TRACE_MAX = 40;
function appendToolTrace(run, toolName, params, error, durationMs) {
  if (!run.toolTrace) run.toolTrace = [];
  if (run.toolTrace.length >= TOOL_TRACE_MAX) run.toolTrace.shift();
  run.toolTrace.push({
    name: toolName,
    params: sanitizeParams(params),
    error: error ? String(error).slice(0, 200) : null,
    durationMs: durationMs || 0,
  });
}

// ─── Phase 4 (C.1.c): sub-agent parent↔child run registry ───────────────────
// Keyed by childRunId so agent_end (fired for the child run) can discover its
// parent run context and forward a compact summary upstream.
const subagentRegistry = new Map(); // childRunId → { parentRunId, childSessionKey, agentId, spawnedAt }
const SUBAGENT_REGISTRY_MAX = 64;

function registerSubagentSpawn(childRunId, parentRunId, meta) {
  if (!childRunId) return;
  if (subagentRegistry.size >= SUBAGENT_REGISTRY_MAX) {
    const first = subagentRegistry.keys().next().value;
    subagentRegistry.delete(first);
  }
  subagentRegistry.set(childRunId, { parentRunId, spawnedAt: Date.now(), ...meta });
}

// Per parent run, accumulate child summaries (produced when the child's agent_end fires).
const parentSubagentSummaries = new Map(); // parentRunId → [ { childRunId, agentId, toolCount, summary, outcome } ]

function appendSubagentSummary(parentRunId, summary) {
  if (!parentRunId) return;
  if (!parentSubagentSummaries.has(parentRunId)) parentSubagentSummaries.set(parentRunId, []);
  const list = parentSubagentSummaries.get(parentRunId);
  if (list.length < 8) list.push(summary); // cap 8 per parent
}

// ─── Phase 4 (B.1): skill_learner_nominate tool definition ───────────────────
const NOMINATION_DIR = path.join(DATA_DIR, "nominations");
const NOMINATION_LOG = path.join(DATA_DIR, "nomination-log.jsonl");

async function writeNominationFile(runId, payload) {
  await fs.mkdir(NOMINATION_DIR, { recursive: true });
  const ts = Date.now();
  const fname = `${runId || "unknown"}-${ts}.json`;
  const full = path.join(NOMINATION_DIR, fname);
  const body = { ...payload, runId, _submittedAt: new Date().toISOString() };
  await fs.writeFile(full, JSON.stringify(body, null, 2), "utf-8");
  // Audit log (JSONL, append-only)
  try {
    await fs.appendFile(NOMINATION_LOG, JSON.stringify({ ts, runId, file: full, topic: payload.topic }) + "\n");
  } catch {}
  return { nominationId: fname.replace(/\.json$/, ""), filePath: full };
}

// Build the tool object OpenClaw registers. We attach runId from the plugin
// tool context so polyfill↔first-class share the same downstream path.
function buildNominationTool() {
  return {
    name: "skill_learner_nominate",
    label: "Skill Learner: Nominate",
    description: [
      "Mark the current session as a skill-learner nomination (Phase B self-nomination).",
      "Call exactly once at the end of a session when ANY holds:",
      "  1) you went down a wrong path and corrected yourself,",
      "  2) you combined ≥3 tools in a non-obvious sequence that worked,",
      "  3) you hit a pitfall and learned a new pattern,",
      "  4) you abandoned your first plan for a materially better approach.",
      "Do NOT call for routine tasks that follow existing skills or AGENTS.md.",
      "Honesty matters — this is a high-trust signal for the external evaluator.",
      "Max 3 nominations per run.",
    ].join(" "),
    parameters: Type.Object({
      topic: Type.String({
        description: "≤1 sentence summary of the reusable pattern (≤100 chars).",
        minLength: 1,
        maxLength: 100,
      }),
      pain_point: Type.String({
        description: "What caused the detour on this run (≤300 chars).",
        minLength: 1,
        maxLength: 300,
      }),
      reusable_pattern: Type.String({
        description: "Abstract reusable pattern, no filenames or system-specific paths (≤500 chars).",
        minLength: 1,
        maxLength: 500,
      }),
      confidence: Type.Union([
        Type.Literal("high"), Type.Literal("medium"), Type.Literal("low"),
      ], { description: "Agent self-estimated confidence." }),
      evidence_turns: Type.Optional(Type.Array(Type.Number(), {
        description: "Turn indices (0-based) pointing at key evidence. ≤8 items.",
        maxItems: 8,
      })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate) {
      // toolCtx is plumbed via closure by the factory below.
      const runId = this.__runId || "__default__";
      const run = getOrCreateRun(runId);
      if (run.nominationCount >= 3) {
        return {
          content: [{ type: "text", text: "nomination cap (3/run) reached" }],
          details: { status: "rejected", reason: "cap" },
        };
      }
      try {
        const { nominationId, filePath } = await writeNominationFile(runId, params);
        run.nominated = true;
        run.nominationCount += 1;
        run.nominationPayload = { ...params, _firstClass: true, _filePath: filePath };
        console.log(`[skill-learner] 🎯 First-class nomination: ${params.topic}`);
        return {
          content: [{ type: "text", text: `queued: ${nominationId}` }],
          details: { status: "queued", nominationId, runId },
        };
      } catch (err) {
        return {
          content: [{ type: "text", text: `nomination failed: ${err.message}` }],
          details: { status: "error", error: err.message },
        };
      }
    },
  };
}

// ─── Plugin Entry ────────────────────────────────────────────────────────────
export default definePluginEntry({
  id: "jarvis-skill-learner",
  name: "Jarvis Skill Learner",
  description: "Auto-detect reusable patterns from complex sessions and create skill drafts",

  register: (api) => {
    console.log("[skill-learner] 🧠 Plugin registered (Phase 4: SDK-Native). Hooks: after_tool_call, agent_end, session_end, subagent_spawned, subagent_ended + tool: skill_learner_nominate");

    // ── Phase 4 B.1: register skill_learner_nominate as a first-class tool ────────────
    // We use the tool factory form so every invocation can read the caller's runId
    // via the ctx passed to the factory. Each execute() closure then captures runId
    // and routes through the shared nomination writer.
    api.registerTool((toolCtx) => {
      const baseTool = buildNominationTool();
      // Attach runId via bound wrapper — execute() reads it from `this` in its body.
      const runId = toolCtx?.sessionKey ? (toolCtx.sessionKey + ":" + (toolCtx.sessionId || "")) : undefined;
      // Override execute so we can inject runId without relying on `this`.
      const origExecute = baseTool.execute;
      baseTool.execute = async function execute(toolCallId, params, signal, onUpdate) {
        // Prefer runId from current active run state if available, else fall back
        // to context-derived key. The plugin's per-run map is keyed on the hook's
        // ctx.runId at after_tool_call time, so the execute path should resolve to
        // the same run so nominated=true is observed before agent_end fires.
        const injected = toolCtx?.runId || runId || "__default__";
        const boundThis = { __runId: injected };
        return origExecute.call(boundThis, toolCallId, params, signal, onUpdate);
      };
      return baseTool;
    }, { name: "skill_learner_nominate" });

    // ── Phase 4 C.1.c: sub-agent lifecycle tracking ──────────────────────────────
    // subagent_spawned: build parent↔child map. The child's own agent_end hook will
    // fire independently when it finishes; at that point we'll look up the parent.
    api.on("subagent_spawned", (event, ctx) => {
      const childRunId = event.runId || ctx?.runId;
      const parentRunId = ctx?.requesterSessionKey || null; // requester side
      if (!childRunId) return;
      registerSubagentSpawn(childRunId, parentRunId, {
        childSessionKey: event.childSessionKey,
        agentId: event.agentId,
        label: event.label || null,
        mode: event.mode,
      });
      console.log(`[skill-learner] 👶 Subagent spawned: child=${childRunId} parent=${parentRunId} agent=${event.agentId}`);
    });

    // subagent_ended: capture outcome + attach outcome to the child's summary slot.
    // The actual transcript summary is built when the child's agent_end fires;
    // here we only record the terminal outcome so we know whether it succeeded.
    api.on("subagent_ended", (event, ctx) => {
      const childRunId = event.runId || ctx?.runId;
      if (!childRunId) return;
      const reg = subagentRegistry.get(childRunId);
      if (!reg) return;
      reg.outcome = event.outcome || "unknown";
      reg.endedAt = event.endedAt || Date.now();
      reg.error = event.error || null;
      console.log(`[skill-learner] 👴 Subagent ended: child=${childRunId} outcome=${reg.outcome}`);
    });

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
      // Phase 4 C.1.b: capture sanitized params into a per-run trace for later inclusion
      // in the evaluate payload. Plugin already received full params via after_tool_call,
      // we just weren't using them. Secrets redacted, strings capped.
      appendToolTrace(run, event.toolName, event.params, event.error, event.durationMs);
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
        const skillName = extractSkillNameFromPath(event.params?.path || "");
        if (skillName) {
          if (!runSkillsUsed.has(runId)) runSkillsUsed.set(runId, new Set());
          runSkillsUsed.get(runId).add(skillName);
          // Track for friction correlation
          run.lastSkillReadIndex = tcIndex;
          run.lastSkillReadName = skillName;
          // Track 4: bump read_count (fire-and-forget; serialized via usageWriteChain)
          bumpSkillRead(skillName);
          console.log(`[skill-learner] 📖 Skill loaded: ${skillName}`);
        }
      }

      // Track 4: detect skill patches via Write/Edit on SKILL.md
      if (event.toolName === "Write_tool" || event.toolName === "write" ||
          event.toolName === "write_tool" || event.toolName === "Edit_tool" ||
          event.toolName === "edit") {
        const patchedSkill = extractSkillNameFromPath(event.params?.path || event.params?.file_path || "");
        if (patchedSkill) {
          bumpSkillPatched(patchedSkill);
          console.log(`[skill-learner] ✏️  Skill patched: ${patchedSkill}`);
        }
      }

      // ── Phase B.2: detect agent self-nomination ───────────────────────────
      // Two routes:
      //   (1) First-class tool: `skill_learner_nominate` (B.1 — needs OpenClaw support).
      //   (2) Polyfill: agent writes a file directly to `data/skill-learner/nominations/`
      //       via `write` / `exec` — unblocks Phase B while B.1 ships.
      if (event.toolName === "skill_learner_nominate") {
        const payload = event.params || {};
        // Hard cap: 3 nominations per run (defense against noisy agents)
        if (run.nominationCount >= 3) {
          console.log(`[skill-learner] ⚠️ Nomination rejected: cap reached (3/run) for ${runId}`);
        } else {
          run.nominated = true;
          run.nominationPayload = payload;
          run.nominationCount += 1;
          console.log(`[skill-learner] 🎯 Agent self-nominated: ${payload.topic || "(untitled)"}`);
        }
      } else if (
        (event.toolName === "write" || event.toolName === "write_tool" ||
         event.toolName === "Write_tool" || event.toolName === "edit" ||
         event.toolName === "exec") &&
        typeof event.params?.path === "string" &&
        event.params.path.includes("/data/skill-learner/nominations/") &&
        event.params.path.endsWith(".json")
      ) {
        // Polyfill detection — can't read the file contents here (params is post-tool output),
        // so just mark the run as nominated; the server will pick up the file from disk.
        run.nominated = true;
        run.nominationCount += 1;
        run.nominationPayload = run.nominationPayload || {
          topic: "(polyfill: written via file)",
          _polyfill: true,
          _filePath: event.params.path,
        };
        console.log(`[skill-learner] 🎯 Nomination polyfill detected: ${event.params.path}`);
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

      // ── Track 4: bump applied_count for skills used in this run ─────────────
      // Fires regardless of whether the run hits TOOL_CALL_THRESHOLD (15) — the
      // applied threshold (5) is lower so we capture lightweight skill uses too.
      if (count >= APPLIED_TOOLCALL_THRESHOLD) {
        const skillsThisRun = runSkillsUsed.get(runId);
        if (skillsThisRun && skillsThisRun.size > 0) {
          for (const skillName of skillsThisRun) {
            bumpSkillApplied(skillName);
          }
        }
      }

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

      // Phase 4 C.1.c: if this run is a sub-agent run, push a compact summary up
      // to the parent run's queue (so the parent evaluation later sees what the
      // child did) instead of emitting a separate evaluation for the child alone.
      const subagentReg = subagentRegistry.get(runId);
      if (subagentReg && subagentReg.parentRunId) {
        appendSubagentSummary(subagentReg.parentRunId, {
          childRunId: runId,
          agentId: subagentReg.agentId || ctx.agentId,
          mode: subagentReg.mode,
          toolCount: count,
          toolNames: [...new Set(run.toolCalls.map(t => t.name))],
          userMessages: (summary?.userMessages || []).slice(0, 3),
          assistantTexts: (summary?.assistantTexts || []).slice(0, 3),
          outcome: subagentReg.outcome || "unknown",
          error: subagentReg.error || null,
        });
        console.log(`[skill-learner] ↗️  Sub-agent summary forwarded to parent ${subagentReg.parentRunId}`);
        // Don't emit a standalone evaluation for the child run — parent is the
        // canonical context. Mark to skip session_end fallback too.
        agentEndFiredHttp.add(runId);
        return;
      }

      // Pull any sub-agent summaries accumulated under this (parent) runId.
      const childSummaries = parentSubagentSummaries.get(runId) || [];
      if (childSummaries.length > 0) {
        console.log(`[skill-learner] 📊 Including ${childSummaries.length} sub-agent summaries in parent eval`);
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
        // Phase 4 C.1.b: sanitized tool-call trace (params + errors) for richer signals
        // Hard-capped at TOOL_TRACE_MAX entries; secrets redacted; strings truncated.
        toolTrace: run.toolTrace || [],
        // Phase C: best-effort forward of session JSONL path so the evaluator can
        // optionally read the full transcript. When OpenClaw exposes it in agent_end
        // (C.1 roadmap), this lights up automatically; until then it's usually null.
        sessionFile: (ctx && (ctx.sessionFile || ctx.session_file)) || event?.sessionFile || null,
        // Track 1: friction signals for skill evolution
        frictionSignals: friction.signals,
        frictionWeight: friction.totalWeight,
        frictionSkill: friction.targetSkill,
        triggerEvolution,
        // Track 2: correction signals for user modeling
        correctionSignals,
        // Phase B: agent self-nomination (first-class via tool, polyfill via file write)
        nominated: !!run.nominated,
        nominationPayload: run.nominationPayload || null,
        // Phase 4 C.1.c: sub-agent summaries collected under this parent run
        subagentSummaries: childSummaries,
      };

      // Mark so session_end skips re-queuing
      agentEndFiredHttp.add(runId);

      // Cleanup parent's child-summary slot
      parentSubagentSummaries.delete(runId);

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
      console.log("[skill-learner] 🧠 Skill Learner Phase 4 active. Threshold: ≥" + TOOL_CALL_THRESHOLD + " tool calls | tool: skill_learner_nominate | hooks: subagent_spawned/ended | params redaction ON.");
    });
  },
});
