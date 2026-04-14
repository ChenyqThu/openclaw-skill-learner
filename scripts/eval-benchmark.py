#!/usr/bin/env python3
"""
Eval Benchmark — Darwin-style evaluation framework for Gemini prompt optimization.

Runs labeled test cases through a specified prompt version, scores results
across 6 dimensions, and outputs a TSV report compatible with darwin-skill's results.tsv.

Usage:
  python3 eval-benchmark.py                          # Run with v1_baseline
  python3 eval-benchmark.py --prompt v2_optimized    # Run with specific version
  python3 eval-benchmark.py --dry-run                # Use cached results, no API calls
  python3 eval-benchmark.py --verbose                # Show per-case details
"""

import json
import os
import re
import sys
import importlib
import importlib.util
from pathlib import Path
from datetime import datetime

from gemini_client import load_env as _load_env
from gemini_client import call_gemini, extract_eval_json, extract_skill_md

# ─── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
TEST_CASES_DIR = SCRIPT_DIR / "test-cases"
RESULTS_DIR = SCRIPT_DIR / "darwin-results"
CACHE_DIR = SCRIPT_DIR / "darwin-results" / "cache"
ALL_SKILLS_DIR = Path.home() / ".openclaw/workspace/skills"

_load_env()

# ─── Scoring Weights (total = 100) ──────────────────────────────────────────
WEIGHTS = {
    "accuracy": 35,    # Correct YES/NO/UPDATE classification
    "precision": 20,   # Of those marked YES, how many are truly valuable
    "recall": 15,      # Of truly valuable sessions, how many detected
    "quality": 15,     # eval_json completeness + skill_md usability
    "dedup": 10,       # Doesn't duplicate existing skills
    "robustness": 5,   # Output format parses correctly
}


# ─── Load Prompt Module ─────────────────────────────────────────────────────
def load_prompt_module(version: str):
    """Dynamically load a prompt version module from scripts/prompts/."""
    module_name = f"prompts.{version}"
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPT_DIR / "prompts" / f"{version}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



# call_gemini, extract_eval_json, extract_skill_md imported from gemini_client


def classify_result(result: str) -> str:
    """Classify Gemini output as YES (new skill), UPDATE, or NO."""
    text = result.strip()
    if "NO_SKILL" in text:
        return "NO"
    if "NO_UPDATE" in text:
        return "NO"
    if "```skill_update" in text:
        return "UPDATE"
    if "```skill_md" in text or "```eval_json" in text:
        return "YES"
    return "NO"


# ─── Existing Skills Context ────────────────────────────────────────────────
def get_existing_skills_summary() -> str:
    summary_lines = []
    for skill_md in ALL_SKILLS_DIR.rglob("SKILL.md"):
        if "/auto-learned/" in str(skill_md):
            continue
        try:
            text = skill_md.read_text()[:1500]
            name = skill_md.parent.name
            desc = ""
            fm = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL | re.MULTILINE)
            if fm:
                dm = re.search(r"description:\s*(.+)", fm.group(1))
                if dm:
                    desc = dm.group(1).strip().strip("\"'")[:200]
            if desc:
                summary_lines.append(f"- {name}: {desc}")
        except Exception:
            continue
    return "\n".join(summary_lines[:60]) if summary_lines else "(none)"


def load_skill_content(skill_name: str) -> str | None:
    """Load existing skill content for update evaluation."""
    for skill_md in ALL_SKILLS_DIR.rglob("SKILL.md"):
        if skill_md.parent.name == skill_name and "/auto-learned/" not in str(skill_md):
            return skill_md.read_text()
    return None


