# OpenClaw 集成指南 — 三轨道自进化系统

本文档面向**可复制部署**设计：定义三轨道自进化系统与宿主 AI Agent 平台之间的集成契约。
无论宿主是 OpenClaw、Hermes 还是其他 Agent 框架，只要满足以下接口约定即可接入。

> **Phase 4 (2026-04)**：本文档描述的三轨道仍然是系统骨架；在此之上新增了 4 层 supervision loop 改进（A 验证、B 提名、C 丰转录、D 回放、E 跨会话）。
>
> **Phase 4 实施方式反转**：最初以为 B.1/C.1.b/C.1.c 需要 OpenClaw 上游 PR；实际 OpenClaw plugin SDK（`openclaw@2026.4.15`）已经提供 `api.registerTool`、`after_tool_call.event.params` 全透传、`subagent_spawned/ended` hooks —— 全部可在 plugin 内完成。详见 [PHASE_4_OPENCLAW_INTEGRATION_REPORT.md](PHASE_4_OPENCLAW_INTEGRATION_REPORT.md) 和 [OPENCLAW_COOPERATION_PHASE2.md](OPENCLAW_COOPERATION_PHASE2.md)。本文档关注的是**最小可运行契约**，Phase 4 只是强化信号质量，不改变契约骨架。

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ 宿主 Agent 平台 (e.g., OpenClaw Jarvis)                      │
│                                                              │
│  Plugin Hooks (in-process, no network calls)                 │
│  ├── after_tool_call → 工具计数 + 错误追踪 + Skill 读取检测   │
│  ├── agent_end → 摩擦/纠正信号检测 + HTTP POST localhost:8300 │
│  └── session_end → 健康检查 + 兜底队列                        │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP POST (fire & forget)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ Skill Learner 评估层 (external process)                      │
│                                                              │
│  evaluate-server.py (localhost:8300)                         │
│  ├── POST /evaluate → Track 0: Skill 候选评估                │
│  ├── POST /evolve   → Track 1: Darwin 进化触发               │
│  └── POST /model    → Track 2: 纠正信号收集                  │
│                                                              │
│  Scheduled Jobs (launchd / cron / systemd)                   │
│  ├── 3:30 AM daily  → Track 0: 批量评估兜底                  │
│  ├── 4:30 AM daily  → Track 1: 批量进化巡检                  │
│  └── 5:00 AM Mon    → Track 2: 周度用户建模分析               │
└─────────────────────────────────────────────────────────────┘
```

**核心设计原则**：
1. Plugin 层零网络调用（通过宿主安全扫描）
2. 评估层完全外部运行（零 context window 成本）
3. 三轨道共享 Gemini client、HTTP server、飞书卡片基础设施
4. Track 0/1 自动运行，Track 2 只生成提案（人在回路）

---

## 二、宿主平台接口契约

### 2.1 必须提供的 Hook 接口

| Hook | 触发时机 | 提供的数据 | 用途 |
|------|---------|-----------|------|
| `after_tool_call` | 每次工具调用完成后 | toolName, durationMs, error, params | 工具计数、Skill 读取检测、错误追踪、Phase B 提名捕获 |
| `agent_end` | 一轮 Agent 执行结束后 | messages (对话历史数组), 可选 sessionFile | 转录提取、摩擦/纠正信号检测、Phase C 丰转录路径 |
| `session_end` | 整个会话结束后 | sessionFile (可选) | 健康检查、兜底队列 |

**Phase 4 可选增强**（见 [OPENCLAW_COOPERATION_PHASE2.md](OPENCLAW_COOPERATION_PHASE2.md)）：

| 扩展 | 类型 | 状态 (2026-04-17) | 落地方式 |
|------|------|------|---------|
| `skill_learner_nominate` 工具 | 新 tool | ✅ **DONE via plugin SDK** | `api.registerTool()` + typebox schema，plugin 内完成，payload 带 `_firstClass: true` |
| `after_tool_call.params` 全透传 | 现有 hook 扩展 | ✅ **DONE via plugin SDK** | event.params 一直是 `Record<string, unknown>`,plugin 新增 `sanitizeParams`+`appendToolTrace`(cap 40) |
| `sub_agent_spawn/ended` events | 现有 hooks | ✅ **DONE via plugin SDK** | `subagent_spawned/ended` 三 hook 已在 SDK；plugin 建 parent↔child map，payload 新增 `subagentSummaries` |
| `skill_considered_rejected` hook | 新 hook | 🟡 需 agent 协作 | 本质是 agent 内部决策，平台不 emit；AGENTS.md 协议 / 新增 `skill_consider_note` 工具 |
| Headless Jarvis runner | CLI/API | 🟡 未探 `registerAgentHarness` | Phase D 已预留 `HeadlessJarvisClient` 骨架 + shell out 到 claude-code |

### 2.2 必须提供的文件系统结构

```
{workspace}/
├── skills/                    # git-tracked
│   ├── {skill-name}/
│   │   ├── SKILL.md           # Track 1 可修改
│   │   └── test-prompts.json  # Track 1 自动生成
│   └── auto-learned/          # git-ignored, Track 0 生成
├── memory/                    # 日记目录
│   └── YYYY-MM-DD.md          # Track 2 读取
├── USER.md                    # Track 2 提案目标
├── SOUL.md                    # Track 2 提案目标 (高风险)
├── AGENTS.md                  # Track 2 提案目标 + 运行指令
└── data/skill-learner/        # 运行时数据 (git-ignored)
    ├── analysis-queue/        # Track 0 待评估队列
    ├── friction-signals.json  # Track 1 摩擦信号日志
    ├── correction-signals.json # Track 2 纠正信号日志
    └── pending-user-updates.json # Track 2 待审提案
