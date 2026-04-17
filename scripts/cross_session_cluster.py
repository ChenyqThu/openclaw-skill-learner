#!/usr/bin/env python3
"""
cross_session_cluster.py — Phase E: Hindsight-style cross-session pattern mining.

Use case: "用户在 2 周内问了 3 次怎么处理飞书文档批量同步" → proactively propose
a skill rather than waiting for the exact moment to auto-extract.

Flow:
    1. Scan the last N days of `analysis-queue/*.json` (or any successfully-completed
       session transcripts) for user messages.
    2. Ask Gemini to cluster them by *abstract intent* — not surface topic.
    3. Any cluster with `cluster_size >= 3` inside the window → emit a
       `proactive-proposal-{ts}.json` suggesting a new skill.
    4. Existing Track 0 + Phase D gate handle the rest (approval, replay).

Usage (CLI):
    python3 cross_session_cluster.py --days 14 --dry-run
    python3 cross_session_cluster.py --days 14 --output /tmp/proactive.json

Status (2026-04-17): SKELETON. The cluster quality depends entirely on Gemini
prompting; first production runs should be monitored for cluster precision.
Defaults are tuned conservatively (min 3 in 14 days) to avoid false positives.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_call_gemini = None
def _get_gemini():
    global _call_gemini
    if _call_gemini is None:
        from gemini_client import call_gemini, load_env
        load_env()  # Read ~/.openclaw/.env so GEMINI_API_KEY is available in cron contexts
        _call_gemini = call_gemini
    return _call_gemini


WORKSPACE = Path.home() / ".openclaw/workspace"
QUEUE_DIR = WORKSPACE / "data/skill-learner/analysis-queue"
PROPOSAL_DIR = WORKSPACE / "data/skill-learner/proactive-proposals"


# ─── Data classes ────────────────────────────────────────────────────────────
@dataclass
class SessionSummary:
    """Thin summary of one session pulled from the queue."""
    request_id: str
    created_at: str
    user_messages: list[str]
    tool_names: list[str]
    nominated: bool = False


@dataclass
class Cluster:
    """A group of semantically-related sessions."""
    theme: str
    abstract_intent: str
    member_request_ids: list[str]
    temporal_span_days: float = 0.0
    pattern_consistency: str = "medium"  # "high" / "medium" / "low"


@dataclass
class ProactiveProposal:
    """A cluster that hit the threshold and should surface as a proposal."""
    proposal_id: str
    generated_at: str
    cluster: Cluster
    confidence: float                 # f(size, span, consistency)
    suggested_skill_name: str
    rationale: str


# ─── Session scanning ────────────────────────────────────────────────────────
def scan_recent_sessions(days: int) -> list[SessionSummary]:
    """Pull session summaries from the analysis queue for the last `days` days."""
    if not QUEUE_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[SessionSummary] = []
    for f in sorted(QUEUE_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        created = data.get("createdAt") or ""
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < cutoff:
            continue
        # Only consider sessions that actually had substance (≥100 char asst output)
        asst = data.get("assistantTexts") or []
        if sum(len(t) for t in asst) < 100:
            continue
        out.append(SessionSummary(
            request_id=data.get("id") or f.stem,
            created_at=created,
            user_messages=data.get("userMessages") or [],
            tool_names=data.get("toolNames") or [],
            nominated=bool(data.get("nominated")),
        ))
    return out


# ─── Gemini clustering ───────────────────────────────────────────────────────
def _parse_cluster_json(result: str) -> list | None:
    """Parse `clusters_json` block, tolerant of truncated closing fences.

    Strategy:
      1. Find the opening ` ```clusters_json ` fence.
      2. Take everything after it up to the first matching closing ` ``` ` if present,
         else to end-of-string.
      3. Trim to the first `[` ... last balanced `]` to survive mid-array truncation.
      4. Return a list (possibly empty) or None on unrecoverable failure.
    """
    if not result:
        return None
    start_m = re.search(r"```clusters_json\s*\n", result)
    if not start_m:
        return None
    body = result[start_m.end():]
    # Cut at closing fence if present
    close = body.find("```")
    if close >= 0:
        body = body[:close]
    # Find first '[' and last balanced ']'
    open_i = body.find("[")
    if open_i < 0:
        return None
    depth = 0
    last_close = -1
    for i, ch in enumerate(body[open_i:], start=open_i):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                last_close = i
    if last_close < 0:
        # Truncated mid-array — attempt to salvage by closing the last complete object.
        # Walk back to find the last complete `}` followed by optional `,`/whitespace.
        tail_obj_end = -1
        obj_depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(body[open_i + 1:], start=open_i + 1):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                obj_depth += 1
            elif ch == "}":
                obj_depth -= 1
                if obj_depth == 0:
                    tail_obj_end = i
        if tail_obj_end < 0:
            return None
        candidate = body[open_i:tail_obj_end + 1] + "]"
    else:
        candidate = body[open_i:last_close + 1]
    try:
        arr = json.loads(candidate)
    except Exception:
        return None
    return arr if isinstance(arr, list) else None


CLUSTER_PROMPT = """You will cluster a list of user prompts from past Jarvis sessions by their ABSTRACT INTENT.

━━━ GOAL ━━━
Identify recurring patterns where the user is asking for the *same kind* of help,
even if surface wording / files / systems differ. The output drives proactive
skill creation — if a cluster has ≥3 members in a short window, Jarvis should
offer to remember the pattern.

━━━ SESSIONS (last {days} days) ━━━
{sessions_block}

━━━ TASK ━━━
Produce 0-8 clusters. IGNORE one-off sessions that don't share intent with any
others. Cluster on abstract intent, not surface topic.

