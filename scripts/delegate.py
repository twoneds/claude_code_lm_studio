#!/usr/bin/env python3
"""
delegate.py — LM Studio delegation script for local-delegate skill.

Usage:
  python delegate.py models                          # Refresh and print model table
  python delegate.py route <task_json>               # Analyse task, return routing decision
  python delegate.py call <model_id> <prompt>        # Call a specific model, return response
  python delegate.py run <task_json>                 # Full: route + call + retry logic

task_json fields:
  prompt        str   The task prompt
  type          str   "code" | "reasoning" | "classify" | "general"  (optional, aids routing)
  complexity    int   1-5 override (optional, skips analysis)
  max_retries   int   Default 2. After exhausting, sets escalate=true in output.
"""

import json
import sys
import os
import time
import urllib.request
import urllib.error
from typing import Optional

LM_STUDIO_URL = "http://localhost:1234/v1"
MODELS_CACHE_PATH = os.path.expanduser("~/.claude/skills/local-delegate/model_cache.json")

# Routing table — updated at session start from live /v1/models
# Defines candidates per role; first available model wins.
ROUTING_TABLE = {
    "triage": {
        "candidates": ["qwen/qwen3-4b-2507", "qwen/qwen3-1.7b", "qwen3-0.6b"],
        "max_tokens": 200,
        "description": "Binary classify / pre-filter"
    },
    "classify": {
        "candidates": ["qwen/qwen3-4b-2507", "qwen/qwen3-1.7b"],
        "max_tokens": 400,
        "description": "JSON extraction, instruction following"
    },
    "fast_code": {
        "candidates": ["meta-llama-3.1-8b-instruct", "qwen3-0.6b-coders"],
        "max_tokens": 2048,
        "description": "Sub-task code generation, low latency"
    },
    "fast_reasoning": {
        "candidates": ["qwen/qwen3-4b-thinking-2507", "qwen/qwen3-4b-2507"],
        "max_tokens": 1024,
        "description": "Reasoning, math, edge calculation"
    },
    "code": {
        "candidates": ["qwen/qwen3-coder-next", "qwen/qwen3-coder-30b"],
        "max_tokens": 8192,
        "description": "Complex multi-file / architecture code"
    },
    "reasoning": {
        "candidates": ["qwen/qwen3-30b-a3b-2507", "qwen/qwen3-coder-30b"],
        "max_tokens": 8192,
        "description": "Complex reasoning, novel decisions"
    },
    "general": {
        "candidates": ["google/gemma-3-12b", "qwen/qwen3-30b-a3b-2507"],
        "max_tokens": 4096,
        "description": "General tasks not fitting other roles"
    }
}

# Complexity → role mapping
# Complexity 1-2: fast local models
# Complexity 3:   mid-tier local models
# Complexity 4-5: large local models (escalate to Claude if they fail)
COMPLEXITY_ROLE = {
    1: "classify",
    2: "fast_code",      # overridden to fast_reasoning if type==reasoning
    3: "fast_reasoning", # overridden to fast_code if type==code
    4: "code",           # overridden to reasoning if type==reasoning
    5: "reasoning",      # overridden to code if type==code
}


def _http(method: str, path: str, body: Optional[dict] = None, timeout: int = 120) -> dict:
    url = f"{LM_STUDIO_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise ConnectionError(f"LM Studio unreachable at {LM_STUDIO_URL}: {e}") from e


def check_health() -> bool:
    try:
        _http("GET", "/models", timeout=4)
        return True
    except ConnectionError:
        return False


def fetch_models() -> list[str]:
    """Fetch live model list from LM Studio, update cache, return IDs."""
    data = _http("GET", "/models")
    ids = [m["id"] for m in data.get("data", []) if m.get("object") == "model"
           and not m["id"].startswith("text-embedding")
           and not m["id"].startswith("mlx-community")]
    os.makedirs(os.path.dirname(MODELS_CACHE_PATH), exist_ok=True)
    with open(MODELS_CACHE_PATH, "w") as f:
        json.dump({"models": ids, "updated": time.time()}, f, indent=2)
    return ids


def load_cached_models() -> list[str]:
    if not os.path.exists(MODELS_CACHE_PATH):
        return []
    with open(MODELS_CACHE_PATH) as f:
        return json.load(f).get("models", [])


def resolve_model(role: str, available: list[str]) -> Optional[str]:
    """Return first candidate for role that is in available models."""
    for candidate in ROUTING_TABLE.get(role, {}).get("candidates", []):
        if candidate in available:
            return candidate
    return None


def analyse_complexity(prompt: str, available: list[str]) -> int:
    """
    Use the triage model to score prompt complexity 1-5.
    Falls back to 3 if triage model unavailable or call fails.
    """
    triage_model = resolve_model("triage", available)
    if not triage_model:
        return 3

    system = (
        "You are a task complexity classifier. "
        "Reply with ONLY a single digit 1-5 where:\n"
        "1 = trivial (rename, lookup, one-liner)\n"
        "2 = simple (small function, single-file edit, clear spec)\n"
        "3 = moderate (multi-step, requires context, some reasoning)\n"
        "4 = complex (multi-file, architecture decisions, debugging)\n"
        "5 = expert (novel reasoning, cross-module, ambiguous spec)\n"
        "No explanation. Just the digit."
    )
    try:
        resp = _http("POST", "/chat/completions", {
            "model": triage_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt[:800]}  # truncate for speed
            ],
            "max_tokens": 5,
            "temperature": 0
        }, timeout=15)
        text = resp["choices"][0]["message"]["content"].strip()
        digit = int(text[0])
        return digit if 1 <= digit <= 5 else 3
    except Exception:
        return 3


