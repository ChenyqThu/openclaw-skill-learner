# Architecture

## Overview

OpenClaw Skill Learner is a two-layer self-evolution system with **four tracks** plus a **Phase 4 supervision loop** redesign that inverts the signal model from *pure external inference* to *agent-participated + replay-validated*. The latest addition (**Track 4 — Skill Curator**, 2026-04-30) borrows from Hermes Agent's Curator design to close the "skills only ever go in" gap with per-skill telemetry, deterministic lifecycle, and bi-weekly consolidation review.

1. **Plugin layer** (`plugin/index.js`): Runs inside OpenClaw, zero network calls, collects session data + friction signals + agent nominations + per-skill usage telemetry via hooks
2. **Evaluator layer** (`scripts/`): Runs externally, gates on nomination/friction, calls Gemini API, validates drafts structurally + via replay, sends Feishu notifications, runs evolution loops + curator lifecycle

**Four Tracks**:
- **Track 0 (Skill Learning)**: Conversation → detect reusable patterns → generate candidates → human approval
- **Track 1 (Darwin Evolution)**: Friction detection → 8-dim scoring → hill-climbing → git ratchet → auto-commit/revert
- **Track 2 (User Modeling)**: Diary + conversation attribution → USER.md/SOUL.md proposals → human confirmation
- **Track 4 (Skill Curator)**: Per-skill telemetry → deterministic lifecycle (active→stale→archived) → bi-weekly Gemini consolidation review → Feishu approval

**Phase 4 additions** (2026-04):
- **A. Hard-gate validators** — both in evaluator (pre-write) and server (pre-card) reject malformed Gemini output
- **B. Agent self-nomination** — Jarvis calls `skill_learner_nominate` when it knows it did something reusable; evaluator only fires on `nominated OR friction≥3`
- **C. Rich transcript** — evaluator reads full session JSONL instead of truncated hook summaries (opt-in `PROMPT_VERSION=v4_rich_transcript`)
- **D. Replay gate** — skill draft must be loaded + produce expected tool trajectory before card fires
- **E. Cross-session clustering** — 14-day window → proactive skill proposals when `≥3` sessions share abstract intent
- **Feedback loop** — skip/discuss writes `rejection-context.json`; next prompt reads as negative examples

**Phase 4.2 additions** (2026-04-30, Track 4):
- **Per-skill telemetry** — plugin increments `read_count`/`applied_count`/`patch_count` to `skill-usage.json`
- **Frontmatter migration** — every SKILL.md gains `pinned/source/created_at`; Darwin must preserve them on rewrite
- **Deterministic state machine** — apply-based thresholds (30d auto-no-apply / 60d any-no-apply / 30d-stale→archive)
- **Pin protection** — `pinned: true` blocks Curator transitions, LLM review, and Darwin evolution
- **LLM consolidation** — every 14d when ≥5 active auto-learned: Gemini suggests merge/archive; Lucien approves via Feishu

See [OPENCLAW_COOPERATION_PHASE2.md](OPENCLAW_COOPERATION_PHASE2.md) for the OpenClaw-side changes Phase 4B/C/D depend on.

This plugin/evaluator separation is required by OpenClaw's security scanner, which blocks network calls from plugins.

