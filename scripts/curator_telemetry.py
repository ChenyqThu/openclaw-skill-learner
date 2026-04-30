"""
Curator telemetry sidecar manager.

Owns reads/writes to `data/skill-learner/skill-usage.json` (counters + lifecycle
state). Frontmatter (pinned/source/created_at) is source-of-truth in SKILL.md
and managed by curator_migrate_frontmatter.py + curator_lifecycle.py.

Concurrency model: this module acquires fcntl LOCK_EX around every read-modify-
write. The plugin (plugin/index.js) writes best-effort without a lock — its
counter increments may be lost on conflict, which is acceptable per the plan
(counters are approximate). The atomic tmp+rename guarantees the file is never
half-written.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from config import ALL_SKILLS_DIR, SKILL_USAGE_FILE, WORKSPACE

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

SCHEMA_VERSION = 1


# ─── Time helpers ────────────────────────────────────────────────────────────

def now_iso() -> str:
    """Local-tz ISO timestamp matching the plugin's daily-stats convention."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─── File lock + atomic write ────────────────────────────────────────────────

@contextmanager
def _locked_file(path: Path) -> Iterator[None]:
    """Take an exclusive fcntl lock on `path` (creating it if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _empty_doc() -> dict:
    return {
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "last_curator_tick_at": None,
            "last_llm_review_at": None,
        },
        "skills": {},
    }


def read_usage() -> dict:
    """Read sidecar JSON. Returns empty doc if missing or unreadable."""
    if not SKILL_USAGE_FILE.exists():
        return _empty_doc()
    try:
        return json.loads(SKILL_USAGE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return _empty_doc()


def write_usage(data: dict) -> None:
    """Atomic write via tmp + os.replace. Caller must hold the lock."""
    SKILL_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SKILL_USAGE_FILE.with_suffix(SKILL_USAGE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, SKILL_USAGE_FILE)


# ─── Skill entry helpers ─────────────────────────────────────────────────────

def _empty_entry(now: str) -> dict:
    return {
        "read_count": 0,
        "applied_count": 0,
        "patch_count": 0,
        "last_read_at": None,
        "last_applied_at": None,
        "last_patched_at": None,
        "state": "active",
        "state_changed_at": now,
        "archived_at": None,
        "archive_path": None,
    }


def _ensure_entry(data: dict, name: str, now: str) -> dict:
    if name not in data["skills"]:
        data["skills"][name] = _empty_entry(now)
    return data["skills"][name]


# ─── Counter bumps (called by curator.py on demand, NOT by plugin) ──────────
# The plugin (plugin/index.js) writes counters directly via its own JS helpers.
# These Python bumps are used by tests, manual operations, and any scripted
# touches.

def bump_read(name: str, ts: str | None = None) -> None:
    ts = ts or now_iso()
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        entry = _ensure_entry(data, name, ts)
        entry["read_count"] += 1
        entry["last_read_at"] = ts
        write_usage(data)


def bump_applied(name: str, ts: str | None = None) -> None:
    ts = ts or now_iso()
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        entry = _ensure_entry(data, name, ts)
        entry["applied_count"] += 1
        entry["last_applied_at"] = ts
        write_usage(data)


def bump_patched(name: str, ts: str | None = None) -> None:
    ts = ts or now_iso()
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        entry = _ensure_entry(data, name, ts)
        entry["patch_count"] += 1
        entry["last_patched_at"] = ts
        write_usage(data)


# ─── Lifecycle state transitions ─────────────────────────────────────────────

def set_state(name: str, new_state: str, archive_path: str | None = None,
              ts: str | None = None) -> None:
    """Update lifecycle state (active/stale/archived) and timestamps atomically."""
    ts = ts or now_iso()
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        entry = _ensure_entry(data, name, ts)
        if entry["state"] != new_state:
            entry["state"] = new_state
            entry["state_changed_at"] = ts
        if new_state == "archived":
            entry["archived_at"] = ts
            if archive_path is not None:
                entry["archive_path"] = archive_path
        elif new_state == "active":
            entry["archived_at"] = None
            entry["archive_path"] = None
        write_usage(data)


def set_meta(key: str, value) -> None:
    """Update a `_meta` field (e.g., last_curator_tick_at)."""
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        data["_meta"][key] = value
        write_usage(data)


# ─── Bootstrap (one-time, callable many times — idempotent) ─────────────────

def _git_first_commit_date(skill_path: Path) -> str | None:
    """Return ISO date of the first commit that added skills/<name>/SKILL.md, or None."""
    rel = skill_path.relative_to(WORKSPACE)
    try:
        r = subprocess.run(
            ["git", "log", "--diff-filter=A", "--reverse", "--format=%aI",
             "--", str(rel / "SKILL.md")],
            cwd=str(WORKSPACE), capture_output=True, text=True, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            first_line = r.stdout.strip().splitlines()[0]
            return first_line  # e.g. "2026-03-15T14:22:13+08:00"
    except FileNotFoundError:
        pass
    return None


def _stat_birthtime(skill_path: Path) -> str:
    """Fallback: use APFS st_birthtime (macOS)."""
    md = skill_path / "SKILL.md"
    if md.exists():
        st = md.stat()
        ts = getattr(st, "st_birthtime", st.st_mtime)
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    return now_iso()


def discover_created_at(skill_path: Path) -> str:
    """Best-effort: git first-add commit date, fallback to filesystem birthtime."""
    return _git_first_commit_date(skill_path) or _stat_birthtime(skill_path)


def bootstrap_one(name: str, *, source: str = "user_created",
                  created_at: str | None = None, state: str = "active") -> None:
    """Seed a single skill's sidecar entry (idempotent — does not overwrite counters)."""
    now = now_iso()
    with _locked_file(SKILL_USAGE_FILE):
        data = read_usage()
        if name not in data["skills"]:
            data["skills"][name] = _empty_entry(now)
            data["skills"][name]["state_changed_at"] = created_at or now
            data["skills"][name]["state"] = state
        # source/created_at live in frontmatter — not duplicated here.
        write_usage(data)


