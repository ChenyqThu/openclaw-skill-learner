# OpenClaw 侧协作需求 — Phase B / C / D

**对象**：OpenClaw 平台开发 team
**目的**：历史文档 — 记录 Phase 4 初期以为"必须等 OpenClaw 上游 PR"的能力，实际大半在 plugin SDK 里直接能做。

> **⚡ 2026-04-17 重大修正**：先前"需要 OpenClaw 平台 PR"的判断是错的。OpenClaw 的 plugin SDK 在当前版本（`openclaw@2026.4.15`）已经提供了 B.1 / C.1.b / C.1.c 所需要的全部接口 — `api.registerTool`、`after_tool_call.event.params` 已经全量透传、`subagent_spawned/ended` 三个 hook 全有。详见 `PHASE_4_OPENCLAW_INTEGRATION_REPORT.md`。

## 状态速览 (2026-04-17 修正版)

| 项 | 原先判断 | 当前状态 | 证据 |
|---|---|---|---|
| Plugin Phase 2→3 同步 | — | ✅ 完成 | runtime 路径是 `~/.openclaw/extensions/jarvis-skill-learner/`（不是 `~/.openclaw/plugins/`） |
| AGENTS.md §六 自我提名协议 | — | ✅ 完成 | Jarvis 侧已注入 |
| 日记四类信号 cron prompt | — | ✅ 完成 | Jarvis 侧已注入 |
| **B.1 first-class tool** | P0 需 PR | ✅ **DONE via plugin SDK** | `api.registerTool()` + typebox schema → 30 行 plugin 代码搞定。agent 可直接调用 `skill_learner_nominate`，payload 带 `_firstClass: true` |
| **C.1.b params 全透传** | P2 需 PR | ✅ **DONE via plugin SDK** | `after_tool_call.event.params` 一直是 `Record<string, unknown>` 全透传，只是 plugin 以前自己没读。新增 `sanitizeParams` 脱敏 + `appendToolTrace` 环形缓冲 (cap 40) |
| **C.1.c sub_agent hooks** | P1 需 PR | ✅ **DONE via plugin SDK** | `subagent_spawned` / `subagent_ended` 两个 hook 都在。plugin 建立 parent↔child runId 映射，子 run 的 `agent_end` 把 summary forward 到父 run 的 HTTP payload |
| C.1.a `skill_considered_rejected` | P3 需 PR | 🟢 **任务已下发 Jarvis** | AGENTS.md 协议路径,周回顾补标 `[考虑未用]` 标记。详见 [PHASE_4_1_C1A_AGENT_PROTOCOL_TASK.md](PHASE_4_1_C1A_AGENT_PROTOCOL_TASK.md) |
| D Headless Jarvis CLI | P4 optional | ✅ **DONE via claude-code shell-out** | `HeadlessJarvisClient` 用 `claude --bare --print --output-format stream-json --append-system-prompt` 实现,端到端测过 (positive/negative 区分正常,1.7s/run,~$0.02)。`api.registerAgentHarness` 原生路径留作后续升级 |

以下章节保留当时的规格原文作为历史记录 + 实现参考。已落地的项在章节标题加了 `[DONE]` 前缀指向 plugin 实现。

---

## B.1 — `skill_learner_nominate` 工具 `[DONE via plugin SDK]`

**原判断**：P0（决定 Phase B 的核心价值兑现）
**实际**：✅ 已在 plugin 内用 `api.registerTool()` 注册完成。不需要 OpenClaw 上游改动。Agent 调用工具即返回 `{queued: <nominationId>}`，文件落盘到 `data/skill-learner/nominations/` + `nomination-log.jsonl` 审计。payload 带 `_firstClass: true` 区分自 polyfill。下文规格保留作为接口参考。

### 工具规格

Tool name: `skill_learner_nominate`
Visibility: Jarvis 主会话 + 子 agent (`jarvis-exec` 等) 都应可见
Side effects: 写一个 JSON 文件 + 返回 ack

### 参数

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `topic` | string | required, ≤100 chars | 一句话概括可复用模式 |
| `pain_point` | string | required, ≤300 chars | 本次走弯路的触点 |
| `reusable_pattern` | string | required, ≤500 chars | 抽象模式（不含具体文件名/系统） |
| `evidence_turns` | number[] | optional, ≤8 items | 关键 turn 的 index |
| `confidence` | `"high" \| "medium" \| "low"` | required | agent 自估置信度 |
| `session_id` | string | optional | plugin/ctx 自动填 |
| `run_id` | string | optional | plugin/ctx 自动填 |

### 返回

```ts
{ queued: true, nominationId: string }
```

空字段或超长字段 → 返回错误字符串，让 agent 看到 `"nominate 需要 topic/pain_point/reusable_pattern 非空"`。**不要静默成功**，让 agent 学会什么时候该调。

### 实现

1. 落盘到 `~/.openclaw/workspace/data/skill-learner/nominations/{runId}-{timestamp}.json`，payload + timestamp 一起写。
2. 审计日志：追加一行到 `nomination-log.jsonl`。
3. 单 run 上限 3 次（超出返回错误 `"nomination cap (3/run) reached"`）。

