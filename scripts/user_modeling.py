#!/usr/bin/env python3
"""
user_modeling.py — Track 2: User Modeling Analyzer (Gap 7)

Scans diary entries and conversation correction signals to identify
updates needed for USER.md / SOUL.md / AGENTS.md.

Key constraint: NEVER auto-commits. Generates proposals for human review.

Usage:
  python3 user_modeling.py --analyze                # Full analysis
  python3 user_modeling.py --analyze --dry-run       # Preview only
  python3 user_modeling.py --analyze --days 14       # Custom lookback
  python3 user_modeling.py --status                  # Show pending proposals
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

from gemini_client import load_env, call_gemini

load_env()

# ─── Paths ───────────────────────────────────────────────────────────────────
WORKSPACE = Path.home() / ".openclaw/workspace"
DATA_DIR = WORKSPACE / "data/skill-learner"
MEMORY_DIR = WORKSPACE / "memory"
FRICTION_LOG = DATA_DIR / "friction-signals.json"
PENDING_UPDATES = DATA_DIR / "pending-user-updates.json"
MODELING_LOG = DATA_DIR / "user-modeling.log"

# Core spec files that Track 2 can propose changes to
SPEC_FILES = {
    "USER.md": WORKSPACE / "USER.md",
    "SOUL.md": WORKSPACE / "SOUL.md",
    "AGENTS.md": WORKSPACE / "AGENTS.md",
}

DEFAULT_DAYS_BACK = 7


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class UpdateProposal:
    id: str                     # unique proposal id
    target_file: str            # USER.md / SOUL.md / AGENTS.md
    section: str                # target section heading
    action: str                 # "append" or "modify"
    current_text: str           # existing text (for modify)
    proposed_text: str          # suggested replacement/addition
    reason: str                 # why this change (cites source)
    confidence: str             # "high" / "medium" / "low"
    source_refs: list = field(default_factory=list)
    status: str = "pending"     # pending / applied / rejected
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.id:
            self.id = f"up-{int(datetime.now().timestamp())}-{hash(self.proposed_text) % 10000:04d}"


# ─── Signal Loaders ─────────────────────────────────────────────────────────

def scan_diaries(days_back: int = DEFAULT_DAYS_BACK) -> list[dict]:
    """Scan recent diary entries from memory/YYYY-MM-DD.md files."""
    entries = []
    if not MEMORY_DIR.exists():
        return entries

    cutoff = datetime.now() - timedelta(days=days_back)

    for f in sorted(MEMORY_DIR.glob("*.md"), reverse=True):
        # Parse date from filename
        try:
            date_str = f.stem  # e.g., "2026-04-12"
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                break
        except ValueError:
            continue

        try:
            content = f.read_text()
            # Extract meaningful entries (skip empty or very short)
            if len(content.strip()) < 50:
                continue

            # Extract key sections/entries
            entries.append({
                "date": date_str,
                "content": content[:3000],  # cap per diary
                "file": f.name,
            })
        except Exception:
            continue

    return entries


def scan_correction_signals(days_back: int = DEFAULT_DAYS_BACK) -> list[dict]:
    """Extract user correction signals from friction log."""
    if not FRICTION_LOG.exists():
        return []

    try:
        signals = json.loads(FRICTION_LOG.read_text())
    except Exception:
        return []

    cutoff = datetime.now() - timedelta(days=days_back)
    corrections = []

    for entry in signals:
        # Filter to correction-type signals only
        ts = entry.get("timestamp", "")
        try:
            entry_date = datetime.fromisoformat(ts)
            if entry_date < cutoff:
                continue
        except Exception:
            continue

        for sig in entry.get("frictionSignals", []):
            if sig.get("type") in ("user_correction", "explicit_feedback"):
                corrections.append({
                    "timestamp": ts,
                    "type": sig["type"],
                    "evidence": sig.get("evidence", ""),
                    "skill": entry.get("skillName"),
                    "runId": entry.get("runId"),
                })

    return corrections


# ─── Gemini Attribution ─────────────────────────────────────────────────────

def build_attribution_prompt(diary_entries: list[dict], corrections: list[dict],
                              specs: dict[str, str]) -> str:
    """Build the Gemini prompt for user modeling attribution."""

    # Format diary entries
    diary_text = ""
    for entry in diary_entries[:10]:  # cap at 10 entries
        diary_text += f"\n### {entry['date']}\n{entry['content'][:1500]}\n"

    if not diary_text.strip():
        diary_text = "（近期无日记条目）"

    # Format correction signals
    correction_text = ""
    for corr in corrections[:20]:
        correction_text += f"- [{corr['timestamp'][:10]}] {corr['type']}: {corr['evidence'][:200]}\n"

    if not correction_text.strip():
        correction_text = "（近期无纠正信号）"

    # Truncate specs
    user_md = specs.get("USER.md", "")[:4000]
    soul_md = specs.get("SOUL.md", "")[:4000]
    agents_md = specs.get("AGENTS.md", "")[:4000]

    return f"""你是 Jarvis（OpenClaw AI 智能体）的用户建模分析器。
