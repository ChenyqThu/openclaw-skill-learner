# Feishu Notification Card

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
