# OpenClaw Skill Learner — Project Instructions

## 项目概述

三轨道自进化系统 + Phase 4 supervision loop 重设计，为 OpenClaw Jarvis 提供 Skill 自动学习、进化、用户建模，以及 Agent 参与式提名 + 回放校验能力。

- **Track 0** (Skill Learning): Plugin Hook → [Phase B gate] → Gemini 评估 → [A.1 验证] → Skill 候选 → [A.2 发卡 gate] → [Phase D 回放] → 人工审批 → [A.3 反馈]
- **Track 1** (Darwin Evolution): 摩擦检测 → 8 维评分 → 爬山棘轮 → git commit/revert
- **Track 2** (User Modeling): 日记+纠正信号 → Gemini 归因 → USER.md/SOUL.md/AGENTS.md 提案
- **Phase E** (Cross-session): 扫 14 天 queue → Gemini 聚类 → ≥3 匹配 → proactive proposal

## 架构约束

- **Plugin 层** (`plugin/index.js`): 运行在 OpenClaw 内部，**禁止网络调用**（会被安全扫描拦截）。所有数据通过 HTTP POST 到 localhost:8300 传递
- **评估层** (`scripts/`): 外部运行，调用 Gemini API，发送飞书通知
- **安全边界**: Track 1 只能修改 `skills/*/SKILL.md`，blocklist 拒绝 SOUL.md/AGENTS.md/USER.md 的自动修改。Track 2 只生成提案，不自动写入
- **Phase 4 landing gate**: 任何 skill 草稿必须过 A.1 验证器才落盘；任何飞书卡片必须过 A.2 server-side gate；可选启用 Phase D 回放 gate
- **Phase B gate**: 评估器只在 `nominated OR frictionWeight≥3` 时跑 Gemini；否则 202 skipped

## 关键文件

| 文件 | 职责 |
|------|------|
| `plugin/index.js` | OpenClaw 插件：Hook 数据采集 + 摩擦/纠正信号检测 + B.2 提名捕获 + C 转发 sessionFile |
| `scripts/gemini_client.py` | 共享 Gemini API 客户端；`extract_skill_md` 已修复支持 frontmatter |
| `scripts/skill-learner-evaluate.py` | Track 0: 批量 Skill 评估 + A.1 `_validate_skill_candidate` + C loader |
| `scripts/skill_evolution.py` | Track 1: Darwin 进化引擎 |
| `scripts/user_modeling.py` | Track 2: 用户建模归因 |
| `scripts/evaluate-server.py` | HTTP 服务：/evaluate + /evolve + /model，B.3 提名 gate + A.2 发卡 gate |
| `scripts/skill_action.py` | 飞书卡片回调：approve/skip/discuss/revert/profile_*，A.3 写 rejection-context |
| `scripts/replay_gate.py` | **Phase D** 回放校验 gate (骨架)；`HeadlessJarvisClient` 待 OpenClaw headless mode |
| `scripts/cross_session_cluster.py` | **Phase E** 跨会话聚类 → proactive proposal (骨架) |
| `scripts/prompts/v3_balanced.py` | 生产 prompt；已追加 A.4 rejection injection + B.4 nomination block |
| `scripts/prompts/v4_rich_transcript.py` | **Phase C** opt-in 变体 (PROMPT_VERSION=v4_rich_transcript) |
| `scripts/config.py` | 共享路径常量 |
| `docs/OPENCLAW_COOPERATION_PHASE2.md` | Phase 4 的 OpenClaw 侧协作规格（B.1 工具 + C.1 hooks + D headless） |

## 开发规范

- **Gemini 调用**: 统一使用 `gemini_client.py`，不要在脚本中重复实现
- **Plugin ESM**: `plugin/index.js` 必须保持 ESM 格式（import/export），不能用 CommonJS
- **环境变量**: 从 `~/.openclaw/.env` 加载，通过 `gemini_client.load_env()` 或 `config.py`
- **测试**: `python3 skill_evolution.py --list` / `--dry-run` / `user_modeling.py --status`
- **Git**: workspace git (`~/.openclaw/workspace`) 只追踪 SKILL.md + 核心规范文件，不追踪 data/

