#!/usr/bin/env python3
"""
Skill Learner Evaluate Server — Phase 2 real-time evaluation microservice.

Listens on localhost:8300.
POST /evaluate  — receives transcript summary from plugin, writes queue file,
                  triggers process_queue(), sends Feishu notification on match.
GET  /health    — returns {"status": "ok", "uptime": seconds, "evaluated": count}

Design:
  - stdlib only (http.server + threading + json + urllib)
  - Reuses process_queue() from skill-learner-evaluate.py (import)
  - Concurrent control via threading.Lock (one evaluation at a time)
  - Rate limit: max 5 Gemini calls/min across all requests
  - Feishu notification via `openclaw message send` CLI
  - Logs to ~/.openclaw/workspace/data/skill-learner/server.log
"""

import json
import base64
import os
import sys
import time
import threading
import subprocess
import logging
import random
import string
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".openclaw/workspace/data/skill-learner"
QUEUE_DIR = DATA_DIR / "analysis-queue"
LOG_FILE = DATA_DIR / "server.log"
SKIP_LIST_FILE = DATA_DIR / "skipped-skills.json"  # blacklist for skipped skill names
EVALUATE_SCRIPT = Path(__file__).parent / "skill-learner-evaluate.py"

# Add script dir to path so we can import skill-learner-evaluate and config
sys.path.insert(0, str(Path(__file__).parent))
from config import FEISHU_TARGET_OPEN_ID

# ─── Logging Setup ────────────────────────────────────────────────────────────
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [evaluate-server] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE)),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("evaluate-server")

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MB
RATE_LIMIT_PER_MIN = 5

# ─── State ────────────────────────────────────────────────────────────────────
start_time = time.time()
evaluated_count = 0

# Concurrency: one evaluation at a time
eval_lock = threading.Lock()

# Rate limiting: max 5 Gemini calls per minute
rate_lock = threading.Lock()
gemini_call_times = []  # timestamps of recent Gemini calls

# Notification threads tracking (for graceful shutdown)
_notification_threads = []
_notification_lock = threading.Lock()

# ─── Import evaluator ─────────────────────────────────────────────────────────
_evaluator_imported = False
_process_queue = None
_pending_review_path = None

