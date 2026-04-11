#!/bin/bash
# Skill Learner Evaluator — wrapper script for cron
# Loads env vars from OpenClaw .env and runs the evaluator
set -euo pipefail

# Load API keys
if [ -f "$HOME/.openclaw/.env" ]; then
  set -a
  source "$HOME/.openclaw/.env"
  set +a
fi

# Run evaluator
/opt/homebrew/bin/python3 "$HOME/.openclaw/workspace/scripts/skill-learner-evaluate.py" \
  >> "$HOME/.openclaw/workspace/data/skill-learner/evaluate.log" 2>&1
