"""
v3 — Balanced recall + precision.
Changes from v2:
  1. Keep recall improvements (pattern focus, debugging/cron examples)
  2. Add stronger precision guard: routine cron without DEVIATION is NOT a skill
  3. Add "deviation test": session must show agent DEVIATING from expected path
  4. Strengthen red flags with specific false positive patterns from v2 evaluation

Phase A updates:
  A.4: inject rejection-context.json (recent skipped/discussed skills) as negative examples
  A.5: relax cron red flag — multi-hop orchestration / error recovery can qualify
  A.6: drop quoting on quality_score fields so Gemini emits integers (weights corrected)
"""

import json
from pathlib import Path

REJECTION_CONTEXT_FILE = Path.home() / ".openclaw/workspace/data/skill-learner/rejection-context.json"
_RECENT_REJECTIONS_CAP = 10


def _load_recent_rejections_note() -> str:
    """Read the last N entries from rejection-context.json and format as negative examples.

    Returns empty string if no rejections recorded yet or file missing.
    """
    if not REJECTION_CONTEXT_FILE.exists():
        return ""
    try:
        entries = json.loads(REJECTION_CONTEXT_FILE.read_text())
        if not isinstance(entries, list):
            return ""
    except Exception:
        return ""
    recent = entries[-_RECENT_REJECTIONS_CAP:]
    if not recent:
        return ""
    lines = []
    for r in recent:
        if not isinstance(r, dict):
            continue
        neg = r.get("promptNegativeExample") or ""
        if not neg:
            continue
        lines.append(f"- {neg}")
    if not lines:
        return ""
    return (
        "\n━━━ 用户已拒绝的历史提议 (不要重复) ━━━\n"
        + "\n".join(lines)
        + "\n如果本次 session 的模式在抽象层与上述任一被拒提议相同，应输出 NO_SKILL。\n"
        + "判断的是「抽象模式」，不是「表面话题」——"
        + "即使表面话题不同，只要底层做法重合，也属于重复提议。\n"
    )


# ─── Phase 4 (C.1.b/C.1.c) context enrichers ──────────────────────────────
_TOOL_TRACE_RENDER_CAP = 20          # render at most this many entries in prompt
_TOOL_TRACE_PARAM_CHARS = 180        # per-field truncation when rendering
_SUBAGENT_RENDER_CAP = 5             # render at most this many child summaries


def _format_params_inline(params: dict | None) -> str:
    """Render a redacted params dict inline, heavily truncated for prompt budget.

    Plugin already ran `sanitizeParams` (secrets redacted, long strings tagged);
    we just need to keep the prompt budget under control.
    """
    if not isinstance(params, dict) or not params:
        return ""
    skip = {"__truncated"}
    parts = []
    truncated_note = params.get("__truncated") is True
    for k, v in params.items():
        if k in skip:
            continue
        if isinstance(v, (dict, list)):
            try:
                v_str = json.dumps(v, ensure_ascii=False)
            except Exception:
                v_str = str(v)
        else:
            v_str = str(v)
        if len(v_str) > _TOOL_TRACE_PARAM_CHARS:
            v_str = v_str[:_TOOL_TRACE_PARAM_CHARS] + "…"
        parts.append(f"{k}={v_str}")
    rendered = ", ".join(parts) if parts else "(no params)"
    if truncated_note:
        rendered += " [payload-truncated]"
    return rendered


def _build_tool_trace_note(trace: list | None) -> str:
    """Format a sanitized tool trace as an ordered timeline for Gemini.

    Phase 4 C.1.b: plugin's `appendToolTrace` already redacts secrets and caps
    each entry. Here we further truncate for prompt budget and show the first N.
    """
    if not isinstance(trace, list) or not trace:
        return ""
    lines = []
    rendered = 0
    for entry in trace[:_TOOL_TRACE_RENDER_CAP]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or "?"
        err = entry.get("error")
        params_str = _format_params_inline(entry.get("params"))
        tag = f" ❌ error={str(err)[:80]}" if err else ""
        dur = entry.get("durationMs")
        dur_str = f" ({int(dur)}ms)" if isinstance(dur, (int, float)) and dur else ""
        lines.append(f"{rendered + 1}. {name}{dur_str}{tag}  params: {params_str}")
        rendered += 1
    if rendered == 0:
        return ""
    more = len(trace) - rendered
    suffix = f"\n  (+{more} earlier call(s) elided)" if more > 0 else ""
    return (
        "\n━━━ 本次 session 工具调用轨迹 (redacted, Phase 4 C.1.b) ━━━\n"
        "从轨迹可以判断:调用顺序是否有模式价值,是否出现错误恢复,是否组合了非显然工具。\n"
        + "\n".join(lines)
        + suffix
        + "\n"
    )


