# Architecture

## Overview

OpenClaw Skill Learner is a two-layer system:

1. **Plugin layer** (`plugin/index.js`): Runs inside OpenClaw, zero network calls, collects session data via hooks
2. **Evaluator layer** (`scripts/`): Runs externally, calls Gemini API, sends Feishu notifications

This separation is required by OpenClaw's security scanner, which blocks network calls from plugins.

---

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│ OpenClaw Agent Session                               │
│                                                      │
│  after_tool_call hook                                │
│    → increment per-run tool counter                  │
│    → detect SKILL.md reads (Read_tool)               │
│    → accumulate daily tool stats                     │
│                                                      │
│  agent_end hook                                      │
│    → if toolCount >= 5:                              │
│        extract transcript summary from event.messages│
│        HTTP POST → localhost:8300/evaluate           │
│        (fallback: write to analysis-queue/ on disk)  │
│                                                      │
│  session_end hook                                    │
│    → check MEMORY.md size (health warning)           │
│    → persist daily tool usage stats                  │
│    → fallback: queue remaining un-fired runs         │
└──────────────────┬──────────────────────────────────┘
                   │ HTTP POST (fire & forget)
                   ▼
┌─────────────────────────────────────────────────────┐
│ evaluate-server.py (localhost:8300)                  │
│                                                      │
│  Rate limit: 5 Gemini calls/min                      │
│  Concurrency: one evaluation at a time (Lock)        │
│                                                      │
│  1. Write queue file (for audit trail)               │
│  2. Import & call process_queue()                    │
│  3. Detect new .eval.json files                      │
│  4. Send Feishu Card 2.0 notification                │
└──────────────────┬──────────────────────────────────┘
                   │ imports
                   ▼
┌─────────────────────────────────────────────────────┐
│ skill-learner-evaluate.py                            │
│                                                      │
│  Pre-filter (data-driven):                           │
│    asst_chars < 100 → skip                           │
│    single-tool + no user msgs → skip                 │
│                                                      │
│  Pluggable prompt (PROMPT_VERSION env var):           │
│    v1_baseline → v2_recall_dedup → v3_balanced       │
│                                                      │
│  Related skill? (hook signal > topic heuristic)      │
│    YES → build_update_skill_prompt()                 │
│    NO  → build_new_skill_prompt()                    │
│                                                      │
│  Call Gemini 3 Flash (with Deviation Test)            │
│    → parse eval_json (problem/approach/quality_score) │
│    → parse skill_md block                            │
│    → quality_score < 40 → silent store (no notify)   │
│    → write SKILL.md + .meta.json + .eval.json        │
└──────────────────┬──────────────────────────────────┘
                   │ reads .eval.json
                   ▼
┌─────────────────────────────────────────────────────┐
│ Feishu Card 2.0 Notification                         │
│                                                      │
│  Header: 🧠 Skill 候选 · {action} · {name} · {score}│
│  Body:   问题发现 + 推荐方案                          │
│          适用场景 + 关键模式 + 已知雷区               │
│          [折叠] 来源 & Session 详情 + 质量评分        │
│  Form:   multiline 优化建议输入框                     │
│  Buttons: ✅ 通过落地 / 💬 方案优化讨论 / ⏭ 跳过    │
│                                                      │
│  Button name encodes metadata:                       │
│  "verb||base64(skill_name)||action"                  │
└─────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### Why external evaluator (not inline)?

OpenClaw's security scanner blocks `fetch`/HTTP from plugins. More importantly, running Gemini evaluation *outside* the agent loop means:
- Zero context window cost
- No latency impact on the main conversation
- Evaluator failures don't affect agent behavior

### Why two evaluation paths?

| Path | Trigger | Use case |
|------|---------|----------|
| Real-time (`evaluate-server.py`) | `agent_end` HTTP POST | Normal sessions — result available immediately |
| Batch (`skill-learner-evaluate.py` via launchd) | 3:30 AM cron | Server was down, sessions queued on disk |

The batch path also acts as the audit trail — every session that triggers analysis writes a JSON file regardless of path taken.

### Why Feishu Card 2.0 (not 1.0)?