# ─── Test Case Loader ────────────────────────────────────────────────────────
def load_test_cases() -> list[dict]:
    """Load all test cases with ground truth labels."""
    cases = []
    for category in ["should-extract", "should-reject", "should-update"]:
        category_dir = TEST_CASES_DIR / category
        if not category_dir.exists():
            continue
        ground_truth = {"should-extract": "YES", "should-reject": "NO", "should-update": "UPDATE"}[category]
        for f in sorted(category_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                cases.append({
                    "file": f.name,
                    "category": category,
                    "ground_truth": ground_truth,
                    "data": data,
                })
            except Exception as e:
                print(f"WARN: Failed to load {f}: {e}")
    return cases


# ─── Cache Management ────────────────────────────────────────────────────────
def get_cache_path(version: str, case_file: str) -> Path:
    return CACHE_DIR / version / f"{case_file}.result.txt"


def load_cached_result(version: str, case_file: str) -> str | None:
    p = get_cache_path(version, case_file)
    if p.exists():
        return p.read_text()
    return None


def save_cached_result(version: str, case_file: str, result: str):
    p = get_cache_path(version, case_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(result)


# ─── Scoring Functions ───────────────────────────────────────────────────────
def score_accuracy(predictions: list[dict]) -> float:
    """Percentage of correct YES/NO/UPDATE classifications (0-10 scale)."""
    if not predictions:
        return 0.0
    correct = sum(1 for p in predictions if p["predicted"] == p["ground_truth"])
    return (correct / len(predictions)) * 10


def score_precision(predictions: list[dict]) -> float:
    """Of predicted YES/UPDATE, how many are correct (0-10 scale)."""
    positives = [p for p in predictions if p["predicted"] in ("YES", "UPDATE")]
    if not positives:
        return 10.0  # No false positives if no positives predicted
    correct = sum(1 for p in positives if p["predicted"] == p["ground_truth"])
    return (correct / len(positives)) * 10


def score_recall(predictions: list[dict]) -> float:
    """Of actual YES/UPDATE, how many were detected (0-10 scale)."""
    actual_pos = [p for p in predictions if p["ground_truth"] in ("YES", "UPDATE")]
    if not actual_pos:
        return 10.0
    detected = sum(1 for p in actual_pos if p["predicted"] in ("YES", "UPDATE"))
    return (detected / len(actual_pos)) * 10


def score_quality(predictions: list[dict]) -> float:
    """Average quality of produced eval_json + skill_md content (0-10 scale)."""
    scored = [p for p in predictions if p["predicted"] in ("YES", "UPDATE") and p.get("result")]
    if not scored:
        return 5.0  # Neutral if nothing to score

    total = 0.0
    for p in scored:
        result = p["result"]
        sub = 0.0
        ej = extract_eval_json(result)
        # eval_json completeness (0-5): check key fields
        expected_keys = {"skill_name", "problem_context", "recommended_approach", "when_to_use"}
        if ej:
            present = sum(1 for k in expected_keys if ej.get(k))
            sub += (present / len(expected_keys)) * 5
        # skill_md usability (0-5): check sections exist
        sm = extract_skill_md(result) or ""
        expected_sections = ["适用场景", "操作步骤", "已知雷区", "验证方式"]
        sections_found = sum(1 for s in expected_sections if s in sm)
        sub += (sections_found / len(expected_sections)) * 5
        total += sub
    return total / len(scored)


def score_dedup(predictions: list[dict]) -> float:
    """Check dedup: skillsUsed + ground_truth=NO should predict NO; skillsUsed + ground_truth=UPDATE should predict UPDATE (not YES)."""
    dedup_cases = [p for p in predictions if p["data"].get("skillsUsed")]
    if not dedup_cases:
        return 8.0  # Default good if no dedup cases
    correct = 0
    for p in dedup_cases:
        if p["ground_truth"] == "NO" and p["predicted"] == "NO":
            correct += 1  # Correctly rejected despite using a skill
        elif p["ground_truth"] == "UPDATE" and p["predicted"] == "UPDATE":
            correct += 1  # Correctly identified as update, not new skill
        elif p["ground_truth"] == "UPDATE" and p["predicted"] == "NO":
            correct += 0.5  # Conservative but acceptable
    return (correct / len(dedup_cases)) * 10


def score_robustness(predictions: list[dict]) -> float:
    """Output format parse success rate (0-10 scale)."""
    parseable = [p for p in predictions if p.get("result")]
    if not parseable:
        return 5.0
    success = 0
    for p in parseable:
        result = p["result"]
        pred = p["predicted"]
        if pred == "NO":
            # Should contain NO_SKILL or NO_UPDATE
            if "NO_SKILL" in result or "NO_UPDATE" in result:
                success += 1
        elif pred in ("YES", "UPDATE"):
            # Should have parseable eval_json
            if extract_eval_json(result):
                success += 1
        else:
            success += 1
    return (success / len(parseable)) * 10


def compute_scores(predictions: list[dict]) -> dict:
    """Compute all 6 scoring dimensions and weighted total."""
    raw = {
        "accuracy": score_accuracy(predictions),
        "precision": score_precision(predictions),
        "recall": score_recall(predictions),
        "quality": score_quality(predictions),
        "dedup": score_dedup(predictions),
        "robustness": score_robustness(predictions),
    }
    weighted_total = sum(raw[k] * WEIGHTS[k] / 10 for k in raw)
    return {"raw": raw, "total": round(weighted_total, 1)}


# ─── Run Benchmark ───────────────────────────────────────────────────────────
def run_benchmark(version: str = "v1_baseline", dry_run: bool = False, verbose: bool = False) -> dict:
    """Run the full benchmark: load cases, evaluate, score."""
    print(f"\n{'='*60}")
    print(f"  Darwin Eval Benchmark — Prompt: {version}")
    print(f"  Mode: {'DRY RUN (cached)' if dry_run else 'LIVE (Gemini API)'}")
    print(f"{'='*60}\n")

    # Load prompt module
    prompt_mod = load_prompt_module(version)
    existing_summary = get_existing_skills_summary()

    # Load test cases
    cases = load_test_cases()
    print(f"Loaded {len(cases)} test cases:")
    for gt in ["YES", "NO", "UPDATE"]:
        n = sum(1 for c in cases if c["ground_truth"] == gt)
        print(f"  {gt}: {n}")
    print()

    # Evaluate each case
    predictions = []
    for i, case in enumerate(cases, 1):
        data = case["data"]
        gt = case["ground_truth"]
        fname = case["file"]

        # Check cache first
        cached = load_cached_result(version, fname)
        if cached and dry_run:
            result = cached
            source = "cache"
        elif cached and not dry_run:
            # Re-evaluate even with cache in live mode
            result = None
            source = "api"
        else:
            result = None
            source = "api"

        if result is None:
            if dry_run:
                print(f"  [{i}/{len(cases)}] {fname}: SKIP (no cache, dry-run mode)")
                predictions.append({
                    "file": fname, "ground_truth": gt, "predicted": "SKIP",
                    "result": None, "data": data,
                })
                continue

            # Build prompt based on ground truth type
            if gt == "UPDATE" or (data.get("relatedSkill") and gt != "NO"):
                skill_name = data.get("relatedSkill") or data.get("skillName", "unknown")
                skill_content = load_skill_content(skill_name)
                if skill_content:
                    prompt = prompt_mod.build_update_skill_prompt(data, skill_name, skill_content)
                else:
                    prompt = prompt_mod.build_new_skill_prompt(data, existing_summary)
            else:
                prompt = prompt_mod.build_new_skill_prompt(data, existing_summary)

            result = call_gemini(prompt)
            if result:
                save_cached_result(version, fname, result)
                source = "api"
            else:
                print(f"  [{i}/{len(cases)}] {fname}: ERROR (Gemini call failed)")
                predictions.append({
                    "file": fname, "ground_truth": gt, "predicted": "ERROR",
                    "result": None, "data": data,
                })
                continue

        predicted = classify_result(result)
        match = "✓" if predicted == gt else "✗"
        print(f"  [{i}/{len(cases)}] {fname}: {gt} → {predicted} {match} ({source})")

        if verbose and predicted != gt:
            print(f"         Result preview: {result[:200].replace(chr(10), ' ')}")

        predictions.append({
            "file": fname, "ground_truth": gt, "predicted": predicted,
            "result": result, "data": data,
        })

    # Score
    valid = [p for p in predictions if p["predicted"] not in ("SKIP", "ERROR")]
    scores = compute_scores(valid)

    # Display results
    print(f"\n{'─'*60}")
    print(f"  SCORES (prompt: {version})")
    print(f"{'─'*60}")
    for dim, raw_score in scores["raw"].items():
        weighted = raw_score * WEIGHTS[dim] / 10
        bar = "█" * int(raw_score) + "░" * (10 - int(raw_score))
        print(f"  {dim:12s}  {bar}  {raw_score:.1f}/10  (×{WEIGHTS[dim]:2d} = {weighted:.1f})")
    print(f"{'─'*60}")
    print(f"  TOTAL: {scores['total']}/100")
    print(f"{'─'*60}\n")

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_file = RESULTS_DIR / f"{version}-{timestamp}.json"
    result_file.write_text(json.dumps({
        "version": version,
        "timestamp": timestamp,
        "dry_run": dry_run,
        "total_cases": len(cases),
        "evaluated": len(valid),
        "scores": scores,
        "predictions": [
            {"file": p["file"], "ground_truth": p["ground_truth"], "predicted": p["predicted"]}
            for p in predictions
        ],
    }, indent=2, ensure_ascii=False))
    print(f"Results saved: {result_file.relative_to(SCRIPT_DIR)}")

    # Append to TSV (darwin-skill compatible)
    tsv_file = RESULTS_DIR / "results.tsv"
    header = "timestamp\tversion\ttotal_score\taccuracy\tprecision\trecall\tquality\tdedup\trobustness\tn_cases\n"
    if not tsv_file.exists():
        tsv_file.write_text(header)
    with open(tsv_file, "a") as f:
        row = [timestamp, version, str(scores["total"])]
        row += [f"{scores['raw'][d]:.1f}" for d in WEIGHTS]
        row += [str(len(valid))]
        f.write("\t".join(row) + "\n")
    print(f"TSV appended: {tsv_file.relative_to(SCRIPT_DIR)}")

    return scores


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    version = "v1_baseline"
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv

    for arg in sys.argv[1:]:
        if arg.startswith("--prompt"):
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                version = sys.argv[idx + 1]

    run_benchmark(version=version, dry_run=dry_run, verbose=verbose)
