#!/usr/bin/env python3
"""
replay_gate.py — Phase D: Replay validation gate for skill drafts.

Usage (programmatic):
    from replay_gate import replay_skill
    verdict = replay_skill("resilient-multi-source-collection", source_request_id="1776...")
    if verdict.pass_rate >= 0.6 and verdict.skill_loaded_rate >= 0.5:
        # let the Feishu card fire
        ...

Usage (CLI, for manual testing):
    python3 replay_gate.py --skill resilient-multi-source-collection \\
        --source-request 1776123-abc --output /tmp/replay-out.json

Gate semantics (docs/OPENCLAW_COOPERATION_PHASE2.md §D):
    1. Gemini generates 3-5 test prompts derived from the originating session.
    2. For each prompt, an ephemeral Jarvis instance runs with the draft skill
       installed in an isolated `skills_dir_override`.
    3. Trajectory comparator scores overlap vs. the skill's `operation_steps`.
    4. Gate OPEN when:
         - ≥50% of replays show the new SKILL.md was read (skill_loaded_rate)
         - ≥60% average tool-trajectory overlap (pass_rate)
    5. Otherwise, card does NOT fire — draft is held back with verdict metadata
       attached to `.replay.json` for user inspection.

Status (2026-04-17): This is the Phase D SKELETON. The HeadlessJarvisClient
below is stubbed (raises NotImplementedError) because OpenClaw doesn't yet
expose a headless mode — see docs/OPENCLAW_COOPERATION_PHASE2.md §D.
Use --dry-run for end-to-end testing without a real runner.

Migration paths when OpenClaw ships headless mode:
    - Plug your runner into `HeadlessJarvisClient.run()`
    - Flip the default of `use_runner` in `replay_skill` to True
    - Re-run benchmark: `python3 replay_gate.py --skill <x> --benchmark`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Lazy import to avoid pulling gemini_client at import time
_call_gemini = None
def _get_gemini():
    global _call_gemini
    if _call_gemini is None:
        from gemini_client import call_gemini, load_env
        load_env()  # Read ~/.openclaw/.env so GEMINI_API_KEY is available
        _call_gemini = call_gemini
    return _call_gemini


WORKSPACE = Path.home() / ".openclaw/workspace"
AUTO_LEARNED = WORKSPACE / "skills/auto-learned"
QUEUE_DIR = WORKSPACE / "data/skill-learner/analysis-queue"


# ─── Data classes ────────────────────────────────────────────────────────────
@dataclass
class TestPrompt:
    """One generated test prompt derived from the originating session."""
    prompt: str
    derived_from_turn: int | None = None
    expected_approach: str = ""


@dataclass
class ReplayRun:
    """Single execution of one test prompt in the ephemeral runner."""
    prompt: str
    skill_loaded: bool = False          # Was the draft SKILL.md read?
    tool_trajectory: list[str] = field(default_factory=list)
    overlap_score: float = 0.0          # 0-1
    error: str | None = None
    duration_ms: int = 0


@dataclass
class ReplayVerdict:
    """Aggregate decision for one skill."""
    skill_name: str
    test_prompts: list[TestPrompt]
    runs: list[ReplayRun]
    pass_rate: float                    # Mean overlap across runs
    skill_loaded_rate: float            # Fraction of runs that Read the skill
    gate_open: bool                     # Final decision
    dry_run: bool = False

    def summary_line(self) -> str:
        status = "PASS" if self.gate_open else "FAIL"
        return (
            f"[{status}] {self.skill_name}: "
            f"pass_rate={self.pass_rate:.2f}, loaded={self.skill_loaded_rate:.2f}, "
            f"runs={len(self.runs)}"
        )


# ─── Test prompt generation (Gemini) ─────────────────────────────────────────
TEST_PROMPT_TEMPLATE = """You will generate test prompts for a newly-proposed Agent Skill.

━━━ SKILL DRAFT ━━━
{skill_md}

━━━ ORIGINATING SESSION (first 2k chars) ━━━
{session_excerpt}

━━━ TASK ━━━
Produce {n} distinct test prompts that an end-user might send to Jarvis, each of
which SHOULD trigger this skill. Prompts must:
  - Be naturally phrased (Chinese or English matching original)
  - Cover different angles (not paraphrases of one prompt)
  - Reference the pattern abstractly (not specific files from the original session)
  - Include at least one prompt that looks similar but SHOULD NOT match
    (negative probe — tests precision of `when_to_use`)