def determine_role(complexity: int, task_type: Optional[str]) -> str:
    base_role = COMPLEXITY_ROLE.get(complexity, "general")
    if not task_type:
        return base_role
    # Override based on explicit type
    if task_type == "code" and complexity >= 4:
        return "code"
    if task_type == "code" and complexity == 3:
        return "fast_code"
    if task_type == "reasoning" and complexity >= 4:
        return "reasoning"
    if task_type == "reasoning" and complexity <= 2:
        return "fast_reasoning"
    if task_type == "classify":
        return "classify"
    return base_role


def call_model(model_id: str, prompt: str, max_tokens: int = 2048, system: str = "") -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = _http("POST", "/chat/completions", {
        "model": model_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3
    })
    return resp["choices"][0]["message"]["content"]


def run_with_retry(task: dict, available: list[str]) -> dict:
    """
    Route task, call local model, retry up to max_retries.
    On exhaustion sets escalate=true for Claude cloud handling.
    """
    prompt = task["prompt"]
    task_type = task.get("type")
    max_retries = task.get("max_retries", 2)

    # Determine complexity
    complexity = task.get("complexity") or analyse_complexity(prompt, available)
    role = determine_role(complexity, task_type)
    model = resolve_model(role, available)

    if not model:
        # No suitable local model — escalate immediately
        return {
            "success": False,
            "escalate": True,
            "reason": f"No local model available for role '{role}'",
            "complexity": complexity,
            "role": role,
            "model": None,
            "response": None,
            "attempts": 0
        }

    max_tokens = ROUTING_TABLE[role]["max_tokens"]
    last_error = None

    for attempt in range(1, max_retries + 2):  # +2: max_retries failures + 1 final attempt
        try:
            response = call_model(model, prompt, max_tokens=max_tokens)
            return {
                "success": True,
                "escalate": False,
                "complexity": complexity,
                "role": role,
                "model": model,
                "response": response,
                "attempts": attempt
            }
        except Exception as e:
            last_error = str(e)
            if attempt <= max_retries:
                time.sleep(1)
            continue

    # All retries exhausted
    return {
        "success": False,
        "escalate": True,
        "reason": f"All {max_retries + 1} attempts failed. Last error: {last_error}",
        "complexity": complexity,
        "role": role,
        "model": model,
        "response": None,
        "attempts": max_retries + 1
    }


def cmd_models(_args):
    """Refresh model list from LM Studio and print routing table with resolved models."""
    if not check_health():
        print(json.dumps({"error": "LM Studio not reachable at http://localhost:1234"}))
        sys.exit(1)
    available = fetch_models()
    table = {}
    for role, cfg in ROUTING_TABLE.items():
        resolved = resolve_model(role, available)
        table[role] = {
            "description": cfg["description"],
            "resolved_model": resolved,
            "candidates": cfg["candidates"],
            "available": resolved is not None
        }
    print(json.dumps({"available_models": available, "routing_table": table}, indent=2))


def cmd_route(args):
    """Analyse a task and return routing decision without calling the model."""
    if not check_health():
        print(json.dumps({"error": "LM Studio not reachable at http://localhost:1234"}))
        sys.exit(1)
    task = json.loads(args[0])
    available = load_cached_models() or fetch_models()
    complexity = task.get("complexity") or analyse_complexity(task["prompt"], available)
    role = determine_role(complexity, task.get("type"))
    model = resolve_model(role, available)
    print(json.dumps({
        "complexity": complexity,
        "role": role,
        "model": model,
        "description": ROUTING_TABLE.get(role, {}).get("description", "")
    }, indent=2))


def cmd_call(args):
    """Call a specific model directly."""
    if not check_health():
        print(json.dumps({"error": "LM Studio not reachable at http://localhost:1234"}))
        sys.exit(1)
    model_id, prompt = args[0], args[1]
    response = call_model(model_id, prompt)
    print(json.dumps({"model": model_id, "response": response}))


def cmd_run(args):
    """Full pipeline: route + call + retry + escalation flag."""
    if not check_health():
        print(json.dumps({
            "error": "LM Studio not reachable at http://localhost:1234",
            "escalate": True
        }))
        sys.exit(1)
    task = json.loads(args[0])
    available = load_cached_models() or fetch_models()
    result = run_with_retry(task, available)
    print(json.dumps(result, indent=2))


COMMANDS = {
    "models": cmd_models,
    "route":  cmd_route,
    "call":   cmd_call,
    "run":    cmd_run,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: delegate.py <{'|'.join(COMMANDS)}> [args...]")
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
