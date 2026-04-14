# OpenClaw Skill Learner — Project Instructions

## 项目概述

三轨道自进化系统，为 OpenClaw Jarvis 提供 Skill 自动学习、进化和用户建模能力。

- **Track 0** (Skill Learning): Plugin Hook → Gemini 评估 → Skill 候选 → 人工审批
- **Track 1** (Darwin Evolution): 摩擦检测 → 8 维评分 → 爬山棘轮 → git commit/revert
- **Track 2** (User Modeling): 日记+纠正信号 → Gemini 归因 → USER.md/SOUL.md/AGENTS.md 提案

## 架构约束

- **Plugin 层** (`plugin/index.js`): 运行在 OpenClaw 内部，**禁止网络调用**（会被安全扫描拦截）。所有数据通过 HTTP POST 到 localhost:8300 传递
- **评估层** (`scripts/`): 外部运行，调用 Gemini API，发送飞书通知
- **安全边界**: Track 1 只能修改 `skills/*/SKILL.md`，blocklist 拒绝 SOUL.md/AGENTS.md/USER.md 的自动修改。Track 2 只生成提案，不自动写入

## 关键文件

| 文件 | 职责 |
|------|------|
| `plugin/index.js` | OpenClaw 插件：Hook 数据采集 + 摩擦/纠正信号检测 |
| `scripts/gemini_client.py` | 共享 Gemini API 客户端 |
| `scripts/skill-learner-evaluate.py` | Track 0: 批量 Skill 评估 |
| `scripts/skill_evolution.py` | Track 1: Darwin 进化引擎 |
| `scripts/user_modeling.py` | Track 2: 用户建模归因 |
| `scripts/evaluate-server.py` | HTTP 服务：/evaluate + /evolve + /model |
| `scripts/skill_action.py` | 飞书卡片回调：approve/skip/discuss/revert/profile_* |
| `scripts/config.py` | 共享路径常量 |

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
# Track 0: 批量评估
python3 scripts/skill-learner-evaluate.py --dry-run

# Track 1: 进化
python3 scripts/skill_evolution.py --list
python3 scripts/skill_evolution.py --skill <name> --dry-run

# Track 2: 用户建模
python3 scripts/user_modeling.py --analyze --dry-run
python3 scripts/user_modeling.py --status

# 评估基准
python3 scripts/eval-benchmark.py --prompt v3_balanced

# 服务器健康检查
curl http://127.0.0.1:8300/health
```
