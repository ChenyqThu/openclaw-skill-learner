# Phase 4 对接报告 — OpenClaw 侧完成情况

> **作者**：Jarvis (OpenClaw 侧)
> **时间**：2026-04-17 PT
> **范围**：Skill Learner Phase 4 的 plugin SDK 原生对接
> **对接人**：Claude Code（skill-learner 项目重构方）

---

## 0. 核心结论（TL;DR）

**先前 `OPENCLAW_COOPERATION_PHASE2.md` 的"必须等 OpenClaw 平台 PR"判断是错的。** OpenClaw 的 plugin SDK 在当前版本（`openclaw@2026.4.15`）已经**完整提供**了 B.1 / C.1.b / C.1.c 所需要的全部接口，只需要 skill-learner 的 plugin 代码正确调用即可。不需要任何上游 PR。

已在 OpenClaw 侧完成这三项的 plugin 端实现和验证，skill-learner 项目（evaluate-server.py 等）需要配套接收新字段。

---

## 1. OpenClaw Plugin SDK 实际能力盘点

### 1.1 源码证据

- Plugin SDK 入口：`/opt/homebrew/lib/node_modules/openclaw/dist/plugin-sdk/`
- Hook 事件枚举：`plugin-sdk/src/plugins/hook-types.d.ts` `PluginHookName` 类型
- Plugin API：`plugin-sdk/src/plugins/types.d.ts` `OpenClawPluginApi`
- 工具类型：`node_modules/@mariozechner/pi-agent-core/dist/types.d.ts` `AgentTool`

### 1.2 关键能力

| 能力 | SDK 位置 | 是否需要 PR |
|---|---|---|
| **注册 agent 工具** | `api.registerTool(tool, opts)` | ❌ 不需要 |
| **`after_tool_call.event.params` 全量参数** | `PluginHookAfterToolCallEvent.params: Record<string, unknown>` | ❌ 不需要 — 一直是全量透传的 |
| **`before_tool_call` 可阻塞/改参数** | `PluginHookBeforeToolCallResult.params / block` | ❌ 不需要 |
| **`subagent_spawning` hook（可阻止）** | `PluginHookSubagentSpawningEvent` | ❌ 不需要 |
| **`subagent_spawned` hook（获 runId）** | `PluginHookSubagentSpawnedEvent { runId, childSessionKey, agentId, requester, mode }` | ❌ 不需要 |
| **`subagent_ended` hook** | `PluginHookSubagentEndedEvent { outcome, runId, childSessionKey, requesterSessionKey }` | ❌ 不需要 |
| **`agent_end.messages` 全量 transcript** | `PluginHookAgentEndEvent.messages: unknown[]` | ❌ 不需要 — 一直全量 |
| **`session_end.sessionFile` JSONL 路径** | `PluginHookSessionEndEvent.sessionFile?: string` | ❌ 不需要 |

### 1.3 `OPENCLAW_COOPERATION_PHASE2.md` 判断修正表

| 条目 | 原判断 | 实际 | 修正说明 |
|---|---|---|---|
| **B.1 first-class tool** | P0，需 OpenClaw PR 3-6h | ✅ 本次 plugin 侧 ~30 行代码完成 | `api.registerTool` 接受 `AgentTool` 对象或 factory |
| **C.1.a `skill_considered_rejected`** | P1，弱 polyfill | 仍需 PR 或 agent 协作标记 | 本质是 agent 内部决策，不可观测 |
| **C.1.b params 透传** | P2，需 PR | ✅ 已全量透传，plugin 没读而已 | plugin 只消费了 `Read_tool.path`，其他工具的 params 被丢弃 |
| **C.1.c sub_agent events** | P1，需 PR | ✅ 三个 hook 全有 | `subagent_spawning/spawned/ended` |
| **D headless CLI** | P2，optional | 暂未探索 agent harness 注册接口 | 需要后续评估 `registerAgentHarness` 的可行性 |

---

## 2. 本次改动清单（~/Projects/openclaw-skill-learner）

### 2.1 `plugin/package.json`

新增 typebox 依赖（用于 `Type.Object` schema）：

```diff
-  "version": "1.0.0",
+  "version": "1.1.0",
+  "dependencies": {
+    "@sinclair/typebox": "0.34.49"
+  },
```

> 注意：`openclaw plugins install` 会自动调 `npm install` 把依赖装到 `~/.openclaw/extensions/jarvis-skill-learner/node_modules/`。

### 2.2 `plugin/index.js`

