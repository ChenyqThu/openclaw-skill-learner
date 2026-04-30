"""
Curator LLM review (Phase 3) — Gemini-backed consolidation/archive recommender.

Pipeline:
    1. Collect active skills (excluding pinned + archived) with metadata
    2. Build prompt via prompts.curator_v1
    3. Call Gemini (reuses scripts/gemini_client.call_gemini)
    4. Validate output JSON: drop consolidations referencing missing skills,
       enforce hard rules (applied_count > 3 → never archive, etc.)
    5. Write run.json (machine) + REPORT.md (human) under
       data/skill-learner/curator-reports/<ISO-ts>/
    6. Update sidecar `_meta.last_llm_review_at`
    7. Optionally push a Feishu card via evaluate-server.send_curator_report

The CLI dispatch lives in scripts/curator.py (--llm-review / --llm-review-if-due).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from config import ALL_SKILLS_DIR, CURATOR_REPORTS_DIR
from curator_lifecycle import _locate_skill_dir
from curator_telemetry import (
    days_since,
    read_skill_meta,
    read_usage,
    set_meta,
)

# Gemini client is shared across tracks
from gemini_client import call_gemini  # type: ignore
from prompts.curator_v1 import build_prompt, render_report_markdown

LLM_REVIEW_INTERVAL_DAYS = 14
LLM_REVIEW_MIN_ACTIVE_AUTO_LEARNED = 5
GEMINI_MODEL = "gemini-3-flash-preview"


# ─── Build the input context ────────────────────────────────────────────────

def collect_active_skills() -> list[dict]:
    """Return skills eligible for LLM review (active, not pinned, not archived).

    Returns dicts with name/source/age_days/last_applied_at/applied_count/skill_md.
    Skills missing on disk are silently dropped.
    """
    data = read_usage()
    out: list[dict] = []
    for name, entry in data.get("skills", {}).items():
        if entry.get("state") in ("archived",):
            continue
        skill_dir = _locate_skill_dir(name)
        if skill_dir is None or "_archived" in str(skill_dir):
            continue
        meta = read_skill_meta(skill_dir)
        if meta.get("pinned"):
            continue
        skill_md_path = skill_dir / "SKILL.md"
        try:
            content = skill_md_path.read_text()
        except OSError:
            continue
        out.append({
            "name": name,
            "source": meta.get("source", "user_created"),
            "age_days": days_since(meta.get("created_at")),
            "last_applied_at": entry.get("last_applied_at"),
            "applied_count": entry.get("applied_count", 0),
            "read_count": entry.get("read_count", 0),
            "skill_md": content,
        })
    # Stable order: oldest first (highest archive risk near the top).
    out.sort(key=lambda s: (s.get("age_days") or 0), reverse=True)
    return out


# ─── LLM call + JSON validation ─────────────────────────────────────────────

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _parse_llm_json(raw: str) -> dict | None:
    """Extract a JSON object from Gemini's reply. Returns None if invalid."""
    if not raw:
        return None
    candidate = raw.strip()
    m = JSON_FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def validate_review(result: dict, active_names: set[str],
                    applied_counts: dict[str, int],
                    sources: dict[str, str]) -> tuple[dict, list[str]]:
    """Filter LLM output against hard rules. Returns (clean, warnings).

    Drops:
      - consolidations referencing skills not in `active_names`
      - consolidations crossing source boundaries
      - archives referencing skills with applied_count > 3
      - archives referencing skills not in `active_names`
    """
    cons_in   = result.get("consolidations") or []
    archs_in  = result.get("archives") or []
    keeps_in  = result.get("keep") or []
    warnings: list[str] = []

    cons_out: list[dict] = []
    for c in cons_in:
        names = c.get("skills") or []
        if not names or len(names) < 2:
            warnings.append(f"consolidation {c.get('id')}: needs >=2 skills, dropped")
            continue
        missing = [n for n in names if n not in active_names]
        if missing:
            warnings.append(f"consolidation {c.get('id')}: unknown skills {missing}, dropped")
            continue
        srcs = {sources.get(n, "?") for n in names}
        if len(srcs) > 1:
            warnings.append(f"consolidation {c.get('id')}: cross-source {srcs}, dropped")
            continue
        cons_out.append(c)

    archs_out: list[dict] = []
    for a in archs_in:
        n = a.get("skill")
        if n not in active_names:
            warnings.append(f"archive {a.get('id')}: unknown skill {n!r}, dropped")
            continue
        if applied_counts.get(n, 0) > 3:
            warnings.append(f"archive {a.get('id')}: applied_count>3 for {n!r}, dropped")
            continue
        archs_out.append(a)

    return {"consolidations": cons_out, "archives": archs_out, "keep": keeps_in}, warnings


# ─── Run-directory writers ──────────────────────────────────────────────────

def _run_dir() -> Path:
    ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    d = CURATOR_REPORTS_DIR / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _update_latest_symlink(target: Path) -> None:
    latest = CURATOR_REPORTS_DIR / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(target.name)
    except OSError:
        pass  # symlinks may fail on weird filesystems — non-fatal


