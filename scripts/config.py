"""
Shared configuration for Skill Learner scripts.

Hardcoded single-user values are now backed by environment variables
with sensible defaults for backward compatibility.
"""

import os
from pathlib import Path

# ─── Feishu ──────────────────────────────────────────────────────────────────
FEISHU_TARGET_OPEN_ID = os.environ.get(
    "FEISHU_TARGET_OPEN_ID", "ou_8d1ce0fa1d435070ed695baeabe25adc"
)

# ─── Notion ──────────────────────────────────────────────────────────────────
NOTION_CALENDAR_DB = os.environ.get(
    "NOTION_CALENDAR_DB", "2f015375830d80b7b057cfe94de8a40c"
)

# ─── Health Data ─────────────────────────────────────────────────────────────
HEALTH_GITHUB_REPO = os.environ.get(
    "HEALTH_GITHUB_REPO", "ChenyqThu/health-data"
)

# ─── Paths ───────────────────────────────────────────────────────────────────
WORKSPACE = Path.home() / ".openclaw/workspace"
DATA_DIR = WORKSPACE / "data/skill-learner"
QUEUE_DIR = DATA_DIR / "analysis-queue"
SKILLS_DIR = WORKSPACE / "skills/auto-learned"
ALL_SKILLS_DIR = WORKSPACE / "skills"
MEMORY_MD = WORKSPACE / "MEMORY.md"