**行数变化**：546 → 915（+369 行，无删除），`git diff --stat`: `+276 -0`（有些是行 wrap）。

**新增功能模块**：

#### (a) params 脱敏工具（~50 行）
```js
const REDACT_KEYS = /(password|secret|token|api[_-]?key|auth|private[_-]?key|credential|bearer)/i;
const MAX_STRING_LEN = 2000;
const MAX_PARAMS_BYTES = 8000;

function sanitizeParams(params) { /* ... */ }
function appendToolTrace(run, toolName, params, error, durationMs) { /* ring buffer, cap 40 */ }
```

- Key 匹配脱敏：`password` / `secret` / `token` / `api_key` / `auth` / `private_key` / `credential` / `bearer`
- Value 内嵌 `Bearer <token>` 替换为 `Bearer [REDACTED]`
- 单字段超 2000 chars 截断加 `…[+N]` 标记
- 单 payload 超 8KB 标记 `__truncated: true` 停止追加
- 每 run 最多保存 40 条 toolTrace（ring buffer）

#### (b) 子 agent parent↔child registry（~25 行）
```js
const subagentRegistry = new Map();       // childRunId → { parentRunId, agentId, outcome, ... }
const parentSubagentSummaries = new Map();// parentRunId → [childSummary, ...]
function registerSubagentSpawn(childRunId, parentRunId, meta) { /* cap 64 */ }
function appendSubagentSummary(parentRunId, summary) { /* cap 8/parent */ }
```

#### (c) nomination 文件 & audit log helper（~20 行）
```js
const NOMINATION_DIR = path.join(DATA_DIR, "nominations");
const NOMINATION_LOG = path.join(DATA_DIR, "nomination-log.jsonl");
async function writeNominationFile(runId, payload) { /* 返回 {nominationId, filePath} */ }
```

#### (d) `skill_learner_nominate` 工具定义（~60 行）
- typebox schema（`topic` ≤100, `pain_point` ≤300, `reusable_pattern` ≤500, `confidence` union, `evidence_turns?` array<=8）
- `execute()` 写 `nominations/<runId>-<ts>.json` + JSONL 审计 + 标记 `run.nominated=true`
- Hard cap 3/run，超过返回错误字符串
- 注册时用 factory 形式，通过 toolCtx 注入 runId

#### (e) 工具注册（`api.registerTool` 调用）
```js
api.registerTool((toolCtx) => {
  const baseTool = buildNominationTool();
  // wrap execute 以从 toolCtx 注入 runId
  baseTool.execute = async function(toolCallId, params, signal, onUpdate) {
    const injected = toolCtx?.runId || /* ... */ "__default__";
    return origExecute.call({__runId: injected}, toolCallId, params, signal, onUpdate);
  };
  return baseTool;
}, { name: "skill_learner_nominate" });
```

#### (f) subagent hook 订阅（~30 行）
```js
api.on("subagent_spawned", (event, ctx) => {
  registerSubagentSpawn(event.runId, ctx?.requesterSessionKey, {
    childSessionKey: event.childSessionKey,
    agentId: event.agentId,
    label, mode,
  });
});
api.on("subagent_ended", (event, ctx) => {
  const reg = subagentRegistry.get(event.runId);
  if (reg) { reg.outcome = event.outcome; reg.endedAt = event.endedAt; /* ... */ }
});
```

#### (g) `after_tool_call` 扩展
```js
run.toolCalls.push({ ... });
appendToolTrace(run, event.toolName, event.params, event.error, event.durationMs);  // <-- 新增
recordToolUsage(...);
```

#### (h) `agent_end` payload 扩展
```js
// 如果这是一个 sub-agent 的 agent_end：forward summary 到 parent 后 return（不再独立 fire eval）
const subagentReg = subagentRegistry.get(runId);
if (subagentReg?.parentRunId) {
  appendSubagentSummary(subagentReg.parentRunId, {
    childRunId, agentId, mode, toolCount, toolNames,
    userMessages: first3, assistantTexts: first3,
    outcome, error,
  });
  agentEndFiredHttp.add(runId);
  return;  // <-- 避免重复评估
}

// Parent run: 把累积的 child summaries 一起塞进 payload
const childSummaries = parentSubagentSummaries.get(runId) || [];

const payload = {
  // ... 现有字段
  toolTrace: run.toolTrace || [],        // <-- 新增 (C.1.b)
  subagentSummaries: childSummaries,     // <-- 新增 (C.1.c)
  nominated: !!run.nominated,
  nominationPayload: run.nominationPayload || null,
};
parentSubagentSummaries.delete(runId);   // cleanup
```

