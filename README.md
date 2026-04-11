# 🧠 OpenClaw Skill Learner

**Auto-learn reusable skills from complex agent sessions.**

An OpenClaw plugin that brings Hermes-style self-evolution to the OpenClaw ecosystem — without modifying OpenClaw's source code.

## How It Works

```
Your conversations with the agent
  │
  ├── after_tool_call hook → counts tool calls, detects skill usage
  ├── agent_end hook → marks sessions with ≥5 tool calls for analysis
  └── session_end hook → extracts transcript summary → queues for evaluation
                                    │
                        ┌───────────┴───────────┐
                        │   Analysis Queue       │
                        │   (JSON files on disk)  │
                        └───────────┬───────────┘
                                    │
                    Cron / Manual trigger
                                    │
                        ┌───────────┴───────────┐
                        │   Evaluator Script     │
                        │   (Gemini Flash API)   │
                        └───────────┬───────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              New Skill?     Update Skill?      No Pattern
                    │               │               │
          skills/auto-learned/  .update-proposal.md  discard
                    │               │
                    └───────┬───────┘
                            │
                  Morning Report → User Review → Approve/Reject
```

## Features

### 🔍 Smart Session Detection
- **`after_tool_call` hook**: Real-time tool call counting + skill usage tracking
- **`agent_end` hook**: Threshold-based marking (≥5 tool calls = complex session)
- **`session_end` hook**: JSONL transcript extraction + queue writing

### 📝 Skill Creation & Update
- **New skills**: Generated from scratch when a novel reusable pattern is detected
- **Skill updates**: When a session *uses* an existing skill and encounters new pitfalls or better approaches, generates an `.update-proposal.md` patch
- **Precise detection**: Tracks actual `Read_tool` calls to `SKILL.md` files, not just topic overlap

### 📊 Bonus: Memory Health + Tool Stats
- **Memory health monitoring**: Checks `MEMORY.md` size at every session end, warns when approaching limits
- **Tool usage statistics**: Per-tool call count, error rate, and duration — rolling 30-day window

### 🔒 Security Design
- Plugin contains **zero network calls** (passes OpenClaw's security scanner)
- All Gemini API calls happen in the external evaluator script
- Skill drafts require human approval before activation

## Installation

### 1. Install the Plugin

```bash
openclaw plugins install ./plugin
```

### 2. Set Up the Evaluator

The evaluator script needs a Gemini API key:

```bash
# Add to ~/.openclaw/.env (or your environment)
GEMINI_API_KEY=your-key-here
```

### 3. Schedule Daily Evaluation

**macOS (launchd — recommended):**

```bash
# Edit the plist to match your paths, then:
cp ai.openclaw.skill-learner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/ai.openclaw.skill-learner.plist
```

**Linux (crontab):**

```bash
# Add to crontab -e:
30 3 * * * /path/to/scripts/run-skill-learner.sh
```

**Manual:**

```bash
python3 scripts/skill-learner-evaluate.py          # Process queue
python3 scripts/skill-learner-evaluate.py --dry-run # Preview only
```

### 4. Restart Gateway

```bash
openclaw gateway restart
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `TOOL_CALL_THRESHOLD` | 5 | Minimum tool calls to trigger analysis |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Model for skill evaluation |
| `MEMORY_LINE_WARN` | 250 | MEMORY.md line count warning threshold |
| `MEMORY_LINE_DANGER` | 300 | MEMORY.md line count danger threshold |

Edit `plugin/index.js` constants or `scripts/skill-learner-evaluate.py` to customize.

## File Structure

```
plugin/
├── index.js                  # OpenClaw plugin (hooks registration)
├── package.json              # Plugin package manifest
└── openclaw.plugin.json      # Plugin metadata

scripts/
├── skill-learner-evaluate.py # External evaluator (calls Gemini API)
└── run-skill-learner.sh      # Wrapper for cron/launchd

ai.openclaw.skill-learner.plist  # macOS launchd schedule
```

### Runtime Data (created automatically)

```
~/.openclaw/workspace/
├── data/skill-learner/
│   ├── analysis-queue/       # Pending session analysis requests
│   ├── tool-usage-stats.json # Daily tool usage statistics
│   ├── memory-health.json    # Latest memory health check
│   └── evaluate.log          # Evaluator script log
└── skills/auto-learned/      # Generated skill drafts (pending review)
    ├── {skill-name}/
    │   ├── SKILL.md           # Skill draft
    │   └── .meta.json         # Creation metadata
    └── .pending-review.json   # Review queue for morning report
```

## Inspired By

This project is inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s self-improving learning loop, adapted for the OpenClaw ecosystem. Key differences:

| Aspect | Hermes | OpenClaw Skill Learner |
|--------|--------|----------------------|
| Review execution | In-process background thread | External script (zero context cost) |
| Trigger mechanism | Nudge counter in agent loop | Plugin hooks (`after_tool_call` / `agent_end` / `session_end`) |
| Skill writing | Direct via `skill_manage` tool | Draft + human approval gate |
| Skill updates | LLM self-judges relevance | Precise hook-based skill usage detection |
| Security | Same process trust boundary | Plugin isolated from network; evaluator runs externally |

For the full research report comparing Hermes and OpenClaw architectures, see the [Evaluation Document](https://te3ozb67sn.feishu.cn/wiki/PATGwIiFfiMBevksWthc4gbdn0d).

## Morning Report Integration

If you use an OpenClaw morning report system, the plugin writes `.pending-review.json` which can be consumed by your report card builder. See the [project documentation](https://te3ozb67sn.feishu.cn/wiki/PLACEHOLDER) for integration details.

## License

MIT
