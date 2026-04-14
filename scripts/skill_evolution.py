#!/usr/bin/env python3
"""
skill_evolution.py — Darwin-style SKILL.md evolution engine.

Fuses darwin-skill's 8-dimension rubric with skill-learner's infrastructure.
Triggered by friction signals from the plugin or manual invocation.

Core loop: evaluate → diagnose weakest dimension → improve → re-evaluate → keep/revert

Usage:
  python3 skill_evolution.py --skill <name>                    # Evolve one skill
  python3 skill_evolution.py --skill <name> --dry-run          # Preview only
  python3 skill_evolution.py --skill <name> --max-rounds 5     # Custom rounds
  python3 skill_evolution.py --batch                           # Scan for accumulated friction
  python3 skill_evolution.py --list                            # List eligible skills
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gemini_client import load_env, call_gemini

load_env()

# ─── Paths ───────────────────────────────────────────────────────────────────
WORKSPACE = Path.home() / ".openclaw/workspace"
SKILLS_DIR = WORKSPACE / "skills"
SCRIPT_DIR = Path(__file__).parent
RESULTS_DIR = SCRIPT_DIR / "darwin-results"
EVOLUTION_TSV = RESULTS_DIR / "evolution-results.tsv"
FRICTION_LOG = WORKSPACE / "data/skill-learner/friction-signals.json"

# ─── Safety: files that MUST NEVER be modified by evolution ──────────────────
BLOCKED_FILES = {"SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md"}
BLOCKED_PATHS = {"auto-learned"}

# ─── 8-Dimension Scoring Rubric (from darwin-skill) ─────────────────────────
# Structure (60 points): static analysis of SKILL.md content
# Effectiveness (40 points): simulated test-prompt execution
DIMENSIONS = {
    # Structure dimensions (60 total)
    "frontmatter":       {"weight": 8,  "category": "structure", "description": "Name convention, description includes purpose+when+triggers"},
    "workflow_clarity":   {"weight": 15, "category": "structure", "description": "Clear numbered steps, explicit inputs/outputs per step"},
    "edge_case_coverage": {"weight": 10, "category": "structure", "description": "Exception handling, fallback paths, error recovery"},
    "checkpoint_design":  {"weight": 7,  "category": "structure", "description": "User confirmations before critical decisions"},
    "instruction_specificity": {"weight": 15, "category": "structure", "description": "Non-ambiguous, concrete parameters/formats/examples"},
    "resource_integration": {"weight": 5, "category": "structure", "description": "References/scripts/assets correctly linked"},
    # Effectiveness dimensions (40 total)
    "architecture":       {"weight": 15, "category": "effectiveness", "description": "Clear hierarchy, no redundancy, consistency with ecosystem"},
    "test_performance":   {"weight": 25, "category": "effectiveness", "description": "Test with prompts; compare output quality vs. without skill"},
}

MAX_ROUNDS = 3
GEMINI_MODEL = "gemini-3-flash-preview"


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class EvolutionScore:
    raw_dims: dict = field(default_factory=dict)  # dim_name → 1-10 score
    total: float = 0.0

    def compute_total(self):
        self.total = sum(
            self.raw_dims.get(d, 5) * DIMENSIONS[d]["weight"] / 10
            for d in DIMENSIONS
        )
        self.total = round(self.total, 1)
        return self.total


@dataclass
class EvolutionResult:
    skill_name: str
    before_score: float
    after_score: float
    rounds: int
    commits: list = field(default_factory=list)
    status: str = "unchanged"  # improved, unchanged, reverted
    weakest_dim: str = ""
    change_summary: str = ""


# ─── Git Operations ─────────────────────────────────────────────────────────

def git_run(*args, cwd=None) -> subprocess.CompletedProcess:
    """Run a git command in the workspace."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd or WORKSPACE),
        capture_output=True, text=True,
    )


def git_is_initialized() -> bool:
    """Check if workspace has git with at least one commit."""
    r = git_run("rev-parse", "HEAD")
    return r.returncode == 0


def git_create_branch(branch_name: str) -> bool:
    r = git_run("checkout", "-b", branch_name)
    return r.returncode == 0


def git_current_branch() -> str:
    r = git_run("branch", "--show-current")
    return r.stdout.strip()