def _build_subagent_note(summaries: list | None) -> str:
    """Format sub-agent (sessions_spawn) child summaries for parent session eval.

    Phase 4 C.1.c: each summary carries the child run's toolCount / toolNames /
    first 3 user+assistant messages + outcome. Gives Gemini visibility into the
    spawned work that the parent's own transcript otherwise hides.
    """
    if not isinstance(summaries, list) or not summaries:
        return ""
    blocks = []
    for idx, s in enumerate(summaries[:_SUBAGENT_RENDER_CAP], start=1):
        if not isinstance(s, dict):
            continue
        agent_id = s.get("agentId") or "sub-agent"
        tool_count = s.get("toolCount") or 0
        tool_names = ", ".join((s.get("toolNames") or [])[:8])
        outcome = s.get("outcome") or "unknown"
        err = s.get("error")
        user_msgs = s.get("userMessages") or []
        asst_texts = s.get("assistantTexts") or []
        u_join = "\n    ".join((m or "")[:300] for m in user_msgs[:3])
        a_join = "\n    ".join((t or "")[:300] for t in asst_texts[:3])
        header = f"[{idx}] {agent_id}  tools={tool_count} ({tool_names})  outcome={outcome}"
        if err:
            header += f"  error={str(err)[:80]}"
        blocks.append(
            header
            + ("\n  用户消息 (前 3):\n    " + u_join if u_join else "")
            + ("\n  Agent 回复 (前 3):\n    " + a_join if a_join else "")
        )
    if not blocks:
        return ""
    more = len(summaries) - len(blocks)
    suffix = f"\n(+{more} other sub-agent(s) elided)" if more > 0 else ""
    return (
        "\n━━━ 子 Agent 派发任务上下文 (Phase 4 C.1.c) ━━━\n"
        "父 session 通过 sessions_spawn 派发给子 agent 的工作在此:\n"
        "判断模式可复用性时,子 run 的工具链也要纳入考量 (之前是盲区)。\n\n"
        + "\n\n".join(blocks)
        + suffix
        + "\n"
    )


