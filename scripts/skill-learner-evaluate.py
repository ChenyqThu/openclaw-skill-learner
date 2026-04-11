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

CRITERIA (need ≥2):
1. Non-trivial multi-step workflow?
2. Likely to recur?
3. Specific tool combos, parameter patterns, or pitfalls?

If YES: output a complete SKILL.md (YAML frontmatter: name, description, version, tags. Sections: When to Use, Procedure, Pitfalls, Verification).
If NO: output exactly: NO_SKILL"""


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"
    user_msgs = "\n".join(f"{i+1}. {m}" for i, m in enumerate(request.get("userMessages", [])))
    asst_msgs = "\n".join(f"{i+1}. {t}" for i, t in enumerate(request.get("assistantTexts", [])))

    # Truncate existing skill to save tokens
    truncated_skill = skill_content[:3000]

    return f"""An AI agent used the skill "{skill_name}" during this session. Analyze whether the session revealed NEW information that should be added to the skill.

EXISTING SKILL (truncated):
{truncated_skill}

SESSION: {tool_info}

USER REQUESTS:
{user_msgs}

AGENT RESPONSES:
{asst_msgs}

EVALUATION:
1. Were there pitfalls/errors NOT covered by the existing skill?
2. Did the agent discover a better approach than what the skill describes?
3. Did the user correct the agent's method?

If YES to any: output a PATCH in this format:
---
action: update
skill: {skill_name}
sections_to_add:
  pitfalls:
    - "New pitfall discovered: ..."
  procedure:
    - "Additional step: ..."
  notes:
    - "New finding: ..."
---

If NO updates needed: output exactly: NO_UPDATE"""


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

            # Write update proposal next to existing skill
            patch_file = Path(related_info["path"]).parent / ".update-proposal.md"
            patch_content = f"# Update Proposal for {related_name}\n"
            patch_content += f"Generated: {datetime.now().isoformat()}\n"
            patch_content += f"Source session: {req_file.name}\n\n"
            patch_content += result
            patch_file.write_text(patch_content)

            print(f"  📝 Update proposal written: {patch_file.relative_to(ALL_SKILLS_DIR)}")
            skills_updated.append({
                "skillName": related_name,
                "patchFile": str(patch_file),
                "toolCount": request["toolCount"],
                "createdAt": datetime.now().isoformat(),
                "action": "update",
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

            # Extract name
            name_match = None
            for line in result.strip().split("\n"):
                if line.strip().startswith("name:"):
                    name_match = line.split(":", 1)[1].strip()
                    break
            skill_name = name_match or f"auto-{req_file.stem[:12]}"

            # Clean fences
            skill_content = result.strip()
            if skill_content.startswith("```"):
                skill_content = skill_content.split("\n", 1)[1] if "\n" in skill_content else skill_content
            if skill_content.endswith("```"):
                skill_content = skill_content.rsplit("\n", 1)[0]

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

            print(f"  ✅ New skill draft: {skill_name}")
            skills_created.append({
                "skillName": skill_name,
                "toolCount": request["toolCount"],
                "toolNames": request.get("toolNames", [])[:10],
                "createdAt": datetime.now().isoformat(),
                "action": "create",
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