---

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│ OpenClaw Agent Session                               │
│                                                      │
│  after_tool_call hook                                │
│    → increment per-run tool counter                  │
│    → detect SKILL.md reads (Read_tool)               │
│    → track errors per tool (friction signal)         │
│    → detect error after skill read (friction)        │
│    → [Phase B] capture skill_learner_nominate call   │
│    → [Phase B] capture nominations/*.json writes     │
│    → accumulate daily tool stats                     │
│                                                      │
│  agent_end hook                                      │
│    → if toolCount >= 15 (A.6):                       │
│        extract transcript summary                     │
│        scan userMessages for friction keywords       │
│        include nominated + nominationPayload (B.2)   │
│        forward sessionFile path if available (C)      │
│        HTTP POST → localhost:8300/evaluate           │
│        (fallback: write to analysis-queue/ on disk)  │
│                                                      │
│  session_end hook                                    │
│    → check MEMORY.md size (health warning)           │
│    → persist daily tool usage stats                  │
│    → fallback: queue remaining un-fired runs         │
└──────────────────┬──────────────────────────────────┘
                   │ HTTP POST (fire & forget, always 202)
                   ▼
┌─────────────────────────────────────────────────────┐
│ evaluate-server.py (localhost:8300)                  │
│                                                      │
│  [Phase B.3] Gate — runs FIRST in background thread  │
│    body.nominated OR body.frictionWeight ≥ 3?        │
│    → open: proceed                                   │
│    → closed: log skip + return (no Gemini call)      │
│    ENV OMC_SKIP_GATE=1 bypasses the gate             │
│                                                      │
│  Rate limit: 5 Gemini calls/min                      │
│  Concurrency: one evaluation at a time (Lock)        │
│  Evolution: separate lock, 2/hour rate limit         │
│                                                      │
│  Track 0 path:                                       │
│  1. Write queue file (audit trail + nominated field) │
│  2. Import & call process_queue()                    │
│  3. Detect new .eval.json files                      │
│  4. [Phase A.2] _validate_eval_card_ready →          │
│      reject auto-*/unknown names + structural check   │
│  5. [Phase D opt-in] replay_gate.replay_skill()      │
│  6. Send Feishu Card 2.0 notification (🧠 Skill 候选)│
│                                                      │
│  Track 1 path (if triggerEvolution=true):            │
│  1. Log friction signals                             │
│  2. Import skill_evolution.py                        │
│  3. Run SkillEvolver.evolve() in background thread   │
│  4. Send Feishu evolution report (🧬 Skill 进化)     │
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
│    v1_baseline → v2_recall_dedup → v3_balanced →     │
│    v4_rich_transcript (Phase 4C, reads session JSONL)│
│                                                      │
│  Prompt injection order (v3/v4):                     │
│    1. Existing skills (dedup)                        │
│    2. [A.4] rejection-context.json (last 10 skips)   │
│    3. [B.4] nomination block (if nominated=true)     │
│    4. [C] full session transcript (v4 only)          │
│                                                      │
│  Related skill? (hook signal > topic heuristic)      │
│    YES → build_update_skill_prompt()                 │
│    NO  → build_new_skill_prompt()                    │
│                                                      │
│  Call Gemini 3 Flash (with Deviation Test)            │
│    → parse eval_json + skill_md                       │
│    → [A.1] _validate_skill_candidate (6 hard checks)  │
│        name non-placeholder + frontmatter + ≥3 sections│
│        + problem_context ≥20 + approach ≥30 + q≥40    │
│    → on fail: status=no_skill_name|invalid_skill|... │
│        + validationErrors in queue file, NO disk write│
│    → on pass: write SKILL.md + .meta.json + .eval.json│
└──────────────────┬──────────────────────────────────┘
                   │ reads .eval.json
                   ▼
┌─────────────────────────────────────────────────────┐
│ Feishu Card 2.0 Notification (only if A.2 passes)    │
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
│                                                      │
│  [A.3] skip/discuss → rejection-context.json (FIFO50)│
│    ↑ next Gemini prompt reads last 10 as neg examples│
└─────────────────────────────────────────────────────┘

(Parallel) Phase E cron:
  cross_session_cluster.py scans analysis-queue last 14 days
    → Gemini clusters by abstract intent
    → ≥3 matching sessions in window → proactive-proposal-*.json
    → confidence = f(size, temporal_span, pattern_consistency)

(Parallel) Track 4 — Skill Curator
┌───────────────────────────────────────────────────────────────────────────┐
│ Plugin telemetry (real-time, plugin/index.js bumpSkillUsage)              │
│   after_tool_call: Read on /skills/X/SKILL.md       → read_count++        │
│                    Write/Edit on /skills/X/SKILL.md → patch_count++       │
│   agent_end:       skill in runSkillsUsed AND       → applied_count++     │
│                    toolCalls.length >= 5                                   │
│   sidecar: data/skill-learner/skill-usage.json                            │
│   serialization: in-process Promise chain + atomic tmp+rename             │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │ (counters drive decisions)
                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ ai.openclaw.skill-curator-cron (daily 05:30, post-Darwin)                 │
│   curator.py --tick                                                        │
│     evaluate_transitions(now): apply-based thresholds                      │
│       active → stale: auto_learned never-applied >30d  OR  any >60d       │
│       stale → archived: state_changed_at >30d AND not pinned              │
│     apply_archive: mv skills/X → skills/_archived/X-<date>                │
│       + sidecar set_state(archived) + workspace git commit                 │
│     pinned: true skips every transition                                    │
│                                                                            │
│   Inside tick: --llm-review-if-due                                         │
│     last_llm_review_at >14d AND active auto_learned >=5? → fire review    │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │ (when due)
                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ curator_llm.run_review (Gemini consolidation pass)                        │