def _import_evaluator():
    global _evaluator_imported, _process_queue, _pending_review_path
    if _evaluator_imported:
        return True
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("skill_learner_evaluate", str(EVALUATE_SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _process_queue = mod.process_queue
        _pending_review_path = mod.PENDING_REVIEW
        _evaluator_imported = True
        log.info(f"Imported evaluator from {EVALUATE_SCRIPT}")
        return True
    except Exception as e:
        log.error(f"Failed to import evaluator: {e}")
        return False

# ─── Rate Limiter ─────────────────────────────────────────────────────────────
def check_rate_limit() -> bool:
    """Return True if we can proceed (under rate limit), False if throttled."""
    now = time.time()
    with rate_lock:
        # Remove calls older than 60 seconds
        global gemini_call_times
        gemini_call_times = [t for t in gemini_call_times if now - t < 60]
        if len(gemini_call_times) >= RATE_LIMIT_PER_MIN:
            return False
        gemini_call_times.append(now)
        return True

# ─── Queue File Writer ────────────────────────────────────────────────────────
def write_queue_file(body: dict) -> str:
    """Write a queue-compatible JSON file and return its request ID."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    request_id = f"{int(time.time() * 1000)}-{random_suffix}"

    request = {
        "id": request_id,
        "sessionFile": body.get("sessionFile"),
        "createdAt": body.get("timestamp") or datetime.now().isoformat(),
        "toolCount": body.get("toolCount", 0),
        "toolNames": body.get("toolNames", []),
        "userMessages": body.get("userMessages", []),
        "assistantTexts": body.get("assistantTexts", []),
        "skillsUsed": body.get("skillsUsed", []),
        "runId": body.get("runId"),
        "agentId": body.get("agentId", "jarvis"),
        "sessionKey": body.get("sessionKey", ""),
        "sessionId": body.get("sessionId", ""),
        "status": "pending",
        "source": "evaluate-server",
    }

    req_file = QUEUE_DIR / f"{request_id}.json"
    req_file.write_text(json.dumps(request, indent=2))
    log.info(f"Queue file written: {request_id} ({request['toolCount']} tool calls)")
    return request_id

# ─── Feishu Notification ──────────────────────────────────────────────────────
def _get_feishu_token() -> str | None:
    """Fetch tenant_access_token from Feishu API using app credentials."""
    import urllib.request as ureq
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        log.warning("FEISHU_APP_ID or FEISHU_APP_SECRET not set")
        return None
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = ureq.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with ureq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("tenant_access_token")
    except Exception as e:
        log.warning(f"Failed to get Feishu token: {e}")
        return None


def _load_eval_data(skill_name: str, action: str) -> dict:
    """
    Load .eval.json for a skill candidate.
    For new skills: ~/.openclaw/workspace/skills/auto-learned/{name}/.eval.json
    For updates: scan all skills dirs for {name}/.eval.json
    Returns dict with eval fields (empty dict if not found).
    """
    from pathlib import Path as P
    if action == "create":
        p = P.home() / f".openclaw/workspace/skills/auto-learned/{skill_name}/.eval.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}
    else:
        all_skills_dir = P.home() / ".openclaw/workspace/skills"
        for skill_md in all_skills_dir.rglob("SKILL.md"):
            if skill_md.parent.name == skill_name:
                p = skill_md.parent / ".eval.json"
                if p.exists():
                    try:
                        return json.loads(p.read_text())
                    except Exception:
                        pass
        return {}


def send_feishu_notification(skill_name: str, action: str, tool_count: int,
                              agent_id: str, session_key: str,
                              last_inbound_message_id: str | None = None):
    """
    Send a Feishu interactive card DM to Lucien.
    Card: header with skill name, rich eval content, collapsed session details,
    three action buttons (approve / discuss / skip) + optimization input form.
    Replies to last_inbound_message_id if available.
    """
    import urllib.request as ureq

    action_label = "新建" if action == "create" else "更新"
    header_color = "orange" if action == "create" else "blue"

    # ── Load structured eval data ──────────────────────────────────────────────
    ev = _load_eval_data(skill_name, action)
    problem_context = ev.get("problem_context") or ev.get("problem") or "（Gemini 未返回结构化评估，请查阅草稿文件）"
    recommended_approach = ev.get("recommended_approach") or ev.get("approach") or ""
    when_to_use = ev.get("when_to_use") or []
    key_patterns = ev.get("key_patterns") or []
    pitfalls = ev.get("pitfalls") or ev.get("new_pitfalls") or []
    tool_names = ev.get("toolNames") or []

    def fmt_list(items, max_n=5):
        if not items:
            return "暂无"
        return "\n".join(f"- {i}" for i in items[:max_n])

    # Quality score display
    quality_data = ev.get("quality_score", {})
    quality_total = quality_data.get("total", 0) if isinstance(quality_data, dict) else 0

    short_session = session_key.split(":")[-1][:40] if session_key else "unknown"
    tool_names_str = ", ".join(tool_names[:8]) if tool_names else "暂无"

    main_content_lines = []
    if problem_context:
        main_content_lines.append(f"🔍 **问题发现**\n{problem_context}")
    if recommended_approach:
        main_content_lines.append(f"💡 **推荐方案**\n{recommended_approach}")
    main_content = "\n\n".join(main_content_lines) if main_content_lines else "（评估内容较少，请查阅草稿）"

    when_content = fmt_list(when_to_use, 5) if when_to_use else "请查阅 SKILL.md"
    patterns_and_pitfalls = ""
    if key_patterns:
        patterns_and_pitfalls += f"**关键模式**\n{fmt_list(key_patterns, 4)}\n\n"
    if pitfalls:
        patterns_and_pitfalls += f"**已知雷区**\n{fmt_list(pitfalls, 4)}"

    quality_line = f"\n**质量评分**：{quality_total}/100" if quality_total > 0 else ""
    detail_content = f"**来源**：{agent_id}\n**Session**：`{short_session}`\n**工具涉及**：{tool_count} 次\n**工具列表**：{tool_names_str}{quality_line}"

    # ── Build Card JSON (2.0) ───────────────────────────────────────────────────
    body_elements = [
        {"tag": "markdown", "content": main_content},
        {"tag": "markdown", "content": f"📋 **适用场景**\n{when_content}"},
    ]
    if patterns_and_pitfalls.strip():
        body_elements.append({"tag": "markdown", "content": patterns_and_pitfalls.strip()})

    # Collapsed session details (grey panel, OUTSIDE form — collapsible_panel cannot nest in form)
    body_elements.append({
        "tag": "collapsible_panel",
        "expanded": False,
        "background_color": "grey-50",
        "header": {
            "title": {"tag": "markdown", "content": "📎 **来源 & Session 详情**"},
            "background_color": "grey-100",
        },
        "border": {"color": "grey-200", "corner_radius": "8px"},
        "elements": [{"tag": "markdown", "content": detail_content, "text_size": "notation"}],
    })

    # Form with input + 3 buttons (Card 2.0)
    # Buttons use width="auto" for left-aligned natural sizing (not stretched)
    body_elements.append({
        "tag": "form",
        "name": "skill_action_form",
        "elements": [
            {
                "tag": "input",
                "name": "optimization_note",
                "input_type": "multiline_text",
                "label": {"tag": "plain_text", "content": "💬 优化建议（可选，点击「方案优化讨论」时带给 Jarvis）"},
                "label_position": "top",
                "placeholder": {"tag": "plain_text", "content": "输入优化建议，或对该 Skill 的想法..."},
                "rows": 3, "auto_resize": True, "max_rows": 10,
                "width": "fill",
            },
            # Metadata encoded in button name: "verb||base64(skill_name)||action"
            # Avoids disabled-but-visible hidden input; decoded in card_action callback
            {
                "tag": "column_set", "flex_mode": "none",
                "columns": [
                    {"tag": "column", "width": "auto", "elements": [{
                        "tag": "button", "type": "primary",
                        "name": f"approve||{base64.urlsafe_b64encode(skill_name.encode()).decode().rstrip('=')}||{action}",
                        "form_action_type": "submit",
                        "text": {"tag": "plain_text", "content": "✅ 通过落地"},
                        "confirm": {"title": {"tag": "plain_text", "content": "确认落地此 Skill？"},
                                    "text": {"tag": "plain_text", "content": f"将把「{skill_name}」从草稿移入正式 skills 目录"}},
                    }]},
                    {"tag": "column", "width": "auto", "elements": [{
                        "tag": "button", "type": "default",
                        "name": f"discuss||{base64.urlsafe_b64encode(skill_name.encode()).decode().rstrip('=')}||{action}",
                        "form_action_type": "submit",
                        "text": {"tag": "plain_text", "content": "💬 方案优化讨论"},
                    }]},
                    {"tag": "column", "width": "auto", "elements": [{
                        "tag": "button", "type": "danger",
                        "name": f"skip||{base64.urlsafe_b64encode(skill_name.encode()).decode().rstrip('=')}||{action}",
                        "form_action_type": "submit",
                        "text": {"tag": "plain_text", "content": "⏭ 跳过"},
                        "confirm": {"title": {"tag": "plain_text", "content": "确认跳过？"},
                                    "text": {"tag": "plain_text", "content": "将删除此 Skill 草稿，不可恢复"}},
                    }]},
                ],
            },
        ],
    })

    card = {
        "schema": "2.0",
        "config": {"width_mode": "fill"},
        "header": {
            "title": {"content": f"🧠 Skill 候选 · {action_label} · {skill_name}" + (f" · {quality_total}分" if quality_total > 0 else ""), "tag": "plain_text"},
            "template": header_color,
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": body_elements,
        },
    }
    # ── Send via openclaw CLI (avoids token management in launchd env) ──────────
    card_json = json.dumps(card)
    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", f"user:{FEISHU_TARGET_OPEN_ID}",
        "--card", card_json,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            log.info(f"Feishu card sent for skill: {skill_name} ({action_label})")
        else:
            log.warning(f"openclaw message send failed (rc={result.returncode}): {result.stderr.strip()}")
            _send_feishu_plain_fallback(skill_name, action_label, tool_count, agent_id, session_key)
    except Exception as e:
        log.warning(f"Card send exception: {e}")
        _send_feishu_plain_fallback(skill_name, action_label, tool_count, agent_id, session_key)

def _send_feishu_plain_fallback(skill_name: str, action_label: str, tool_count: int,
                                 agent_id: str, session_key: str):
    """Fallback: send plain text via openclaw CLI if card send fails."""
    message = (
        f"🧠 Skill 候选（{action_label}）\n"
        f"名称: {skill_name}\n"
        f"工具调用: {tool_count} 次 | 来源: {agent_id}\n\n"
        f"回复「通过 {skill_name}」落地，或「跳过」忽略"
    )
    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", "user:ou_8d1ce0fa1d435070ed695baeabe25adc",
        "--message", message,
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        log.info(f"Fallback plain text sent for skill: {skill_name}")
    except Exception as e:
        log.warning(f"Fallback notification failed: {e}")

# ─── Core Evaluate Handler ─────────────────────────────────────────────────────
def handle_evaluate(body: dict) -> dict:
    """
    Write queue file, call process_queue(), check for new skills, notify.
    Returns {"status": "ok"|"throttled"|"error", ...}
    """
    global evaluated_count

    # Rate limit check
    if not check_rate_limit():
        log.warning("Rate limit reached (5/min), dropping request")
        return {"status": "throttled", "message": "Rate limit: max 5/min"}

    # Write queue file
    try:
        request_id = write_queue_file(body)
    except Exception as e:
        log.error(f"Failed to write queue file: {e}")
        return {"status": "error", "message": str(e)}

    # Import evaluator
    if not _import_evaluator():
        return {"status": "error", "message": "Could not import skill-learner-evaluate.py"}

    # Run process_queue with lock (one at a time)
    if not eval_lock.acquire(blocking=False):
        log.info("Evaluation already in progress, queue file will be picked up by cron")
        return {"status": "queued", "requestId": request_id, "message": "Evaluation in progress, queued for cron"}

    try:
        log.info(f"Starting evaluation for request {request_id}")

        # Snapshot pending review file before evaluation
        pending_before = set()
        try:
            if _pending_review_path and _pending_review_path.exists():
                existing = json.loads(_pending_review_path.read_text())
                pending_before = {item.get("skillName") for item in existing}
        except Exception:
            pending_before = set()

        # Run evaluation
        _process_queue()
        evaluated_count += 1

        # Check what new skills/updates were created
        try:
            if _pending_review_path and _pending_review_path.exists():
                existing2 = json.loads(_pending_review_path.read_text())
                new_items = [item for item in existing2 if item.get("skillName") not in pending_before]
                # Load skip blacklist
                skip_list = set()
                try:
                    if SKIP_LIST_FILE.exists():
                        skip_list = set(json.loads(SKIP_LIST_FILE.read_text()))
                except Exception:
                    pass

                for item in new_items:
                    skill_name = item.get("skillName", "unknown")
                    action = item.get("action", "create")
                    # Skip if user previously rejected this skill name
                    if skill_name in skip_list:
                        log.info(f"Skipping notification for blacklisted skill: {skill_name}")
                        continue
                    # Quality-gate: only notify for quality >= 40 (low quality stored silently)
                    quality_score = item.get("qualityScore", 0)
                    if quality_score > 0 and quality_score < 40:
                        log.info(f"Quality too low ({quality_score}/100), silent store: {skill_name}")
                        continue
                    tool_count = item.get("toolCount", body.get("toolCount", 0))
                    agent_id = body.get("agentId", "jarvis")
                    session_key = body.get("sessionKey", "")
                    last_msg_id = item.get("lastInboundMessageId") or body.get("lastInboundMessageId")
                    # Send Feishu notification in tracked thread
                    t = threading.Thread(
                        target=send_feishu_notification,
                        args=(skill_name, action, tool_count, agent_id, session_key, last_msg_id),
                    )
                    t.start()
                    with _notification_lock:
                        _notification_threads.append(t)
        except Exception as e:
            log.warning(f"Could not check pending review for notifications: {e}")

        log.info(f"Evaluation complete for request {request_id}")
        return {"status": "ok", "requestId": request_id, "evaluated": evaluated_count}

    except Exception as e:
        log.error(f"Evaluation error: {e}", exc_info=True)
        return {"status": "error", "requestId": request_id, "message": str(e)}
    finally:
        eval_lock.release()

# ─── HTTP Request Handler ─────────────────────────────────────────────────────
class EvaluateHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Route access log to our logger
        log.debug(f"{self.address_string()} - {format % args}")

    def send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            uptime = int(time.time() - start_time)
            with rate_lock:
                rate_used = len([t for t in gemini_call_times if time.time() - t < 60])
            self.send_json(200, {
                "status": "ok",
                "uptime": uptime,
                "evaluated": evaluated_count,
                "rateLimitUsed": rate_used,
                "rateLimitMax": RATE_LIMIT_PER_MIN,
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/evaluate":
            self.send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_json(400, {"error": "empty body"})
            return
        if content_length > MAX_BODY_SIZE:
            self.send_json(413, {"error": f"body too large ({content_length} > {MAX_BODY_SIZE})"})
            return

        try:
            raw = self.rfile.read(content_length)
            body = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": f"invalid JSON: {e}"})
            return

        # Validate field types
        tool_count = body.get("toolCount", 0)
        if not isinstance(tool_count, (int, float)):
            self.send_json(400, {"error": "toolCount must be a number"})
            return
        tool_count = int(tool_count)
        if not isinstance(body.get("toolNames", []), list):
            self.send_json(400, {"error": "toolNames must be an array"})
            return

        log.info(f"POST /evaluate: runId={body.get('runId')} toolCount={tool_count} agentId={body.get('agentId')}")

        # Run in background thread so we can return immediately
        def run_async():
            result = handle_evaluate(body)
            log.info(f"Async evaluation result: {result}")

        threading.Thread(target=run_async, daemon=True).start()

        # Return 202 immediately (fire-and-forget from plugin side)
        self.send_json(202, {"status": "accepted", "toolCount": tool_count})

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Load env vars from ~/.openclaw/.env
    env_file = Path.home() / ".openclaw/.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                if line.startswith("export "):
                    line = line[7:]
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)
        log.info(f"Loaded env from {env_file}")

    server = HTTPServer(("127.0.0.1", 8300), EvaluateHandler)

    def graceful_shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        # Wait for in-flight notification threads
        with _notification_lock:
            threads = list(_notification_threads)
        for t in threads:
            t.join(timeout=5)
        server.shutdown()

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    log.info("Skill Learner Evaluate Server started on http://127.0.0.1:8300")
    log.info(f"Evaluator script: {EVALUATE_SCRIPT}")
    log.info(f"Queue dir: {QUEUE_DIR}")
    log.info(f"Log file: {LOG_FILE}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Server stopped.")

if __name__ == "__main__":
    main()
