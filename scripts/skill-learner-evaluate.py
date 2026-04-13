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
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta

QUEUE_DIR = Path.home() / ".openclaw/workspace/data/skill-learner/analysis-queue"
SKILLS_DIR = Path.home() / ".openclaw/workspace/skills/auto-learned"
ALL_SKILLS_DIR = Path.home() / ".openclaw/workspace/skills"
PENDING_REVIEW = SKILLS_DIR / ".pending-review.json"
GEMINI_MODEL = "gemini-3-flash-preview"  # upgraded from 3.1-flash-lite for better judgment accuracy

DRY_RUN = "--dry-run" in sys.argv


# ─── Result Parsers ──────────────────────────────────────────────────────────

def _extract_eval_json(result: str) -> dict:
    """Extract the ```eval_json block from Gemini output and parse it."""
    m = re.search(r'```eval_json\s*\n(.*?)\n```', result, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _extract_skill_md(result: str) -> str:
    """Extract the ```skill_md block, falling back to the full result."""
    m = re.search(r'```skill_md\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Legacy fallback: strip outer fences if present
    content = result.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
    if content.endswith("```"):
        content = content.rsplit("\n", 1)[0]
    return content


def _extract_name_from_result(result: str) -> str | None:
    """Fallback: extract name from YAML frontmatter 'name:' line."""
    for line in result.strip().split("\n"):
        if line.strip().startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def call_gemini(prompt: str) -> str | None:
    import urllib.request
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NANO_BANANA_API_KEY")
    if not api_key:
        print("ERROR: No GEMINI_API_KEY found")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
    except Exception as e:
        print(f"ERROR: Gemini API failed: {e}")
        return None


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

━━━ QUALIFICATION CRITERIA ━━━
Need ALL of (A) + (B), plus at least one of (C)–(E):

A. The pattern is reusable across ≥2 DIFFERENT future contexts
   ("different" = different problem domain, different file type, or different tool chain)
B. It's about Jarvis's tool usage or workflow orchestration — not "fix script X"
C. Required non-obvious trial and error or course correction to discover
D. Contains specific tool combos, parameters, or pitfalls worth documenting
E. The user corrected the agent's method — Jarvis would repeat the mistake without this

━━━ RED FLAGS → output NO_SKILL ━━━
• Improvement is "add error handling/retry to script X" → fix the script directly
• Pattern only applies to one specific file/cron/config
• The approach is obvious or already covered by an existing skill above

━━━ EXAMPLES ━━━

Example 1 — QUALIFIES (new skill):
Session: Agent tried 3 approaches to parse a complex PDF, first with plain text extraction (failed),
then with page-by-page OCR (too slow), finally discovered combining PyMuPDF structured extraction
with fallback OCR only for scanned pages. User said "that's much better, remember this approach."
→ This qualifies: trial-and-error (C), specific tool combo (D), reusable across PDF tasks (A).

Example 2 — NO_SKILL:
Session: Agent fixed a typo in config.yaml and restarted the service.
→ NO_SKILL: one-off fix, not a behavioral pattern, not reusable.

Example 3 — NO_SKILL:
Session: Agent added logging to a Python script to debug an error, found the bug, removed the logging.
→ NO_SKILL: standard debugging workflow, obvious approach, not worth documenting as a skill.

━━━ INSTRUCTIONS ━━━

Step 1 — REASONING (mandatory): Before deciding, analyze the session by answering:
  (1) Is the pattern reusable across ≥2 different contexts? Why or why not?
  (2) Is it about agent behavior/workflow, or just a code fix?
  (3) Which of criteria C, D, E apply? Cite specific evidence from the conversation.

Step 2 — DECISION:
  If NOT qualified: output your reasoning, then on a new line: NO_SKILL
  If qualified: output your reasoning, then BOTH blocks below in order:

```eval_json
{{
  "skill_name": "<concise Title Case name>",
  "problem_context": "<1-2 sentences: what recurring challenge this solves, why non-obvious>",
  "recommended_approach": "<2-4 sentences: the key insight, what makes it work, when to apply it>",
  "when_to_use": ["<scenario 1>", "<scenario 2>", "<scenario 3>"],
  "key_patterns": ["<specific tool combo or param 1>", "<pattern 2>"],
  "pitfalls": ["<pitfall 1>", "<pitfall 2>"]
}}
```

```skill_md
---
name: <name>
description: <one-line description>
version: 1.0.0
tags: [<tag1>, <tag2>]
---

# <name>

## When to Use
- Scenario 1
- Scenario 2
- Scenario 3

## When NOT to Use
- Anti-pattern 1 (when this skill does not apply)
- Anti-pattern 2

## Procedure
1. Step 1: what to do and why
2. Step 2: ...
3. Step 3: ...

## Example
**Situation**: Brief description of a concrete scenario
**Approach**: What Jarvis should do step-by-step
**Result**: Expected outcome

## Pitfalls
- Pitfall 1: what goes wrong if you miss this
- Pitfall 2: ...

## Verification
- How to confirm the approach worked (specific checks or success criteria)

## Related Skills
- List any related existing skills (or "None" if standalone)
```"""


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    formatted_user, formatted_asst = _format_messages(request)
    truncated_skill = skill_content[:3000]

    return f"""You are evaluating whether a session revealed new information to UPDATE an existing Skill.

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

  If update is warranted: output your reasoning, then BOTH blocks below:

```eval_json
{{
  "skill_name": "{skill_name}",
  "problem_context": "<what new gap was discovered in this session>",
  "recommended_approach": "<the better approach or fix, and why it's an improvement>",
  "when_to_use": ["<updated scenario if any>"],
  "new_pitfalls": ["<new pitfall 1>", "<new pitfall 2>"],
  "key_changes": ["<what changes and why>"]
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

            # Pre-filter removed: plugin threshold (8) is now the sole gatekeeper.
            # Sessions reaching here have already passed the plugin's TOOL_CALL_THRESHOLD.

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

                # Extract skill_md block (falls back to full result if no block)
                skill_content = _extract_skill_md(result)

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

                print(f"  \u2705 New skill draft: {skill_name}")
                skills_created.append({
                    "skillName": skill_name,
                    "toolCount": request["toolCount"],
                    "toolNames": request.get("toolNames", [])[:10],
                    "createdAt": datetime.now().isoformat(),
                    "action": "create",
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
