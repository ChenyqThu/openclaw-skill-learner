#!/usr/bin/env python3
"""
State Arc Analyzer — 7-day trend analysis for Jarvis weekly review.

Aggregates data from 5 sources:
  1. Notion Calendar (meeting density, deep work time)
  2. Health data (HRV, workouts) from GitHub
  3. Memory files (WAL frequency, score distribution, preference signals)
  4. Tool usage stats (from Skill Learner)
  5. Skill learning rate (analysis queue)

Output: JSON report with signals, health trends, cognitive activity,
        and user profile update candidates.

Usage:
  python3 state-arc-analyzer.py                  # Analyze last 7 days
  python3 state-arc-analyzer.py --days 14        # Custom range
  python3 state-arc-analyzer.py --json           # JSON only (for piping)
  python3 state-arc-analyzer.py --dry-run        # Preview without side effects
"""

import json
import os
import re
import sys
import glob
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add script dir to path for config import
sys.path.insert(0, str(Path(__file__).parent))
from config import (
    NOTION_CALENDAR_DB, HEALTH_GITHUB_REPO,
    WORKSPACE, DATA_DIR, QUEUE_DIR, MEMORY_MD,
)

# ─── Configuration ────────────────────────────────────────────────────────────
DAYS = 7
for i, arg in enumerate(sys.argv):
    if arg == "--days" and i + 1 < len(sys.argv):
        DAYS = int(sys.argv[i + 1])

JSON_ONLY = "--json" in sys.argv
DRY_RUN = "--dry-run" in sys.argv

MEMORY_DIR = WORKSPACE / "memory"
SKILL_LEARNER_DIR = DATA_DIR
TOOL_STATS_FILE = SKILL_LEARNER_DIR / "tool-usage-stats.json"
ANALYSIS_QUEUE_DIR = QUEUE_DIR
MEMORY_HEALTH_FILE = SKILL_LEARNER_DIR / "memory-health.json"

# Timezone
TZ_PT = timezone(timedelta(hours=-7))  # PDT

# ─── Date Range ───────────────────────────────────────────────────────────────
now = datetime.now(TZ_PT)
end_date = now.date()
start_date = end_date - timedelta(days=DAYS)
date_range = [start_date + timedelta(days=i) for i in range(DAYS + 1)]
date_strs = [d.isoformat() for d in date_range]


def log(msg):
    if not JSON_ONLY:
        print(f"  {msg}")


# ─── 1. Notion Calendar ──────────────────────────────────────────────────────

def fetch_calendar_data():
    """Fetch calendar events from Notion for the date range."""
    log("📅 Fetching Notion calendar data...")
    try:
        wide_start = str(start_date - timedelta(days=1))
        wide_end = str(end_date + timedelta(days=2))
        
        filter_payload = json.dumps({
            "and": [
                {"property": "Time", "date": {"on_or_after": wide_start}},
                {"property": "Time", "date": {"before": wide_end}}
            ]
        })
        
        # Use ntn CLI with --notion-version 2022-06-28
        # (default 2026-03-11 returns 400 on this DB; pinned until Notion fixes)
        notion_token = os.environ.get("MAILAGENT_NOTION_TOKEN", "")
        if not notion_token:
            log("  ⚠️ MAILAGENT_NOTION_TOKEN not set, skipping calendar")
            return None
        
        query_body = json.dumps({
            "filter": json.loads(filter_payload),
            "sorts": [{"property": "Time", "direction": "ascending"}],
            "page_size": 100
        })
        
        env = {**os.environ, "NOTION_API_TOKEN": notion_token}
        result = subprocess.run(
            ["ntn", "api", "--notion-version", "2022-06-28",
             f"/v1/databases/{NOTION_CALENDAR_DB}/query",
             "-d", query_body],
            capture_output=True, text=True, timeout=30, env=env
        )
        
        if result.returncode != 0:
            log(f"  ⚠️ ntn query failed: {result.stderr[:200]}")
            return None
        
        data = json.loads(result.stdout)
        events = data.get("results", [])
        
        meetings = 0
        focus_blocks = 0
        total_events = len(events)
        
        for event in events:
            props = event.get("properties", {})
            # 日程类型 is a select property
            schedule_type = ""
            type_prop = props.get("日程类型", {})
            if type_prop.get("select"):
                schedule_type = type_prop["select"].get("name", "")
            
            if "会议" in schedule_type:
                meetings += 1
            elif "专注" in schedule_type:
                focus_blocks += 1
        
        meeting_ratio = meetings / total_events if total_events > 0 else 0
        focus_ratio = focus_blocks / total_events if total_events > 0 else 0
        
        log(f"  Found {total_events} events: {meetings} meetings, {focus_blocks} focus blocks")
        
        return {
            "total_events": total_events,
            "meetings": meetings,
            "focus_blocks": focus_blocks,
            "meeting_ratio": round(meeting_ratio, 2),
            "focus_ratio": round(focus_ratio, 2),
        }
    except Exception as e:
        log(f"  ⚠️ Calendar fetch error: {e}")
        return None