def bootstrap_from_git(skills_root: Path | None = None) -> list[dict]:
    """
    Scan `skills_root/**` and `skills_root/auto-learned/*` and seed sidecar
    entries for every SKILL.md found. Returns a report list of:
        [{"name": str, "created_at": str, "source": str, "seeded": bool}]

    Idempotent: existing entries are not overwritten.
    """
    skills_root = skills_root or ALL_SKILLS_DIR
    report: list[dict] = []
    if not skills_root.exists():
        return report

    seen_names: set[str] = set()

    # Top-level skills (e.g. skills/x-tweet-fetcher/) → user_created
    excluded_dirs = {"auto-learned", "archived"}
    for d in sorted(skills_root.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        if d.name in excluded_dirs:
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue
        seen_names.add(d.name)
        existed = d.name in read_usage()["skills"]
        bootstrap_one(d.name, source="user_created",
                      created_at=discover_created_at(d))
        report.append({
            "name": d.name,
            "created_at": discover_created_at(d),
            "source": "user_created",
            "seeded": not existed,
        })

    # auto-learned drafts: keep visible to the curator (state=active) but
    # mark as auto_learned source. Approve flow flips them into top-level.
    al_root = skills_root / "auto-learned"
    if al_root.exists():
        for d in sorted(al_root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            seen_names.add(d.name)
            existed = d.name in read_usage()["skills"]
            bootstrap_one(d.name, source="auto_learned",
                          created_at=discover_created_at(d))
            report.append({
                "name": d.name,
                "created_at": discover_created_at(d),
                "source": "auto_learned",
                "seeded": not existed,
            })

    return report


# ─── Read-only helpers for status/CLI display ────────────────────────────────

def list_skills_with_state() -> list[tuple[str, dict]]:
    """Return [(name, entry)] sorted by last_applied_at desc (None last)."""
    data = read_usage()
    items = list(data["skills"].items())

    def _key(item):
        ts = item[1].get("last_applied_at")
        return (0 if ts else 1, ts or "")

    return sorted(items, key=_key)


def days_since(ts_str: str | None) -> int | None:
    """Whole days from `ts_str` (ISO) until now. None if ts_str is None."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    delta = datetime.now().astimezone() - ts.astimezone()
    return delta.days


# ─── SKILL.md frontmatter readers ────────────────────────────────────────────
# Source-of-truth for: pinned, source, created_at, name. Counters live in sidecar.

_FIELD_LINE_RE = re.compile(r"^([a-zA-Z_][\w-]*)\s*:\s*(.*?)\s*$")


def parse_frontmatter(skill_md: Path) -> dict:
    """Parse SKILL.md YAML frontmatter into a flat dict.

    Only handles top-level scalar fields (which is all curator cares about).
    Returns {} if no frontmatter.
    """
    if not skill_md.exists():
        return {}
    text = skill_md.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Skip nested keys (those start with whitespace).
        if line[0] in (" ", "\t"):
            continue
        match = _FIELD_LINE_RE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        # Strip optional surrounding quotes; coerce booleans
        if raw.lower() in ("true", "false"):
            out[key] = (raw.lower() == "true")
        elif (raw.startswith('"') and raw.endswith('"')) or \
             (raw.startswith("'") and raw.endswith("'")):
            out[key] = raw[1:-1]
        else:
            out[key] = raw
    return out


def read_skill_meta(skill_dir: Path) -> dict:
    """Convenience: parse frontmatter for a skill directory.

    Defaults: pinned=False, source='user_created', created_at=None.
    """
    fm = parse_frontmatter(skill_dir / "SKILL.md")
    return {
        "name":       fm.get("name", skill_dir.name),
        "pinned":     bool(fm.get("pinned", False)),
        "source":     fm.get("source", "user_created"),
        "created_at": fm.get("created_at"),
    }


def write_frontmatter_field(skill_md: Path, field: str, value) -> bool:
    """Set or update a single top-level frontmatter field. Idempotent.

    Returns True if file was modified, False if value already matches.
    Synthesizes a frontmatter block if the file lacks one entirely.
    """
    text = skill_md.read_text() if skill_md.exists() else ""
    m = FRONTMATTER_RE.match(text)
    yaml_value = "true" if value is True else "false" if value is False else str(value)

    if not m:
        # No frontmatter — synthesize minimal block at the top.
        new = f"---\n{field}: {yaml_value}\n---\n\n{text}"
        skill_md.write_text(new)
        return True

    body = m.group(1)
    rest = text[m.end():]
    body_lines = body.splitlines()
    new_lines: list[str] = []
    found = False
    line_to_add = f"{field}: {yaml_value}"
    for line in body_lines:
        match = _FIELD_LINE_RE.match(line) if line and line[0] not in (" ", "\t") else None
        if match and match.group(1) == field:
            if line == line_to_add:
                return False  # idempotent no-op
            new_lines.append(line_to_add)
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(line_to_add)
    new_text = "---\n" + "\n".join(new_lines) + "\n---\n" + rest
    skill_md.write_text(new_text)
    return True
