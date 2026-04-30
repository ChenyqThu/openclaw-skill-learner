"""
Curator action executors (Phase 3) — what runs when Lucien approves a Feishu rec.

Two action kinds:
  1. apply_archive(name)         — already in curator_lifecycle; thin re-export
  2. apply_consolidation(rec)    — merge two source skills into a new one

Consolidation is intentionally conservative: it does NOT use Gemini to rewrite
the merged skill. Instead it concatenates both source SKILL.md bodies under a
new directory + frontmatter, archives the originals, and leaves a merge note
for Lucien to clean up by hand. This avoids LLM-introduced regressions at the
approve step.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from config import ALL_SKILLS_DIR, WORKSPACE
from curator_lifecycle import apply_archive, _git_commit, _git_run, _locate_skill_dir
from curator_telemetry import (
    bootstrap_one,
    now_iso,
    parse_frontmatter,
    set_state,
    write_frontmatter_field,
)


# ─── Archive (re-exports lifecycle for symmetry) ────────────────────────────

def apply_archive_rec(rec: dict, *, commit: bool = True) -> dict:
    """Apply a single LLM 'archive' recommendation."""
    name = rec["skill"]
    return apply_archive(name, reason=f"LLM: {rec.get('rationale', '')}",
                         commit=commit)


# ─── Consolidate ────────────────────────────────────────────────────────────

def _read_body_after_frontmatter(skill_md: Path) -> str:
    """Return the SKILL.md content with the YAML frontmatter stripped."""
    text = skill_md.read_text()
    if not text.startswith("---"):
        return text
    # Find the second `---` line.
    closing = text.find("\n---", 4)
    if closing < 0:
        return text
    return text[closing + 4:].lstrip("\n")


def _make_merged_skill_md(new_name: str, rationale: str, source_skills: list[dict]) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    fm = (
        f"---\n"
        f"name: {new_name}\n"
        f"description: Merged from {', '.join(s['name'] for s in source_skills)}.\n"
        f"version: 0.1.0\n"
        f"pinned: false\n"
        f"source: user_created\n"
        f"created_at: {today}\n"
        f"---\n\n"
    )
    header = (
        f"# {new_name}\n\n"
        f"> **Merge note** ({today}): consolidated from "
        f"{', '.join('`' + s['name'] + '`' for s in source_skills)} via curator LLM.\n"
        f"> Reason: {rationale}\n"
        f"> Lucien: please review the merged sections below and dedupe by hand.\n\n"
    )
    sections = []
    for s in source_skills:
        sections.append(f"## From: {s['name']}\n\n{s['body'].strip()}\n")
    return fm + header + "\n\n".join(sections) + "\n"


def apply_consolidation(rec: dict, *, commit: bool = True) -> dict:
    """
    Apply a single LLM 'consolidate' recommendation.

    Steps (atomic where possible):
        1. Read both source SKILL.md bodies
        2. Create skills/{new_name}/ with merged SKILL.md
        3. Archive both source skills
        4. Bootstrap sidecar entry for the new skill
        5. Single workspace-git commit

    Returns: {"new_skill_path", "archived": [...], "git_sha"}
    """
    names = rec.get("skills") or []
    new_name = rec.get("new_name")
    rationale = rec.get("rationale", "")
    if len(names) < 2 or not new_name:
        raise ValueError(f"consolidation rec malformed: {rec!r}")

    # Phase 1: pre-flight
    sources: list[dict] = []
    for n in names:
        d = _locate_skill_dir(n)
        if d is None or "_archived" in str(d):
            raise FileNotFoundError(f"source skill {n!r} not active")
        sources.append({
            "name": n,
            "dir": d,
            "body": _read_body_after_frontmatter(d / "SKILL.md"),
        })

    new_dir = ALL_SKILLS_DIR / new_name
    if new_dir.exists():
        raise FileExistsError(f"target skill dir already exists: {new_dir}")

    # Phase 2: create merged skill
    new_dir.mkdir(parents=True)
    merged = _make_merged_skill_md(new_name, rationale, sources)
    (new_dir / "SKILL.md").write_text(merged)

    # Phase 3: archive sources (commit per archive but bundle git operations)
    archived_results: list[dict] = []
    for s in sources:
        try:
            archived = apply_archive(
                s["name"],
                reason=f"consolidated into {new_name}",
                commit=False,  # we'll commit once at the end
            )
            archived_results.append(archived)
        except Exception as e:
            # Best-effort cleanup if mid-flight failure
            shutil.rmtree(new_dir, ignore_errors=True)
            raise RuntimeError(f"consolidation rolled back: {e}") from e

    # Phase 4: sidecar bootstrap for the new skill
    bootstrap_one(new_name, source="user_created",
                  created_at=now_iso())

    # Phase 5: one git commit covering all moves + new dir
    sha = None
    if commit:
        from_paths = [str(a["from_path"]) for a in archived_results]
        to_paths = [str(a["to_path"]) for a in archived_results]
        new_path_rel = str(new_dir.relative_to(WORKSPACE))
        # Stage everything together
        _git_run("add", new_path_rel)
        for p in from_paths + to_paths:
            try:
                rel = str(Path(p).relative_to(WORKSPACE))
                _git_run("add", "-A", rel)
            except ValueError:
                pass
        msg = (f"curator: consolidate {' + '.join(names)} → {new_name}\n\n"
               f"Reason: {rationale}")
        r = _git_run("commit", "-m", msg)
        if r.returncode == 0:
            sha = _git_run("rev-parse", "--short", "HEAD").stdout.strip() or None

    return {
        "new_skill_path": str(new_dir),
        "archived": archived_results,
        "git_sha": sha,
        "names": names,
        "new_name": new_name,
    }


# ─── Recommendation lookup ──────────────────────────────────────────────────

def find_recommendation(run_dir: Path, rec_id: str) -> dict | None:
    """Look up a recommendation by id from a curator run.json."""
    import json
    rj = run_dir / "run.json"
    if not rj.exists():
        return None
    try:
        doc = json.loads(rj.read_text())
    except Exception:
        return None
    result = doc.get("result") or {}
    for bucket in ("consolidations", "archives", "keep"):
        for r in result.get(bucket) or []:
            if r.get("id") == rec_id:
                return r
    return None


def mark_recommendation(run_dir: Path, rec_id: str, status: str,
                        note: str | None = None) -> bool:
    """Mark a rec as approved/rejected in run.json. Idempotent."""
    import json
    rj = run_dir / "run.json"
    if not rj.exists():
        return False
    doc = json.loads(rj.read_text())
    result = doc.get("result") or {}
    found = False
    for bucket in ("consolidations", "archives", "keep"):
        for r in result.get(bucket) or []:
            if r.get("id") == rec_id:
                r["_status"] = status
                if note:
                    r["_note"] = note
                r["_decided_at"] = now_iso()
                found = True
                break
    if found:
        rj.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return found