│   1. collect_active_skills() — exclude pinned + archived                  │
│   2. build_prompt(curator_v1) — hard rules: applied>3 → never archive,    │
│      no cross-source merge, overlap must cite SKILL.md sections           │
│   3. call_gemini → JSON {consolidations, archives, keep}                  │
│   4. validate_review — drop unknown skills, applied>3, cross-source       │
│   5. write curator-reports/<ts>/{run.json, REPORT.md} + latest symlink    │
│   6. send_curator_report → Feishu 📚 card                                 │
└────────────────────────────┬──────────────────────────────────────────────┘
                             │ Lucien clicks 采纳/忽略
                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ skill_action.py curator_approve <rec_id> --run-ts <ts>                    │
│   apply_consolidation: concat source bodies into new skill (no LLM        │
│     rewrite at approve time), archive sources, single git commit          │
│   apply_archive_rec: lifecycle.apply_archive() with rec rationale          │
│   mark_recommendation(rec_id, "approved")                                  │
└───────────────────────────────────────────────────────────────────────────┘

Pin protection (defense in depth):
   curator state machine    → pinned skills skipped
   curator LLM review       → pinned excluded from prompt input
   skill_evolution.validate_skill → reads frontmatter, returns "Blocked: pinned"
   skill_evolution post-rewrite   → _missing_curator_fields auto-revert if Gemini
                                     drops pinned/source/created_at
   skill_action.revert      → NOT blocked (intentional human action)
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

### Track 1: Darwin Evolution (Phase 3)

**Why hook-triggered, not scheduled?**
Friction signals are most valuable in real-time — the user's correction happens during a specific conversation. Waiting for a cron job loses the context. The plugin detects friction and piggybacks it on the existing HTTP POST (no new network calls).

**Why 8-dimension scoring (not 6)?**
The prompt optimizer uses 6 dimensions focused on classification accuracy. Skill evolution needs different dimensions focused on SKILL.md quality — frontmatter, workflow clarity, edge cases, checkpoints, specificity, resources, architecture, and test performance.

**Why git branches for evolution?**
Each evolution runs on `auto-evolve/{skill}-{timestamp}` branch. If all rounds improve, the branch merges to main. If any round regresses, `git revert HEAD --no-edit` (safe revert, not reset). This ensures the ratchet: scores only go up.

**Why separate evolution lock?**
Evolution is expensive (multiple Gemini calls per round). The evolution lock is separate from the evaluation lock so Track 0 can continue while Track 1 runs. Rate limit: 2 evolutions/hour.

**Safety: SKILL.md only, never core files**
`SkillEvolver` has a hardcoded blocklist rejecting SOUL.md/AGENTS.md/USER.md. Only approved skills (not `auto-learned/`) are eligible. This is enforced at the engine level, not just by convention.

---

## Phase History

