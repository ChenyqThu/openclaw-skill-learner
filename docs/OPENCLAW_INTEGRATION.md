# OpenClaw 侧配合需求 — 三轨道自进化系统

本文档定义了三轨道自进化系统对 OpenClaw Jarvis 侧的配合要求。
技能学习系统作为外部插件运行，但需要 OpenClaw 侧在 **运行指令、日记记录、Session 行为** 上做出配合。

---

## 一、AGENTS.md 更新需求（全轨道）

### 问题
AGENTS.md §六「持续优化与自我进化」目前只写了周回顾 cron 和 VFM 评分，没有反映已运行两周的 Skill Learner 系统。三轨道系统的存在对 Jarvis 的行为有直接影响，必须写入运行指令。

### 建议新增内容

在 AGENTS.md §六 中新增以下段落：

```markdown
### 自进化系统（三轨道）

Jarvis 运行着一套外部自进化系统（OpenClaw Skill Learner 插件），通过 Plugin Hook 自动运作：

**轨道 0：Skill 自动提取**
- Plugin Hook 在每次 agent_end 时自动触发（≥8 次工具调用）
- 外部 Gemini 评估器判断是否包含可复用模式
- 候选 Skill 通过飞书卡片通知 Lucien 审批
- Jarvis 不需要主动做任何事，系统自动运行

**轨道 1：Skill 自动进化**
- 当 Jarvis 使用已有 Skill 时出现摩擦（用户纠正、重复失败），系统自动检测
- 触发 Darwin 8 维评分 + 爬山优化循环
- 改进自动 git commit，回归自动 git revert（棘轮机制）
- 进化结果通过飞书卡片汇报，Lucien 可回滚

**轨道 2：用户画像自动更新**
- 每周一凌晨分析近 7 天日记 + 对话纠正信号
- Gemini 归因到 USER.md / SOUL.md / AGENTS.md 的具体段落
- 生成更新提案，通过飞书卡片展示
- 必须经 Lucien 逐条确认才写入（不自动修改）

**Jarvis 的配合义务**：
- 日记中记录偏好变化和行为反馈（见下方日记规范）
- 被用户纠正时，在内部标记为摩擦信号（插件自动处理）
- 不要手动修改 skills/ 目录下的 .eval.json / .meta.json 文件
```

---

## 二、日记体系配合（主要影响 Track 2）

### 问题
Track 2 的归因质量完全依赖日记中是否包含**可归因信号**。纯事件流水账无法触发有效的规范文件更新。

### Jarvis 日记记录规范（建议新增到 AGENTS.md 或日记 cron 指令中）

日记中应**刻意记录**以下四类信号（在常规事件记录之外）：

#### 1. 偏好变化信号 `[偏好]`
当 Lucien 表达了新的偏好或修改了旧偏好时，用 `[偏好]` 标记：
```
[偏好] Lucien 说以后写周报不要用模板，直接写要点就行
[偏好] Lucien 表示不喜欢 Jarvis 在飞书消息里用表情包
```

#### 2. 行为纠正信号 `[纠正]`
当 Lucien 纠正了 Jarvis 的做法时，用 `[纠正]` 标记：
```
[纠正] Lucien 说应该先确认再执行，不要自作主张删除文件
[纠正] 发送消息前应该先给 Lucien 看一遍，这次差点发错
```

#### 3. 决策记录 `[决策]`
Lucien 做出的会影响后续工作方式的决策：
```
[决策] 从今天起，所有 PR 必须通过 CI 才能合并
[决策] 不再使用 Notion 管理日程，改用飞书日历
```

#### 4. 规范反馈 `[反馈]`
对 SOUL.md / AGENTS.md / USER.md 中现有规则的反馈：
```
[反馈] AGENTS.md 里「每日晨报」的格式太冗长了，Lucien 说精简一半
[反馈] SOUL.md 的沟通风格描述和实际期望不太一致
```

### 为什么这些标记重要
Track 2 的 Gemini 归因 prompt 会根据这些标记来：
1. 判断信号是否已稳定沉淀（同一偏好出现 2+ 次）
2. 归因到具体文件和段落（USER.md 偏好 vs SOUL.md 风格 vs AGENTS.md 规则）
3. 过滤掉一次性情绪或临时决定

没有标记的日记内容也会被分析，但归因精度会降低。

---

## 三、Track 0 配合（Skill Learning）

### Jarvis 侧无需主动配合
Plugin Hook 自动运作，无需 Jarvis 改变行为。但有以下注意点：

1. **不要手动修改 `skills/auto-learned/` 目录**
   - 候选 Skill 由系统生成，通过飞书卡片审批
   - 审批后自动移入 `skills/` 正式目录

2. **SKILL.md 读取会被追踪**
   - 当 Jarvis 读取某个 SKILL.md 时，插件会记录
   - 这是 dedup 和 Track 1 摩擦检测的信号源

3. **evaluate-server 必须持续运行**
   - `http://127.0.0.1:8300` 接收实时评估请求
   - 如果 server 不可达，请求会降级到 3:30 AM 批量处理

---

## 四、Track 1 配合（Darwin Evolution）

### Jarvis 侧无需主动配合
摩擦信号由 Plugin Hook 自动检测。但有以下影响点：

1. **用户纠正会触发进化**
   - 当 Lucien 说"不对/错了/wrong"等，且当前使用了某个 Skill
   - 系统会自动触发该 Skill 的 Darwin 进化循环

2. **手动触发**
   - Lucien 说"优化 skill X"会强制触发进化
   - 这不需要 Jarvis 做任何额外处理（Plugin 自动检测）

3. **workspace git 已初始化**
   - `~/.openclaw/workspace/` 现在是 git 仓库
   - 只追踪 `skills/*/SKILL.md` 和核心规范文件
   - Jarvis 不应对此目录执行 `git reset` 或 `git clean`

---

## 五、Track 2 配合（User Modeling）

### Jarvis 日记 cron 需要调整

当前日记 cron 如果只是事件记录，需要增加上述四类信号的自动提取。

**建议**：在日记 cron 的 system prompt 中增加：
```
在记录完当天事件后，额外审视以下维度：
1. Lucien 今天表达了什么新偏好？用 [偏好] 标记
2. Lucien 纠正了 Jarvis 什么做法？用 [纠正] 标记
3. 有什么会影响后续工作方式的决策？用 [决策] 标记
4. 对现有规范文件有什么反馈？用 [反馈] 标记

如果某个维度没有，就不写。不要编造。
```

### 对话层纠正信号自动采集

Plugin 已自动检测对话中的纠正关键词（"不对/错了/应该/wrong"），并将上下文保存到 `correction-signals.json`。这部分不需要 Jarvis 配合。

---

## 六、部署检查清单

| 项 | 状态 | 操作 |
|---|------|------|
| AGENTS.md 更新 | ❌ 未做 | 将上述内容写入 §六 |
| evaluate-server 重启 | ❌ 未做 | `launchctl unload + load` server plist |
| evolution cron 安装 | ❌ 未做 | `cp ai.openclaw.skill-evolution-cron.plist ~/Library/LaunchAgents/ && launchctl load ...` |
| user-modeling cron 安装 | ❌ 未做 | `cp ai.openclaw.user-modeling-cron.plist ~/Library/LaunchAgents/ && launchctl load ...` |
| 日记 cron prompt 更新 | ❌ 未做 | 增加四类信号标记指令 |
| Plugin 同步 | ✅ 已做 | `~/.openclaw/plugins/jarvis-skill-learner/index.js` 已更新 |
| workspace git | ✅ 已做 | 21 个 SKILL.md + 3 个核心文件已追踪 |