# ─── Cadence check (--llm-review-if-due) ────────────────────────────────────

def is_review_due(active_skills: list[dict] | None = None) -> tuple[bool, str]:
    """Return (should_run, reason)."""
    data = read_usage()
    last = data.get("_meta", {}).get("last_llm_review_at")
    if last:
        try:
            ts = datetime.fromisoformat(last)
            age = (datetime.now().astimezone() - ts.astimezone()).days
            if age < LLM_REVIEW_INTERVAL_DAYS:
                return False, f"last review was {age}d ago, < {LLM_REVIEW_INTERVAL_DAYS}d cadence"
        except ValueError:
            pass

    skills = active_skills if active_skills is not None else collect_active_skills()
    auto = sum(1 for s in skills if s.get("source") == "auto_learned")
    if auto < LLM_REVIEW_MIN_ACTIVE_AUTO_LEARNED:
        return False, (f"only {auto} active auto_learned skills "
                       f"(<{LLM_REVIEW_MIN_ACTIVE_AUTO_LEARNED} threshold)")
    return True, f"{auto} active auto_learned skills, cadence overdue"


# ─── Main entry ─────────────────────────────────────────────────────────────

def run_review(*, dry_run: bool = False, send_feishu: bool = True) -> dict:
    """
    Execute one LLM curator pass. Returns a dict:
        {
          "run_dir": str | None,
          "skipped": bool,
          "reason": str,
          "input_count": int,
          "result": {"consolidations":[...], "archives":[...], "keep":[...]} | None,
          "warnings": [str, ...],
          "raw_response": str (only when dry_run),
        }
    """
    skills = collect_active_skills()
    if not skills:
        return {"run_dir": None, "skipped": True,
                "reason": "no active skills to review",
                "input_count": 0, "result": None, "warnings": []}

    prompt = build_prompt(skills)

    if dry_run:
        # Dry-run: do not call Gemini. Synthesize a stub "all keep" review so
        # the file scaffolding is exercised without spending tokens.
        stub_result = {
            "consolidations": [],
            "archives": [],
            "keep": [{"id": f"k{i+1}", "skill": s["name"],
                      "reason": "dry-run stub"} for i, s in enumerate(skills)],
        }
        warnings = ["dry-run: Gemini not called"]
        run_dir = _run_dir()
        ts = run_dir.name
        (run_dir / "run.json").write_text(json.dumps({
            "ts": ts, "dry_run": True, "input_count": len(skills),
            "result": stub_result, "warnings": warnings,
            "prompt_preview": prompt[:1000],
        }, indent=2, ensure_ascii=False))
        (run_dir / "REPORT.md").write_text(
            render_report_markdown(skills, stub_result, ts))
        _update_latest_symlink(run_dir)
        return {"run_dir": str(run_dir), "skipped": False,
                "reason": "dry-run completed",
                "input_count": len(skills), "result": stub_result,
                "warnings": warnings}

    # Real call
    try:
        raw = call_gemini(prompt, model=GEMINI_MODEL, temperature=0.2,
                          max_tokens=8192)
    except TypeError:
        # Older signature without `model` kwarg
        raw = call_gemini(prompt, temperature=0.2, max_tokens=8192)

    parsed = _parse_llm_json(raw or "")
    if parsed is None:
        run_dir = _run_dir()
        (run_dir / "run.json").write_text(json.dumps({
            "ts": run_dir.name, "error": "Gemini returned invalid JSON",
            "raw_response": raw or "", "input_count": len(skills),
        }, indent=2, ensure_ascii=False))
        return {"run_dir": str(run_dir), "skipped": False,
                "reason": "Gemini returned invalid JSON",
                "input_count": len(skills), "result": None,
                "warnings": ["JSON parse failed"]}

    active_names = {s["name"] for s in skills}
    applied_counts = {s["name"]: s.get("applied_count", 0) for s in skills}
    sources = {s["name"]: s.get("source", "user_created") for s in skills}
    clean, warnings = validate_review(parsed, active_names, applied_counts, sources)

    run_dir = _run_dir()
    ts = run_dir.name
    (run_dir / "run.json").write_text(json.dumps({
        "ts": ts, "input_count": len(skills), "result": clean,
        "raw_llm_output": parsed, "warnings": warnings,
    }, indent=2, ensure_ascii=False))
    (run_dir / "REPORT.md").write_text(render_report_markdown(skills, clean, ts))
    _update_latest_symlink(run_dir)

    set_meta("last_llm_review_at",
             datetime.now().astimezone().isoformat(timespec="seconds"))

    out = {"run_dir": str(run_dir), "skipped": False,
           "reason": "completed", "input_count": len(skills),
           "result": clean, "warnings": warnings}

    if send_feishu and (clean["consolidations"] or clean["archives"]):
        try:
            from evaluate_server_curator_card import send_curator_report  # type: ignore
            send_curator_report(run_dir)
            out["feishu_sent"] = True
        except Exception as e:
            out["feishu_sent"] = False
            out["feishu_error"] = str(e)

    return out