Output a JSON array, no prose:
```test_prompts_json
[
  {{"prompt": "...", "expected_approach": "...", "is_negative_probe": false}},
  {{"prompt": "...", "expected_approach": "...", "is_negative_probe": false}},
  {{"prompt": "...", "expected_approach": "N/A", "is_negative_probe": true}}
]
```
"""


def generate_test_prompts(skill_md: str, session_excerpt: str, n: int = 4) -> list[TestPrompt]:
    """Ask Gemini for N test prompts derived from the session + skill draft."""
    call_gemini = _get_gemini()
    prompt = TEST_PROMPT_TEMPLATE.format(
        skill_md=skill_md[:6000],
        session_excerpt=session_excerpt[:2000],
        n=n,
    )
    result = call_gemini(prompt)
    if not result:
        return []
    import re
    m = re.search(r"```test_prompts_json\s*\n(.*?)\n```", result, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for item in arr[:n]:
        if not isinstance(item, dict):
            continue
        prompt_text = (item.get("prompt") or "").strip()
        if not prompt_text:
            continue
        out.append(TestPrompt(
            prompt=prompt_text,
            expected_approach=item.get("expected_approach", "")[:500],
        ))
    return out


# ─── Trajectory comparator ────────────────────────────────────────────────────
def compute_overlap(expected_tools: list[str], observed_tools: list[str]) -> float:
    """Compute 0-1 overlap: |set(expected) ∩ set(observed)| / |set(expected)|.

    Uses set overlap rather than strict sequence matching because real sessions
    have order variance — as long as the characteristic tools show up, the
    trajectory is roughly aligned.
    """
    if not expected_tools:
        return 0.0
    exp_set = set(t.lower() for t in expected_tools)
    obs_set = set(t.lower() for t in observed_tools)
    if not exp_set:
        return 0.0
    return len(exp_set & obs_set) / len(exp_set)


def extract_expected_tools_from_skill_md(skill_md: str) -> list[str]:
    """Best-effort: pull tool names out of the skill's operation steps.

    Very loose heuristic — looks for known tool names in the text. Real impl
    should parse `operation_steps` frontmatter if we add it. For now this gives
    the comparator something to work with.
    """
    import re
    common_tools = [
        "exec", "read", "write", "edit",
        "sessions_spawn", "sessions_history", "sessions_list",
        "memory_search", "memory_save",
        "feishu_send", "feishu_docs",
        "web_fetch", "browser", "Read_tool",
    ]
    found = []
    low = skill_md.lower()
    for tool in common_tools:
        if re.search(rf"\b{re.escape(tool.lower())}\b", low):
            found.append(tool)
    return found


# ─── Headless Jarvis runner (STUB — OpenClaw coop needed) ────────────────────
class HeadlessJarvisClient:
    """Ephemeral Jarvis runner. Currently a stub; see docs/OPENCLAW_COOPERATION_PHASE2.md §D.

    When OpenClaw ships headless mode, replace this with:

        class HeadlessJarvisClient:
            def run(self, prompt: str, skills_dir: Path, timeout: int = 60):
                result = subprocess.run(
                    ["openclaw", "jarvis-headless",
                     "--skills-dir", str(skills_dir),
                     "--prompt", prompt, "--timeout", str(timeout),
                     "--output-jsonl", "/tmp/replay.jsonl"],
                    capture_output=True, timeout=timeout + 10,
                )
                ...parse JSONL for tool trajectory...
    """

    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir

    def run(self, prompt: str, timeout: int = 60) -> ReplayRun:
        raise NotImplementedError(
            "HeadlessJarvisClient.run() requires OpenClaw headless mode "
            "(see docs/OPENCLAW_COOPERATION_PHASE2.md §D). "
            "Use dry_run=True or plug in a claude-code-based fallback."
        )


# ─── Main replay loop ────────────────────────────────────────────────────────
def _load_source_session(source_request_id: str | None) -> str:
    """Pull a plain-text session excerpt for test-prompt generation."""
    if not source_request_id:
        return ""
    # Accept either a request ID or a full filename
    fname = source_request_id if source_request_id.endswith(".json") else f"{source_request_id}.json"
    p = QUEUE_DIR / fname
    if not p.exists():
        # Try suffix match
        matches = list(QUEUE_DIR.glob(f"*{source_request_id}*.json"))
        if matches:
            p = matches[0]
        else:
            return ""
    try:
        data = json.loads(p.read_text())
        parts = []
        for i, m in enumerate(data.get("userMessages", []) or []):
            parts.append(f"[user #{i+1}] {m}")
        for i, t in enumerate(data.get("assistantTexts", []) or []):
            parts.append(f"[agent #{i+1}] {t}")
        return "\n".join(parts)
    except Exception:
        return ""


def replay_skill(
    skill_name: str,
    source_request_id: str | None = None,
    *,
    n_prompts: int = 4,
    dry_run: bool = True,
    use_runner: bool = False,
) -> ReplayVerdict:
    """Top-level replay gate entry point.

    Args:
        skill_name: the draft under `skills/auto-learned/<name>/`
        source_request_id: queue-file id the draft was derived from (for test prompt context)
        n_prompts: how many test prompts to generate
        dry_run: if True, skip the real runner; use Gemini self-play to score
        use_runner: if True, exercise HeadlessJarvisClient (requires OpenClaw coop)

    Returns a ReplayVerdict. When `use_runner=False and dry_run=True`, the
    verdict is a best-effort pre-check based on Gemini's own reading of the
    skill vs. test prompts — useful while waiting on the real runner.
    """
    draft = AUTO_LEARNED / skill_name
    if not draft.exists():
        raise FileNotFoundError(f"Draft not found: {draft}")
    skill_md = (draft / "SKILL.md").read_text()

    session_excerpt = _load_source_session(source_request_id)
    test_prompts = generate_test_prompts(skill_md, session_excerpt, n=n_prompts)

    runs: list[ReplayRun] = []
    expected = extract_expected_tools_from_skill_md(skill_md)

    if use_runner and not dry_run:
        runner = HeadlessJarvisClient(WORKSPACE / "skills")  # will raise until OpenClaw ships
        for tp in test_prompts:
            runs.append(runner.run(tp.prompt))
    else:
        # Dry-run fallback: ask Gemini to predict tool trajectory for each prompt
        runs = _dry_run_predict(skill_md, test_prompts, expected)

    # Aggregate
    if not runs:
        return ReplayVerdict(skill_name, test_prompts, [], 0.0, 0.0, False, dry_run=dry_run)

    pass_rate = sum(r.overlap_score for r in runs) / len(runs)
    skill_loaded_rate = sum(1 for r in runs if r.skill_loaded) / len(runs)
    gate_open = pass_rate >= 0.6 and skill_loaded_rate >= 0.5

    return ReplayVerdict(
        skill_name=skill_name,
        test_prompts=test_prompts,
        runs=runs,
        pass_rate=pass_rate,
        skill_loaded_rate=skill_loaded_rate,
        gate_open=gate_open,
        dry_run=dry_run,
    )


DRY_RUN_TEMPLATE = """You will predict what tools Jarvis would call for a given prompt if it had loaded this SKILL.md.

