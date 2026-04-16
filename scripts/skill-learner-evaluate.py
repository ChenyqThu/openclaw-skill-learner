#!/usr/bin/env python3
"""
Skill Learner Evaluator — Process analysis queue, create or update skills.

Reads pending analysis requests, calls Gemini to evaluate sessions.
Supports both NEW skill creation and UPDATING existing skills.

Usage:
  python3 skill-learner-evaluate.py           # Process all pending
  python3 skill-learner-evaluate.py --dry-run # Preview without creating skills
"""

import fcntl
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta

from gemini_client import load_env, call_gemini, extract_eval_json, extract_skill_md

QUEUE_DIR = Path.home() / ".openclaw/workspace/data/skill-learner/analysis-queue"
SKILLS_DIR = Path.home() / ".openclaw/workspace/skills/auto-learned"
ALL_SKILLS_DIR = Path.home() / ".openclaw/workspace/skills"
PENDING_REVIEW = SKILLS_DIR / ".pending-review.json"

DRY_RUN = "--dry-run" in sys.argv

# ─── Pluggable Prompt Loading ───────────────────────────────────────────────
# Set PROMPT_VERSION env var to use an optimized prompt (e.g., "v2_r1")
# Defaults to built-in prompts if not set or module not found.

_prompt_module = None

def _load_prompt_module():
    global _prompt_module
    version = os.environ.get("PROMPT_VERSION")
    if not version:
        return None
    prompt_file = Path(__file__).parent / "prompts" / f"{version}.py"
    if not prompt_file.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"prompts.{version}", str(prompt_file))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _prompt_module = mod
        print(f"Loaded prompt version: {version}")
        return mod
    except Exception as e:
        print(f"WARN: Failed to load prompt {version}: {e}, using built-in")
        return None

_load_prompt_module()


# ─── Pre-filter (data-driven) ───────────────────────────────────────────────

def should_skip_session(request: dict) -> str | None:
    """
    Data-driven pre-filter. Returns skip reason string, or None to proceed.
    Designed for <5% false negative rate on labeled test data.
    """
    asst_texts = request.get("assistantTexts", [])
    total_asst_chars = sum(len(t) for t in asst_texts)

    # Signal 1: Very short assistant output — not enough substance for a skill
    if total_asst_chars < 100:
        return f"asst_chars={total_asst_chars} < 100"

    # Signal 2: Single tool type + no user messages = simple subagent execution
    tool_types = set(request.get("toolNames", []))
    user_msgs = request.get("userMessages", [])
    if len(tool_types) <= 1 and len(user_msgs) == 0 and request.get("toolCount", 0) < 10:
        return f"single_tool_no_user (types={tool_types})"

    return None


# ─── Result Parsers ──────────────────────────────────────────────────────────

def _extract_eval_json(result: str) -> dict:
    """Extract the ```eval_json block from Gemini output and parse it."""
    return extract_eval_json(result)


def _extract_skill_md(result: str) -> str:
    """Extract the ```skill_md block, falling back to the full result."""
    return extract_skill_md(result)


def _extract_name_from_result(result: str) -> str | None:
    """Fallback: extract name from YAML frontmatter 'name:' line."""
    for line in result.strip().split("\n"):
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None



# call_gemini is now imported from gemini_client


# ─── Existing Skill Scanner ──────────────────────────────────────────────────

def scan_existing_skills() -> dict:
    """Scan all installed skills, return {name: {description, path, tags}}."""
    skills = {}
    for skill_md in ALL_SKILLS_DIR.rglob("SKILL.md"):
        try:
            content = skill_md.read_text()[:2000]
            name = skill_md.parent.name
            # Extract description from frontmatter
            desc = ""
            tags = []
            if content.startswith("---"):
                fm_match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
                if fm_match:
                    fm = fm_match.group(1)
                    desc_match = re.search(r'description:\s*[|>]?\s*\n?\s*(.+)', fm)
                    if desc_match:
                        desc = desc_match.group(1).strip()
                    tags_match = re.search(r'tags:\s*\[(.+?)\]', fm)
                    if tags_match:
                        tags = [t.strip().strip("'\"") for t in tags_match.group(1).split(",")]
            skills[name] = {
                "description": desc,
                "path": str(skill_md),
                "tags": tags,
            }
        except Exception:
            continue
    return skills