你的任务是分析最近的信号源，识别 USER.md / SOUL.md / AGENTS.md 中需要更新的内容。

━━━ 信号源 1：近期日记 ━━━
{diary_text}

━━━ 信号源 2：对话中的用户纠正 ━━━
{correction_text}

━━━ 当前规范文件 ━━━

### USER.md（用户事实和偏好）
{user_md}

### SOUL.md（人格和沟通风格）
{soul_md}

### AGENTS.md（行为规则和操作约束）
{agents_md}

━━━ 归因规则 ━━━

1. **稳定性判断**：只提议已经稳定沉淀的认知变化。标准：
   - 明确表态的偏好（"我不喜欢..."、"以后都这样做"）
   - 至少出现 2 次的行为模式
   - 一次性的情绪或临时决定不算

2. **文件归因**：
   - USER.md → 用户的事实信息、偏好、习惯（"Lucien 喜欢..."）
   - SOUL.md → Jarvis 的人格特征、沟通风格（"用什么语气"）
   - AGENTS.md → 行为规则、操作约束（"不准做..."、"必须先..."）

3. **保守原则**：
   - 不确定的不提议（宁可遗漏，不可误改）
   - 已有内容覆盖的不重复提议
   - 一次性的修复/调整不提议
   - 对 SOUL.md 的改动要特别谨慎（相当于"性格手术"）

4. **格式要求**：
   - action="append"：在指定 section 末尾追加新条目
   - action="modify"：替换指定段落（提供原文和新文本）
   - reason 必须引用具体的日记日期或纠正信号

━━━ 输出 ━━━

输出 JSON 数组（如果没有需要更新的内容，输出 `[]`）：

```json
[
  {{
    "target_file": "USER.md",
    "section": "## 偏好",
    "action": "append",
    "current_text": "",
    "proposed_text": "建议新增的内容",
    "reason": "变更理由（引用信号源）",
    "confidence": "high",
    "source_refs": ["2026-04-12 日记", "对话纠正: xxx"]
  }}
]
```

