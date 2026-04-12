# Gemini Evaluation Prompt Design

## Philosophy

Inspired by Hermes Agent's `_SKILL_REVIEW_PROMPT`, which is intentionally minimal:

> *"Review the conversation above and consider saving or updating a skill if appropriate. Focus on: was a non-trivial approach used to complete a task that required trial and error, or changing course due to experiential findings along the way, or did the user expect or desire a different method or outcome?"*

Hermes captures two essential signals:
1. **Trial and error / course correction** — hard-won knowledge, not obvious upfront
2. **User corrected the agent** — the agent would repeat the mistake without documenting it

Our prompt extends this with structured output and OpenClaw-specific context.

---

## New Skill Prompt Structure

```
[OpenClaw Skill definition]
  What a Skill IS: reusable agent behavioral pattern
  What a Skill is NOT: one-time code fix, script optimization

[Session data]
  Tool count + names
  User messages (up to 8)
  Agent responses (up to 8)

[Existing skills list]
  name: one-line description
  (for dedup — Gemini avoids creating duplicates)

[Hermes core test]
  "Did this require trial and error / changing course / user correction?"

[Qualification criteria A+B + one of C-E]
  A: reusable across ≥2 future contexts
  B: agent workflow pattern (not "fix script X")
  C: required non-obvious T&E
  D: specific tool combos / pitfalls
  E: user corrected agent

[Red flags → NO_SKILL]
  "add error handling to script X" → fix the script
  pattern only applies to one file/cron/config
  obvious or duplicates existing skill

[Output format]
  NO_SKILL  (if not qualified)
  OR:
  ```eval_json  → structured evaluation data
  ```skill_md   → SKILL.md content
```

## Update Skill Prompt Structure

Same Hermes core test, but focused on gap detection:

```
[Existing SKILL.md content (truncated to 3000 chars)]
[Session data]

[Gap detection criteria]
  1. Pitfall/error agent hit that skill didn't warn about
  2. Better/faster approach discovered via T&E
  3. User corrected agent → skill gap

[Output]
  NO_UPDATE  (if no new info)
  OR:
  ```eval_json  → structured update evaluation
  ```patch_yaml → sections to add
```

---

## Pre-filter (before calling Gemini)

Sessions with `toolCount < 8` AND no user correction signal are skipped:

```python
correction_keywords = ["不对", "不是", "错了", "应该", "重新",
                       "no,", "actually", "wait", "wrong", "instead"]
has_correction = any(kw in msg.lower() for msg in user_messages
                     for kw in correction_keywords)

if tool_count < 8 and not has_correction:
    skip()  # ~60% API savings
```

---

## Output Parsing

```python
def _extract_eval_json(result: str) -> dict:
    m = re.search(r'```eval_json\s*\n(.*?)\n```', result, re.DOTALL)
    return json.loads(m.group(1)) if m else {}

def _extract_skill_md(result: str) -> str:
    m = re.search(r'```skill_md\s*\n(.*?)\n```', result, re.DOTALL)
    return m.group(1).strip() if m else legacy_strip_fences(result)
```

The `eval_json` fields:

| Field | Used for |
|-------|----------|
| `skill_name` | Skill directory name + card header |
| `problem_context` | Card: 🔍 问题发现 |
| `recommended_approach` | Card: 💡 推荐方案 |
| `when_to_use` | Card: 📋 适用场景 |
| `key_patterns` | Card: 关键模式 |
| `pitfalls` / `new_pitfalls` | Card: 已知雷区 |

---

## Model

`gemini-2.5-flash-preview-04-17` — upgraded from `gemini-3.1-flash-lite-preview`.

Flash-lite was cheaper but judgment quality was insufficient for the Skill boundary discrimination task. Flash provides significantly better NO_SKILL / qualify discrimination.

---

## Dedup via Existing Skills Injection

```python
def get_existing_skills_summary() -> str:
    lines = []
    for skill_md in ALL_SKILLS_DIR.rglob("SKILL.md"):
        if "/auto-learned/" in str(skill_md): continue
        # extract name + description from frontmatter
        lines.append(f"- {name}: {description}")
    return "\n".join(lines[:40])
```

Injected into prompt:
```
EXISTING SKILLS (do NOT create a skill that duplicates these):
- feishu-bitable: 飞书多维表格 CRUD 和批量操作
- knowledge-os: 知识库摄入、检索和维护
...
```