| Phase | Date | Key change |
|-------|------|------------|
| 1 (Batch) | 2026-04-10 | Plugin hooks + launchd 3:30 AM batch evaluation |
| 2 (Real-time) | 2026-04-12 | evaluate-server microservice + localhost HTTP trigger |
| 2D (State arc) | 2026-04-12 | state-arc-analyzer.py + user modeling signals |
| Card redesign | 2026-04-12 | Feishu Card 2.0 + Gemini prompt upgrade |
| 3 (Darwin prompts) | 2026-04-13 | Darwin-style prompt optimization (66.6→88.9), quality scoring, pluggable prompts, data-driven pre-filter |
| 3 (Track 1) | 2026-04-14 | Darwin skill evolution engine, friction detection, 8-dim rubric, git ratchet, workspace git init, gemini_client extraction, Feishu evolution report card |
| 3 (Track 2) | 2026-04-14 | User modeling analyzer, diary + correction signal attribution, Gemini-powered spec file proposals, profile_approve/reject actions, weekly cron |
| **4 (Supervision loop redesign)** | **2026-04-17** | A: strict validators at both tiers + rejection-context feedback + extract_skill_md frontmatter bugfix; B: agent self-nomination tool + polyfill + gate `nominated OR friction≥3` + prompt high-trust block + AGENTS.md protocol; C: session JSONL loader + v4_rich_transcript prompt variant; D: replay_gate.py skeleton; E: cross_session_cluster.py skeleton. 11 legacy auto-* drafts cleaned. |
| **4.1 (SDK-native integration)** | **2026-04-17 (same day)** | Reversed earlier "need OpenClaw upstream PR" position. Plugin SDK (`openclaw@2026.4.15`) already provides everything: `api.registerTool` (B.1), `after_tool_call.event.params` is fully transparent (C.1.b), `subagent_spawned/ended` hooks (C.1.c). Plugin bumped 546→915 lines in-repo. Evaluator now consumes `toolTrace` + `subagentSummaries` payloads; prompt distinguishes first-class nominations (`_firstClass: true`) from polyfill. Only C.1.a (agent internal decision) + D headless runner still need further work. |
| **4.2 (Track 4 — Skill Curator)** | **2026-04-30** | Borrowed from Hermes Agent Curator. Adds per-skill telemetry (`read_count`/`applied_count`/`patch_count` in `skill-usage.json`); migrates 31 SKILL.md files to add `pinned/source/created_at` frontmatter; deterministic state machine (active→stale→archived via apply-based thresholds); pin protection across Curator + Darwin (Darwin Gemini prompt mandates frontmatter preservation, post-write `_missing_curator_fields` guard auto-reverts); LLM consolidation review every 14d via Gemini with hard rules + local validator; conservative consolidation executor (concat bodies, no LLM rewrite); 6 new HTTP routes on evaluate-server (`/curator/{status,tick,run,pin,unpin,restore}`); 5 new `skill_action.py` verbs (pin/unpin/restore/curator_approve/curator_reject); new launchd cron at 05:30 daily. New files: `curator.py`, `curator_telemetry.py`, `curator_lifecycle.py`, `curator_llm.py`, `curator_actions.py`, `curator_migrate_frontmatter.py`, `prompts/curator_v1.py`. |

---

## Phase 4 Design Rationale

### Why external inference alone was failing