### 为什么是 tool 而不是 hook

Tool 是 agent 主动选择调用的 — 自证即信号。Hook 是被动触发。Nomination 的核心价值在「agent 觉得我刚才做了值得沉淀的事」，这必须是意图，不是推断。

### Polyfill 降级（已可用）

AGENTS.md 的「自我提名协议」给了 agent 一条兼容路径：在工具不存在时 `exec` 一行 shell 写 JSON 文件：

```bash
echo '{"topic":"...","pain_point":"...","reusable_pattern":"...","confidence":"medium","evidence_turns":[5,12]}' > ~/.openclaw/workspace/data/skill-learner/nominations/${RUN_ID}-$(date +%s).json
```

Plugin（`plugin/index.js`）已经能检测这种写入并把 `nominated=true` 打进 payload，所以评估器侧的 gate 立即生效。

但 polyfill 有两个缺点：
- agent 需要自己知道当前 runId（可以从 env 或 ctx 拿，但需要文档提示）
- Plugin 检测到文件写入时**无法读到文件内容**（`after_tool_call.params` 只有 path），所以 payload 标记为 `_polyfill: true`，prompt 走降级块。

所以 polyfill 能让 gate 工作，但高信任信号的完整形态（topic/pain/pattern 全出现在 Gemini prompt 里）需要 B.1 tool 落地才能达到。

### 验收

1. agent 主动调用 `skill_learner_nominate({topic, pain_point, reusable_pattern, confidence})` → 返回 `{queued: true, nominationId}`。
2. 必填字段空 → 返回错误字符串。
3. 同 run 第 4 次调用 → 返回错误字符串。
4. plugin 的 gateway log 打出 `🎯 Agent self-nominated: ...`。
5. 评估器 log 打出 `Gate open: nominated`。

### 估计工作量

OpenClaw team：3-6 小时（工具注册 + 参数校验 + 文件写入 + 审计日志）。

---

## C.1 — 三项 Hook 扩展（填补关键盲区）

**原判断**：P1（B 落地后才有意义）
**实际**：C.1.b 和 C.1.c 已在 plugin SDK 内完成；C.1.a 仍需 agent 协作或新工具。

### C.1.a `skill_considered_rejected` `[仍需 agent 协作]`

**用途**：agent 考虑了某个 skill 但决定不加载 — 这是现在完全不可见的「负证据」。

**调用方式**：agent 端新增一个 tool `skill_mark_considered_rejected(candidate, reason)`，或者在 skill 路由内部自动 emit。

**Payload**：
```ts
{
  candidate: string,       // 考虑的 skill 名
  reason: string,          // 为什么没加载 — "partial match only", "user request doesn't fit when_to_use", ...
  sessionId: string,
  runId: string,
}
```

**Plugin 消费**：累积到 `run.skillsConsideredRejected: []`，在 `agent_end` payload 增补该字段。

**价值**：当 agent 后来踩坑时，我们可以反问 "你考虑过 skill X 吗？" 如果答案是 "考虑过但觉得不匹配"，那就是 skill X 的 `when_to_use` 描述得太窄 → Track 1 Darwin 进化的明确驱动信号。

### C.1.b `after_tool_call.params` 全透传 + 脱敏 `[DONE via plugin SDK]`

**实际**：OpenClaw `PluginHookAfterToolCallEvent.params` 一直是 `Record<string, unknown>` 全透传，只是 plugin 以前自己没读。已新增 `sanitizeParams()`（REDACT_KEYS 正则 + `Bearer <token>` 替换 + 2000 字截断 + 8 KB 总 cap）+ `appendToolTrace()`（ring buffer cap 40）。payload 新增 `toolTrace` 数组，evaluator 端 `_build_tool_trace_note` 已消费。下文原计划保留作脱敏设计参考。


**现状**：plugin 只能读到 Read_tool 的 `params.path`。其他工具（exec/write/edit）的参数完全不可见。

**需求**：把 `event.params` 全量透传给 plugin，只做秘密脱敏：

```js
// 脱敏白名单（拒绝的 key 名 regex）
const REDACT_KEYS = /password|secret|token|api_key|auth|private_key/i;
// 字符串值上限
const MAX_STRING_LEN = 2000;
```

**为什么**：prompt engineering 上差一个数量级。举例：
- exec 调用 `git status` vs `rm -rf /tmp/cache` — 二者语义完全不同，agent reasoning 能看到但评估器看不到
- write 写到 `skills/` vs `logs/` — 重要性差十倍
- edit 做的是 typo 修复还是架构变更 — 现在全都等价

### C.1.c `sub_agent_spawn` / `sub_agent_complete` `[DONE via plugin SDK]`

