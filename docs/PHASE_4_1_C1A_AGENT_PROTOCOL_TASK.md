# Phase 4.1 对接任务 — C.1.a Considered-Rejected 协议

> **对象**：Jarvis (OpenClaw 侧)
> **来源**：陈源泉 / Claude Code
> **日期**：2026-04-17
> **范围**：只改 AGENTS.md + 周回顾 cron prompt，不改代码

---

## 0. 背景（为什么做这件事）

Skill Learner Phase 4 现在看得到的信号是：
- ✅ **正面**：`skill_learner_nominate` — 你主动说"这次有料"
- ✅ **误用**：`frictionSignals` — 用户说"不对/错了" + `skillsUsed` 谁被加载了
- ❌ **盲区**：你**考虑过**某 skill 但决定**不加载**，这件事**完全没信号**

这件盲区会导致：
- 某个 skill 的 `when_to_use` 写得太窄，你本来应该命中但擦边过去 → Track 1 进化完全看不见这种 regression 信号
- 用户后来补救说"你应该用 skill X"时，系统只能看见"用户纠正"，不知道你其实**考虑过并主动决定不用**，两种 case 该分开评估：
  - 你考虑过但判断不匹配 → `when_to_use` 太严，skill 需要拓宽
  - 你根本没注意到 → skill 的发现/命名需要改进

---

## 1. 决策：用 AGENTS.md 协议 + 周回顾补标 (不加新工具)

讨论过两条路径：
- A. AGENTS.md 协议 — 日记里追溯补标 `[考虑未用]`
- B. 新增 `skill_consider_note` plugin 工具，即时记录

选 A。理由：
- 即时记录让你每次 reasoning 多一个心智负担（"我考虑了 skill X 但不适用 → 要不要记录？"），对 session 节奏是负担
- 周回顾时复盘补标更自然 — 你已经在写周报 / 日记，顺手扫一眼"本周考虑过但没用的 skill"成本低
- 数据密度如果够用就不上工具；稀疏到连补标都难再升级 B

**如果后续数据证明 A 不够，再考虑加 `skill_consider_note` 工具（plugin 侧 ~30 行）。**

---

## 2. 要做的事（3 处）

### 2.1 AGENTS.md §六新增「考虑未用」协议

**文件**：`~/.openclaw/workspace/AGENTS.md`
**位置**：§六 自进化系统下「日记四类信号标记规范」那一段的**同级位置**新增一个 bullet list

**追加内容**：

```markdown
**第五类信号:考虑未用 (Considered-Rejected)**

在日记和周回顾中，额外留意下列情况并用 `[考虑未用]` 标记:

- 你在某个 session 里**瞥过某 skill 的 when_to_use 但决定不加载**（即使只在心里过了一下）
- 你**本来应该用某 skill 但没意识到**（用户事后纠正"你应该用 skill X"）→ 同样记为考虑未用,这是 skill 发现机制的问题

格式:

```
[考虑未用] skill <skill-name>: <为什么没用的一句话理由>
```

示例:

```
[考虑未用] skill messaging-patterns: 原想用来写卡片,但那是 Card 2.0 form,skill 没覆盖 form+collapsible 组合
[考虑未用] skill memory-search: 我想直接读 memory/YYYY-MM-DD.md 了,忘了这个 skill 是专门查跨日的
```

**频率**:不是每个 session 都要写,周回顾复盘时一次性补齐即可。数据稀疏没关系,单条高质量条目比十条乱写的更有价值。

**不写的情况**:
- 你**根本没考虑过**这个 skill(因为根本没进入视线)— 如果日记标不到,说明 skill 发现机制该优化,但这不是「考虑未用」的记录范畴
- 你**考虑过并正确决定不用**(skill 确实不适用),也不需要记 — 除非你觉得 when_to_use 描述可以更精准
```

### 2.2 周回顾 cron prompt 注入补标指令

**背景**：周日晚上的周回顾 cron（Jarvis 每周五/周日跑，具体看你的 cron 列表）会提示你复盘本周的日记。那个 prompt 里追加一段：

**追加内容（写到现有周回顾 prompt 末尾）**：

