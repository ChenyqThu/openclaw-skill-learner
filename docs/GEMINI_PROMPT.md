# Gemini Evaluation Prompt Design

## Philosophy

Inspired by Hermes Agent's `_SKILL_REVIEW_PROMPT`, which is intentionally minimal:

> *"Review the conversation above and consider saving or updating a skill if appropriate. Focus on: was a non-trivial approach used to complete a task that required trial and error, or changing course due to experiential findings along the way, or did the user expect or desire a different method or outcome?"*

Hermes captures two essential signals:
1. **Trial and error / course correction** — hard-won knowledge, not obvious upfront
2. **User corrected the agent** — the agent would repeat the mistake without documenting it

Our prompt extends this with structured output, OpenClaw-specific context, a mandatory Deviation Test, and quality scoring. The prompt is versioned and optimized using a Darwin-style hill-climbing approach.

---

## Prompt Versions

| Version | Score | Key changes |
|---------|-------|-------------|
| v1_baseline | 66.6/100 | Original prompt from Phase 2, no system context |
| v2_recall_dedup | 65.7/100 | +recall (pattern focus, debugging examples), -precision (too aggressive) |
| **v3_balanced** | **88.9/100** (+A/B patches) | +Deviation Test, +false positive guards, balanced recall/precision. Phase 4 adds A.4 rejection-context injection + A.5 cron red-flag relaxation + A.6 quality score weight correction + B.4 nomination block. |
| v4_rich_transcript | — (opt-in) | Phase 4C variant: reads full session JSONL via `load_full_session_transcript` (30k char budget, prioritizes `evidence_turns`), requires Gemini to cite `event_ref: "turn N"` in key_patterns. Degrades to v3 when no sessionFile. Enable with `PROMPT_VERSION=v4_rich_transcript`. |

Prompts are stored in `scripts/prompts/` and loaded via `PROMPT_VERSION` env var.

---

## New Skill Prompt Structure (v3)

```
[System Context — NEW in v3]
  OpenClaw description: AI agent orchestration platform
  Jarvis: primary agent, Feishu-accessible
  Session types: direct conversations, cron tasks, subagent spawning
  Tool set: exec, read/write/edit, process, sessions_*, web_fetch, feishu_*, etc.

[Pattern Focus — NEW in v3]
  "Focus on the PATTERN, not the surface context"
  Even debugging sessions can reveal reusable orchestration patterns
  Examples: launchd service → "diagnosing background service env issues"

[Session data]
  Tool count + names + skillsUsed (dedup signal)
  User messages + Agent responses

[Existing skills list → dedup]

[Hermes core test + self-correction signals]
  Trial-and-error keywords + Chinese self-correction: "操", "我想简单了", "想错了"

[Qualification criteria A+B + one of C-E]
  A: PATTERN reusable across ≥2 contexts (not the specific fix)
  B: agent workflow orchestration (approach transfers to other scenarios)
  C-E: trial-and-error / tool combos / user correction

[Deviation Test — NEW in v3, mandatory]
  Must identify a specific DEVIATION moment in the session
  No deviation = NO_SKILL
  Examples: tried A → failed → switched to B; discovered unexpected pitfall

[Red flags → NO_SKILL]
  Trivial one-off fix
  Cron that followed instructions without deviation
  Standard data pipeline with no surprises
  Agent merely synced data without obstacles

[Examples — expanded in v3]
  Example 1: PDF parsing (trial-and-error → QUALIFIES)
  Example 2: Notification debugging (pattern extraction from fix → QUALIFIES)
  Example 3: Cron data collection with failures (resilient pattern → QUALIFIES)
  Example 4: Config typo fix (trivial → NO_SKILL)
  Example 5: Daily journal cron, no deviation (routine → NO_SKILL)

[Reasoning steps — updated in v3]
  (1) What is the underlying PATTERN?
  (2) DEVIATION TEST: quote the specific moment
  (3) Reusable across ≥2 contexts?
  (4) Which of C, D, E apply?

[Output format]
  NO_SKILL  (if not qualified)
  OR:
  ```eval_json  → structured data + quality_score (0-100)
  ```skill_md   → SKILL.md content
```

## Phase 4 Prompt Enrichments (applied on top of v3)

### A.4 — rejection-context injection

Loads the last 10 entries from `~/.openclaw/workspace/data/skill-learner/rejection-context.json` and injects them as negative examples. Helper: `_load_recent_rejections_note()` in `v3_balanced.py`.

```
━━━ 用户已拒绝的历史提议 (不要重复) ━━━
- 曾提议「X」被 skip（原因: 过于特定）；原问题: Y ...
- 曾提议「X'」被 discuss（原因: 抽象层错）；...
...
如果本次 session 的模式在抽象层与上述任一被拒提议相同，应输出 NO_SKILL。
判断的是「抽象模式」，不是「表面话题」
```

### A.5 — cron red-flag relaxation

Old rule rejected any cron session that succeeded. New rule only rejects linear single-branch crons:

```
• Cron/scheduled task that ran as a LINEAR, SINGLE-BRANCH tool chain:
  no sub-agent spawns, no fallback branches, no retries, no source failures that required recovery.
  NOTE: cron tasks that orchestrate multi-hop parallel work, recover from partial source failures,
  combine tools in non-obvious ways, OR made a course-correction mid-run CAN qualify.
  Evaluate on DEVIATION / RECOVERY, not on whether the cron finished.
```

### A.6 — quality_score weight correction

The original template asked for stringified values (`"<1-10>"`) with weights summing to 100 but multiplied by 1-10 — giving a theoretical max of 1000. Fixed to unquoted ints with correct weights:

