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
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
WORKSPACE     = Path.home() / ".openclaw/workspace"
AUTO_LEARNED  = WORKSPACE / "skills/auto-learned"
SKILLS_DIR    = WORKSPACE / "skills"

FEISHU_TARGET_OPEN_ID = "ou_8d1ce0fa1d435070ed695baeabe25adc"
SKIP_LIST_FILE = Path.home() / ".openclaw/workspace/data/skill-learner/skipped-skills.json"


def log(msg: str):
    print(f"[skill_action] {msg}", flush=True)


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

    # Update card or send confirmation
    if message_id:
        done_card = build_done_card(skill_name, "已落地")
        if not openclaw_edit_card(message_id, done_card):
            openclaw_send(message=f"✅ Skill `{skill_name}` 已落地到正式目录。")
    else:
        openclaw_send(message=f"✅ Skill `{skill_name}` 已落地到正式目录。")

    log(f"approve done: {skill_name}")
    return 0


def do_skip(skill_name: str, message_id: str | None):
    draft = AUTO_LEARNED / skill_name
    if draft.exists():
        shutil.rmtree(draft)
        log(f"Deleted draft: {draft}")
    else:
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


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Handle skill candidate card callbacks")
    parser.add_argument("action", choices=["approve", "skip", "discuss", "revert",
                                           "profile_approve", "profile_reject"])
    parser.add_argument("skill_name", help="Skill name or proposal ID")
    parser.add_argument("--message-id", default=None, help="Feishu message_id of the card")
    parser.add_argument("--note", default="", help="Note for discuss action")
    args = parser.parse_args()

    if args.action == "approve":
        sys.exit(do_approve(args.skill_name, args.message_id))
    elif args.action == "skip":
        sys.exit(do_skip(args.skill_name, args.message_id))
    elif args.action == "discuss":
        sys.exit(do_discuss(args.skill_name, args.message_id, args.note))
    elif args.action == "revert":
        sys.exit(do_revert(args.skill_name, args.message_id))
    elif args.action == "profile_approve":
        sys.exit(do_profile_approve(args.skill_name, args.message_id))
    elif args.action == "profile_reject":
        sys.exit(do_profile_reject(args.skill_name, args.message_id))


if __name__ == "__main__":
    main()