def git_commit(skill_name: str, dimension: str, delta: float, round_num: int) -> str | None:
    """Stage SKILL.md changes and commit. Returns commit hash or None."""
    skill_path = f"skills/{skill_name}/SKILL.md"
    git_run("add", skill_path)
    msg = f"evolve({skill_name}): {dimension} +{delta:.1f} (round {round_num})"
    r = git_run("commit", "-m", msg)
    if r.returncode != 0:
        print(f"  ERROR: git commit failed: {r.stderr.strip()}")
        return None
    # Get commit hash
    r2 = git_run("rev-parse", "HEAD")
    return r2.stdout.strip()[:7]


def git_revert() -> bool:
    """Revert the last commit (safe: creates new revert commit)."""
    r = git_run("revert", "HEAD", "--no-edit")
    return r.returncode == 0


def git_diff_summary(skill_name: str) -> str:
    """Get a compact diff summary for the skill."""
    r = git_run("diff", "HEAD~1", "--stat", f"skills/{skill_name}/SKILL.md")
    return r.stdout.strip() if r.returncode == 0 else "(no diff)"


# ─── Skill Validation ───────────────────────────────────────────────────────

def validate_skill(skill_name: str) -> str | None:
    """Validate skill is eligible for evolution. Returns error or None."""
    if skill_name in BLOCKED_FILES:
        return f"Blocked: {skill_name} is a core spec file"
    for blocked in BLOCKED_PATHS:
        if blocked in skill_name:
            return f"Blocked: path contains '{blocked}'"

    skill_dir = SKILLS_DIR / skill_name
    if not skill_dir.exists():
        return f"Skill not found: {skill_dir}"

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return f"SKILL.md not found in {skill_dir}"

    # Check it's not in auto-learned
    if "auto-learned" in str(skill_dir):
        return "Cannot evolve auto-learned drafts (must be approved first)"

    return None


