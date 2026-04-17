# Feishu Notification Cards

三种卡片类型对应三条轨道：
- 🧠 **Skill 候选卡片** (Track 0) — 新 Skill 审批
- 🧬 **Skill 进化报告** (Track 1) — Darwin 进化结果
- 👤 **画像更新建议** (Track 2) — 规范文件更新提案

---

# 🧠 Skill 候选卡片 (Track 0)

## Card Format

Feishu Card 2.0 (`schema: "2.0"`) is required for `input` + `form` support.

## Structure

```
Header: 🧠 Skill 候选 · 新建/更新 · {skill_name} · {quality_score}分
        orange (new) | blue (update)
        quality_score displayed if > 0

Body:
  [markdown] 🔍 问题发现 + 💡 推荐方案
  [markdown] 📋 适用场景 (bullet list, up to 5)
  [markdown] 关键模式 + 已知雷区 (if available)
  [collapsible_panel] grey · 📎 来源 & Session 详情
    source agent, session key, tool count, tool names, quality score

  [form]
    [input] 💬 优化建议
            input_type: multiline_text, rows: 3, auto_resize, width: fill
    [column_set flex_mode:none]
      [column width:auto] [button primary]  ✅ 通过落地  (form_action_type: submit)
      [column width:auto] [button default]  💬 方案优化讨论 (form_action_type: submit)
      [column width:auto] [button danger]   ⏭ 跳过       (form_action_type: submit)
```

## Data Source

Card content is populated from `.eval.json` written by the evaluator:

```json
{
  "action": "create",
  "skill_name": "...",
  "problem_context": "...",
  "recommended_approach": "...",
  "when_to_use": ["...", "..."],
  "key_patterns": ["...", "..."],
  "pitfalls": ["...", "..."],
  "quality_score": {
    "reusability": 8,
    "insight_depth": 7,
    "specificity": 6,
    "pitfall_coverage": 5,
    "completeness": 7,
    "total": 67
  },
  "toolNames": ["..."],
  "lastInboundMessageId": "om_xxx"
}
```

## Button Callback

Buttons use `form_action_type: "submit"` so the optimization note input is included.

Callback payload:
- `action.name`: `"verb||base64(skill_name)||skill_action"`
- `action.form_value.optimization_note`: user's input text

Decode:
```python
import base64
parts = action.name.split("||")
verb = parts[0]          # approve | discuss | skip
skill_name = base64.urlsafe_b64decode(parts[1] + "==").decode()
skill_action = parts[2]  # create | update
note = action.form_value.get("optimization_note", "")
```

## Reply Threading

If `lastInboundMessageId` is available (extracted from `[msg:om_xxx]` in session headers),
the notification is sent as a reply to that message via Feishu reply API:

```
POST /im/v1/messages/{message_id}/reply
```

Otherwise, a new DM is sent to the user's `open_id`.

## Card 2.0 Gotchas

See `messaging-patterns/references/feishu-card-2.0.md` for the full list. Key ones:

- `input` only works inside `form`
- `collapsible_panel` cannot be inside `form`  
- Multi-line input: `input_type: "multiline_text"` (not `multiline: true`)
- Left-aligned buttons: `column width: "auto"` (not `"weighted"`)
- Form submit callback has no `action.value` — encode metadata in `action.name`

## Quality Gate

Cards are only sent when `quality_score.total >= 40`. Skills with lower scores are silently stored in `auto-learned/` without notification. This reduces approval noise — only meaningful skill candidates reach the user's Feishu DM.

The quality score is displayed in the card header (e.g., `🧠 Skill 候选 · 新建 · resilient-pipeline · 67分`) and in the collapsed details panel.

### Phase 4 Additional Gate Layers

Phase A introduces **two-tier validation before the quality gate** to stop the ~5% stream of malformed Feishu cards seen prior to April 2026:

1. **A.1 evaluator-side** (`_validate_skill_candidate`) — rejects drafts at write time:
   - `skill_name` must not be empty, `auto-*` prefix, or literal `"unknown"`
   - `skill_content` must have `---` frontmatter with open + close markers
   - Must contain ≥3 of the 6 canonical section headers (适用场景 / 不适用场景 / 操作步骤 / 示例 / 已知雷区 / 验证方式)
   - `eval_data.problem_context` ≥20 chars; `recommended_approach` ≥30 chars
   - `quality_score.total` ≥ 40 (coerced from string defensively)
   - On fail: status = `no_skill_name` | `no_skill_md` | `incomplete_skill_md` | `shallow_eval_json` | `low_quality`; nothing written to disk.