Phase 3's Track 0 routed all sessions with ≥8 tool calls to Gemini for pattern detection. In practice, only ~10% of the decision-relevant signal was reaching the prompt:
- Messages truncated to 1000/500 chars
- Tool params stripped except Read_tool path
- Agent reasoning tokens unreachable (OpenClaw hooks don't expose them)
- Sub-agent transcripts (`jarvis-exec` via `sessions_spawn`) completely isolated
- Skills that were *considered but not loaded* left no signal

Gemini had to guess whether a session contained a reusable pattern from these leftovers. Result: many malformed outputs that the downstream card loop couldn't distinguish from real proposals, leading to ~5% empty-card leak rate plus a steady trickle of low-quality "please check the draft" Feishu notifications that users learned to ignore.

### The architectural inversion

Phase 4 flips two things simultaneously:

1. **Agent-participated instead of pure inference** (Phase B): Jarvis, running inside the session, knows when it just did something novel. A tool call `skill_learner_nominate` costs the agent one turn; it makes an *intentional* signal that no amount of external observation can replicate. The evaluator becomes a *validator* of the nomination rather than a guesser.

2. **Landing-gate instead of trust-by-default** (Phases A + D): Every draft that lands on disk must pass strict structural checks (A.1). Every card that reaches Feishu must pass an additional `.eval.json` quality check (A.2). Before the gate reaches production, the draft must also pass replay validation (D) — do 3-5 user-like prompts actually load this skill and produce the expected tool trajectory?

These two inversions together cut noise by an estimated 95% while raising the signal quality of the proposals that survive.

### Why the rejection-context loop closes the feedback gap

Previously, when a user clicked Skip, the only effect was deleting the draft and adding the name to `skipped-skills.json`. Gemini saw none of this on its next run, so patterns the user had already rejected kept showing up.

Phase A.3 writes every skip/discuss (with reason) to `rejection-context.json` (FIFO 50, 30-day decay). The next prompt reads the last 10 entries and is instructed to output `NO_SKILL` when the abstract pattern matches. The loop is *abstract*, not *surface*: "this kind of multi-source orchestration" not "a skill named `foo-bar`".

### Why Phase D (replay gate) matters most for long-term quality

Structural validators (A.1/A.2) catch malformed output; rejection-context (A.3/A.4) kills repeat offenders; nomination (B) ensures Gemini only evaluates high-signal sessions. But none of these can distinguish a *well-written but useless* skill from a *well-written and useful* one. Replay is the answer: if the newly-proposed skill doesn't change how Jarvis behaves on similar future prompts, it's useless regardless of quality_score.

The replay gate is the skeleton today because the real runner requires OpenClaw headless mode. Until then, `replay_gate.py --dry-run` uses Gemini self-play to predict tool trajectories, which is cheaper but has higher false-pass rate. Phase D is designed to light up the moment OpenClaw exposes headless mode (see OPENCLAW_COOPERATION_PHASE2.md §D).

---

## Track 4 Design Rationale (Phase 4.2)

### Why Track 4 was needed

Track 0/1/2 solved skill *production* and *evolution* but left a hole: **skills only ever go in**. After 6 months of running Track 0, the auto-learned catalog had 4 skills, one of which (`绕过登录墙的人物语料调研`) had never been applied — surfaced clearly only after Track 4's per-skill telemetry started counting. Without telemetry, dead skills accumulate silently and pollute the catalog (wasted context tokens, lower ranking signal for live skills).

Hermes Agent (NousResearch) shipped a similar feature ("Curator") in late April 2026 to solve the same problem. Track 4 borrows the design and localizes it.

### Three counters, not two (departure from Hermes)

Hermes uses `view_count` (skill displayed) and `use_count` (skill loaded into prompt). OpenClaw doesn't surface these as separate hook events. The plugin's `runSkillsUsed` Map only sees "Read tool fired on SKILL.md". So we redefine the counter pair around signals we actually have:

- `read_count`: `Read_tool` on a SKILL.md path increments. Counts "agent considered this skill"
- `applied_count`: `agent_end` of a run that read the skill AND made ≥5 tool calls. Counts "skill was actually applied" — the threshold filters out "agent peeked at the skill then bailed"
- `patch_count`: `Write_tool`/`Edit_tool` on a SKILL.md increments. Tracks Track 1 evolution + manual edits

This gives the same diagnostic power as Hermes's pair (popularity vs effective use) using only signals OpenClaw exposes.

### Apply-based thresholds, not source-based

The proposal initially specified source-based thresholds (45d for `auto_learned`, 90d for `user_created`). The agent's exploration found this doesn't align with the actual signal — "has been applied at least once" is a much stronger evidence of value than "originated from auto-extraction".

Thresholds adopted:
- 30d-no-apply for never-applied `auto_learned` skills (catches the obvious dead `绕过登录墙的人物语料调研`-style skills early)
- 60d-since-apply for any skill that has been applied at least once (gives long-tail skills like `kos-manual-ingest-fallback` a longer grace period)
- 30d-stale → archived (recoverable via `--restore`, never deleted)

### Frontmatter as source of truth (vs sidecar-only)

The plan considered keeping `pinned/source/created_at` only in `skill-usage.json` (Hermes-style sidecar). Lucien chose to extend SKILL.md frontmatter directly. Trade-off:

| Choice | Pro | Con |
|--------|-----|-----|
| Frontmatter (chosen) | Visible when Lucien reads SKILL.md directly | Risk of Darwin Gemini stripping fields on rewrite |
| Sidecar-only | No migration needed | Pin status invisible without sidecar lookup |

Mitigation for the Darwin risk: the rewrite prompt explicitly mandates `pinned/source/created_at` preservation, plus `_missing_curator_fields` runs post-write and triggers auto-revert if any field disappears. Counters live in sidecar (high write frequency from plugin makes frontmatter writes wasteful and racy).

### Conservative consolidation (no LLM rewriting at approve time)

The Gemini review pass identifies overlapping skills and proposes consolidations. When Lucien approves, the action does NOT call Gemini again to merge the bodies — it concatenates the two source SKILL.md bodies under a "merge note" header, archives the originals, and asks Lucien to clean up by hand. This avoids LLM-introduced regressions at the moment Lucien is ready to commit.

### Why dedicated cron, not piggyback on Darwin

Darwin runs daily at 04:30. The plan considered piggybacking Curator at the end of Darwin's batch run, but rejected because:
- Darwin and Curator have unrelated lifecycles. Coupling means a Darwin failure blocks Curator (or vice versa)
- Curator needs to run even when Darwin has nothing to do
- Manual `curator.py --tick` invocation is cleaner without the Darwin coupling

Solution: dedicated `ai.openclaw.skill-curator-cron.plist` at **05:30** (1h after Darwin so any new evolution commits are visible). The 14-day LLM cadence fires from inside the daily tick by checking `_meta.last_llm_review_at` — no second cron needed.
