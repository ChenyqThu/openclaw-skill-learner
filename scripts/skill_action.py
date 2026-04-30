#!/usr/bin/env python3
"""
skill_action.py — Handle skill candidate card callbacks (approve / skip / discuss / revert).

Usage:
  python3 skill_action.py approve  <skill_name> [--message-id MSG_ID]
  python3 skill_action.py skip     <skill_name> [--message-id MSG_ID]
  python3 skill_action.py discuss  <skill_name> [--message-id MSG_ID] [--note "..."]
  python3 skill_action.py revert   <skill_name> [--message-id MSG_ID]

Actions:
  approve  Move draft from auto-learned/ → skills/  and update card to ✅ 已落地
  skip     Delete draft dir               and delete card message
  discuss  Keep draft, update card with discussion note
  revert   Git revert the last evolution commit for a skill (Track 1)

Env: reads ~/.openclaw/.env for FEISHU_TARGET_OPEN_ID etc.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
WORKSPACE     = Path.home() / ".openclaw/workspace"
AUTO_LEARNED  = WORKSPACE / "skills/auto-learned"
SKILLS_DIR    = WORKSPACE / "skills"

FEISHU_TARGET_OPEN_ID = "ou_8d1ce0fa1d435070ed695baeabe25adc"
SKIP_LIST_FILE = Path.home() / ".openclaw/workspace/data/skill-learner/skipped-skills.json"
REJECTION_CONTEXT_FILE = Path.home() / ".openclaw/workspace/data/skill-learner/rejection-context.json"
REJECTION_CONTEXT_MAX = 50            # FIFO cap
REJECTION_CONTEXT_MAX_DAYS = 30       # entries older than this get pruned on write


def log(msg: str):
    print(f"[skill_action] {msg}", flush=True)


def _append_rejection(skill_name: str, action: str, reason: str, draft_dir: Path) -> None:
    """Append a rejection entry to rejection-context.json so Gemini learns what NOT to propose.

    Entry shape:
      {
        "skillName": ..., "action": "skip"|"discuss", "rejectedAt": ISO-8601,
        "reason": <user's free text or default>,
        "originalProblemContext": ..., "originalRecommendedApproach": ...,
        "sourceSessionRunId": ...,
        "promptNegativeExample": "1-line takeaway for next Gemini prompt"
      }

    Prunes entries older than 30 days and FIFO-caps at 50 on every write.
    """
    now = datetime.now(timezone.utc)
    eval_data: dict = {}
    eval_path = draft_dir / ".eval.json"
    if eval_path.exists():
        try:
            eval_data = json.loads(eval_path.read_text())
        except Exception as e:
            log(f"Warning: could not read {eval_path}: {e}")

    original_context = (eval_data.get("problem_context") or "").strip()[:120]
    original_approach = (eval_data.get("recommended_approach") or "").strip()[:160]
    source_request = eval_data.get("sourceRequest") or ""
    # Derive runId from sourceRequest filename (queue ids look like 1776358252319-ynbbie.json)
    source_run_id = source_request.rsplit(".", 1)[0] if source_request else ""

    user_reason = (reason or "").strip() or (
        "user clicked skip (no comment)" if action == "skip" else "user requested discussion"
    )

    negative_example_parts = [
        f"曾提议「{skill_name}」被 {action}（原因：{user_reason[:80]}）"
    ]
    if original_context:
        negative_example_parts.append(f"原问题：{original_context}")
    if action == "skip":
        negative_example_parts.append("避免再次提出此类抽象模式")
    elif action == "discuss":
        negative_example_parts.append("用户对此模式有保留，需改进后再提")
    negative_example = "；".join(negative_example_parts)

    entry = {
        "skillName": skill_name,
        "action": action,
        "rejectedAt": now.isoformat(),
        "reason": user_reason,
        "originalProblemContext": original_context,
        "originalRecommendedApproach": original_approach,
        "sourceSessionRunId": source_run_id,
        "promptNegativeExample": negative_example,
    }

    # Load + prune + append + cap
    existing: list = []
    if REJECTION_CONTEXT_FILE.exists():
        try:
            existing = json.loads(REJECTION_CONTEXT_FILE.read_text())
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []

    cutoff = now - timedelta(days=REJECTION_CONTEXT_MAX_DAYS)
    def _keep(e: dict) -> bool:
        ts = e.get("rejectedAt", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= cutoff
        except Exception:
            return False  # malformed entries drop on prune

    existing = [e for e in existing if _keep(e)]
    existing.append(entry)
    # FIFO cap: keep newest REJECTION_CONTEXT_MAX
    if len(existing) > REJECTION_CONTEXT_MAX:
        existing = existing[-REJECTION_CONTEXT_MAX:]

    REJECTION_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REJECTION_CONTEXT_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    log(f"rejection-context: +1 ({action}) {skill_name} → {len(existing)} entries")


def load_env():
    env_file = Path.home() / ".openclaw/.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                if line.startswith("export "):
                    line = line[7:]
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def openclaw_send(message: str = None, card: dict = None, target: str = None):
    """Send a Feishu message or card via openclaw CLI."""
    cmd = ["openclaw", "message", "send", "--channel", "feishu",
           "--target", f"user:{target or FEISHU_TARGET_OPEN_ID}"]
    if card:
        cmd += ["--card", json.dumps(card)]
    elif message:
        cmd += ["--message", message]
    else:
        return
    subprocess.run(cmd, check=False)


def openclaw_edit_card(message_id: str, card: dict):
    """Update an existing card message."""
    cmd = ["openclaw", "message", "edit",
           "--channel", "feishu",
           "--message-id", message_id,
           "--card", json.dumps(card)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"edit failed: {result.stderr.strip()}")
    return result.returncode == 0


def openclaw_delete(message_id: str):
    """Delete a bot message."""
    cmd = ["openclaw", "message", "delete",
           "--channel", "feishu",
           "--message-id", message_id]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"delete failed: {result.stderr.strip()}")
    return result.returncode == 0


def build_done_card(skill_name: str, action_label: str, note: str = ""):
    """Build a minimal 'completed' card to replace the interactive one."""
    body_elements = [
        {"tag": "markdown", "content": f"✅ **{action_label}**：`{skill_name}` 已处理完毕。"},
    ]
    if note:
        body_elements.append({"tag": "markdown", "content": f"💬 备注：{note}"})
    return {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "title": {"content": f"🧠 Skill · {action_label} · {skill_name}", "tag": "plain_text"},
            "template": "green" if action_label == "已落地" else "grey",
        },
        "body": {
            "direction": "vertical",
            "elements": body_elements,
        },
    }


def do_approve(skill_name: str, message_id: str | None):
    draft = AUTO_LEARNED / skill_name
    if not draft.exists():
        log(f"Draft not found: {draft}")
        openclaw_send(message=f"⚠️ Skill 草稿不存在：`{skill_name}`，可能已被处理过。")
        return 1

    dest = SKILLS_DIR / skill_name
    if dest.exists():
        log(f"Target already exists: {dest}, overwriting")
        shutil.rmtree(dest)

    shutil.move(str(draft), str(dest))
    log(f"Moved {draft} → {dest}")

    # Clean up eval artifacts
    for f in [".eval.json", ".update-proposal.md"]:
        p = dest / f
        if p.exists():
            p.unlink()

    # Track 4 (Curator): stamp source=auto_learned + bootstrap sidecar entry.
    # Approved-on date is treated as the canonical created_at (the draft date is
    # uninteresting to the curator — what matters is when the skill became active).
    try:
        from curator_telemetry import write_frontmatter_field, bootstrap_one
        skill_md = dest / "SKILL.md"
        today = datetime.now().strftime("%Y-%m-%d")
        if skill_md.exists():
            write_frontmatter_field(skill_md, "source", "auto_learned")
            write_frontmatter_field(skill_md, "created_at", today)
            write_frontmatter_field(skill_md, "pinned", False)
        bootstrap_one(skill_name, source="auto_learned",
                      created_at=today + "T00:00:00")
        log(f"curator: stamped {skill_name} (source=auto_learned, created_at={today})")
    except ImportError:
        pass  # curator not installed
    except Exception as e:
        log(f"Warning: curator stamping failed for {skill_name}: {e}")

    # Update card or send confirmation
    if message_id:
        done_card = build_done_card(skill_name, "已落地")
        if not openclaw_edit_card(message_id, done_card):
            openclaw_send(message=f"✅ Skill `{skill_name}` 已落地到正式目录。")
    else:
        openclaw_send(message=f"✅ Skill `{skill_name}` 已落地到正式目录。")

    log(f"approve done: {skill_name}")
    return 0


def do_skip(skill_name: str, message_id: str | None, reason: str = ""):
    draft = AUTO_LEARNED / skill_name

    # Phase A.3: capture rejection context BEFORE deleting the draft.
    # `_append_rejection` will read `.eval.json` if present.
    if draft.exists():
        _append_rejection(skill_name, "skip", reason, draft)
        shutil.rmtree(draft)
        log(f"Deleted draft: {draft}")
    else:
        # Still record a bare-bones rejection so future prompts know the name was refused.
        _append_rejection(skill_name, "skip", reason, draft)
        log(f"Draft not found (already deleted?): {draft}")

    # Write to skip blacklist so server won't re-suggest this skill name
    try:
        existing = json.loads(SKIP_LIST_FILE.read_text()) if SKIP_LIST_FILE.exists() else []
        if skill_name not in existing:
            existing.append(skill_name)
            SKIP_LIST_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
            log(f"Added to skip blacklist: {skill_name}")
    except Exception as e:
        log(f"Warning: could not write skip blacklist: {e}")

    # Delete card message
    if message_id:
        deleted = openclaw_delete(message_id)
        if not deleted:
            openclaw_send(message=f"⏭ Skill `{skill_name}` 已跳过并删除草稿。")
    else:
        openclaw_send(message=f"⏭ Skill `{skill_name}` 已跳过并删除草稿。")

    log(f"skip done: {skill_name}")
    return 0


def do_revert(skill_name: str, message_id: str | None):
    """Revert the last evolution commit for a skill via git revert."""
    import subprocess

    workspace = WORKSPACE
    # Find the last evolution commit for this skill
    result = subprocess.run(
        ["git", "log", "--oneline", "--all", "-20",
         "--grep", f"evolve({skill_name})"],
        cwd=str(workspace), capture_output=True, text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        log(f"No evolution commits found for: {skill_name}")
        openclaw_send(message=f"⚠️ 未找到「{skill_name}」的进化 commit，无法回滚。")
        return 1

    # Get the latest evolution commit hash
    latest_line = result.stdout.strip().split("\n")[0]
    commit_hash = latest_line.split()[0]
    log(f"Reverting commit {commit_hash} for skill: {skill_name}")

    # Git revert (safe: creates new revert commit)
    revert_result = subprocess.run(
        ["git", "revert", commit_hash, "--no-edit"],
        cwd=str(workspace), capture_output=True, text=True,
    )

    if revert_result.returncode != 0:
        log(f"Git revert failed: {revert_result.stderr.strip()}")
        openclaw_send(message=f"⚠️ 回滚失败：{revert_result.stderr.strip()[:200]}")
        return 1

    log(f"Successfully reverted: {commit_hash}")

    # Update card
    if message_id:
        done_card = build_done_card(skill_name, "已回滚", note=f"已撤销 commit {commit_hash}")
        if not openclaw_edit_card(message_id, done_card):
            openclaw_send(message=f"↩️ Skill「{skill_name}」进化已回滚（reverted {commit_hash}）。")
    else:
        openclaw_send(message=f"↩️ Skill「{skill_name}」进化已回滚（reverted {commit_hash}）。")

    log(f"revert done: {skill_name} ({commit_hash})")
    return 0


def do_discuss(skill_name: str, message_id: str | None, note: str):
    draft = AUTO_LEARNED / skill_name
    if not draft.exists():
        openclaw_send(message=f"⚠️ Skill 草稿不存在：`{skill_name}`")
        return 1

    # Phase A.3: persist the note as a rejection-context entry so Gemini learns from it.
    # Discuss keeps the draft (doesn't delete) — user wants to iterate, not reject outright.
    _append_rejection(skill_name, "discuss", note, draft)

    # Update card to discussion state
    if message_id:
        done_card = build_done_card(skill_name, "讨论中", note=note or "等待进一步讨论")
        openclaw_edit_card(message_id, done_card)

    log(f"discuss triggered: {skill_name}, note={note!r}")
    return 0


def do_profile_approve(proposal_id: str, message_id: str | None):
    """Apply a user modeling proposal to its target spec file (Track 2)."""
    sys.path.insert(0, str(Path(__file__).parent))
    from user_modeling import apply_proposal

    ok, msg = apply_proposal(proposal_id)
    if ok:
        log(f"profile_approve: {msg}")
        if message_id:
            done_card = build_done_card(proposal_id, "已采纳", note=msg)
            openclaw_edit_card(message_id, done_card)
        else:
            openclaw_send(message=f"✅ 画像更新已采纳：{msg}")
    else:
        log(f"profile_approve failed: {msg}")
        openclaw_send(message=f"⚠️ 画像更新失败：{msg}")
    return 0 if ok else 1


def do_profile_reject(proposal_id: str, message_id: str | None):
    """Reject a user modeling proposal (Track 2)."""
    sys.path.insert(0, str(Path(__file__).parent))
    from user_modeling import reject_proposal

    ok, msg = reject_proposal(proposal_id)
    if ok:
        log(f"profile_reject: {msg}")
        if message_id:
            done_card = build_done_card(proposal_id, "已忽略")
            openclaw_edit_card(message_id, done_card)
    else:
        log(f"profile_reject failed: {msg}")
    return 0 if ok else 1


# ─── Track 4 (Curator) actions ──────────────────────────────────────────────

def do_pin(skill_name: str, message_id: str | None):
    """Pin a skill: set frontmatter pinned=true so Darwin/Curator skip it."""
    try:
        from curator_lifecycle import pin
    except ImportError as e:
        log(f"pin failed: import error {e}")
        return 1
    try:
        r = pin(skill_name)
    except FileNotFoundError as e:
        log(f"pin failed: {e}")
        return 1
    log(f"pin: {r['name']} pinned (changed={r['changed']})")
    return 0


def do_unpin(skill_name: str, message_id: str | None):
    try:
        from curator_lifecycle import unpin
    except ImportError as e:
        log(f"unpin failed: import error {e}")
        return 1
    try:
        r = unpin(skill_name)
    except FileNotFoundError as e:
        log(f"unpin failed: {e}")
        return 1
    log(f"unpin: {r['name']} unpinned (changed={r['changed']})")
    return 0


def do_restore(skill_name: str, message_id: str | None):
    """Restore an archived skill: move from _archived/ back to active tree."""
    try:
        from curator_lifecycle import apply_restore
    except ImportError as e:
        log(f"restore failed: import error {e}")
        return 1
    try:
        r = apply_restore(skill_name)
    except (FileNotFoundError, KeyError, FileExistsError, ValueError) as e:
        log(f"restore failed: {e}")
        return 1
    log(f"restore: {r['name']} → {r['to_path']}  [{r.get('git_sha') or 'no-commit'}]")
    return 0


def _resolve_curator_run_dir(run_ts: str | None):
    """Locate a curator-reports/<ts> directory. Defaults to `latest` symlink."""
    base = Path.home() / ".openclaw/workspace/data/skill-learner/curator-reports"
    if run_ts:
        d = base / run_ts
        return d if d.exists() else None
    latest = base / "latest"
    if latest.exists():
        return latest.resolve()
    return None


def do_curator_approve(rec_id: str, message_id: str | None,
                       run_ts: str | None = None):
    """Apply a single LLM curator recommendation (consolidation or archive)."""
    try:
        from curator_actions import (apply_archive_rec, apply_consolidation,
                                      find_recommendation, mark_recommendation)
    except ImportError as e:
        log(f"curator_approve failed: import {e}")
        return 1
    run_dir = _resolve_curator_run_dir(run_ts)
    if run_dir is None:
        log(f"curator_approve: no run dir found (ts={run_ts})")
        return 1

    rec = find_recommendation(run_dir, rec_id)
    if rec is None:
        log(f"curator_approve: rec {rec_id!r} not found in {run_dir.name}")
        return 1

    kind = rec.get("kind")
    try:
        if kind == "consolidate":
            r = apply_consolidation(rec)
            log(f"curator_approve: consolidated {r['names']} → {r['new_name']}  "
                f"[{r.get('git_sha') or 'no-commit'}]")
        elif kind == "archive":
            r = apply_archive_rec(rec)
            log(f"curator_approve: archived {r['name']}  "
                f"[{r.get('git_sha') or 'no-commit'}]")
        else:
            log(f"curator_approve: unknown rec kind {kind!r}")
            return 1
    except Exception as e:
        log(f"curator_approve failed: {e}")
        return 1

    mark_recommendation(run_dir, rec_id, "approved")

    if message_id:
        try:
            done_card = build_done_card(rec_id, "已采纳")
            openclaw_edit_card(message_id, done_card)
        except Exception:
            pass
    return 0


def do_curator_reject(rec_id: str, message_id: str | None,
                      run_ts: str | None = None, note: str = ""):
    """Mark a curator recommendation rejected (no skill mutations)."""
    try:
        from curator_actions import find_recommendation, mark_recommendation
    except ImportError as e:
        log(f"curator_reject failed: import {e}")
        return 1
    run_dir = _resolve_curator_run_dir(run_ts)
    if run_dir is None:
        log(f"curator_reject: no run dir found (ts={run_ts})")
        return 1
    if find_recommendation(run_dir, rec_id) is None:
        log(f"curator_reject: rec {rec_id!r} not found")
        return 1
    mark_recommendation(run_dir, rec_id, "rejected", note=note or None)
    log(f"curator_reject: marked {rec_id} as rejected")
    if message_id:
        try:
            done_card = build_done_card(rec_id, "已忽略")
            openclaw_edit_card(message_id, done_card)
        except Exception:
            pass
    return 0


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Handle skill candidate card callbacks")
    parser.add_argument("action", choices=["approve", "skip", "discuss", "revert",
                                           "profile_approve", "profile_reject",
                                           "pin", "unpin", "restore",
                                           "curator_approve", "curator_reject"])
    parser.add_argument("skill_name", help="Skill name, proposal ID, or curator rec_id")
    parser.add_argument("--message-id", default=None, help="Feishu message_id of the card")
    parser.add_argument("--note", default="", help="Note for discuss action")
    parser.add_argument("--reason", default="", help="Rejection reason for skip action (goes to rejection-context.json)")
    parser.add_argument("--run-ts", default=None, help="Curator run timestamp (for curator_approve/reject)")
    args = parser.parse_args()

    if args.action == "approve":
        sys.exit(do_approve(args.skill_name, args.message_id))
    elif args.action == "skip":
        sys.exit(do_skip(args.skill_name, args.message_id, args.reason))
    elif args.action == "discuss":
        sys.exit(do_discuss(args.skill_name, args.message_id, args.note))
    elif args.action == "revert":
        sys.exit(do_revert(args.skill_name, args.message_id))
    elif args.action == "profile_approve":
        sys.exit(do_profile_approve(args.skill_name, args.message_id))
    elif args.action == "profile_reject":
        sys.exit(do_profile_reject(args.skill_name, args.message_id))
    elif args.action == "pin":
        sys.exit(do_pin(args.skill_name, args.message_id))
    elif args.action == "unpin":
        sys.exit(do_unpin(args.skill_name, args.message_id))
    elif args.action == "restore":
        sys.exit(do_restore(args.skill_name, args.message_id))
    elif args.action == "curator_approve":
        sys.exit(do_curator_approve(args.skill_name, args.message_id,
                                    run_ts=args.run_ts))
    elif args.action == "curator_reject":
        sys.exit(do_curator_reject(args.skill_name, args.message_id,
                                   run_ts=args.run_ts, note=args.note))


if __name__ == "__main__":
    main()