## 运行依赖

- Python 3.13+ (使用 `str | None` 类型语法)
- Node.js (plugin ESM)
- `GEMINI_API_KEY` 环境变量
- `FEISHU_APP_ID` + `FEISHU_APP_SECRET` (通知卡片)
- `openclaw` CLI (飞书消息发送)

## 常用命令

```bash
# Track 0: 批量评估（Phase A 验证器已内置，无效草稿自动拒绝）
python3 scripts/skill-learner-evaluate.py --dry-run

# Track 1: 进化
python3 scripts/skill_evolution.py --list
python3 scripts/skill_evolution.py --skill <name> --dry-run

# Track 2: 用户建模
python3 scripts/user_modeling.py --analyze --dry-run
python3 scripts/user_modeling.py --status

# Phase D: 回放校验 gate（dry-run 用 Gemini 自评, --use-runner 依赖 OpenClaw headless mode）
python3 scripts/replay_gate.py --skill <name> --source-request <queue-id> --dry-run

# Phase E: 跨会话聚类（扫最近 14 天 queue → proactive 提案）
python3 scripts/cross_session_cluster.py --days 14 --dry-run

# 评估基准
python3 scripts/eval-benchmark.py --prompt v3_balanced
# 使用 Phase C 丰转录 prompt 变体(opt-in)
PROMPT_VERSION=v4_rich_transcript python3 scripts/skill-learner-evaluate.py --dry-run

# 手工触发 skip 写入 rejection-context (Phase A.3)
python3 scripts/skill_action.py skip <draft-name> --reason "重复提议此类模式"

# 服务器健康检查
curl http://127.0.0.1:8300/health

# 紧急绕过 Phase B gate (debug / 补跑)
OMC_SKIP_GATE=1 python3 scripts/evaluate-server.py
```

## 关键 ENV 变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `GEMINI_API_KEY` | — | 必填，评估调用 |
| `PROMPT_VERSION` | `v3_balanced` | 切换 prompt 变体（`v1_baseline`/`v2_recall_dedup`/`v3_balanced`/`v4_rich_transcript`） |
| `OMC_SKIP_GATE` | `""` | 设 `"1"` 绕过 Phase B gate |
| `OMC_RICH_BUDGET` | `30000` | v4 rich transcript 字数上限 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | — | 飞书卡片发送 |

## Phase 4 产物清单（2026-04-17）

- Phase A（hot-fix）：A.1+A.2+A.3+A.4+A.5+A.6 全部上线
- Phase B（agent 提名）：B.1-B.5 全部上线;**B.1 first-class 工具已 via plugin SDK `api.registerTool()` 完成**(payload 带 `_firstClass: true`);polyfill 仍保留兼容
- Phase C（丰转录 + 参数捕获 + 子 agent）：C.2 plugin 转发 + C.3 loader/v4 prompt 上线；**C.1.b `after_tool_call.params` 全透传 + C.1.c `subagent_spawned/ended` 已 via plugin SDK 完成**(plugin 内 `sanitizeParams` + `appendToolTrace` + parent↔child registry);C.1.a 仍需 agent 协作
- Phase D（回放 gate）：`replay_gate.py` 骨架 + dry-run 模式；真 runner 可用 `api.registerAgentHarness` 或 shell out 到 claude-code
- Phase E（跨会话）：`cross_session_cluster.py` 骨架 + 置信度评分
- evaluator 消费 Phase 4.1 payload：`evaluate-server.py` 持久化 `toolTrace` + `subagentSummaries`;`v3_balanced.py` 提供 `_build_tool_trace_note` + `_build_subagent_note` + 三态 nomination 块(first-class / polyfill-full / polyfill-empty)