2. **A.2 server-side** (`_validate_eval_card_ready`) — rejects cards before sending:
   - `skill_name` starts with `auto-` / `unknown` / empty → skip
   - `quality_score < 40` → skip (strict; no more `> 0 AND < 40` half-bug)
   - Re-reads `.eval.json` on disk and requires `problem_context ≥20`, `recommended_approach ≥30`, `when_to_use ≥2`, `key_patterns ≥1`

### Skip-with-reason Feedback Loop (Phase A.3)

The Skip button's outcome now writes to `rejection-context.json` (FIFO 50 entries, 30-day auto-prune):

```json
{
  "skillName": "...",
  "action": "skip" | "discuss",
  "rejectedAt": "ISO-8601",
  "reason": "user-supplied or 'user clicked skip (no comment)'",
  "originalProblemContext": "...",
  "originalRecommendedApproach": "...",
  "sourceSessionRunId": "...",
  "promptNegativeExample": "曾提议「X」被 skip(原因:...)；原问题:Y；避免再次提出此类抽象模式"
}
```

Whatever text the user types in the card's input box is routed to `skill_action.py skip --reason "..."`. When no text is provided, a default reason is recorded. If OpenClaw's `card_action` hook eventually lands, the full reason goes straight through; until then the plain-skip path writes a stub entry that's still useful for Gemini's negative-example list.

The next Gemini evaluation reads the last 10 rejection entries and is instructed to output `NO_SKILL` when the *abstract pattern* (not surface topic) overlaps.

---

# 🧬 Skill 进化报告 (Track 1)

## Structure

```
Header: 🧬 Skill 进化成功/失败/回退 · {skill_name} · +{delta}
        green (improved) | orange (reverted) | red (error)

Body:
  [markdown] 📊 分数变化：{old} → {new} (+{delta})
  [markdown] 🔍 触发信号 (bullet list of friction signals)
  [collapsible_panel] 📎 详细维度分数 & Git Diff
    优化维度, 改动摘要, 轮次, commit hash
  [form]
    [column_set]
      [button primary] ✅ 确认
      [button danger]  ↩️ 回滚 (with confirm dialog)
```

## Button Callback

Button name format: `"evo_confirm||base64(skill_name)"` or `"evo_revert||base64(skill_name)"`

Decode:
```python
parts = action.name.split("||")
verb = parts[0]          # evo_confirm | evo_revert
skill_name = base64.urlsafe_b64decode(parts[1] + "==").decode()
```

- `evo_confirm`: No-op acknowledgment (commit already made)
- `evo_revert`: Calls `skill_action.py revert {skill_name}` → git revert

## Trigger Conditions

- Friction weight ≥ 4 (user correction, repeated failure, etc.)
- Manual trigger: user says "优化 skill X"
- Batch cron: daily 4:30 AM scans friction log

---

# 👤 画像更新建议 (Track 2)

## Structure

```
Header: 👤 画像更新建议 · {N} 条
        blue

Body:
  [markdown] 📊 分析结果：{N} 条建议
  [markdown] 提案 1: {target_file} § {section} (action, confidence)
             建议：...
             理由：...
  [markdown] 提案 2: ...
  [collapsible_panel] 📎 低置信度提案 ({M} 条)
  [markdown] CLI 审核命令
```

## Button Callback

Track 2 卡片当前使用 CLI 审核模式（`user_modeling.py --apply/--reject`），
未来可扩展为卡片内 per-proposal 按钮。

Button name format: `"profile_approve||proposal_id"` or `"profile_reject||proposal_id"`

## Trigger Conditions

- Weekly cron: Monday 5:00 AM
- Manual: `python3 user_modeling.py --analyze`

## Key Difference from Track 0/1

Track 2 卡片是**批量展示**模式（一张卡片包含多个提案），而非逐个通知。
这是 KOS 架构决策：不逐条打扰，积累一段时间后一次性展示。