def list_eligible_skills() -> list[str]:
    """List all skills eligible for evolution."""
    eligible = []
    if not SKILLS_DIR.exists():
        return eligible
    for d in sorted(SKILLS_DIR.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            if validate_skill(d.name) is None:
                eligible.append(d.name)
    return eligible


# ─── Test Prompts ────────────────────────────────────────────────────────────

def load_test_prompts(skill_name: str) -> list[dict] | None:
    """Load test-prompts.json for a skill, or None if not found."""
    tp = SKILLS_DIR / skill_name / "test-prompts.json"
    if tp.exists():
        try:
            return json.loads(tp.read_text())
        except Exception:
            return None
    return None


def generate_test_prompts(skill_name: str, skill_content: str) -> list[dict]:
    """Generate test prompts using Gemini if none exist."""
    prompt = f"""You are creating test prompts for evaluating an AI agent skill.

Read this SKILL.md and generate 2-3 test prompts that would exercise it:

```
{skill_content[:4000]}
```

For each test prompt, provide:
1. A realistic user request that would trigger this skill
2. What a good response should include (key elements)

Output as JSON array:
```json
[
  {{"id": 1, "prompt": "user says...", "expected": "response should include..."}},
  {{"id": 2, "prompt": "...", "expected": "..."}}
]
```

Write prompts in the same language as the skill (Chinese if skill is in Chinese).
Output ONLY the JSON array, no other text."""

    result = call_gemini(prompt, temperature=0.3)
    if not result:
        return _default_test_prompts(skill_name)

    # Extract JSON
    import re
    m = re.search(r'\[.*\]', result, re.DOTALL)
    if m:
        try:
            prompts = json.loads(m.group(0))
            if isinstance(prompts, list) and len(prompts) >= 1:
                # Save for future use
                tp_file = SKILLS_DIR / skill_name / "test-prompts.json"
                tp_file.write_text(json.dumps(prompts, indent=2, ensure_ascii=False))
                print(f"  Generated {len(prompts)} test prompts → {tp_file.name}")
                return prompts
        except Exception:
            pass

    return _default_test_prompts(skill_name)


def _default_test_prompts(skill_name: str) -> list[dict]:
    """Fallback: generate minimal test prompts."""
    return [
        {"id": 1, "prompt": f"Execute the {skill_name} skill on a typical scenario", "expected": "Should follow the skill's procedure steps"},
        {"id": 2, "prompt": f"Apply {skill_name} to an edge case with errors", "expected": "Should handle errors per the skill's pitfall section"},
    ]


# ─── 8-Dimension Evaluator ──────────────────────────────────────────────────

def evaluate_skill(skill_name: str, skill_content: str, test_prompts: list[dict]) -> EvolutionScore:
    """Score a skill across all 8 dimensions using Gemini."""
    test_prompts_text = json.dumps(test_prompts[:3], ensure_ascii=False, indent=2)

    prompt = f"""You are evaluating an AI agent skill (SKILL.md) across 8 dimensions.
Score each dimension 1-10 (integer).

SKILL.md content:
```
{skill_content[:6000]}
```

TEST PROMPTS (for effectiveness evaluation):
{test_prompts_text}

━━━ 8 DIMENSIONS ━━━

STRUCTURE (60 points total):
1. frontmatter (weight 8): Name convention, description includes purpose+when+triggers, ≤1024 chars
2. workflow_clarity (weight 15): Clear numbered steps, explicit inputs/outputs per step
3. edge_case_coverage (weight 10): Exception handling, fallback paths, error recovery documented
4. checkpoint_design (weight 7): User confirmations before critical decisions, safety gates
5. instruction_specificity (weight 15): Non-ambiguous, concrete parameters/formats/examples, directly executable
6. resource_integration (weight 5): References/scripts/assets correctly linked and accessible

EFFECTIVENESS (40 points total):
7. architecture (weight 15): Clear hierarchy, no redundancy, consistency with ecosystem
8. test_performance (weight 25): Given the test prompts above, would an agent following this skill
   produce high-quality results? Score based on: clarity of guidance, completeness of steps,
   handling of edge cases in test scenarios.

━━━ OUTPUT ━━━
Output ONLY a JSON object with dimension scores (1-10 each):

```json
{{
  "frontmatter": <1-10>,
  "workflow_clarity": <1-10>,
  "edge_case_coverage": <1-10>,
  "checkpoint_design": <1-10>,
  "instruction_specificity": <1-10>,
  "resource_integration": <1-10>,
  "architecture": <1-10>,
  "test_performance": <1-10>,
  "reasoning": "<brief 1-2 sentence rationale for weakest dimension>"
}}
```"""

    result = call_gemini(prompt, temperature=0.1)
    if not result:
        return EvolutionScore()

    import re
    m = re.search(r'\{.*\}', result, re.DOTALL)
    if not m:
        return EvolutionScore()

    try:
        data = json.loads(m.group(0))
        score = EvolutionScore()
        for dim in DIMENSIONS:
            val = data.get(dim, 5)
            score.raw_dims[dim] = max(1, min(10, int(val)))
        score.compute_total()
        return score
    except Exception:
        return EvolutionScore()


# ─── Improvement Generator ──────────────────────────────────────────────────

# Optimization strategies per dimension (from darwin-skill)
DIMENSION_STRATEGIES = {
    "frontmatter": [
        "Ensure description includes: purpose, when to trigger, key keywords",
        "Add tags array if missing",
        "Keep description under 1024 chars but informative",
    ],
    "workflow_clarity": [
        "Add numbered steps if currently prose",
        "Add explicit input/output for each step",
        "Break complex steps into substeps",
    ],
    "edge_case_coverage": [
        "Add '已知雷区' section with specific failure modes",
        "Add fallback paths for common errors",
        "Document what to do when prerequisites fail",
    ],
    "checkpoint_design": [
        "Add user confirmation before destructive or irreversible operations",
        "Add decision points where user input changes the approach",
    ],
    "instruction_specificity": [
        "Replace vague verbs with specific tool calls and parameters",
        "Add concrete examples with actual file paths, commands, or API calls",
        "Add format templates for expected outputs",
    ],
    "resource_integration": [
        "Link referenced scripts/tools with actual paths",
        "Verify all mentioned resources exist",
    ],
    "architecture": [
        "Remove duplicate sections or redundant instructions",
        "Ensure consistent structure with other skills in the ecosystem",
        "Add clear section hierarchy (## for major, ### for sub)",
    ],
    "test_performance": [
        "Add more concrete examples in the skill",
        "Clarify ambiguous instructions that lead to poor test results",
        "Add explicit success criteria so the agent can self-verify",
    ],
}


def generate_improvement(
    skill_content: str,
    weakest_dim: str,
    dim_score: int,
    scores: EvolutionScore,
    skill_name: str,
) -> str | None:
    """Generate an improved SKILL.md targeting the weakest dimension."""
    strategies = DIMENSION_STRATEGIES.get(weakest_dim, ["Improve this dimension"])
    strategy_text = "\n".join(f"  - {s}" for s in strategies)
    dim_info = DIMENSIONS[weakest_dim]

    prompt = f"""You are optimizing an AI agent skill (SKILL.md) to improve one specific dimension.

CURRENT SKILL ({skill_name}):
```
{skill_content[:6000]}
```

CURRENT SCORES:
{json.dumps(scores.raw_dims, indent=2)}
Total: {scores.total}/100

TARGET: Improve "{weakest_dim}" (currently {dim_score}/10)
Dimension definition: {dim_info['description']}

STRATEGIES:
{strategy_text}

━━━ RULES ━━━
1. Make ONE targeted improvement to the weakest dimension
2. Do NOT change the skill's core purpose or function
3. Do NOT add new external dependencies
4. Keep the same YAML frontmatter structure
5. Keep file size reasonable (optimized ≤ 150% of original)
6. Preserve the language style (Chinese-first if original is Chinese)
7. Do NOT remove existing content that scores well on other dimensions

Output the COMPLETE improved SKILL.md file (not just the diff).
Wrap it in ```skill_md ... ``` fences."""

    result = call_gemini(prompt, temperature=0.3, max_tokens=8192)
    if not result:
        return None

    import re
    m = re.search(r'```skill_md\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Fallback: try markdown fences
    m = re.search(r'```(?:markdown)?\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        content = m.group(1).strip()
        if content.startswith("---"):
            return content

    return None


# ─── Evolution Loop ─────────────────────────────────────────────────────────

class SkillEvolver:
    """Core evolution engine for a single SKILL.md."""

    def __init__(self, skill_name: str, max_rounds: int = MAX_ROUNDS, dry_run: bool = False):
        self.skill_name = skill_name
        self.max_rounds = max_rounds
        self.dry_run = dry_run
        self.skill_dir = SKILLS_DIR / skill_name
        self.skill_md = self.skill_dir / "SKILL.md"
        self.branch_name = f"auto-evolve/{skill_name}-{datetime.now().strftime('%Y%m%d-%H%M')}"

    def setup(self) -> str | None:
        """Validate and prepare for evolution. Returns error or None."""
        err = validate_skill(self.skill_name)
        if err:
            return err

        if not git_is_initialized():
            return "Workspace git not initialized. Run: bash scripts/init-workspace-git.sh"

        if not self.dry_run:
            if not git_create_branch(self.branch_name):
                return f"Failed to create branch: {self.branch_name}"
            print(f"  Created branch: {self.branch_name}")

        return None

    def evolve(self) -> EvolutionResult:
        """Run the full evolution loop. Returns EvolutionResult."""
        skill_content = self.skill_md.read_text()

        # Load or generate test prompts
        test_prompts = load_test_prompts(self.skill_name)
        if not test_prompts:
            print(f"  No test-prompts.json found, generating...")
            test_prompts = generate_test_prompts(self.skill_name, skill_content)

        # Phase 1: Baseline evaluation
        print(f"\n  ── Baseline Evaluation ──")
        baseline = evaluate_skill(self.skill_name, skill_content, test_prompts)
        if not baseline.raw_dims:
            return EvolutionResult(self.skill_name, 0, 0, 0, status="error",
                                   change_summary="Baseline evaluation failed")

        print(f"  Baseline: {baseline.total}/100")
        self._print_scores(baseline)

        best = baseline
        current_content = skill_content
        result = EvolutionResult(
            skill_name=self.skill_name,
            before_score=baseline.total,
            after_score=baseline.total,
            rounds=0,
        )

        # Phase 2: Optimization loop
        for round_num in range(1, self.max_rounds + 1):
            print(f"\n  ── Round {round_num}/{self.max_rounds} ──")

            # Find weakest dimension
            weakest_dim = min(best.raw_dims, key=best.raw_dims.get)
            weakest_score = best.raw_dims[weakest_dim]
            print(f"  Weakest: {weakest_dim} ({weakest_score}/10)")

            if weakest_score >= 9:
                print(f"  All dimensions ≥ 9, plateau reached.")
                break

            # Generate improvement
            print(f"  Generating improvement...")
            improved = generate_improvement(
                current_content, weakest_dim, weakest_score, best, self.skill_name
            )
            if not improved:
                print(f"  ERROR: Failed to generate improvement.")
                break

            # Validate improved content has frontmatter
            if not improved.strip().startswith("---"):
                print(f"  ERROR: Improved content missing frontmatter, skipping.")
                break

            # Write and evaluate
            if not self.dry_run:
                self.skill_md.write_text(improved)

            new_score = evaluate_skill(self.skill_name, improved, test_prompts)
            if not new_score.raw_dims:
                print(f"  ERROR: Re-evaluation failed.")
                if not self.dry_run:
                    self.skill_md.write_text(current_content)  # restore
                break

            delta = new_score.total - best.total
            result.rounds = round_num
            result.weakest_dim = weakest_dim

            if delta > 0:
                # KEEP: improvement
                print(f"  ✓ KEEP: {best.total} → {new_score.total} (+{delta:.1f})")
                self._print_scores(new_score)

                if not self.dry_run:
                    commit_hash = git_commit(self.skill_name, weakest_dim, delta, round_num)
                    if commit_hash:
                        result.commits.append(commit_hash)
                        self._log_tsv(commit_hash, best.total, new_score.total, "keep",
                                       weakest_dim, "evolution")

                best = new_score
                current_content = improved
                result.after_score = new_score.total
                result.status = "improved"
                result.change_summary = f"{weakest_dim} {weakest_score}→{new_score.raw_dims.get(weakest_dim, '?')}"
            else:
                # REVERT: regression
                print(f"  ✗ REVERT: {best.total} → {new_score.total} ({delta:+.1f})")

                if not self.dry_run:
                    self.skill_md.write_text(current_content)  # restore original
                    self._log_tsv("reverted", best.total, new_score.total, "revert",
                                   weakest_dim, "evolution")

                result.status = "reverted" if round_num == 1 else result.status
                break

        # Switch back to main branch
        if not self.dry_run:
            original_branch = "main"
            if result.status == "improved":
                # Merge evolution branch to main
                git_run("checkout", original_branch)
                merge_r = git_run("merge", self.branch_name, "--no-edit")
                if merge_r.returncode == 0:
                    print(f"\n  Merged {self.branch_name} → {original_branch}")
                    # Clean up branch
                    git_run("branch", "-d", self.branch_name)
                else:
                    print(f"\n  ⚠️ Merge failed, changes remain on branch: {self.branch_name}")
            else:
                # No improvements, clean up branch
                git_run("checkout", original_branch)
                git_run("branch", "-D", self.branch_name)

        return result

    def _print_scores(self, score: EvolutionScore):
        """Print score breakdown."""
        for dim, info in DIMENSIONS.items():
            raw = score.raw_dims.get(dim, 0)
            weighted = raw * info["weight"] / 10
            cat = "S" if info["category"] == "structure" else "E"
            bar = "█" * raw + "░" * (10 - raw)
            print(f"    [{cat}] {dim:24s} {bar} {raw}/10 (×{info['weight']:2d} = {weighted:.1f})")
        print(f"    {'─'*60}")
        print(f"    TOTAL: {score.total}/100")

    def _log_tsv(self, commit: str, old_score: float, new_score: float,
                  status: str, dimension: str, trigger: str):
        """Append evolution result to TSV."""
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        header = "timestamp\tcommit\tskill\told_score\tnew_score\tstatus\tdimension\ttrigger\teval_mode\n"
        if not EVOLUTION_TSV.exists():
            EVOLUTION_TSV.write_text(header)
        with open(EVOLUTION_TSV, "a") as f:
            row = [
                datetime.now().isoformat(),
                commit,
                self.skill_name,
                str(old_score),
                str(new_score),
                status,
                dimension,
                trigger,
                "dry_run" if self.dry_run else "full",
            ]
            f.write("\t".join(row) + "\n")


# ─── Batch Mode ──────────────────────────────────────────────────────────────

def run_batch():
    """Scan friction log for skills needing evolution."""
    if not FRICTION_LOG.exists():
        print("No friction signals logged yet.")
        return

    try:
        signals = json.loads(FRICTION_LOG.read_text())
    except Exception:
        print("Failed to parse friction log.")
        return

    # Aggregate friction by skill
    skill_friction = {}
    for entry in signals:
        skill = entry.get("frictionSkill")
        weight = entry.get("frictionWeight", 0)
        if skill and weight >= 4:
            skill_friction[skill] = skill_friction.get(skill, 0) + weight

    if not skill_friction:
        print("No skills with sufficient friction signals.")
        return

    print(f"Skills with friction signals:")
    for name, weight in sorted(skill_friction.items(), key=lambda x: -x[1]):
        err = validate_skill(name)
        status = "✓ eligible" if not err else f"✗ {err}"
        print(f"  {name}: weight={weight} ({status})")

    # Evolve eligible skills
    for name in sorted(skill_friction, key=skill_friction.get, reverse=True):
        if validate_skill(name) is None:
            print(f"\n{'='*60}")
            print(f"  Evolving: {name} (friction={skill_friction[name]})")
            print(f"{'='*60}")
            evolver = SkillEvolver(name)
            err = evolver.setup()
            if err:
                print(f"  Setup failed: {err}")
                continue
            result = evolver.evolve()
            print(f"\n  Result: {result.status} ({result.before_score} → {result.after_score})")


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Darwin-style SKILL.md evolution engine")
    parser.add_argument("--skill", help="Skill name to evolve")
    parser.add_argument("--max-rounds", type=int, default=MAX_ROUNDS, help=f"Max optimization rounds (default {MAX_ROUNDS})")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing changes")
    parser.add_argument("--batch", action="store_true", help="Scan friction log and evolve eligible skills")
    parser.add_argument("--list", action="store_true", help="List eligible skills")
    args = parser.parse_args()

    if args.list:
        skills = list_eligible_skills()
        print(f"Eligible skills for evolution ({len(skills)}):")
        for s in skills:
            tp = SKILLS_DIR / s / "test-prompts.json"
            tp_status = "✓ test-prompts" if tp.exists() else "✗ no test-prompts"
            print(f"  {s} ({tp_status})")
        return

    if args.batch:
        run_batch()
        return

    if not args.skill:
        parser.print_help()
        print("\nError: --skill or --batch required")
        sys.exit(1)

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Skill Evolution Engine — Darwin Ratchet                ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"\n  Skill: {args.skill}")
    print(f"  Max rounds: {args.max_rounds}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")

    evolver = SkillEvolver(args.skill, max_rounds=args.max_rounds, dry_run=args.dry_run)
    err = evolver.setup()
    if err:
        print(f"\n  ERROR: {err}")
        sys.exit(1)

    result = evolver.evolve()

    print(f"\n{'═'*60}")
    print(f"  EVOLUTION COMPLETE")
    print(f"{'═'*60}")
    print(f"  Skill:       {result.skill_name}")
    print(f"  Status:      {result.status}")
    print(f"  Before:      {result.before_score}/100")
    print(f"  After:       {result.after_score}/100")
    delta = result.after_score - result.before_score
    print(f"  Delta:       {delta:+.1f}")
    print(f"  Rounds:      {result.rounds}")
    print(f"  Commits:     {', '.join(result.commits) if result.commits else '(none)'}")
    if result.change_summary:
        print(f"  Changes:     {result.change_summary}")

    # Return result as JSON for server integration
    result_dict = {
        "skillName": result.skill_name,
        "status": result.status,
        "beforeScore": result.before_score,
        "afterScore": result.after_score,
        "delta": delta,
        "rounds": result.rounds,
        "commits": result.commits,
        "weakestDim": result.weakest_dim,
        "changeSummary": result.change_summary,
    }
    # Write result for server to pick up
    result_file = RESULTS_DIR / f"evolution-{result.skill_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(result_dict, indent=2, ensure_ascii=False))
    print(f"\n  Result file: {result_file.relative_to(SCRIPT_DIR)}")


if __name__ == "__main__":
    main()
