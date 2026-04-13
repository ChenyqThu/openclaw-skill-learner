# Darwin 优化测试数据集

标注过的 session 数据，用于评估和优化 Gemini 评估提示词。

## 目录结构

```
should-extract/   # 应该产出新技能的 session (ground truth = YES)
should-reject/    # 不应该产出技能的 session (ground truth = NO)
should-update/    # 应该更新已有技能的 session (ground truth = UPDATE)
```

## 标注规范

每条 session JSON 来自 `~/.openclaw/workspace/data/skill-learner/analysis-queue/`。
分类依据：

- **should-extract**: session 中包含可复用的 agent 行为模式，满足 A+B + C/D/E 标准
- **should-reject**: 常规操作、例行 cron 任务、或已有技能覆盖的场景
- **should-update**: 已有技能存在但 session 暴露了新信息（新雷区、新场景、更优方法）

## 数据来源

从 41 条历史记录中选取 18 条（8 extract + 6 reject + 4 update），覆盖：
- 工具调用数范围：6-37
- 来源类型：cron 任务、交互式对话、子 agent
- 领域：数据管道、飞书通知、微信提取、健康教练、知识管理