def build_new_skill_prompt(request: dict, existing_summary: str) -> str:
    """Build prompt for evaluating whether a session should produce a NEW skill."""
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"

    user_msgs = request.get("userMessages", [])
    asst_msgs = request.get("assistantTexts", [])
    formatted_user = "\n".join(f"User [turn {i+1}]: {m}" for i, m in enumerate(user_msgs))
    formatted_asst = "\n".join(f"Agent [turn {i+1}]: {t}" for i, t in enumerate(asst_msgs))

    # Build dedup signal from skillsUsed
    skills_used = request.get("skillsUsed", [])
    skills_used_note = ""
    if skills_used:
        skills_used_note = (
            "\n⚠️ DEDUP SIGNAL: This session actively USED these existing skills: "
            + ", ".join(skills_used)
            + "\nIf the session simply applied an existing skill successfully without discovering"
            + " anything new, output NO_SKILL. Only create a new skill if the pattern is"
            + " genuinely DIFFERENT from these existing skills.\n"
        )

    # Phase A.4: inject recent user rejections as negative examples
    rejections_note = _load_recent_rejections_note()

    # Phase 4 C.1.b: per-call tool trace (redacted params)
    tool_trace_note = _build_tool_trace_note(request.get("toolTrace"))

    # Phase 4 C.1.c: sub-agent (sessions_spawn) child summaries
    subagent_note = _build_subagent_note(request.get("subagentSummaries"))

    # Phase B.4 + Phase 4 B.1: agent self-nomination as high-trust signal.
    # Now distinguishes first-class tool (complete payload) from polyfill paths.
    nomination_note = ""
    if request.get("nominated") and isinstance(request.get("nominationPayload"), dict):
        np = request["nominationPayload"]
        topic = (np.get("topic") or "").strip()
        pain = (np.get("pain_point") or "").strip()
        reusable = (np.get("reusable_pattern") or "").strip()
        conf = np.get("confidence") or "medium"
        evidence = np.get("evidence_turns") or []
        if isinstance(evidence, list):
            evidence_str = ", ".join(f"turn {t}" for t in evidence[:8])
        else:
            evidence_str = str(evidence)
        is_first_class = bool(np.get("_firstClass"))
        is_polyfill = bool(np.get("_polyfill"))

        if is_first_class and (topic or reusable or pain):
            # Phase 4 B.1 first-class tool path — highest trust.
            nomination_note = (
                "\n━━━ AGENT SELF-NOMINATION (first-class tool, 最高信任信号) ━━━\n"
                f"Jarvis 在本次 session 主动调用了 `skill_learner_nominate` 工具。\n"
                f"  Topic: {topic or '(未填)'}\n"
                f"  Pain point: {pain or '(未填)'}\n"
                f"  Reusable pattern: {reusable or '(未填)'}\n"
                f"  Confidence: {conf}\n"
                f"  Evidence turns: {evidence_str or '(未标注)'}\n"
                "\n权重说明:这是 OpenClaw 原生工具路径——agent 明确声明本次有可沉淀模式,\n"
                "这是比 polyfill 更强的信号(agent 走工具 schema 校验,而不是写 JSON 文件)。\n"
                "  • 若 session 内容能支持 Jarvis 的自述 → 倾向 QUALIFY,即便 deviation 不显眼。\n"
                "  • 若 session 与 nomination 完全不匹配(Jarvis 说发现了 X,transcript 里看不到 X)→ 仍可 NO_SKILL。\n"
                "  • confidence=low 的 nomination 需要更强 session 佐证才 qualify。\n"
            )
        elif topic or reusable or pain:
            # Polyfill path with full payload (agent wrote JSON file with all fields).
            label = "via polyfill file" if is_polyfill else "via polyfill"
            nomination_note = (
                f"\n━━━ AGENT SELF-NOMINATION ({label}, 高信任信号) ━━━\n"
                f"Jarvis 在本次 session 通过 polyfill 文件写入的方式提名(first-class 工具未就绪时降级)。\n"
                f"  Topic: {topic or '(未填)'}\n"
                f"  Pain point: {pain or '(未填)'}\n"
                f"  Reusable pattern: {reusable or '(未填)'}\n"
                f"  Confidence: {conf}\n"
                f"  Evidence turns: {evidence_str or '(未标注)'}\n"
                "\n权重与 first-class 相同:polyfill 只是传输路径不同,语义等价。\n"
            )
        elif is_polyfill:
            # Polyfill path but plugin couldn't read the payload (write→only got path).
            nomination_note = (
                "\n━━━ AGENT NOMINATION (polyfill,payload 未捕获) ━━━\n"
                "Jarvis 向 nominations/ 目录写了文件但 plugin 只看到 file path 没读内容。\n"
                "按常规标准评估,但把「agent 主动标记」作为弱加分。\n"
            )

    prompt = (
        "You are evaluating an AI agent session to decide if a reusable Skill should be created.\n"
        "\n"
        "━━━ SYSTEM CONTEXT ━━━\n"
        "OpenClaw is an AI agent orchestration platform. \"Jarvis\" is the primary agent instance,\n"
        "accessible via Feishu (Chinese workplace platform, similar to Slack). Jarvis handles:\n"
        "- Direct user conversations (Feishu DMs) — interactive problem-solving\n"
        "- Cron/scheduled tasks — daily journal, intel gathering, morning reports, memory sync\n"
        "- Subagent spawning — parallel task decomposition via sessions_spawn tool\n"
        "\n"
        "Jarvis's tool set includes: exec (shell commands), read/write/edit (files), process (background jobs),\n"
        "sessions_spawn/sessions_history (agent orchestration), web_fetch/browser (web), feishu_* (docs/messages),\n"
        "and domain-specific tools (nano-banana-image, notebooklm, etc.).\n"
        "\n"
        "Skills are stored as SKILL.md files and loaded by Jarvis at runtime to guide behavioral\n"
        "patterns for recurring task types.\n"
        "\n"
        f"SESSION: {tool_info}\n"
        f"{skills_used_note}"
        "\n"
        "CONVERSATION:\n"
        f"{formatted_user}\n"
        "\n"
        "AGENT RESPONSES:\n"
        f"{formatted_asst}\n"
        "\n"
        "EXISTING SKILLS (do NOT create a skill that duplicates these):\n"
        f"{existing_summary}\n"
        f"{rejections_note}"
        f"{nomination_note}"
        f"{tool_trace_note}"
        f"{subagent_note}"
        "\n"
        "━━━ WHAT IS AN OPENCLAW SKILL ━━━\n"
        "A Skill is a reusable *agent behavioral pattern* — a guide for HOW Jarvis should approach\n"
        "a class of tasks in the future. It is NOT: a code fix for one specific script, a one-time\n"
        "optimization, or general programming advice.\n"
        "\n"
        "IMPORTANT — Focus on the PATTERN, not the surface context:\n"
        "Even if a session is about a specific system (e.g., fixing evaluate-server.py), the\n"
        "underlying pattern may be highly reusable. Ask: \"Would this approach help Jarvis in a\n"
        "DIFFERENT system with a similar class of problem?\" For example:\n"
        "- Debugging a launchd service → pattern: \"diagnosing background service env issues\"\n"
        "- Fixing notification delivery → pattern: \"tracing multi-hop notification pipelines\"\n"
        "- Data pipeline with error recovery → pattern: \"resilient multi-source data collection\"\n"
        "\n"
        "Core test (from Hermes): \"Did this session require trial and error, changing course due to\n"
        "experiential findings, or did the user correct the agent's approach?\" If yes → strong Skill candidate.\n"
        "\n"
        "Trial-and-error signals to look for:\n"
        "- \"didn't work\", \"tried\", \"instead\", \"realized\", \"turns out\", \"actually\"\n"
        "- Agent changed approach mid-session after a failure\n"
        "- User corrected the agent: \"不对\", \"不是\", \"应该\", \"错了\", \"no,\", \"wrong\"\n"
        "- Agent self-corrected: \"操\", \"赶紧恢复\", \"我想简单了\", \"想错了\"\n"
        "\n"
        "━━━ QUALIFICATION CRITERIA ━━━\n"
        "Need ALL of (A) + (B), plus at least one of (C)–(E):\n"
        "\n"
        "A. The PATTERN (not the specific fix) is reusable across ≥2 DIFFERENT future contexts\n"
        "   (\"different\" = different problem domain, different file type, or different tool chain)\n"
        "B. It's about Jarvis's tool usage or workflow orchestration — not \"fix script X\"\n"
        "   NOTE: Even sessions that FIX something can reveal reusable orchestration patterns.\n"
        "   The question is whether the APPROACH (not the fix itself) transfers to other scenarios.\n"
        "C. Required non-obvious trial and error or course correction to discover\n"
        "D. Contains specific tool combos, parameters, sequencing, or pitfalls worth documenting\n"
        "E. The user corrected the agent's method — Jarvis would repeat the mistake without this\n"
        "\n"
        "━━━ DEVIATION TEST (mandatory) ━━━\n"
        "Before qualifying a skill, you MUST identify a specific DEVIATION — a moment where the\n"
        "agent's path diverged from what would be expected. Examples of deviations:\n"
        "- Agent tried approach A, it failed, switched to approach B\n"
        "- Agent discovered an unexpected pitfall mid-execution\n"
        "- User corrected the agent's direction\n"
        "- Agent combined tools in a non-obvious sequence\n"
        "If you cannot point to a specific deviation moment, output NO_SKILL.\n"
        "\n"
        "━━━ RED FLAGS → output NO_SKILL ━━━\n"
        "• Pattern only applies to one specific file/config AND the approach is trivial\n"
        "• The session merely follows a pre-written script without any deviation or discovery\n"
        "• The approach is obvious (standard debugging, simple file edit, routine API call)\n"
        "• An existing skill above already covers this exact pattern\n"
        "• Cron/scheduled task that ran as a LINEAR, SINGLE-BRANCH tool chain:\n"
        "  no sub-agent spawns, no fallback branches, no retries, no source failures that\n"
        "  required recovery. A cron that simply executed its playbook step-by-step and succeeded\n"
        "  is NOT a skill — using many tools ≠ discovering a pattern.\n"
        "  NOTE: cron tasks that orchestrate multi-hop parallel work, recover from partial source\n"
        "  failures, combine tools in non-obvious ways, OR made a course-correction mid-run\n"
        "  CAN qualify. Evaluate on DEVIATION / RECOVERY, not on whether the cron finished.\n"
        "• Session describes a standard data read → format → output pipeline with no surprises\n"
        "• Agent merely synced data between two systems without encountering obstacles\n"
        "\n"
        "━━━ EXAMPLES ━━━\n"
        "\n"
        "Example 1 — QUALIFIES (new skill):\n"
        "Session: Agent tried 3 approaches to parse a complex PDF, first with plain text extraction (failed),\n"
        "then with page-by-page OCR (too slow), finally discovered combining PyMuPDF structured extraction\n"
        "with fallback OCR only for scanned pages. User said \"that's much better, remember this approach.\"\n"
        "→ This qualifies: trial-and-error (C), specific tool combo (D), reusable across PDF tasks (A).\n"
        "\n"
        "Example 2 — QUALIFIES (pattern from debugging session):\n"
        "Session: Agent was debugging why Feishu card notifications failed. Traced the issue through:\n"
        "plugin → HTTP POST → evaluate-server → Feishu API → discovered launchd env var missing.\n"
        "→ This qualifies: the PATTERN \"tracing notification delivery through multi-hop pipeline\"\n"
        "  is reusable for any notification/webhook debugging (A), required trial-and-error (C),\n"
        "  specific diagnostic sequence (D).\n"
        "\n"
        "Example 3 — QUALIFIES (pattern from cron task):\n"
        "Session: Cron task for data collection. Multiple sources failed (Reddit timeout, LLM rate limit).\n"
        "Agent developed a pattern: check each source independently, continue on failure, aggregate\n"
        "partial results, report failures separately.\n"
        "→ This qualifies: resilient multi-source collection PATTERN is reusable (A),\n"
        "  specific error handling sequence (D), discovered through failures (C).\n"
        "\n"
        "Example 4 — NO_SKILL:\n"
        "Session: Agent fixed a typo in config.yaml and restarted the service.\n"
        "→ NO_SKILL: one-off fix, trivial approach, not a behavioral pattern.\n"
        "\n"
        "Example 5 — NO_SKILL:\n"
        "Session: Daily journal cron — agent reads session logs, writes diary entry, updates index.\n"
        "No errors, no course corrections, followed the script exactly.\n"
        "→ NO_SKILL: routine execution without any novel discovery or approach change.\n"
        "\n"
        "━━━ INSTRUCTIONS ━━━\n"
        "\n"
        "Step 1 — REASONING (mandatory): Before deciding, analyze the session by answering:\n"
        "  (1) What is the underlying PATTERN? (abstract away from the specific system)\n"
        "  (2) DEVIATION TEST: Point to the specific moment the agent deviated from the expected\n"
        "      path. Quote the relevant text. If no deviation exists, this is NOT a skill.\n"
        "  (3) Is this pattern reusable across ≥2 different contexts? Why or why not?\n"
        "  (4) Which of criteria C, D, E apply? Cite specific evidence from the conversation.\n"
        "\n"
        "Step 2 — DECISION:\n"
        "  If NOT qualified: output your reasoning, then on a new line: NO_SKILL\n"
        "  If qualified: output your reasoning, then BOTH blocks below in order.\n"
        "\n"
        "⚠️ LANGUAGE REQUIREMENT: ALL text fields MUST be written in Simplified Chinese (简体中文).\n"
        "skill_name may use Chinese or a short English identifier. Do NOT write English prose in any field.\n"
        "\n"
        "```eval_json\n"
        "{\n"
        '  "skill_name": "<简洁名称，中文或短英文>",\n'
        '  "problem_context": "<1-2句：这个模式解决什么反复出现的挑战，为什么不显而易见>",\n'
        '  "recommended_approach": "<2-4句：核心洞察、为什么有效、何时应用>",\n'
        '  "when_to_use": ["<场景1>", "<场景2>", "<场景3>"],\n'
        '  "key_patterns": ["<具体工具组合或参数1>", "<模式2>"],\n'
        '  "pitfalls": ["<雷区1>", "<雷区2>"],\n'
        '  "quality_score": {\n'
        '    "reusability": <1-10 整数>,\n'
        '    "insight_depth": <1-10 整数>,\n'
        '    "specificity": <1-10 整数>,\n'
        '    "pitfall_coverage": <1-10 整数>,\n'
        '    "completeness": <1-10 整数>,\n'
        '    "total": <0-100 整数, 五项加权总分: reusability×2.5 + insight_depth×2.5 + specificity×2.0 + pitfall_coverage×1.5 + completeness×1.5>\n'
        "  }\n"
        "}\n"
        "```\n"
        "\n"
        "```skill_md\n"
        "---\n"
        "name: <name>\n"
        "description: <一句话描述（中文）>\n"
        "version: 1.0.0\n"
        "tags: [<tag1>, <tag2>]\n"
        "---\n"
        "\n"
        "# <name>\n"
        "\n"
        "## 适用场景\n"
        "- 场景1\n"
        "- 场景2\n"
        "- 场景3\n"
        "\n"
        "## 不适用场景\n"
        "- 反模式1\n"
        "- 反模式2\n"
        "\n"
        "## 操作步骤\n"
        "1. 第一步：做什么及原因\n"
        "2. 第二步：...\n"
        "3. 第三步：...\n"
        "\n"
        "## 示例\n"
        "**场景**：具体场景简述\n"
        "**做法**：逐步操作\n"
        "**结果**：预期产出\n"
        "\n"
        "## 已知雷区\n"
        "- 雷区1：错过会发生什么\n"
        "- 雷区2：...\n"
        "\n"
        "## 验证方式\n"
        "- 如何确认成功生效\n"
        "\n"
        "## 相关 Skill\n"
        "- 列出相关已有 Skill，或写「无」\n"
        "```"
    )

    return prompt


