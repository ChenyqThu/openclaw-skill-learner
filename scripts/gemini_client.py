"""
gemini_client.py — Shared Gemini API client for Skill Learner scripts.

Extracted from skill-learner-evaluate.py, eval-benchmark.py, darwin-optimize.py
to eliminate code duplication.

Functions:
  load_env()           — Load env vars from ~/.openclaw/.env
  call_gemini()        — Call Gemini API with prompt
  extract_eval_json()  — Parse ```eval_json``` block from response
  extract_skill_md()   — Parse ```skill_md``` block from response
"""

import json
import os
import re
from pathlib import Path

GEMINI_MODEL = "gemini-3-flash-preview"


def load_env():
    """Load environment variables from ~/.openclaw/.env (idempotent)."""
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


def call_gemini(
    prompt: str,
    model: str = GEMINI_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str | None:
    """Call Gemini API and return the text response, or None on error."""
    import urllib.request

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("NANO_BANANA_API_KEY")
    if not api_key:
        print("ERROR: No GEMINI_API_KEY found")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
    except Exception as e:
        print(f"ERROR: Gemini API failed: {e}")
        return None


def extract_eval_json(result: str) -> dict:
    """Extract the ```eval_json``` block from Gemini output and parse it."""
    m = re.search(r'```eval_json\s*\n(.*?)\n```', result, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def extract_skill_md(result: str) -> str:
    """Extract the ```skill_md``` block, falling back to the full result."""
    m = re.search(r'```skill_md\s*\n(.*?)\n```', result, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Legacy fallback: strip outer fences if present
    content = result.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
    if content.endswith("```"):
        content = content.rsplit("\n", 1)[0]
    return content