def find_related_skill(request: dict, existing_skills: dict) -> tuple:
    """Check if session's tools/topics overlap with an existing skill.
    Returns (skill_name, skill_info) or (None, None)."""
    tool_names = set(t.lower() for t in request.get("toolNames", []))
    user_text = " ".join(request.get("userMessages", [])).lower()

    for name, info in existing_skills.items():
        desc = info["description"].lower()
        tags = [t.lower() for t in info["tags"]]

        # Check overlap: skill tags/name mentioned in user messages or tool names
        name_words = set(name.replace("-", " ").split())
        overlap_score = 0
        for word in name_words:
            if len(word) >= 3 and word in user_text:  # skip short words like "a", "to"
                overlap_score += 2
        for tag in tags:
            if tag in user_text or tag in tool_names:
                overlap_score += 1
        for tool in tool_names:
            if tool in desc or tool in name:
                overlap_score += 1

        if overlap_score >= 5:
            return name, info

    return None, None


# ─── Prompt Builders ─────────────────────────────────────────────────────────

def get_existing_skills_summary() -> str:
    """Return a compact summary of installed skills for dedup context."""
    summary_lines = []
    for skill_md in ALL_SKILLS_DIR.rglob("SKILL.md"):
        if "/auto-learned/" in str(skill_md):
            continue
        try:
            text = skill_md.read_text()[:1500]
            name = skill_md.parent.name
            desc = ""
            fm = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
            if fm:
                dm = re.search(r"description:\s*(.+)", fm.group(1))
                if dm:
                    desc = dm.group(1).strip().strip("\"'")[:200]
            if desc:
                summary_lines.append(f"- {name}: {desc}")
        except Exception:
            continue
    return "\n".join(summary_lines[:60]) if summary_lines else "(none)"


def _format_messages(request: dict) -> tuple[str, str]:
    """Format user and assistant messages with role + turn markers."""
    user_msgs = request.get("userMessages", [])
    asst_msgs = request.get("assistantTexts", [])
    formatted_user = "\n".join(f"User [turn {i+1}]: {m}" for i, m in enumerate(user_msgs))
    formatted_asst = "\n".join(f"Agent [turn {i+1}]: {t}" for i, t in enumerate(asst_msgs))
    return formatted_user, formatted_asst


def build_new_skill_prompt(request: dict) -> str:
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    formatted_user, formatted_asst = _format_messages(request)
    existing = get_existing_skills_summary()

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
{existing}

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

IMPORTANT — Focus on the PATTERN, not the surface context:
Even if a session is about a specific system (e.g., fixing evaluate-server.py), the
underlying pattern may be highly reusable. Ask: "Would this approach help Jarvis in a
DIFFERENT system with a similar class of problem?" For example:
- Debugging a launchd service → pattern: "diagnosing background service env issues"
- Fixing notification delivery → pattern: "tracing multi-hop notification pipelines"
- Data pipeline with error recovery → pattern: "resilient multi-source data collection"

━━━ QUALIFICATION CRITERIA ━━━
Need ALL of (A) + (B), plus at least one of (C)–(E):

A. The PATTERN (not the specific fix) is reusable across ≥2 DIFFERENT future contexts
   ("different" = different problem domain, different file type, or different tool chain)
B. It's about Jarvis's tool usage or workflow orchestration — not "fix script X"
   NOTE: Even sessions that FIX something can reveal reusable orchestration patterns.
   The question is whether the APPROACH (not the fix itself) transfers to other scenarios.
C. Required non-obvious trial and error or course correction to discover
D. Contains specific tool combos, parameters, sequencing, or pitfalls worth documenting
E. The user corrected the agent's method — Jarvis would repeat the mistake without this

━━━ DEVIATION TEST (mandatory) ━━━
Before qualifying a skill, you MUST identify a specific DEVIATION — a moment where the
agent's path diverged from what would be expected. Examples of deviations:
- Agent tried approach A, it failed, switched to approach B
- Agent discovered an unexpected pitfall mid-execution
- User corrected the agent's direction
- Agent combined tools in a non-obvious sequence
If you cannot point to a specific deviation moment, output NO_SKILL.

