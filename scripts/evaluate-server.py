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
import os
import sys
import time
import threading
import subprocess
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import datetime

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".openclaw/workspace/data/skill-learner"
QUEUE_DIR = DATA_DIR / "analysis-queue"
LOG_FILE = DATA_DIR / "server.log"
EVALUATE_SCRIPT = Path(__file__).parent / "skill-learner-evaluate.py"

# Add script dir to path so we can import skill-learner-evaluate
sys.path.insert(0, str(Path(__file__).parent))

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

# ─── State ────────────────────────────────────────────────────────────────────
start_time = time.time()
evaluated_count = 0

# Concurrency: one evaluation at a time
eval_lock = threading.Lock()

# Rate limiting: max 5 Gemini calls per minute
rate_lock = threading.Lock()
gemini_call_times = []  # timestamps of recent Gemini calls
RATE_LIMIT_PER_MIN = 5

# ─── Import evaluator ─────────────────────────────────────────────────────────
_evaluator_imported = False
_process_queue = None

def _import_evaluator():
    global _evaluator_imported, _process_queue
    if _evaluator_imported:
        return True
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("skill_learner_evaluate", str(EVALUATE_SCRIPT))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _process_queue = mod.process_queue
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
    import random
    import string
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
def send_feishu_notification(skill_name: str, action: str, tool_count: int,
                              agent_id: str, session_key: str):
    """
    Send a Feishu DM to Lucien with Skill update candidate info.
    Uses openclaw message send CLI (fire-and-forget via subprocess).
    """
    action_label = "新建" if action == "create" else "更新"
    short_session = session_key.split(":")[-1][:20] if session_key else "unknown"

    message = (
        f"🧠 Skill 更新候选\n"
        f"名称: {skill_name}\n"
        f"动作: {action_label}\n"
        f"工具调用: {tool_count} 次\n"
        f"来源: {agent_id} / {session_key}\n\n"
        f"回复「通过 {skill_name}」落地此 Skill，或「跳过」忽略"
    )

    cmd = [
        "openclaw", "message", "send",
        "--channel", "feishu",
        "--target", "user:ou_8d1ce0fa1d435070ed695baeabe25adc",
        "--message", message,
    ]

    try:
        # Fire and forget — don't block the response
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info(f"Feishu notification fired for skill: {skill_name} ({action_label})")
    except Exception as e:
        log.warning(f"Feishu notification failed: {e}")

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
            import importlib.util
            spec = importlib.util.spec_from_file_location("skill_learner_evaluate", str(EVALUATE_SCRIPT))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            pending_review_path = mod.PENDING_REVIEW
            if pending_review_path.exists():
                existing = json.loads(pending_review_path.read_text())
                pending_before = {item.get("skillName") for item in existing}
        except Exception:
            pending_before = set()

        # Run evaluation
        _process_queue()
        evaluated_count += 1

        # Check what new skills/updates were created
        try:
            spec2 = importlib.util.spec_from_file_location("skill_learner_evaluate2", str(EVALUATE_SCRIPT))
            mod2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(mod2)
            pending_review_path2 = mod2.PENDING_REVIEW
            if pending_review_path2.exists():
                existing2 = json.loads(pending_review_path2.read_text())
                new_items = [item for item in existing2 if item.get("skillName") not in pending_before]
                for item in new_items:
                    skill_name = item.get("skillName", "unknown")
                    action = item.get("action", "create")
                    tool_count = item.get("toolCount", body.get("toolCount", 0))
                    agent_id = body.get("agentId", "jarvis")
                    session_key = body.get("sessionKey", "")
                    # Send Feishu notification in background thread
                    threading.Thread(
                        target=send_feishu_notification,
                        args=(skill_name, action, tool_count, agent_id, session_key),
                        daemon=True,
                    ).start()
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
            self.send_json(200, {
                "status": "ok",
                "uptime": uptime,
                "evaluated": evaluated_count,
                "rateLimitUsed": len([t for t in gemini_call_times if time.time() - t < 60]),
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

        try:
            raw = self.rfile.read(content_length)
            body = json.loads(raw)
        except Exception as e:
            self.send_json(400, {"error": f"invalid JSON: {e}"})
            return

        # Validate minimum fields
        tool_count = body.get("toolCount", 0)
        if tool_count < 5:
            self.send_json(200, {"status": "skipped", "reason": f"toolCount={tool_count} < 5"})
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
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
        log.info(f"Loaded env from {env_file}")

    server = HTTPServer(("127.0.0.1", 8300), EvaluateHandler)
    log.info("Skill Learner Evaluate Server started on http://127.0.0.1:8300")
    log.info(f"Evaluator script: {EVALUATE_SCRIPT}")
    log.info(f"Queue dir: {QUEUE_DIR}")
    log.info(f"Log file: {LOG_FILE}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Server stopped.")

if __name__ == "__main__":
    main()