Output ONE json block, no prose:
```clusters_json
[
  {{
    "theme": "<≤50 字,抽象意图>",
    "abstract_intent": "<2-4 句,用户反复想做什么>",
    "member_request_ids": ["id1", "id2", "id3"],
    "pattern_consistency": "high|medium|low"
  }},
  ...
]
```

Rules:
  - `member_request_ids` must have ≥2 items to emit a cluster. Clusters with 1 item are useless.
  - Only emit `high` consistency when the abstract intent is clearly identical across all members.
  - If uncertain, prefer `medium` over `high`.
"""


def cluster_sessions(sessions: list[SessionSummary], days: int) -> list[Cluster]:
    if len(sessions) < 3:
        return []
    call_gemini = _get_gemini()

    # Build compact session block — first user message + date + id per session
    block_lines = []
    for s in sessions:
        first_msg = (s.user_messages[0] if s.user_messages else "")[:300]
        block_lines.append(f"[{s.request_id}] {s.created_at[:10]}: {first_msg}")
    sessions_block = "\n".join(block_lines[:200])  # cap at 200 to stay within context

    prompt = CLUSTER_PROMPT.format(days=days, sessions_block=sessions_block)
    # 8k tokens — CJK clusters often exceed the 4k default and get truncated
    result = call_gemini(prompt, max_tokens=8192) or ""
    arr = _parse_cluster_json(result)
    if arr is None:
        return []
    out: list[Cluster] = []
    id_to_date = {s.request_id: s.created_at for s in sessions}
    for item in arr:
        if not isinstance(item, dict):
            continue
        members = item.get("member_request_ids") or []
        if not isinstance(members, list) or len(members) < 2:
            continue
        # Compute temporal span
        dates = []
        for mid in members:
            d = id_to_date.get(mid)
            if d:
                try:
                    dates.append(datetime.fromisoformat(d.replace("Z", "+00:00")))
                except Exception:
                    pass
        span_days = 0.0
        if len(dates) >= 2:
            span_days = (max(dates) - min(dates)).total_seconds() / 86400.0
        out.append(Cluster(
            theme=(item.get("theme") or "").strip()[:50],
            abstract_intent=(item.get("abstract_intent") or "").strip()[:500],
            member_request_ids=[m for m in members if m in id_to_date],
            temporal_span_days=round(span_days, 1),
            pattern_consistency=item.get("pattern_consistency") or "medium",
        ))
    return out


# ─── Proposal generation ─────────────────────────────────────────────────────
def score_cluster_confidence(cluster: Cluster) -> float:
    """f(size, span, consistency) → confidence in [0, 1]."""
    size_score = min(1.0, (len(cluster.member_request_ids) - 2) / 3.0)   # 3→0.33, 5+→1.0
    span_score = 0.5 if cluster.temporal_span_days <= 14 else 0.3        # compact → higher
    consistency_score = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(
        cluster.pattern_consistency, 0.5
    )
    # Weighted average
    return round(0.4 * size_score + 0.2 * span_score + 0.4 * consistency_score, 2)


def _derive_slug(theme: str, member_ids: list[str]) -> str:
    """Derive a stable slug for the proposal.

    CJK themes don't survive the ASCII-only `[a-z0-9\\-]+` filter, so fall back to
    a short hash of (theme + first member id) to keep proposals distinguishable
    on disk + in card headers.
    """
    ascii_slug = re.sub(r"[^a-z0-9\-]+", "-", theme.lower()).strip("-")[:30]
    if ascii_slug:
        return ascii_slug
    import hashlib
    seed = theme + "|" + (member_ids[0] if member_ids else "")
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    return f"cluster-{len(member_ids)}m-{digest}"


def build_proposals(clusters: list[Cluster], min_members: int = 3) -> list[ProactiveProposal]:
    out: list[ProactiveProposal] = []
    for c in clusters:
        if len(c.member_request_ids) < min_members:
            continue
        conf = score_cluster_confidence(c)
        slug = _derive_slug(c.theme, c.member_request_ids)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        proposal_id = f"proactive-{ts}-{slug[:30]}"
        out.append(ProactiveProposal(
            proposal_id=proposal_id,
            generated_at=ts,
            cluster=c,
            confidence=conf,
            suggested_skill_name=slug,
            rationale=(
                f"在 {c.temporal_span_days:.1f} 天内出现 {len(c.member_request_ids)} 次相同"
                f"抽象意图(一致性:{c.pattern_consistency})。建议沉淀为 Skill。"
            ),
        ))
    return out


# ─── Main flow ────────────────────────────────────────────────────────────────
def run_cross_session_analysis(days: int = 14, dry_run: bool = False, min_members: int = 3) -> list[ProactiveProposal]:
    sessions = scan_recent_sessions(days)
    if not sessions:
        return []
    clusters = cluster_sessions(sessions, days)
    proposals = build_proposals(clusters, min_members=min_members)

    if not dry_run and proposals:
        PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
        for p in proposals:
            target = PROPOSAL_DIR / f"{p.proposal_id}.json"
            target.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2))
    return proposals


def main():
    parser = argparse.ArgumentParser(description="Phase E cross-session pattern mining")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--min-members", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    proposals = run_cross_session_analysis(
        days=args.days, dry_run=args.dry_run, min_members=args.min_members
    )

    print(f"Scanned last {args.days} days. Produced {len(proposals)} proactive proposal(s).")
    for p in proposals:
        print(f"  • {p.proposal_id}  conf={p.confidence}  members={len(p.cluster.member_request_ids)}  "
              f"span={p.cluster.temporal_span_days}d  consistency={p.cluster.pattern_consistency}")
        print(f"    theme: {p.cluster.theme}")

    if args.output:
        Path(args.output).write_text(
            json.dumps([asdict(p) for p in proposals], ensure_ascii=False, indent=2)
        )
        print(f"all proposals written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