**polyfill 路径完整保留**：Write_tool / exec 写到 `nominations/*.json` 依然被 `after_tool_call` 捕获并标记 `nominated=true`。现在等于有**两条冗余路径**（first-class 优先，polyfill 兜底）。

---

## 3. 端到端验证结果

### 3.1 Plugin 加载
```
[skill-learner] 🧠 Plugin registered (Phase 4: SDK-Native). Hooks: after_tool_call,
   agent_end, session_end, subagent_spawned, subagent_ended + tool: skill_learner_nominate
[skill-learner] 🧠 Skill Learner Phase 4 active. Threshold: ≥15 tool calls |
   tool: skill_learner_nominate | hooks: subagent_spawned/ended | params redaction ON.
```

### 3.2 B.1 `skill_learner_nominate` 调用

Agent（我）在对话中直接调用 `skill_learner_nominate({ topic, pain_point, reusable_pattern, confidence, evidence_turns })`，返回：
```
queued: agent:jarvis:feishu:direct:ou_...:4a9ceb79-...-1776460994013
```

验证：
- ✅ 文件落盘到 `data/skill-learner/nominations/<runId>-<ts>.json`
- ✅ `nomination-log.jsonl` 追加审计行
- ✅ Plugin log 打出 `🎯 First-class nomination: <topic>`
- ✅ `run.nominated = true` 设置成功（execute 闭包内改 runStats）

### 3.3 C.1.b params 脱敏单测

独立 node 脚本 7/7 通过：
- ✅ 按 key 脱敏（password, api_key）
- ✅ 按 value 脱敏（Bearer <token>）
- ✅ 超长字符串截断（2500→2000+标记）
- ✅ 嵌套对象 JSON 化
- ✅ null/number/boolean 直通
- ✅ 字节总额 cap 触发 `__truncated: true`

### 3.4 C.1.c subagent_spawned hook

派了一个 jarvis-exec 子任务，log 出现：
```
[skill-learner] 👶 Subagent spawned: child=c5534c55-18ab-4826-9d8c-362a8d4445f4
   parent=agent:jarvis:feishu:direct:ou_8d1ce0fa1d435070ed695baeabe25adc
   agent=jarvis-exec
```

✅ parent↔child runId 映射建立成功。

**未完全测到的路径**（需要 tool-heavy 子任务）：
- 子 run 自己的 `agent_end` 触发 → 查 `subagentRegistry` → 写入 `parentSubagentSummaries[parent]` → 父 `agent_end` 时把 summaries 塞进 payload
- 代码路径已在，逻辑审查过，未观察到实际触发（本次测试子任务 toolCount=0 被 THRESHOLD 过滤）

### 3.5 向后兼容

Polyfill 路径（写文件到 `nominations/`）仍会被 `after_tool_call` 检测到：
```
[skill-learner] 🎯 Nomination polyfill detected: .../test-polyfill-smoke-20260417.json
```

---

## 4. Skill Learner 侧需要配套的改动（交给 Claude Code）

Plugin 已经在 `agent_end` 的 HTTP payload 里新增两个字段，但 `evaluate-server.py` 当前**不消费它们**（会被静默丢弃）。以下是建议改动清单：

### 4.1 `scripts/evaluate-server.py` — 接收新字段

当前 `handle_evaluate` 把 body 映射到 request dict 时只拷贝认识的字段（见 L176-191）。需要新增：

```python
# 在 "nominationPayload" 后追加：
"toolTrace": body.get("toolTrace", []),           # Phase 4 C.1.b
"subagentSummaries": body.get("subagentSummaries", []),  # Phase 4 C.1.c
```

### 4.2 Prompt 层（`scripts/prompts/v3_balanced.py` 或更新的 prompt 版本）

把 toolTrace / subagentSummaries 编入 Gemini prompt：

- **toolTrace**：按顺序列出工具名 + 参数关键字段 + 错误。用来判断"工具调用序列是否构成可复用模式"。
- **subagentSummaries**：当父 run 有子 run 时，把子 run 的 toolCount / toolNames / 前 3 条 userMessages+assistantTexts 作为"扩展上下文"塞进 prompt。解决了之前"父 run 看不到 sessions_spawn 的子任务内容"的盲区。

### 4.3 Nomination payload 标记