```
复盘本周日记时，额外审视:

1. 本周是否有 session 用户事后纠正说「你应该用 skill X」?
   — 是 → 找到对应日记条目追加 `[考虑未用] skill X: <为什么没意识到>`

2. 本周是否有自己在 reasoning 里瞥过某 skill 的 when_to_use 觉得不适用?
   — 是 → 在周回顾小结里列一行 `[考虑未用] skill X: <判断的理由>`

3. 复盘扫到的 `[考虑未用]` 聚集到同一个 skill ≥2 次? → 在周回顾建议里单独提出该 skill 的 when_to_use 需要优化

无则不写,不要编造。
```

### 2.3 考虑未用信号的去处

**让信号最终流入 skill-learner 评估系统**：日记里的 `[考虑未用]` 标记由 Track 2 cron 每周扫描日记时识别并汇总。

目前 Track 2 (`scripts/user_modeling.py`) 已经在识别 `[偏好]/[纠正]/[决策]/[反馈]` 四类标记。新增的 `[考虑未用]` 只需要加到它的识别 regex 里。

**Skill-Learner 侧的配套已由 Claude Code 预先设计好位置**（本次 plugin 侧不用改）：
- `scripts/user_modeling.py` 扫描 regex 追加 `[考虑未用]`
- 新产出文件 `data/skill-learner/considered-rejected.jsonl`（每周 cron 产出）
- Track 1 Darwin 进化在读 friction signals 时,把 `considered-rejected.jsonl` 中针对 target skill 的条目**作为额外证据**送 Gemini — 输入信号是「when_to_use 是否要拓宽」

**这段实现等 AGENTS.md 和 cron prompt 先生效一周后,再根据数据稀疏性决定具体聚合规则**。协议先走，code 最后跟。

---

## 3. 验收 checklist

- [ ] `AGENTS.md` §六 日记四类信号下面新增「第五类:考虑未用」段落,格式 + 示例 + 不写的情况齐全
- [ ] 周回顾 cron prompt 末尾追加 3 条补标审视项
- [ ] 下一次周回顾实际跑一遍,看输出中有没有 `[考虑未用]` 标记(数据稀疏时可以 0 条,不代表失败)
- [ ] 不改 plugin 代码,不动 `~/.openclaw/extensions/jarvis-skill-learner/`

---

## 4. 不做的事

- 不加 `skill_consider_note` plugin 工具 — 先跑协议路径
- 不改 `scripts/user_modeling.py` 现在就支持新标记 — 等协议先生效一周
- 不动 Track 1 的 skill evolution engine — 等 `considered-rejected.jsonl` 有数据再说

---

## 5. 时机

这个改动是补齐信号，不是阻塞项。Phase A/B/C/E 已经把噪声砍了 ~95%，目前更紧迫的是等日常使用的 nomination 数据累积。这件事建议**在下一次你 review AGENTS.md 时顺手做**，不用单独腾时间。

做完简短回复即可（不用长报告）：
- AGENTS.md 第几行加的
- 周回顾 cron 哪一份 prompt 加的
- 下周回顾执行完会有数据出来观察

---

## 6. Claude Code 侧的后续动作（FYI）

**一周后，根据日记中 `[考虑未用]` 标记的密度**，Claude Code 会：
- **密度够** (≥5 条/周): 实现 `user_modeling.py` 的聚合逻辑 + `considered-rejected.jsonl` 产出 + Track 1 进化 prompt 注入
- **密度不够** (<2 条/周): 加 `skill_consider_note` plugin 工具作为即时记录通道

所以这件事做完，**一周后看数据就知道下一步怎么做**。不用先写代码等着。

---

## 附:关于 skills 的补充约定（同时加进 AGENTS.md）

与「考虑未用」相关的一条原则:

> 当你正在解决问题,心里过了一下"好像有 skill 能用,但...",**即便决定不用,也要给这个决定一个可追溯的理由**。如果理由是「skill 没覆盖这个变体」,那就是一次候选「当前 skill 该进化」的信号,不要默默过去。

这条更像行为准则而不是信号标记,放在 §六 顶部合适。