**实际**：OpenClaw plugin SDK 提供 `subagent_spawning` / `subagent_spawned` / `subagent_ended` 三个 hook。Plugin 已注册 `subagent_spawned`（建立 parent↔child runId 映射）+ `subagent_ended`（记录 outcome / error）。子 run 的 `agent_end` 触发后 forward summary 到 `parentSubagentSummaries[parentRunId]`，父 run 的 HTTP payload 新增 `subagentSummaries` 数组。evaluator 端 `_build_subagent_note` 已消费。下文原计划保留作事件命名参考。


**现状**：当 jarvis 调 `sessions_spawn` 给 jarvis-exec 干活，子 session 的 transcript 完全隔离，plugin 只看到父 session 的那一个工具调用。对一个需要长链路的 skill 来说，这是最大盲区。

**需求**：
```ts
sub_agent_spawn({
  parentRunId: string,
  childRunId: string,
  childAgentId: string,        // "jarvis-exec" | "jarvis-daily" | ...
  task: string,                // spawn 时的指令摘要
})

sub_agent_complete({
  parentRunId: string,
  childRunId: string,
  messages: Message[],         // 完整 transcript（或 summary）
  exitStatus: "ok" | "error" | "timeout",
  durationMs: number,
})
```

**Plugin 消费**：`run.subAgentRuns: [{...}]` 持久化，`agent_end` payload 增补。

### Polyfill 降级

- **C.1.a**：无法 polyfill — 本质是 agent 内部决策过程。AGENTS.md 可以要求 agent 手动写 `considered-rejected.jsonl`，但信号质量会打折。
- **C.1.b**：plugin 可以改为读 `~/.openclaw/logs/gateway.log`（已记录原始 tool 参数）做后处理，但实时性和完整性差。
- **C.1.c**：plugin 可以监听文件系统变化（`~/.openclaw/sandboxes/agent-*/`）+ 读 sandbox 里的 session 文件，但非常脆弱且易碰触 OpenClaw 内部实现。

---

## D — Headless Jarvis 模式 `[DONE via claude-code shell-out]`

**实际**：Phase D 的 `HeadlessJarvisClient` 已经用 `claude` CLI shell-out 实现（`scripts/replay_gate.py`）。命令形态:
`claude --bare --print --output-format stream-json --verbose --append-system-prompt <SKILL.md block> --disallowedTools Bash,Write,Edit,WebFetch,WebSearch --max-budget-usd 0.05 <prompt>`

端到端测过(2026-04-17):匹配 prompt → `skill_loaded: True`;不匹配 prompt → `skill_loaded: False`;1.7s/run,$0.02 平均成本。stream-json 解析器抽取 `tool_use` 事件构成 trajectory,通过 `Loading skill: <name>` 文本标记确认加载。

原生 `api.registerAgentHarness` 路径留作后续升级(类型定义在 plugin-sdk/src/plugins/types.d.ts:1628),当前 shell-out 已足以支撑 Phase D 验证场景。

---

**原规格**:

**优先级**：P2（Phase D 回放校验 gate 的基础）

**需求**：暴露一个 CLI 或 API，可以给定 `(skills_dir_override, prompt)` 跑一次 ephemeral Jarvis session，返回完整 transcript 或至少 tool trajectory。

**用法**：
```bash
openclaw jarvis-headless \
  --skills-dir /tmp/replay-sandbox/skills \
  --prompt "帮我分析这三个源的故障" \
  --timeout 60 \
  --output-jsonl /tmp/replay-42.jsonl
```

**价值**：Gemini 提出 skill 草稿 → 自动从原 session 派生 3-5 个测试 prompt → 跑 headless Jarvis → 检查新 skill 是否真的被加载 + tool trajectory 是否符合预期 → 通过才进审批卡。

**Polyfill 降级**：我方可以 shell out 到 `claude-code`（不同 agent，行为不完全一致，但能做粗粒度验证）。更弱的降级是 Gemini 自评 — 让 Gemini 读 SKILL.md + 测试 prompt，自己推理 tool trajectory — 省了实际执行。

**Phase D 骨架（我方已搭）会预留 `HeadlessJarvisClient` 接口**，OpenClaw 侧实现就能替换进来。

---

## 推进节奏建议

| 阶段 | 谁做 | 何时落地 | 可 polyfill |
|------|------|---------|------------|
| B.1 tool | OpenClaw | Week 1 | ✅ 已就绪 |
| B.5 AGENTS.md | 我方 | ✅ 已完成 | — |
| C.1.a hook | OpenClaw | Week 2-3 | ⚠️ 弱 polyfill |
| C.1.b params | OpenClaw | Week 2 | ⚠️ 可经 gateway.log 粗代 |
| C.1.c sub_agent | OpenClaw | Week 2-3 | ❌ 几乎无 polyfill |
| D headless | OpenClaw | Week 3-5 | ⚠️ claude-code shell out |

**关键点**：B.1 不落地不影响系统运转（polyfill 顶）；落地后高信任信号更完整。C.1.c 不落地，我们看不到子 agent 的工作 — 那是最大盲区，OpenClaw team 建议优先这个。

---

## 联系

任何接口细节、边界条件、错误码需要澄清，直接提 issue 到 `github.com/ChenyqThu/openclaw-skill-learner` 或 DM。