重要：所有文本字段使用简体中文。"""


def parse_proposals(result: str) -> list[UpdateProposal]:
    """Parse Gemini output into UpdateProposal list."""
    if not result:
        return []

    # Extract JSON array
    m = re.search(r'```json\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        json_text = m.group(1)
    else:
        # Try to find bare JSON array
        m = re.search(r'\[.*\]', result, re.DOTALL)
        if m:
            json_text = m.group(0)
        else:
            return []

    try:
        data = json.loads(json_text)
        if not isinstance(data, list):
            return []
    except Exception:
        return []

    proposals = []
    for item in data:
        try:
            p = UpdateProposal(
                id="",  # auto-generated in __post_init__
                target_file=item.get("target_file", ""),
                section=item.get("section", ""),
                action=item.get("action", "append"),
                current_text=item.get("current_text", ""),
                proposed_text=item.get("proposed_text", ""),
                reason=item.get("reason", ""),
                confidence=item.get("confidence", "low"),
                source_refs=item.get("source_refs", []),
            )
            # Validate target file
            if p.target_file not in SPEC_FILES:
                continue
            # Skip low confidence
            if p.confidence == "low":
                continue
            if not p.proposed_text.strip():
                continue
            proposals.append(p)
        except Exception:
            continue

    return proposals


# ─── Core Analyzer ───────────────────────────────────────────────────────────

class UserModelAnalyzer:
    """Analyzes diary + corrections to generate spec file update proposals."""

    def __init__(self, days_back: int = DEFAULT_DAYS_BACK, dry_run: bool = False):
        self.days_back = days_back
        self.dry_run = dry_run

    def analyze(self) -> list[UpdateProposal]:
        """Run full analysis pipeline."""
        print(f"\n{'='*60}")
        print(f"  User Modeling Analyzer — Track 2")
        print(f"  Lookback: {self.days_back} days | Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        print(f"{'='*60}\n")

        # 1. Load signals
        print("── Loading signals ──")
        diary_entries = scan_diaries(self.days_back)
        corrections = scan_correction_signals(self.days_back)
        print(f"  Diary entries: {len(diary_entries)}")
        print(f"  Correction signals: {len(corrections)}")

        if not diary_entries and not corrections:
            print("  No signals found. Nothing to analyze.")
            return []

        # 2. Load current specs
        print("\n── Loading current spec files ──")
        specs = {}
        for name, path in SPEC_FILES.items():
            if path.exists():
                specs[name] = path.read_text()
                print(f"  {name}: {len(specs[name])} chars")
            else:
                specs[name] = ""
                print(f"  {name}: (not found)")

        # 3. Gemini attribution
        print("\n── Running Gemini attribution ──")
        prompt = build_attribution_prompt(diary_entries, corrections, specs)

        if self.dry_run:
            print("  [DRY RUN] Would call Gemini with attribution prompt")
            print(f"  Prompt length: {len(prompt)} chars")
            return []

        result = call_gemini(prompt, temperature=0.2, max_tokens=8192)
        if not result:
            print("  ERROR: Gemini call failed")
            return []

        # 4. Parse proposals
        proposals = parse_proposals(result)
        print(f"\n── Proposals: {len(proposals)} ──")

        for p in proposals:
            print(f"  [{p.confidence}] {p.target_file} § {p.section}")
            print(f"    Action: {p.action}")
            print(f"    Proposed: {p.proposed_text[:100]}...")
            print(f"    Reason: {p.reason[:100]}")
            print()

        # 5. Save proposals
        if proposals:
            self._save_proposals(proposals)

        return proposals

    def _save_proposals(self, new_proposals: list[UpdateProposal]):
        """Append new proposals to pending-user-updates.json."""
        existing = []
        if PENDING_UPDATES.exists():
            try:
                existing = json.loads(PENDING_UPDATES.read_text())
            except Exception:
                existing = []

        # Only add truly new proposals (avoid duplicates by proposed_text hash)
        existing_texts = {p.get("proposed_text", "") for p in existing}
        added = 0
        for p in new_proposals:
            if p.proposed_text not in existing_texts:
                existing.append(asdict(p))
                added += 1

        PENDING_UPDATES.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        print(f"  Saved {added} new proposals → {PENDING_UPDATES.name}")
        print(f"  Total pending: {len(existing)}")


# ─── Proposal Application ───────────────────────────────────────────────────

def apply_proposal(proposal_id: str) -> tuple[bool, str]:
    """Apply a single proposal to its target file. Returns (success, message)."""
    if not PENDING_UPDATES.exists():
        return False, "No pending updates file found"

    proposals = json.loads(PENDING_UPDATES.read_text())
    target = None
    for p in proposals:
        if p.get("id") == proposal_id:
            target = p
            break

    if not target:
        return False, f"Proposal not found: {proposal_id}"

    if target.get("status") == "applied":
        return False, f"Proposal already applied: {proposal_id}"

    target_file = SPEC_FILES.get(target["target_file"])
    if not target_file:
        return False, f"Unknown target file: {target['target_file']}"

    if not target_file.exists():
        return False, f"Target file not found: {target_file}"

    content = target_file.read_text()

    if target["action"] == "append":
        # Find the section and append after it
        section = target["section"]
        if section in content:
            # Find the end of the section (next ## or end of file)
            section_idx = content.index(section) + len(section)
            # Find next section heading
            next_section = re.search(r'\n## ', content[section_idx:])
            if next_section:
                insert_pos = section_idx + next_section.start()
            else:
                insert_pos = len(content)
            # Insert the proposed text
            new_content = content[:insert_pos].rstrip() + "\n" + target["proposed_text"] + "\n" + content[insert_pos:]
        else:
            # Section not found, append at end
            new_content = content.rstrip() + f"\n\n{target['section']}\n{target['proposed_text']}\n"
    elif target["action"] == "modify":
        if target["current_text"] and target["current_text"] in content:
            new_content = content.replace(target["current_text"], target["proposed_text"], 1)
        else:
            return False, f"Current text not found in {target['target_file']}, cannot modify"
    else:
        return False, f"Unknown action: {target['action']}"

    # Write back (NO git commit — human decides)
    target_file.write_text(new_content)

    # Mark proposal as applied
    target["status"] = "applied"
    target["applied_at"] = datetime.now().isoformat()
    PENDING_UPDATES.write_text(json.dumps(proposals, indent=2, ensure_ascii=False))

    return True, f"Applied to {target['target_file']} § {target['section']}"


def reject_proposal(proposal_id: str) -> tuple[bool, str]:
    """Mark a proposal as rejected."""
    if not PENDING_UPDATES.exists():
        return False, "No pending updates file found"

    proposals = json.loads(PENDING_UPDATES.read_text())
    for p in proposals:
        if p.get("id") == proposal_id:
            p["status"] = "rejected"
            p["rejected_at"] = datetime.now().isoformat()
            PENDING_UPDATES.write_text(json.dumps(proposals, indent=2, ensure_ascii=False))
            return True, f"Rejected: {proposal_id}"

    return False, f"Proposal not found: {proposal_id}"


def get_pending_proposals() -> list[dict]:
    """Get all pending proposals."""
    if not PENDING_UPDATES.exists():
        return []
    try:
        proposals = json.loads(PENDING_UPDATES.read_text())
        return [p for p in proposals if p.get("status") == "pending"]
    except Exception:
        return []


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Track 2: User Modeling Analyzer")
    parser.add_argument("--analyze", action="store_true", help="Run full analysis")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK, help=f"Lookback days (default {DEFAULT_DAYS_BACK})")
    parser.add_argument("--dry-run", action="store_true", help="Preview without Gemini calls")
    parser.add_argument("--status", action="store_true", help="Show pending proposals")
    parser.add_argument("--apply", help="Apply a specific proposal by ID")
    parser.add_argument("--reject", help="Reject a specific proposal by ID")
    args = parser.parse_args()

    if args.status:
        pending = get_pending_proposals()
        print(f"Pending proposals: {len(pending)}")
        for p in pending:
            print(f"  [{p.get('confidence')}] {p.get('id')}")
            print(f"    {p.get('target_file')} § {p.get('section')}")
            print(f"    {p.get('proposed_text', '')[:80]}...")
            print(f"    Reason: {p.get('reason', '')[:80]}")
            print()
        return

    if args.apply:
        ok, msg = apply_proposal(args.apply)
        print(f"{'✅' if ok else '❌'} {msg}")
        sys.exit(0 if ok else 1)

    if args.reject:
        ok, msg = reject_proposal(args.reject)
        print(f"{'✅' if ok else '❌'} {msg}")
        sys.exit(0 if ok else 1)

    if args.analyze:
        analyzer = UserModelAnalyzer(days_back=args.days, dry_run=args.dry_run)
        proposals = analyzer.analyze()

        print(f"\n{'='*60}")
        print(f"  ANALYSIS COMPLETE")
        print(f"{'='*60}")
        print(f"  Proposals generated: {len(proposals)}")
        for p in proposals:
            print(f"    [{p.confidence}] {p.target_file} § {p.section}: {p.action}")
        if proposals and not args.dry_run:
            print(f"\n  Pending updates saved to: {PENDING_UPDATES.name}")
            print(f"  Next: review via Feishu card or `python3 user_modeling.py --status`")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
