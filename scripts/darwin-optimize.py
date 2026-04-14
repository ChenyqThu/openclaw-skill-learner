#!/usr/bin/env python3
"""
Darwin Optimizer — Hill-climbing loop for Gemini prompt optimization.

Inspired by Karpathy's autoresearch and darwin-skill's ratchet mechanism.
Iteratively improves the evaluation prompt by:
  1. Running baseline benchmark
  2. Diagnosing the weakest scoring dimension
  3. Applying a targeted prompt edit
  4. Re-running benchmark
  5. Keeping improvement or reverting (ratchet: scores only go up)

Usage:
  python3 darwin-optimize.py                    # Full optimization loop
  python3 darwin-optimize.py --max-rounds 3     # Limit rounds
  python3 darwin-optimize.py --dry-run           # Preview without API calls
  python3 darwin-optimize.py --baseline-only     # Just run baseline, no optimization
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from gemini_client import load_env as _load_env

_load_env()

SCRIPT_DIR = Path(__file__).parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
RESULTS_DIR = SCRIPT_DIR / "darwin-results"

MAX_ROUNDS = 5
GEMINI_MODEL = "gemini-3-flash-preview"  # Same model as evaluator

# ─── Dimension Diagnostics ───────────────────────────────────────────────────
# Maps each scoring dimension to the prompt sections that influence it,
# and provides optimization strategies.

DIMENSION_STRATEGIES = {
    "accuracy": {
        "prompt_sections": ["QUALIFICATION CRITERIA", "RED FLAGS", "EXAMPLES"],
        "strategies": [
            "Add more discriminating examples (1 YES + 1 NO) showing boundary cases",
            "Sharpen the A+B criteria with more specific negative examples",
            "Add explicit decision tree: check A → check B → check C/D/E → decide",
        ],
    },
    "precision": {
        "prompt_sections": ["RED FLAGS", "WHAT IS AN OPENCLAW SKILL"],
        "strategies": [
            "Strengthen RED FLAGS section with patterns from false positives",
            "Add anti-pattern: routine cron execution without novel discovery is NOT a skill",
            "Require the reasoning to explicitly cite evidence for criterion C, D, or E",
        ],
    },
    "recall": {
        "prompt_sections": ["Trial-and-error signals", "QUALIFICATION CRITERIA"],
        "strategies": [
            "Expand trial-and-error signal list with more Chinese keywords",
            "Lower the evidence threshold: subtle course corrections also count",
            "Add criterion: if agent used ≥3 different tool types in novel combination → consider",
        ],
    },
    "quality": {
        "prompt_sections": ["eval_json", "skill_md"],
        "strategies": [
            "Add explicit field validation rules in the output template",
            "Require problem_context to be ≥20 chars and mention the core challenge",
            "Add a completeness checklist the model must verify before outputting",
        ],
    },
    "dedup": {
        "prompt_sections": ["EXISTING SKILLS"],
        "strategies": [
            "Emphasize: if an existing skill covers >70% of this pattern → NO_SKILL",
            "Add instruction: compare each existing skill name/description before deciding",
            "Require explicit dedup reasoning: 'Checked against [skill X], this differs because...'",
        ],
    },
    "robustness": {
        "prompt_sections": ["INSTRUCTIONS", "output format"],
        "strategies": [
            "Add format validation reminder: 'Ensure eval_json is valid JSON'",
            "Simplify output structure to reduce parse failures",
            "Add fallback instruction: if unsure about format, output NO_SKILL",
        ],
    },
}


# ─── Gemini Meta-Optimizer ───────────────────────────────────────────────────
def call_gemini_meta(prompt: str) -> str | None:
    """Call Gemini for meta-optimization (improving the prompt itself)."""
    from gemini_client import call_gemini
    return call_gemini(prompt, model=GEMINI_MODEL, temperature=0.3, max_tokens=8192)


# ─── Benchmark Runner ────────────────────────────────────────────────────────
def run_benchmark(version: str, dry_run: bool = False) -> dict | None:
    """Run eval-benchmark.py and return scores."""
    # Import and run directly for better integration
    import importlib.util
    spec = importlib.util.spec_from_file_location("bench", str(SCRIPT_DIR / "eval-benchmark.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run_benchmark(version=version, dry_run=dry_run)


# ─── Prompt Editor ───────────────────────────────────────────────────────────
def read_current_prompt(version: str) -> str:
    """Read the current prompt module source code."""
    return (PROMPTS_DIR / f"{version}.py").read_text()


def write_new_prompt_version(version: str, content: str):
    """Write a new prompt version file."""
    (PROMPTS_DIR / f"{version}.py").write_text(content)


def generate_improved_prompt(
    current_prompt_source: str,
    weakest_dim: str,
    dim_score: float,
    scores: dict,
    predictions_summary: str,
    round_num: int,
) -> str | None:
    """Use Gemini to generate an improved version of the prompt."""
    strategies = DIMENSION_STRATEGIES.get(weakest_dim, {})
    strategy_options = "\n".join(f"  - {s}" for s in strategies.get("strategies", []))
    target_sections = ", ".join(strategies.get("prompt_sections", []))

    meta_prompt = f"""You are a prompt engineering expert optimizing a Gemini evaluation prompt.