Card 1.0 doesn't support `input` elements. Card 2.0 is required for:
- `form` container with `input` (multiline text)
- `collapsible_panel` with styled headers
- `input_type: "multiline_text"` for the optimization suggestion field

**Key Card 2.0 constraints**:
- `input` must be inside `form`
- `collapsible_panel` cannot be inside `form`
- `input_type: "multiline_text"` for multi-line (not `multiline: true`)
- Buttons in `form` use `form_action_type: "submit"` — callback returns `action.name` + `action.form_value`
- No `action.value` for form-submit buttons → metadata encoded in button `name`

### Metadata encoding in button name

Form-submit buttons don't carry `action.value` in their callback. Instead, metadata is encoded as:

```
"approve||UmVzaWxpZW50IE11bHRpLVNvdXJjZQ==||create"
 ^verb    ^base64(skill_name)                 ^action
```

Decoded in the callback handler to route the approval action.

### Skill definition boundary

A Skill is a **reusable agent behavioral pattern** — NOT a one-time code fix.

Core test (from Hermes): *"Did this require trial and error, changing course, or user correction?"*

**Deviation Test** (v3): The prompt requires Gemini to identify a specific moment where the agent deviated from the expected path. Focus on the underlying PATTERN, not the surface context — even debugging sessions can reveal reusable orchestration patterns.

Red flags that disqualify a session from generating a Skill:
- Pattern only applies to one specific file/config AND the approach is trivial
- Cron task that followed instructions step-by-step without encountering obstacles
- Standard data read → format → output pipeline with no surprises

### Quality scoring

Gemini outputs `quality_score` (0-100) based on 5 weighted dimensions:
- **reusability** (×25): Can this pattern apply across different contexts?
- **insight_depth** (×25): How far beyond the obvious does it go?
- **specificity** (×20): Are the steps concrete and actionable?
- **pitfall_coverage** (×15): Are edge cases and failure modes documented?
- **completeness** (×15): Does the SKILL.md cover all sections?

Skills with `quality_score < 40` are silently stored without Feishu notification, reducing approval noise.

### Pluggable prompts

Prompt versions are stored in `scripts/prompts/` and selected via `PROMPT_VERSION` env var. This enables Darwin-style optimization: test a new prompt version against labeled data, keep it only if scores improve.

| Version | Score | Key change |
|---------|-------|------------|
| v1_baseline | 66.6 | Original prompt |
| v2_recall_dedup | 65.7 | +recall, -precision |
| v3_balanced | **88.9** | +Deviation Test, +pattern focus |

---

## Gemini Prompt Design

Inspired by Hermes `_SKILL_REVIEW_PROMPT` but more structured. See [GEMINI_PROMPT.md](GEMINI_PROMPT.md) for full details.

```
[System Context]
  OpenClaw/Jarvis description, tool set, session types

[Pattern Focus]
  Focus on PATTERN not surface context
  Even debugging can reveal reusable patterns

[Criteria A-E]
  A: PATTERN reusable ≥2 contexts
  B: agent workflow (approach transfers)
  C-E: trial-and-error / tool combos / user correction

[Deviation Test — mandatory]
  Must identify specific deviation moment
  No deviation = NO_SKILL

[Red flags + false positive guards]
[Existing skills list → dedup]

[Output format]
  eval_json → structured data + quality_score
  skill_md  → SKILL.md content
```

The `eval_json` block populates the notification card and includes `quality_score` for filtering. The `skill_md` block becomes the SKILL.md file.

---

## Phase History

| Phase | Date | Key change |
|-------|------|------------|
| 1 (Batch) | 2026-04-10 | Plugin hooks + launchd 3:30 AM batch evaluation |
| 2 (Real-time) | 2026-04-12 | evaluate-server microservice + localhost HTTP trigger |
| 2D (State arc) | 2026-04-12 | state-arc-analyzer.py + user modeling signals |
| Card redesign | 2026-04-12 | Feishu Card 2.0 + Gemini prompt upgrade |
| 3 (Darwin) | 2026-04-13 | Darwin-style prompt optimization (66.6→88.9), quality scoring, pluggable prompts, data-driven pre-filter |
