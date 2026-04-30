"""
Curator lifecycle (Phase 2) — deterministic state machine + archive/restore/pin.

State transitions (from plan §4):
    active → stale:
      - last_applied_at is null AND now - created_at > 30d AND source == auto_learned
      - last_applied_at is not null AND now - last_applied_at > 60d
    stale → archived:
      - state == stale AND now - state_changed_at > 30d AND NOT pinned
    Any pinned skill is unconditionally skipped.

Pin/source/created_at live in SKILL.md frontmatter (source-of-truth).
Counters/state/timestamps live in skill-usage.json (sidecar).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config import ALL_SKILLS_DIR, ARCHIVED_SKILLS_DIR, WORKSPACE
from curator_telemetry import (
    _locked_file,
    now_iso,
    read_skill_meta,
    read_usage,
    set_meta,
    set_state,
    write_frontmatter_field,
    write_usage,
)

STALE_AUTO_LEARNED_NEVER_APPLIED_DAYS = 30
STALE_AFTER_LAST_APPLIED_DAYS = 60
ARCHIVED_AFTER_STALE_DAYS = 30


# ─── State machine ───────────────────────────────────────────────────────────

@dataclass
class Transition:
    name: str
    old_state: str
    new_state: str
    reason: str
    skipped_pinned: bool = False


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Sidecar timestamps are full ISO; frontmatter created_at may be date-only.
        if "T" in s:
            return datetime.fromisoformat(s).astimezone()
        # Date-only "2026-04-15" → midnight local
        return datetime.fromisoformat(s + "T00:00:00").astimezone()
    except ValueError:
        return None


def _days_between(later: datetime, earlier: datetime) -> int:
    return (later - earlier).days


def _locate_skill_dir(name: str) -> Path | None:
    """Best-effort: find a skill's current directory across the workspace tree."""
    for root in (ALL_SKILLS_DIR, ALL_SKILLS_DIR / "auto-learned",
                 ARCHIVED_SKILLS_DIR):
        p = root / name
        if (p / "SKILL.md").exists():
            return p
    return None


def evaluate_transitions(now: datetime | None = None) -> list[Transition]:
    """Pure function: read sidecar + frontmatters, return state-transition plan.

    Does not write anything.
    """
    now = now or datetime.now().astimezone()
    data = read_usage()
    out: list[Transition] = []

    for name, entry in data.get("skills", {}).items():
        skill_dir = _locate_skill_dir(name)
        if not skill_dir:
            # Lucien manually deleted/moved this skill — skip, lifecycle has no claim.
            continue

        fm = read_skill_meta(skill_dir)
        if fm.get("pinned"):
            out.append(Transition(name, entry.get("state", "active"),
                                  entry.get("state", "active"),
                                  "skipped (pinned)", skipped_pinned=True))
            continue

        old_state = entry.get("state", "active")
        last_applied = _parse_date(entry.get("last_applied_at"))
        created = _parse_date(fm.get("created_at"))
        state_changed = _parse_date(entry.get("state_changed_at"))
        source = fm.get("source", "user_created")

        new_state = old_state
        reason = ""

        if old_state == "active":
            if last_applied is None and source == "auto_learned" and created and \
                    _days_between(now, created) > STALE_AUTO_LEARNED_NEVER_APPLIED_DAYS:
                new_state = "stale"
                reason = (f"auto_learned, never applied, "
                          f"{_days_between(now, created)}d old "
                          f"(>{STALE_AUTO_LEARNED_NEVER_APPLIED_DAYS}d)")
            elif last_applied is not None and \
                    _days_between(now, last_applied) > STALE_AFTER_LAST_APPLIED_DAYS:
                new_state = "stale"
                reason = (f"no apply for {_days_between(now, last_applied)}d "
                          f"(>{STALE_AFTER_LAST_APPLIED_DAYS}d)")

        elif old_state == "stale":
            if state_changed and \
                    _days_between(now, state_changed) > ARCHIVED_AFTER_STALE_DAYS:
                new_state = "archived"
                reason = (f"stale for {_days_between(now, state_changed)}d "
                          f"(>{ARCHIVED_AFTER_STALE_DAYS}d)")

        if new_state != old_state:
            out.append(Transition(name, old_state, new_state, reason))

    return out


# ─── Archive / restore (mutations) ──────────────────────────────────────────

def _git_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(WORKSPACE),
        capture_output=True, text=True, check=False,
    )


def _git_commit(message: str, *paths: str) -> str | None:
    """Stage paths, commit; return short SHA or None on failure."""
    if not paths:
        return None
    add = _git_run("add", "--", *paths)
    if add.returncode != 0:
        # Some paths may have been deleted (mv) — git add -A on those parents
        for p in paths:
            _git_run("add", "-A", p.split("/", 1)[0] if "/" in p else p)
    r = _git_run("commit", "-m", message)
    if r.returncode != 0:
        return None
    rev = _git_run("rev-parse", "--short", "HEAD")
    return rev.stdout.strip() or None