def build_update_skill_prompt(request: dict, skill_name: str, skill_content: str) -> str:
    """Build prompt for evaluating whether a session reveals updates for an existing skill."""
    tool_info = f"{request['toolCount']} calls ({', '.join(request.get('toolNames', [])[:10])})"

    user_msgs = request.get("userMessages", [])
    asst_msgs = request.get("assistantTexts", [])
    formatted_user = "\n".join(f"User [turn {i+1}]: {m}" for i, m in enumerate(user_msgs))
    formatted_asst = "\n".join(f"Agent [turn {i+1}]: {t}" for i, t in enumerate(asst_msgs))

    truncated_skill = skill_content[:3000]

    prompt = (
        "You are evaluating whether a session revealed new information to UPDATE an existing Skill.\n"
        "\n"
        "━━━ SYSTEM CONTEXT ━━━\n"
        "OpenClaw is an AI agent orchestration platform. \"Jarvis\" is the primary agent, accessible via\n"
        "Feishu (Chinese workplace platform). Sessions include: direct conversations, cron tasks (daily\n"
        "journal, intel gathering, morning reports), and subagent spawning for parallel work.\n"
        "\n"
        f'EXISTING SKILL "{skill_name}" (truncated):\n'
        f"{truncated_skill}\n"
        "\n"
        f"SESSION: {tool_info}\n"
        "\n"
        "CONVERSATION:\n"
        f"{formatted_user}\n"
        "\n"
        "AGENT RESPONSES:\n"
        f"{formatted_asst}\n"
        "\n"
        "━━━ EVALUATION CRITERIA (Hermes-inspired) ━━━\n"
        "Did this session reveal something NOT covered by the existing skill? Look for:\n"
        "1. A pitfall or error the agent hit that the skill didn't warn about\n"
        "2. A better/faster approach than what the skill describes (discovered via trial and error)\n"
        "3. The user corrected the agent's method — indicating a gap in the skill's guidance\n"
        "4. A new scenario where the skill applies but wasn't documented in \"When to Use\"\n"
        "5. A boundary case or failure mode that should be added to \"When NOT to Use\"\n"
        "\n"
        "IMPORTANT: Even if the session's surface topic differs from the skill's primary domain,\n"
        "check whether the underlying PATTERN reveals gaps. For example, a weekly review session\n"
        "might reveal new messaging patterns not covered by the messaging-patterns skill.\n"
        "\n"
        "━━━ EXAMPLES ━━━\n"
        "\n"
        "Example — QUALIFIES for update:\n"
        "Existing skill: \"Multi-Source Data Aggregation\" describes using parallel API calls.\n"
        "Session: Agent hit a rate limit on source B, had to add exponential backoff + circuit breaker.\n"
        "User said \"记住这个坑\". The skill's Pitfalls section didn't mention rate limiting.\n"
        "→ UPDATE: add rate limiting pitfall + backoff procedure step.\n"
        "\n"
        "Example — QUALIFIES for update:\n"
        "Existing skill: \"messaging-patterns\" covers notification card templates.\n"
        "Session: Weekly review cron used a new card layout with collapsed sections and form inputs.\n"
        "The skill didn't document Card 2.0 interactive elements.\n"
        "→ UPDATE: add Card 2.0 interactive patterns (form, collapsible_panel) to the skill.\n"
        "\n"
        "Example — NO_UPDATE:\n"
        "Existing skill: \"Git Branch Cleanup\" describes pruning merged branches.\n"
        "Session: Agent used the same procedure successfully on a different repo.\n"
        "→ NO_UPDATE: skill worked as documented, no new information.\n"
        "\n"
        "━━━ INSTRUCTIONS ━━━\n"
        "\n"
        "Step 1 — REASONING (mandatory): Analyze the session and explain:\n"
        "  (1) Did the agent encounter something the skill didn't cover?\n"
        "  (2) What specific gap was revealed? Cite evidence from the conversation.\n"
        "  (3) Is this gap generalizable (will other sessions hit it too)?\n"
        "\n"
        "Step 2 — DECISION:\n"
        "  If NONE of the criteria apply: output your reasoning, then: NO_UPDATE\n"
        "\n"
        "  If update is warranted: output your reasoning, then BOTH blocks below.\n"
        "\n"
        "⚠️ LANGUAGE REQUIREMENT: ALL text fields MUST be written in Simplified Chinese (简体中文).\n"
        "Do NOT write English prose in any field.\n"
        "\n"
        "```eval_json\n"
        "{\n"
        f'  "skill_name": "{skill_name}",\n'
        '  "problem_context": "<本次 session 发现了现有 Skill 中的什么空白>",\n'
        '  "recommended_approach": "<更好的做法或修正，为什么是改进>",\n'
        '  "when_to_use": ["<更新后的适用场景>"],\n'
        '  "new_pitfalls": ["<新雷区1>", "<新雷区2>"],\n'
        '  "key_changes": ["<修改什么及原因>"],\n'
        '  "quality_score": {\n'
        '    "reusability": <1-10 整数>,\n'
        '    "insight_depth": <1-10 整数>,\n'
        '    "specificity": <1-10 整数>,\n'
        '    "pitfall_coverage": <1-10 整数>,\n'
        '    "completeness": <1-10 整数>,\n'
        '    "total": <0-100 整数, 五项加权总分: reusability×2.5 + insight_depth×2.5 + specificity×2.0 + pitfall_coverage×1.5 + completeness×1.5>\n'
        "  }\n"
        "}\n"
        "```\n"
        "\n"
        "```skill_update\n"
        "## Sections to Add/Modify\n"
        "\n"
        "### Pitfalls (append)\n"
        "- New pitfall: ...\n"
        "\n"
        "### Procedure (append or modify)\n"
        "- Additional step: ...\n"
        "\n"
        "### When to Use (append if new scenario)\n"
        "- New scenario: ...\n"
        "\n"
        "### When NOT to Use (append if new boundary)\n"
        "- New anti-pattern: ...\n"
        "```"
    )

    return prompt
