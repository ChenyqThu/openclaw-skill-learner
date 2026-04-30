#!/usr/bin/env python3
"""
Track 4 — Skill Curator CLI.

Phase 1 subcommands:
    --bootstrap      Initialize skill-usage.json + migrate SKILL.md frontmatter
    --status         Print sortable usage table + LRU candidates

(Phase 2/3 subcommands will be added below: --tick, --pin, --unpin, --restore,
--llm-review, --llm-review-if-due. Until then those flags raise NotImplementedError.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import ALL_SKILLS_DIR, SKILL_USAGE_FILE
from curator_telemetry import (
    bootstrap_from_git,
    days_since,
    list_skills_with_state,
    read_skill_meta,
    read_usage,
)
from curator_migrate_frontmatter import migrate_all


# ─── Bootstrap ──────────────────────────────────────────────────────────────

def cmd_bootstrap(args: argparse.Namespace) -> int:
    print("[curator] Step 1/2: migrating SKILL.md frontmatter...")
    fm_reports = migrate_all(dry_run=args.dry_run)
    fm_updated = sum(1 for r in fm_reports if r["action"] in ("updated", "created"))
    fm_errors  = sum(1 for r in fm_reports if r["action"] == "error")
    print(f"  → {fm_updated} files migrated, {fm_errors} errors")
    if fm_errors:
        for r in fm_reports:
            if r["action"] == "error":
                print(f"    ✗ {r['name']}: {r['reason']}")

    print("[curator] Step 2/2: seeding skill-usage.json from git history...")
    if args.dry_run:
        print("  → DRY-RUN: would seed sidecar entries (skipped)")
    else:
        usage_reports = bootstrap_from_git()
        new = sum(1 for r in usage_reports if r["seeded"])
        print(f"  → {len(usage_reports)} skills tracked, {new} new entries seeded")
        print(f"  Sidecar: {SKILL_USAGE_FILE}")

    return 0 if fm_errors == 0 else 1


# ─── Status ─────────────────────────────────────────────────────────────────

def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_status(args: argparse.Namespace) -> int:
    data = read_usage()
    items = list_skills_with_state()
    if not items:
        print("No skills tracked yet. Run: python3 scripts/curator.py --bootstrap")
        return 0

    # Header
    fmt = "  {name:<36s}  {read:>4s}  {applied:>7s}  {patched:>7s}  {last_app:<19s}  {state:<8s}  {pinned:<6s}  {source:<13s}  {age:>3s}d"
    print(fmt.format(name="SKILL", read="READ", applied="APPLIED",
                     patched="PATCHED", last_app="LAST_APPLIED",
                     state="STATE", pinned="PINNED", source="SOURCE", age="AGE"))
    print("  " + "─" * 120)

    for name, entry in items:
        fm = read_skill_meta(_locate_skill_dir(name))
        last_app = entry.get("last_applied_at") or "never"
        if last_app != "never":
            last_app = last_app[:19].replace("T", " ")
        age = days_since(fm.get("created_at"))
        age_str = str(age) if age is not None else "?"
        print(fmt.format(
            name=_truncate(name, 36),
            read=str(entry.get("read_count", 0)),
            applied=str(entry.get("applied_count", 0)),
            patched=str(entry.get("patch_count", 0)),
            last_app=last_app,
            state=entry.get("state", "active"),
            pinned="yes" if fm.get("pinned") else "no",
            source=fm.get("source", "?")[:13],
            age=age_str,
        ))

    # LRU candidates: skills with no applies, sorted by oldest created
    print()
    print("LRU 候选 (Phase 2 stale 风险):")
    never_applied = [
        (n, e, read_skill_meta(_locate_skill_dir(n)))
        for n, e in items
        if e.get("applied_count", 0) == 0
    ]
    # Sort by created_at ascending (oldest first)
    never_applied.sort(
        key=lambda t: t[2].get("created_at") or "9999"
    )
    for n, e, fm in never_applied[:5]:
        age = days_since(fm.get("created_at"))
        age_str = f"{age}d" if age is not None else "?"
        src = fm.get("source", "?")
        print(f"  · {n:<40s} (age={age_str:<5s} reads={e.get('read_count', 0)}  source={src})")

    print()
    meta = data.get("_meta", {})
    print(f"Last tick:  {meta.get('last_curator_tick_at') or 'never'}")
    print(f"Last LLM:   {meta.get('last_llm_review_at') or 'never'}")
    print(f"Sidecar:    {SKILL_USAGE_FILE}")
    return 0


def _locate_skill_dir(name: str) -> Path:
    """Find a skill directory by name (top-level or auto-learned)."""
    p = ALL_SKILLS_DIR / name
    if p.exists():
        return p
    p2 = ALL_SKILLS_DIR / "auto-learned" / name
    if p2.exists():
        return p2
    p3 = ALL_SKILLS_DIR / "_archived" / name
    if p3.exists():
        return p3
    # Fallback: best guess (won't have SKILL.md)
    return ALL_SKILLS_DIR / name


# ─── Phase 2: lifecycle commands ────────────────────────────────────────────

def cmd_tick(args: argparse.Namespace) -> int:
    from curator_lifecycle import run_tick
    result = run_tick(dry_run=args.dry_run, commit=not args.no_commit)
    transitions = result["transitions"]
    if not transitions:
        print("[curator] tick: no transitions" + (" (dry-run)" if args.dry_run else ""))
    else:
        print(f"[curator] tick: {len(transitions)} transition(s)"
              + (" (dry-run, no changes written)" if args.dry_run else ""))
        for t in transitions:
            sign = "·" if t["skipped_pinned"] else "→"
            print(f"  {sign} {t['name']:<40s} {t['from']:<8s} {sign} {t['to']:<8s}  {t['reason']}")
    if result["archived"]:
        print(f"\nArchived ({len(result['archived'])}):")
        for a in result["archived"]:
            sha = a.get("git_sha") or "(no commit)"
            print(f"  ✓ {a['name']:<40s} → {a['to_path']}  [{sha}]")
    if result["errors"]:
        print(f"\nErrors ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"  ✗ {e['name']}: {e['reason']}")
        return 1
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    from curator_lifecycle import pin
    try:
        r = pin(args.pin)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if r["changed"]:
        print(f"✓ Pinned {r['name']} (frontmatter updated)")
    else:
        print(f"· {r['name']} already pinned")
    return 0


def cmd_unpin(args: argparse.Namespace) -> int:
    from curator_lifecycle import unpin
    try:
        r = unpin(args.unpin)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if r["changed"]:
        print(f"✓ Unpinned {r['name']}")
    else:
        print(f"· {r['name']} already unpinned")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from curator_lifecycle import apply_restore
    try:
        r = apply_restore(args.restore, commit=not args.no_commit)
    except (FileNotFoundError, KeyError, FileExistsError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    sha = r.get("git_sha") or "(no commit)"
    print(f"✓ Restored {r['name']} → {r['to_path']}  [{sha}]")
    return 0


# ─── Phase 3: LLM consolidation review ──────────────────────────────────────

def cmd_llm_review(args: argparse.Namespace) -> int:
    from curator_llm import run_review, is_review_due, collect_active_skills

    if args.llm_review_if_due:
        skills = collect_active_skills()
        due, reason = is_review_due(skills)
        if not due:
            print(f"[curator] LLM review not due: {reason}")
            return 0
        print(f"[curator] LLM review due ({reason})")

    out = run_review(dry_run=args.dry_run,
                     send_feishu=not args.no_feishu)
    if out["skipped"]:
        print(f"[curator] skipped: {out['reason']}")
        return 0

    print(f"[curator] LLM review complete  →  {out['run_dir']}")
    print(f"  input skills:    {out['input_count']}")
    res = out.get("result") or {}
    print(f"  consolidations:  {len(res.get('consolidations', []) or [])}")
    print(f"  archives:        {len(res.get('archives', []) or [])}")
    print(f"  keep:            {len(res.get('keep', []) or [])}")
    if out.get("warnings"):
        print(f"  warnings:")
        for w in out["warnings"]:
            print(f"    · {w}")
    if "feishu_sent" in out:
        if out["feishu_sent"]:
            print(f"  feishu:          ✓ sent")
        else:
            print(f"  feishu:          ✗ {out.get('feishu_error', 'failed')}")
    return 0


# ─── Argument parsing ───────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Skill Curator (Track 4): per-skill telemetry, lifecycle, "
                    "and LLM consolidation review.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bootstrap", action="store_true",
                   help="Initialize skill-usage.json + migrate SKILL.md frontmatter")
    g.add_argument("--status", action="store_true",
                   help="Print usage table + LRU candidates")
    g.add_argument("--tick", action="store_true",
                   help="Run lifecycle state machine (Phase 2)")
    g.add_argument("--pin", metavar="NAME",
                   help="Pin a skill (Phase 2)")
    g.add_argument("--unpin", metavar="NAME",
                   help="Unpin a skill (Phase 2)")
    g.add_argument("--restore", metavar="NAME",
                   help="Restore an archived skill (Phase 2)")
    g.add_argument("--llm-review", action="store_true",
                   help="Run Gemini consolidation review (Phase 3)")
    g.add_argument("--llm-review-if-due", action="store_true",
                   help="Run --llm-review only if 14d cadence due (Phase 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't modify any files; print what would change")
    ap.add_argument("--no-commit", action="store_true",
                    help="For --tick / --restore: apply changes but skip workspace git commit")
    ap.add_argument("--no-feishu", action="store_true",
                    help="For --llm-review: skip the Feishu card send (still writes report)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.bootstrap:
        return cmd_bootstrap(args)
    if args.status:
        return cmd_status(args)
    if args.tick:
        return cmd_tick(args)
    if args.pin:
        return cmd_pin(args)
    if args.unpin:
        return cmd_unpin(args)
    if args.restore:
        return cmd_restore(args)
    if args.llm_review or args.llm_review_if_due:
        return cmd_llm_review(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