━━━ RED FLAGS → output NO_SKILL ━━━
• Pattern only applies to one specific file/config AND the approach is trivial
• The session merely follows a pre-written script without any deviation or discovery
• The approach is obvious (standard debugging, simple file edit, routine API call)
• An existing skill above already covers this exact pattern
• Cron/scheduled task that completed successfully by following its instructions step-by-step
  (even if it uses many tools — using many tools is NOT the same as discovering a pattern)
• Session describes a standard data read → format → output pipeline with no surprises
• Agent merely synced data between two systems without encountering obstacles

━━━ EXAMPLES ━━━

Example 1 — QUALIFIES (new skill):
Session: Agent tried 3 approaches to parse a complex PDF, first with plain text extraction (failed),
then with page-by-page OCR (too slow), finally discovered combining PyMuPDF structured extraction
with fallback OCR only for scanned pages. User said "that's much better, remember this approach."
→ This qualifies: trial-and-error (C), specific tool combo (D), reusable across PDF tasks (A).

Example 2 — QUALIFIES (pattern from debugging session):
Session: Agent was debugging why Feishu card notifications failed. Traced the issue through:
plugin → HTTP POST → evaluate-server → Feishu API → discovered launchd env var missing.
→ This qualifies: the PATTERN "tracing notification delivery through multi-hop pipeline"
  is reusable for any notification/webhook debugging (A), required trial-and-error (C),
  specific diagnostic sequence (D).

Example 3 — NO_SKILL:
Session: Agent fixed a typo in config.yaml and restarted the service.
→ NO_SKILL: one-off fix, trivial approach, not a behavioral pattern.

Example 4 — NO_SKILL:
Session: Daily journal cron — agent reads session logs, writes diary entry, updates index.
No errors, no course corrections, followed the script exactly.
→ NO_SKILL: routine execution without any novel discovery or approach change.

━━━ INSTRUCTIONS ━━━

Step 1 — REASONING (mandatory): Before deciding, analyze the session by answering:
  (1) What is the underlying PATTERN? (abstract away from the specific system)
  (2) DEVIATION TEST: Point to the specific moment the agent deviated from the expected
      path. Quote the relevant text. If no deviation exists, this is NOT a skill.
  (3) Is this pattern reusable across ≥2 different contexts? Why or why not?
  (4) Which of criteria C, D, E apply? Cite specific evidence from the conversation.

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
  "pitfalls": ["<雷区1>", "<雷区2>"],
  "quality_score": {{
    "reusability": <1-10, 能跨多少不同场景使用>,
    "insight_depth": <1-10, 多大程度超越显而易见的做法>,
    "specificity": <1-10, 步骤是否足够具体可执行>,
    "pitfall_coverage": <1-10, 雷区和边界情况覆盖>,
    "completeness": <1-10, SKILL.md 各节是否完整>,
    "total": <0-100, 五项加权总分: reusability×25 + insight_depth×25 + specificity×20 + pitfall_coverage×15 + completeness×15>
  }}
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
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    formatted_user, formatted_asst = _format_messages(request)
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
  "key_changes": ["<修改什么及原因>"],
  "quality_score": {{
    "reusability": <1-10>,
    "insight_depth": <1-10>,
    "specificity": <1-10>,
    "pitfall_coverage": <1-10>,
    "completeness": <1-10>,
    "total": <0-100, 五项加权总分: reusability×25 + insight_depth×25 + specificity×20 + pitfall_coverage×15 + completeness×15>
  }}
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

# ─── Processing ──────────────────────────────────────────────────────────────

