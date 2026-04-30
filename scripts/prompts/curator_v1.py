"""
Curator LLM review prompt v1 — consolidation / archive / keep recommendations.

Input shape (from curator_llm.py):
    skills_context = [
        {"name": str,
         "source": "auto_learned" | "user_created",
         "age_days": int,
         "last_applied_at": ISO | None,
         "applied_count": int,
         "read_count": int,
         "skill_md": str},  # full file content
        ...
    ]

Output schema (Gemini must produce valid JSON):
    {
      "consolidations": [
        {"id": "c1", "kind": "consolidate",
         "skills": ["a", "b"], "new_name": "...",
         "rationale": "...", "overlap_pct": 0-100}
      ],
      "archives": [
        {"id": "a1", "kind": "archive",
         "skill": "X", "rationale": "..."}
      ],
      "keep": [
        {"id": "k1", "skill": "Y", "reason": "..."}
      ]
    }

Hard rules surfaced explicitly in the prompt so violations are easier to catch.
"""

import json


def _format_skill_block(s: dict) -> str:
    last = s.get("last_applied_at") or "never"
    return (
        f"━━━ skill: {s['name']} ━━━\n"
        f"  source:        {s.get('source', '?')}\n"
        f"  age:           {s.get('age_days', '?')}d\n"
        f"  read_count:    {s.get('read_count', 0)}\n"
        f"  applied_count: {s.get('applied_count', 0)}\n"
        f"  last_applied:  {last}\n"
        f"  ── SKILL.md ──\n"
        f"{s.get('skill_md', '').rstrip()}\n"
    )


def build_prompt(skills_context: list[dict]) -> str:
    skills_text = "\n\n".join(_format_skill_block(s) for s in skills_context)
    return f"""你是 Jarvis 的 Skill 图书馆管理员 (curator)。任务: 审查下列 {len(skills_context)} 个 active skills, 给出合并 / 归档 / 保留建议。

━━━ 输入 ━━━

{skills_text}

━━━ 决策规则 (硬约束, 违反必然被本地 validator 丢弃) ━━━

1. **applied_count > 3 的 skill 严禁推荐 archive** — 已经被实际应用多次, 即使表面看起来过时也不能裁
2. **不跨 source 边界合并** — auto_learned 与 user_created 不能合并到同一 skill (责任来源不同)
3. **consolidation 必须有具体证据** — 必须从 SKILL.md 内容里举出至少两段重叠的工作流 / 触发场景描述
4. **重叠阈值 60%** — 模糊"看着相似"不算; 工作流步骤、触发场景、关键命令必须有 ≥60% 重合
5. **archive 只针对 applied_count == 0 且 age >= 30d 的** — 否则保持 keep
6. **新合并 skill 的 new_name 用 kebab-case** — 与现有命名约定一致

━━━ 期望输出 (严格 JSON, 不要其他文字) ━━━

```json
{{
  "consolidations": [
    {{
      "id": "c1",
      "kind": "consolidate",
      "skills": ["skill-a", "skill-b"],
      "new_name": "merged-skill-name",
      "rationale": "两者都处理 X 场景的 Y 工作流, 关键步骤一致 (引用具体 SKILL.md 段落)",
      "overlap_pct": 75
    }}
  ],
  "archives": [
    {{
      "id": "a1",
      "kind": "archive",
      "skill": "obsolete-skill",
      "rationale": "applied_count=0, age=48d, 工作流过于狭窄"
    }}
  ],
  "keep": [
    {{
      "id": "k1",
      "skill": "well-used",
      "reason": "applied_count=12, 高频使用"
    }}
  ]
}}
```

━━━ 注意事项 ━━━

- 三个数组都可以为空数组 `[]`
- 每个 id 在整个 JSON 内唯一 (c1, c2, ..., a1, a2, ..., k1, k2, ...)
- 列表外不要任何文字、markdown 包裹或思考过程
- 务必输出有效 JSON, 任何语法错误都会被 validator 丢弃
"""


def render_report_markdown(skills_context: list[dict], llm_output: dict,
                            run_ts: str) -> str:
    """Render REPORT.md from a validated curator review result."""
    lines = [
        f"# Skill Curator 报告 — {run_ts}",
        "",
        "## 输入快照",
        f"- 审查 skills: **{len(skills_context)}**",
        f"- auto_learned: {sum(1 for s in skills_context if s.get('source') == 'auto_learned')}",
        f"- user_created: {sum(1 for s in skills_context if s.get('source') == 'user_created')}",
        "",
        "## LLM 建议",
        "",
    ]

    cons = llm_output.get("consolidations", []) or []
    archs = llm_output.get("archives", []) or []
    keeps = llm_output.get("keep", []) or []

    if cons:
        lines.append("### 合并建议 (需 Lucien 审批)")
        for c in cons:
            skills = " + ".join(f"`{s}`" for s in c.get("skills", []))
            lines.append(f"- {skills} → **{c.get('new_name', '?')}** "
                         f"(overlap ~{c.get('overlap_pct', '?')}%)")
            lines.append(f"  - {c.get('rationale', '')}")
            lines.append(f"  - rec_id: `{c.get('id', '?')}`")
        lines.append("")

    if archs:
        lines.append("### 归档建议 (需 Lucien 审批)")
        for a in archs:
            lines.append(f"- `{a.get('skill', '?')}` — {a.get('rationale', '')}")
            lines.append(f"  - rec_id: `{a.get('id', '?')}`")
        lines.append("")

    if keeps:
        lines.append("### 保留 (informational)")
        for k in keeps[:10]:
            lines.append(f"- `{k.get('skill', '?')}` — {k.get('reason', '')}")
        if len(keeps) > 10:
            lines.append(f"- ... +{len(keeps) - 10} more")
        lines.append("")

    if not (cons or archs or keeps):
        lines.append("_LLM gave no recommendations._")

    return "\n".join(lines) + "\n"
