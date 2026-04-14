# 🧠 OpenClaw Skill Learner

**Auto-learn reusable skills from complex agent sessions.**

An OpenClaw plugin that brings Hermes-style self-evolution to the OpenClaw ecosystem — without modifying OpenClaw's source code.

> **Current Version**: Phase 3 (Darwin-style prompt optimization + quality scoring)
> **Evaluation Accuracy**: 88.9/100 (v3 prompt, 16/18 correct on labeled test set)
> **Full project doc**: [OpenClaw Skill Learner — 项目文档 & 实现全记录](https://www.feishu.cn/wiki/OqW8wQhj6iJbZnkbMxWcNzLgnlb)

---

## Architecture

```
Your conversations with Jarvis
  │
  ├── after_tool_call hook → counts tools, detects SKILL.md reads
  ├── agent_end hook → if ≥5 calls: HTTP POST → localhost:8300/evaluate
  └── session_end hook → memory health check + fallback queue write
                                    │
                   ┌────────────────┴────────────────┐
                   │  Phase 2 (real-time)             │  Phase 1 (fallback)
                   │  evaluate-server.py              │  launchd 3:30 AM cron
                   │  localhost:8300                  │  run-skill-learner.sh
                   └────────────────┬────────────────┘
                                    │
                        Gemini 3 Flash API
                        (structured eval_json + skill_md + quality_score)
                                    │
                              Pre-filter (data-driven)
                              + Deviation Test (v3 prompt)
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              New Skill?     Update Skill?      NO_SKILL
              + quality_score   + quality_score
                    │               │
          skills/auto-learned/  .update-proposal.md
          + .eval.json            + .eval.json
                    │               │
              Quality gate: ≥40 → notify, <40 → silent store
                    │               │
              Feishu Card 2.0 interactive notification
              (problem → approach → scenarios → quality score)
                    │
          [✅ 通过落地] [💬 方案优化讨论] [⏭ 跳过]
```

---

## Features

### 🔍 Smart Session Detection
- **`after_tool_call` hook**: Real-time tool call counting + precise skill usage detection (tracks actual `Read_tool` calls to `SKILL.md` files)
- **`agent_end` hook**: Fires HTTP POST to localhost:8300 for real-time evaluation when threshold met (≥5 tool calls)
- **`session_end` hook**: Memory health check, tool stats persistence, fallback queue write
- **Inbound message ID extraction**: Parses `[msg:om_xxx]` from session headers to enable reply-to-thread notifications

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

### 🔒 Security Design
- Plugin contains **zero network calls** (passes OpenClaw's security scanner)
- All Gemini API calls happen in the external evaluator script or evaluate-server
- Skill drafts require human approval before activation

---

## Installation

### 1. Install the Plugin

```bash
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
| `TOOL_CALL_THRESHOLD` | `5` | Minimum tool calls to mark for analysis |
| `EVALUATE_SERVER_URL` | `http://127.0.0.1:8300/evaluate` | Real-time eval server endpoint |
| `MEMORY_LINE_WARN` | `250` | MEMORY.md line warning threshold |
| `MEMORY_LINE_DANGER` | `300` | MEMORY.md line danger threshold |

### Evaluator (`scripts/skill-learner-evaluate.py`)

| Constant | Default | Description |
|----------|---------|-------------|
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model for evaluation |
| `PROMPT_VERSION` | (env var) | Pluggable prompt version (e.g., `v3_balanced`) |

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
├── skill-learner-evaluate.py         # Batch evaluator (Gemini API + quality scoring)
├── evaluate-server.py                # Real-time eval microservice (localhost:8300)
├── eval-benchmark.py                 # Darwin benchmark: 6-dimension scoring framework
├── darwin-optimize.py                # Hill-climbing optimizer (ratchet mechanism)
├── run-skill-learner.sh              # Wrapper for cron/launchd
├── state-arc-analyzer.py             # User state arc analysis (Phase 2D)
├── prompts/                          # Pluggable prompt versions
│   ├── v1_baseline.py                #   Original prompt (baseline)
│   ├── v2_recall_dedup.py            #   Recall + dedup improvements
│   └── v3_balanced.py                #   Production prompt (88.9/100)
├── test-cases/                       # Labeled test dataset (18 cases)
│   ├── should-extract/               #   Ground truth = YES (8 cases)
│   ├── should-reject/                #   Ground truth = NO (6 cases)
│   └── should-update/                #   Ground truth = UPDATE (4 cases)
└── darwin-results/                   # Optimization history + cached API results
    └── results.tsv                   #   Score tracking (darwin-skill compatible)

ai.openclaw.skill-learner.plist       # launchd: batch evaluator (3:30 AM)
ai.openclaw.skill-learner-server.plist # launchd: real-time server (persistent)
```

### Runtime Data (auto-created)

```
~/.openclaw/workspace/
├── data/skill-learner/
│   ├── analysis-queue/         # Pending session JSON files
│   ├── tool-usage-stats.json   # Daily tool usage statistics
│   ├── memory-health.json      # Latest memory health check
│   ├── server.log              # evaluate-server log
│   └── evaluate.log            # Batch evaluator log
└── skills/
    ├── auto-learned/           # Generated skill drafts
    │   ├── {skill-name}/
    │   │   ├── SKILL.md        # Skill draft
    │   │   ├── .meta.json      # Creation metadata
    │   │   └── .eval.json      # Structured Gemini evaluation (for card display)
    │   └── .pending-review.json
    └── {existing-skill}/
        ├── SKILL.md
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

## Roadmap

- [ ] **Card action handler**: Implement `card_action` plugin hook to handle button clicks (approve/discuss/skip) directly without text reply
- [ ] **SCAN layer**: Embed security scan into eval pipeline
- [ ] **User modeling**: Detect preference signals in `session_end` hook, auto-propose USER.md updates
- [ ] **Fallback model**: Automatic Gemini model fallback on rate limits
- [x] ~~**Darwin optimization**: Prompt hill-climbing with labeled test set~~ (done: v3, 88.9/100)
- [x] ~~**Quality scoring**: 0-100 score driving notification strategy~~ (done: quality_score in eval_json)
- [x] ~~**Data-driven pre-filter**: Replace keyword heuristic with feature analysis~~ (done: 0% false negative)

---

## License

MIT