def process_queue():
    if not QUEUE_DIR.exists():
        print("No analysis queue found.")
        return

    pending_files = sorted(QUEUE_DIR.glob("*.json"))
    pending_files = [f for f in pending_files if json.loads(f.read_text()).get("status") == "pending"]

    if not pending_files:
        print("Queue empty. No pending sessions.")
        return

    print(f"Found {len(pending_files)} pending request(s)")

    existing_skills = scan_existing_skills()
    print(f"Scanned {len(existing_skills)} existing skills for overlap detection\n")

    skills_created = []
    skills_updated = []

    for req_file in pending_files:
        # Acquire exclusive lock to prevent concurrent processing
        lock_fd = None
        try:
            lock_fd = open(req_file, "r+")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            # Another process holds the lock, skip this file
            if lock_fd:
                lock_fd.close()
            continue

        try:
            request = json.loads(req_file.read_text())

            print(f"─── {req_file.name} ───")
            print(f"  Tools: {request['toolCount']} ({', '.join(request.get('toolNames', [])[:6])})")

            # Data-driven pre-filter (conservative: <5% false negative rate)
            skip_reason = should_skip_session(request)
            if skip_reason:
                print(f"  ⏭️ Pre-filtered: {skip_reason}")
                request["status"] = "pre_filtered"
                request["skipReason"] = skip_reason
                req_file.write_text(json.dumps(request, indent=2))
                continue

            # Check if session actually USED an existing skill (precise signal from hook)
            # Fall back to topic-overlap heuristic if no hook data
            related_name, related_info = None, None
            skills_used = request.get("skillsUsed", [])
            if skills_used:
                # Precise: plugin detected Read_tool loading SKILL.md
                for sname in skills_used:
                    if sname in existing_skills and "/auto-learned/" not in existing_skills[sname]["path"]:
                        related_name = sname
                        related_info = existing_skills[sname]
                        print(f"  📌 Skill actually used (from hook): {sname}")
                        break
            if not related_name:
                # Fallback: topic overlap heuristic
                related_name, related_info = find_related_skill(request, existing_skills)

            if related_name:
                print(f"  📎 Related to existing skill: {related_name}")
                if DRY_RUN:
                    print("  [DRY RUN] Would evaluate for skill UPDATE")
                    request["status"] = "dry_run"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                skill_content = Path(related_info["path"]).read_text()
                if _prompt_module:
                    prompt = _prompt_module.build_update_skill_prompt(request, related_name, skill_content)
                else:
                    prompt = build_update_skill_prompt(request, related_name, skill_content)
                result = call_gemini(prompt)

                if not result:
                    request["status"] = "error"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                if result.strip().startswith("NO_UPDATE"):
                    print("  ⏭️ No updates needed for existing skill")
                    request["status"] = "no_update"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                # Parse eval_json block if present
                eval_data = _extract_eval_json(result)

                # Write update proposal next to existing skill
                patch_file = Path(related_info["path"]).parent / ".update-proposal.md"
                patch_content = f"# Update Proposal for {related_name}\n"
                patch_content += f"Generated: {datetime.now().isoformat()}\n"
                patch_content += f"Source session: {req_file.name}\n\n"
                patch_content += result
                patch_file.write_text(patch_content)

                # Write eval.json for richer notification card
                eval_file = Path(related_info["path"]).parent / ".eval.json"
                eval_file.write_text(json.dumps({
                    "action": "update",
                    "generatedAt": datetime.now().isoformat(),
                    "sourceRequest": req_file.name,
                    "toolCount": request["toolCount"],
                    "toolNames": request.get("toolNames", []),
                    "lastInboundMessageId": request.get("lastInboundMessageId"),
                    **eval_data,
                }, indent=2, ensure_ascii=False))

                print(f"  📝 Update proposal written: {patch_file.relative_to(ALL_SKILLS_DIR)}")
                skills_updated.append({
                    "skillName": related_name,
                    "patchFile": str(patch_file),
                    "toolCount": request["toolCount"],
                    "createdAt": datetime.now().isoformat(),
                    "action": "update",
                    "lastInboundMessageId": request.get("lastInboundMessageId"),
                })

                request["status"] = "update_proposed"
                request["relatedSkill"] = related_name
                req_file.write_text(json.dumps(request, indent=2))

            else:
                # New skill evaluation
                if DRY_RUN:
                    print("  [DRY RUN] Would evaluate for NEW skill")
                    request["status"] = "dry_run"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                if _prompt_module:
                    existing = get_existing_skills_summary()
                    prompt = _prompt_module.build_new_skill_prompt(request, existing)
                else:
                    prompt = build_new_skill_prompt(request)
                result = call_gemini(prompt)

                if not result:
                    request["status"] = "error"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                if result.strip().startswith("NO_SKILL"):
                    print("  ⏭️ No reusable pattern")
                    request["status"] = "no_skill"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                # Parse structured eval_json block
                eval_data = _extract_eval_json(result)
                skill_name = (eval_data.get("skill_name")
                              or _extract_name_from_result(result)
                              or f"auto-{req_file.stem[:12]}")

                # Extract skill_md block
                skill_content = _extract_skill_md(result)

                # Guard: skip if no valid SKILL.md content was extracted
                if not skill_content or not skill_content.startswith("#"):
                    print(f"  ⏭️ No valid SKILL.md block extracted (name would be: {skill_name}), skipping")
                    request["status"] = "no_skill_md"
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                # Write
                skill_dir = SKILLS_DIR / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(skill_content)
                (skill_dir / ".meta.json").write_text(json.dumps({
                    "createdAt": datetime.now().isoformat(),
                    "sourceRequest": req_file.name,
                    "toolCount": request["toolCount"],
                    "toolNames": request.get("toolNames", []),
                    "status": "pending_review",
                }, indent=2))
                # Extract quality_score if present
                quality = eval_data.get("quality_score", {})
                quality_total = quality.get("total", 0) if isinstance(quality, dict) else 0

                # Write .eval.json for richer notification card
                (skill_dir / ".eval.json").write_text(json.dumps({
                    "action": "create",
                    "generatedAt": datetime.now().isoformat(),
                    "sourceRequest": req_file.name,
                    "toolCount": request["toolCount"],
                    "toolNames": request.get("toolNames", []),
                    "lastInboundMessageId": request.get("lastInboundMessageId"),
                    **eval_data,
                }, indent=2, ensure_ascii=False))

                # Quality-gate: low-score skills stored silently
                if quality_total > 0 and quality_total < 40:
                    print(f"  ⚠️ Low quality ({quality_total}/100), stored silently: {skill_name}")
                    request["status"] = "low_quality"
                    request["skillName"] = skill_name
                    request["qualityScore"] = quality_total
                    req_file.write_text(json.dumps(request, indent=2))
                    continue

                quality_label = f" (quality: {quality_total}/100)" if quality_total > 0 else ""
                print(f"  \u2705 New skill draft: {skill_name}{quality_label}")
                skills_created.append({
                    "skillName": skill_name,
                    "toolCount": request["toolCount"],
                    "toolNames": request.get("toolNames", [])[:10],
                    "createdAt": datetime.now().isoformat(),
                    "action": "create",
                    "qualityScore": quality_total,
                    "lastInboundMessageId": request.get("lastInboundMessageId"),
                })

                request["status"] = "completed"
                request["skillName"] = skill_name
                req_file.write_text(json.dumps(request, indent=2))

        except Exception as e:
            print(f"  ⚠️ Error processing {req_file.name}: {e}")
        finally:
            if lock_fd:
                lock_fd.close()

    # Write pending review
    all_pending = skills_created + skills_updated
    if all_pending:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        existing = []
        try:
            existing = json.loads(PENDING_REVIEW.read_text())
        except Exception:
            pass
        existing.extend(all_pending)
        PENDING_REVIEW.write_text(json.dumps(existing, indent=2))

    # Cleanup old queue files (non-pending, older than 7 days)
    cleanup_old_queue_files()

    print(f"\n{'='*50}")
    print(f"新建 Skill: {len(skills_created)}")
    print(f"更新提案: {len(skills_updated)}")
    print(f"总待审批: {len(all_pending)}")


def cleanup_old_queue_files(max_age_days=7):
    """Delete non-pending queue files older than max_age_days."""
    if not QUEUE_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0
    for f in QUEUE_DIR.glob("*.json"):
        try:
            req = json.loads(f.read_text())
            if req.get("status") == "pending":
                continue
            created = req.get("createdAt", "")
            if created:
                created_dt = datetime.fromisoformat(created)
                if created_dt < cutoff:
                    f.unlink()
                    removed += 1
        except Exception:
            continue
    if removed:
        print(f"🧹 Cleaned up {removed} old queue file(s)")


if __name__ == "__main__":
    process_queue()
