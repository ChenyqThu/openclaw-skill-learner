# 🧠 OpenClaw Skill Learner

**Auto-learn and self-evolve reusable skills from agent sessions.**

An OpenClaw plugin that brings Hermes-style self-evolution to the OpenClaw ecosystem — without modifying OpenClaw's source code. Now with Darwin-style skill evolution (Track 1) and agent-nominated learning + replay validation (Phase 4).

> **Current Version**: Phase 4 (Supervision Loop Redesign)
> — Track 0/1/2 + Agent Self-Nomination + Rejection Feedback + Replay Gate + Cross-Session Pattern Mining
> **Evaluation Accuracy**: 88.9/100 (v3 prompt, 16/18 correct on labeled test set)
> **Evolution Engine**: 8-dimension scoring + hill-climbing ratchet
> **Full project doc**: [OpenClaw Skill Learner — 项目文档 & 实现全记录](https://www.feishu.cn/wiki/OqW8wQhj6iJbZnkbMxWcNzLgnlb)
> **Phase 4 design**: [plan file](../../../.claude/plans/review-openclaw-effervescent-squid.md) · **OpenClaw-side spec**: [OPENCLAW_COOPERATION_PHASE2.md](docs/OPENCLAW_COOPERATION_PHASE2.md)

---

## Architecture

```
Your conversations with Jarvis
  │
  ├── after_tool_call hook → counts tools, detects SKILL.md reads,
  │                          tracks errors + friction signals,
  │                          captures skill_learner_nominate (Phase B) + polyfill
  ├── agent_end hook → if ≥15 calls AND (nominated OR friction≥3):
  │                    HTTP POST → localhost:8300/evaluate
  │                    (Phase 4 gate — otherwise 202 skipped, no Gemini call)
  └── session_end hook → memory health check + fallback queue write
                                    │
                   ┌────────────────┴────────────────┐
                   │                                 │
            triggerEvolution?                  Standard eval
            frictionSkill?                    (Track 0 + Phase A validators)
                   │                                 │
            POST /evolve                     Gemini 3 Flash API
            skill_evolution.py               eval_json + skill_md
                   │                                 │
            Darwin 8-dim rubric              Pre-filter + Deviation Test +
            (structure 60 + effect 40)        Rejection-context injection (A.4)
                   │                                 │
            ┌──────┴──────┐                   _validate_skill_candidate()
         KEEP → git commit                    6 hard checks (A.1):
         DROP → git revert                      name, frontmatter, ≥3 sections,
                   │                             problem_ctx≥20, approach≥30,
            Feishu 🧬 进化卡片                   quality≥40
            [✅ 确认] [↩️ 回滚]                        │
                                         skills/auto-learned/ drafts
                                               │
                                    _validate_eval_card_ready (A.2)
                                               │
                              [opt] Phase D replay gate (replay_gate.py)
                                               │
                                       Feishu 🧠 Skill 候选卡片
                                       [✅ 通过落地] [💬 讨论] [⏭ 跳过 --reason "..."]
                                               │
                                   skip/discuss → rejection-context.json (A.3)
                                   → next Gemini prompt reads as negative examples

(Parallel) Phase E: cron scans analysis-queue every 14 days → Gemini clustering
  → ≥3 similar sessions → proactive-proposal-*.json → approval card
```

---

## Features

### 🔍 Smart Session Detection (Track 0)
- **`after_tool_call` hook**: Real-time tool call counting + precise skill usage detection + error/friction tracking
- **`agent_end` hook**: Fires HTTP POST to localhost:8300 for real-time evaluation when threshold met (≥8 tool calls). Scans user messages for friction signals (Track 1)
- **`session_end` hook**: Memory health check, tool stats persistence, fallback queue write
- **Inbound message ID extraction**: Parses `[msg:om_xxx]` from session headers to enable reply-to-thread notifications

### 🧬 Darwin Skill Evolution (Track 1)
- **Friction signal detection**: User correction, explicit feedback, repeated tool failures, errors after skill read, manual trigger ("优化 skill X")
- **8-dimension rubric**: Structure (60pts: frontmatter, workflow, edge cases, checkpoints, specificity, resources) + Effectiveness (40pts: architecture, test performance)
- **Hill-climbing ratchet**: Diagnose weakest dimension → Gemini generates improvement → re-evaluate → keep if improved, git revert if not
- **Auto test-prompt generation**: Gemini creates test-prompts.json for skills that don't have one
- **Feishu evolution report**: 🧬 card with score delta, trigger signals, git diff, confirm/rollback buttons
- **Git-backed**: All evolution on `auto-evolve/` branches, merged to main on success

### 🤖 Gemini-Powered Evaluation (Hermes-inspired)

Evaluation prompt inspired by Hermes `_SKILL_REVIEW_PROMPT`. Core test:

> *"Did this require trial and error, changing course due to experiential findings, or did the user correct the agent's approach?"*

**Qualification criteria** (need A+B plus one of C–E):
- **A**: Reusable across ≥2 different future contexts (not just this one script)
- **B**: About agent tool/workflow patterns, not "fix script X"
- **C**: Required non-obvious trial and error to discover
- **D**: Contains specific tool combos, parameters, or pitfalls worth documenting
- **E**: User corrected the agent's method

**Deviation Test** (v3, mandatory): Gemini must identify a specific moment where the agent deviated from the expected path (tried A → failed → switched to B, self-corrected, user corrected). No deviation = NO_SKILL.

**Red flags → NO_SKILL**:
- Pattern only applies to one specific file/config AND the approach is trivial
- Cron task that followed its instructions step-by-step without encountering obstacles
- Standard data read → format → output pipeline with no surprises

**Pre-filter** (data-driven): Sessions with `asst_chars < 100` or single-tool-type subagent sessions are skipped (0% false negative rate).

**Quality scoring**: Gemini outputs `quality_score` (0-100) based on 5 dimensions: reusability, insight depth, specificity, pitfall coverage, completeness. Score < 40 → silently stored without notification.

**Dedup**: Existing installed skills are injected into the prompt so Gemini can avoid creating duplicates.

### 📣 Rich Feishu Card 2.0 Notification

When a skill candidate is found, a structured interactive card is sent:

| Section | Content |
|---------|---------|
| Header | `🧠 Skill 候选 · 新建/更新 · {skill_name} · {quality_score}分` (orange/blue) |
| Body | 🔍 问题发现 + 💡 推荐方案 |
| Scenarios | 📋 适用场景 (bullet list) |
| Patterns | 关键模式 + 已知雷区 |
| Details | Collapsed grey panel: source, session, tool count, quality score |
| Input | `multiline_text` input for optimization suggestions |
| Buttons | ✅ 通过落地 · 💬 方案优化讨论 · ⏭ 跳过 |

Metadata is encoded in button `name` (`verb||base64(skill_name)||action`) — no visible hidden fields.

### 📊 Bonus: Memory Health + Tool Stats
- **Memory health monitoring**: Warns when `MEMORY.md` approaches size limits
- **Tool usage statistics**: Per-tool call count, error rate, and duration — rolling 30-day window

### 🎯 Phase 4: Supervision Loop Redesign (new 2026-04-17)

Fixes the two root failure modes of Track 0: *empty/malformed drafts leaking as Feishu cards* and *no useful suggestions because external inference lacks agent intent*.

**Four reinforcing layers** (see `/docs/OPENCLAW_COOPERATION_PHASE2.md` for the OpenClaw-side specs):

| Layer | What it does | Shipped |
|-------|-------------|---------|
| **A. Validators (hot-fix)** | `_validate_skill_candidate` in evaluator (6 hard checks) + `_validate_eval_card_ready` in server. Both reject malformed Gemini output before it can reach Feishu. Pre-existing `extract_skill_md` bug rejecting frontmatter drafts also fixed. | ✅ Live |
| **B. Agent nomination** | Jarvis self-calls `skill_learner_nominate` when a session meets 4 defined conditions. Evaluator only runs Gemini when `nominated OR friction≥3` (else returns 202 skipped). Rate-capped 3/run. Polyfill via file-write for agents without the tool. | ✅ Live + polyfill |
| **C. Rich transcript** | `load_full_session_transcript()` reads session JSONL with 30k-char budget, prioritizes `evidence_turns`. Opt-in via `PROMPT_VERSION=v4_rich_transcript`. Gemini outputs cite specific `turn N` references. | ✅ Skeleton (needs OpenClaw hooks for full value) |
| **D. Replay gate** | `replay_gate.py` generates test prompts from originating session → runs them against new SKILL.md → requires ≥50% skill-load rate + ≥60% trajectory overlap before card fires. Dry-run uses Gemini self-play when headless Jarvis unavailable. | ✅ Skeleton (real runner needs OpenClaw headless mode) |
| **E. Cross-session mining** | `cross_session_cluster.py` scans 14-day queue → Gemini clusters by abstract intent → ≥3 matching sessions produce proactive proposals. Confidence = f(size, temporal_span, pattern_consistency). | ✅ Skeleton |

**Feedback loop**: `rejection-context.json` (FIFO 50 entries, 30-day decay) captures every skip/discuss with user reason. Next Gemini prompt reads last 10 entries and is instructed to output NO_SKILL if the abstract pattern matches.

### 🔒 Security Design
- Plugin contains **zero network calls** (passes OpenClaw's security scanner)
- All Gemini API calls happen in the external evaluator script or evaluate-server
- Skill drafts require human approval before activation
- Phase A.1 validators block structurally invalid drafts from reaching disk
- Phase B gate drops untrusted sessions before any Gemini call (cost + noise control)

---

## Installation

### 1. Install the Plugin

```bash
openclaw plugins install ./plugin
openclaw gateway restart
# Verify: gateway.log should print `Plugin registered (Phase 3: Self-Evolution)`
```

⚠️ **Upgrade pitfall** — plugins installed via `openclaw plugins install` land at `~/.openclaw/extensions/jarvis-skill-learner/` (NOT `~/.openclaw/plugins/`). Never `cp` into `~/.openclaw/plugins/` — the runtime won't see it. To force a re-sync after editing `plugin/index.js`:

```bash
rm -rf ~/.openclaw/extensions/jarvis-skill-learner
openclaw plugins install ./plugin
openclaw gateway restart
```

### 2. Configure Environment

```bash
# Add to ~/.openclaw/.env
GEMINI_API_KEY=your-key-here
FEISHU_APP_ID=your-app-id        # For card notifications
FEISHU_APP_SECRET=your-secret
```

### 3. Start the Real-time Evaluation Server (Phase 2)

```bash
# Start manually
python3 scripts/evaluate-server.py

# Or install as a persistent launchd service (macOS)
cp ai.openclaw.skill-learner-server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ai.openclaw.skill-learner-server.plist
```

The server listens on `http://127.0.0.1:8300`. Check health:

```bash
curl http://127.0.0.1:8300/health
```

### 4. Schedule Fallback Evaluation (Batch Mode)

For sessions missed by the real-time server:

```bash
# macOS launchd (runs at 3:30 AM daily)
cp ai.openclaw.skill-learner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ai.openclaw.skill-learner.plist

# Or manually
python3 scripts/skill-learner-evaluate.py
python3 scripts/skill-learner-evaluate.py --dry-run  # preview only
```

---

## Configuration

### Plugin (`plugin/index.js`)

| Constant | Default | Description |
|----------|---------|-------------|
| `TOOL_CALL_THRESHOLD` | `15` | Lower bound for evaluation (Phase A.6 raised from 8) |
| `FRICTION_THRESHOLD` | `4` | Friction weight to trigger evolution |
| Nomination cap | `3/run` | Max `skill_learner_nominate` calls per run (hard-coded) |
| `EVALUATE_SERVER_URL` | `http://127.0.0.1:8300/evaluate` | Real-time eval server endpoint |
| `MEMORY_LINE_WARN` | `250` | MEMORY.md line warning threshold |
| `MEMORY_LINE_DANGER` | `300` | MEMORY.md line danger threshold |

### Evaluator (`scripts/skill-learner-evaluate.py` + `evaluate-server.py`)

| Constant / Env | Default | Description |
|----------|---------|-------------|
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model for evaluation |
| `PROMPT_VERSION` | (env var) | Pluggable prompt version (e.g., `v3_balanced`, `v4_rich_transcript`) |
| `FRICTION_FALLBACK_MIN` | `3` | Phase B gate: friction≥3 opens gate when not nominated |
| `OMC_SKIP_GATE` | `""` | Set to `"1"` to bypass Phase B gate (emergency/debug) |
| `OMC_RICH_BUDGET` | `30000` | Char budget for v4 rich transcript prompt |
| `REJECTION_CONTEXT_MAX` | `50` (FIFO) | Max entries in `rejection-context.json` |
| `REJECTION_CONTEXT_MAX_DAYS` | `30` | Auto-prune threshold |

### Quality Gate Thresholds

| Score Range | Behavior |
|-------------|----------|
| `≥ 40` | Normal notification via Feishu Card 2.0 |
| `< 40` | Silently stored, no notification (reduces approval noise) |

---

## File Structure

```
plugin/
├── index.js                          # OpenClaw plugin (hooks)
├── package.json
└── openclaw.plugin.json

scripts/
├── gemini_client.py                  # Shared Gemini API client (DRY extraction)
├── skill-learner-evaluate.py         # Batch evaluator (Gemini API + quality scoring)
├── skill_evolution.py                # Track 1: Darwin evolution engine (8-dim + ratchet)
├── evaluate-server.py                # Real-time eval + evolution microservice (:8300)
├── skill_action.py                   # Card callback handler (approve/skip/discuss/revert/profile_*)
├── user_modeling.py                  # Track 2: User modeling analyzer (diary + corrections → proposals)
├── eval-benchmark.py                 # Darwin benchmark: 6-dimension scoring framework
├── darwin-optimize.py                # Hill-climbing optimizer for eval prompts
├── init-workspace-git.sh             # Idempotent workspace git initialization
├── config.py                         # Shared configuration (paths, constants)
├── run-skill-learner.sh              # Wrapper for cron/launchd
├── state-arc-analyzer.py             # User state arc analysis (Phase 2D)
├── replay_gate.py                    # Phase 4D: replay validation gate for drafts (skeleton)
├── cross_session_cluster.py          # Phase 4E: 14-day pattern mining → proactive proposals
├── prompts/                          # Pluggable prompt versions
│   ├── v1_baseline.py                #   Original prompt (baseline)
│   ├── v2_recall_dedup.py            #   Recall + dedup improvements
│   ├── v3_balanced.py                #   Production prompt (88.9/100) — now with A.4 rejection ctx + B.4 nomination block
│   └── v4_rich_transcript.py         #   Phase 4C: rich transcript + turn citations (opt-in)
├── test-cases/                       # Labeled test dataset (18 cases)
│   ├── should-extract/               #   Ground truth = YES (8 cases)
│   ├── should-reject/                #   Ground truth = NO (6 cases)
│   └── should-update/                #   Ground truth = UPDATE (4 cases)
└── darwin-results/                   # Optimization history + cached API results
    ├── results.tsv                   #   Prompt optimization tracking
    └── evolution-results.tsv         #   Skill evolution tracking

ai.openclaw.skill-learner.plist       # launchd: batch evaluator (3:30 AM)
ai.openclaw.skill-learner-server.plist # launchd: real-time server (persistent)
ai.openclaw.skill-evolution-cron.plist # launchd: batch evolution (4:30 AM)
ai.openclaw.user-modeling-cron.plist  # launchd: weekly user modeling (Mon 5:00 AM)
```

### Runtime Data (auto-created)

```
~/.openclaw/workspace/
├── data/skill-learner/
│   ├── analysis-queue/         # Pending session JSON files (Phase B: carries nominated + frictionWeight)
│   ├── nominations/            # Phase B.1 polyfill drop zone: {runId}-{ts}.json
│   ├── proactive-proposals/    # Phase E: cross-session clustering outputs
│   ├── rejection-context.json  # Phase A.3: FIFO 50 skip/discuss entries (30-day decay)
│   ├── skipped-skills.json     # Phase A.3: persistent skill-name blacklist
│   ├── friction-signals.json   # Track 1 friction log
│   ├── correction-signals.json # Track 2 correction log
│   ├── tool-usage-stats.json   # Daily tool usage statistics
│   ├── memory-health.json      # Latest memory health check
│   ├── server.log              # evaluate-server log
│   └── evaluate.log            # Batch evaluator log
└── skills/                     # Git-tracked (darwin ratchet)
    ├── auto-learned/           # Generated skill drafts (git-ignored)
    │   ├── {skill-name}/
    │   │   ├── SKILL.md        # Skill draft
    │   │   ├── .meta.json      # Creation metadata
    │   │   ├── .eval.json      # Structured Gemini evaluation (for card display)
    │   │   └── .replay.json    # Phase D: replay verdict (when gate is active)
    │   └── .pending-review.json
    └── {existing-skill}/       # Approved skills (git-tracked)
        ├── SKILL.md            # Evolved by Track 1
        ├── test-prompts.json   # Auto-generated test prompts for evolution
        ├── .update-proposal.md # Update patch (if session found improvements)
        └── .eval.json          # Structured update evaluation
```

---

## Handling Approval Callbacks

When a user clicks a button on the Feishu notification card, the callback carries:

- `action.name`: `"approve||<base64(skill_name)>||<action>"` — decode to get skill name and action type
- `action.form_value.optimization_note`: User's optimization suggestion text (if any)

Decode example:
```python
import base64
parts = action_name.split("||")
verb = parts[0]            # "approve" | "discuss" | "skip"
skill_name = base64.urlsafe_b64decode(parts[1] + "==").decode()
skill_action = parts[2]    # "create" | "update"
```

> ⚠️ Card action handler (plugin `card_action` hook) is not yet implemented. Currently, approvals are done via text reply in the morning report. See [roadmap](#roadmap).

---

## Comparison with Hermes Agent

| Aspect | Hermes | OpenClaw Skill Learner |
|--------|--------|----------------------|
| Review execution | In-process background thread | External script / microservice (zero context cost) |
| Trigger | Nudge counter (every N tool calls) | Plugin hooks (`after_tool_call` / `agent_end`) |
| Evaluation model | Main agent model | Dedicated Gemini 2.5 Flash |
| Skill writing | Direct via `skill_manage` tool | Draft + human approval gate |
| Skill update detection | LLM self-judges | Precise hook-based SKILL.md read tracking |
| Dedup | None | Existing skills injected into eval prompt |
| Notification | None | Rich Feishu Card 2.0 with approval buttons |
| Security | Same process trust boundary | Plugin isolated; evaluator runs externally |

---

## Darwin Prompt Optimization

Evaluation prompts are optimized using a Darwin-style hill-climbing approach (inspired by [darwin-skill](https://github.com/alchaincyf/darwin-skill)):

```bash
# Run benchmark on current prompt
python3 scripts/eval-benchmark.py

# Run with a specific prompt version
python3 scripts/eval-benchmark.py --prompt v3_balanced

# Run full optimization loop (auto hill-climbing)
python3 scripts/darwin-optimize.py --max-rounds 5

# Preview without API calls (uses cached results)
python3 scripts/eval-benchmark.py --dry-run
```

**6-Dimension Scoring** (total 100):

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Accuracy | 35 | Correct YES/NO/UPDATE classification |
| Precision | 20 | Of predicted YES, how many are correct |
| Recall | 15 | Of actual YES, how many detected |
| Quality | 15 | eval_json completeness + skill_md usability |
| Dedup | 10 | Doesn't duplicate existing skills |
| Robustness | 5 | Output format parse success rate |

**Optimization History**:

| Version | Score | Key Change |
|---------|-------|------------|
| v1_baseline | 66.6 | Original prompt from Phase 2 |
| v2_recall_dedup | 65.7 | +recall but -precision (too aggressive) |
| v3_balanced | **88.9** | +Deviation Test, +pattern focus, +false positive guards |

**Ratchet mechanism**: Scores only go up. Each improvement attempt must beat the current best or gets reverted.

---

## Skill Evolution (Track 1)

Evolve approved skills using Darwin-style 8-dimension scoring:

```bash
# List eligible skills
python3 scripts/skill_evolution.py --list

# Dry-run evolution on a specific skill
python3 scripts/skill_evolution.py --skill messaging-patterns --dry-run

# Live evolution (commits to workspace git)
python3 scripts/skill_evolution.py --skill messaging-patterns

# Batch: scan friction log and evolve eligible skills
python3 scripts/skill_evolution.py --batch
```

**8-Dimension Scoring** (total 100):

| Category | Dimension | Weight | Description |
|----------|-----------|--------|-------------|
| Structure | frontmatter | 8 | Name, description, triggers |
| Structure | workflow_clarity | 15 | Numbered steps, I/O per step |
| Structure | edge_case_coverage | 10 | Error handling, fallbacks |
| Structure | checkpoint_design | 7 | User confirmation gates |
| Structure | instruction_specificity | 15 | Concrete params/examples |
| Structure | resource_integration | 5 | Linked references |
| Effect | architecture | 15 | Hierarchy, consistency |
| Effect | test_performance | 25 | Test-prompt execution quality |

**Friction signals** that trigger evolution:

| Signal | Weight | Detection |
|--------|--------|-----------|
| User correction ("不对/错了/wrong") | 3 | agent_end user message scan |
| Explicit feedback ("skill 有问题") | 3 | agent_end keyword match |
| Repeated tool failure (≥2x) | 2 | after_tool_call error counter |
| Error after skill read (within 5 calls) | 2 | after_tool_call correlation |
| Manual trigger ("优化 skill X") | forced | agent_end regex |

---

## User Modeling (Track 2)

Automatically detect USER.md / SOUL.md / AGENTS.md updates from diary entries and conversation corrections. **Never auto-commits** — generates proposals for human review.

```bash
# Run analysis (scans last 7 days of diaries + correction signals)
python3 scripts/user_modeling.py --analyze

# Preview without Gemini calls
python3 scripts/user_modeling.py --analyze --dry-run

# Custom lookback window
python3 scripts/user_modeling.py --analyze --days 14

# View pending proposals
python3 scripts/user_modeling.py --status

# Apply or reject a proposal
python3 scripts/user_modeling.py --apply <proposal-id>
python3 scripts/user_modeling.py --reject <proposal-id>
```

**Signal sources**: diary entries (`memory/YYYY-MM-DD.md`) + conversation correction signals (user says "不对/错了")

**Safety**: proposals only, no auto-commit. Applied changes write to spec files but Lucien decides whether to commit.

---

## Roadmap

### Done
- [x] **Track 0**: session → Gemini eval → candidate → approval (Phase 2)
- [x] **Track 1: Darwin evolution**: 8-dim scoring + hill-climbing ratchet for SKILL.md (Phase 3)
- [x] **Track 2: User modeling**: auto-propose USER.md/SOUL.md updates (Phase 3)
- [x] **Darwin prompt optimization**: Hill-climbing with labeled test set (v3, 88.9/100)
- [x] **Quality scoring**: 0-100 score driving notification strategy
- [x] **Data-driven pre-filter**: Replace keyword heuristic with feature analysis (0% false negative)
- [x] **Workspace git initialization**: Git-track skills/ for ratchet mechanism
- [x] **Phase 4A** (2026-04): Strict validators eliminate empty/malformed Feishu cards; rejection-context feedback loop; red-flag relaxation; `extract_skill_md` frontmatter bug fixed
- [x] **Phase 4B** (2026-04): Agent self-nomination via `skill_learner_nominate` + polyfill; `nominated OR friction≥3` gate; prompt high-trust block; AGENTS.md self-nomination protocol
- [x] **Phase 4C skeleton** (2026-04): `load_full_session_transcript` + `v4_rich_transcript` prompt (opt-in via `PROMPT_VERSION`)
- [x] **Phase 4D skeleton** (2026-04): `replay_gate.py` with test-prompt generator, trajectory overlap scorer, dry-run via Gemini self-play
- [x] **Phase 4E skeleton** (2026-04): `cross_session_cluster.py` for 14-day pattern mining → proactive proposals

### In flight (needs OpenClaw side — see `docs/OPENCLAW_COOPERATION_PHASE2.md`)
- [ ] **B.1 `skill_learner_nominate` tool** — first-class registration (currently polyfill via file-write)
- [ ] **C.1.a `skill_considered_rejected` hook** — captures which skills agent rejected (negative evidence)
- [ ] **C.1.b `after_tool_call.params` full pass-through** with secret redaction allowlist
- [ ] **C.1.c `sub_agent_spawn/complete` events** — cover `sessions_spawn` handoffs
- [ ] **D headless Jarvis runner** — replaces `HeadlessJarvisClient` stub for real replay validation

### Open (no OpenClaw dep)
- [ ] **Card action handler**: `card_action` hook so skip-with-reason writes directly
- [ ] **SCAN layer**: Embed security scan into eval pipeline
- [ ] **Fallback model**: Automatic Gemini model fallback on rate limits
- [ ] **Phase E cron**: launchd plist for weekly cross-session clustering

---

## License

MIT