TASK: Improve the prompt to increase the "{weakest_dim}" score (currently {dim_score:.1f}/10).

CURRENT SCORES:
{json.dumps(scores['raw'], indent=2)}
Total: {scores['total']}/100

WEAKEST DIMENSION: {weakest_dim}
TARGET PROMPT SECTIONS: {target_sections}

POSSIBLE STRATEGIES:
{strategy_options}

PREDICTION ERRORS (cases where the prompt got it wrong):
{predictions_summary}

CURRENT PROMPT SOURCE CODE:
```python
{current_prompt_source}
```

━━━ INSTRUCTIONS ━━━

1. Analyze the prediction errors to understand WHY the prompt fails on the weakest dimension.
2. Choose ONE specific strategy from the list above (or devise a better one).
3. Make a MINIMAL, targeted edit to the prompt — change only what affects the weakest dimension.
4. Do NOT change the output format structure (eval_json, skill_md blocks must stay the same).
5. Do NOT change function signatures or the module structure.
6. Keep all Chinese language requirements intact.

Output the COMPLETE modified Python file (not just the diff).
Wrap it in ```python ... ``` fences.

IMPORTANT: Make only ONE change. If you change too much, we can't attribute score changes."""

    result = call_gemini_meta(meta_prompt)
    if not result:
        return None

    # Extract Python code from response
    m = re.search(r'```python\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        return m.group(1)
    return None


def get_prediction_errors(version: str) -> str:
    """Load latest benchmark results and format prediction errors."""
    results_files = sorted(RESULTS_DIR.glob(f"{version}-*.json"), reverse=True)
    if not results_files:
        return "(no results available)"

    data = json.loads(results_files[0].read_text())
    errors = []
    for p in data.get("predictions", []):
        if p["predicted"] != p["ground_truth"] and p["predicted"] not in ("SKIP", "ERROR"):
            errors.append(f"  {p['file']}: expected {p['ground_truth']}, got {p['predicted']}")
    return "\n".join(errors) if errors else "(all correct)"


# ─── Git Operations ──────────────────────────────────────────────────────────
def git_commit(message: str):
    """Create a git commit with current changes."""
    subprocess.run(["git", "add", "scripts/prompts/"], cwd=SCRIPT_DIR.parent, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=SCRIPT_DIR.parent, capture_output=True,
    )


def git_revert():
    """Revert the last commit (ratchet: reject regression)."""
    subprocess.run(
        ["git", "revert", "HEAD", "--no-edit"],
        cwd=SCRIPT_DIR.parent, capture_output=True,
    )


# ─── Main Loop ───────────────────────────────────────────────────────────────
def main():
    dry_run = "--dry-run" in sys.argv
    baseline_only = "--baseline-only" in sys.argv
    max_rounds = MAX_ROUNDS

    for arg in sys.argv[1:]:
        if arg.startswith("--max-rounds"):
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                max_rounds = int(sys.argv[idx + 1])

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Darwin Optimizer — Prompt Hill-Climbing                ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # ─── Phase 0: Setup ──────────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    branch_name = f"auto-optimize/prompt-{timestamp}"

    # Create optimization branch (skip in dry-run)
    if not dry_run:
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=SCRIPT_DIR.parent, capture_output=True,
        )
        print(f"Created branch: {branch_name}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ─── Phase 1: Baseline ───────────────────────────────────────────────────
    print("\n── Phase 1: Baseline Evaluation ──")
    baseline_scores = run_benchmark("v1_baseline", dry_run=dry_run)
    if not baseline_scores:
        print("ERROR: Baseline evaluation failed")
        return

    best_scores = baseline_scores
    best_version = "v1_baseline"
    current_version = "v1_baseline"

    print(f"\nBaseline total: {best_scores['total']}/100")

    if baseline_only:
        print("\n── Baseline only mode, stopping. ──")
        return

    # ─── Phase 2: Optimization Loop ──────────────────────────────────────────
    print(f"\n── Phase 2: Optimization Loop (max {max_rounds} rounds) ──")

    for round_num in range(1, max_rounds + 1):
        print(f"\n{'━'*60}")
        print(f"  Round {round_num}/{max_rounds}")
        print(f"{'━'*60}")

        # Step 1: Diagnose weakest dimension
        raw = best_scores["raw"]
        weakest_dim = min(raw, key=raw.get)
        weakest_score = raw[weakest_dim]
        print(f"  Weakest dimension: {weakest_dim} ({weakest_score:.1f}/10)")

        if weakest_score >= 9.0:
            print(f"  All dimensions ≥ 9.0, optimization plateau reached.")
            break

        # Step 2: Generate improved prompt
        print(f"  Generating improved prompt...")
        current_source = read_current_prompt(current_version)
        errors_summary = get_prediction_errors(current_version)

        new_source = generate_improved_prompt(
            current_source, weakest_dim, weakest_score,
            best_scores, errors_summary, round_num,
        )

        if not new_source:
            print(f"  ERROR: Failed to generate improvement. Stopping.")
            break

        # Step 3: Write new version
        new_version = f"v{round_num + 1}_r{round_num}"
        write_new_prompt_version(new_version, new_source)
        print(f"  Written: prompts/{new_version}.py")

        # Verify the new module loads
        try:
            from importlib import import_module
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"prompts.{new_version}", PROMPTS_DIR / f"{new_version}.py"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            assert hasattr(mod, "build_new_skill_prompt")
            assert hasattr(mod, "build_update_skill_prompt")
        except Exception as e:
            print(f"  ERROR: New prompt module failed to load: {e}")
            print(f"  Skipping this round.")
            (PROMPTS_DIR / f"{new_version}.py").unlink(missing_ok=True)
            continue

        # Step 4: Re-evaluate
        print(f"  Evaluating {new_version}...")
        new_scores = run_benchmark(new_version, dry_run=dry_run)
        if not new_scores:
            print(f"  ERROR: Evaluation failed. Skipping.")
            (PROMPTS_DIR / f"{new_version}.py").unlink(missing_ok=True)
            continue

        # Step 5: Ratchet decision
        delta = new_scores["total"] - best_scores["total"]
        if delta > 0:
            print(f"\n  ✓ KEEP: {best_scores['total']} → {new_scores['total']} (+{delta:.1f})")
            if not dry_run:
                git_commit(f"optimize prompt: {weakest_dim} +{delta:.1f} (round {round_num})")
            best_scores = new_scores
            best_version = new_version
            current_version = new_version
        else:
            print(f"\n  ✗ REVERT: {best_scores['total']} → {new_scores['total']} ({delta:+.1f})")
            # Remove failed version
            (PROMPTS_DIR / f"{new_version}.py").unlink(missing_ok=True)
            if not dry_run:
                # Clean up cache for failed version
                cache_dir = RESULTS_DIR / "cache" / new_version
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
            # Continue with current best
            print(f"  Plateau on {weakest_dim}, trying next dimension...")

            # Try second-weakest dimension
            sorted_dims = sorted(raw, key=raw.get)
            if len(sorted_dims) > 1:
                alt_dim = sorted_dims[1]
                print(f"  Attempting {alt_dim} ({raw[alt_dim]:.1f}/10) instead...")
                # For simplicity, we'll let the next round pick it up
                # by continuing the loop (the weakest may have changed)

    # ─── Phase 3: Summary ────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  OPTIMIZATION COMPLETE")
    print(f"{'═'*60}")
    print(f"  Baseline:  {baseline_scores['total']}/100")
    print(f"  Final:     {best_scores['total']}/100")
    delta = best_scores["total"] - baseline_scores["total"]
    print(f"  Improvement: {delta:+.1f}")
    print(f"  Best version: {best_version}")
    print()

    # Show dimension comparison
    dims = list(baseline_scores["raw"].keys())
    print(f"  {'Dimension':<14s} {'Baseline':>10s} {'Final':>10s} {'Delta':>10s}")
    print(f"  {'─'*44}")
    for dim in dims:
        b = baseline_scores["raw"].get(dim, 0)
        f = best_scores["raw"].get(dim, 0)
        d = f - b
        marker = "↑" if d > 0 else ("↓" if d < 0 else " ")
        print(f"  {dim:<14s} {b:>9.1f} {f:>9.1f} {d:>+9.1f} {marker}")
    print()

    if not dry_run and best_version != "v1_baseline":
        print(f"  Optimized prompt on branch: {branch_name}")
        print(f"  Review with: git diff main...{branch_name} -- scripts/prompts/")
    elif best_version == "v1_baseline":
        print(f"  No improvements found. Baseline prompt is already optimal for current test set.")


if __name__ == "__main__":
    main()