# ─── 2. Health Data ───────────────────────────────────────────────────────────

def fetch_health_data():
    """Fetch health metrics from GitHub."""
    log("❤️ Fetching health data...")
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{HEALTH_GITHUB_REPO}/contents/latest.json",
             "--jq", ".content"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log(f"  ⚠️ GitHub API failed: {result.stderr[:200]}")
            return None
        
        import base64
        content = base64.b64decode(result.stdout.strip()).decode("utf-8")
        data = json.loads(content)
        
        metrics_list = data.get("data", {}).get("metrics", [])
        workouts = data.get("data", {}).get("workouts", [])
        
        hrv_values = []
        spo2_values = []
        
        for metric in metrics_list:
            name = metric.get("name", "")
            entries = metric.get("data", [])
            
            if name == "heart_rate_variability":
                for entry in entries:
                    try:
                        hrv_values.append(float(entry.get("qty", 0)))
                    except (ValueError, TypeError):
                        pass
            elif name == "blood_oxygen_saturation":
                for entry in entries:
                    try:
                        spo2_values.append(float(entry.get("qty", 0)))
                    except (ValueError, TypeError):
                        pass
        
        hrv_avg = round(sum(hrv_values) / len(hrv_values), 1) if hrv_values else None
        hrv_trend = "stable"
        if len(hrv_values) >= 3:
            first_half = sum(hrv_values[:len(hrv_values)//2]) / (len(hrv_values)//2)
            second_half = sum(hrv_values[len(hrv_values)//2:]) / (len(hrv_values) - len(hrv_values)//2)
            if second_half < first_half * 0.85:
                hrv_trend = "declining"
            elif second_half > first_half * 1.15:
                hrv_trend = "improving"
        
        workout_count = len(workouts)
        
        log(f"  HRV: avg={hrv_avg}ms, trend={hrv_trend}, workouts={workout_count}")
        
        return {
            "hrv_7d_avg": hrv_avg,
            "hrv_trend": hrv_trend,
            "hrv_data_points": len(hrv_values),
            "spo2_avg": round(sum(spo2_values) / len(spo2_values), 1) if spo2_values else None,
            "workout_count": workout_count,
        }
    except Exception as e:
        log(f"  ⚠️ Health data error: {e}")
        return None


# ─── 3. Memory Analysis ──────────────────────────────────────────────────────

# User modeling signal patterns
# Only match lines that look like Lucien's direct speech (contain quotes or diary-style markers)
PREFERENCE_PATTERNS = [
    (r"(?:Lucien|用户).*(?:我更喜欢|我偏好|以后都|以后用)", "preference"),
    (r"(?:Lucien|用户).*(?:我不喜欢|以后别|不要再)", "negative_preference"),
    (r"(?:Lucien|用户).*(?:记住|下次记得|以后都要)", "instruction"),
    (r"(?:Lucien|用户).*(?:不是.*而是|应该用.*不是)", "correction"),
    (r"(?:Lucien|用户).*(?:我现在觉得|想法变了|不再认为)", "stance_change"),
    (r"(?:Lucien|反馈).*(?:新项目|开始负责|接手了)", "role_change"),
]


def analyze_memory_files():
    """Analyze memory files for the date range."""
    log("🧠 Analyzing memory files...")
    
    total_lines = 0
    total_entries = 0
    high_score_entries = 0  # [10+]
    preference_signals = []
    files_found = 0
    
    for d in date_range:
        date_str = d.isoformat()
        # Match both YYYY-MM-DD.md and YYYY-MM-DD-*.md
        pattern = str(MEMORY_DIR / f"{date_str}*.md")
        matched = glob.glob(pattern)
        
        for filepath in matched:
            files_found += 1
            try:
                content = Path(filepath).read_text(encoding="utf-8")
                lines = content.split("\n")
                total_lines += len(lines)
                
                # Count scored entries
                for line in lines:
                    score_match = re.search(r'\[(\d+)\]', line)
                    if score_match:
                        total_entries += 1
                        score = int(score_match.group(1))
                        if score >= 10:
                            high_score_entries += 1
                
                # Scan for preference signals
                for line_num, line in enumerate(lines, 1):
                    for pattern_re, signal_type in PREFERENCE_PATTERNS:
                        if re.search(pattern_re, line):
                            # Extract context (the line + surrounding)
                            context = line.strip()[:200]
                            if context and not context.startswith("#"):
                                preference_signals.append({
                                    "source": f"{Path(filepath).name}:L{line_num}",
                                    "type": signal_type,
                                    "text": context,
                                })
                            break  # One signal per line
            except Exception:
                continue
    
    # MEMORY.md health
    memory_health = None
    try:
        if MEMORY_HEALTH_FILE.exists():
            memory_health = json.loads(MEMORY_HEALTH_FILE.read_text())
    except Exception:
        pass
    
    if not memory_health:
        try:
            content = MEMORY_MD.read_text(encoding="utf-8")
            memory_health = {
                "memoryLines": len(content.split("\n")),
                "memoryChars": len(content),
                "status": "healthy",
            }
            if memory_health["memoryLines"] > 300:
                memory_health["status"] = "danger"
            elif memory_health["memoryLines"] > 250:
                memory_health["status"] = "warning"
        except Exception:
            pass
    
    high_score_ratio = round(high_score_entries / total_entries, 2) if total_entries > 0 else 0
    
    # Deduplicate preference signals (same text)
    seen_texts = set()
    unique_signals = []
    for sig in preference_signals:
        short = sig["text"][:80]
        if short not in seen_texts:
            seen_texts.add(short)
            unique_signals.append(sig)
    
    log(f"  {files_found} files, {total_lines} lines, {total_entries} scored entries ({high_score_entries} high-score)")
    log(f"  {len(unique_signals)} preference signals detected")
    
    return {
        "files_found": files_found,
        "total_lines": total_lines,
        "total_entries": total_entries,
        "high_score_entries": high_score_entries,
        "high_score_ratio": high_score_ratio,
        "preference_signals": unique_signals[:10],  # Cap at 10
        "memory_health": memory_health,
    }


# ─── 4. Tool Usage Stats ─────────────────────────────────────────────────────

def analyze_tool_stats():
    """Analyze tool usage statistics."""
    log("🔧 Analyzing tool usage stats...")
    
    if not TOOL_STATS_FILE.exists():
        log("  No tool stats file yet")
        return None
    
    try:
        all_stats = json.loads(TOOL_STATS_FILE.read_text())
        
        period_stats = {}
        total_calls = 0
        total_errors = 0
        
        for d in date_range:
            date_str = d.isoformat()
            if date_str in all_stats:
                day_stats = all_stats[date_str]
                for tool, counts in day_stats.items():
                    if tool not in period_stats:
                        period_stats[tool] = {"calls": 0, "errors": 0}
                    period_stats[tool]["calls"] += counts.get("calls", 0)
                    period_stats[tool]["errors"] += counts.get("errors", 0)
                    total_calls += counts.get("calls", 0)
                    total_errors += counts.get("errors", 0)
        
        # Top tools
        top_tools = sorted(period_stats.items(), key=lambda x: x[1]["calls"], reverse=True)[:10]
        
        log(f"  {total_calls} total calls, {total_errors} errors, {len(period_stats)} unique tools")
        
        return {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "unique_tools": len(period_stats),
            "top_tools": [{
                "name": name,
                "calls": stats["calls"],
                "errors": stats["errors"],
            } for name, stats in top_tools],
        }
    except Exception as e:
        log(f"  ⚠️ Tool stats error: {e}")
        return None


# ─── 5. Skill Learning Rate ──────────────────────────────────────────────────

def analyze_skill_learning():
    """Analyze skill learning queue activity."""
    log("📚 Analyzing skill learning rate...")
    
    if not ANALYSIS_QUEUE_DIR.exists():
        log("  No analysis queue directory")
        return {"evaluations": 0, "new_skills": 0, "updates": 0}
    
    evaluations = 0
    new_skills = 0
    updates = 0
    
    try:
        for f in ANALYSIS_QUEUE_DIR.glob("*.json"):
            try:
                req = json.loads(f.read_text())
                created = req.get("createdAt", "")
                if created:
                    created_date = datetime.fromisoformat(created).date()
                    if start_date <= created_date <= end_date:
                        evaluations += 1
                        status = req.get("status", "")
                        if status == "completed":
                            new_skills += 1
                        elif status == "update_proposed":
                            updates += 1
            except Exception:
                continue
    except Exception:
        pass
    
    log(f"  {evaluations} evaluations, {new_skills} new skills, {updates} updates")
    
    return {
        "evaluations": evaluations,
        "new_skills": new_skills,
        "updates": updates,
    }


# ─── Signal Detection ────────────────────────────────────────────────────────

def detect_signals(calendar, health, memory, tools, skills):
    """Detect anomalies and generate signals."""
    signals = []
    
    # Creative time signals
    if calendar:
        if calendar["meeting_ratio"] > 0.6:
            signals.append({
                "category": "creative_time",
                "level": "warning",
                "message": f"本周会议占比 {calendar['meeting_ratio']*100:.0f}%，挤压了创造者时间",
                "data": {"meeting_ratio": calendar["meeting_ratio"]},
            })
        if calendar["focus_ratio"] < 0.2 and calendar["total_events"] > 5:
            signals.append({
                "category": "creative_time",
                "level": "alert" if calendar["focus_ratio"] < 0.1 else "warning",
                "message": f"深度工作时间仅占 {calendar['focus_ratio']*100:.0f}%，建议保护上午时段",
                "data": {"focus_ratio": calendar["focus_ratio"]},
            })
    
    # Health signals
    if health:
        if health.get("hrv_trend") == "declining":
            signals.append({
                "category": "health",
                "level": "warning",
                "message": f"HRV 呈下降趋势（均值 {health['hrv_7d_avg']}ms），注意恢复",
                "data": {"hrv_avg": health["hrv_7d_avg"], "trend": "declining"},
            })
        if health.get("workout_count", 0) < 2:
            signals.append({
                "category": "health",
                "level": "info",
                "message": f"本周运动 {health['workout_count']} 次，低于常规频率",
                "data": {"workout_count": health["workout_count"]},
            })
    
    # Memory health
    if memory and memory.get("memory_health"):
        mh = memory["memory_health"]
        if mh.get("status") == "danger":
            signals.append({
                "category": "memory",
                "level": "warning",
                "message": f"MEMORY.md 已达 {mh.get('memoryLines', '?')} 行，需要整理",
                "data": {"lines": mh.get("memoryLines")},
            })
    
    # Cognitive activity
    if memory and memory.get("total_lines", 0) < 50 and DAYS >= 7:
        signals.append({
            "category": "cognitive",
            "level": "info",
            "message": "本周记忆写入偏少，Jarvis 交互可能减少",
            "data": {"total_lines": memory["total_lines"]},
        })

    # Tool usage signals
    if tools and tools.get("total_calls", 0) > 0:
        total_calls = tools["total_calls"]
        total_errors = tools["total_errors"]
        overall_error_rate = total_errors / total_calls if total_calls else 0

        if overall_error_rate > 0.1:
            signals.append({
                "category": "tool_quality",
                "level": "warning",
                "message": f"工具整体错误率 {overall_error_rate*100:.1f}%（{total_errors}/{total_calls}），建议排查",
                "data": {"error_rate": round(overall_error_rate, 3), "total_errors": total_errors},
            })

        # Flag individual high-error-rate tools
        for tool_info in tools.get("top_tools", []):
            t_calls = tool_info["calls"]
            t_errors = tool_info["errors"]
            if t_calls >= 5 and t_errors / t_calls > 0.15:
                signals.append({
                    "category": "tool_quality",
                    "level": "info",
                    "message": f"工具 {tool_info['name']} 错误率 {t_errors/t_calls*100:.0f}%（{t_errors}/{t_calls}）",
                    "data": {"tool": tool_info["name"], "error_rate": round(t_errors / t_calls, 3)},
                })

    # Skill learning rate signals
    if skills:
        if skills.get("evaluations", 0) > 0 and skills.get("new_skills", 0) == 0 and skills.get("updates", 0) == 0:
            signals.append({
                "category": "skill_learning",
                "level": "info",
                "message": f"本周 {skills['evaluations']} 次评估但无新技能产出，提示词可能偏严或会话复杂度不够",
                "data": skills,
            })
        if skills.get("new_skills", 0) >= 3:
            signals.append({
                "category": "skill_learning",
                "level": "info",
                "message": f"本周新增 {skills['new_skills']} 个技能草稿，学习效率良好",
                "data": skills,
            })

    # Overall level
    levels = [s["level"] for s in signals]
    if "alert" in levels:
        overall = "alert"
    elif "warning" in levels:
        overall = "warning"
    else:
        overall = "normal"
    
    return signals, overall


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not JSON_ONLY:
        print(f"\n🔭 State Arc Analysis: {start_date} ~ {end_date} ({DAYS} days)\n")
    
    calendar = fetch_calendar_data()
    health = fetch_health_data()
    memory = analyze_memory_files()
    tools = analyze_tool_stats()
    skills = analyze_skill_learning()
    
    signals, overall = detect_signals(calendar, health, memory, tools, skills)
    
    # User profile candidates from memory preference signals
    profile_candidates = []
    if memory and memory.get("preference_signals"):
        for sig in memory["preference_signals"]:
            profile_candidates.append({
                "source": sig["source"],
                "signal_type": sig["type"],
                "text": sig["text"],
                "proposed_update": f"Review if this should update USER.md: {sig['text'][:80]}",
            })
    
    report = {
        "period": f"{start_date} ~ {end_date}",
        "days": DAYS,
        "generated_at": datetime.now(TZ_PT).isoformat(),
        "overall": overall,
        "signals": signals,
        "calendar": calendar,
        "health_trend": health,
        "cognitive_activity": {
            "memory_files": memory["files_found"] if memory else 0,
            "memory_lines": memory["total_lines"] if memory else 0,
            "memory_entries": memory["total_entries"] if memory else 0,
            "high_score_ratio": memory["high_score_ratio"] if memory else 0,
            "skill_evaluations": skills["evaluations"] if skills else 0,
            "new_skills": skills["new_skills"] if skills else 0,
        },
        "tool_usage": tools,
        "user_profile_candidates": profile_candidates,
        "memory_health": memory.get("memory_health") if memory else None,
    }
    
    # Write report
    if not DRY_RUN:
        output_dir = SKILL_LEARNER_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "state-arc-latest.json"
        output_file.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        if not JSON_ONLY:
            log(f"\n📄 Report written to {output_file}")
    
    if JSON_ONLY:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'='*50}")
        print(f"Overall: {overall.upper()}")
        print(f"Signals: {len(signals)}")
        for s in signals:
            icon = "🔴" if s["level"] == "alert" else "🟡" if s["level"] == "warning" else "ℹ️"
            print(f"  {icon} [{s['category']}] {s['message']}")
        if profile_candidates:
            print(f"\nUser Profile Candidates: {len(profile_candidates)}")
            for c in profile_candidates[:5]:
                print(f"  📌 [{c['signal_type']}] {c['text'][:80]}")
        print()
    
    return report


if __name__ == "__main__":
    main()