def apply_archive(name: str, *, reason: str = "stale", commit: bool = True) -> dict:
    """
    Move skills/{name}/ → skills/_archived/{name}-{YYYY-MM-DD}/, update sidecar,
    workspace-git commit.

    Returns: {"name", "from_path", "to_path", "git_sha"}
    """
    src = _locate_skill_dir(name)
    if src is None or src.parent == ARCHIVED_SKILLS_DIR:
        raise FileNotFoundError(f"Skill {name!r} not found in active tree")

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    dst_name = f"{name}-{today}"
    ARCHIVED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVED_SKILLS_DIR / dst_name

    if dst.exists():
        # Same skill archived twice in a single day — rare but handle.
        ts = datetime.now().astimezone().strftime("%H%M%S")
        dst = ARCHIVED_SKILLS_DIR / f"{name}-{today}-{ts}"

    shutil.move(str(src), str(dst))
    archive_path_rel = str(dst.relative_to(ALL_SKILLS_DIR))
    set_state(name, "archived", archive_path=archive_path_rel)

    sha = None
    if commit:
        src_rel = str(src.relative_to(WORKSPACE))
        dst_rel = str(dst.relative_to(WORKSPACE))
        sha = _git_commit(f"curator: archive {name} ({reason})", src_rel, dst_rel)

    return {"name": name, "from_path": str(src), "to_path": str(dst), "git_sha": sha}


def apply_restore(name: str, *, commit: bool = True) -> dict:
    """
    Move skills/_archived/<archive_path>/ → skills/{name}/, reset sidecar
    state, workspace-git commit.

    Returns: {"name", "from_path", "to_path", "git_sha"}
    """
    data = read_usage()
    entry = data.get("skills", {}).get(name)
    if not entry:
        raise KeyError(f"Skill {name!r} not in sidecar")
    archive_path = entry.get("archive_path")
    if not archive_path:
        raise ValueError(f"Skill {name!r} is not archived (no archive_path)")

    src = ALL_SKILLS_DIR / archive_path
    if not src.exists():
        raise FileNotFoundError(f"Archive directory missing: {src}")

    dst = ALL_SKILLS_DIR / name
    if dst.exists():
        raise FileExistsError(f"Active skill already exists at {dst}; resolve first")

    shutil.move(str(src), str(dst))
    set_state(name, "active")  # also clears archived_at + archive_path

    sha = None
    if commit:
        src_rel = str(src.relative_to(WORKSPACE))
        dst_rel = str(dst.relative_to(WORKSPACE))
        sha = _git_commit(f"curator: restore {name}", src_rel, dst_rel)

    return {"name": name, "from_path": str(src), "to_path": str(dst), "git_sha": sha}


# ─── Pin / unpin (frontmatter writes) ───────────────────────────────────────

def pin(name: str) -> dict:
    skill_dir = _locate_skill_dir(name)
    if skill_dir is None:
        raise FileNotFoundError(f"Skill {name!r} not found")
    changed = write_frontmatter_field(skill_dir / "SKILL.md", "pinned", True)
    return {"name": name, "pinned": True, "changed": changed,
            "path": str(skill_dir / "SKILL.md")}


def unpin(name: str) -> dict:
    skill_dir = _locate_skill_dir(name)
    if skill_dir is None:
        raise FileNotFoundError(f"Skill {name!r} not found")
    changed = write_frontmatter_field(skill_dir / "SKILL.md", "pinned", False)
    return {"name": name, "pinned": False, "changed": changed,
            "path": str(skill_dir / "SKILL.md")}


# ─── Tick (orchestrates evaluate + apply) ───────────────────────────────────

def run_tick(*, dry_run: bool = False, commit: bool = True) -> dict:
    """
    Run the deterministic state machine once. Stale transitions just update the
    sidecar; archive transitions move the directory + update sidecar + commit.

    Returns:
        {
          "transitions":  [Transition-as-dict, ...],
          "archived":     [archive-result-dict, ...],
          "errors":       [{"name", "reason"}, ...],
          "dry_run":      bool
        }
    """
    transitions = evaluate_transitions()
    out = {"transitions": [], "archived": [], "errors": [], "dry_run": dry_run}
    now = now_iso()

    for t in transitions:
        out["transitions"].append({
            "name": t.name, "from": t.old_state, "to": t.new_state,
            "reason": t.reason, "skipped_pinned": t.skipped_pinned,
        })
        if t.skipped_pinned or t.new_state == t.old_state:
            continue
        if dry_run:
            continue
        try:
            if t.new_state == "stale":
                set_state(t.name, "stale")
            elif t.new_state == "archived":
                arch = apply_archive(t.name, reason=t.reason, commit=commit)
                out["archived"].append(arch)
        except Exception as e:
            out["errors"].append({"name": t.name, "reason": str(e)})

    if not dry_run:
        set_meta("last_curator_tick_at", now)

    return out