━━━ SKILL.md ━━━
{skill_md}

━━━ PROMPT ━━━
{prompt}

━━━ TASK ━━━
Two questions:
  1. Would Jarvis read this SKILL.md when given this prompt? (yes/no)
  2. What tools would Jarvis call if it followed the skill's operation_steps?

Output a JSON object, no prose:
```predict_json
{{"skill_loaded": true/false, "tool_trajectory": ["tool1", "tool2", ...]}}
```
"""


def _dry_run_predict(skill_md: str, test_prompts: list[TestPrompt], expected: list[str]) -> list[ReplayRun]:
    """Dry-run fallback — use Gemini to predict what tools Jarvis would call.

    Cheaper than a real runner and useful for sanity-checking draft quality
    before OpenClaw headless mode lands. Expected to have higher false-pass
    rate than real replay; the real gate should only ship with a real runner.
    """
    call_gemini = _get_gemini()
    import re
    runs: list[ReplayRun] = []
    for tp in test_prompts:
        prompt = DRY_RUN_TEMPLATE.format(skill_md=skill_md[:6000], prompt=tp.prompt)
        result = call_gemini(prompt) or ""
        m = re.search(r"```predict_json\s*\n(.*?)\n```", result, re.DOTALL)
        if not m:
            runs.append(ReplayRun(prompt=tp.prompt, error="no_predict_json"))
            continue
        try:
            pj = json.loads(m.group(1))
        except Exception:
            runs.append(ReplayRun(prompt=tp.prompt, error="invalid_json"))
            continue
        traj = pj.get("tool_trajectory") or []
        if not isinstance(traj, list):
            traj = []
        runs.append(ReplayRun(
            prompt=tp.prompt,
            skill_loaded=bool(pj.get("skill_loaded")),
            tool_trajectory=[str(t) for t in traj],
            overlap_score=compute_overlap(expected, [str(t) for t in traj]),
        ))
    return runs


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Phase D replay validation gate (skeleton)")
    parser.add_argument("--skill", required=True)
    parser.add_argument("--source-request", default=None,
                        help="Queue file id the draft came from (for test prompt context)")
    parser.add_argument("--n-prompts", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true",
                        help="Use Gemini self-play; skip headless runner (default)")
    parser.add_argument("--use-runner", action="store_true",
                        help="Attempt real HeadlessJarvisClient (will fail until OpenClaw coop)")
    parser.add_argument("--output", default=None, help="Write verdict JSON to path")
    args = parser.parse_args()

    verdict = replay_skill(
        skill_name=args.skill,
        source_request_id=args.source_request,
        n_prompts=args.n_prompts,
        dry_run=not args.use_runner,
        use_runner=args.use_runner,
    )

    print(verdict.summary_line())
    for i, r in enumerate(verdict.runs):
        print(f"  [{i+1}] loaded={r.skill_loaded} overlap={r.overlap_score:.2f} err={r.error or '-'}")
        print(f"      prompt: {r.prompt[:120]}")
        if r.tool_trajectory:
            print(f"      traj: {r.tool_trajectory}")

    if args.output:
        Path(args.output).write_text(json.dumps(asdict(verdict), ensure_ascii=False, indent=2))
        print(f"verdict written to {args.output}")

    return 0 if verdict.gate_open else 1


if __name__ == "__main__":
    sys.exit(main())
