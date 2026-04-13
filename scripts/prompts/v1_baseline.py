"""
v1 Baseline — 从 skill-learner-evaluate.py 原样提取的提示词模板。
Darwin 优化的起点。
"""


def build_new_skill_prompt(request: dict, existing_summary: str) -> str:
    """Build prompt for evaluating whether a session should produce a NEW skill."""
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"

    user_msgs = request.get("userMessages", [])
    asst_msgs = request.get("assistantTexts", [])
    formatted_user = "\n".join(f"User [turn {i+1}]: {m}" for i, m in enumerate(user_msgs))
    formatted_asst = "\n".join(f"Agent [turn {i+1}]: {t}" for i, t in enumerate(asst_msgs))

    return f"""You are evaluating an AI agent session to decide if a reusable Skill should be created.

━━━ SYSTEM CONTEXT ━━━
OpenClaw is an AI agent orchestration platform. "Jarvis" is the primary agent instance,
accessible via Feishu (Chinese workplace platform, similar to Slack). Jarvis handles:
- Direct user conversations (Feishu DMs) — interactive problem-solving
- Cron/scheduled tasks — daily journal, intel gathering, morning reports, memory sync
- Subagent spawning — parallel task decomposition via sessions_spawn tool

Jarvis's tool set includes: exec (shell commands), read/write/edit (files), process (background jobs),
sessions_spawn/sessions_history (agent orchestration), web_fetch/browser (web), feishu_* (docs/messages),
and domain-specific tools (nano-banana-image, notebooklm, etc.).

Skills are stored as SKILL.md files in ~/.openclaw/workspace/skills/ and loaded by Jarvis at runtime
to guide behavioral patterns for recurring task types.

SESSION: {tool_info}

CONVERSATION:
{formatted_user}

AGENT RESPONSES:
{formatted_asst}

EXISTING SKILLS (do NOT create a skill that duplicates these):
{existing_summary}

━━━ WHAT IS AN OPENCLAW SKILL ━━━
A Skill is a reusable *agent behavioral pattern* — a guide for HOW Jarvis should approach
a class of tasks in the future. It is NOT: a code fix for one specific script, a one-time
optimization, or general programming advice.

Core test (from Hermes): "Did this session require trial and error, changing course due to
experiential findings, or did the user correct the agent's approach?" If yes → strong Skill candidate.

Trial-and-error signals to look for:
- "didn't work", "tried", "instead", "realized", "turns out", "actually"
- Agent changed approach mid-session after a failure
- User corrected the agent: "不对", "不是", "应该", "错了", "no,", "wrong"
- Agent self-corrected: "操", "赶紧恢复", "我想简单了", "想错了"

━━━ COMMON FALSE POSITIVES — DO NOT CREATE SKILLS FOR THESE ━━━
• Routine cron task execution (daily journal, morning report, memory sync, intel gathering)
  that follows a pre-defined script without encountering novel obstacles
• Subagent sessions that simply execute a delegated subtask without course correction
• Sessions where the agent just reads data and formats output (no tool orchestration discovery)
• One-off debugging that fixes a specific config/env issue (not a reusable pattern)

━━━ QUALIFICATION CRITERIA ━━━
Need ALL of (A) + (B), plus at least one of (C)–(E):

A. The pattern is reusable across ≥2 DIFFERENT future contexts
   ("different" = different problem domain, different file type, or different tool chain)
B. It's about Jarvis's tool usage or workflow orchestration — not "fix script X"
C. Required non-obvious trial and error or course correction to discover
D. Contains specific tool combos, parameters, or pitfalls worth documenting
E. The user corrected the agent's method — Jarvis would repeat the mistake without this

━━━ RED FLAGS → output NO_SKILL ━━━
• Improvement is "add error handling/retry to script X" → fix the script directly
• Pattern only applies to one specific file/cron/config
• The approach is obvious or already covered by an existing skill above

━━━ EXAMPLES ━━━

Example 1 — QUALIFIES (new skill):
Session: Agent tried 3 approaches to parse a complex PDF, first with plain text extraction (failed),
then with page-by-page OCR (too slow), finally discovered combining PyMuPDF structured extraction
with fallback OCR only for scanned pages. User said "that's much better, remember this approach."
→ This qualifies: trial-and-error (C), specific tool combo (D), reusable across PDF tasks (A).

Example 2 — NO_SKILL:
Session: Agent fixed a typo in config.yaml and restarted the service.
→ NO_SKILL: one-off fix, not a behavioral pattern, not reusable.

Example 3 — NO_SKILL:
Session: Agent added logging to a Python script to debug an error, found the bug, removed the logging.
→ NO_SKILL: standard debugging workflow, obvious approach, not worth documenting as a skill.

━━━ INSTRUCTIONS ━━━

Step 1 — REASONING (mandatory): Before deciding, analyze the session by answering:
  (1) Is the pattern reusable across ≥2 different contexts? Why or why not?
  (2) Is it about agent behavior/workflow, or just a code fix?
  (3) Which of criteria C, D, E apply? Cite specific evidence from the conversation.

Step 2 — DECISION:
  If NOT qualified: output your reasoning, then on a new line: NO_SKILL
  If qualified: output your reasoning, then BOTH blocks below in order.

⚠️ LANGUAGE REQUIREMENT: ALL text fields MUST be written in Simplified Chinese (简体中文). skill_name may use Chinese or a short English identifier. Do NOT write English prose in any field.

```eval_json
{{
  "skill_name": "<简洁名称，中文或短英文>",
  "problem_context": "<1-2句：这个模式解决什么反复出现的挑战，为什么不显而易见>",
  "recommended_approach": "<2-4句：核心洞察、为什么有效、何时应用>",
  "when_to_use": ["<场景1>", "<场景2>", "<场景3>"],
  "key_patterns": ["<具体工具组合或参数1>", "<模式2>"],
  "pitfalls": ["<雷区1>", "<雷区2>"]
}}
```

```skill_md
---
name: <name>
description: <一句话描述（中文）>
version: 1.0.0
tags: [<tag1>, <tag2>]
---

# <name>

## 适用场景
- 场景1
- 场景2
- 场景3

## 不适用场景
- 反模式1
- 反模式2

## 操作步骤
1. 第一步：做什么及原因
2. 第二步：...
3. 第三步：...

## 示例
**场景**：具体场景简述
**做法**：逐步操作
**结果**：预期产出

## 已知雷区
- 雷区1：错过会发生什么
- 雷区2：...

## 验证方式
- 如何确认成功生效

## 相关 Skill
- 列出相关已有 Skill，或写「无」
```"""


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    """Build prompt for evaluating whether a session reveals updates for an existing skill."""
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"

    user_msgs = request.get("userMessages", [])
    asst_msgs = request.get("assistantTexts", [])
    formatted_user = "\n".join(f"User [turn {i+1}]: {m}" for i, m in enumerate(user_msgs))
    formatted_asst = "\n".join(f"Agent [turn {i+1}]: {t}" for i, t in enumerate(asst_msgs))

    truncated_skill = skill_content[:3000]

    return f"""You are evaluating whether a session revealed new information to UPDATE an existing Skill.

━━━ SYSTEM CONTEXT ━━━
OpenClaw is an AI agent orchestration platform. "Jarvis" is the primary agent, accessible via
Feishu (Chinese workplace platform). Sessions include: direct conversations, cron tasks (daily
journal, intel gathering, morning reports), and subagent spawning for parallel work.

EXISTING SKILL "{skill_name}" (truncated):
{truncated_skill}

SESSION: {tool_info}

CONVERSATION:
{formatted_user}

AGENT RESPONSES:
{formatted_asst}

━━━ EVALUATION CRITERIA (Hermes-inspired) ━━━
Did this session reveal something NOT covered by the existing skill? Look for:
1. A pitfall or error the agent hit that the skill didn't warn about
2. A better/faster approach than what the skill describes (discovered via trial and error)
3. The user corrected the agent's method — indicating a gap in the skill's guidance
4. A new scenario where the skill applies but wasn't documented in "When to Use"

━━━ EXAMPLE ━━━

Example — QUALIFIES for update:
Existing skill: "Multi-Source Data Aggregation" describes using parallel API calls.
Session: Agent hit a rate limit on source B, had to add exponential backoff + circuit breaker.
User said "记住这个坑". The skill's Pitfalls section didn't mention rate limiting.
→ UPDATE: add rate limiting pitfall + backoff procedure step.

Example — NO_UPDATE:
Existing skill: "Git Branch Cleanup" describes pruning merged branches.
Session: Agent used the same procedure successfully on a different repo.
→ NO_UPDATE: skill worked as documented, no new information.

━━━ INSTRUCTIONS ━━━

Step 1 — REASONING (mandatory): Analyze the session and explain:
  (1) Did the agent encounter something the skill didn't cover?
  (2) What specific gap was revealed? Cite evidence from the conversation.
  (3) Is this gap generalizable (will other sessions hit it too)?

Step 2 — DECISION:
  If NONE of the criteria apply: output your reasoning, then: NO_UPDATE

  If update is warranted: output your reasoning, then BOTH blocks below.

⚠️ LANGUAGE REQUIREMENT: ALL text fields MUST be written in Simplified Chinese (简体中文). Do NOT write English prose in any field.

```eval_json
{{
  "skill_name": "{skill_name}",
  "problem_context": "<本次 session 发现了现有 Skill 中的什么空白>",
  "recommended_approach": "<更好的做法或修正，为什么是改进>",
  "when_to_use": ["<更新后的适用场景>"],
  "new_pitfalls": ["<新雷区1>", "<新雷区2>"],
  "key_changes": ["<修改什么及原因>"]
}}
```

```skill_update
## Sections to Add/Modify

### Pitfalls (append)
- New pitfall: ...

### Procedure (append or modify)
- Additional step: ...

### When to Use (append if new scenario)
- New scenario: ...

### When NOT to Use (append if new boundary)
- New anti-pattern: ...
```"""
