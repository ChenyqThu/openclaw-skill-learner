#!/usr/bin/env bash
#
# init-workspace-git.sh — Idempotent git initialization for ~/.openclaw/workspace
#
# Prerequisite for Darwin ratchet mechanism: git revert requires version control.
# Only tracks skills/*/SKILL.md, skills/*/test-prompts.json, and core spec files.
# Ignores data/, auto-learned/, logs, and other transient files.
#
# Usage:
#   bash scripts/init-workspace-git.sh          # Initialize
#   bash scripts/init-workspace-git.sh --check  # Check status only

set -euo pipefail

WORKSPACE="$HOME/.openclaw/workspace"

# ─── Check mode ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--check" ]]; then
    if [ -d "$WORKSPACE/.git" ]; then
        cd "$WORKSPACE"
        echo "✅ Workspace git initialized"
        echo "   Commits: $(git rev-list --count HEAD 2>/dev/null || echo 0)"
        echo "   Branch:  $(git branch --show-current 2>/dev/null || echo '(none)')"
        git status --short
    else
        echo "❌ Workspace not yet git-initialized"
        echo "   Run: bash scripts/init-workspace-git.sh"
    fi
    exit 0
fi

# ─── Validate workspace exists ──────────────────────────────────────────────
if [ ! -d "$WORKSPACE" ]; then
    echo "ERROR: Workspace not found: $WORKSPACE"
    exit 1
fi

cd "$WORKSPACE"

# ─── Idempotent: skip if already initialized ────────────────────────────────
if [ -d ".git" ]; then
    COMMITS=$(git rev-list --count HEAD 2>/dev/null || echo 0)
    if [ "$COMMITS" -gt 0 ]; then
        echo "✅ Already initialized ($COMMITS commits). Nothing to do."
        exit 0
    fi
    echo "⚠️  .git exists but no commits — will create initial commit."
fi

# ─── Git init ────────────────────────────────────────────────────────────────
if [ ! -d ".git" ]; then
    git init
    echo "📁 git init done"
fi

# ─── Create .gitignore ──────────────────────────────────────────────────────
cat > .gitignore << 'GITIGNORE'
# Darwin Ratchet — only track skills and core spec files
# Everything else is ignored by default

# Ignore everything
*

# Track .gitignore itself
!.gitignore

# Track core spec files (read-only references for evolution, NOT auto-modified)
!AGENTS.md
!USER.md
!SOUL.md

# Track skills directory structure
!skills/
!skills/*/
!skills/**/

# Track SKILL.md and test-prompts.json in each skill
!skills/*/SKILL.md
!skills/*/test-prompts.json

# But ignore auto-learned drafts (not yet approved)
skills/auto-learned/

# Ignore all hidden/meta files inside skills
skills/*/.meta.json
skills/*/.eval.json
skills/*/.update-proposal.md
skills/*/.pending-review.json
GITIGNORE

echo "📝 .gitignore created"

# ─── Stage and commit ────────────────────────────────────────────────────────
git add .gitignore

# Add core spec files if they exist
for f in AGENTS.md USER.md SOUL.md; do
    [ -f "$f" ] && git add "$f"
done

# Add all tracked SKILL.md files (excluding auto-learned/)
find skills -name "SKILL.md" -not -path "*/auto-learned/*" 2>/dev/null | while read -r f; do
    git add "$f" 2>/dev/null || true
done

# Add test-prompts.json files
find skills -name "test-prompts.json" -not -path "*/auto-learned/*" 2>/dev/null | while read -r f; do
    git add "$f" 2>/dev/null || true
done

# Create initial commit
git commit -m "chore: initial commit for darwin ratchet

Tracks skills/*/SKILL.md + core spec files (AGENTS.md, USER.md, SOUL.md).
This commit serves as the baseline for the skill evolution ratchet mechanism."

echo ""
echo "✅ Workspace git initialized successfully"
echo "   Path: $WORKSPACE"
echo "   Tracked files:"
git ls-files | head -20
TRACKED=$(git ls-files | wc -l | tr -d ' ')
echo "   Total: $TRACKED files"