```

### 2.3 必须提供的通知渠道

系统通过 CLI 命令发送通知卡片：
```bash
{agent-cli} message send --channel {channel} --target {user} --card {json}
```

当前实现使用 `openclaw message send --channel feishu`，可替换为任何支持卡片消息的平台。

---

## 三、宿主 Agent 配合义务

### 3.1 运行指令更新 (AGENTS.md)

宿主 Agent 的运行指令中**必须**包含三轨道系统的描述，否则 Agent 不知道自己在被观测和进化。

**最小必要内容**（已写入 OpenClaw AGENTS.md §六）：

```markdown
### 自进化系统（三轨道）

运行着外部自进化系统（Skill Learner 插件），通过 Plugin Hook 自动运作：

- 轨道 0：agent_end 触发 Skill 评估（Phase B 起:需 nominated 或 friction≥3）,候选通知审批
- 轨道 1：摩擦信号触发 Darwin 8 维评分 + 棘轮优化，改进 auto-commit
- 轨道 2：每周分析日记 + 纠正信号，生成规范文件更新提案

配合义务：
- Session 结束前审视是否触发「自我提名协议」四条之一（走弯路/非显然组合/踩坑/主动换方案）→ 是就调 skill_learner_nominate
- 日记中标记 [偏好]/[纠正]/[决策]/[反馈] 四类信号
- 不手动修改 skills/ 下的 .eval.json / .meta.json
- 不对 workspace git 执行 reset --hard 或 clean -f
```

**Phase 4 新增:自我提名协议**（完整版见 [OPENCLAW_COOPERATION_PHASE2.md §B.1](OPENCLAW_COOPERATION_PHASE2.md) + OpenClaw AGENTS.md §六）。核心要求:

- 工具名:`skill_learner_nominate`
- 必填:topic / pain_point / reusable_pattern / confidence
- 触发条件:走弯路被纠正 / 非显然 ≥3 工具组合 / 踩坑 / 主动换方案
- 上限:3 次/run
- 降级:工具未注册时 agent 用 `exec` 写 JSON 到 `data/skill-learner/nominations/`，plugin 会检测并转发

### 3.2 日记信号标记规范

Track 2 的归因精度完全依赖日记中的可归因信号。Agent 的日记 cron prompt 必须包含信号提取指令。

**四类标记**：

| 标记 | 含义 | 示例 |
|------|------|------|
| `[偏好]` | 用户表达的新偏好或偏好变更 | `[偏好] Lucien 说不喜欢用表情包` |
| `[纠正]` | 用户纠正了 Agent 的做法 | `[纠正] 应该先确认再执行，不要自作主张` |
| `[决策]` | 影响后续工作方式的决策 | `[决策] 从今天起 PR 必须过 CI` |
| `[反馈]` | 对现有规范文件的反馈 | `[反馈] AGENTS.md 晨报格式太冗长` |

**日记 cron prompt 追加内容**：
```
在记录完当天事件后，必须审视以下维度并追加「## 自进化信号」章节：
1. 用户今天表达了什么新偏好？用 [偏好] 标记
2. 用户纠正了 Agent 什么做法？用 [纠正] 标记
3. 有什么影响后续工作方式的决策？用 [决策] 标记
4. 对现有规范文件有什么反馈？用 [反馈] 标记
如果某个维度没有，就不写。不要编造。
```

### 3.3 Workspace Git 维护

Track 1 棘轮依赖 git。宿主需要：
- 新增 Skill 后运行 `git add skills/*/SKILL.md && git commit`
- 不对 workspace 执行 `git reset --hard` 或 `git clean -f`
- 定期检查未追踪的新 skill 目录

---

## 四、部署检查清单

### 4.1 首次部署

| # | 项 | 命令 | 验证 |
|---|---|------|------|
| 1 | 安装 Plugin | `openclaw plugins install ./plugin` | 重启后 console 输出 `[skill-learner] Plugin registered` |
| 2 | 配置环境变量 | 写入 `~/.openclaw/.env`: GEMINI_API_KEY, FEISHU_APP_ID, FEISHU_APP_SECRET | `echo $GEMINI_API_KEY` 有值 |
| 3 | 初始化 workspace git | `bash scripts/init-workspace-git.sh` | `cd ~/.openclaw/workspace && git log` 有 initial commit |
| 4 | 启动 evaluate-server | `launchctl load ~/Library/LaunchAgents/ai.openclaw.skill-learner-server.plist` | `curl localhost:8300/health` 返回 ok |
| 5 | 安装 Track 0 批量 cron | `cp ai.openclaw.skill-learner.plist ~/Library/LaunchAgents/ && launchctl load ...` | — |
| 6 | 安装 Track 1 进化 cron | `cp ai.openclaw.skill-evolution-cron.plist ~/Library/LaunchAgents/ && launchctl load ...` | — |
| 7 | 安装 Track 2 建模 cron | `cp ai.openclaw.user-modeling-cron.plist ~/Library/LaunchAgents/ && launchctl load ...` | — |
| 8 | 同步 Plugin | **不要直接 `cp`**;runtime 加载点是 `~/.openclaw/extensions/jarvis-skill-learner/`(由 `openclaw plugins install` 从 path source 安装)。正确做法:`rm -rf ~/.openclaw/extensions/jarvis-skill-learner && openclaw plugins install <repo>/plugin && openclaw gateway restart`,验证 gateway.log 出现 `Plugin registered (Phase 3: Self-Evolution)` | — |
| 9 | 更新 AGENTS.md | 写入三轨道运行指令（见 §3.1） | Agent review 能看到 |
| 10 | 更新日记 cron prompt | 注入四类信号标记指令（见 §3.2） | 次日日记包含 `## 自进化信号` |

### 4.2 当前部署状态 (OpenClaw Jarvis, 2026-04-14)

| # | 项 | 状态 | 备注 |
|---|---|------|------|
| 1 | Plugin 安装 | ✅ | Phase 2 起已运行 |
| 2 | 环境变量 | ✅ | GEMINI_API_KEY + FEISHU credentials |
| 3 | workspace git | ✅ | 21+ SKILL.md + 核心文件，含 context-doctor/figma 等新 skill |
| 4 | evaluate-server | ✅ | 运行中，uptime 52794s |
| 5 | Track 0 cron | ✅ | 3:30 AM daily |
| 6 | Track 1 cron | ✅ | 4:30 AM daily (2026-04-14 加载) |
| 7 | Track 2 cron | ✅ | Mon 5:00 AM (2026-04-14 加载) |
| 8 | Plugin 同步 | ✅ | Phase 3 版本含摩擦+纠正信号 |
| 9 | AGENTS.md | ✅ | §六 已写入三轨道描述 + 配合义务 |
| 10 | 日记 cron prompt | ✅ | 已注入四类信号 + 回溯补标 5 天 (04-08~04-13) |

---

## 五、可复制部署指南

### 5.1 适配到其他 Agent 平台

本系统设计为**平台无关**，适配需要修改以下组件：

| 组件 | 需要适配的部分 | 工作量 |
|------|--------------|--------|
| `plugin/index.js` | Hook 注册方式（`definePluginEntry` → 目标平台 SDK） | 中 |
| `evaluate-server.py` | 通知发送（`openclaw message send` → 目标平台 CLI/API） | 小 |
| `skill_action.py` | 卡片回调处理（飞书 Card 2.0 → 目标平台卡片格式） | 中 |
| `gemini_client.py` | 可替换为 Claude/GPT API | 小 |
| plist 文件 | macOS launchd → Linux systemd / cron | 小 |

**不需要修改**：`skill_evolution.py`、`user_modeling.py`、`eval-benchmark.py`、`darwin-optimize.py`。这些是纯逻辑层，不依赖具体平台。

### 5.2 最小可行部署 (MVP)

如果只需要 Track 0（Skill 学习），最小部署为：
1. Plugin Hook 注册 (`after_tool_call` + `agent_end`)
2. `evaluate-server.py` 运行
3. `skill-learner-evaluate.py` + `gemini_client.py`
4. Gemini API key

Track 1 和 Track 2 是增量功能，可以在 Track 0 稳定后逐步启用。

### 5.3 环境要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.12+ | 评估层（使用 `str \| None` 类型语法） |
| Node.js | 18+ | Plugin 层（ESM） |
| git | 2.x | Track 1 棘轮机制 |
| Gemini API | gemini-3-flash-preview | 评估 + 进化 + 归因 |
| 通知渠道 | 飞书 / Slack / Discord | 审批卡片（可选） |

---

## 六、安全边界

| 约束 | 实现方式 | 为什么 |
|------|---------|--------|
| Plugin 零网络调用 | 所有 HTTP 通过 localhost:8300 | 通过宿主安全扫描 |
| Track 1 只改 SKILL.md | `SkillEvolver` 硬编码 blocklist | SOUL/AGENTS/USER 是高风险文件 |
| Track 1 只改已批准 Skill | 拒绝 `auto-learned/` 路径 | 未审批的草稿不应被进化 |
| Track 2 不 auto-commit | `apply_proposal()` 只写文件 | 核心文件修改必须人工确认 |
| git 操作用 revert 不用 reset | `git revert HEAD --no-edit` | 保留完整历史，可追溯 |
| 进化频率限制 | 2 次/小时 | 防止 Gemini API 成本失控 |

---

## 七、监控与故障排查

### 日志文件

| 文件 | 内容 |
|------|------|
| `data/skill-learner/server.log` | evaluate-server 请求日志 |
| `data/skill-learner/evaluate.log` | Track 0 批量评估日志 |
| `data/skill-learner/evolution.log` | Track 1 进化日志 |
| `data/skill-learner/user-modeling.log` | Track 2 建模日志 |

### 健康检查

```bash
# Server 状态
curl http://127.0.0.1:8300/health

# Track 1: 查看 eligible skills
python3 scripts/skill_evolution.py --list

# Track 2: 查看待审提案
python3 scripts/user_modeling.py --status

# Workspace git 状态
cd ~/.openclaw/workspace && git status && git log --oneline -5
```

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| evaluate-server 无响应 | 进程挂掉 | `launchctl unload + load` server plist |
| Track 1 无进化记录 | 无摩擦信号 or cron 未加载 | 检查 `friction-signals.json` + `launchctl list` |
| Track 2 无提案 | 日记缺少标记 or cron 未跑 | 检查日记 `## 自进化信号` + cron log |
| Skill 进化失败 | workspace git 脏状态 | `cd workspace && git status`，解决冲突 |
