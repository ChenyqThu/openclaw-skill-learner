#!/usr/bin/env python3
"""
Skill Learner Evaluator — Process analysis queue, create or update skills.

Reads pending analysis requests, calls Gemini to evaluate sessions.
Supports both NEW skill creation and UPDATING existing skills.

Usage:
  python3 skill-learner-evaluate.py           # Process all pending
  python3 skill-learner-evaluate.py --dry-run # Preview without creating skills
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

QUEUE_DIR = Path.home() / ".openclaw/workspace/data/skill-learner/analysis-queue"
SKILLS_DIR = Path.home() / ".openclaw/workspace/skills/auto-learned"
ALL_SKILLS_DIR = Path.home() / ".openclaw/workspace/skills"
PENDING_REVIEW = SKILLS_DIR / ".pending-review.json"
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"

DRY_RUN = "--dry-run" in sys.argv


# ─── Result Parsers ──────────────────────────────────────────────────────────

def _extract_eval_json(result: str) -> dict:
    """Extract the ```eval_json block from Gemini output and parse it."""
    import re
    m = re.search(r'```eval_json\s*\n(.*?)\n```', result, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def _extract_skill_md(result: str) -> str:
    """Extract the ```skill_md block, falling back to the full result."""
    import re
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
                import re
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
        # Skip auto-learned skills (those go into new creation)
        if "/auto-learned/" in info["path"]:
            continue

        desc = info["description"].lower()
        tags = [t.lower() for t in info["tags"]]

        # Check overlap: skill tags/name mentioned in user messages or tool names
        name_words = set(name.replace("-", " ").split())
        overlap_score = 0
        for word in name_words:
            if word in user_text:
                overlap_score += 2
        for tag in tags:
            if tag in user_text or tag in tool_names:
                overlap_score += 1
        for tool in tool_names:
            if tool in desc or tool in name:
                overlap_score += 1

        if overlap_score >= 3:
            return name, info

    return None, None


# ─── Prompt Builders ─────────────────────────────────────────────────────────

def build_new_skill_prompt(request: dict) -> str:
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    user_msgs = "\n".join(f"{i+1}. {m}" for i, m in enumerate(request.get("userMessages", [])))
    asst_msgs = "\n".join(f"{i+1}. {t}" for i, t in enumerate(request.get("assistantTexts", [])))

    return f"""Analyze this AI agent session for reusable workflow patterns.

SESSION: {tool_info}

USER REQUESTS:
{user_msgs}

AGENT RESPONSES:
{asst_msgs}

WHAT IS AN OPENCLAW SKILL (read carefully before judging):
A Skill is a reusable *agent behavioral pattern* — it tells Jarvis HOW to approach a class of tasks.
A Skill is NOT: a code fix to a specific script, a one-time optimization, implementation details of
one cron job/file, or general programming advice. The key test: "If Jarvis encounters a DIFFERENT
but structurally similar challenge next month, would this Skill guide the approach?" If the pattern
only applies to ONE specific script or file, it should be fixed directly, not turned into a Skill.

CRITERIA (need ALL of 1+2, plus at least one of 3-4):
1. Reusable across at least 2-3 DIFFERENT future contexts (not just the one script in this session)?
2. About Jarvis's tool usage/workflow patterns (not just "this specific cron job needs better error handling")?
3. Contains non-obvious tool combos, parameter patterns, or pitfalls worth documenting for future reference?
4. Corrects a recurring agent mistake that Jarvis would likely repeat without this guidance?

Red flags — output NO_SKILL if any apply:
- The improvement is "add error handling / retry logic to script X" (fix the script, not a new Skill)
- The pattern is already obvious or well-covered by existing tools/docs
- The session was debugging one specific file or config

If criteria NOT met: output exactly: NO_SKILL

If criteria MET: output a JSON block followed by the SKILL.md content, exactly in this format:

```eval_json
{{
  "skill_name": "<concise kebab-case or Title Case name>",
  "problem_context": "<1-2 sentences: what problem or gap this session was solving, why it matters>",
  "recommended_approach": "<2-4 sentences: the key insight or approach discovered, what makes it work well>",
  "when_to_use": ["<scenario 1>", "<scenario 2>", "<scenario 3>"],
  "key_patterns": ["<specific tool combo or param pattern 1>", "<pattern 2>"],
  "pitfalls": ["<pitfall or gotcha 1>", "<pitfall 2>"]
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
<bullet list of scenarios>

## Procedure
<numbered steps>

## Pitfalls
<bullet list>

## Verification
<how to confirm it worked>
```"""


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    user_msgs = "\n".join(f"{i+1}. {m}" for i, m in enumerate(request.get("userMessages", [])))
    asst_msgs = "\n".join(f"{i+1}. {t}" for i, t in enumerate(request.get("assistantTexts", [])))

    # Truncate existing skill to save tokens
    truncated_skill = skill_content[:3000]

    return f"""An AI agent used the skill "{skill_name}" during this session. Analyze whether the session revealed NEW information that should update the skill.

EXISTING SKILL (truncated):
{truncated_skill}

SESSION: {tool_info}

USER REQUESTS:
{user_msgs}

AGENT RESPONSES:
{asst_msgs}

EVALUATION CRITERIA (need ≥1 to qualify for update):
1. Were there pitfalls/errors NOT covered by the existing skill?
2. Did the agent discover a better or faster approach?
3. Did the user correct the agent's method or flag a gap?

If NO updates needed: output exactly: NO_UPDATE

If update is warranted: output a JSON block followed by the patch YAML, exactly in this format:

```eval_json
{{
  "skill_name": "{skill_name}",
  "problem_context": "<1-2 sentences: what new gap or problem was discovered in this session>",
  "recommended_approach": "<2-3 sentences: the better approach or fix found, and why it's an improvement>",
  "when_to_use": ["<updated scenario 1>", "<updated scenario 2>"],
  "new_pitfalls": ["<new pitfall 1>", "<new pitfall 2>"],
  "key_changes": ["<what changes and why>"]
}}
```

```patch_yaml
action: update
skill: {skill_name}
sections_to_add:
  pitfalls:
    - "New pitfall: ..."
  procedure:
    - "Additional step: ..."
  notes:
    - "New finding: ..."
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
        request = json.loads(req_file.read_text())

        print(f"─── {req_file.name} ───")
        print(f"  Tools: {request['toolCount']} ({', '.join(request.get('toolNames', [])[:6])})")

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

    print(f"\n{'='*50}")
    print(f"新建 Skill: {len(skills_created)}")
    print(f"更新提案: {len(skills_updated)}")
    print(f"总待审批: {len(all_pending)}")


if __name__ == "__main__":
    process_queue()