```
"quality_score": {
  "reusability": <1-10 整数>,
  "insight_depth": <1-10 整数>,
  "specificity": <1-10 整数>,
  "pitfall_coverage": <1-10 整数>,
  "completeness": <1-10 整数>,
  "total": <0-100 整数, 五项加权总分: reusability×2.5 + insight_depth×2.5 + specificity×2.0 + pitfall_coverage×1.5 + completeness×1.5>
}
```

Evaluator/server both also run `_coerce_int` defensively so string outputs are still accepted.

### B.4 — nomination high-trust block

When the plugin captured a `skill_learner_nominate` call, the request payload carries `nominated=true` plus the full `nominationPayload`. The prompt lifts this into an explicit trust signal:

```
━━━ AGENT SELF-NOMINATION (高信任信号) ━━━
Jarvis 在本次 session 结束前主动调用了 skill_learner_nominate。
  Topic: ...
  Pain point: ...
  Reusable pattern: ...
  Confidence: high|medium|low
  Evidence turns: turn 5, turn 12, turn 18

权重说明：Agent 自证发现了可沉淀模式,这是比外部观察更可靠的信号。
  • 若 session 内容能支持 Jarvis 的自述 → 倾向 QUALIFY
  • 若 session 与 nomination 完全不匹配 → 仍可 NO_SKILL
  • confidence=low 的 nomination 需要更强 session 佐证才 qualify
```

Polyfill case (agent wrote the file via `exec` but plugin couldn't capture the JSON payload) uses a reduced block — the gate still opens but the prompt doesn't get the trusted topic/pattern details.

### C — v4_rich_transcript full session loader

Activated via `PROMPT_VERSION=v4_rich_transcript`. Behavior differences:

1. When `request.sessionFile` points to a JSONL path, `load_full_session_transcript()` is called with:
   - `max_chars = int(os.environ.get("OMC_RICH_BUDGET", "30000"))`
   - `priority_turns = nominationPayload.evidence_turns`
2. Rich transcript is inserted before `━━━ WHAT IS AN OPENCLAW SKILL ━━━`
3. Grounding directive requires `key_patterns` to cite specific `turn N` references
4. Falls back to v3 behavior when no sessionFile

Update-path prompts still route to v3 until C.3 extends them.

---

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

**Phase 3 (data-driven)**: Replaced the keyword-based heuristic with feature analysis derived from 41 historical sessions. Designed for 0% false negative rate:

```python
def should_skip_session(request: dict) -> str | None:
    asst_texts = request.get("assistantTexts", [])
    total_asst_chars = sum(len(t) for t in asst_texts)

    # Signal 1: Very short assistant output — not enough substance
    if total_asst_chars < 100:
        return f"asst_chars={total_asst_chars} < 100"

    # Signal 2: Single tool type + no user messages = simple subagent execution
    tool_types = set(request.get("toolNames", []))
    user_msgs = request.get("userMessages", [])
    if len(tool_types) <= 1 and len(user_msgs) == 0 and request.get("toolCount", 0) < 10:
        return f"single_tool_no_user"

    return None  # proceed to Gemini
```

**Why not keyword-based?** Analysis of 41 sessions showed:
- `toolCount` doesn't discriminate (both completed and no_update averaged ~15)
- Correction keywords appeared in only 2/41 sessions (too rare to be useful)
- `asst_chars < 100` perfectly separates low-signal sessions (0% false negatives)

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
| `quality_score` | Quality gate + card header badge |

### Quality Score (new in Phase 3)

```json
"quality_score": {
  "reusability": 8,       // ×25
  "insight_depth": 7,     // ×25
  "specificity": 6,       // ×20
  "pitfall_coverage": 5,  // ×15
  "completeness": 7,      // ×15
  "total": 67             // weighted sum
}
```

Used by `evaluate-server.py` to gate notifications:
- `total ≥ 40`: Send Feishu Card 2.0 notification
- `total < 40`: Silently store draft, no notification

---

## Darwin Optimization Framework

The prompt is optimized using `scripts/eval-benchmark.py` and `scripts/darwin-optimize.py`:

1. **Labeled test set**: 18 sessions in `scripts/test-cases/` (8 extract + 6 reject + 4 update)
2. **6-dimension scoring**: accuracy (×35), precision (×20), recall (×15), quality (×15), dedup (×10), robustness (×5)
3. **Hill-climbing**: diagnose weakest dimension → targeted edit → re-evaluate → keep/revert
4. **Ratchet**: scores only go up; failed attempts are reverted

```bash
python3 scripts/eval-benchmark.py                    # run benchmark
python3 scripts/eval-benchmark.py --prompt v3_balanced  # specific version
python3 scripts/darwin-optimize.py --max-rounds 5    # auto-optimize
```

---

## Model

`gemini-3-flash-preview` — upgraded from `gemini-2.5-flash-preview-04-17`.

Flash-lite was cheaper but judgment quality was insufficient for the Skill boundary discrimination task. Flash provides significantly better NO_SKILL / qualify discrimination.

All Gemini calls now go through the shared `gemini_client.py` module (extracted in Phase 3 to eliminate duplication across 4 scripts).

---

## Track 1: Evolution Scoring (separate from evaluation)

Track 1 uses a different 8-dimension rubric for scoring SKILL.md quality (not session classification). See `skill_evolution.py` for details.

| Category | Dimension | Weight |
|----------|-----------|--------|
| Structure (60) | frontmatter, workflow_clarity, edge_case_coverage, checkpoint_design, instruction_specificity, resource_integration | 8+15+10+7+15+5 |
| Effectiveness (40) | architecture, test_performance | 15+25 |

The evolution prompt asks Gemini to score each dimension 1-10, then a meta-prompt targets the weakest dimension for improvement. This is distinct from the evaluation prompt optimization (which uses 6 classification-focused dimensions).

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
