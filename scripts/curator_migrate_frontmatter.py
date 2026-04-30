"""
One-time migration: add Curator fields (pinned, source, created_at) to every
SKILL.md's YAML frontmatter.

Idempotent: re-running on a SKILL.md that already has these fields is a no-op.

Source detection:
- Skills under `skills/auto-learned/X/SKILL.md` → source: auto_learned
- Everything else (`skills/X/SKILL.md`)         → source: user_created
- `_archived/` is skipped (those skills aren't on the active list anyway)

The CLI entry is `python3 scripts/curator_migrate_frontmatter.py` or
`scripts/curator.py --bootstrap` (which calls this).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from config import ALL_SKILLS_DIR
from curator_telemetry import discover_created_at

CURATOR_FIELDS = ("pinned", "source", "created_at")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


def _has_field(frontmatter_body: str, field: str) -> bool:
    """True if `field:` appears as a top-level key in YAML frontmatter."""
    for line in frontmatter_body.splitlines():
        # Match `field:` or `field :` at line start (no indentation).
        if re.match(rf"^{re.escape(field)}\s*:", line):
            return True
    return False


def _detect_source(skill_md: Path) -> str:
    """auto_learned if under skills/auto-learned/, else user_created."""
    parts = skill_md.parts
    if "auto-learned" in parts:
        return "auto_learned"
    return "user_created"


def _format_created_at(ts: str) -> str:
    """Date-only YAML scalar (e.g. 2026-03-15). Sidecar keeps full ISO."""
    return ts.split("T", 1)[0] if "T" in ts else ts


def _build_minimal_frontmatter(name: str, source: str, created_at: str) -> str:
    """Synthesize a frontmatter block for SKILL.md files that lack one."""
    return (
        f"---\n"
        f"name: {name}\n"
        f"pinned: false\n"
        f"source: {source}\n"
        f"created_at: {created_at}\n"
        f"---\n\n"
    )


def migrate_skill(skill_md: Path, *, dry_run: bool = False) -> dict:
    """
    Migrate one SKILL.md file. Returns a status dict:
        {"name": str, "path": str, "action": "skipped"|"updated"|"created"|"error",
         "reason": str, "added": [str]}
    """
    name = skill_md.parent.name
    text = skill_md.read_text()
    source = _detect_source(skill_md)
    created_at = _format_created_at(discover_created_at(skill_md.parent))

    m = FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter at all — synthesize one and prepend.
        new_text = _build_minimal_frontmatter(name, source, created_at) + text
        if not dry_run:
            skill_md.write_text(new_text)
        return {"name": name, "path": str(skill_md), "action": "created",
                "reason": "no frontmatter; prepended minimal block",
                "added": list(CURATOR_FIELDS) + ["name"]}

    body, rest = m.group(1), m.group(2)

    missing = [f for f in CURATOR_FIELDS if not _has_field(body, f)]
    if not missing:
        return {"name": name, "path": str(skill_md), "action": "skipped",
                "reason": "all curator fields already present", "added": []}

    additions: list[str] = []
    for f in missing:
        if f == "pinned":
            additions.append("pinned: false")
        elif f == "source":
            additions.append(f"source: {source}")
        elif f == "created_at":
            additions.append(f"created_at: {created_at}")

    new_body = body.rstrip("\n") + "\n" + "\n".join(additions)
    new_text = f"---\n{new_body}\n---\n{rest}"

    if not dry_run:
        skill_md.write_text(new_text)

    return {"name": name, "path": str(skill_md), "action": "updated",
            "reason": f"added {', '.join(missing)}", "added": missing}


def discover_skill_files(skills_root: Path | None = None) -> list[Path]:
    """List every SKILL.md under skills/ that should be migrated."""
    skills_root = skills_root or ALL_SKILLS_DIR
    if not skills_root.exists():
        return []

    out: list[Path] = []
    excluded_dirs = {"auto-learned", "archived"}

    # Top-level skills
    for d in sorted(skills_root.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        if d.name in excluded_dirs:
            continue
        md = d / "SKILL.md"
        if md.exists():
            out.append(md)

    # auto-learned drafts
    al = skills_root / "auto-learned"
    if al.exists():
        for d in sorted(al.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            md = d / "SKILL.md"
            if md.exists():
                out.append(md)

    return out


def migrate_all(*, dry_run: bool = False) -> list[dict]:
    """Migrate every SKILL.md under ALL_SKILLS_DIR. Returns per-file reports."""
    reports = [migrate_skill(p, dry_run=dry_run) for p in discover_skill_files()]
    return reports


def _fmt_report(reports: list[dict]) -> str:
    lines = []
    counts = {"updated": 0, "created": 0, "skipped": 0, "error": 0}
    signs = {"updated": "✓", "created": "+", "skipped": "·", "error": "✗"}
    for r in reports:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        sign = signs.get(r["action"], "?")
        lines.append(f"  {sign} {r['name']:<40s} {r['action']:<8s} {r['reason']}")
    summary = (f"\nTotal: {len(reports)} skills "
               f"({counts['updated']} updated, "
               f"{counts['created']} created, "
               f"{counts['skipped']} skipped, "
               f"{counts['error']} errors)")
    return "\n".join(lines) + summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate SKILL.md frontmatter for Curator")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing")
    ap.add_argument("--skill", help="Migrate only this skill (by name)")
    args = ap.parse_args()

    if args.skill:
        # Find by name in either user_created or auto_learned trees
        for path in discover_skill_files():
            if path.parent.name == args.skill:
                report = migrate_skill(path, dry_run=args.dry_run)
                print(_fmt_report([report]))
                return 0
        print(f"Skill not found: {args.skill}", file=sys.stderr)
        return 1

    reports = migrate_all(dry_run=args.dry_run)
    print(_fmt_report(reports))
    if any(r["action"] == "error" for r in reports):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
