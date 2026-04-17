"""
v4 — Rich session transcript prompt (Phase C.3 skeleton).

Differences from v3_balanced:
  1. When `request.sessionFile` points to a JSONL file, load the FULL session
     via `load_full_session_transcript` instead of the truncated
     `userMessages`/`assistantTexts` pair — 30k char budget, priority loading
     for `nominationPayload.evidence_turns`.
  2. Prompt asks Gemini to cite specific `event_ref: "turn 47"` in key_patterns
     so proposals are grounded in observable evidence, not hallucinated.

Fallback: when no sessionFile / loader yields empty, drop to v3 behavior.
This prompt version is NOT the default — opt in via env `PROMPT_VERSION=v4_rich_transcript`.
"""

import json
import os
import sys
from pathlib import Path

# Reach into the parent scripts dir so we can borrow v3's helpers and the
# transcript loader from the main evaluator without re-implementing.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import v3_balanced as v3  # noqa: E402


def _load_rich_transcript(request: dict) -> list[dict]:
    """Defensive loader — swallows all failures and returns []."""
    session_file = request.get("sessionFile")
    if not session_file:
        return []
    priority = []
    np = request.get("nominationPayload") or {}
    if isinstance(np, dict):
        priority = np.get("evidence_turns") or []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sle_loader", str(_SCRIPTS_DIR / "skill-learner-evaluate.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.load_full_session_transcript(
            session_file,
            max_chars=int(os.environ.get("OMC_RICH_BUDGET", "30000")),
            priority_turns=priority,
        )
    except Exception:
        return []


def _format_rich_turns(turns: list[dict]) -> str:
    if not turns:
        return ""
    lines = []
    for t in turns:
        role = t.get("role") or "?"
        content = (t.get("content") or "").replace("\n", "\n    ")
        header = f"[turn {t['turn']}, {role}]"
        if t.get("tool_name"):
            header += f" (tool: {t['tool_name']}"
            if t.get("error"):
                header += f", error: {str(t['error'])[:80]}"
            header += ")"
        if t.get("_truncated"):
            content += "\n    …[truncated]"
        lines.append(f"{header}\n    {content}")
    return "\n\n".join(lines)


def build_new_skill_prompt(request: dict, existing_summary: str) -> str:
    """v4 prompt with rich transcript + turn citations."""
    turns = _load_rich_transcript(request)
    if not turns:
        # No JSONL available — degrade gracefully to v3.
        return v3.build_new_skill_prompt(request, existing_summary)

    base = v3.build_new_skill_prompt(request, existing_summary)
    rich_block = (
        "\n━━━ FULL SESSION TRANSCRIPT (rich mode, Phase C) ━━━\n"
        "以下是本次 session 的完整 JSONL 转录（上面的 CONVERSATION/AGENT RESPONSES 是截断摘要,这里是原始数据）。\n"
        "评估时应优先引用此段的具体 turn 编号作为证据。\n\n"
        + _format_rich_turns(turns)
        + "\n\n"
    )

    # Add a grounding directive to the eval_json schema guidance
    grounding_directive = (
        "\n\n⚠️ PHASE C 强制要求：\n"
        "- `key_patterns` 中每条至少引用一个 `event_ref: \"turn N\"`(N = 上面 rich transcript 中的 turn 编号),\n"
        "  让 SKILL.md 落地后能追溯到原 session 的具体时刻。\n"
        "- 若 rich transcript 中完全找不到支持 `reusable_pattern` 的 turn,应输出 NO_SKILL。\n"
    )

    # Insert rich block before DEVIATION TEST section so Gemini sees both
    insertion_point = "━━━ WHAT IS AN OPENCLAW SKILL ━━━"
    if insertion_point in base:
        before, after = base.split(insertion_point, 1)
        return before + rich_block + insertion_point + after + grounding_directive
    # Fallback: append at end before format spec
    return base + rich_block + grounding_directive


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    """Update prompts don't get the rich transcript treatment in v4 yet.

    Phase C.3 will extend this once the update-path prompt is rebased. For now,
    passthrough to v3 to avoid silently changing update-proposal quality.
    """
    return v3.build_update_skill_prompt(request, skill_name, skill_content)