First-class nomination 的 `nominationPayload` 现在包含 `_firstClass: true`（但磁盘文件不包含此字段，只在 HTTP payload 里）。
Prompt 层可以用这个标记**区分 first-class vs polyfill**：
- `_firstClass: true` → Gemini prompt 里用"完整 nomination"块（含 topic/pain/pattern/confidence）
- `_polyfill: true` → 降级"agent 主动写了文件但 payload 未捕获"（旧逻辑，保留）

### 4.4 `plugin/package.json` 版本号

我已经把 plugin 版本从 `1.0.0` 升到 `1.1.0`。如果 skill-learner 项目有独立的 release 流程，需要同步 CHANGELOG。

### 4.5 验收 checklist（Claude Code 用）

- [ ] evaluate-server 能接收并持久化 toolTrace / subagentSummaries 字段（看 `analysis-queue/*.json`）
- [ ] v3 prompt 有至少一个版本消费了 toolTrace（可以先加 `PROMPT_VERSION=v4_toolcall_trace` 开关）
- [ ] v3 prompt 对 `_firstClass: true` 的 nominationPayload 走高信任块
- [ ] Docs（`OPENCLAW_INTEGRATION.md` / `OPENCLAW_COOPERATION_PHASE2.md`）更新判断表，标注"B.1/C.1.b/C.1.c = DONE via plugin SDK"
- [ ] `ARCHITECTURE.md` 的 Phase 4 描述更新，反映"不再等上游，全部在 plugin 内完成"

---

## 5. 仍未解决/可选项

### 5.1 C.1.a `skill_considered_rejected`
本质是 agent 内部决策过程，OpenClaw runtime 不会 emit。两种路径：
- **路径 A（最小代价）**：AGENTS.md 新增"考虑但没加载 skill 时写 `considered-rejected.jsonl`"协议，agent 自己标记
- **路径 B（较重）**：在 plugin 里加 `skill_consider_note` 工具，agent 可主动调用来声明"考虑了 skill X 但不匹配"
- 建议路径 A 先跑，数据稀疏再评估 B

### 5.2 D Headless Jarvis
OpenClaw plugin SDK 有 `registerAgentHarness`（`types.d.ts:1628`），理论上可以从 plugin 侧注册一个 ephemeral agent harness。未探索，评估工作量需额外半天。
- **polyfill 路径**：skill-learner 已预留 shell out 到 `claude-code` 的接口，可先用这个跑 Phase D
- **原生路径**：等 Phase D replay gate 产品化后再做

### 5.3 `openclaw tools list` 不可见
`openclaw tools` CLI 命令目前被 `plugins.allow` 排除，看不到 `skill_learner_nominate` 注册详情。不影响工具调用（agent 侧已可用），只是调试时看不到。需要在 `~/.openclaw/openclaw.json` 的 `plugins.allow` 里加 `"tools"`。

---

## 6. 文件清单（本次改动）

| 文件 | 改动 | 说明 |
|---|---|---|
| `~/Projects/openclaw-skill-learner/plugin/index.js` | +276 -0 | Phase 4 SDK-native 实现 |
| `~/Projects/openclaw-skill-learner/plugin/package.json` | 加 typebox 依赖 | 工具 schema 需要 |
| `~/Projects/openclaw-skill-learner/plugin/package-lock.json` | 新文件 | npm install 产物 |
| `~/Projects/openclaw-skill-learner/docs/PHASE_4_OPENCLAW_INTEGRATION_REPORT.md` | 新文件 | 本报告 |

**部署状态**：Plugin 已通过 `openclaw plugins install` 重装到 `~/.openclaw/extensions/jarvis-skill-learner/`，gateway 已重启，Phase 4 active。

**非本次改动但相关**：
- `~/Projects/openclaw-skill-learner/scripts/evaluate-server.py` — 上午单独修过 3 处 `timeout=20` → `timeout=60`（卡片通知 timeout 修复，与 Phase 4 正交）

---

## 7. 联系 & 澄清

有任何不清楚的接口语义、实际 hook 行为、payload 格式细节，直接在 plugin 代码里搜对应 `Phase 4` 注释块，都标注了用途和约束。

关键约束（Claude Code 重构时请勿破坏）：
1. **Plugin 零 child_process**：所有外发走 HTTP localhost:8300，不 spawn 进程
2. **Nomination hard cap 3/run**：reject 要返回错误字符串，**不要**静默成功
3. **toolTrace ring buffer 上限 40**：超了就 shift，不要无限累加
4. **子 run 的 agent_end 不独立 fire eval**：必须 forward 到父 run 后 `return`，否则重复评估
5. **polyfill 路径保留**：`after_tool_call` 里文件写入检测代码**不要删**，向后兼容
